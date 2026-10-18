"""Fetching store lists: the Decky API (compat/store.md §1) and static indexes (§6.2), with
optional ed25519 signatures, version compatibility, and semver comparison."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import platform
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import aiohttp

from .sources import DECKY_CDN, StoreSource

log = logging.getLogger("remora.store")

CACHE_SECONDS = 300
FETCH_TIMEOUT = 30
MAX_INDEX_BYTES = 32 * 1024 * 1024
SIG_FORMAT = "decky-index/1"

try:  # optional: without it signatures are recorded as unchecked (signed: false)
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    HAVE_CRYPTO = True
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_CRYPTO = False


class SourceError(Exception):
    pass


# -- versions ----------------------------------------------------------------------------------

_SEMVER = re.compile(r"^[v=]*(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?"
                     r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z.-]+)?$")


def parse_version(text: str | None) -> tuple[Any, ...] | None:
    """A sortable key for a semver-ish version (what Decky's compare-versions accepts), or None."""
    m = _SEMVER.match((text or "0.0.0").strip())
    if m is None:
        return None
    nums = tuple(int(p or 0) for p in m.groups()[:4])
    pre = m.group(5)
    if pre is None:
        return nums + (1, ())  # a release sorts above its prereleases
    ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return nums + (0, ids)


def is_newer(remote: str | None, installed: str | None) -> bool:
    if remote is None or remote == installed:
        return False
    r, i = parse_version(remote), parse_version(installed)
    return r is not None and i is not None and r > i


def machine_arch() -> str:
    m = platform.machine().lower()
    return {"arm64": "aarch64", "amd64": "x86_64"}.get(m, m)


def compatible(version: dict[str, Any], decky_version: str) -> tuple[bool, str | None]:
    arch = version.get("arch")
    if isinstance(arch, list) and arch and "any" not in arch and machine_arch() not in arch:
        return False, f"built for {', '.join(map(str, arch))}, this device is {machine_arch()}"
    min_loader = version.get("min_loader")
    if isinstance(min_loader, str):
        need, have = parse_version(min_loader), parse_version(decky_version)
        if need is not None and have is not None and need > have:
            return False, f"needs loader {min_loader}"
    return True, None


# -- signatures --------------------------------------------------------------------------------

def signed_message(name: str, version: str, sha256: str) -> bytes:
    return f"{SIG_FORMAT}\n{name}\n{version}\n{sha256.lower()}".encode()


def check_signature(keys: list[dict[str, Any]], name: str, version: dict[str, Any]) -> bool | str:
    """True: a known key signed it. False: unsigned, or no way to check. "invalid": refuse it."""
    keys = [k for k in keys if isinstance(k, dict) and k.get("alg", "ed25519") == "ed25519"]
    sig = version.get("sig")
    if not keys:
        return False
    if not isinstance(sig, dict):
        return "invalid"  # a signing source sent an unsigned version: fail closed
    if not HAVE_CRYPTO:
        return False
    key = next((k for k in keys if k.get("id") == sig.get("key")), None)
    if key is None:
        return "invalid"
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(key["pub"]))
        pub.verify(base64.b64decode(sig.get("value", "")),
                   signed_message(name, str(version.get("name")), str(version.get("hash", ""))))
        return True
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return "invalid"


# -- fetching ----------------------------------------------------------------------------------

@dataclass
class Listing:
    plugins: list[dict[str, Any]]
    # name -> [version, hash, artifact]; for index sources, derived from the list
    versions: dict[str, list[Any]] = field(default_factory=dict)


def _read_file_url(url: str) -> bytes:
    path = Path(unquote(urlparse(url).path))
    with open(path, "rb") as f:
        data = f.read(MAX_INDEX_BYTES + 1)
    if len(data) > MAX_INDEX_BYTES:
        raise SourceError("the index is too large")
    return data


