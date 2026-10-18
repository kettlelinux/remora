"""HTTP on :1337 (compat/backend.md §6.1, W1, W9, W10): Host check, auth, CORS, file routes and
the fetchNoCors proxy."""

from __future__ import annotations

import json
import os

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from remora.cdp import CDP
from remora.config import Config
from remora.plugins import manifest
from remora.plugins.manager import PluginManager
from remora.server import STEAM_ORIGIN, Server
from remora.utilities import Utilities

from conftest import free_port

TOKEN = "test-token"
BUNDLE = "export default function plugin() {}\n"


@pytest.fixture
def plugin_dirs(make_plugin, cfg: Config, tmp_path):
    path = make_plugin("my-plugin", "My Plugin", None, files={
        "dist/index.js": BUNDLE,
        "dist/assets/logo.svg": "<svg/>",
        "dist/sub/chunk.js": "chunk",
        "secret.txt": "not served",
    })
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    os.symlink(outside, path / "dist" / "escape.js")
    os.symlink(tmp_path, path / "dist" / "escape-dir")
    data = cfg.homebrew / "data" / "my-plugin"
    data.mkdir(parents=True)
    (data / "x.txt").write_text("data file")
    os.symlink(tmp_path, data / "escape")
    return path


@pytest.fixture
async def client(cfg: Config, settings, ws, plugin_dirs):
    manager = PluginManager(cfg, settings, ws)
    manager._register(manifest.read_plugin(plugin_dirs))
    cdp = CDP(cfg)
    utilities = Utilities(cfg, settings, manager, cdp, ws)
    utilities.register(ws)
    server = Server(cfg, TOKEN, ws, manager, utilities)
    async with TestClient(TestServer(server.app(), host="127.0.0.1", port=cfg.port)) as c:
        yield c
    await utilities.close()
    await cdp.close()
    await ws.close()


AUTH = {"X-Decky-Auth": TOKEN}


# -- Host, auth, CORS -----------------------------------------------------------------------

@pytest.mark.parametrize("host", ["evil.example:1337", "evil.example", "127.0.0.1:1", "attacker.localhost"])
async def test_wrong_host_is_misdirected(client: TestClient, host: str) -> None:
    res = await client.get("/frontend/index.js", headers={"Host": host})
    assert res.status == 421


async def test_localhost_host_is_fine(client: TestClient, cfg: Config) -> None:
    res = await client.get("/plugins/My%20Plugin/dist/index.js", headers={"Host": f"localhost:{cfg.port}"})
    assert res.status == 200


async def test_auth_token_needs_steam_origin(client: TestClient) -> None:
    assert (await client.get("/auth/token")).status == 403
    assert (await client.get("/auth/token", headers={"Origin": "https://evil.example"})).status == 403
    res = await client.get("/auth/token", headers={"Origin": STEAM_ORIGIN})
    assert res.status == 200 and await res.text() == TOKEN
    assert res.headers["Access-Control-Allow-Origin"] == STEAM_ORIGIN


async def test_protected_routes_need_the_header(client: TestClient) -> None:
    for path in ("/plugins/My%20Plugin/frontend_bundle", "/methods/ping", "/nowhere"):
        method = client.post if path.startswith("/methods") else client.get
        assert (await method(path)).status == 403, path
        assert (await method(path, headers={"X-Decky-Auth": "wrong"})).status == 403, path
    res = await client.post("/methods/ping", headers=AUTH, json={})
    assert res.status == 200 and await res.json() == {"success": True, "result": "pong"}


async def test_methods_unknown_utility(client: TestClient) -> None:
    res = await client.post("/methods/nope", headers=AUTH, data="not json")
    assert await res.json() == {"success": False, "result": "no utility named nope"}


async def test_ws_needs_token(client: TestClient) -> None:
    assert (await client.get("/ws")).status == 403
    assert (await client.get("/ws?auth=wrong")).status == 403
    ws = await client.ws_connect(f"/ws?auth={TOKEN}")
    await ws.send_str(json.dumps({"type": 0, "id": 1, "route": "utilities/ping", "args": []}))
    assert json.loads((await ws.receive(timeout=5)).data) == {"type": 1, "id": 1, "result": "pong"}
    await ws.close()


