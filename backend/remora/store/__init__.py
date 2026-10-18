"""The plugin store: sources, listing, install, update, uninstall (docs/PROTOCOL.md)."""

from __future__ import annotations

from typing import Any

from ..config import Config
from ..plugins.manager import PluginManager
from ..settings import Settings
from ..wsrouter import WSRouter


class StoreService:
    def __init__(self, cfg: Config, settings: Settings, manager: PluginManager, ws: WSRouter) -> None:
        self.cfg = cfg
        self.settings = settings
        self.manager = manager
        self.ws = ws

    def register(self, ws: WSRouter) -> None:
        pass

    async def close(self) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {}