class Fetcher:
    def __init__(self, session_factory: Any, ssl: Any, decky_version: str) -> None:
        self._session = session_factory
        self._ssl = ssl
        self.decky_version = decky_version
        self._cache: dict[tuple[str, ...], tuple[float, Any]] = {}
        self._locks: dict[tuple[str, ...], asyncio.Lock] = {}

    def clear(self) -> None:
        self._cache.clear()

    async def _get_json(self, url: str) -> Any:
        if urlparse(url).scheme == "file":
            data = await asyncio.to_thread(_read_file_url, url)
        else:
            headers = {"X-Decky-Version": self.decky_version, "Accept": "application/json"}
            timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
            try:
                async with self._session().get(url, headers=headers, ssl=self._ssl,
                                               timeout=timeout) as res:
                    if res.status == 426:
                        raise SourceError(
                            f"this store requires a newer loader than {self.decky_version} (HTTP 426)")
                    if res.status != 200:
                        raise SourceError(f"HTTP {res.status} from {url}")
                    data = await res.content.read(MAX_INDEX_BYTES + 1)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                raise SourceError(f"can't reach {url}: {e or type(e).__name__}") from None
            if len(data) > MAX_INDEX_BYTES:
                raise SourceError("the store answer is too large")
        try:
            return json.loads(data)
        except ValueError:
            raise SourceError(f"{url} did not answer with JSON") from None

    async def _cached(self, key: tuple[str, ...], load: Any) -> Any:
        hit = self._cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._cache.get(key)
            if hit is not None and time.monotonic() - hit[0] < CACHE_SECONDS:
                return hit[1]
            value = await load()
            self._cache[key] = (time.monotonic(), value)
            return value

    async def listing(self, source: StoreSource) -> Listing:
        key = (source.id, source.kind, source.url, "list")
        if source.kind == "decky":
            return await self._cached(key, lambda: self._decky_list(source))
        return await self._cached(key, lambda: self._index(source))

    async def versions(self, source: StoreSource) -> dict[str, list[Any]]:
        """The update map, {name: [version, hash, artifact]}."""
        if source.kind == "index":
            return (await self.listing(source)).versions
        key = (source.id, source.kind, source.url, "versions")
        return await self._cached(key, lambda: self._decky_versions(source))

    # -- Decky API -----------------------------------------------------------------------------

    async def _decky_list(self, source: StoreSource) -> Listing:
        data = await self._get_json(source.decky_base)
        if not isinstance(data, list):
            raise SourceError("the store list is not a JSON array")
        return Listing([p for p in (self._decky_plugin(source, raw) for raw in data) if p])

    def _decky_plugin(self, source: StoreSource, raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            return None
        plugin = _plugin_defaults(raw, source)
        versions = []
        for v in raw.get("versions") or []:
            if not isinstance(v, dict) or not v.get("hash"):
                continue
            v = dict(v)
            # Decky's own servers never send artifact; zips live on their CDN by hash
            v["artifact"] = v.get("artifact") or DECKY_CDN.format(hash=v["hash"])
            v["signed"] = False
            v["compatible"] = True
            versions.append(v)
        plugin["versions"] = versions
        return plugin

    async def _decky_versions(self, source: StoreSource) -> dict[str, list[Any]]:
        data = await self._get_json(source.decky_base + "/versions")
        if not isinstance(data, dict):
            raise SourceError("the store's version map is not a JSON object")
        result = {}
        for name, entry in data.items():
            if isinstance(entry, list) and len(entry) >= 2:
                artifact = entry[2] if len(entry) > 2 and entry[2] else DECKY_CDN.format(hash=entry[1])
                result[name] = [str(entry[0]), str(entry[1]), artifact]
        return result

    # -- static index --------------------------------------------------------------------------

    async def _index(self, source: StoreSource) -> Listing:
        url = source.index_url
        data = await self._get_json(url)
        keys: list[dict[str, Any]] = list(source.keys)
        if isinstance(data, dict):
            meta = data.get("source") if isinstance(data.get("source"), dict) else {}
            if not keys and isinstance(meta.get("keys"), list):
                keys = meta["keys"]
            raw_plugins = data.get("plugins")
        else:
            raw_plugins = data  # a bare Decky list works as an index too
        if not isinstance(raw_plugins, list):
            raise SourceError("the index has no plugin list")
        if keys and not HAVE_CRYPTO:
            log.warning("%s: signed index, but the cryptography module is missing; "
                        "signatures are not checked", source.id)
        plugins, versions = [], {}
        for raw in raw_plugins:
            if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                continue
            plugin = _plugin_defaults(raw, source)
            out = []
            for v in raw.get("versions") or []:
                if not isinstance(v, dict) or not v.get("hash") or v.get("name") is None:
                    continue
                v = dict(v)
                v["name"] = str(v["name"])
                # no fallback to anyone's CDN: an index names its own zips (compat/store.md §6.2)
                v["artifact"] = urljoin(url, v["artifact"]) if v.get("artifact") else None
                v["signed"] = check_signature(keys, plugin["name"], v)
                ok, why = compatible(v, self.decky_version)
                v["compatible"] = ok
                if why:
                    v["incompatible_reason"] = why
                out.append(v)
            plugin["versions"] = out
            plugins.append(plugin)
            newest = next((v for v in out if v["compatible"] and v["artifact"]
                           and v["signed"] != "invalid"), None)
            if newest is not None:
                versions[plugin["name"]] = [newest["name"], newest["hash"], newest["artifact"]]
        return Listing(plugins, versions)


def _plugin_defaults(raw: dict[str, Any], source: StoreSource) -> dict[str, Any]:
    plugin = dict(raw)
    plugin.setdefault("id", None)
    plugin.setdefault("author", "")
    plugin.setdefault("description", "")
    plugin["tags"] = [str(t) for t in raw.get("tags") or [] if isinstance(t, (str, int))]
    plugin.setdefault("image_url", None)
    plugin["source"] = source.id
    return plugin
