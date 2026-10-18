"""The install pipeline: download, verify, unpack into staging, ask for root, swap atomically,
roll back on failure (compat/store.md §2, §6.2 "client rules"); and uninstall."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import unquote, urlparse

import aiohttp

from ..config import Config
from ..plugins import manifest
from ..plugins.manager import ROOT_APPROVED, Entry, PluginManager
from ..plugins.manifest import PluginInfo
from ..settings import Settings
from ..wsrouter import RouteError, WSRouter
from . import archive

log = logging.getLogger("remora.store")

INSTALL_SOURCE = "remora.installSource"
SETTING_LISTS = ("pluginOrder", "hiddenPlugins", "frozenPlugins", "disabled_plugins")
MAX_DOWNLOAD = 256 * 1024 * 1024
ROOT_PROMPT_SECONDS = 300.0
CHUNK = 256 * 1024


class InstallError(Exception):
    pass


@dataclass
class InstallItem:
    artifact: str
    name: str
    version: str
    hash: str
    install_type: int = 0
    # filled from the store listing the hash was found in, if any
    source: str | None = None
    signed: bool | str = False
    size: int | None = None
    compatible: bool = True
    incompatible_reason: str | None = None
    root: bool | None = None

    def prompt(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "hash": self.hash,
                "install_type": self.install_type}

    def details(self) -> dict[str, Any]:
        return {**self.prompt(), "source": self.source, "signed": self.signed,
                "compatible": self.compatible, "root": self.root}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _rmtree(path: Path) -> None:
    if os.path.lexists(path):
        if path.is_symlink() or not path.is_dir():
            path.unlink()
        else:
            shutil.rmtree(path)


def _chown_root(path: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(path):
        os.lchown(dirpath, 0, 0)
        for name in dirnames + filenames:
            os.lchown(os.path.join(dirpath, name), 0, 0)


class Installer:
    def __init__(self, cfg: Config, settings: Settings, manager: PluginManager, ws: WSRouter,
                 session: Callable[[], aiohttp.ClientSession], ssl: Any,
                 after_install: Callable[[InstallItem, bool], Awaitable[None]] | None = None) -> None:
        self.cfg = cfg
        self.settings = settings
        self.manager = manager
        self.ws = ws
        self._session = session
        self._ssl = ssl
        self.after_install = after_install
        self.root_prompt_seconds = ROOT_PROMPT_SECONDS
        self._root_waits: dict[str, asyncio.Future[bool]] = {}
        self._lock = asyncio.Lock()

    @property
    def developer(self) -> bool:
        return bool(self.settings.get("developer.enabled"))

    # -- checks ------------------------------------------------------------------------------

    def managed(self, info: PluginInfo) -> bool:
        """A real folder directly in plugins/: not a link to a system copy, not an extra dir."""
        if os.path.islink(info.path):
            return False
        return os.path.realpath(info.path.parent) == os.path.realpath(self.cfg.plugins_dir)

    def _check_managed(self, entry: Entry, action: str) -> None:
        if not self.managed(entry.info):
            raise InstallError(f"{entry.info.name} is part of the system image ({entry.info.path} is "
                               f"not a plugin folder Remora manages), so it can't be {action} here; "
                               "it changes with system updates")

    # -- root prompt -------------------------------------------------------------------------

    def answer_root(self, request_id: str, allow: bool) -> None:
        fut = self._root_waits.get(request_id)
        if fut is None or fut.done():
            log.info("root answer for unknown or finished request %s", request_id)
            return
        fut.set_result(bool(allow))

    async def _ask_root(self, info: PluginInfo) -> bool:
        prompt_id = secrets.token_hex(8)
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._root_waits[prompt_id] = fut
        try:
            await self.ws.emit("remora/root_prompt", prompt_id, info.name, info.version)
            return await asyncio.wait_for(fut, self.root_prompt_seconds)
        except asyncio.TimeoutError:
            log.info("%s: no answer to the root prompt, cancelling the install", info.name)
            return False
        finally:
            self._root_waits.pop(prompt_id, None)

    # -- download ----------------------------------------------------------------------------

    async def _progress(self, percent: int, key: str) -> None:
        await self.ws.emit("loader/plugin_download_info", percent, key)

    async def _download(self, url: str, dest: Path, size: int | None) -> None:
        scheme = urlparse(url).scheme
        limit = size if size else MAX_DOWNLOAD
        if scheme == "file":
            if not self.developer:
                raise InstallError("file:// installs need developer mode")
            src = Path(unquote(urlparse(url).path))
            await asyncio.to_thread(self._copy_local, src, dest, limit)
            return
        if scheme not in ("http", "https"):
            raise InstallError(f"unsupported download URL {url!r}")
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
        try:
            async with self._session().get(url, ssl=self._ssl, timeout=timeout) as res:
                if res.status != 200:
                    raise InstallError(f"download failed: HTTP {res.status} from {url}")
                total = res.content_length
                if total is not None and total > limit:
                    raise InstallError(f"the download is {total} bytes, more than the {limit} allowed")
                got, shown = 0, 10
                with open(dest, "wb") as f:
                    async for chunk in res.content.iter_chunked(CHUNK):
                        got += len(chunk)
                        if got > limit:
                            raise InstallError(f"the download is larger than the {limit} bytes allowed")
                        await asyncio.to_thread(f.write, chunk)
                        if total:
                            percent = 10 + int(60 * got / total)
                            if percent >= shown + 5:
                                shown = percent
                                await self._progress(percent, "Remora.install.downloading")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise InstallError(f"download failed: {e or type(e).__name__}") from None
        if size and got != size:
            raise InstallError(f"the download is {got} bytes, the store says {size}")

    @staticmethod
    def _copy_local(src: Path, dest: Path, limit: int) -> None:
        with open(src, "rb") as f, open(dest, "wb") as out:
            got = 0
            while chunk := f.read(CHUNK):
                got += len(chunk)
                if got > limit:
                    raise InstallError(f"{src} is larger than the {limit} bytes allowed")
                out.write(chunk)

    async def _remote_binaries(self, plugin_dir: Path) -> None:
        try:
            package = json.loads((plugin_dir / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        binaries = package.get("remote_binary") if isinstance(package, dict) else None
        if not binaries:
            return
        if not isinstance(binaries, list):
            raise InstallError("package.json remote_binary is not a list")
        bin_dir = plugin_dir / "bin"
        if bin_dir.is_symlink() or (bin_dir.exists() and not bin_dir.is_dir()):
            raise InstallError("the plugin's bin is not a folder")
        bin_dir.mkdir(mode=0o755, exist_ok=True)
        os.chmod(bin_dir, 0o755)
        for entry in binaries:
            if not isinstance(entry, dict):
                raise InstallError("bad remote_binary entry")
            name, url, want = entry.get("name"), entry.get("url"), entry.get("sha256hash")
            if not isinstance(name, str) or not name or "/" in name or "\\" in name \
                    or name in (".", "..") or "\0" in name:
                raise InstallError(f"bad remote_binary name {name!r}")
            if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https"):
                raise InstallError(f"remote_binary {name}: bad URL {url!r}")
            if not isinstance(want, str) or not want:
                raise InstallError(f"remote_binary {name}: no sha256hash")
            target = bin_dir / name
            tmp = bin_dir / f".{name}.part"
            await self._download(url, tmp, None)
            got = await asyncio.to_thread(_sha256_file, tmp)
            if got != want.strip().lower():
                tmp.unlink(missing_ok=True)
                raise InstallError(f"remote_binary {name}: hash mismatch")
            if target.is_symlink():
                target.unlink()
            os.replace(tmp, target)
            os.chmod(target, 0o755)

    # -- install -----------------------------------------------------------------------------

    async def install(self, item: InstallItem) -> None:
        """Install one plugin; failures are reported to the frontend, not raised."""
        async with self._lock:
            await self.ws.emit("loader/plugin_download_start", item.name)
            try:
                await self._install(item)
            except (InstallError, archive.ArchiveError, manifest.ManifestError, RouteError, OSError) as e:
                message = e.message if isinstance(e, RouteError) else str(e)
                log.error("installing %s %s failed: %s", item.name, item.version, message)
                await self.ws.emit("remora/install_failed", item.name, message)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep the loader alive whatever goes wrong
                log.exception("installing %s failed", item.name)
                await self.ws.emit("remora/install_failed", item.name, f"{type(e).__name__}: {e}")
            finally:
                await self.ws.emit("loader/plugin_download_finish", item.name)

    async def _install(self, item: InstallItem) -> None:
        dev_install = self.developer and not item.hash
        if not item.hash and not dev_install:
            raise InstallError("no hash for this download (only developer mode installs without one)")
        if item.signed == "invalid":
            raise InstallError("the store's signature for this version does not match; not installing")
        if not item.compatible and not self.developer:
            raise InstallError(f"this version does not fit this device: {item.incompatible_reason}")
        existing = self.manager.plugins.get(item.name)
        if existing is not None:
            self._check_managed(existing, "updated")

        plugins = self.cfg.plugins_dir
        plugins.mkdir(parents=True, exist_ok=True)
        tag = secrets.token_hex(6)
        staging = plugins / f".staging-{tag}"
        with self.manager.watch_paused():
            staging.mkdir(mode=0o700)
            try:
                await self._install_staged(item, staging, tag, dev_install)
            finally:
                await asyncio.to_thread(_rmtree, staging)

    async def _install_staged(self, item: InstallItem, staging: Path, tag: str, dev_install: bool) -> None:
        zip_path = staging / "plugin.zip"
        await self._progress(5, "Remora.install.downloading")
        await self._download(item.artifact, zip_path, item.size)
        await self._progress(70, "Remora.install.verifying")
        got = await asyncio.to_thread(_sha256_file, zip_path)
        if item.hash and got != item.hash.strip().lower():
            raise InstallError(f"hash mismatch: expected {item.hash}, the download is {got}")

        await self._progress(75, "Remora.install.unpacking")
        unpacked = staging / "unpacked"
        unpacked.mkdir(mode=0o755)

        def unpack() -> Path:
            try:
                with zipfile.ZipFile(zip_path) as zf:
                    return archive.extract(zf, unpacked)
            except zipfile.BadZipFile as e:
                raise InstallError(f"not a valid zip: {e}") from None

        staged = await asyncio.to_thread(unpack)
        zip_path.unlink()
        info = manifest.read_plugin(staged)
        if info.name != item.name:
            if not dev_install:
                raise InstallError(f"the zip holds a plugin called {info.name!r}, not {item.name!r}")
            log.info("developer install: %s is called %r", item.artifact, info.name)
            item.name = info.name

        existing = self.manager.plugins.get(item.name)
        if existing is not None:
            self._check_managed(existing, "updated")
            target = existing.info.path
        else:
            target = self.cfg.plugins_dir / staged.name
            if os.path.lexists(target):
                other = self.manager.find_dir(staged.name)
                who = f"plugin {other.info.name!r}" if other else "something else"
                raise InstallError(f"the folder {staged.name} is already taken by {who}")

        await self._progress(80, "Remora.install.binaries")
        await self._remote_binaries(staged)
        if self.cfg.running_as_root:
            await asyncio.to_thread(_chown_root, staged)

        if info.root and not self.manager.root_approved(info.name):
            if not await self._ask_root(info):
                raise InstallError(f"{info.name} asks for root, and that was not allowed; not installed")
            approve_root = True
        else:
            approve_root = False

        await self._progress(90, "Remora.install.installing")
        await self._swap(staged, target, info, existing, tag, approve_root)
        if existing is None:
            self.settings.list_add("pluginOrder", info.name)
        self._record_source(info.name, item.source)
        await self._progress(95, "Remora.install.installing")
        if self.after_install is not None:
            try:
                await self.after_install(item, existing is not None)
            except Exception:
                log.debug("after-install hook failed", exc_info=True)
        log.info("installed %s %s into %s", info.name, info.version, target)

    async def _swap(self, staged: Path, target: Path, info: PluginInfo, existing: Entry | None,
                    tag: str, approve_root: bool) -> None:
        aside = self.cfg.plugins_dir / f".old-{tag}"
        name = info.name
        if existing is not None:
            # unload, not uninstall: an update must not run _uninstall (compat/store.md §2.5)
            await self.manager.unload(name)
            try:
                os.rename(target, aside)
            except OSError as e:
                await self._restart(target)
                raise InstallError(f"can't move the old version aside: {e}") from None
        placed = False
        try:
            os.rename(staged, target)
            placed = True
            if approve_root:
                self.manager.set_root_approved(name, True)
            await self.manager.load_dir(target)
        except Exception as e:
            log.error("%s: swapping in the new version failed (%s), rolling back", name, e)
            if approve_root:
                self.manager.set_root_approved(name, False)
            if name in self.manager.plugins:
                await self.manager.unload(name)
            if placed:
                await asyncio.to_thread(_rmtree, target)
            if existing is not None:
                os.rename(aside, target)
                await self._restart(target)
            message = e.message if isinstance(e, RouteError) else str(e)
            raise InstallError(f"installing failed, the old version is back: {message}"
                               if existing is not None else f"installing failed: {message}") from None
        if existing is not None:
            await asyncio.to_thread(shutil.rmtree, aside, True)

    async def _restart(self, path: Path) -> None:
        try:
            await self.manager.load_dir(path)
        except Exception:
            log.exception("restarting %s failed", path)

    def _record_source(self, name: str, source: str | None) -> None:
        sources = dict(self.settings.get(INSTALL_SOURCE) or {})
        if source is None:
            if name not in sources:
                return
            sources.pop(name)
        else:
            sources[name] = source
        self.settings.set(INSTALL_SOURCE, sources)

    # -- uninstall ---------------------------------------------------------------------------

    async def uninstall(self, name: str) -> None:
        async with self._lock:
            entry = self.manager.plugins.get(name)
            if entry is None:
                raise RouteError("KeyError", f"no plugin named {name!r}")
            try:
                self._check_managed(entry, "uninstalled")
            except InstallError as e:
                raise RouteError("PermissionError", str(e)) from None
            path = entry.info.path
            with self.manager.watch_paused():
                await self.manager.unload(name, uninstall=True)
                await asyncio.to_thread(_rmtree, path)
            # ~/homebrew/{settings,data,logs}/<folder> stay, as in Decky (compat/store.md §2.4)
            for key in SETTING_LISTS + (ROOT_APPROVED,):
                self.settings.list_remove(key, name)
            self._record_source(name, None)
            log.info("uninstalled %s", name)
