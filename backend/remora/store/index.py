"""Fetching store lists: the Decky API (compat/store.md §1) and static indexes (§6.2), with
optional ed25519 signatures, version compatibility, and semver comparison."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import platform
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urljoin, urlparse

import aiohttp

from ..device import normalize_machine
from .sources import DECKY_CDN, StoreSource

log = logging.getLogger("remora.store")

CACHE_SECONDS = 300
ERROR_CACHE_SECONDS = 60  # a failing source is asked again after this long, not on every call
STALE_SECONDS = 30  # a hash miss refetches only lists older than this
FETCH_TIMEOUT = 30
CONNECT_TIMEOUT = 15
MAX_INDEX_BYTES = 32 * 1024 * 1024
SIG_FORMAT = "decky-index/1"

# Needed for sources with pinned keys: without it their versions are "invalid" (refused). The
# packages depend on the distro's python-cryptography; Remora's private site dir doesn't hide the
# system site-packages (python3 -P keeps them on sys.path).
try:
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
    return normalize_machine(platform.machine())


def compatible(version: dict[str, Any], decky_version: str,
               arch_here: str | None = None) -> tuple[bool, str | None]:
    arch, here = version.get("arch"), arch_here or machine_arch()
    if isinstance(arch, list) and arch and "any" not in arch and here not in arch:
        return False, f"built for {', '.join(map(str, arch))}, this device is {here}"
    min_loader = version.get("min_loader")
    if isinstance(min_loader, str):
        need, have = parse_version(min_loader), parse_version(decky_version)
        if need is not None and have is not None and need > have:
            return False, f"needs loader {min_loader}"
    return True, None


# -- signatures --------------------------------------------------------------------------------

def signed_message(name: str, version: str, sha256: str) -> bytes:
    return f"{SIG_FORMAT}\n{name}\n{version}\n{sha256.lower()}".encode()


def check_signature(keys: list[dict[str, Any]], name: str, version: dict[str, Any],
                    pinned: bool = True) -> bool | str:
    """True: a key the user pinned signed it. "self": signed by a key the index lists itself,
    which proves nothing about who wrote it (whoever can change the index can change its keys).
    False: unsigned, or only self-listed keys and no way to check them. "invalid": refuse it."""
    keys = [k for k in keys if isinstance(k, dict) and k.get("alg", "ed25519") == "ed25519"]
    sig = version.get("sig")
    if not keys:
        return False
    if not isinstance(sig, dict):
        return "invalid"  # a signing source sent an unsigned version: fail closed
    if not HAVE_CRYPTO:
        # pinned keys are a promise the user relies on: never turn them into "unchecked"
        return "invalid" if pinned else False
    key = next((k for k in keys if k.get("id") == sig.get("key")), None)
    if key is None:
        return "invalid"
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(key["pub"]))
        pub.verify(base64.b64decode(sig.get("value", "")),
                   signed_message(name, str(version.get("name")), str(version.get("hash", ""))))
        return True if pinned else "self"
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return "invalid"


def newest(versions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The highest installable version by semver; ones that don't parse rank below all that do,
    in list order (Decky lists newest first)."""
    usable = [v for v in versions if v.get("compatible") and v.get("artifact")
              and v.get("signed") != "invalid"]
    if not usable:
        return None
    ranked = [(parse_version(v["name"]), -i, v) for i, v in enumerate(usable)]
    parsed = [r for r in ranked if r[0] is not None]
    if parsed:
        return max(parsed, key=lambda r: (r[0], r[1]))[2]
    return usable[0]


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


# -- fetching ----------------------------------------------------------------------------------

@dataclass
class Listing:
    plugins: list[dict[str, Any]]
    # name -> [version, hash, artifact]; for index sources, derived from the list
    versions: dict[str, list[Any]] = field(default_factory=dict)


def _read_file_url(url: str) -> bytes:
    path = Path(unquote(urlparse(url).path))
    # O_NONBLOCK: a FIFO there must not hang the listing; only regular files are read
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as e:
        raise SourceError(f"can't open {path}: {e.strerror}") from None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SourceError(f"{path} is not a regular file")
        data = f.read(MAX_INDEX_BYTES + 1)
    if len(data) > MAX_INDEX_BYTES:
        raise SourceError("the index is too large")
    return data


def source_problem(source: StoreSource, developer: bool) -> str | None:
    """Why this source may not be fetched right now, or None. Checked at every fetch, so a
    file:// or plain-http source stops working when developer mode is turned off."""
    scheme = urlparse(source.url).scheme
    if scheme == "https":
        return None
    if scheme == "file":
        if source.kind == "index" and developer:
            return None
        return "file:// sources need developer mode"
    if scheme == "http":
        if developer or (source.kind == "index" and source.keys):
            return None
        return "plain http sources need pinned keys or developer mode; use https"
    return f"unsupported URL scheme {scheme!r}"


