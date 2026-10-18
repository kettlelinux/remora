"""The frontend WebSocket protocol (docs/PROTOCOL.md, compat/frontend.md §4.2)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from remora.wsrouter import Discard, MessageType, RouteError, WSRouter

from conftest import wait_for


class Peer:
    """One frontend connection."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws

    async def send(self, message: dict[str, Any]) -> None:
        await self.ws.send_str(json.dumps(message))

    async def call(self, msg_id: int, route: str, *args: Any) -> None:
        await self.send({"type": MessageType.CALL, "id": msg_id, "route": route, "args": list(args)})

    async def ack(self, msg_id: int) -> None:
        await self.send({"type": MessageType.RECEIVED_RESPONSE, "id": msg_id})

    async def receive(self, timeout: float = 5.0) -> dict[str, Any]:
        msg = await self.ws.receive(timeout=timeout)
        return json.loads(msg.data)

    async def nothing_for(self, seconds: float) -> None:
        with pytest.raises(asyncio.TimeoutError):
            await self.ws.receive(timeout=seconds)


@pytest.fixture
def router() -> WSRouter:
    r = WSRouter()
    r.counts = {}  # type: ignore[attr-defined]

    def counted(name: str, fn: Any) -> Any:
        async def wrapper(*args: Any) -> Any:
            r.counts[name] = r.counts.get(name, 0) + 1  # type: ignore[attr-defined]
            return await fn(*args)
        return wrapper

    async def add(a: int, b: int) -> int:
        return a + b

    async def boom() -> None:
        raise ValueError("kaboom")

    async def named() -> None:
        raise RouteError("PluginError", "named failure", "the trace")

    async def discard() -> None:
        raise Discard()

    async def not_json() -> object:
        return object()

    r.gate = asyncio.Event()  # type: ignore[attr-defined]

    async def gated() -> str:
        await r.gate.wait()  # type: ignore[attr-defined]
        return "released"

    for name, fn in {"t/add": add, "t/boom": boom, "t/named": named, "t/discard": discard,
                     "t/not_json": not_json, "t/gated": gated}.items():
        r.add_route(name, counted(name, fn))
    return r


@pytest.fixture
async def client(router: WSRouter):
    app = web.Application()
    app.router.add_get("/ws", router.handle)
    async with TestClient(TestServer(app)) as c:
        yield c
    await router.close()


@pytest.fixture
def connect(client: TestClient):
    async def open_peer() -> Peer:
        return Peer(await client.ws_connect("/ws"))
    return open_peer


async def test_call_reply(connect) -> None:
    peer = await connect()
    await peer.call(1, "t/add", 2, 3)
    assert await peer.receive() == {"type": MessageType.REPLY, "id": 1, "result": 5}


async def test_error_has_message_and_error(connect) -> None:
    peer = await connect()
    await peer.call(1, "t/boom")
    reply = await peer.receive()
    assert reply["type"] == MessageType.ERROR and reply["id"] == 1
    err = reply["error"]
    assert err["name"] == "ValueError"
    assert err["message"] == err["error"] == "kaboom"
    assert "kaboom" in err["traceback"]


async def test_route_error_keeps_its_name(connect) -> None:
    peer = await connect()
    await peer.call(1, "t/named")
    err = (await peer.receive())["error"]
    assert err == {"name": "PluginError", "message": "named failure", "error": "named failure",
                   "traceback": "the trace"}


async def test_wrong_arguments_are_an_error(connect) -> None:
    peer = await connect()
    await peer.call(1, "t/add", 1)
    reply = await peer.receive()
    assert reply["type"] == MessageType.ERROR and reply["error"]["name"] == "TypeError"


async def test_non_json_result_is_an_error_not_a_hang(connect) -> None:
    peer = await connect()
    await peer.call(1, "t/not_json")
    reply = await peer.receive()
    assert reply["type"] == MessageType.ERROR and reply["error"]["name"] == "TypeError"


