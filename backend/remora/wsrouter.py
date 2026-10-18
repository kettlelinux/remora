"""The frontend's WebSocket: Decky's message protocol (compat/frontend.md §4.2, docs/PROTOCOL.md)."""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from dataclasses import dataclass
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


def _call_key(route: Any, args: Any) -> str:
    """What a call is, beyond its id: ids restart at 1 in a new JS context, so the same id with
    another route or other arguments is another call."""
    try:
        return json.dumps([route, args], sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr((route, args))


@dataclass
class _Call:
    key: str
    reply: str | None = None  # the encoded reply, once there is one


class WSRouter:
    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.ws: web.WebSocketResponse | None = None
        # calls by id: running (no reply yet) or answered and waiting for RECEIVED_RESPONSE
        self._calls: dict[int, _Call] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def _replies(self) -> dict[int, str]:
        """Answered calls waiting for their ack (tests look at this)."""
        return {i: c.reply for i, c in self._calls.items() if c.reply is not None}

    def add_route(self, name: str, fn: Route) -> None:
        if name in self.routes:
            raise ValueError(f"route {name} registered twice")
        self.routes[name] = fn

    async def _send_text(self, text: str) -> None:
        ws = self.ws
        if ws is None or ws.closed:
            return
        try:
            await ws.send_str(text)
        except (ConnectionResetError, RuntimeError) as e:
            log.debug("send failed: %s", e)

    async def _send(self, message: dict[str, Any]) -> None:
        await self._send_text(json.dumps(message, ensure_ascii=False))

    async def emit(self, event: str, *args: Any) -> None:
        if self.ws is None or self.ws.closed:
            log.debug("no frontend connected, dropping event %s", event)
            return
        await self._send({"type": MessageType.EVENT, "event": event, "args": list(args)})

    async def _run_call(self, msg_id: int, call: _Call, route: str, args: list[Any]) -> None:
        fn = self.routes.get(route)
        reply: dict[str, Any]
        text: str | None = None
        if fn is None:
            reply = {"type": MessageType.ERROR, "id": msg_id,
                     "error": error_payload("RouteNotFoundError", f"Route {route} does not exist.", None)}
        else:
            try:
                result = await fn(*args)
                # encoded once, here: a result that isn't JSON is an error reply, not a failed send
                text = json.dumps({"type": MessageType.REPLY, "id": msg_id, "result": result},
                                  ensure_ascii=False)
                reply = {}
            except Discard:
                reply = {"type": MessageType.DISCARD, "id": msg_id}
            except RouteError as e:
                reply = {"type": MessageType.ERROR, "id": msg_id,
                         "error": error_payload(e.name, e.message, e.tb)}
            except Exception as e:
                log.debug("route %s failed", route, exc_info=True)
                reply = {"type": MessageType.ERROR, "id": msg_id,
                         "error": error_payload(type(e).__name__, str(e), traceback.format_exc())}
        if text is None:
            text = json.dumps(reply, ensure_ascii=False)
        if self._calls.get(msg_id) is not call:
            log.debug("dropping the reply to orphaned call %d (%s)", msg_id, route)
            return
        call.reply = text
        await self._send_text(text)

    def _handle_call(self, message: dict[str, Any]) -> None:
        msg_id = message.get("id")
        if not isinstance(msg_id, int) or isinstance(msg_id, bool):
            return
        route = message.get("route")
        args = message.get("args")
        if args is None:
            args = []
        if not isinstance(args, list):
            args = [args]
        key = _call_key(route, args)
        known = self._calls.get(msg_id)
        if known is not None and known.key == key:
            if known.reply is not None:
                self._spawn(self._send_text(known.reply))
            return  # still running; its reply goes to whichever socket is current
        if known is not None:
            log.debug("call id %d reused for another call; the old one is orphaned", msg_id)
        call = _Call(key)
        self._calls[msg_id] = call
        self._spawn(self._run_call(msg_id, call, str(route), args))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _handle(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == MessageType.CALL:
            self._handle_call(message)
        elif kind == MessageType.RECEIVED_RESPONSE:
            msg_id = message.get("id")
            call = self._calls.get(msg_id) if isinstance(msg_id, int) else None
            if call is not None and call.reply is not None:
                del self._calls[msg_id]  # type: ignore[arg-type]
        elif kind == MessageType.FULL_SYNC:
            messages = message.get("messages") or []
            calls = [m for m in messages if isinstance(m, dict)] if isinstance(messages, list) else []
            wanted = {m.get("id") for m in calls if isinstance(m.get("id"), int)}
            # answered calls the frontend no longer waits for, and running ones it doesn't know
            # (a new JS context): forget them; a running one's reply is dropped
            for stale in [i for i in self._calls if i not in wanted]:
                del self._calls[stale]
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
                        try:
                            self._handle(data)
                        except Exception:  # one bad message must not drop the connection
                            log.warning("bad message from the frontend", exc_info=True)
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
