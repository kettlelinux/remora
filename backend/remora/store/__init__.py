"""The plugin store: sources, listing, install, update, uninstall (docs/PROTOCOL.md)."""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

import aiohttp
from yarl import URL

from .. import device as device_mod
from ..config import Config
from ..plugins.manager import PluginManager
from ..settings import Settings
from ..wsrouter import RouteError, WSRouter
from .compat import Compat, CompatList, blocked
from .index import Fetcher, Listing, SourceError, is_newer
from .install import INSTALL_SOURCE, InstallItem, Installer
from .sources import Sources, StoreSource
from .tls import ssl_context

log = logging.getLogger("remora.store")

TELEMETRY_KEY = "remora.storeTelemetry"
SORT_KEYS = ("name", "date", "downloads")

Results = list[tuple[StoreSource, Listing | None, str | None]]


def _matches(plugin: dict[str, Any], query: str) -> bool:
    q = query.lower()
    fields = [plugin.get("name"), plugin.get("description"), plugin.get("author"),
              *(plugin.get("tags") or [])]
    return any(q in str(f).lower() for f in fields if f)


def _sort_key(sort_by: str) -> Any:
    if sort_by == "date":
        return lambda p: str(p.get("updated") or (p.get("versions") or [{}])[0].get("created") or "")
    if sort_by == "downloads":
        return lambda p: p.get("downloads") if isinstance(p.get("downloads"), int) else 0
    return lambda p: str(p.get("name") or "").lower()


