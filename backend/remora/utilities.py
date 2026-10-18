"""WebSocket routes: loader/*, utilities/*, updater/* (Decky's) and remora/* (docs/PROTOCOL.md)."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import ssl
import stat
from pathlib import Path
from typing import Any

import aiohttp
import certifi

from .cdp import CDP
from .config import Config
from .plugins.manager import PluginManager
from .settings import Settings
from .wsrouter import RouteError, WSRouter

log = logging.getLogger("remora.utilities")

# extra_opts keys of utilities/http_request passed on to aiohttp; Decky passes all of them
HTTP_OPTS = {"headers", "data", "json", "params", "allow_redirects", "max_redirects", "cookies",
             "compress", "chunked", "expect100", "read_until_eof", "proxy"}


def ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


class Utilities:
    def __init__(self, cfg: Config, settings: Settings, manager: PluginManager, cdp: CDP,
                 ws: WSRouter) -> None:
        self.cfg = cfg
        self.settings = settings
        self.manager = manager
        self.cdp = cdp
        self.ws = ws
        self._ssl = ssl_context()
        self._session: aiohttp.ClientSession | None = None
        self.legacy = {
            "ping": self.ping,
            "http_request": self.http_request,
            "execute_in_tab": self.execute_in_tab,
            "inject_css_into_tab": self.inject_css_into_tab,
            "remove_css_from_tab": self.remove_css_from_tab,
            "set_setting": self.set_setting,
            "get_setting": self.get_setting,
            "filepicker_ls": self.filepicker_ls,
            "get_tab_id": self.get_tab_id,
            "get_user_info": self.get_user_info,
        }

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(auto_decompress=True)
        return self._session

    def register(self, ws: WSRouter) -> None:
        m = self.manager
        routes = {
            "loader/get_plugins": self.get_plugins,
            "loader/reload_plugin": m.reload,
            "loader/call_plugin_method": m.call,
            "loader/call_legacy_plugin_method": m.call_legacy,
            "utilities/ping": self.ping,
            "utilities/settings/get": self.get_setting,
            "utilities/settings/set": self.set_setting,
            "utilities/get_user_info": self.get_user_info,
            "utilities/execute_in_tab": self.execute_in_tab,
            "utilities/inject_css_into_tab": self.inject_css_into_tab,
            "utilities/remove_css_from_tab": self.remove_css_from_tab,
            "utilities/get_tab_id": self.get_tab_id,
            "utilities/http_request": self.http_request,
            "utilities/_call_legacy_utility": self.call_legacy_utility,
            "utilities/filepicker_ls": self.filepicker_ls,
            "utilities/enable_plugin": m.enable,
            "utilities/disable_plugin": m.disable,
            "utilities/set_all_plugins_disabled": m.set_all_disabled,
            "utilities/restart_webhelper": self.restart_webhelper,
            "utilities/close_cef_socket": self.noop,
            "utilities/allow_remote_debugging": self.allow_remote_debugging,
            "utilities/disallow_remote_debugging": self.disallow_remote_debugging,
            "utilities/start_ssh": self.start_ssh,
            "utilities/stop_ssh": self.stop_ssh,
            "utilities/enable_rdt": self.noop,
            "utilities/disable_rdt": self.noop,
            "updater/get_version_info": self.get_version_info,
            "updater/check_for_updates": self.get_version_info,
            "updater/do_restart": self.do_restart,
            "updater/do_shutdown": self.do_restart,
            "updater/do_update": self.noop,
            "updater/get_testing_versions": self.empty_list,
            "updater/download_testing_version": self.noop,
            "remora/status": self.status,
            "remora/logs": self.logs,
            "remora/permissions/list": self.permissions_list,
            "remora/permissions/set": self.permissions_set,
        }
        for name, fn in routes.items():
            ws.add_route(name, fn)

    # -- loader ------------------------------------------------------------------------------

    async def get_plugins(self) -> list[dict[str, Any]]:
        return self.manager.get_plugins()

    async def noop(self, *args: Any) -> None:
        return None

    async def empty_list(self, *args: Any) -> list[Any]:
        return []

    async def ping(self, **kwargs: Any) -> str:
        return "pong"

    async def get_setting(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    async def set_setting(self, key: str, value: Any) -> Any:
        return self.settings.set(key, value)

    async def get_user_info(self) -> dict[str, str]:
        return {"username": self.cfg.user, "path": str(self.cfg.user_home)}

    async def get_version_info(self) -> dict[str, Any]:
        return {"current": self.cfg.decky_version, "remote": None, "all": [], "updatable": False,
                "supported": True, "repo": "kettlelinux/remora", "gitBranch": "main",
                "remora": self.cfg.version}

    async def status(self) -> dict[str, Any]:
        return {"version": self.cfg.version, "deckyVersion": self.cfg.decky_version,
                "plugins": self.manager.statuses(),
                "scanErrors": [{"path": p, "error": e} for p, e in self.manager.scan_errors]}

    async def logs(self, name: str, max_lines: int = 500) -> str:
        entry = self.manager.plugins.get(name)
        if entry is None:
            raise RouteError("KeyError", f"no plugin named {name!r}")
        log_dir = self.cfg.homebrew / "logs" / entry.info.dir_name

        def newest() -> str:
            try:
                files = sorted(p for p in os.listdir(log_dir) if p.endswith(".log"))
            except OSError:
                return ""
            if not files:
                return ""
            from .fsutil import open_user_file_beneath
            fd = open_user_file_beneath(log_dir, [files[-1]], self.cfg.user_home)
            with os.fdopen(fd, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            return "".join(lines[-max(1, int(max_lines)):])

        return await asyncio.to_thread(newest)

    async def permissions_list(self) -> list[dict[str, Any]]:
        return [{"name": e.info.name, "root": True, "approved": self.manager.root_approved(e.info.name)}
                for e in self.manager.plugins.values() if e.info.root]

    async def permissions_set(self, name: str, approved: bool) -> None:
        await self.manager.set_root_permission(name, bool(approved))

    # -- CEF ---------------------------------------------------------------------------------

    async def execute_in_tab(self, tab: str, run_async: bool, code: str) -> dict[str, Any]:
        return await self.cdp.execute_in_tab(tab, run_async, code)

    async def inject_css_into_tab(self, tab: str, style: str) -> str:
        return await self.cdp.inject_css(tab, style)

    async def remove_css_from_tab(self, tab: str, css_id: str) -> None:
        await self.cdp.remove_css(tab, css_id)

    async def get_tab_id(self, name: str) -> str:
        return (await self.cdp.target_by_title(name)).id

    async def restart_webhelper(self) -> None:
        proc = await asyncio.create_subprocess_exec("pkill", "-TERM", "-x", "steamwebhelper")
        await proc.wait()

    # -- services ----------------------------------------------------------------------------

    async def _systemctl(self, action: str, unit: str) -> None:
        proc = await asyncio.create_subprocess_exec("systemctl", action, unit,
                                                    stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()

    async def allow_remote_debugging(self) -> None:
        await self._systemctl("start", "steam-web-debug-portforward.service")

    async def disallow_remote_debugging(self) -> None:
        await self._systemctl("stop", "steam-web-debug-portforward.service")

    async def start_ssh(self) -> None:
        await self._systemctl("start", "sshd.service")

    async def stop_ssh(self) -> None:
        await self._systemctl("stop", "sshd.service")

    async def do_restart(self) -> None:
        # systemd starts us again (Restart=always)
        asyncio.get_running_loop().call_later(0.5, os._exit, 0)

    # -- HTTP --------------------------------------------------------------------------------

    async def http_request(self, method: str = "GET", url: str = "", extra_opts: dict[str, Any] | None = None,
                           timeout: float | None = None, **kwargs: Any) -> dict[str, Any]:
        opts = {k: v for k, v in (extra_opts or {}).items() if k in HTTP_OPTS}
        dropped = set(extra_opts or {}) - HTTP_OPTS
        if dropped:
            log.debug("http_request: ignoring options %s", ", ".join(sorted(dropped)))
        client_timeout = aiohttp.ClientTimeout(total=float(timeout)) if timeout else None
        async with self.session.request(method, url, ssl=self._ssl, timeout=client_timeout, **opts) as res:
            body = await res.text(errors="replace")
            return {"status": res.status, "headers": dict(res.headers), "body": body}

    async def call_legacy_utility(self, name: str, kwargs: dict[str, Any] | None = None) -> dict[str, Any]:
        fn = self.legacy.get(name)
        if fn is None:
            return {"success": False, "result": f"no utility named {name}"}
        try:
            return {"success": True, "result": await fn(**(kwargs or {}))}
        except Exception as e:
            return {"success": False, "result": str(e)}

    # -- file picker -------------------------------------------------------------------------

    async def filepicker_ls(self, path: str | None = None, include_files: bool = True,
                            include_folders: bool = True, include_ext: list[str] | None = None,
                            include_hidden: bool = False, order_by: str = "name_asc",
                            filter_for: str | None = None, page: int = 1, max: int = 1000) -> dict[str, Any]:
        return await asyncio.to_thread(self._ls, path, include_files, include_folders, include_ext,
                                       include_hidden, order_by, filter_for, page, max)

    def _ls(self, path: str | None, include_files: bool, include_folders: bool,
            include_ext: list[str] | None, include_hidden: bool, order_by: str,
            filter_for: str | None, page: int, max_items: int) -> dict[str, Any]:
        base = Path(path) if path else self.cfg.user_home
        real = os.path.realpath(base)
        exts = [e.lower().lstrip(".") for e in (include_ext or ["all_files"])]
        all_files = "all_files" in exts
        matcher = None
        if filter_for:
            try:
                rx = re.compile(filter_for)
                matcher = lambda n: rx.search(n) is not None  # noqa: E731
            except re.error:
                matcher = lambda n: filter_for in n  # noqa: E731
        files: list[dict[str, Any]] = []
        with os.scandir(real) as it:
            for entry in it:
                if not include_hidden and entry.name.startswith("."):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                is_dir = stat.S_ISDIR(st.st_mode)
                if is_dir and not include_folders:
                    continue
                if not is_dir:
                    if not include_files:
                        continue
                    if not all_files and entry.name.rsplit(".", 1)[-1].lower() not in exts:
                        continue
                if matcher is not None and not matcher(entry.name):
                    continue
                files.append({"isdir": is_dir, "name": entry.name,
                              "realpath": os.path.realpath(entry.path), "size": st.st_size,
                              "modified": st.st_mtime, "created": getattr(st, "st_birthtime", st.st_ctime)})
        field, _, direction = (order_by or "name_asc").partition("_")
        key = {"name": lambda f: f["name"].lower(), "modified": lambda f: f["modified"],
               "created": lambda f: f["created"], "size": lambda f: f["size"]}.get(field,
                                                                                   lambda f: f["name"].lower())
        files.sort(key=key, reverse=direction == "desc")
        files.sort(key=lambda f: not f["isdir"])  # folders first, stable
        page = max(1, int(page or 1))
        max_items = max(1, int(max_items or 1000))
        start = (page - 1) * max_items
        return {"realpath": real, "files": files[start:start + max_items], "total": len(files)}

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
