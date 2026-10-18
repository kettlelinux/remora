"""Root approval: the confirmers (command, keys, terminal), the control socket and its CLI,
approvals stored per source, and the permissions routes."""

from __future__ import annotations

import asyncio
import os
import struct
import time
from pathlib import Path

import pytest

from remora import cli
from remora.config import Config
from remora.confirm import (KEY_VOLUMEDOWN, KEY_VOLUMEUP, KEYS_HINT, ConfirmService,
                            ControlServer, control_request, find_key_inputs, wait_for_keys)
from remora.plugins.manager import ROOT_APPROVED, ROOT_MIGRATED, PluginManager
from remora.plugins.process import State

from conftest import wait_for

ROOTY = """
class Plugin:
    async def add(self, a, b):
        return a + b
"""


def confirmer(tmp_path: Path, code: int) -> str:
    script = tmp_path / f"confirm-{code}"
    script.write_text(f"#!/bin/sh\necho \"asked: $1\" > {tmp_path}/asked\nexit {code}\n")
    script.chmod(0o755)
    return str(script)


@pytest.fixture
def sock(tmp_path: Path) -> Path:
    return tmp_path / "run" / "c.sock"


# -- modes ----------------------------------------------------------------------------------

def test_auto_mode(cfg: Config, tmp_path: Path) -> None:
    cfg.confirm = "auto"
    cfg.input_sysfs = tmp_path / "nothing"
    service = ConfirmService(cfg)
    assert service.mode() == "terminal"
    assert service.info("Foo") == {"mode": "terminal",
                                   "hint": "Run `sudo remora approve Foo` in a terminal on this device"}
    cfg.confirm_command = confirmer(tmp_path, 0)
    assert service.info() == {"mode": "command", "hint": KEYS_HINT}
    cfg.confirm_command = str(tmp_path / "missing")
    assert service.mode() == "terminal"


async def test_command_confirms(cfg: Config, settings, ws, make_plugin, tmp_path: Path) -> None:
    cfg.confirm = "command"
    cfg.confirm_command = confirmer(tmp_path, 0)
    manager = PluginManager(cfg, settings, ws)
    assert await manager.confirm_root("Rooty", version="1.2", source="decky", request_id="r1")
    assert "Rooty 1.2" in (tmp_path / "asked").read_text()
    assert manager.root_approved("Rooty", "decky")
    assert not manager.root_approved("Rooty")  # no recorded install source: not that approval
    assert not manager.root_approved("Rooty", "other")
    waiting = ws.named("remora/confirm_waiting")
    assert waiting[0][:4] == ["r1", "Rooty", "command", KEYS_HINT]
    assert waiting[0][4] > time.time()
    assert ws.named("remora/confirm_done") == [["r1", "Rooty", True]]
    assert settings.get(ROOT_APPROVED) == [{"name": "Rooty", "source": "decky"}]


async def test_command_refuses(cfg: Config, settings, ws, tmp_path: Path) -> None:
    cfg.confirm = "command"
    cfg.confirm_command = confirmer(tmp_path, 1)
    manager = PluginManager(cfg, settings, ws)
    assert not await manager.confirm_root("Rooty")
    assert not manager.root_approved("Rooty")
    assert ws.named("remora/confirm_done") == [[None, "Rooty", False]]


async def test_command_mode_without_a_command_fails_closed(cfg: Config) -> None:
    cfg.confirm = "command"
    cfg.confirm_command = ""
    cfg.confirm_timeout = 0.2
    assert not await ConfirmService(cfg).confirm("X")


def test_old_string_approvals_and_sources(cfg: Config, settings, ws) -> None:
    settings.update({ROOT_APPROVED: ["Old", "Store"], "remora.installSource": {"Store": "decky"}})
    manager = PluginManager(cfg, settings, ws)
    assert manager.root_approved("Old")
    assert manager.root_approved("Store") and manager.root_approved("Store", "decky")
    assert not manager.root_approved("Store", "elsewhere")
    manager.set_root_approved("Store", False)  # every source of that name
    assert not manager.root_approved("Store", "decky")


async def test_migration_rewrites_the_old_form(make_plugin, manager: PluginManager, settings) -> None:
    settings.update({ROOT_APPROVED: ["Rooty"], ROOT_MIGRATED: True})
    make_plugin("rooty", "Rooty", ROOTY, flags=["root"])
    await manager.start()
    assert settings.get(ROOT_APPROVED) == [{"name": "Rooty", "source": None}]
    await wait_for(lambda: manager.plugins["Rooty"].proc
                   and manager.plugins["Rooty"].proc.state == State.RUNNING)


