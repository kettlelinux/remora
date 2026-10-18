"""Decky's `helpers` module as legacy plugins import it (compat/backend.md §3.1 step 5)."""

from __future__ import annotations

import grp
import os
import pwd
import ssl
import subprocess

import certifi

user_agent = f"Decky/{os.environ.get('DECKY_VERSION', '')} (https://decky.xyz)"

SSHD_UNIT = "sshd.service"
REMOTE_DEBUGGER_UNIT = "steam-web-debug-portforward.service"


def get_ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


def get_csrf_token() -> str:
    # plugin processes don't get the loader's token (Decky's forked children did)
    return ""


def get_loader_version() -> str:
    return os.environ.get("DECKY_VERSION", "")


def get_homebrew_path() -> str:
    return os.environ.get("DECKY_HOME", "")


def get_home_path(username: str | None = None) -> str:
    if username is None:
        return os.environ.get("DECKY_USER_HOME", "")
    return pwd.getpwnam(username).pw_dir


def get_user() -> str:
    return os.environ.get("DECKY_USER", "")


def get_user_id() -> int:
    return pwd.getpwnam(get_user()).pw_uid


def get_user_group() -> str:
    return grp.getgrgid(get_user_group_id()).gr_name


def get_user_group_id() -> int:
    return pwd.getpwnam(get_user()).pw_gid


def get_effective_user() -> str:
    return pwd.getpwuid(os.geteuid()).pw_name


def get_effective_user_id() -> int:
    return os.geteuid()


def get_effective_user_group() -> str:
    return grp.getgrgid(os.getegid()).gr_name


def get_effective_user_group_id() -> int:
    return os.getegid()


def get_user_owner(file_path: str) -> str:
    return pwd.getpwuid(os.stat(file_path).st_uid).pw_name


def get_user_group_owner(file_path: str) -> str:
    return grp.getgrgid(os.stat(file_path).st_gid).gr_name


def mkdir_as_user(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def set_user() -> None:
    pass


def set_user_group() -> str:
    return get_user_group()


def get_system_pythonpaths() -> list[str]:
    return []


def _systemctl(*args: str) -> bool:
    return subprocess.run(["systemctl", *args], capture_output=True).returncode == 0


async def is_systemd_unit_active(unit_name: str) -> bool:
    return _systemctl("is-active", unit_name)


async def stop_systemd_unit(unit_name: str) -> bool:
    return _systemctl("stop", unit_name)


async def start_systemd_unit(unit_name: str) -> bool:
    return _systemctl("start", unit_name)


async def restart_systemd_unit(unit_name: str) -> bool:
    return _systemctl("restart", unit_name)
