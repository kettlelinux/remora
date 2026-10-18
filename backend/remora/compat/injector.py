"""Decky's `injector` module as plugins use it: CEF tabs through the devtools port.

DeckWebBrowser and others call get_tab(title) / get_gamepadui_tab() and evaluate JS in a tab.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from aiohttp import ClientSession, ClientWebSocketResponse

BASE_ADDRESS = os.environ.get("REMORA_CEF_URL", "http://127.0.0.1:8080")

_GAMEPADUI_TITLES = ("SharedJSContext", "Steam Shared Context presented by Valve™", "Steam", "SP")


class Tab:
    def __init__(self, data: dict[str, Any]) -> None:
        self.title: str = data.get("title", "")
        self.id: str = data.get("id", "")
        self.url: str = data.get("url", "")
        self.ws_url: str = data.get("webSocketDebuggerUrl", "")
        self.session: ClientSession | None = None
        self.websocket: ClientWebSocketResponse | None = None
        self.cmd_id = 0

    async def open_websocket(self) -> None:
        self.session = ClientSession()
        self.websocket = await self.session.ws_connect(self.ws_url, max_msg_size=0)

    async def close_websocket(self) -> None:
        if self.websocket is not None:
            await self.websocket.close()
        if self.session is not None:
            await self.session.close()
        self.websocket = self.session = None

    async def _send(self, method: str, params: dict[str, Any] | None = None,
                    get_result: bool = True) -> Any:
        assert self.websocket is not None
        self.cmd_id += 1
        my_id = self.cmd_id
        await self.websocket.send_json({"id": my_id, "method": method, "params": params or {}})
        if not get_result:
            return None
        async for msg in self.websocket:
            data = json.loads(msg.data)
            if data.get("id") == my_id:
                return data
        return None

    async def _with_socket(self, manage_socket: bool, coro_fn: Any) -> Any:
        if manage_socket:
            await self.open_websocket()
        try:
            return await coro_fn()
        finally:
            if manage_socket:
                await self.close_websocket()

    async def evaluate(self, js: str, run_async: bool = False, manage_socket: bool = True,
                       get_result: bool = True) -> Any:
        return await self._with_socket(manage_socket, lambda: self._send(
            "Runtime.evaluate",
            {"expression": js, "userGesture": True, "awaitPromise": run_async, "returnByValue": True},
            get_result))

    async def has_global_var(self, var_name: str, manage_socket: bool = True) -> bool:
        res = await self.evaluate(f"window['{var_name}'] !== null && window['{var_name}'] !== undefined",
                                  False, manage_socket)
        try:
            return bool(res["result"]["result"]["value"])
        except (TypeError, KeyError):
            return False

    async def reload(self, manage_socket: bool = True) -> None:
        await self._with_socket(manage_socket, lambda: self._send("Page.reload", get_result=False))

    async def close(self, manage_socket: bool = True) -> None:
        await self._with_socket(manage_socket, lambda: self._send("Page.close", get_result=False))

    async def enable(self) -> None:
        await self._send("Page.enable", get_result=False)

    async def disable(self) -> None:
        await self._send("Page.disable", get_result=False)

    async def listen_for_message(self) -> Any:
        assert self.websocket is not None
        async for msg in self.websocket:
            yield json.loads(msg.data)

    def __repr__(self) -> str:
        return f"Tab({self.title!r}, {self.url!r})"


async def get_tabs() -> list[Tab]:
    async with ClientSession() as session:
        for _ in range(10):
            try:
                async with session.get(f"{BASE_ADDRESS}/json", timeout=3) as res:
                    if res.status == 200:
                        return [Tab(t) for t in await res.json(content_type=None)]
            except Exception:
                pass
            await asyncio.sleep(1)
    raise RuntimeError("could not reach Steam's CEF devtools")


async def get_tab(tab_name: str) -> Tab:
    for tab in await get_tabs():
        if tab.title == tab_name:
            return tab
    raise ValueError(f"Tab {tab_name} not found")


async def get_tab_lambda(test: Any) -> Tab:
    for tab in await get_tabs():
        if test(tab):
            return tab
    raise ValueError("Tab not found by lambda")


async def get_gamepadui_tab() -> Tab:
    for tab in await get_tabs():
        if tab.title in _GAMEPADUI_TITLES and "steamloopback.host" in tab.url:
            return tab
    raise ValueError("GamepadUI Tab not found")


async def inject_to_tab(tab_name: str, js: str, run_async: bool = False) -> Any:
    tab = await get_tab(tab_name)
    return await tab.evaluate(js, run_async)
