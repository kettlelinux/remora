"""Physical confirmation for root plugins, and the root-only control socket.

Any process running as the Game Mode user can drive Steam's UI through CEF devtools (:8080), so
a click in Remora's UI proves nothing. Approving a root plugin needs a channel that process
can't drive: the device's own volume keys, a confirm command (Kettle's confirm-keys), or root on
a terminal (`remora approve`, over the control socket).

Modes (Config.confirm, REMORA_CONFIRM):
- auto: `command` when REMORA_CONFIRM_COMMAND is set and executable, else `keys` when the device
  has platform volume keys, else `terminal`
- command: run `<command> "<what>"` as root; exit 0 confirms
- keys: Volume Up + Volume Down held together for a second, on platform inputs only (not uinput,
  USB, Bluetooth), pressed after the request
- terminal: wait for `remora approve <name>` from root

`remora approve` over the control socket also answers a request in the other modes: root on a
terminal is always trusted.

Control socket (one JSON object per line, one answer per request):
  {"cmd": "pending"}                 -> {"ok": true, "pending": [{id, name, version, source, what, since}]}
  {"cmd": "approve", "target": x}    -> {"ok": true, ...}; x is a request id or a plugin name
  {"cmd": "deny", "target": x}       -> {"ok": true, ...}
  {"cmd": "list"}                    -> {"ok": true, "approved": [{name, source, installed, state}]}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import stat
import struct
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import Config

log = logging.getLogger("remora.confirm")

KEYS_HINT = "Hold Volume Up and Volume Down together for a second"
MODES = ("keys", "command", "terminal")

EV_KEY = 1
KEY_VOLUMEDOWN = 114
KEY_VOLUMEUP = 115
VOLUME_KEYS = (KEY_VOLUMEUP, KEY_VOLUMEDOWN)
HOLD_SECONDS = 1.0
_EVENT = struct.Struct("llHHi")  # struct input_event: timeval, type, code, value


def terminal_hint(name: str | None) -> str:
    return f"Run `sudo remora approve {name or '<name>'}` in a terminal on this device"


def hint_for(mode: str, name: str | None = None) -> str:
    return terminal_hint(name) if mode == "terminal" else KEYS_HINT


# -- keys ---------------------------------------------------------------------------------

@dataclass
class KeyInput:
    path: Path  # /dev/input/eventN
    name: str
    keys: set[int]


def _key_bits(text: str) -> set[int]:
    """capabilities/key: hex words of the kernel's long size, most significant first."""
    word_bits = struct.calcsize("l") * 8
    found: set[int] = set()
    for i, word in enumerate(reversed(text.split())):
        try:
            value = int(word, 16)
        except ValueError:
            return set()
        for key in VOLUME_KEYS:
            if i * word_bits <= key < (i + 1) * word_bits and value >> (key - i * word_bits) & 1:
                found.add(key)
    return found


def _is_platform_input(input_dir: Path) -> bool:
    """The input's parent device sits on the platform bus: built-in keys, not uinput/USB/HID."""
    real = os.path.realpath(input_dir)
    if "/virtual/" in real + "/":
        return False
    parent = input_dir / "device"
    try:
        subsystem = os.path.basename(os.path.realpath(parent / "subsystem"))
    except OSError:
        return False
    return parent.exists() and subsystem == "platform"


def find_key_inputs(cfg: Config) -> list[KeyInput]:
    """The device's own inputs with volume keys."""
    found: list[KeyInput] = []
    try:
        names = sorted(os.listdir(cfg.input_sysfs))
    except OSError:
        return found
    for event in names:
        if not event.startswith("event"):
            continue
        input_dir = cfg.input_sysfs / event / "device"
        try:
            name = (input_dir / "name").read_text().strip()
            keys = _key_bits((input_dir / "capabilities" / "key").read_text())
        except OSError:
            continue
        if not keys or not _is_platform_input(input_dir):
            continue
        if cfg.confirm_keys and name not in cfg.confirm_keys:
            continue
        found.append(KeyInput(cfg.input_dev / event, name, keys))
    return found


def keys_available(cfg: Config) -> bool:
    keys: set[int] = set()
    for item in find_key_inputs(cfg):
        keys |= item.keys
    return all(k in keys for k in VOLUME_KEYS)