async def test_unknown_route(connect) -> None:
    peer = await connect()
    await peer.call(7, "nope/nope")
    reply = await peer.receive()
    assert reply["type"] == MessageType.ERROR and reply["id"] == 7
    assert reply["error"]["name"] == "RouteNotFoundError"
    assert reply["error"]["error"] == "Route nope/nope does not exist."


async def test_discard(connect) -> None:
    peer = await connect()
    await peer.call(3, "t/discard")
    assert await peer.receive() == {"type": MessageType.DISCARD, "id": 3}


async def test_scalar_args_are_wrapped(connect, router: WSRouter) -> None:
    peer = await connect()
    await peer.send({"type": MessageType.CALL, "id": 1, "route": "t/boom", "args": "x"})
    reply = await peer.receive()
    assert reply["error"]["name"] == "TypeError"  # boom() got one argument: it was passed


async def test_duplicate_call_before_ack_gets_cached_reply(connect, router: WSRouter) -> None:
    peer = await connect()
    await peer.call(1, "t/add", 1, 1)
    assert (await peer.receive())["result"] == 2
    await peer.call(1, "t/add", 1, 1)
    assert (await peer.receive())["result"] == 2
    assert router.counts["t/add"] == 1  # type: ignore[attr-defined]


async def test_ack_drops_the_cached_reply(connect, router: WSRouter) -> None:
    peer = await connect()
    await peer.call(1, "t/add", 1, 1)
    await peer.receive()
    await peer.ack(1)
    await wait_for(lambda: 1 not in router._replies, what="the ack")
    await peer.call(1, "t/add", 2, 2)
    assert (await peer.receive())["result"] == 4
    assert router.counts["t/add"] == 2  # type: ignore[attr-defined]


async def test_full_sync_replays_and_drops_stale(connect, router: WSRouter) -> None:
    peer = await connect()
    await peer.call(1, "t/add", 1, 1)
    await peer.call(2, "t/add", 2, 2)
    replies = {(await peer.receive())["id"] for _ in range(2)}
    assert replies == {1, 2}
    await peer.ws.close()

    # the frontend reconnects and still waits for 1 (it never got it) and has a new call 3;
    # 2 isn't in the sync, so the frontend has it and the cached reply goes
    peer = await connect()
    await peer.send({"type": MessageType.FULL_SYNC, "messages": [
        {"type": MessageType.CALL, "id": 1, "route": "t/add", "args": [1, 1]},
        {"type": MessageType.CALL, "id": 3, "route": "t/add", "args": [3, 3]},
    ]})
    got = {}
    for _ in range(2):
        reply = await peer.receive()
        got[reply["id"]] = reply["result"]
    assert got == {1: 2, 3: 6}
    assert router.counts["t/add"] == 3  # type: ignore[attr-defined]  # 1 was not run again
    assert 2 not in router._replies
    await peer.call(2, "t/add", 5, 5)  # a fresh call that reuses the id runs
    assert (await peer.receive())["result"] == 10


async def test_running_call_replies_to_the_new_connection(connect, router: WSRouter) -> None:
    first = await connect()
    await first.call(1, "t/gated")
    await wait_for(lambda: router.counts.get("t/gated"), what="the call to start")  # type: ignore[attr-defined]
    await first.ws.close()
    second = await connect()
    await second.send({"type": MessageType.FULL_SYNC, "messages": [
        {"type": MessageType.CALL, "id": 1, "route": "t/gated", "args": []}]})
    await asyncio.sleep(0.05)
    router.gate.set()  # type: ignore[attr-defined]
    assert await second.receive() == {"type": MessageType.REPLY, "id": 1, "result": "released"}
    assert router.counts["t/gated"] == 1  # type: ignore[attr-defined]


async def test_events(connect, router: WSRouter) -> None:
    peer = await connect()
    await wait_for(lambda: router.ws is not None, what="the connection")
    await router.emit("loader/plugin_event", {"plugin": "P", "event": "e", "args": [1]}, 2)
    assert await peer.receive() == {"type": MessageType.EVENT, "event": "loader/plugin_event",
                                    "args": [{"plugin": "P", "event": "e", "args": [1]}, 2]}


