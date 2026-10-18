"""Host lifecycle edge cases: huge output lines, children holding the host's pipes, malformed
host messages, start/stop races, double enables, rename conflicts, the watcher without inotify
watches, and legacy calls that must always resolve."""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import signal
import socket
import time
from pathlib import Path

import pytest

from remora.config import Config
from remora.ipc import Channel
from remora.plugins.manager import PluginManager
from remora.plugins.manifest import read_plugin
from remora.plugins.process import PluginProcess, State
from remora.wsrouter import RouteError

from conftest import wait_for

SIMPLE = """
import os, sys

class Plugin:
    async def add(self, a, b):
        return a + b

    async def pid(self):
        return os.getpid()

    async def shout(self, n):
        sys.stdout.write("x" * n)  # no newline
        sys.stdout.write("\\nshort line\\n")
        sys.stdout.flush()
        return n

    async def spawn_holder(self, pidfile):
        # setsid: out of the host's process group; os.system: fork+exec without closing fds
        os.system(f"setsid sleep 30 >&1 & echo $! > {pidfile}")
        return True

    async def die(self):
        os._exit(4)
"""


def host_pids(main_py: Path) -> list[int]:
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            cmdline = Path(f"/proc/{entry}/cmdline").read_bytes()
        except OSError:
            continue
        if b"remora.host" in cmdline and str(main_py).encode() in cmdline:
            pids.append(int(entry))
    return pids


def kill_pidfile(path: Path) -> None:
    try:
        os.kill(int(path.read_text().strip()), signal.SIGKILL)
    except (OSError, ValueError):
        pass


async def test_huge_output_line_does_not_kill_the_relay(make_plugin, started, caplog) -> None:
    make_plugin("loud", "Loud", SIMPLE)
    manager = await started()
    caplog.set_level(logging.INFO, logger="plugin.loud")
    for _ in range(3):  # far more than a pipe holds: a dead relay would block the plugin
        assert await asyncio.wait_for(manager.call("Loud", "shout", 300_000), 10) == 300_000
    await wait_for(lambda: sum("short line" in r.getMessage() for r in caplog.records) == 3,
                   what="the lines after the huge ones")
    assert any("more bytes" in r.getMessage() for r in caplog.records)
    assert all(len(r.getMessage()) < 20_000 for r in caplog.records)


async def test_children_holding_the_pipes_dont_stall_stop(make_plugin, started, cfg: Config,
                                                          tmp_path, caplog) -> None:
    cfg.stop_timeout = 3.0
    make_plugin("holder", "Holder", SIMPLE)
    manager = await started()
    pidfile = tmp_path / "holder.pid"
    try:
        assert await manager.call("Holder", "spawn_holder", str(pidfile))
        await wait_for(lambda: pidfile.exists() and pidfile.read_text().strip(), what="the child")
        proc = manager.plugins["Holder"].proc
        assert proc is not None
        t0 = time.monotonic()
        await manager.disable("Holder")
        assert time.monotonic() - t0 < 2.5
        assert proc.state == State.STOPPED
        assert not any("did not stop" in r.getMessage() for r in caplog.records)
    finally:
        kill_pidfile(pidfile)


async def test_crash_with_children_holding_the_pipes_is_seen(make_plugin, started, tmp_path,
                                                             fast_restarts) -> None:
    make_plugin("holder", "Holder", SIMPLE)
    manager = await started()
    pidfile = tmp_path / "holder.pid"
    try:
        await manager.call("Holder", "spawn_holder", str(pidfile))
        await wait_for(lambda: pidfile.exists() and pidfile.read_text().strip(), what="the child")
        first = await manager.call("Holder", "pid")
        t0 = time.monotonic()
        with pytest.raises(RouteError) as e:
            await asyncio.wait_for(manager.call("Holder", "die"), 5)
        assert "exited with code 4" in e.value.message
        # the IPC socket isn't inherited, so its EOF comes at once (no 1 s grace for a holder)
        assert time.monotonic() - t0 < 0.9
        # and it comes back, despite the child still holding the old host's stdout
        assert await asyncio.wait_for(manager.call("Holder", "pid"), 5) != first
    finally:
        kill_pidfile(pidfile)


