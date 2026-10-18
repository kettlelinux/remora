"""Shared fixtures: a Config rooted in tmp_path, plugin folders written on the fly, a WSRouter that
records what the loader sends the frontend, and a PluginManager that runs real host processes."""

from __future__ import annotations

import asyncio
import json
import os
import pwd
import socket
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from remora.config import Config  # noqa: E402
from remora.plugins import process as process_mod  # noqa: E402
from remora.plugins.manager import PluginManager  # noqa: E402
from remora.settings import Settings  # noqa: E402
from remora.wsrouter import WSRouter  # noqa: E402

COMPAT_DIR = BACKEND / "remora" / "compat"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RecordingWS(WSRouter):
    """A WSRouter that also keeps every event it emits, frontend connected or not."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, list[Any]]] = []

    async def emit(self, event: str, *args: Any) -> None:
        self.events.append((event, list(args)))
        await super().emit(event, *args)

    def named(self, event: str) -> list[list[Any]]:
        return [args for name, args in self.events if name == event]


async def wait_for(predicate: Callable[[], Any], timeout: float = 10.0, interval: float = 0.02,
                   what: str = "condition") -> Any:
    """Poll predicate until it returns something truthy."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout} s waiting for {what}")
        await asyncio.sleep(interval)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def cfg(home: Path) -> Config:
    entry = pwd.getpwuid(os.getuid())
    homebrew = home / "homebrew"
    return Config(
        user=entry.pw_name,
        uid=entry.pw_uid,
        gid=entry.pw_gid,
        user_home=home,
        homebrew=homebrew,
        privileged=homebrew,
        port=free_port(),
        live_reload=False,
        inject=False,
        stop_timeout=3.0,
        # never the workstation's own volume keys; tests that confirm pick a mode
        confirm="terminal",
        terminal_timeout=5.0,
        input_sysfs=home / "no-sysfs",
        input_dev=home / "no-dev",
    )


@pytest.fixture
def make_plugin(cfg: Config) -> Callable[..., Path]:
    """Write a plugin folder into cfg.plugins_dir (or `into`) and return its path."""

    def make(dir_name: str, name: str | None = None, main: str | None = None, *,
             api_version: int | None = 1, flags: list[str] | None = None, version: str | None = "1.0.0",
             esm: bool = True, files: dict[str, str] | None = None, into: Path | None = None,
             plugin_json: dict[str, Any] | None = None) -> Path:
        path = (into or cfg.plugins_dir) / dir_name
        path.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {"name": name or dir_name, "author": "tester", "flags": flags or []}
        if api_version is not None:
            data["api_version"] = api_version
        if plugin_json is not None:
            data = plugin_json
        (path / "plugin.json").write_text(json.dumps(data))
        if version is not None:
            package: dict[str, Any] = {"name": dir_name, "version": version}
            if esm:
                package["type"] = "module"
            (path / "package.json").write_text(json.dumps(package))
        if main is not None:
            (path / "main.py").write_text(textwrap.dedent(main))
        for rel, content in (files or {}).items():
            target = path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return path

    return make


@pytest.fixture
def fast_restarts(monkeypatch: pytest.MonkeyPatch) -> tuple[float, ...]:
    """Crash restarts after 0.1 s and 0.2 s, and giving up on the third crash."""
    delays = (0.1, 0.2)
    monkeypatch.setattr(process_mod, "RESTART_DELAYS", delays)
    return delays


@pytest.fixture
def ws() -> RecordingWS:
    return RecordingWS()


@pytest.fixture
def settings(cfg: Config) -> Settings:
    return Settings(cfg.settings_file)


@pytest.fixture
async def manager(cfg: Config, settings: Settings, ws: RecordingWS):
    """A PluginManager; start it with `await manager.start()` once the plugins are written."""
    mgr = PluginManager(cfg, settings, ws)
    yield mgr
    await mgr.shutdown()
    procs = [e.proc for e in mgr.plugins.values() if e.proc is not None]
    for proc in procs:
        if proc.proc is not None and proc.proc.returncode is None:
            proc._kill_group(proc.proc)


StartPlugins = Callable[[], Awaitable[PluginManager]]


@pytest.fixture
def started(manager: PluginManager) -> StartPlugins:
    """Start the manager and wait until every host that should run is running."""

    async def start() -> PluginManager:
        await manager.start()
        await wait_for(lambda: all(e.proc is None or e.proc.state.value in ("running", "crashed")
                                   for e in manager.plugins.values()), what="hosts to start")
        return manager

    return start