class Fetcher:
    def __init__(self, session_factory: Any, ssl: Any, decky_version: str,
                 developer: Callable[[], bool] = lambda: False,
                 arch: Callable[[], str] = machine_arch) -> None:
        self._session = session_factory
        self.arch = arch  # the device's CPU, for index versions' `arch`
        self._ssl = ssl
        self.decky_version = decky_version
        self.developer = developer
        self.allow_http = False  # tests only: plain http to a local fake store
        # key -> (time, value, error): a failure is kept for ERROR_CACHE_SECONDS
        self._cache: dict[tuple[str, ...], tuple[float, Any, SourceError | None]] = {}
        self._locks: dict[tuple[str, ...], asyncio.Lock] = {}

    def clear(self) -> None:
        self._cache.clear()

    def drop_stale(self, older_than: float | None = None) -> int:
        """Forget successful answers older than `older_than` seconds (failures keep their own,
        shorter, lifetime). Returns how many were dropped."""
        older_than = STALE_SECONDS if older_than is None else older_than
        now = time.monotonic()
        stale = [k for k, (t, _, err) in self._cache.items() if err is None and now - t >= older_than]
        for k in stale:
            del self._cache[k]
        return len(stale)

    def check(self, source: StoreSource) -> None:
        problem = source_problem(source, bool(self.developer()))
        if problem is not None and not (self.allow_http and urlparse(source.url).scheme == "http"):
            raise SourceError(problem)

    async def _get_json(self, url: str) -> Any:
        if urlparse(url).scheme == "file":
            data = await asyncio.to_thread(_read_file_url, url)
        else:
            headers = {"X-Decky-Version": self.decky_version, "Accept": "application/json"}
            timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT, connect=CONNECT_TIMEOUT,
                                            sock_connect=CONNECT_TIMEOUT)
            try:
                async with self._session().get(url, headers=headers, ssl=self._ssl,
                                               timeout=timeout) as res:
                    if res.status == 426:
                        raise SourceError(
                            f"this store requires a newer loader than {self.decky_version} (HTTP 426)")
                    if res.status != 200:
                        raise SourceError(f"HTTP {res.status} from {url}")
                    # read(n) returns what has arrived so far, not the whole body
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in res.content.iter_chunked(256 * 1024):
                        chunks.append(chunk)
                        size += len(chunk)
                        if size > MAX_INDEX_BYTES:
                            break
                    data = b"".join(chunks)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                raise SourceError(f"can't reach {url}: {e or type(e).__name__}") from None
            if len(data) > MAX_INDEX_BYTES:
                raise SourceError("the store answer is too large")
        try:
            return json.loads(data)
        except ValueError:
            raise SourceError(f"{url} did not answer with JSON") from None

    def _hit(self, key: tuple[str, ...]) -> tuple[bool, Any]:
        hit = self._cache.get(key)
        if hit is None:
            return False, None
        t, value, err = hit
        if time.monotonic() - t >= (ERROR_CACHE_SECONDS if err is not None else CACHE_SECONDS):
            return False, None
        if err is not None:
            raise SourceError(str(err))
        return True, value

    async def _cached(self, key: tuple[str, ...], load: Any) -> Any:
        found, value = self._hit(key)
        if found:
            return value
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            found, value = self._hit(key)
            if found:
                return value
            try:
                value = await load()
            except SourceError as e:
                self._cache[key] = (time.monotonic(), None, e)
                raise
            self._cache[key] = (time.monotonic(), value, None)
            return value

    async def listing(self, source: StoreSource) -> Listing:
        self.check(source)
        key = (source.id, source.kind, source.url, "list")
        if source.kind == "decky":
            return await self._cached(key, lambda: self._decky_list(source))
        return await self._cached(key, lambda: self._index(source))

    async def versions(self, source: StoreSource) -> dict[str, list[Any]]:
        """The update map, {name: [version, hash, artifact]}."""
        self.check(source)
        if source.kind == "index":
            return (await self.listing(source)).versions
        key = (source.id, source.kind, source.url, "versions")
        return await self._cached(key, lambda: self._decky_versions(source))

    # -- Decky API -----------------------------------------------------------------------------

    async def _decky_list(self, source: StoreSource) -> Listing:
        data = await self._get_json(source.decky_base)
        if not isinstance(data, list):
            raise SourceError("the store list is not a JSON array")
        return Listing([p for p in (_safely(source, raw, self._decky_plugin) for raw in data) if p])

    def _decky_plugin(self, source: StoreSource, raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            return None
        plugin = _plugin_defaults(raw, source)
        versions = []
        for v in _version_dicts(raw):
            # Decky's own servers never send artifact; zips live on their CDN by hash
            artifact = v.get("artifact")
            v["artifact"] = artifact if isinstance(artifact, str) and artifact \
                else DECKY_CDN.format(hash=v["hash"])
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
            if isinstance(entry, list) and len(entry) >= 2 and isinstance(entry[1], str) and entry[1]:
                artifact = entry[2] if len(entry) > 2 and isinstance(entry[2], str) and entry[2] \
                    else DECKY_CDN.format(hash=entry[1])
                result[str(name)] = [str(entry[0]), entry[1], artifact]
        return result

    # -- static index --------------------------------------------------------------------------

    async def _index(self, source: StoreSource) -> Listing:
        url = source.index_url
        data = await self._get_json(url)
        keys: list[dict[str, Any]] = list(source.keys)
        pinned = bool(keys)
        if isinstance(data, dict):
            meta = data.get("source") if isinstance(data.get("source"), dict) else {}
            if not keys and isinstance(meta.get("keys"), list):
                keys = meta["keys"]
            raw_plugins = data.get("plugins")
        else:
            raw_plugins = data  # a bare Decky list works as an index too
        if not isinstance(raw_plugins, list):
            raise SourceError("the index has no plugin list")
        if pinned and not HAVE_CRYPTO:
            log.error("%s: keys are pinned, but the cryptography module is missing; every version "
                      "is refused (install python-cryptography)", source.id)
        plugins, versions = [], {}

        def one(src: StoreSource, raw: Any) -> dict[str, Any] | None:
            if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                return None
            plugin = _plugin_defaults(raw, src)
            out = []
            for v in _version_dicts(raw):
                artifact = v.get("artifact")
                # no fallback to anyone's CDN: an index names its own zips (compat/store.md §6.2)
                v["artifact"] = urljoin(url, artifact) if isinstance(artifact, str) and artifact else None
                v["signed"] = check_signature(keys, plugin["name"], v, pinned)
                ok, why = compatible(v, self.decky_version, self.arch())
                v["compatible"] = ok
                if why:
                    v["incompatible_reason"] = why
                out.append(v)
            plugin["versions"] = out
            return plugin

        for raw in raw_plugins:
            plugin = _safely(source, raw, one)
            if plugin is None:
                continue
            plugins.append(plugin)
            best = newest(plugin["versions"])
            if best is not None:
                versions[plugin["name"]] = [best["name"], best["hash"], best["artifact"]]
        return Listing(plugins, versions)


def _safely(source: StoreSource, raw: Any, parse: Callable[[StoreSource, Any], Any]) -> Any:
    """One malformed plugin is skipped, not the whole list."""
    try:
        return parse(source, raw)
    except Exception as e:
        name = raw.get("name") if isinstance(raw, dict) else None
        log.warning("%s: skipping malformed plugin %r: %s: %s", source.id, name, type(e).__name__, e)
        return None


def _version_dicts(raw: dict[str, Any]) -> list[dict[str, Any]]:
    versions = raw.get("versions")
    out = []
    for v in versions if isinstance(versions, list) else []:
        if not isinstance(v, dict) or not isinstance(v.get("hash"), str) or not v["hash"] \
                or v.get("name") is None or isinstance(v.get("name"), (dict, list)):
            continue
        v = dict(v)
        v["name"] = str(v["name"])
        v["hash"] = v["hash"].strip().lower()
        if "size" in v:
            size = _as_int(v["size"])
            if size is None or size <= 0:
                v.pop("size")
            else:
                v["size"] = size
        out.append(v)
    return out


def _plugin_defaults(raw: dict[str, Any], source: StoreSource) -> dict[str, Any]:
    plugin = dict(raw)
    plugin.setdefault("id", None)
    for text in ("author", "description"):
        plugin[text] = raw[text] if isinstance(raw.get(text), str) else ""
    tags = raw.get("tags")
    plugin["tags"] = [str(t) for t in tags if isinstance(t, (str, int)) and not isinstance(t, bool)] \
        if isinstance(tags, list) else []
    plugin["downloads"] = max(_as_int(raw.get("downloads")) or 0, 0)
    if not isinstance(plugin.get("image_url"), str):
        plugin["image_url"] = None
    plugin["source"] = source.id
    return plugin
