"""The install pipeline: download, verify, unpack into staging, ask for root, swap atomically,
roll back on failure (compat/store.md §2, §6.2 "client rules"); rollback to the kept previous
version; and uninstall.

Folders in plugins/ that the installer owns (all hidden from the plugin scan by their dot):
  .staging-<tag>        a download being unpacked; removed at startup
  .old-<tag>-<dir>      <dir>'s old version during a swap; put back at startup if <dir> is missing
  .prev-<dir>           <dir>'s previous version, kept until the next update (remora/store/rollback)
  .prev-<dir>.json      {name, version, source} of that previous version
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import secrets
import shutil
import stat
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import unquote, urlparse

import aiohttp

from ..config import Config
from ..plugins import manifest
from ..plugins.manager import Entry, PluginManager
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
CONNECT_TIMEOUT = 30.0
DOWNLOAD_DEADLINE = 15 * 60.0  # the whole download, however fast it is going
MIN_RATE = 10 * 1024  # bytes/s, averaged over RATE_WINDOW; slower than that is a stall
RATE_WINDOW = 30.0
STAGING_PREFIX = ".staging-"
OLD_PREFIX = ".old-"
PREV_PREFIX = ".prev-"


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


def _open_regular(path: Path) -> int:
    """An fd for a regular file; O_NONBLOCK so a FIFO there can't hang us."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as e:
        raise InstallError(f"can't open {path}: {e.strerror}") from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise InstallError(f"{path} is not a regular file")
    return fd


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
        self.connect_timeout = CONNECT_TIMEOUT
        self.download_deadline = DOWNLOAD_DEADLINE
        self.min_rate = MIN_RATE
        self.rate_window = RATE_WINDOW
        self._root_waits: dict[str, asyncio.Future[bool]] = {}
        self._lock = asyncio.Lock()

    @property
    def developer(self) -> bool:
        return bool(self.settings.get("developer.enabled"))

    # -- crash recovery ----------------------------------------------------------------------

    def recover(self) -> None:
        """Before the plugins load: finish what a crash or power cut interrupted. An old version
        whose folder is gone goes back; one whose folder holds the new version becomes .prev;
        staging folders go."""
        plugins = self.cfg.plugins_dir
        try:
            names = os.listdir(plugins)
        except OSError:
            return
        for name in sorted(names):
            path = plugins / name
            try:
                if name.startswith(STAGING_PREFIX):
                    log.info("removing the leftover %s", path)
                    _rmtree(path)
                elif name.startswith(OLD_PREFIX):
                    _, sep, dir_name = name[len(OLD_PREFIX):].partition("-")
                    if not sep or not dir_name or dir_name.startswith(".") or path.is_symlink():
                        log.warning("leaving %s alone: no plugin folder name in it", path)
                        continue
                    target = plugins / dir_name
                    if not os.path.lexists(target):
                        os.rename(path, target)
                        log.warning("an update of %s was interrupted; the old version is back", dir_name)
                    else:
                        self._keep_prev(path, dir_name, None)
                        log.warning("an update of %s was interrupted after the new version was in "
                                    "place; the old one is kept for rollback", dir_name)
            except OSError as e:
                log.error("recovering %s failed: %s", path, e)

    def _prev_paths(self, dir_name: str) -> tuple[Path, Path]:
        plugins = self.cfg.plugins_dir
        return plugins / f"{PREV_PREFIX}{dir_name}", plugins / f"{PREV_PREFIX}{dir_name}.json"

    def _keep_prev(self, aside: Path, dir_name: str, meta: dict[str, Any] | None) -> None:
        prev, side = self._prev_paths(dir_name)
        _rmtree(prev)
        side.unlink(missing_ok=True)
        os.rename(aside, prev)
        if meta is not None:
            tmp = side.with_name(side.name + ".new")
            tmp.write_text(json.dumps(meta), encoding="utf-8")
            os.replace(tmp, side)

    def _drop_prev(self, dir_name: str) -> None:
        prev, side = self._prev_paths(dir_name)
        _rmtree(prev)
        side.unlink(missing_ok=True)

    def _read_prev_meta(self, dir_name: str) -> dict[str, Any]:
        _, side = self._prev_paths(dir_name)
        try:
            meta = json.loads(side.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return meta if isinstance(meta, dict) else {}

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
        """The UI's Allow / Deny. Allow only starts the device's own confirmation
        (manager.confirm_root); it never approves anything by itself."""
        fut = self._root_waits.get(request_id)
        if fut is None or fut.done():
            log.info("root answer for unknown or finished request %s", request_id)
            return
        fut.set_result(bool(allow))

    def _root_ok(self, name: str, source: str | None) -> bool:
        """Approved for root from this very source. A plugin from no known source is asked about
        every time, as is one from another source than the approval's."""
        return source is not None and bool(self.manager.root_approved(name, source))

    async def _confirm_mode(self) -> dict[str, Any]:
        mode: Any = self.manager.confirm_mode()
        if inspect.isawaitable(mode):
            mode = await mode
        return mode if isinstance(mode, dict) else {}

    async def _ask_root(self, info: PluginInfo, source: str | None) -> bool:
        prompt_id = secrets.token_hex(8)
        mode = await self._confirm_mode()
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._root_waits[prompt_id] = fut
        try:
            await self.ws.emit("remora/root_prompt", prompt_id, info.name, info.version,
                               mode.get("mode"), mode.get("hint"))
            allow = await asyncio.wait_for(fut, self.root_prompt_seconds)
        except asyncio.TimeoutError:
            log.info("%s: no answer to the root prompt, cancelling the install", info.name)
            return False
        finally:
            self._root_waits.pop(prompt_id, None)
        if not allow:
            return False
        ok = await self.manager.confirm_root(info.name, version=info.version, source=source,
                                             request_id=prompt_id)
        if not ok:
            log.info("%s: root was not confirmed on the device", info.name)
        return bool(ok)

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
        deadline = time.monotonic() + self.download_deadline
        timeout = aiohttp.ClientTimeout(total=self.download_deadline, connect=self.connect_timeout,
                                        sock_connect=self.connect_timeout)
        got = 0
        try:
            async with self._session().get(url, ssl=self._ssl, timeout=timeout) as res:
                if res.status != 200:
                    raise InstallError(f"download failed: HTTP {res.status} from {url}")
                total = res.content_length
                if total is not None and total > limit:
                    raise InstallError(f"the download is {total} bytes, more than the {limit} allowed")
                shown = 10
                window_start, window_got = time.monotonic(), 0
                with open(dest, "wb") as f:
                    while True:
                        now = time.monotonic()
                        if now >= deadline:
                            raise InstallError(f"the download took more than "
                                               f"{self.download_deadline / 60:g} minutes; cancelled")
                        if now - window_start >= self.rate_window:
                            if window_got < self.min_rate * self.rate_window:
                                raise InstallError(
                                    f"the download stalled (under {self.min_rate / 1024:g} KB/s for "
                                    f"{self.rate_window:g} s); cancelled")
                            window_start, window_got = now, 0
                        wait = min(self.rate_window - (now - window_start), deadline - now)
                        try:
                            chunk = await asyncio.wait_for(res.content.read(CHUNK), max(wait, 0.01))
                        except asyncio.TimeoutError:
                            continue  # the checks above decide
                        if not chunk:
                            break
                        got += len(chunk)
                        window_got += len(chunk)
                        if got > limit:
                            raise InstallError(f"the download is larger than the {limit} bytes allowed")
                        await asyncio.to_thread(f.write, chunk)
                        if total:
                            percent = 10 + int(60 * got / total)
                            if percent >= shown + 5:
                                shown = percent
                                await self._progress(percent, "Remora.install.downloading")
        except aiohttp.ClientError as e:
            raise InstallError(f"download failed: {e or type(e).__name__}") from None
        if size and got != size:
            raise InstallError(f"the download is {got} bytes, the store says {size}")

    @staticmethod
    def _copy_local(src: Path, dest: Path, limit: int) -> None:
        with os.fdopen(_open_regular(src), "rb") as f, open(dest, "wb") as out:
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

    async def install(self, item: InstallItem) -> bool:
        """Install one plugin; failures are reported to the frontend, not raised. Cancelling
        (utilities/cancel_plugin_install) stops it and leaves the old version in place."""
        async with self._lock:
            await self.ws.emit("loader/plugin_download_start", item.name)
            try:
                await self._install(item)
                return True
            except (InstallError, archive.ArchiveError, manifest.ManifestError, RouteError, OSError) as e:
                message = e.message if isinstance(e, RouteError) else str(e)
                log.error("installing %s %s failed: %s", item.name, item.version, message)
                await self.ws.emit("remora/install_failed", item.name, message)
            except asyncio.CancelledError:
                log.info("installing %s %s was cancelled", item.name, item.version)
                await self.ws.emit("remora/install_failed", item.name, "cancelled")
                raise
            except Exception as e:  # keep the loader alive whatever goes wrong
                log.exception("installing %s failed", item.name)
                await self.ws.emit("remora/install_failed", item.name, f"{type(e).__name__}: {e}")
            finally:
                await self.ws.emit("loader/plugin_download_finish", item.name)
            return False

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
        staging = plugins / f"{STAGING_PREFIX}{tag}"
        with self.manager.watch_paused():
            staging.mkdir(mode=0o700)
            try:
                await self._install_staged(item, staging, tag, dev_install)
            finally:
                # shielded: a cancel must not leave the half-unpacked folder behind
                await asyncio.shield(asyncio.to_thread(_rmtree, staging))

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

        approved_now = False
        if info.root and not self._root_ok(info.name, item.source):
            if not await self._ask_root(info, item.source):
                raise InstallError(f"{info.name} asks for root, and that was not confirmed; not installed")
            approved_now = True

        await self._progress(90, "Remora.install.installing")
        prev_meta = None
        if existing is not None:
            prev_meta = {"name": existing.info.name, "version": existing.info.version,
                         "source": (self.settings.get(INSTALL_SOURCE) or {}).get(existing.info.name)}

        def after_swap() -> None:
            if existing is None:
                self.settings.list_add("pluginOrder", info.name)
            self._record_source(info.name, item.source)

        await self._commit(staged, target, info, existing, tag, after_swap,
                           revoke_on_fail=approved_now and existing is None, prev_meta=prev_meta)
        await self._progress(95, "Remora.install.installing")
        if self.after_install is not None:
            try:
                await self.after_install(item, existing is not None)
            except Exception:
                log.debug("after-install hook failed", exc_info=True)
        log.info("installed %s %s into %s", info.name, info.version, target)

    async def _commit(self, staged: Path, target: Path, info: PluginInfo, existing: Entry | None,
                      tag: str, after_swap: Callable[[], None], *, revoke_on_fail: bool = False,
                      prev_meta: dict[str, Any] | None = None, failed_to: Path | None = None) -> None:
        """The swap is not cut in half by a cancel: it finishes (or rolls back) first, and the
        cancel is passed on after."""
        swap = asyncio.ensure_future(self._swap(staged, target, info, existing, tag,
                                                revoke_on_fail, prev_meta, failed_to))
        cancelled = False
        while not swap.done():
            try:
                await asyncio.shield(swap)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                break  # the swap's own failure, read below
        if swap.cancelled():
            raise asyncio.CancelledError()
        error = swap.exception()
        if error is None:
            after_swap()
        if cancelled:
            raise asyncio.CancelledError()
        if error is not None:
            raise error

    async def _swap(self, staged: Path, target: Path, info: PluginInfo, existing: Entry | None,
                    tag: str, revoke_on_fail: bool, prev_meta: dict[str, Any] | None,
                    failed_to: Path | None) -> None:
        aside = self.cfg.plugins_dir / f"{OLD_PREFIX}{tag}-{target.name}"
        name = info.name
        if existing is not None:
            # unload, not uninstall: an update must not run _uninstall (compat/store.md §2.5)
            await self.manager.unload(existing.info.name)
            try:
                os.rename(target, aside)
            except OSError as e:
                await self._restart(target)
                raise InstallError(f"can't move the old version aside: {e}") from None
        placed = False
        try:
            os.rename(staged, target)
            placed = True
            await self.manager.load_dir(target)
        except BaseException as e:  # CancelledError too: the old version must come back
            log.error("%s: swapping in the new version failed (%s), rolling back", name,
                      type(e).__name__ if isinstance(e, asyncio.CancelledError) else e)
            if revoke_on_fail:
                self.manager.set_root_approved(name, False)
            if name in self.manager.plugins:
                await self.manager.unload(name)
            if placed:
                if failed_to is not None:
                    os.rename(target, failed_to)
                else:
                    await asyncio.to_thread(_rmtree, target)
            if existing is not None:
                os.rename(aside, target)
                await self._restart(target)
            if not isinstance(e, Exception):
                raise
            message = e.message if isinstance(e, RouteError) else str(e)
            raise InstallError(f"installing failed, the old version is back: {message}"
                               if existing is not None else f"installing failed: {message}") from None
        if existing is not None:
            try:
                await asyncio.to_thread(self._keep_prev, aside, target.name, prev_meta)
            except OSError as e:
                log.warning("%s: can't keep the previous version (%s); removing it", name, e)
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

    # -- rollback ----------------------------------------------------------------------------

    def previous(self, name: str) -> dict[str, Any] | None:
        """{version, source} of the kept previous version of an installed plugin, if any."""
        entry = self.manager.plugins.get(name)
        if entry is None or not self.managed(entry.info):
            return None
        prev, _ = self._prev_paths(entry.info.path.name)
        if prev.is_symlink() or not prev.is_dir():
            return None
        meta = self._read_prev_meta(entry.info.path.name)
        return {"version": meta.get("version"), "source": meta.get("source")}

    async def rollback(self, name: str) -> dict[str, Any]:
        """Put the kept previous version back; the current one becomes the kept one."""
        async with self._lock:
            entry = self.manager.plugins.get(name)
            if entry is None:
                raise RouteError("KeyError", f"no plugin named {name!r}")
            try:
                self._check_managed(entry, "rolled back")
            except InstallError as e:
                raise RouteError("PermissionError", str(e)) from None
            target = entry.info.path
            prev, side = self._prev_paths(target.name)
            if prev.is_symlink() or not prev.is_dir():
                raise RouteError("FileNotFoundError", f"no previous version of {name} is kept")
            try:
                info = manifest.read_plugin(prev)
            except manifest.ManifestError as e:
                raise RouteError("ValueError", f"the kept version of {name} is broken: {e}") from None
            if info.name != name:
                raise RouteError("ValueError", f"the kept folder holds {info.name!r}, not {name!r}")
            meta = self._read_prev_meta(target.name)
            source = meta.get("source") if isinstance(meta.get("source"), str) else None
            if info.root and not self._root_ok(name, source):
                if not await self._ask_root(info, source):
                    raise RouteError("PermissionError",
                                     f"{name} {info.version} asks for root, and that was not confirmed")
            current = {"name": name, "version": entry.info.version,
                       "source": (self.settings.get(INSTALL_SOURCE) or {}).get(name)}
            tag = secrets.token_hex(6)
            staged = self.cfg.plugins_dir / f"{STAGING_PREFIX}{tag}"
            with self.manager.watch_paused():
                # out of the .prev name first: the current version takes it
                os.rename(prev, staged)
                side.unlink(missing_ok=True)
                try:
                    await self._commit(staged, target, info, entry, tag,
                                       lambda: self._record_source(name, source),
                                       prev_meta=current, failed_to=prev)
                except InstallError as e:
                    if os.path.lexists(staged) and not os.path.lexists(prev):
                        os.rename(staged, prev)
                    if meta:
                        side.write_text(json.dumps(meta), encoding="utf-8")
                    raise RouteError("RuntimeError", str(e)) from None
            log.info("rolled %s back from %s to %s", name, current["version"], info.version)
            return {"name": name, "version": info.version}

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
                await asyncio.to_thread(self._drop_prev, path.name)
            # ~/homebrew/{settings,data,logs}/<folder> stay, as in Decky (compat/store.md §2.4)
            for key in SETTING_LISTS:
                self.settings.list_remove(key, name)
            self.manager.set_root_approved(name, False)
            self._record_source(name, None)
            log.info("uninstalled %s", name)