async def test_malformed_host_messages_are_skipped(make_plugin, cfg: Config) -> None:
    info = read_plugin(make_plugin("x", "X", SIMPLE))

    async def nothing(*args) -> None:
        pass

    proc = PluginProcess(cfg, info, nothing, nothing)
    a, b = socket.socketpair()
    channel = await Channel.from_socket(a)
    fut = asyncio.get_running_loop().create_future()
    proc._pending[1] = fut
    b.sendall(b'{"t": "result", "id": [1], "ok": true}\n'
              b'{"t": "event", "event": "e", "args": 5}\n'
              b'{"t": "result", "id": {"a": 1}}\n'
              b'{"t": "result", "id": 1, "ok": true, "value": 42}\n')
    b.close()
    await asyncio.wait_for(proc._read(channel), 5)  # returns at EOF, never raises
    assert fut.result() == 42
    channel.close()


async def test_stop_while_starting_leaves_no_host(make_plugin, cfg: Config) -> None:
    info = read_plugin(make_plugin("x", "X", SIMPLE))
    (cfg.homebrew / "data" / "x").mkdir(parents=True)

    async def nothing(*args) -> None:
        pass

    proc = PluginProcess(cfg, info, nothing, nothing)
    starting = asyncio.ensure_future(proc.start())
    await asyncio.sleep(0)  # start is spawning the host
    await proc.stop()
    await starting
    assert proc.state == State.STOPPED
    assert proc.proc is None or proc.proc.returncode is not None
    await wait_for(lambda: not host_pids(info.main_py), timeout=3, what="no host left")


async def test_double_enable_starts_one_host(make_plugin, started) -> None:
    path = make_plugin("modern", "Modern", SIMPLE)
    manager = await started()
    pid = await manager.call("Modern", "pid")
    await asyncio.gather(manager.enable("Modern"), manager.enable("Modern"))
    assert await manager.call("Modern", "pid") == pid  # enabling a running plugin is a no-op
    await manager.disable("Modern")
    await asyncio.gather(manager.enable("Modern"), manager.enable("Modern"), manager.reload("Modern"))
    await wait_for(lambda: manager.plugins["Modern"].proc
                   and manager.plugins["Modern"].proc.state == State.RUNNING)
    await manager.call("Modern", "pid")
    await wait_for(lambda: len(host_pids(path / "main.py")) == 1, timeout=3, what="one host")


async def test_reload_renamed_onto_another_plugin(make_plugin, started) -> None:
    make_plugin("a", "A", SIMPLE)
    b = make_plugin("b", "B", SIMPLE)
    manager = await started()
    a_pid = await manager.call("A", "pid")
    make_plugin("b", "A", SIMPLE)  # b's plugin.json now says "A"
    with pytest.raises(RouteError) as e:
        await manager.reload("B")
    assert e.value.name == "NameConflict"
    assert manager.plugins["A"].info.path != b
    assert await manager.call("A", "pid") == a_pid
    assert "already uses" in (manager.status("B")["error"] or "")


async def test_watcher_out_of_inotify_watches_is_not_fatal(make_plugin, manager: PluginManager,
                                                           cfg: Config, monkeypatch) -> None:
    from watchdog.observers import Observer

    def no_watches(self, *args, **kwargs):
        raise OSError(errno.ENOSPC, "inotify watch limit reached")

    monkeypatch.setattr(Observer, "schedule", no_watches)
    cfg.live_reload = True
    make_plugin("dbg", "Dbg", SIMPLE, flags=["debug"])
    await manager.start()  # doesn't raise
    assert manager.watch_error is not None and manager.watch_error.errno == errno.ENOSPC
    await wait_for(lambda: manager.plugins["Dbg"].proc.state == State.RUNNING)  # type: ignore[union-attr]
    assert await manager.call("Dbg", "add", 1, 2) == 3


async def test_no_watcher_without_debug_plugins(make_plugin, manager: PluginManager,
                                                cfg: Config) -> None:
    cfg.live_reload = True
    make_plugin("plain", "Plain", SIMPLE)
    await manager.start()
    assert manager._observer is None


async def test_legacy_calls_never_reject(make_plugin, started, fast_restarts) -> None:
    make_plugin("legacy", "Legacy", """
        import os
        class Plugin:
            async def ok(self, x=1):
                return x
            async def die(self):
                os._exit(2)
    """, api_version=0)
    manager = await started()
    assert await manager.call_legacy("Legacy", "ok", {"x": 5}) == {"success": True, "result": 5}
    for args in (("Legacy", "ok", [1, 2]), ("Legacy", "ok", "nope"), ("Nobody", "ok", {}),
                 ("Legacy", "_private", {}), ("Legacy", ["unhashable"], {}), ("Legacy", "die", {})):
        result = await asyncio.wait_for(manager.call_legacy(*args), 5)
        assert result["success"] is False and isinstance(result["result"], str), args
    await manager.disable("Legacy")
    assert (await manager.call_legacy("Legacy", "ok", {}))["success"] is False
