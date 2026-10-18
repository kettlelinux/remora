"""All plugins: discovery, start/stop, enable/disable, reload, root approval, frontend events."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..config import Config
from ..fsutil import makedirs_as_user
from ..settings import Settings
from ..wsrouter import Discard, RouteError, WSRouter
from . import manifest
from .manifest import PluginInfo
from .process import PluginCallError, PluginProcess, PluginStopped

log = logging.getLogger("remora.plugins")

ROOT_APPROVED = "remora.rootApproved"
ROOT_MIGRATED = "remora.rootMigrated"


@dataclass
class Entry:
    info: PluginInfo
    proc: PluginProcess | None = None
    error: str | None = None  # why it isn't running, when no process explains it


class PluginManager:
    def __init__(self, cfg: Config, settings: Settings, ws: WSRouter) -> None:
        self.cfg = cfg
        self.settings = settings
        self.ws = ws
        self.plugins: dict[str, Entry] = {}
        self.scan_errors: list[tuple[str, str]] = []
        self._watch_paused = 0
        self._observer: Any = None
        self._reload_lock = asyncio.Lock()

    # -- settings-backed state ---------------------------------------------------------------

    def disabled(self, name: str) -> bool:
        return name in (self.settings.get("disabled_plugins") or [])

    def root_approved(self, name: str) -> bool:
        return name in (self.settings.get(ROOT_APPROVED) or [])

    def set_root_approved(self, name: str, approved: bool) -> None:
        if approved:
            self.settings.list_add(ROOT_APPROVED, name)
        else:
            self.settings.list_remove(ROOT_APPROVED, name)

    def _migrate_root_approvals(self, infos: list[PluginInfo]) -> None:
        """Coming from Decky, every root plugin already installed was trusted; keep it that way."""
        if self.settings.get(ROOT_MIGRATED):
            return
        approved = sorted({i.name for i in infos if i.root} | set(self.settings.get(ROOT_APPROVED) or []))
        self.settings.update({ROOT_APPROVED: approved, ROOT_MIGRATED: True})
        if approved:
            log.info("approved existing root plugins: %s", ", ".join(approved))

    # -- discovery ---------------------------------------------------------------------------

    def ensure_dirs(self, info: PluginInfo) -> None:
        for sub in ("settings", "data", "logs"):
            try:
                makedirs_as_user(self.cfg.homebrew / sub / info.dir_name, self.cfg.user_home,
                                 self.cfg.uid, self.cfg.gid)
            except OSError as e:
                log.error("%s: can't make its %s folder: %s", info.name, sub, e)

    def find_dir(self, dir_name: str) -> Entry | None:
        return next((e for e in self.plugins.values() if e.info.dir_name == dir_name), None)

    def _register(self, info: PluginInfo) -> Entry | None:
        existing = self.plugins.get(info.name)
        if existing is not None and existing.info.dir_name != info.dir_name:
            message = (f"{info.dir_name} is also called {info.name!r}, like {existing.info.dir_name}; "
                       "not loaded")
            log.error(message)
            self.scan_errors.append((info.dir_name, message))
            return None
        entry = Entry(info)
        self.plugins[info.name] = entry
        return entry

    def _make_process(self, info: PluginInfo) -> PluginProcess:
        async def on_event(event: str, args: list[Any]) -> None:
            await self.ws.emit("loader/plugin_event", {"plugin": info.name, "event": event, "args": args})

        async def on_state(proc: PluginProcess) -> None:
            await self.ws.emit("remora/plugin_status", self.status(info.name))

        return PluginProcess(self.cfg, info, on_event, on_state)

    async def _start_entry(self, entry: Entry) -> None:
        info = entry.info
        entry.error = None
        self.ensure_dirs(info)
        if self.disabled(info.name) or info.passive:
            entry.proc = None
            return
        if info.root and not self.root_approved(info.name):
            entry.proc = None
            entry.error = "asks for root and isn't approved"
            log.warning("%s asks for root and isn't approved; not started", info.name)
            return
        entry.proc = self._make_process(info)
        await entry.proc.start()

    async def start(self) -> None:
        self.cfg.plugins_dir.mkdir(parents=True, exist_ok=True)
        infos, errors = manifest.scan(self.cfg.plugins_dir)
        for extra in self.cfg.extra_plugin_dirs:
            more, more_errors = manifest.scan(extra)
            infos += more
            errors += more_errors
        self.scan_errors = [(str(p), e) for p, e in errors]
        for path, error in errors:
            log.error("skipping %s: %s", path, error)
        self._migrate_root_approvals(infos)
        entries = [e for e in (self._register(i) for i in infos) if e is not None]
        await asyncio.gather(*(self._start_entry(e) for e in entries))
        if self.settings.get("pluginOrder") is None:
            self.settings.set("pluginOrder", [e.info.name for e in entries])
        log.info("loaded %d plugins", len(entries))
        self._start_watcher()

    # -- frontend-facing ---------------------------------------------------------------------

    def get_plugins(self) -> list[dict[str, Any]]:
        order = self.settings.get("pluginOrder") or []
        rank = {name: i for i, name in enumerate(order)}
        entries = sorted(self.plugins.values(), key=lambda e: rank.get(e.info.name, len(rank)))
        return [{"name": e.info.name, "version": e.info.version, "load_type": e.info.load_type,
                 "disabled": self.disabled(e.info.name)} for e in entries]

    def status(self, name: str) -> dict[str, Any]:
        entry = self.plugins[name]
        info = entry.info
        if self.disabled(name):
            state, error = "disabled", None
        elif info.passive:
            state, error = "passive", None
        elif entry.proc is None:
            state, error = ("blocked" if info.root and not self.root_approved(name) else "stopped"), entry.error
        else:
            state, error = entry.proc.state.value, entry.proc.error
        return {
            "name": name, "dir": info.dir_name, "version": info.version, "loadType": info.load_type,
            "state": state, "restarts": entry.proc.restarts if entry.proc else 0, "error": error,
            "root": info.root, "approved": (not info.root) or self.root_approved(name),
        }

    def statuses(self) -> list[dict[str, Any]]:
        return [self.status(name) for name in self.plugins]

    def _entry(self, name: str) -> Entry:
        try:
            return self.plugins[name]
        except KeyError:
            raise RouteError("KeyError", f"no plugin named {name!r}") from None

    async def _call(self, name: str, method: str, args: list[Any], kwargs: dict[str, Any],
                    legacy: bool) -> Any:
        if method.startswith("_"):
            raise RouteError("PermissionError", f"{method} is private")
        entry = self._entry(name)
        if entry.info.passive:
            raise RouteError("RuntimeError", "This plugin is passive")
        if entry.proc is None:
            raise RouteError("RuntimeError", f"{name} is not running"
                             + (f": {entry.error}" if entry.error else ""))
        try:
            return await entry.proc.call(method, args, kwargs, legacy)
        except PluginStopped:
            raise Discard() from None

    async def call(self, name: str, method: str, *args: Any) -> Any:
        try:
            return await self._call(name, method, list(args), {}, False)
        except PluginCallError as e:
            err = e.error
            raise RouteError(str(err.get("name") or "Exception"), str(err.get("message") or ""),
                             err.get("traceback")) from None

    async def call_legacy(self, name: str, method: str, kwargs: dict[str, Any] | None = None) -> Any:
        try:
            value = await self._call(name, method, [], dict(kwargs or {}), True)
            return {"success": True, "result": value}
        except PluginCallError as e:
            return {"success": False, "result": str(e.error.get("message") or e)}
        except RouteError as e:
            return {"success": False, "result": e.message}

    async def emit_import(self, info: PluginInfo) -> None:
        await self.ws.emit("loader/import_plugin", info.name, info.version, info.load_type, True, 15000)

    async def reload(self, name: str) -> None:
        async with self._reload_lock:
            entry = self._entry(name)
            if entry.proc is not None:
                await entry.proc.stop()
            try:
                info = manifest.read_plugin(entry.info.path)
            except manifest.ManifestError as e:
                entry.error = str(e)
                entry.proc = None
                await self.ws.emit("remora/plugin_status", self.status(name))
                raise RouteError("ManifestError", str(e)) from None
            if info.name != name:
                del self.plugins[name]
                self._register(info)
                await self.ws.emit("loader/unload_plugin", name)
            entry = self.plugins[info.name]
            entry.info = info
            await self._start_entry(entry)
            await self.ws.emit("remora/plugin_status", self.status(info.name))
            if not self.disabled(info.name):
                await self.emit_import(info)

    async def load_dir(self, path: Path) -> PluginInfo:
        """Load a folder that just appeared in plugins/ (an install)."""
        info = manifest.read_plugin(path)
        entry = self._register(info)
        if entry is None:
            raise RouteError("NameConflict", f"another plugin is already called {info.name!r}")
        await self._start_entry(entry)
        await self.ws.emit("remora/plugin_status", self.status(info.name))
        if not self.disabled(info.name):
            await self.emit_import(info)
        return info

    async def unload(self, name: str, uninstall: bool = False) -> None:
        """Stop a plugin and forget it (before an update or an uninstall)."""
        entry = self.plugins.get(name)
        if entry is None:
            return
        await self.ws.emit("loader/unload_plugin", name)
        if entry.proc is not None:
            await entry.proc.stop(uninstall)
        del self.plugins[name]

    async def disable(self, name: str) -> None:
        entry = self._entry(name)
        self.settings.list_add("disabled_plugins", name)
        if entry.proc is not None:
            await entry.proc.stop()
            entry.proc = None
        await self.ws.emit("loader/disable_plugin", name)
        await self.ws.emit("remora/plugin_status", self.status(name))

    async def enable(self, name: str) -> None:
        entry = self._entry(name)
        self.settings.list_remove("disabled_plugins", name)
        await self._start_entry(entry)
        await self.ws.emit("remora/plugin_status", self.status(name))
        await self.emit_import(entry.info)

    async def set_all_disabled(self) -> None:
        for name in list(self.plugins):
            if not self.disabled(name):
                await self.disable(name)

    async def set_root_permission(self, name: str, approved: bool) -> None:
        entry = self._entry(name)
        self.set_root_approved(name, approved)
        if not entry.info.root:
            return
        if entry.proc is not None:
            await entry.proc.stop()
            entry.proc = None
        await self._start_entry(entry)
        await self.ws.emit("remora/plugin_status", self.status(name))

    async def shutdown(self) -> None:
        self._stop_watcher()
        procs = [e.proc for e in self.plugins.values() if e.proc is not None]
        await asyncio.gather(*(p.stop() for p in procs), return_exceptions=True)

    # -- live reload -------------------------------------------------------------------------

    @contextlib.contextmanager
    def watch_paused(self) -> Iterator[None]:
        """No live reloads while the store rewrites plugin folders."""
        self._watch_paused += 1
        try:
            yield
        finally:
            self._watch_paused -= 1

    def _start_watcher(self) -> None:
        if not self.cfg.live_reload:
            return
        try:
            from watchdog.events import FileSystemEvent, FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            log.info("watchdog is not installed, no live reload")
            return
        loop = asyncio.get_running_loop()
        pending: dict[str, asyncio.TimerHandle] = {}
        manager = self

        def changed(path: str) -> None:
            if manager._watch_paused or "__pycache__" in path:
                return
            if not (path.endswith("main.py") or path.endswith("/dist/index.js") or path.endswith(".py")):
                return
            try:
                rel = Path(path).relative_to(manager.cfg.plugins_dir)
            except ValueError:
                return
            entry = manager.find_dir(rel.parts[0]) if rel.parts else None
            if entry is None or not entry.info.debug:
                return  # as in Decky, only "debug" plugins reload on their own
            name = entry.info.name
            if name in pending:
                pending[name].cancel()
            pending[name] = loop.call_later(0.5, lambda: asyncio.ensure_future(manager._live_reload(name)))

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event: FileSystemEvent) -> None:
                # watchdog also reports opens and reads; starting the plugin reads main.py
                if not event.is_directory and event.event_type in ("modified", "created", "moved",
                                                                   "deleted", "closed"):
                    loop.call_soon_threadsafe(changed, str(event.src_path))

        observer = Observer()
        observer.schedule(Handler(), str(self.cfg.plugins_dir), recursive=True)
        observer.daemon = True
        observer.start()
        self._observer = observer

    async def _live_reload(self, name: str) -> None:
        if name not in self.plugins:
            return
        log.info("%s changed, reloading", name)
        try:
            await self.reload(name)
        except Exception:
            log.exception("reloading %s failed", name)

    def _stop_watcher(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer = None
