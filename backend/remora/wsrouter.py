"""The frontend's WebSocket: Decky's message protocol (compat/frontend.md §4.2, docs/PROTOCOL.md)."""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from enum import IntEnum
from typing import Any, Awaitable, Callable

from aiohttp import WSMsgType, web

log = logging.getLogger("remora.ws")

Route = Callable[..., Awaitable[Any]]


class MessageType(IntEnum):
    ERROR = -1
    CALL = 0
    REPLY = 1
    DISCARD = 2
    RECEIVED_RESPONSE = 3
    FULL_SYNC = 4
    EVENT = 5


class Discard(Exception):
    """Raised by a route whose call can't complete (the plugin stopped). The frontend gets DISCARD."""


class RouteError(Exception):
    """An error with a chosen name for the frontend's PyError."""

    def __init__(self, name: str, message: str, tb: str | None = None) -> None:
        super().__init__(message)
        self.name = name
        self.message = message
        self.tb = tb


def error_payload(name: str, message: str, tb: str | None) -> dict[str, Any]:
    # Decky's backend sends `message` and its frontend reads `error`; send both
    return {"name": name, "message": message, "error": message, "traceback": tb}


class WSRouter:
    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.ws: web.WebSocketResponse | None = None
        self._replies: dict[int, dict[str, Any]] = {}  # sent, waiting for RECEIVED_RESPONSE
        self._running: set[int] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    def add_route(self, name: str, fn: Route) -> None:
        if name in self.routes:
            raise ValueError(f"route {name} registered twice")
        self.routes[name] = fn

    async def _send(self, message: dict[str, Any]) -> None:
        ws = self.ws
        if ws is None or ws.closed:
            return
        try:
            await ws.send_str(json.dumps(message, ensure_ascii=False))
        except (ConnectionResetError, RuntimeError) as e:
            log.debug("send failed: %s", e)

    async def emit(self, event: str, *args: Any) -> None:
        if self.ws is None or self.ws.closed:
            log.debug("no frontend connected, dropping event %s", event)
            return
        await self._send({"type": MessageType.EVENT, "event": event, "args": list(args)})

    async def _run_call(self, msg_id: int, route: str, args: list[Any]) -> None:
        fn = self.routes.get(route)
        reply: dict[str, Any]
        if fn is None:
            reply = {"type": MessageType.ERROR, "id": msg_id,
                     "error": error_payload("RouteNotFoundError", f"Route {route} does not exist.", None)}
        else:
            try:
                result = await fn(*args)
                json.dumps(result, ensure_ascii=False)  # fail here, as an error reply, not on send
                reply = {"type": MessageType.REPLY, "id": msg_id, "result": result}
            except Discard:
                reply = {"type": MessageType.DISCARD, "id": msg_id}
            except RouteError as e:
                reply = {"type": MessageType.ERROR, "id": msg_id,
                         "error": error_payload(e.name, e.message, e.tb)}
            except Exception as e:
                log.debug("route %s failed", route, exc_info=True)
                reply = {"type": MessageType.ERROR, "id": msg_id,
                         "error": error_payload(type(e).__name__, str(e), traceback.format_exc())}
        self._running.discard(msg_id)
        self._replies[msg_id] = reply
        await self._send(reply)

    def _handle_call(self, message: dict[str, Any]) -> None:
        msg_id = message.get("id")
        if not isinstance(msg_id, int):
            return
        if msg_id in self._replies:
            task = asyncio.ensure_future(self._send(self._replies[msg_id]))
        elif msg_id in self._running:
            return  # still running; its reply goes to whichever socket is current
        else:
            self._running.add(msg_id)
            args = message.get("args") or []
            if not isinstance(args, list):
                args = [args]
            task = asyncio.ensure_future(self._run_call(msg_id, str(message.get("route")), args))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _handle(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == MessageType.CALL:
            self._handle_call(message)
        elif kind == MessageType.RECEIVED_RESPONSE:
            self._replies.pop(message.get("id"), None)  # type: ignore[arg-type]
        elif kind == MessageType.FULL_SYNC:
            calls = [m for m in message.get("messages") or [] if isinstance(m, dict)]
            wanted = {m.get("id") for m in calls}
            for stale in [i for i in self._replies if i not in wanted]:
                del self._replies[stale]
            for call in calls:
                self._handle_call(call)
        else:
            log.debug("ignoring message type %r", kind)

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=64 * 1024 * 1024, heartbeat=30)
        await ws.prepare(request)
        old, self.ws = self.ws, ws
        if old is not None and not old.closed:
            log.info("new frontend connection, closing the previous one")
            await old.close()
        log.info("frontend connected")
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    if msg.data == "close":
                        break
                    try:
                        data = json.loads(msg.data)
                    except ValueError:
                        log.warning("frontend sent invalid JSON")
                        continue
                    if isinstance(data, dict):
                        self._handle(data)
                elif msg.type == WSMsgType.ERROR:
                    log.warning("websocket error: %s", ws.exception())
        finally:
            if self.ws is ws:
                self.ws = None
            log.info("frontend disconnected")
        return ws

    async def close(self) -> None:
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        for task in list(self._tasks):
            task.cancel()
