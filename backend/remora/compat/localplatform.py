"""Decky's `localplatform` module, the parts plugins reach for."""

from __future__ import annotations

import os
import platform

import helpers

ON_WINDOWS = platform.system() == "Windows"
ON_MAC = platform.system() == "Darwin"
ON_LINUX = not ON_WINDOWS and not ON_MAC
ON_ARM64 = platform.machine() in ("arm64", "aarch64")


def get_privileged_path() -> str:
    return os.environ.get("PRIVILEGED_PATH", helpers.get_homebrew_path())


def get_unprivileged_path() -> str:
    return helpers.get_homebrew_path()


def get_unprivileged_user() -> str:
    return helpers.get_user()


def get_username() -> str:
    return helpers.get_user()


def get_home_path(user: object = None) -> str:
    return os.environ.get("HOME", "")


def chown(path: str, user: object = None, recursive: bool = True) -> bool:
    # Remora never chowns on a plugin's behalf; plugins own their dirs already
    return True


def chmod(path: str, permissions: int, recursive: bool = True) -> bool:
    try:
        os.chmod(path, int(str(permissions), 8))
    except OSError:
        return False
    return True
