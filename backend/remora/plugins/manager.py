"""All plugins: discovery, start/stop, enable/disable, reload, root approval, frontend events."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator

from ..config import Config
from ..confirm import ConfirmService
from ..fsutil import makedirs_as_user
from ..settings import Settings
from ..wsrouter import Discard, RouteError, WSRouter
from . import manifest
from .manifest import PluginInfo
from .process import PluginCallError, PluginProcess, PluginStopped

log = logging.getLogger("remora.plugins")

ROOT_APPROVED = "remora.rootApproved"
ROOT_MIGRATED = "remora.rootMigrated"
INSTALL_SOURCE = "remora.installSource"


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
        self.confirm = ConfirmService(cfg)
        self.plugins: dict[str, Entry] = {}
        self.scan_errors: list[tuple[str, str]] = []
        self._watch_paused = 0
        self._observer: Any = None
        self._watches: dict[str, Any] = {}  # folder -> watchdog ObservedWatch
        self._reload_timers: dict[str, asyncio.TimerHandle] = {}
        self.watch_error: OSError | None = None  # why live reload isn't watching
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        # the store's compat answer for an installed plugin (store/compat.py), set by the store
        self.compat_of: Callable[[PluginInfo], dict[str, Any]] | None = None

    # -- per-plugin lifecycle lock -----------------------------------------------------------

    @contextlib.asynccontextmanager
    async def locked(self, *names: str) -> AsyncIterator[None]:
        """Lifecycle operations on a plugin (start, stop, enable, disable, reload, load, unload,
        permission changes) run one at a time per plugin name. Several names lock in order."""
        async with contextlib.AsyncExitStack() as stack:
            for name in sorted(set(names)):
                await stack.enter_async_context(self._locks.setdefault(name, asyncio.Lock()))
            yield

    # -- settings-backed state ---------------------------------------------------------------

    def disabled(self, name: str) -> bool:
        return name in (self.settings.get("disabled_plugins") or [])

    def install_source(self, name: str) -> str | None:
        sources = self.settings.get(INSTALL_SOURCE)
        value = sources.get(name) if isinstance(sources, dict) else None
        return value if isinstance(value, str) else None

    def _approvals(self) -> list[dict[str, Any]]:
        """remora.rootApproved as [{name, source}]. The old list of names reads as approvals of
        the source each plugin is recorded with now (normally none)."""
        result: list[dict[str, Any]] = []
        for item in self.settings.get(ROOT_APPROVED) or []:
            if isinstance(item, str):
                result.append({"name": item, "source": self.install_source(item)})
            elif isinstance(item, dict) and isinstance(item.get("name"), str):
                source = item.get("source")
                result.append({"name": item["name"],
                               "source": source if isinstance(source, str) else None})
        return result

    def root_approved(self, name: str, source: str | None = None) -> bool:
        """Is there an approval for `name` from this source? source None: the plugin's recorded
        install source (none for local and migrated plugins)."""
        want = source if source is not None else self.install_source(name)
        return any(a["name"] == name and a["source"] == want for a in self._approvals())

    def set_root_approved(self, name: str, approved: bool, source: str | None = None) -> None:
        """Record or drop an approval, without asking. Only for approvals already confirmed."""
        approvals = [a for a in self._approvals() if a["name"] != name]
        if approved:
            approvals.append({"name": name,
                              "source": source if source is not None else self.install_source(name)})
        self.settings.set(ROOT_APPROVED, approvals)

    def _migrate_root_approvals(self, infos: list[PluginInfo]) -> None:
        """Coming from Decky, every root plugin already installed was trusted; keep it that way.
        Also rewrites the old list-of-names form as [{name, source}]."""
        approvals = self._approvals()
        if not self.settings.get(ROOT_MIGRATED):
            have = {a["name"] for a in approvals}
            added = sorted({i.name for i in infos if i.root} - have)
            approvals += [{"name": n, "source": self.install_source(n)} for n in added]
            self.settings.update({ROOT_APPROVED: approvals, ROOT_MIGRATED: True})
            if added:
                log.info("approved existing root plugins: %s", ", ".join(added))
        elif approvals != self.settings.get(ROOT_APPROVED):
            self.settings.set(ROOT_APPROVED, approvals)

    def approved_root_list(self) -> list[dict[str, Any]]:
        result = []
        for a in self._approvals():
            entry = self.plugins.get(a["name"])
            result.append({"name": a["name"], "source": a["source"], "installed": entry is not None,
                           "state": self.status(a["name"])["state"] if entry else None})
        return result

    # -- root confirmation -------------------------------------------------------------------

    def confirm_mode(self, name: str | None = None) -> dict[str, str]:
        """{mode: "keys"|"command"|"terminal", hint}: how root approval is confirmed here."""
        return self.confirm.info(name)

    async def confirm_root(self, name: str, *, version: str | None = None, source: str | None = None,
                           request_id: str | None = None, what: str | None = None) -> bool:
        """Ask for physical (or root terminal) confirmation that `name` may run as root. Emits
        remora/confirm_waiting and remora/confirm_done; on success records the approval for
        `source` (None: the plugin's recorded install source). One at a time."""

        async def waiting(mode: str, hint: str, until: float) -> None:
            await self.ws.emit("remora/confirm_waiting", request_id, name, mode, hint, until)

        ok = False
        try:
            ok = await self.confirm.confirm(name, version=version, source=source,
                                            request_id=request_id, what=what, on_waiting=waiting)
            if ok:
                self.set_root_approved(name, True, source)
        finally:
            await self.ws.emit("remora/confirm_done", request_id, name, ok)
        return ok

    async def request_root(self, name: str) -> dict[str, Any]:
        """remora/permissions/set(name, true): confirm, then start the plugin."""
        entry = self._entry(name)
        if not entry.info.root or self.root_approved(name):
            return {"approved": True}
        ok = await self.confirm_root(name, version=entry.info.version)
        if not ok:
            return {"approved": False, "reason": "not confirmed"}
        await self._restart_after_permission(name)
        return {"approved": True}

    async def revoke_root(self, name: str) -> dict[str, Any]:
        """remora/permissions/set(name, false): drop the approval and stop the plugin at once."""
        self._entry(name)
        self.set_root_approved(name, False)
        await self._restart_after_permission(name)
        return {"approved": False}

    async def set_root_permission(self, name: str, approved: bool, source: str | None = None) -> None:
        """Set an approval that is already confirmed (tests, the control socket) and apply it."""
        self._entry(name)
        self.set_root_approved(name, approved, source)
        await self._restart_after_permission(name)

    async def _restart_after_permission(self, name: str) -> None:
        async with self.locked(name):
            entry = self.plugins.get(name)
            if entry is None or not entry.info.root:
                return
            await self._start_entry(entry)  # stops the old host first
        await self.ws.emit("remora/plugin_status", self.status(name))

    async def approve_from_terminal(self, name: str) -> dict[str, Any]:
        """`remora approve <name>` with no request pending: approve an installed root plugin."""
        entry = self.plugins.get(name)
        if entry is None:
            return {"ok": False, "error": f"nothing pending for {name!r} and no plugin by that name"}
        if not entry.info.root:
            return {"ok": False, "error": f"{name} doesn't ask for root"}
        await self.set_root_permission(name, True)
        log.info("approved %s from the terminal", name)
        return {"ok": True, "name": name, "answered": "approved"}

    async def revoke_from_terminal(self, name: str) -> dict[str, Any]:
        if not any(a["name"] == name for a in self._approvals()):
            return {"ok": False, "error": f"nothing pending for {name!r} and it isn't approved"}
        self.set_root_approved(name, False)
        if name in self.plugins:
            await self._restart_after_permission(name)
        log.info("revoked %s from the terminal", name)
        return {"ok": True, "name": name, "answered": "revoked"}

    # -- discovery ---------------------------------------------------------------------------

    def ensure_dirs(self, info: PluginInfo) -> list[str]:
        """Make the plugin's settings, data and logs folders. Returns what failed."""
        failed = []
        for sub in ("settings", "data", "logs"):
            try:
                makedirs_as_user(self.cfg.homebrew / sub / info.dir_name, self.cfg.user_home,
                                 self.cfg.uid, self.cfg.gid)
            except OSError as e:
                log.error("%s: can't make its %s folder: %s", info.name, sub, e)
                failed.append(f"{sub}: {e}")
        return failed

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
        if existing is not None:  # the same folder again: keep its entry (and its process)
            existing.info = info
            return existing
        entry = Entry(info)
        self.plugins[info.name] = entry
        return entry

    def _make_process(self, info: PluginInfo) -> PluginProcess:
        async def on_event(event: str, args: list[Any]) -> None:
            await self.ws.emit("loader/plugin_event", {"plugin": info.name, "event": event, "args": args})

        async def on_state(proc: PluginProcess) -> None:
            entry = self.plugins.get(info.name)
            if entry is not None and entry.proc is proc:
                await self.ws.emit("remora/plugin_status", self.status(info.name))

        return PluginProcess(self.cfg, info, on_event, on_state)

    async def _stop_entry(self, entry: Entry, uninstall: bool = False) -> None:
        proc, entry.proc = entry.proc, None
        if proc is not None:
            await proc.stop(uninstall)

    async def _start_entry(self, entry: Entry) -> None:
        """Start the plugin if it should run. Always stops the host it already has first, so no
        operation leaves two hosts. Callers hold the plugin's lock."""
        await self._stop_entry(entry)
        info = entry.info
        entry.error = None
        failed = self.ensure_dirs(info)
        if self.disabled(info.name) or info.passive:
            return
        if info.root and not self.root_approved(info.name):
            entry.error = "asks for root and isn't approved"
            log.warning("%s asks for root and isn't approved; not started", info.name)
            return
        if info.root and failed and self.cfg.running_as_root:
            # its host would create log files as root in folders the user controls
            entry.error = "its folders are unsafe: " + "; ".join(failed)
            log.error("%s: not started as root, %s", info.name, entry.error)
            return
        entry.proc = self._make_process(info)
        await entry.proc.start()

    async def _start_locked(self, entry: Entry) -> None:
        async with self.locked(entry.info.name):
            await self._start_entry(entry)

    async def start(self) -> None:
        self.cfg.plugins_dir.mkdir(parents=True, exist_ok=True)
        infos, errors = manifest.scan(self.cfg.plugins_dir)
        for extra in self.cfg.extra_plugin_dirs:
            if (extra / "plugin.json").is_file():  # a single plugin's folder
                try:
                    infos.append(manifest.read_plugin(extra))
                except manifest.ManifestError as e:
                    errors.append((extra, str(e)))
                continue
            more, more_errors = manifest.scan(extra)
            infos += more
            errors += more_errors
        self.scan_errors = [(str(p), e) for p, e in errors]
        for path, error in errors:
            log.error("skipping %s: %s", path, error)
        self._migrate_root_approvals(infos)
        entries = [e for e in (self._register(i) for i in infos) if e is not None]
        await asyncio.gather(*(self._start_locked(e) for e in entries))
        if self.settings.get("pluginOrder") is None:
            self.settings.set("pluginOrder", [e.info.name for e in entries])
        log.info("loaded %d plugins", len(entries))
        self._sync_watches()

    # -- frontend-facing ---------------------------------------------------------------------

    def get_plugins(self) -> list[dict[str, Any]]:
        order = self.settings.get("pluginOrder") or []
        rank = {name: i for i, name in enumerate(order)}
        entries = sorted(self.plugins.values(), key=lambda e: rank.get(e.info.name, len(rank)))
        return [{"name": e.info.name, "version": e.info.version, "load_type": e.info.load_type,
                 "disabled": self.disabled(e.info.name), **self._compat(e.info)} for e in entries]

    def _compat(self, info: PluginInfo) -> dict[str, Any]:
        """{"compat": ...} for get_plugins and status, or nothing when the store can't say."""
        if self.compat_of is None:
            return {}
        try:
            return {"compat": self.compat_of(info)}
        except Exception:
            log.exception("compat check of %s failed", info.name)
            return {}

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
            **self._compat(info),
        }

    def statuses(self) -> list[dict[str, Any]]:
        return [self.status(name) for name in self.plugins]

    def _entry(self, name: str) -> Entry:
        try:
            return self.plugins[name]
        except (KeyError, TypeError):
            raise RouteError("KeyError", f"no plugin named {name!r}") from None

    async def _call(self, name: str, method: str, args: list[Any], kwargs: dict[str, Any],
                    legacy: bool) -> Any:
        if not isinstance(method, str):
            raise RouteError("TypeError", "the method name must be a string")
        if method.startswith("_"):
            raise RouteError("PermissionError", f"{method} is private")
        entry = self._entry(name)
        if entry.info.passive:
            raise RouteError("RuntimeError", "This plugin is passive")
        proc = entry.proc
        if proc is None:
            raise RouteError("RuntimeError", f"{name} is not running"
                             + (f": {entry.error}" if entry.error else ""))
        try:
            return await proc.call(method, args, kwargs, legacy)
        except PluginStopped as e:
            if e.deliberate:
                raise Discard() from None  # stopped on purpose: the frontend drops the call
            raise RouteError("PluginStopped", str(e)) from None

    async def call(self, name: str, method: str, *args: Any) -> Any:
        try:
            return await self._call(name, method, list(args), {}, False)
        except PluginCallError as e:
            err = e.error
            raise RouteError(str(err.get("name") or "Exception"), str(err.get("message") or ""),
                             err.get("traceback")) from None

    async def call_legacy(self, name: str, method: str, kwargs: Any = None) -> Any:
        """Legacy calls always resolve, with {success, result}: old frontends don't catch."""
        try:
            if kwargs is not None and not isinstance(kwargs, dict):
                raise TypeError("kwargs must be an object")
            value = await self._call(name, method, [], dict(kwargs or {}), True)
            return {"success": True, "result": value}
        except PluginCallError as e:
            return {"success": False, "result": str(e.error.get("message") or e)}
        except RouteError as e:
            return {"success": False, "result": e.message}
        except Discard:
            return {"success": False, "result": f"{name} stopped"}
        except Exception as e:
            return {"success": False, "result": str(e) or type(e).__name__}

    async def emit_import(self, info: PluginInfo) -> None:
        await self.ws.emit("loader/import_plugin", info.name, info.version, info.load_type, True, 15000)

    async def reload(self, name: str) -> None:
        entry = self._entry(name)
        try:
            info = manifest.read_plugin(entry.info.path)
        except manifest.ManifestError as e:
            async with self.locked(name):
                await self._stop_entry(entry)
                entry.error = str(e)
            await self.ws.emit("remora/plugin_status", self.status(name))
            raise RouteError("ManifestError", str(e)) from None
        async with self.locked(name, info.name):
            if self.plugins.get(name) is not entry:
                raise RouteError("KeyError", f"{name} went away while reloading")
            if info.name != name:
                other = self.plugins.get(info.name)
                if other is not None:
                    await self._stop_entry(entry)
                    entry.error = (f"renamed to {info.name!r}, which another plugin "
                                   f"({other.info.dir_name}) already uses; not loaded")
                    await self.ws.emit("remora/plugin_status", self.status(name))
                    raise RouteError("NameConflict", entry.error)
                await self._stop_entry(entry)
                del self.plugins[name]
                await self.ws.emit("loader/unload_plugin", name)
                new = self._register(info)
                if new is None:  # can't happen after the check above, but never index blindly
                    raise RouteError("NameConflict", f"another plugin is already called {info.name!r}")
                entry = new
            entry.info = info
            await self._start_entry(entry)
        self._sync_watches()
        await self.ws.emit("remora/plugin_status", self.status(info.name))
        if not self.disabled(info.name):
            await self.emit_import(info)

    async def load_dir(self, path: Path) -> PluginInfo:
        """Load a folder that just appeared in plugins/ (an install)."""
        info = manifest.read_plugin(path)
        async with self.locked(info.name):
            entry = self._register(info)
            if entry is None:
                raise RouteError("NameConflict", f"another plugin is already called {info.name!r}")
            await self._start_entry(entry)
        self._sync_watches()
        await self.ws.emit("remora/plugin_status", self.status(info.name))
        if not self.disabled(info.name):
            await self.emit_import(info)
        return info

    async def unload(self, name: str, uninstall: bool = False) -> None:
        """Stop a plugin and forget it (before an update or an uninstall)."""
        async with self.locked(name):
            entry = self.plugins.get(name)
            if entry is None:
                return
            await self.ws.emit("loader/unload_plugin", name)
            await self._stop_entry(entry, uninstall)
            if self.plugins.get(name) is entry:
                del self.plugins[name]
        self._sync_watches()

    async def disable(self, name: str) -> None:
        async with self.locked(name):
            entry = self._entry(name)
            self.settings.list_add("disabled_plugins", name)
            await self._stop_entry(entry)
        await self.ws.emit("loader/disable_plugin", name)
        await self.ws.emit("remora/plugin_status", self.status(name))

    async def enable(self, name: str) -> None:
        async with self.locked(name):
            entry = self._entry(name)
            was_disabled = self.disabled(name)
            self.settings.list_remove("disabled_plugins", name)
            proc = entry.proc
            running = proc is not None and proc.state.value in ("starting", "running")
            if was_disabled or not running:  # enabling a running plugin doesn't restart it
                await self._start_entry(entry)
        await self.ws.emit("remora/plugin_status", self.status(name))
        await self.emit_import(entry.info)

    async def set_all_disabled(self) -> None:
        for name in list(self.plugins):
            if not self.disabled(name):
                await self.disable(name)

    async def shutdown(self) -> None:
        self._stop_watcher()
        for task in list(self._tasks):
            task.cancel()
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

    def _watch_folders(self) -> list[str]:
        """What live reload watches: everything with reload_all (remora dev), else only the
        folders of plugins with the "debug" flag (as in Decky, only those reload on change)."""
        if not self.cfg.live_reload:
            return []
        if self.cfg.reload_all:
            folders = [self.cfg.plugins_dir, *self.cfg.extra_plugin_dirs]
        else:
            folders = [e.info.path for e in self.plugins.values() if e.info.debug]
        return sorted({str(f) for f in folders})

    def _sync_watches(self) -> None:
        """Start, stop or retarget the watcher. Never fatal: with no inotify watches left
        (ENOSPC) the loader runs on without live reload."""
        try:
            self._sync_watches_or_raise()
        except Exception as e:
            self.watch_error = e if isinstance(e, OSError) else OSError(str(e))
            hint = (" (out of inotify watches: raise fs.inotify.max_user_watches)"
                    if isinstance(e, OSError) and e.errno == errno.ENOSPC else "")
            log.warning("live reload is off: %s%s", e, hint)

    def _sync_watches_or_raise(self) -> None:
        wanted = self._watch_folders()
        if not wanted and self._observer is None:
            return
        if self._observer is None:
            try:
                from watchdog.observers import Observer
            except ImportError:
                log.info("watchdog is not installed, no live reload")
                return
            observer = Observer()
            observer.daemon = True
            observer.start()
            self._observer = observer
        observer = self._observer
        for folder in [f for f in self._watches if f not in wanted]:
            try:
                observer.unschedule(self._watches.pop(folder))
            except Exception:
                pass
        handler = None
        for folder in wanted:
            if folder not in self._watches:
                handler = handler or self._handler()
                self._watches[folder] = observer.schedule(handler, folder, recursive=True)
        self.watch_error = None

    def _handler(self) -> Any:
        from watchdog.events import FileSystemEvent, FileSystemEventHandler

        loop = asyncio.get_running_loop()
        pending = self._reload_timers
        manager = self

        def changed(path: str) -> None:
            if manager._watch_paused or "__pycache__" in path or "/node_modules/" in path:
                return
            if not (path.endswith("main.py") or path.endswith("/dist/index.js") or path.endswith(".py")):
                return
            entry = next((e for e in manager.plugins.values()
                          if path.startswith(str(e.info.path) + "/")), None)
            if entry is None or not (entry.info.debug or manager.cfg.reload_all):
                return
            name = entry.info.name
            if name in pending:
                pending[name].cancel()
            pending[name] = loop.call_later(0.5, manager._spawn_live_reload, name)

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event: FileSystemEvent) -> None:
                # watchdog also reports opens and reads; starting the plugin reads main.py
                if not event.is_directory and event.event_type in ("modified", "created", "moved",
                                                                   "deleted", "closed"):
                    loop.call_soon_threadsafe(changed, str(event.src_path))
                    # editors and sed -i save by renaming a temp file over the original
                    if getattr(event, "dest_path", ""):
                        loop.call_soon_threadsafe(changed, str(event.dest_path))

        return Handler()

    def _spawn_live_reload(self, name: str) -> None:
        self._reload_timers.pop(name, None)
        task = asyncio.ensure_future(self._live_reload(name))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _live_reload(self, name: str) -> None:
        if name not in self.plugins:
            return
        log.info("%s changed, reloading", name)
        try:
            await self.reload(name)
        except Exception:
            log.exception("reloading %s failed", name)

    def _stop_watcher(self) -> None:
        for timer in self._reload_timers.values():
            timer.cancel()
        self._reload_timers.clear()
        if self._observer is not None:
            try:
                self._observer.stop()
            except Exception:
                pass
            self._observer = None
            self._watches.clear()
