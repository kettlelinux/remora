"""Loader configuration, from the same environment variables Decky reads (compat/backend.md §1)."""

from __future__ import annotations

import logging
import os
import pwd
from dataclasses import dataclass, field
from pathlib import Path

from . import DECKY_COMPAT_VERSION, __version__


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off", "")


def _host_user() -> pwd.struct_passwd:
    """The Game Mode user: UNPRIVILEGED_USER, else the owner of UNPRIVILEGED_PATH's home, else uid
    1000, else whoever runs Remora (development)."""
    name = os.environ.get("UNPRIVILEGED_USER")
    if name:
        return pwd.getpwnam(name)
    hb = os.environ.get("UNPRIVILEGED_PATH")
    if hb:
        best = None
        for entry in pwd.getpwall():
            home = entry.pw_dir.rstrip("/")
            if home and home != "/" and (hb == home or hb.startswith(home + "/")):
                if best is None or len(home) > len(best.pw_dir):
                    best = entry
        if best is not None:
            return best
    if os.geteuid() == 0:
        try:
            return pwd.getpwuid(1000)
        except KeyError:
            pass
    return pwd.getpwuid(os.getuid())


@dataclass
class Config:
    user: str
    uid: int
    gid: int
    user_home: Path
    homebrew: Path  # UNPRIVILEGED_PATH: per-plugin settings, data and logs
    privileged: Path  # PRIVILEGED_PATH: plugins/ and settings/loader.json
    host: str = "127.0.0.1"
    port: int = 1337
    cef_url: str = "http://127.0.0.1:8080"
    log_level: int = logging.INFO
    live_reload: bool = True
    inject: bool = True
    # seconds a plugin method call may take; 0 = no limit (Decky has none, and some plugins'
    # calls run for minutes)
    call_timeout: float = 0.0
    stop_timeout: float = 5.0
    version: str = __version__
    decky_version: str = DECKY_COMPAT_VERSION
    extra_plugin_dirs: list[Path] = field(default_factory=list)

    @property
    def plugins_dir(self) -> Path:
        return self.privileged / "plugins"

    @property
    def settings_file(self) -> Path:
        return self.privileged / "settings" / "loader.json"

    @property
    def running_as_root(self) -> bool:
        return os.geteuid() == 0

    @classmethod
    def from_env(cls) -> "Config":
        entry = _host_user()
        home = Path(entry.pw_dir)
        homebrew = Path(os.environ.get("UNPRIVILEGED_PATH") or home / "homebrew")
        privileged = Path(os.environ.get("PRIVILEGED_PATH") or homebrew)
        level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
        return cls(
            user=entry.pw_name,
            uid=entry.pw_uid,
            gid=entry.pw_gid,
            user_home=home,
            homebrew=homebrew,
            privileged=privileged,
            host=os.environ.get("SERVER_HOST", "127.0.0.1"),
            port=int(os.environ.get("SERVER_PORT", "1337")),
            cef_url=os.environ.get("REMORA_CEF_URL", "http://127.0.0.1:8080"),
            log_level=getattr(logging, level_name, logging.INFO),
            live_reload=_flag("LIVE_RELOAD", True),
            inject=_flag("REMORA_INJECT", True),
            call_timeout=float(os.environ.get("REMORA_CALL_TIMEOUT", "0")),
            decky_version=os.environ.get("REMORA_DECKY_VERSION", DECKY_COMPAT_VERSION),
        )
