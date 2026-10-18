"""HTTP on 127.0.0.1:1337: Decky's routes (compat/backend.md §6.1), with the hardening from
compat/backend.md W1, W9, W10."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
from aiohttp import web

from .config import Config
from .fsutil import UnsafePath, open_beneath, open_user_file_beneath, split_relative
from .plugins.manager import PluginManager
from .utilities import Utilities, ssl_context
from .wsrouter import WSRouter

log = logging.getLogger("remora.server")

STEAM_ORIGIN = "https://steamloopback.host"
STATIC_DIR = Path(__file__).resolve().parent / "static"
LOCALES_DIR = Path(__file__).resolve().parent / "locales"

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")

# routes anyone may fetch without the token (by route name, not by matching URL strings)
OPEN_ROUTES = {"auth_token", "frontend", "static", "plugin_dist", "plugin_assets", "plugin_data",
               "ws", "fetch", "locales"}

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _route_name(request: web.Request) -> str | None:
    route = request.match_info.route
    return getattr(route, "name", None) if route is not None else None


class Server:
    def __init__(self, cfg: Config, token: str, ws: WSRouter, manager: PluginManager,
                 utilities: Utilities) -> None:
        self.cfg = cfg
        self.token = token
        self.ws = ws
        self.manager = manager
        self.utilities = utilities
        self._ssl = ssl_context()
        self._fetch_session: aiohttp.ClientSession | None = None
        self.allowed_hosts = {f"127.0.0.1:{cfg.port}", f"localhost:{cfg.port}", f"[::1]:{cfg.port}"}

    # -- middlewares -------------------------------------------------------------------------

    @web.middleware
    async def host_check(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        # a web page can't reach us through DNS rebinding: its requests carry its own Host
        if request.host not in self.allowed_hosts:
            return web.Response(status=421, text="Misdirected Request")
        return await handler(request)

    @web.middleware
    async def cors(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        origin = request.headers.get("Origin")
        if request.method == "OPTIONS":
            response = web.Response(status=204)
            if origin == STEAM_ORIGIN:
                self._cors_headers(response, origin)
                response.headers["Access-Control-Allow-Methods"] = request.headers.get(
                    "Access-Control-Request-Method", "GET, POST, PUT, DELETE, PATCH, HEAD")
                response.headers["Access-Control-Allow-Headers"] = request.headers.get(
                    "Access-Control-Request-Headers", "*")
                response.headers["Access-Control-Max-Age"] = "600"
            return response
        try:
            response = await handler(request)
        except web.HTTPException as e:
            if origin == STEAM_ORIGIN:
                self._cors_headers(e, origin)
            raise
        if origin == STEAM_ORIGIN and not response.prepared:
            self._cors_headers(response, origin)
        return response

    @staticmethod
    def _cors_headers(response: web.StreamResponse, origin: str) -> None:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Expose-Headers"] = "*"
        response.headers["Vary"] = "Origin"

    @web.middleware
    async def auth(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        if request.method == "OPTIONS" or _route_name(request) in OPEN_ROUTES:
            return await handler(request)
        if request.headers.get("X-Decky-Auth") != self.token:
            raise web.HTTPForbidden(text="Forbidden")
        return await handler(request)

    # -- app ---------------------------------------------------------------------------------

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self.host_check, self.cors, self.auth],
                              client_max_size=256 * 1024 * 1024)
        r = app.router
        r.add_get("/auth/token", self.auth_token, name="auth_token")
        r.add_get("/ws", self.websocket, name="ws")
        r.add_route("*", "/fetch", self.fetch, name="fetch")
        r.add_get("/frontend/{path:.*}", self.frontend, name="frontend")
        r.add_get("/static/{path:.*}", self.frontend, name="static")
        r.add_get("/locales/{lang}", self.locales, name="locales")
        r.add_get("/plugins/{name}/dist/{path:.*}", self.plugin_dist, name="plugin_dist")
        r.add_get("/plugins/{name}/assets/{path:.*}", self.plugin_assets, name="plugin_assets")
        r.add_get("/plugins/{name}/data/{path:.*}", self.plugin_data, name="plugin_data")
        r.add_get("/plugins/{name}/frontend_bundle", self.frontend_bundle, name="frontend_bundle")
        r.add_post("/methods/{name}", self.methods, name="methods")
        app.on_cleanup.append(self._cleanup)
        return app

    async def _cleanup(self, app: web.Application) -> None:
        if self._fetch_session is not None:
            await self._fetch_session.close()

    # -- handlers ----------------------------------------------------------------------------

    async def auth_token(self, request: web.Request) -> web.Response:
        # the injected frontend gets the token from the CDP bootstrap; this is for code in
        # Steam's pages that still asks for it the Decky way (compat/backend.md W1)
        if request.headers.get("Origin") != STEAM_ORIGIN:
            raise web.HTTPForbidden(text="Forbidden")
        return web.Response(text=self.token)

    async def websocket(self, request: web.Request) -> web.StreamResponse:
        if request.query.get("auth") != self.token:
            raise web.HTTPForbidden(text="Forbidden")
        return await self.ws.handle(request)

    async def _send_fd(self, request: web.Request, fd: int, name: str,
                       content_type: str | None = None) -> web.StreamResponse:
        with os.fdopen(fd, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            ctype = content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
            response = web.StreamResponse(headers={"Cache-Control": "no-cache", "Content-Type": ctype,
                                                   "Content-Length": str(size)})
            origin = request.headers.get("Origin")
            if origin == STEAM_ORIGIN:
                self._cors_headers(response, origin)
            await response.prepare(request)
            if request.method != "HEAD":
                while True:
                    chunk = await asyncio.to_thread(f.read, 256 * 1024)
                    if not chunk:
                        break
                    await response.write(chunk)
            await response.write_eof()
            return response

    async def _serve(self, request: web.Request, base: str, rel: str,
                     user_base: Path | None = None) -> web.StreamResponse:
        try:
            parts = split_relative(rel)
            if user_base is not None:
                fd = open_user_file_beneath(user_base, parts, self.cfg.user_home)
            else:
                fd = open_beneath(base, parts)
        except (UnsafePath, FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError,
                OSError):
            raise web.HTTPNotFound() from None
        return await self._send_fd(request, fd, parts[-1] if parts else rel)

    async def frontend(self, request: web.Request) -> web.StreamResponse:
        return await self._serve(request, str(STATIC_DIR), request.match_info["path"])

    async def locales(self, request: web.Request) -> web.Response:
        lang = request.match_info["lang"].removesuffix(".json")
        path = LOCALES_DIR / f"{lang}.json"
        if "/" in lang or ".." in lang or not path.is_file():
            return web.json_response({}, headers={"Cache-Control": "no-cache"})
        return web.Response(text=path.read_text(encoding="utf-8"), content_type="application/json",
                            headers={"Cache-Control": "no-cache"})

    def _plugin(self, request: web.Request) -> Any:
        entry = self.manager.plugins.get(request.match_info["name"])
        if entry is None:
            raise web.HTTPNotFound()
        return entry.info

    async def plugin_dist(self, request: web.Request) -> web.StreamResponse:
        info = self._plugin(request)
        return await self._serve(request, os.path.join(os.path.realpath(info.path), "dist"),
                                 request.match_info["path"])

    async def plugin_assets(self, request: web.Request) -> web.StreamResponse:
        info = self._plugin(request)
        return await self._serve(request, os.path.join(os.path.realpath(info.path), "dist", "assets"),
                                 request.match_info["path"])

    async def plugin_data(self, request: web.Request) -> web.StreamResponse:
        info = self._plugin(request)
        return await self._serve(request, "", request.match_info["path"],
                                 user_base=self.cfg.homebrew / "data" / info.dir_name)

    async def frontend_bundle(self, request: web.Request) -> web.StreamResponse:
        info = self._plugin(request)
        try:
            fd = open_beneath(os.path.join(os.path.realpath(info.path), "dist"), ["index.js"])
        except OSError:
            raise web.HTTPNotFound() from None
        return await self._send_fd(request, fd, "index.js", "application/javascript")

    async def methods(self, request: web.Request) -> web.Response:
        try:
            kwargs = await request.json() if request.can_read_body else {}
        except ValueError:
            kwargs = {}
        result = await self.utilities.call_legacy_utility(request.match_info["name"],
                                                          kwargs if isinstance(kwargs, dict) else {})
        return web.json_response(result)

    # -- fetchNoCors proxy (compat/backend.md §6.1) ------------------------------------------

    DROP_HEADERS = {"host", "origin", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "cookie",
                    "content-length", "transfer-encoding", "connection", "referer"}
    DROP_RESPONSE = {"access-control-allow-origin", "access-control-allow-credentials",
                     "access-control-expose-headers", "transfer-encoding", "connection",
                     "content-length"}

    async def fetch(self, request: web.Request) -> web.StreamResponse:
        if request.query.get("auth") != self.token:
            raise web.HTTPForbidden(text="Forbidden")
        url = request.query.get("fetch_url")
        if not url or not url.startswith(("http://", "https://")):
            raise web.HTTPBadRequest(text="fetch_url must be an http(s) URL")
        excluded = {h.strip().lower() for h in
                    request.headers.get("X-Decky-Fetch-Excluded-Headers", "").split(",") if h.strip()}
        headers: dict[str, str] = {}
        for key, value in request.headers.items():
            low = key.lower()
            if low.startswith("x-decky-header-"):
                headers[key[len("x-decky-header-"):]] = value
            elif low.startswith("x-decky-") or low in self.DROP_HEADERS or low in excluded:
                continue
            else:
                headers[key] = value
        headers = {k: v for k, v in headers.items() if k.lower() not in excluded}
        headers["User-Agent"] = f"Decky/{self.cfg.decky_version} (https://decky.xyz) Remora/{self.cfg.version}"
        if self._fetch_session is None or self._fetch_session.closed:
            self._fetch_session = aiohttp.ClientSession(auto_decompress=False)
        body = await request.read() if request.can_read_body else None
        try:
            upstream = await self._fetch_session.request(request.method, url, headers=headers, data=body,
                                                         ssl=self._ssl, allow_redirects=True)
        except aiohttp.ClientError as e:
            raise web.HTTPBadGateway(text=str(e)) from None
        async with upstream:
            response = web.StreamResponse(status=upstream.status, reason=upstream.reason)
            for key, value in upstream.headers.items():
                if key.lower() not in self.DROP_RESPONSE:
                    response.headers.add(key, value)
            origin = request.headers.get("Origin")
            if origin == STEAM_ORIGIN:
                self._cors_headers(response, origin)
            await response.prepare(request)
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                await response.write(chunk)
            await response.write_eof()
            return response