async def test_events_without_a_frontend_are_dropped(router: WSRouter) -> None:
    await router.emit("x", 1)  # no error


async def test_new_connection_replaces_the_old(connect, router: WSRouter) -> None:
    first = await connect()
    await wait_for(lambda: router.ws is not None, what="the first connection")
    second = await connect()
    msg = await first.ws.receive(timeout=5)
    assert msg.type.name in ("CLOSE", "CLOSED", "CLOSING")
    await second.call(1, "t/add", 1, 2)
    assert (await second.receive())["result"] == 3


async def test_close_text_and_bad_json(connect, router: WSRouter) -> None:
    peer = await connect()
    await peer.ws.send_str("{not json")
    await peer.ws.send_str("[1, 2]")
    await peer.send({"type": 99})
    await peer.call(1, "t/add", 1, 2)
    assert (await peer.receive())["result"] == 3
    await peer.ws.send_str("close")
    msg = await peer.ws.receive(timeout=5)
    assert msg.type.name in ("CLOSE", "CLOSED", "CLOSING")
    await wait_for(lambda: router.ws is None, what="the session to end")


def test_route_registered_twice(router: WSRouter) -> None:
    with pytest.raises(ValueError):
        router.add_route("t/add", lambda: None)  # type: ignore[arg-type,return-value]


async def test_new_context_reusing_an_id_is_a_new_call(connect, router: WSRouter) -> None:
    """A reloaded JS context starts its ids at 1 again: the same id with other arguments is
    another call, not the old one's cached reply."""
    first = await connect()
    await first.call(1, "t/add", 1, 1)
    assert (await first.receive())["result"] == 2  # never acked
    await first.ws.close()
    second = await connect()
    await second.call(1, "t/add", 2, 2)
    assert (await second.receive())["result"] == 4


async def test_new_context_reusing_a_running_id_is_not_dropped(connect, router: WSRouter) -> None:
    first = await connect()
    await first.call(1, "t/gated")
    await wait_for(lambda: router.counts.get("t/gated"), what="the call to start")  # type: ignore[attr-defined]
    await first.ws.close()
    second = await connect()
    await second.call(1, "t/add", 3, 4)  # not "still running"
    assert await second.receive() == {"type": MessageType.REPLY, "id": 1, "result": 7}
    router.gate.set()  # type: ignore[attr-defined]
    await second.nothing_for(0.3)  # the orphaned call's reply is dropped


async def test_full_sync_orphans_running_calls_it_does_not_list(connect, router: WSRouter) -> None:
    first = await connect()
    await first.call(5, "t/gated")
    await wait_for(lambda: router.counts.get("t/gated"), what="the call to start")  # type: ignore[attr-defined]
    await first.ws.close()
    second = await connect()
    await second.send({"type": MessageType.FULL_SYNC, "messages": []})
    await asyncio.sleep(0.05)
    router.gate.set()  # type: ignore[attr-defined]
    await second.nothing_for(0.3)
    assert router._calls == {}


@pytest.mark.parametrize("bad", [
    {"type": MessageType.RECEIVED_RESPONSE, "id": [1]},
    {"type": MessageType.CALL, "id": [1], "route": "t/add", "args": [1, 2]},
    {"type": MessageType.CALL, "id": True, "route": "t/add", "args": [1, 2]},
    {"type": MessageType.FULL_SYNC, "messages": {"not": "a list"}},
    {"type": MessageType.FULL_SYNC, "messages": [{"type": 0, "id": {"x": 1}}]},
    {"type": [1]},
])
async def test_malformed_messages_keep_the_connection(connect, router: WSRouter, bad) -> None:
    peer = await connect()
    await peer.send(bad)
    await peer.call(1, "t/add", 1, 2)
    assert (await peer.receive())["result"] == 3