async def test_one_confirmation_at_a_time(cfg: Config, tmp_path: Path) -> None:
    cfg.confirm = "command"
    script = tmp_path / "slow"
    script.write_text(f"#!/bin/sh\necho start >> {tmp_path}/log\nsleep 0.3\necho end >> {tmp_path}/log\n")
    script.chmod(0o755)
    cfg.confirm_command = str(script)
    service = ConfirmService(cfg)
    assert await asyncio.gather(service.confirm("A"), service.confirm("B")) == [True, True]
    assert (tmp_path / "log").read_text().split() == ["start", "end", "start", "end"]


# -- terminal mode and the control socket ---------------------------------------------------

async def test_terminal_approve_and_deny(cfg: Config, settings, ws, make_plugin, sock: Path) -> None:
    cfg.confirm = "terminal"
    manager = PluginManager(cfg, settings, ws)
    control = ControlServer(sock, manager.confirm, manager)
    await control.start()
    try:
        assert sock.exists() and oct(sock.stat().st_mode & 0o777) == "0o600"
        assert oct(sock.parent.stat().st_mode & 0o777) == "0o700"
        task = asyncio.ensure_future(manager.confirm_root("Rooty", version="2.0", request_id="abc"))
        await wait_for(lambda: manager.confirm.pending, what="the request")
        answer = await asyncio.to_thread(control_request, sock, {"cmd": "pending"})
        assert answer["ok"] and answer["pending"][0]["id"] == "abc"
        assert answer["pending"][0]["name"] == "Rooty" and answer["pending"][0]["version"] == "2.0"
        answer = await asyncio.to_thread(control_request, sock, {"cmd": "approve", "target": "Rooty"})
        assert answer == {"ok": True, "name": "Rooty", "request": "abc", "answered": "approved"}
        assert await asyncio.wait_for(task, 5) is True
        assert manager.root_approved("Rooty")
        assert ws.named("remora/confirm_waiting")[0][2] == "terminal"

        task = asyncio.ensure_future(manager.confirm_root("Other"))
        await wait_for(lambda: manager.confirm.pending, what="the second request")
        pid = next(iter(manager.confirm.pending))
        answer = await asyncio.to_thread(control_request, sock, {"cmd": "deny", "target": pid})
        assert answer["ok"] and answer["answered"] == "denied"
        assert await asyncio.wait_for(task, 5) is False

        answer = await asyncio.to_thread(control_request, sock, {"cmd": "list"})
        assert answer == {"ok": True, "approved": [{"name": "Rooty", "source": None,
                                                    "installed": False, "state": None}]}
        answer = await asyncio.to_thread(control_request, sock, {"cmd": "approve", "target": "Nope"})
        assert answer["ok"] is False
        answer = await asyncio.to_thread(control_request, sock, {"cmd": "bogus"})
        assert answer["ok"] is False
    finally:
        await control.close()


async def test_terminal_timeout(cfg: Config) -> None:
    cfg.confirm = "terminal"
    cfg.terminal_timeout = 0.2
    service = ConfirmService(cfg)
    assert await service.confirm("X") is False
    assert service.pending == {}


async def test_cli_over_the_socket(cfg: Config, settings, ws, make_plugin, sock: Path,
                                   monkeypatch, capsys) -> None:
    cfg.confirm = "terminal"
    settings.update({ROOT_MIGRATED: True})
    make_plugin("rooty", "Rooty", ROOTY, flags=["root"])
    manager = PluginManager(cfg, settings, ws)
    await manager.start()
    assert manager.status("Rooty")["state"] == "blocked"
    control = ControlServer(sock, manager.confirm, manager)
    await control.start()

    def run(*argv: str) -> int:
        try:
            cli.main([*argv, "--socket", str(sock)])
        except SystemExit as e:
            return int(e.code or 0)
        return 0

    try:
        monkeypatch.setattr(cli, "_is_root", lambda: False)
        assert await asyncio.to_thread(run, "permissions") == 1
        assert "as root" in capsys.readouterr().err
        monkeypatch.setattr(cli, "_is_root", lambda: True)
        assert await asyncio.to_thread(run, "approve") == 0
        assert "nothing is waiting" in capsys.readouterr().out
        # nothing pending: approving an installed, blocked root plugin starts it
        assert await asyncio.to_thread(run, "approve", "Rooty") == 0
        assert "approved Rooty" in capsys.readouterr().out
        await wait_for(lambda: manager.plugins["Rooty"].proc
                       and manager.plugins["Rooty"].proc.state == State.RUNNING)
        assert await manager.call("Rooty", "add", 1, 2) == 3
        assert await asyncio.to_thread(run, "permissions") == 0
        assert "Rooty  source local  running" in capsys.readouterr().out
        assert await asyncio.to_thread(run, "deny", "Rooty") == 0  # revokes
        assert manager.status("Rooty")["state"] == "blocked"
    finally:
        await control.close()
        await manager.shutdown()