async def wait_for_keys(inputs: list[KeyInput], hold: float = HOLD_SECONDS) -> bool:
    """Until Volume Up and Volume Down are both held for `hold` seconds. Only presses seen after
    the call count: a key already down when it starts doesn't."""
    loop = asyncio.get_running_loop()
    done: asyncio.Future[bool] = loop.create_future()
    down = {k: False for k in VOLUME_KEYS}
    timer: list[asyncio.TimerHandle | None] = [None]
    fds: list[int] = []
    buffers: dict[int, bytes] = {}

    def check() -> None:
        if all(down.values()) and not done.done():
            done.set_result(True)

    def key(code: int, value: int) -> None:
        if code not in down or value == 2:  # 2: autorepeat
            return
        down[code] = value == 1
        if all(down.values()):
            if timer[0] is None:
                timer[0] = loop.call_later(hold, check)
        elif timer[0] is not None:
            timer[0].cancel()
            timer[0] = None

    def readable(fd: int) -> None:
        try:
            data = os.read(fd, _EVENT.size * 64)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:  # the input went away
            loop.remove_reader(fd)
            return
        data = buffers.get(fd, b"") + data
        usable = len(data) - len(data) % _EVENT.size
        buffers[fd] = data[usable:]
        for offset in range(0, usable, _EVENT.size):
            _, _, etype, code, value = _EVENT.unpack_from(data, offset)
            if etype == EV_KEY:
                key(code, value)

    try:
        for item in inputs:
            try:
                fd = os.open(item.path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            except OSError as e:
                log.warning("can't read %s (%s): %s", item.path, item.name, e)
                continue
            fds.append(fd)
            loop.add_reader(fd, readable, fd)
        if not fds:
            return False
        return await done
    finally:
        if timer[0] is not None:
            timer[0].cancel()
        for fd in fds:
            loop.remove_reader(fd)
            os.close(fd)


# -- command ------------------------------------------------------------------------------

def command_usable(command: str) -> bool:
    return bool(command) and os.path.isfile(command) and os.access(command, os.X_OK)


async def run_command(command: str, what: str, timeout: float) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            command, what, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        log.error("confirm command %s failed to start: %s", command, e)
        return False
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        log.warning("confirm command %s took over %g s", command, timeout)
        return False
    finally:
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
            await proc.wait()
    text = out.decode(errors="replace").strip()
    if text:
        log.info("confirm command: %s", text[-2000:])
    return proc.returncode == 0


# -- requests -----------------------------------------------------------------------------

@dataclass
class Pending:
    id: str
    name: str
    version: str | None
    source: str | None
    what: str
    since: float
    answer: asyncio.Future[bool] = field(repr=False)

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "version": self.version, "source": self.source,
                "what": self.what, "since": self.since}


Waiting = Callable[[str, str, float], Awaitable[None]]  # (mode, hint, until epoch seconds)


