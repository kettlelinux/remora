"""WebSocket routes: loader/*, utilities/*, updater/* (Decky's) and remora/* (docs/PROTOCOL.md)."""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import signal
import sys
from typing import Any

import aiohttp

from .cdp import CDP
from .config import Config
from .fsutil import list_dir, open_user_file_beneath
from .plugins.manager import PluginManager
from .plugins.process import PACKAGE_PARENT
from .settings import Settings
from .store.tls import ssl_context  # noqa: F401  (server.py imports it from here)
from .wsrouter import RouteError, WSRouter

log = logging.getLogger("remora.utilities")

# extra_opts keys of utilities/http_request passed on to aiohttp; Decky passes all of them
# loader.json keys only the UI may change, through remora/settings/set: Remora's own and
# developer.enabled (it allows installs without a hash). Every other remora.* key is refused
# (the store's sources have remora/store/set_sources)
UI_SETTINGS = {"remora.storeTelemetry": bool, "remora.verbose": bool, "developer.enabled": bool}

HTTP_OPTS = {"headers", "data", "json", "params", "allow_redirects", "max_redirects", "cookies",
             "compress", "chunked", "expect100", "read_until_eof", "proxy"}




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
            "updater/do_shutdown": self.do_shutdown,
            "updater/do_update": self.noop,
            "updater/get_testing_versions": self.empty_list,
            "updater/download_testing_version": self.noop,
            "remora/status": self.status,
            "remora/logs": self.logs,
            "remora/permissions/list": self.permissions_list,
            "remora/permissions/set": self.permissions_set,
            "remora/permissions/confirm_mode": self.confirm_mode,
            "remora/settings/set": self.set_remora_setting,
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
        if not isinstance(key, str):
            raise RouteError("TypeError", "the key must be a string")
        if key.startswith("remora.") or key in UI_SETTINGS:
            # root approvals, sources and the like: not through the generic route a plugin or
            # anything driving Steam's UI can reach
            raise RouteError("PermissionError", f"{key} can't be set with utilities/settings/set")
        return self.settings.set(key, value)

    async def set_remora_setting(self, key: str, value: Any) -> Any:
        kind = UI_SETTINGS.get(key) if isinstance(key, str) else None
        if kind is None:
            raise RouteError("PermissionError", f"{key} can't be set from the UI")
        if not isinstance(value, kind):
            raise RouteError("TypeError", f"{key} takes a {kind.__name__}")
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

        keep = min(max(1, int(max_lines)), 100_000)

        def newest() -> str:
            try:
                files = sorted(p for p in os.listdir(log_dir) if p.endswith(".log"))
            except OSError:
                return ""
            if not files:
                return ""
            fd = open_user_file_beneath(log_dir, [files[-1]], self.cfg.user_home)
            with os.fdopen(fd, "rb") as f:
                # the tail only: read back from the end until there are enough lines
                size = f.seek(0, os.SEEK_END)
                pos, data = size, b""
                while pos > 0 and data.count(b"\n") <= keep:
                    step = min(pos, 64 * 1024)
                    pos -= step
                    f.seek(pos)
                    data = f.read(step) + data
                    if len(data) > 32 * 1024 * 1024:
                        break
            lines = collections.deque(data.decode("utf-8", errors="replace").splitlines(True),
                                      maxlen=keep)
            return "".join(lines)

        return await asyncio.to_thread(newest)

    async def permissions_list(self) -> list[dict[str, Any]]:
        return [{"name": e.info.name, "root": True, "approved": self.manager.root_approved(e.info.name),
                 "source": self.manager.install_source(e.info.name)}
                for e in self.manager.plugins.values() if e.info.root]

    async def permissions_set(self, name: str, approved: bool) -> dict[str, Any]:
        """false revokes at once; true asks for physical confirmation (blocks until it's given
        or times out) and starts the plugin when given."""
        if not approved:
            return await self.manager.revoke_root(name)
        return await self.manager.request_root(name)

    async def confirm_mode(self, name: str | None = None) -> dict[str, str]:
        return self.manager.confirm_mode(name)

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
        # a clean shutdown (plugins' _unload, settings flushed); systemd starts us again
        asyncio.get_running_loop().call_later(0.5, os.kill, os.getpid(), signal.SIGTERM)

    async def do_shutdown(self) -> None:
        asyncio.get_running_loop().call_later(0.5, os.kill, os.getpid(), signal.SIGTERM)

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
        args = {"path": str(path) if path else str(self.cfg.user_home),
                "include_files": include_files, "include_folders": include_folders,
                "include_ext": include_ext, "include_hidden": include_hidden, "order_by": order_by,
                "filter_for": filter_for, "page": page, "max_items": max}
        if not self.cfg.running_as_root or self.cfg.uid == 0:
            return await asyncio.to_thread(list_dir, **args)
        # as root, list as the Game Mode user: the picker shows what the user may see, not /root
        return await self._list_as_user(args)

    async def _list_as_user(self, args: dict[str, Any]) -> dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-P", "-s", "-m", "remora.fsutil",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd="/",
            env={"PATH": "/usr/bin:/bin", "LANG": os.environ.get("LANG", "C.UTF-8"),
                 "PYTHONPATH": PACKAGE_PARENT, "HOME": str(self.cfg.user_home)},
            user=self.cfg.uid, group=self.cfg.gid,
            extra_groups=os.getgrouplist(self.cfg.user, self.cfg.gid))
        try:
            out, err = await asyncio.wait_for(proc.communicate(json.dumps(args).encode()), 30)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RouteError("TimeoutError", "listing the folder took too long") from None
        try:
            answer = json.loads(out)
        except ValueError:
            raise RouteError("RuntimeError", f"listing failed: {err.decode(errors='replace')[-500:]}") from None
        if not answer.get("ok"):
            raise RouteError(str(answer.get("name") or "OSError"), str(answer.get("message") or ""))
        return answer["value"]

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