# -- keys -----------------------------------------------------------------------------------

def fake_input(tmp_path: Path, event: str, name: str, bus: str, keys: list[int]) -> Path:
    """sysfs: class/input/<event>/device -> devices/<bus>/dev/input/inputN, whose device is the
    parent with a subsystem link; /dev/input/<event> is a fifo."""
    sysfs = tmp_path / "sys"
    parent = sysfs / "devices" / bus / f"{name}-dev"
    input_dir = parent / "input" / f"input-{event}"
    (input_dir / "capabilities").mkdir(parents=True)
    (input_dir / "name").write_text(name + "\n")
    value = 0
    for k in keys:
        value |= 1 << k
    words, bits = [], struct.calcsize("l") * 8
    while value:
        words.append(f"{value & ((1 << bits) - 1):x}")
        value >>= bits
    (input_dir / "capabilities" / "key").write_text(" ".join(reversed(words or ["0"])) + "\n")
    (sysfs / "bus" / bus).mkdir(parents=True, exist_ok=True)
    if not (parent / "subsystem").exists():
        os.symlink(sysfs / "bus" / bus, parent / "subsystem")
    os.symlink(parent, input_dir / "device")
    (sysfs / "class" / "input" / event).mkdir(parents=True)
    os.symlink(input_dir, sysfs / "class" / "input" / event / "device")
    dev = tmp_path / "dev"
    dev.mkdir(exist_ok=True)
    os.mkfifo(dev / event)
    return dev / event


def ev(code: int, value: int) -> bytes:
    return struct.pack("llHHi", 0, 0, 1, code, value)


async def test_keys(cfg: Config, tmp_path: Path) -> None:
    up = fake_input(tmp_path, "event0", "gpio-keys", "platform", [KEY_VOLUMEUP])
    down = fake_input(tmp_path, "event1", "pm8941_resin", "platform", [KEY_VOLUMEDOWN])
    fake_input(tmp_path, "event2", "usb pad", "usb", [KEY_VOLUMEUP, KEY_VOLUMEDOWN])
    cfg.input_sysfs = tmp_path / "sys" / "class" / "input"
    cfg.input_dev = tmp_path / "dev"
    inputs = find_key_inputs(cfg)
    assert [i.name for i in inputs] == ["gpio-keys", "pm8941_resin"]
    cfg.confirm = "auto"
    assert ConfirmService(cfg).mode() == "keys"
    cfg.confirm_keys = ["gpio-keys"]
    assert [i.name for i in find_key_inputs(cfg)] == ["gpio-keys"]
    cfg.confirm_keys = []

    task = asyncio.ensure_future(wait_for_keys(inputs, hold=0.2))
    await asyncio.sleep(0.05)
    w_up = os.open(up, os.O_WRONLY | os.O_NONBLOCK)
    w_down = os.open(down, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(w_up, ev(KEY_VOLUMEUP, 1))
        os.write(w_down, ev(KEY_VOLUMEDOWN, 1))
        await asyncio.sleep(0.05)
        os.write(w_up, ev(KEY_VOLUMEUP, 0))  # let go too early
        await asyncio.sleep(0.3)
        assert not task.done()
        os.write(w_up, ev(KEY_VOLUMEUP, 1))
        assert await asyncio.wait_for(task, 2) is True
    finally:
        os.close(w_up)
        os.close(w_down)
        task.cancel()


# -- routes ---------------------------------------------------------------------------------

async def test_permissions_routes(make_plugin, manager: PluginManager, cfg: Config, settings,
                                  ws, tmp_path: Path) -> None:
    from remora.cdp import CDP
    from remora.utilities import Utilities

    settings.update({ROOT_MIGRATED: True})
    make_plugin("rooty", "Rooty", ROOTY, flags=["root"])
    await manager.start()
    utilities = Utilities(cfg, settings, manager, CDP(cfg), ws)
    cfg.confirm = "command"
    cfg.confirm_command = confirmer(tmp_path, 1)
    assert await utilities.confirm_mode() == {"mode": "command", "hint": KEYS_HINT}
    assert await utilities.permissions_set("Rooty", True) == {"approved": False,
                                                             "reason": "not confirmed"}
    assert manager.status("Rooty")["state"] == "blocked"
    cfg.confirm_command = confirmer(tmp_path, 0)
    assert await utilities.permissions_set("Rooty", True) == {"approved": True}
    await wait_for(lambda: manager.plugins["Rooty"].proc
                   and manager.plugins["Rooty"].proc.state == State.RUNNING)
    assert (await utilities.permissions_list())[0]["approved"] is True
    assert await utilities.permissions_set("Rooty", False) == {"approved": False}
    assert manager.plugins["Rooty"].proc is None
    assert manager.status("Rooty")["state"] == "blocked"
    await utilities.close()