class ConfirmService:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.pending: dict[str, Pending] = {}
        self._lock = asyncio.Lock()

    def mode(self) -> str:
        mode = self.cfg.confirm
        if mode in MODES:
            return mode
        if command_usable(self.cfg.confirm_command):
            return "command"
        if keys_available(self.cfg):
            return "keys"
        return "terminal"

    def info(self, name: str | None = None) -> dict[str, str]:
        mode = self.mode()
        return {"mode": mode, "hint": hint_for(mode, name)}

    def pending_list(self) -> list[dict[str, Any]]:
        return [p.to_json() for p in self.pending.values()]

    def find(self, target: str) -> Pending | None:
        if target in self.pending:
            return self.pending[target]
        return next((p for p in self.pending.values() if p.name == target), None)

    def answer(self, target: str, ok: bool) -> Pending | None:
        """Answer a pending request from root's terminal. Returns it, or None if none matches."""
        pending = self.find(target)
        if pending is not None and not pending.answer.done():
            pending.answer.set_result(ok)
        return pending

    async def confirm(self, name: str, *, version: str | None = None, source: str | None = None,
                      request_id: str | None = None, what: str | None = None,
                      on_waiting: Waiting | None = None) -> bool:
        what = what or f"Allow the plugin {name}{' ' + version if version else ''} to run as root"
        pid = request_id or uuid.uuid4().hex[:8]
        while pid in self.pending:
            pid = uuid.uuid4().hex[:8]
        pending = Pending(pid, name, version, source, what, time.time(),
                          asyncio.get_running_loop().create_future())
        self.pending[pid] = pending
        try:
            async with self._lock:  # one confirmation at a time; the next waits its turn
                if pending.answer.done():  # answered from the terminal while it waited
                    return pending.answer.result()
                mode = self.mode()
                timeout = self.cfg.terminal_timeout if mode == "terminal" else self.cfg.confirm_timeout
                log.info("root approval for %s: waiting (%s, %g s)", name, mode, timeout)
                if on_waiting is not None:
                    try:
                        await on_waiting(mode, hint_for(mode, name), time.time() + timeout)
                    except Exception:
                        log.exception("confirm_waiting failed")
                ok = await self._wait(mode, pending, what, timeout)
                log.info("root approval for %s: %s", name, "confirmed" if ok else "not confirmed")
                return ok
        finally:
            self.pending.pop(pid, None)
            if not pending.answer.done():
                pending.answer.cancel()

    async def _wait(self, mode: str, pending: Pending, what: str, timeout: float) -> bool:
        waiters: list[asyncio.Future[bool]] = [pending.answer]
        local: asyncio.Task[bool] | None = None
        if mode == "command":
            if not command_usable(self.cfg.confirm_command):
                log.error("REMORA_CONFIRM=command, but %r is not an executable",
                          self.cfg.confirm_command)
            else:
                local = asyncio.ensure_future(run_command(self.cfg.confirm_command, what,
                                                          timeout + 15))
        elif mode == "keys":
            inputs = find_key_inputs(self.cfg)
            if not inputs:
                log.error("REMORA_CONFIRM=keys, but no platform input has volume keys")
            else:
                local = asyncio.ensure_future(wait_for_keys(inputs))
        if local is not None:
            waiters.append(local)
        try:
            done, _ = await asyncio.wait(waiters, timeout=timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            for fut in done:
                if not fut.cancelled() and fut.exception() is None:
                    return bool(fut.result())
                if not fut.cancelled():
                    log.error("confirmation failed: %s", fut.exception())
            return False
        finally:
            if local is not None and not local.done():
                local.cancel()
                try:
                    await local
                except (asyncio.CancelledError, Exception):
                    pass


# -- control socket -----------------------------------------------------------------------

def _peer_uid(sock: socket.socket) -> int | None:
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    except OSError:
        return None
    return struct.unpack("3i", creds)[1]


class ControlServer:
    """`remora approve/deny/permissions` talk to this. Only the loader's own uid (root on a real
    install) may connect: the directory is 0700, the socket 0600, and the peer uid is checked."""

    def __init__(self, path: Path, confirm: ConfirmService, manager: Any) -> None:
        self.path = Path(path)
        self.confirm = confirm
        self.manager = manager
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    def _prepare_dir(self) -> None:
        folder = self.path.parent
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = os.lstat(folder)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
            raise PermissionError(f"{folder} is not a folder owned by uid {os.geteuid()}")
        os.chmod(folder, 0o700)
        try:
            if stat.S_ISSOCK(os.lstat(self.path).st_mode):
                os.unlink(self.path)
        except FileNotFoundError:
            pass

    async def start(self) -> None:
        try:
            self._prepare_dir()
            self._server = await asyncio.start_unix_server(self._client, str(self.path))
            os.chmod(self.path, 0o600)
        except OSError as e:
            log.warning("no control socket at %s (`remora approve` won't work): %s", self.path, e)
            self._server = None
            return
        log.info("control socket at %s", self.path)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            try:
                os.unlink(self.path)
            except OSError:
                pass
        for task in list(self._tasks):
            task.cancel()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        sock = writer.get_extra_info("socket")
        try:
            if sock is None or _peer_uid(sock) != os.geteuid():
                writer.write(json.dumps({"ok": False, "error": "permission denied"}).encode() + b"\n")
                await writer.drain()
                return
            while True:
                line = await reader.readline()
                if not line:
                    return
                try:
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise ValueError("not an object")
                    answer = await self.handle(message)
                except Exception as e:
                    answer = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                writer.write(json.dumps(answer, ensure_ascii=False).encode() + b"\n")
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    async def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        cmd = message.get("cmd")
        target = message.get("target")
        if cmd == "pending":
            return {"ok": True, "pending": self.confirm.pending_list()}
        if cmd == "list":
            return {"ok": True, "approved": self.manager.approved_root_list()}
        if cmd not in ("approve", "deny"):
            return {"ok": False, "error": f"unknown command {cmd!r}"}
        if not isinstance(target, str) or not target:
            return {"ok": False, "error": "no target"}
        ok = cmd == "approve"
        pending = self.confirm.answer(target, ok)
        if pending is not None:
            log.info("%s %s from the terminal", "approved" if ok else "denied", pending.name)
            return {"ok": True, "name": pending.name, "request": pending.id,
                    "answered": "approved" if ok else "denied"}
        # nothing pending: approve or revoke an installed root plugin directly
        if ok:
            return await self.manager.approve_from_terminal(target)
        return await self.manager.revoke_from_terminal(target)


def control_request(path: Path | str, message: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    """One request to the control socket (the CLI side)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall(json.dumps(message).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    if not data:
        raise ConnectionError("the loader closed the control socket")
    answer = json.loads(data)
    if not isinstance(answer, dict):
        raise ValueError("bad answer from the loader")
    return answer
