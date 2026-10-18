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

def _setup_logger() -> logging.Logger:
    root = logging.getLogger()
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if DECKY_PLUGIN_LOG_DIR and os.path.isdir(DECKY_PLUGIN_LOG_DIR):
        try:
            logs = sorted(p for p in os.listdir(DECKY_PLUGIN_LOG_DIR) if p.endswith(".log"))
            for old in logs[:-4]:  # Decky keeps the newest four, plus the new one
                try:
                    os.unlink(os.path.join(DECKY_PLUGIN_LOG_DIR, old))
                except OSError:
                    pass
            handlers.insert(0, logging.FileHandler(DECKY_PLUGIN_LOG, encoding="utf-8"))
        except OSError:
            pass
    logging.basicConfig(handlers=handlers, format="[%(asctime)s][%(levelname)s]: %(message)s",
                        force=True)
    root.setLevel(logging.INFO)
    return root


logger: logging.Logger = _setup_logger()
