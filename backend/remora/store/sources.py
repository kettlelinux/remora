"""Store sources: the list in settings, the built-in Decky stores, and migration from Decky's
`store` / `store-url` settings (compat/store.md §1.1, §6.2)."""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

from ..settings import Settings
from ..wsrouter import RouteError

log = logging.getLogger("remora.store")

SOURCES_KEY = "remora.storeSources"
KINDS = ("decky", "index")

OFFICIAL_ID = "decky"
TESTING_ID = "decky-testing"
OFFICIAL_URL = "https://plugins.deckbrew.xyz"
TESTING_URL = "https://testing.deckbrew.xyz"

# where the official stores keep zips; their list never carries `artifact` (compat/store.md §1.6)
DECKY_CDN = "https://cdn.tzatzikiweeb.moe/file/steam-deck-homebrew/versions/{hash}.zip"


@dataclass
class StoreSource:
    id: str
    name: str
    kind: str
    url: str
    enabled: bool = True
    builtin: bool = False
    # pinned ed25519 keys for an index source ({id, alg, pub}); empty = trust the index's own
    keys: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        if not self.keys:
            del data["keys"]
        return data

    @property
    def decky_base(self) -> str:
        """The Decky API base, which includes the /plugins segment (compat/store.md §1.1)."""
        url = self.url.rstrip("/")
        return url if urlparse(url).path.endswith("/plugins") else url + "/plugins"

    @property
    def index_url(self) -> str:
        url = self.url
        return url if urlparse(url).path.endswith(".json") else url.rstrip("/") + "/index.json"


def builtins() -> list[StoreSource]:
    return [
        StoreSource(OFFICIAL_ID, "Decky", "decky", OFFICIAL_URL, True, True),
        StoreSource(TESTING_ID, "Decky testing", "decky", TESTING_URL, False, True),
    ]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "source"


def _check_url(url: Any, kind: str, developer: bool) -> str:
    if not isinstance(url, str) or not url.strip():
        raise RouteError("ValueError", "a store source needs a URL")
    url = url.strip()
    scheme = urlparse(url).scheme
    if scheme in ("http", "https"):
        return url
    if scheme == "file" and kind == "index" and developer:
        return url
    raise RouteError("ValueError", f"unsupported store URL {url!r}: use http(s)"
                     + (" (file:// needs developer mode)" if scheme == "file" else ""))


def _parse(raw: Any, developer: bool) -> StoreSource:
    if not isinstance(raw, dict):
        raise RouteError("ValueError", "a store source must be an object")
    kind = raw.get("kind", "decky")
    if kind not in KINDS:
        raise RouteError("ValueError", f"unknown store source kind {kind!r}")
    url = _check_url(raw.get("url"), kind, developer)
    name = str(raw.get("name") or urlparse(url).netloc or url)
    keys = raw.get("keys") or []
    if not isinstance(keys, list) or not all(isinstance(k, dict) for k in keys):
        raise RouteError("ValueError", "keys must be a list of {id, alg, pub}")
    return StoreSource(id=str(raw.get("id") or ""), name=name, kind=kind, url=url,
                       enabled=bool(raw.get("enabled", True)), builtin=False, keys=keys)


def _migrate(settings: Settings) -> list[StoreSource]:
    """First run: turn Decky's single store choice into the source list."""
    sources = builtins()
    choice = settings.get("store")
    if choice == 1:
        sources[1].enabled = True
    elif choice == 2:
        url = settings.get("store-url")
        if isinstance(url, str) and urlparse(url).scheme in ("http", "https"):
            sources.append(StoreSource("custom", urlparse(url).netloc or "Custom", "decky", url, True))
    return sources


class Sources:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _developer(self) -> bool:
        return bool(self.settings.get("developer.enabled"))

    def all(self) -> list[StoreSource]:
        raw = self.settings.get(SOURCES_KEY)
        if not isinstance(raw, list):
            sources = _migrate(self.settings)
            self.settings.set(SOURCES_KEY, [s.to_json() for s in sources])
            return sources
        stored = {r.get("id"): r for r in raw if isinstance(r, dict)}
        sources = []
        builtin_ids = set()
        for b in builtins():
            builtin_ids.add(b.id)
            if b.id in stored:
                b.enabled = bool(stored[b.id].get("enabled", b.enabled))
            sources.append(b)
        for r in raw:
            if not isinstance(r, dict) or r.get("id") in builtin_ids:
                continue
            try:
                sources.append(_parse(r, developer=True))
            except RouteError as e:
                log.warning("ignoring store source %r: %s", r.get("id"), e.message)
        # keep the user's order for the custom ones, the built-ins first
        return sources

    def enabled(self) -> list[StoreSource]:
        return [s for s in self.all() if s.enabled]

    def get(self, source_id: str | None) -> StoreSource | None:
        return next((s for s in self.all() if s.id == source_id), None)

    def set(self, raw: Any) -> list[StoreSource]:
        """Replace the list. Built-ins can only be switched on or off; they always stay listed."""
        if not isinstance(raw, list):
            raise RouteError("ValueError", "set_sources takes a list of sources")
        developer = self._developer()
        result = builtins()
        by_id = {s.id: s for s in result}
        taken = set(by_id)
        for item in raw:
            if isinstance(item, dict) and item.get("id") in by_id:
                by_id[item["id"]].enabled = bool(item.get("enabled", True))
                continue
            source = _parse(item, developer)
            base = source.id = _slug(source.id or source.name)
            n = 2
            while source.id in taken:
                source.id = f"{base}-{n}"
                n += 1
            taken.add(source.id)
            result.append(source)
        self.settings.set(SOURCES_KEY, [s.to_json() for s in result])
        return result