class StoreService:
    def __init__(self, cfg: Config, settings: Settings, manager: PluginManager, ws: WSRouter) -> None:
        self.cfg = cfg
        self.settings = settings
        self.manager = manager
        self.ws = ws
        self.sources = Sources(settings)
        self._ssl = ssl_context()
        self._http: aiohttp.ClientSession | None = None
        self.compat = Compat(device_mod.current(str(cfg.system_root)), CompatList.load(cfg.compat_paths))
        self.fetcher = Fetcher(self.session, self._ssl, cfg.decky_version,
                               developer=lambda: bool(settings.get("developer.enabled")),
                               arch=lambda: self.compat.device.arch)
        self.installer = Installer(cfg, settings, manager, ws, self.session, self._ssl,
                                   after_install=self._telemetry, compat=self.compat)
        manager.compat_of = lambda info: self.compat.installed(info.name, info.version, info.path)
        # before the plugin manager loads anything: undo what a crash cut short
        self.installer.recover()
        self._requests: dict[str, list[InstallItem]] = {}
        self._running: dict[str, asyncio.Task[Any]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    def session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(auto_decompress=True)
        return self._http

    def register(self, ws: WSRouter) -> None:
        routes = {
            "utilities/install_plugin": self.install_plugin,
            "utilities/install_plugins": self.install_plugins,
            "utilities/confirm_plugin_install": self.confirm_plugin_install,
            "utilities/cancel_plugin_install": self.cancel_plugin_install,
            "utilities/uninstall_plugin": self.uninstall_plugin,
            "remora/store/sources": self.get_sources,
            "remora/store/set_sources": self.set_sources,
            "remora/store/list": self.list,
            "remora/store/updates": self.updates,
            "remora/store/answer_root": self.answer_root,
            "remora/store/rollback": self.rollback,
            "remora/store/previous": self.previous,
            "remora/device": self.device,
        }
        for name, fn in routes.items():
            ws.add_route(name, fn)

    async def close(self) -> None:
        for task in list(self._tasks) + list(self._running.values()):
            task.cancel()
        if self._http is not None:
            await self._http.close()

    def status(self) -> dict[str, Any]:
        return {"sources": [s.to_json() for s in self.sources.all()],
                "pendingRequests": len(self._requests), "runningInstalls": len(self._running)}

    # -- sources -----------------------------------------------------------------------------

    async def get_sources(self) -> list[dict[str, Any]]:
        return [s.to_json() for s in self.sources.all()]

    async def set_sources(self, sources: Any) -> list[dict[str, Any]]:
        result = self.sources.set(sources)
        self.fetcher.clear()
        return [s.to_json() for s in result]

    async def device(self) -> dict[str, Any]:
        return {**self.compat.device.to_json(), "compat_lists": list(self.compat.lists.files)}

    # -- listing -----------------------------------------------------------------------------

    async def _listings(self) -> Results:
        async def one(source: StoreSource) -> tuple[StoreSource, Listing | None, str | None]:
            try:
                return source, await self.fetcher.listing(source), None
            except SourceError as e:
                log.warning("store source %s: %s", source.id, e)
                return source, None, str(e)
            except Exception as e:
                log.exception("store source %s failed", source.id)
                return source, None, f"{type(e).__name__}: {e}"

        return list(await asyncio.gather(*(one(s) for s in self.sources.enabled())))

    async def list(self, sort_by: str | None = None, sort_direction: str | None = None,
                   query: str | None = None) -> dict[str, Any]:
        status: list[dict[str, Any]] = []
        plugins: list[dict[str, Any]] = []
        for source, listing, error in await self._listings():
            entry: dict[str, Any] = {"id": source.id, "ok": error is None}
            if error is not None:
                entry["error"] = error
            status.append(entry)
            if listing is not None:
                plugins += [p for p in listing.plugins if p.get("visible", True) is not False]
        if query:
            plugins = [p for p in plugins if _matches(p, query)]
        # all of them, with `compat`: the UI hides unsupported and broken ones unless
        # remora.storeShowIncompatible is on
        self.compat.annotate(plugins)
        sort_by = sort_by if sort_by in SORT_KEYS else "name"
        plugins.sort(key=_sort_key(sort_by), reverse=sort_direction == "desc")
        return {"sources": status, "plugins": plugins}

    def _find_version(self, results: Results, name: str, sha256: str
                      ) -> tuple[StoreSource, dict[str, Any], dict[str, Any]] | None:
        sha256 = sha256.strip().lower()
        installed_from = (self.settings.get(INSTALL_SOURCE) or {}).get(name)
        # the source the plugin came from wins when several carry the same zip
        for source, listing, _ in sorted(results, key=lambda r: r[0].id != installed_from):
            if listing is None:
                continue
            for plugin in listing.plugins:
                if plugin.get("name") != name:
                    continue
                for version in plugin.get("versions") or []:
                    if str(version.get("hash", "")).lower() == sha256:
                        return source, plugin, version
        return None

    # -- updates -----------------------------------------------------------------------------

    async def updates(self) -> dict[str, dict[str, Any]]:
        sources = self.sources.enabled()

        async def versions(source: StoreSource) -> tuple[str, dict[str, list[Any]]]:
            try:
                return source.id, await self.fetcher.versions(source)
            except SourceError as e:
                log.info("update check on %s: %s", source.id, e)
            except Exception:
                log.exception("update check on %s failed", source.id)
            return source.id, {}

        maps = dict(await asyncio.gather(*(versions(s) for s in sources)))
        order = [s.id for s in sources]
        recorded = self.settings.get(INSTALL_SOURCE) or {}
        frozen = set(self.settings.get("frozenPlugins") or [])
        developer = bool(self.settings.get("developer.enabled"))
        result: dict[str, dict[str, Any]] = {}
        for name, entry in self.manager.plugins.items():
            if name in frozen or not self.installer.managed(entry.info):
                continue
            home = recorded.get(name)
            # only its own source; any enabled one when that is unknown, disabled or removed
            candidates = [home] if home in maps else order
            for source_id in candidates:
                remote = maps.get(source_id, {}).get(name)
                if remote is None:
                    continue
                # an update this device can't run is no update (developer mode still offers it)
                if is_newer(remote[0], entry.info.version) \
                        and (developer or not blocked(self.compat.check(name, remote[0]))):
                    result[name] = {"version": remote[0], "hash": remote[1], "artifact": remote[2],
                                    "source": source_id}
                break
        return result

    # -- install requests --------------------------------------------------------------------

    async def _make_item(self, raw: dict[str, Any], results: Results | None,
                         refetched: list[bool] | None = None) -> InstallItem:
        artifact = raw.get("artifact")
        if not isinstance(artifact, str) or not artifact:
            raise RouteError("ValueError", "install needs an artifact URL")
        try:
            install_type = int(raw.get("install_type") or 0)
        except (TypeError, ValueError):
            install_type = 0
        item = InstallItem(artifact=artifact, name=str(raw.get("name") or "No name"),
                           version=str(raw.get("version") or "dev"), hash=str(raw.get("hash") or ""),
                           install_type=install_type)
        found = self._find_version(results, item.name, item.hash) if item.hash and results else None
        if found is None and item.hash and results is not None \
                and (refetched is None or not refetched[0]):
            # maybe published after the list was cached: once per request, and only lists that
            # are not fresh anyway (failures stay cached for a minute)
            if refetched is not None:
                refetched[0] = True
            if self.fetcher.drop_stale():
                results[:] = await self._listings()
                found = self._find_version(results, item.name, item.hash)
        if found is not None:
            source, plugin, version = found
            item.source = source.id
            item.signed = version.get("signed", False)
            item.size = version.get("size") if isinstance(version.get("size"), int) else None
            item.compatible = bool(version.get("compatible", True))
            item.incompatible_reason = version.get("incompatible_reason")
            item.compat = self.compat.check(item.name, item.version, version.get("arch"))
            flags = version.get("flags")
            if isinstance(flags, list):
                item.root = "root" in flags
            elif "root" in (plugin.get("tags") or []):
                item.root = True
        else:
            item.compat = self.compat.check(item.name, item.version)
        return item

    async def _emit_details(self, request_id: str, items: list[InstallItem]) -> None:
        await self.ws.emit("remora/install_request", request_id, [i.details() for i in items])

    async def install_plugin(self, artifact: str = "", name: str = "No name", version: str = "dev",
                             hash: str = "", install_type: int = 0) -> None:
        results = await self._listings() if hash else None
        item = await self._make_item({"artifact": artifact, "name": name, "version": version,
                                      "hash": hash, "install_type": install_type}, results, [False])
        request_id = secrets.token_hex(8)
        self._requests[request_id] = [item]
        await self.ws.emit("loader/add_plugin_install_prompt", item.name, item.version, request_id,
                           item.hash, item.install_type)
        await self._emit_details(request_id, [item])

    async def install_plugins(self, requests: list[dict[str, Any]]) -> None:
        if not isinstance(requests, list) or not requests:
            raise RouteError("ValueError", "install_plugins takes a list of plugins")
        results = await self._listings()
        refetched = [False]
        items = [await self._make_item(r if isinstance(r, dict) else {}, results, refetched)
                 for r in requests]
        request_id = secrets.token_hex(8)
        self._requests[request_id] = items
        await self.ws.emit("loader/add_multiple_plugins_install_prompt", request_id,
                           [i.prompt() for i in items])
        await self._emit_details(request_id, items)

    async def confirm_plugin_install(self, request_id: str) -> None:
        request_id = str(request_id)
        items = self._requests.pop(request_id, None)
        if items is None:
            raise RouteError("KeyError", f"no install request {request_id!r}")

        async def run() -> None:
            for item in items:
                await self.installer.install(item)

        task = asyncio.ensure_future(run())
        self._running[request_id] = task
        try:
            # shielded: a dropped WebSocket doesn't cut an install short, only cancel does
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                return  # cancel_plugin_install; the installer rolled back and reported it
            raise
        finally:
            if task.done():
                self._running.pop(request_id, None)
            else:
                task.add_done_callback(lambda _: self._running.pop(request_id, None))

    async def cancel_plugin_install(self, request_id: str) -> None:
        """Drops a pending request, or stops a confirmed one that is still running (the old
        version stays, or comes back)."""
        request_id = str(request_id)
        self._requests.pop(request_id, None)
        task = self._running.get(request_id)
        if task is not None and not task.done():
            log.info("cancelling install request %s", request_id)
            task.cancel()

    async def uninstall_plugin(self, name: str) -> None:
        await self.installer.uninstall(name)

    async def rollback(self, name: str) -> dict[str, Any]:
        return await self.installer.rollback(str(name))

    async def previous(self, name: str) -> dict[str, Any] | None:
        return self.installer.previous(str(name))

    async def answer_root(self, request_id: str, allow: bool) -> None:
        self.installer.answer_root(str(request_id), allow is True)

    # -- telemetry ---------------------------------------------------------------------------

    async def _telemetry(self, item: InstallItem, is_update: bool) -> None:
        """Decky's install counter (compat/store.md §1.5), off unless the user turns it on."""
        if not self.settings.get(TELEMETRY_KEY, False) or item.source is None:
            return
        source = self.sources.get(item.source)
        if source is None or source.kind != "decky":
            return
        url = (URL(source.decky_base) / item.name / "versions" / item.version / "increment") \
            .with_query({"isUpdate": str(is_update)})

        async def post() -> None:
            try:
                async with self.session().post(url, headers={"X-Decky-Version": self.cfg.decky_version},
                                               ssl=self._ssl,
                                               timeout=aiohttp.ClientTimeout(total=15)) as res:
                    if res.status != 200:
                        log.info("install counter for %s: HTTP %d", item.name, res.status)
            except Exception as e:
                log.info("install counter for %s failed: %s", item.name, e)

        task = asyncio.ensure_future(post())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
