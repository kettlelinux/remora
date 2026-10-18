"""The `decky` module plugins import (also `decky_plugin`): compat/backend.md §5.

Constants come from the environment the supervisor set for this plugin's host.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

__version__ = "1.0.0"

HOME: str = os.environ.get("HOME", "")
USER: str = os.environ.get("USER", "")
DECKY_VERSION: str = os.environ.get("DECKY_VERSION", "")
DECKY_USER: str = os.environ.get("DECKY_USER", "")
DECKY_USER_HOME: str = os.environ.get("DECKY_USER_HOME", "")
DECKY_HOME: str = os.environ.get("DECKY_HOME", "")
DECKY_PLUGIN_SETTINGS_DIR: str = os.environ.get("DECKY_PLUGIN_SETTINGS_DIR", "")
DECKY_PLUGIN_RUNTIME_DIR: str = os.environ.get("DECKY_PLUGIN_RUNTIME_DIR", "")
DECKY_PLUGIN_LOG_DIR: str = os.environ.get("DECKY_PLUGIN_LOG_DIR", "")
DECKY_PLUGIN_DIR: str = os.environ.get("DECKY_PLUGIN_DIR", "")
DECKY_PLUGIN_NAME: str = os.environ.get("DECKY_PLUGIN_NAME", "")
DECKY_PLUGIN_VERSION: str = os.environ.get("DECKY_PLUGIN_VERSION", "")
DECKY_PLUGIN_AUTHOR: str = os.environ.get("DECKY_PLUGIN_AUTHOR", "")
DECKY_PLUGIN_LOG: str = os.path.join(DECKY_PLUGIN_LOG_DIR, time.strftime("%Y-%m-%d %H.%M.%S") + ".log")


# -- migration helpers ---------------------------------------------------------------------

def migrate_any(target_dir: str, *files_or_directories: str) -> dict[str, str]:
    """Move files or folder contents into target_dir. Returns {old path: new path}; a missing
    source maps to ""."""
    moved: dict[str, str] = {}
    os.makedirs(target_dir, exist_ok=True)
    for source in files_or_directories:
        src = Path(source)
        if not src.exists():
            moved[source] = ""
            continue
        if src.is_dir():
            for child in src.iterdir():
                dest = Path(target_dir) / child.name
                if child.is_dir() and not child.is_symlink():
                    shutil.copytree(child, dest, symlinks=True, dirs_exist_ok=True)
                else:
                    shutil.copy2(child, dest, follow_symlinks=False)
            shutil.rmtree(src)
            moved[source] = target_dir
        else:
            dest = Path(target_dir) / src.name
            shutil.copy2(src, dest, follow_symlinks=False)
            src.unlink()
            moved[source] = str(dest)
    return moved


def migrate_settings(*files_or_directories: str) -> dict[str, str]:
    return migrate_any(DECKY_PLUGIN_SETTINGS_DIR, *files_or_directories)


def migrate_runtime(*files_or_directories: str) -> dict[str, str]:
    return migrate_any(DECKY_PLUGIN_RUNTIME_DIR, *files_or_directories)


def migrate_logs(*files_or_directories: str) -> dict[str, str]:
    return migrate_any(DECKY_PLUGIN_LOG_DIR, *files_or_directories)


# -- events --------------------------------------------------------------------------------

_emit: Callable[..., Awaitable[None]] | None = None


def _remora_set_emit(fn: Callable[..., Awaitable[None]]) -> None:
    global _emit
    _emit = fn


async def emit(event: str, *args: Any) -> None:
    """Send an event to this plugin's frontend listeners (addEventListener(event, ...))."""
    if _emit is None:
        raise RuntimeError("decky.emit is only available inside a plugin")
    await _emit(event, *args)


# -- logging -------------------------------------------------------------------------------

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_log_dir(path: str) -> int:
    """The plugin's log folder as a directory fd, walked one component at a time without
    following links below the user's home: a root plugin's host must not rotate or create log
    files wherever a link the user planted points."""
    home = os.path.realpath(DECKY_USER_HOME) if DECKY_USER_HOME else ""
    path = os.path.abspath(path)
    if home and (path + "/").startswith(home.rstrip("/") + "/"):
        base, rest = home, os.path.relpath(path, home)
    else:
        base, rest = os.path.realpath(os.path.dirname(path)), os.path.basename(path)
    fd = os.open(base, _DIR_FLAGS)
    try:
        for part in [p for p in rest.split("/") if p and p != "."]:
            if part == "..":
                raise OSError(f"refused {path}")
            nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def _log_handler() -> logging.Handler | None:
    if not DECKY_PLUGIN_LOG_DIR:
        return None
    try:
        dir_fd = _open_log_dir(DECKY_PLUGIN_LOG_DIR)
    except OSError:
        return None
    try:
        logs = sorted(p for p in os.listdir(dir_fd) if p.endswith(".log"))
        for old in logs[:-4]:  # Decky keeps the newest four, plus the new one
            try:
                os.unlink(old, dir_fd=dir_fd)
            except OSError:
                pass
        fd = os.open(os.path.basename(DECKY_PLUGIN_LOG),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644,
                     dir_fd=dir_fd)
        try:
            if os.geteuid() == 0:  # a root plugin's log stays the user's to read and delete
                st = os.fstat(dir_fd)
                os.fchown(fd, st.st_uid, st.st_gid)
        except OSError:
            pass
        handler = logging.FileHandler(DECKY_PLUGIN_LOG, encoding="utf-8", delay=True)
        handler.stream = os.fdopen(fd, "a", encoding="utf-8")
        return handler
    except OSError:
        return None
    finally:
        os.close(dir_fd)


def _setup_logger() -> logging.Logger:
    root = logging.getLogger()
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    file_handler = _log_handler()
    if file_handler is not None:
        handlers.insert(0, file_handler)
    logging.basicConfig(handlers=handlers, format="[%(asctime)s][%(levelname)s]: %(message)s",
                        force=True)
    root.setLevel(logging.INFO)
    return root


logger: logging.Logger = _setup_logger()