async def test_cors_preflight(client: TestClient) -> None:
    res = await client.options("/plugins/My%20Plugin/frontend_bundle", headers={
        "Origin": STEAM_ORIGIN, "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "X-Decky-Auth"})
    assert res.status == 204
    assert res.headers["Access-Control-Allow-Origin"] == STEAM_ORIGIN
    assert res.headers["Access-Control-Allow-Credentials"] == "true"
    assert res.headers["Access-Control-Allow-Methods"] == "GET"
    assert res.headers["Access-Control-Allow-Headers"] == "X-Decky-Auth"


async def test_cors_preflight_other_origin(client: TestClient) -> None:
    res = await client.options("/methods/ping", headers={"Origin": "https://evil.example",
                                                         "Access-Control-Request-Method": "POST"})
    assert res.status == 204
    assert "Access-Control-Allow-Origin" not in res.headers


async def test_cors_headers_on_responses(client: TestClient) -> None:
    res = await client.post("/methods/ping", headers={**AUTH, "Origin": STEAM_ORIGIN}, json={})
    assert res.headers["Access-Control-Allow-Origin"] == STEAM_ORIGIN
    assert res.headers["Access-Control-Allow-Credentials"] == "true"
    res = await client.get("/plugins/My%20Plugin/dist/index.js", headers={"Origin": STEAM_ORIGIN})
    assert res.headers["Access-Control-Allow-Origin"] == STEAM_ORIGIN
    # errors too, so the frontend can read the status
    res = await client.get("/plugins/My%20Plugin/frontend_bundle", headers={"Origin": STEAM_ORIGIN})
    assert res.status == 403 and res.headers["Access-Control-Allow-Origin"] == STEAM_ORIGIN
    res = await client.get("/plugins/My%20Plugin/dist/index.js", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in res.headers


# -- file routes ----------------------------------------------------------------------------

async def test_plugin_dist_assets_data(client: TestClient) -> None:
    res = await client.get("/plugins/My%20Plugin/dist/index.js")
    assert res.status == 200 and await res.text() == BUNDLE
    assert res.headers["Content-Type"].startswith("application/javascript")
    assert res.headers["Cache-Control"] == "no-cache"
    res = await client.get("/plugins/My%20Plugin/dist/sub/chunk.js")
    assert await res.text() == "chunk"
    res = await client.get("/plugins/My%20Plugin/assets/logo.svg")
    assert res.status == 200 and await res.text() == "<svg/>"
    res = await client.get("/plugins/My%20Plugin/data/x.txt")
    assert res.status == 200 and await res.text() == "data file"


async def test_head(client: TestClient) -> None:
    res = await client.head("/plugins/My%20Plugin/dist/index.js")
    assert res.status == 200 and res.headers["Content-Length"] == str(len(BUNDLE))


@pytest.mark.parametrize("path", [
    "/plugins/My%20Plugin/dist/escape.js",          # a link out of dist
    "/plugins/My%20Plugin/dist/escape-dir/outside.txt",
    "/plugins/My%20Plugin/data/escape/outside.txt",  # a link out of the data folder
    "/plugins/My%20Plugin/dist/",                    # a folder
    "/plugins/My%20Plugin/dist/sub",
    "/plugins/My%20Plugin/dist/missing.js",
    "/plugins/Nobody/dist/index.js",
    "/plugins/My%20Plugin/assets/../index.js",  # the client folds it to an unknown route: 403
])
async def test_file_routes_refuse(client: TestClient, path: str) -> None:
    res = await client.get(path)
    assert res.status in (403, 404), path
    assert "outside" not in await res.text()


@pytest.mark.parametrize("raw", [
    "/plugins/My%20Plugin/dist/..%2Fsecret.txt",
    "/plugins/My%20Plugin/dist/%2E%2E/secret.txt",
    "/plugins/My%20Plugin/dist/sub/%2E%2E/%2E%2E/secret.txt",
    "/plugins/My%20Plugin/data/..%2F..%2F..%2Fhomebrew",
    "/plugins/My%20Plugin/dist/..%5Csecret.txt",
    "/frontend/..%2F__init__.py",
    "/frontend/%2E%2E/__init__.py",
])
async def test_dot_dot_is_refused(client: TestClient, raw: str) -> None:
    res = await client.get(URL(raw, encoded=True))
    assert res.status in (403, 404), raw
    assert "not served" not in await res.text()


async def test_frontend_bundle(client: TestClient) -> None:
    assert (await client.get("/plugins/My%20Plugin/frontend_bundle")).status == 403
    res = await client.get("/plugins/My%20Plugin/frontend_bundle", headers=AUTH)
    assert res.status == 200 and await res.text() == BUNDLE
    assert res.headers["Content-Type"].startswith("application/javascript")
    assert (await client.get("/plugins/Nobody/frontend_bundle", headers=AUTH)).status == 404


async def test_frontend_static_and_locales(client: TestClient, monkeypatch, tmp_path) -> None:
    res = await client.get("/frontend/does-not-exist.js")
    assert res.status == 404
    res = await client.get("/locales/xx-YY", headers=AUTH)
    assert res.status == 200 and await res.json() == {}
    res = await client.get("/locales/..%2Fsettings", headers=AUTH)
    assert await res.json() == {}


async def test_frontend_files_are_served(cfg: Config, settings, ws, plugin_dirs, monkeypatch,
                                         tmp_path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.js").write_text("console.log('remora')")
    import remora.server as server_mod

    monkeypatch.setattr(server_mod, "STATIC_DIR", static)
    manager = PluginManager(cfg, settings, ws)
    server = Server(cfg, TOKEN, ws, manager, Utilities(cfg, settings, manager, CDP(cfg), ws))
    async with TestClient(TestServer(server.app(), host="127.0.0.1", port=cfg.port)) as c:
        res = await c.get("/frontend/index.js")
        assert res.status == 200 and "remora" in await res.text()
        assert res.headers["Content-Type"].startswith("application/javascript")
        assert (await c.get("/static/index.js")).status == 200


# -- /fetch ---------------------------------------------------------------------------------

@pytest.fixture
async def upstream():
    seen: list[dict] = []

    async def echo(request: web.Request) -> web.Response:
        body = await request.read()
        seen.append({"method": request.method, "headers": dict(request.headers), "body": body.decode()})
        return web.json_response({"ok": True}, headers={
            "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Credentials": "false",
            "X-Upstream": "yes"})

    app = web.Application()
    app.router.add_route("*", "/echo", echo)
    server = TestServer(app, host="127.0.0.1", port=free_port())
    await server.start_server()
    yield server, seen
    await server.close()


async def test_fetch_rewrites_headers(client: TestClient, upstream, cfg: Config) -> None:
    server, seen = upstream
    url = f"http://127.0.0.1:{server.port}/echo"
    res = await client.post("/fetch", params={"auth": TOKEN, "fetch_url": url}, data="payload", headers={
        "Origin": STEAM_ORIGIN,
        "X-Decky-Header-Authorization": "Bearer abc",
        "X-Decky-Header-X-Remove-Too": "1",
        "X-Decky-Auth": TOKEN,
        "X-Decky-Something": "internal",
        "X-Decky-Fetch-Excluded-Headers": "X-Remove-Me, X-Remove-Too",
        "X-Remove-Me": "1",
        "X-Keep-Me": "kept",
        "Cookie": "session=1",
        "Sec-Fetch-Mode": "cors",
        "Referer": "https://steamloopback.host/",
    })
    assert res.status == 200 and await res.json() == {"ok": True}
    # the CORS layer answers for us, not with upstream's values
    assert res.headers["Access-Control-Allow-Origin"] == STEAM_ORIGIN
    assert res.headers["Access-Control-Allow-Credentials"] == "true"
    assert res.headers["X-Upstream"] == "yes"
    sent = {k.lower(): v for k, v in seen[0]["headers"].items()}
    assert seen[0]["method"] == "POST" and seen[0]["body"] == "payload"
    assert sent["authorization"] == "Bearer abc"
    assert sent["x-keep-me"] == "kept"
    assert sent["host"] == f"127.0.0.1:{server.port}"
    for gone in ("origin", "x-remove-me", "x-remove-too", "x-decky-auth", "x-decky-something",
                 "x-decky-fetch-excluded-headers", "cookie", "sec-fetch-mode", "referer"):
        assert gone not in sent, gone
    assert sent["user-agent"].startswith(f"Decky/{cfg.decky_version} (https://decky.xyz)")


async def test_fetch_methods(client: TestClient, upstream) -> None:
    server, seen = upstream
    url = f"http://127.0.0.1:{server.port}/echo"
    for method in ("GET", "PUT", "DELETE", "PATCH"):
        res = await client.request(method, "/fetch", params={"auth": TOKEN, "fetch_url": url})
        assert res.status == 200
    assert [s["method"] for s in seen] == ["GET", "PUT", "DELETE", "PATCH"]


async def test_fetch_refusals(client: TestClient, upstream) -> None:
    server, _ = upstream
    url = f"http://127.0.0.1:{server.port}/echo"
    assert (await client.get("/fetch", params={"fetch_url": url})).status == 403
    assert (await client.get("/fetch", params={"auth": "wrong", "fetch_url": url})).status == 403
    assert (await client.get("/fetch", params={"auth": TOKEN})).status == 400
    assert (await client.get("/fetch", params={"auth": TOKEN, "fetch_url": "file:///etc/passwd"})).status == 400
    dead = f"http://127.0.0.1:{free_port()}/"
    assert (await client.get("/fetch", params={"auth": TOKEN, "fetch_url": dead})).status == 502


async def test_fifo_in_data_folder_is_refused(client: TestClient, cfg: Config) -> None:
    os.mkfifo(cfg.homebrew / "data" / "my-plugin" / "fifo")
    import asyncio

    res = await asyncio.wait_for(client.get("/plugins/My%20Plugin/data/fifo"), 3)
    assert res.status == 404
