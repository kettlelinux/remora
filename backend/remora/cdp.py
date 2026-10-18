"""Steam's CEF devtools (port 8080): injecting the frontend, and running JS or CSS in a tab
(compat/frontend.md §1)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

import aiohttp

from .config import Config

log = logging.getLogger("remora.cdp")

GAMEPADUI_TITLES = ("SharedJSContext", "Steam Shared Context presented by Valve™", "Steam", "SP")
GAMEPADUI_URLS = ("https://steamloopback.host/routes/", "https://steamloopback.host/index.html")


@dataclass
class Target:
    id: str
    title: str
    url: str
    ws_url: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Target":
        return cls(data.get("id", ""), data.get("title", ""), data.get("url", ""),
                   data.get("webSocketDebuggerUrl", ""))

    @property
    def is_gamepadui(self) -> bool:
        return self.title in GAMEPADUI_TITLES and any(u in self.url for u in GAMEPADUI_URLS)


class CDP:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._session: aiohttp.ClientSession | None = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def targets(self) -> list[Target]:
        async with self.session.get(f"{self.cfg.cef_url}/json",
                                    timeout=aiohttp.ClientTimeout(total=3)) as res:
            return [Target.from_json(t) for t in await res.json(content_type=None)]

    async def target_by_title(self, title: str) -> Target:
        for target in await self.targets():
            if target.title == title:
                return target
        raise LookupError(f"Tab {title} not found")

    async def evaluate(self, target: Target, expression: str, await_promise: bool = False) -> dict[str, Any]:
        """One Runtime.evaluate over a short-lived connection. Returns CDP's result object."""
        return await asyncio.wait_for(self._evaluate(target, expression, await_promise), 30)

    async def _evaluate(self, target: Target, expression: str, await_promise: bool) -> dict[str, Any]:
        async with self.session.ws_connect(target.ws_url, max_msg_size=0) as ws:
            await ws.send_json({"id": 1, "method": "Runtime.evaluate", "params": {
                "expression": expression, "userGesture": True, "awaitPromise": await_promise,
                "returnByValue": True}})
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                data = json.loads(msg.data)
                if data.get("id") == 1:
                    return data.get("result") or {}
        raise ConnectionError("devtools closed the connection")

    # -- utilities backed by CDP (compat/backend.md §6.2) ------------------------------------

    async def execute_in_tab(self, tab: str, run_async: bool, code: str) -> dict[str, Any]:
        try:
            target = await self.target_by_title(tab)
            result = await self.evaluate(target, code, bool(run_async))
        except Exception as e:
            return {"success": False, "result": str(e)}
        if "exceptionDetails" in result:
            return {"success": False, "result": result["exceptionDetails"]}
        return {"success": True, "result": (result.get("result") or {}).get("value")}

    async def inject_css(self, tab: str, css: str) -> str:
        css_id = str(uuid.uuid4())
        code = ("(function(){const s=document.createElement('style');s.id=%s;"
                "document.head.append(s);s.textContent=%s;})()") % (json.dumps(css_id), json.dumps(css))
        result = await self.execute_in_tab(tab, False, code)
        if not result["success"]:
            raise RuntimeError(f"could not inject CSS into {tab}: {result['result']}")
        return css_id

    async def remove_css(self, tab: str, css_id: str) -> None:
        code = "(function(){const s=document.getElementById(%s);if(s)s.remove();})()" % json.dumps(css_id)
        result = await self.execute_in_tab(tab, False, code)
        if not result["success"]:
            raise RuntimeError(f"could not remove CSS from {tab}: {result['result']}")

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


class Injector:
    """Keeps the frontend injected into GamepadUI's shared JS context."""

    CRASH_WINDOW = 60.0
    FALLBACK_AFTER = 3
    PAUSE_AFTER = 5

    def __init__(self, cfg: Config, cdp: CDP, token: str) -> None:
        self.cfg = cfg
        self.cdp = cdp
        self.token = token
        self._crashes: list[float] = []
        self._task: asyncio.Task[None] | None = None
        self.status = "waiting for Steam"

    def bootstrap(self, script: str) -> str:
        base = f"http://127.0.0.1:{self.cfg.port}"
        return f"""(function() {{
  if (window.deckyHasLoaded) {{
    setTimeout(() => SteamClient.Browser.RestartJSContext(), 100);
    return;
  }}
  window.deckyHasLoaded = true;
  window.remoraHasLoaded = true;
  window.deckyAuthToken = {json.dumps(self.token)};
  window.remoraAuthToken = window.deckyAuthToken;
  import({json.dumps(f"{base}/frontend/{script}?v={self.cfg.version}")})
    .catch(e => console.error('Remora: the frontend failed to load', e));
}})();"""

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _find_target(self) -> Target:
        while True:
            try:
                for target in await self.cdp.targets():
                    if target.is_gamepadui:
                        return target
                await asyncio.sleep(2)
            except (aiohttp.ClientConnectorError, ConnectionRefusedError):
                await asyncio.sleep(5)  # Steam isn't up
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                await asyncio.sleep(1)

    def _note_crash(self) -> int:
        now = time.monotonic()
        if self._crashes and now - self._crashes[-1] > self.CRASH_WINDOW:
            self._crashes.clear()
        self._crashes.append(now)
        return len(self._crashes)

    async def _run(self) -> None:
        while True:
            self.status = "waiting for Steam"
            target = await self._find_target()
            try:
                await self._attach(target)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("devtools connection failed: %s", e)
            crashes = self._note_crash()
            if crashes >= self.PAUSE_AFTER:
                self.status = "paused after repeated Steam UI crashes"
                log.error("Steam's UI went away %d times in a row; not injecting for 5 minutes", crashes)
                await asyncio.sleep(300)
                self._crashes.clear()
            else:
                await asyncio.sleep(1)

    async def _attach(self, target: Target) -> None:
        log.info("attaching to %s", target.title)
        async with self.cdp.session.ws_connect(target.ws_url, max_msg_size=0) as ws:
            cmd = 0

            async def send(method: str, params: dict[str, Any] | None = None) -> None:
                nonlocal cmd
                cmd += 1
                await ws.send_json({"id": cmd, "method": method, "params": params or {}})

            async def inject() -> None:
                script = "fallback.js" if len(self._crashes) >= self.FALLBACK_AFTER else "index.js"
                self.status = "injected" if script == "index.js" else "injected the crash-loop fallback"
                await send("Runtime.evaluate", {"expression": self.bootstrap(script), "userGesture": True})

            await send("Page.enable")
            await inject()
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                data = json.loads(msg.data)
                method = data.get("method")
                if method == "Page.domContentEventFired":
                    # Steam reloaded the context; the bootstrap's guard is gone with it
                    await inject()
                elif method == "Inspector.detached":
                    log.info("devtools detached")
                    return
