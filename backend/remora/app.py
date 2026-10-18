"""Wiring: settings, plugins, routes, the HTTP server and the Steam injector."""

from __future__ import annotations

import asyncio
import logging
import secrets
import signal

from aiohttp import web

from .cdp import CDP, Injector
from .config import Config
from .plugins.manager import PluginManager
from .server import Server
from .settings import Settings
from .store import StoreService
from .utilities import Utilities
from .wsrouter import WSRouter

log = logging.getLogger("remora")


class Remora:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.token = secrets.token_urlsafe(32)
        self.settings = Settings(cfg.settings_file)
        self.ws = WSRouter()
        self.cdp = CDP(cfg)
        self.manager = PluginManager(cfg, self.settings, self.ws)
        self.utilities = Utilities(cfg, self.settings, self.manager, self.cdp, self.ws)
        self.store = StoreService(cfg, self.settings, self.manager, self.ws)
        self.server = Server(cfg, self.token, self.ws, self.manager, self.utilities)
        self.injector = Injector(cfg, self.cdp, self.token)
        self.utilities.register(self.ws)
        self.store.register(self.ws)
        self.ws.add_route("remora/injector_status", self.injector_status)
        self._runner: web.AppRunner | None = None

    async def injector_status(self) -> str:
        return self.injector.status

    async def start(self) -> None:
        log.info("Remora %s (Decky compatibility %s), user %s, plugins in %s",
                 self.cfg.version, self.cfg.decky_version, self.cfg.user, self.cfg.plugins_dir)
        if not self.cfg.running_as_root:
            log.warning("not running as root: every plugin runs as %s, root plugins included",
                        self.cfg.user)
        self._runner = web.AppRunner(self.server.app(), handle_signals=False, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.cfg.host, self.cfg.port, reuse_address=True)
        await site.start()
        log.info("listening on %s:%d", self.cfg.host, self.cfg.port)
        await self.manager.start()
        if self.cfg.inject:
            self.injector.start()

    async def stop(self) -> None:
        log.info("shutting down")
        await self.injector.stop()
        await self.manager.shutdown()
        await self.store.close()
        await self.ws.close()
        await self.utilities.close()
        await self.cdp.close()
        if self._runner is not None:
            await self._runner.cleanup()


async def run(cfg: Config) -> None:
    remora = Remora(cfg)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await remora.start()
    await stop.wait()
    await remora.stop()
