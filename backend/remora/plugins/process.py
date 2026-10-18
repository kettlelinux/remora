"""One running plugin, seen from the supervisor: its host process, calls in flight, crash restarts.

Differences from Decky (compat/backend.md §4, R1-R4, W4, W7):
- a fresh interpreter (subprocess), not a fork: nothing of the loader (token, sockets, memory)
  is inherited; the host starts as the plugin's user with that user's own groups
- a socketpair, not a socket path in /tmp
- a host that dies fails its pending calls and is restarted with backoff
- stop kills the plugin's whole process group once its _unload is done or the budget ran out
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..config import Config
from ..ipc import Channel, encode
from .manifest import PluginInfo

log = logging.getLogger("remora.plugins")

COMPAT_DIR = str(Path(__file__).resolve().parent.parent / "compat")
# the folder that holds the remora package, for the host's PYTHONPATH
PACKAGE_PARENT = str(Path(__file__).resolve().parent.parent.parent)

RESTART_DELAYS = (1, 2, 5, 10, 30)
CRASH_WINDOW = 600.0  # this many crashes within ten minutes and the plugin stays down


class State(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPED = "stopped"
    CRASHED = "crashed"


class PluginCallError(Exception):
    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(error.get("message", ""))
        self.error = error


class PluginStopped(Exception):
    """The call can't complete because the host isn't there. `deliberate`: it was stopped on
    purpose (disable, reload, uninstall), so the frontend gets DISCARD; otherwise it crashed or
    failed to start, and the caller gets an error with the plugin's error text."""

    def __init__(self, message: str, deliberate: bool = False) -> None:
        super().__init__(message)
        self.deliberate = deliberate


EventHandler = Callable[[str, list[Any]], Awaitable[None]]
StateHandler = Callable[["PluginProcess"], Awaitable[None]]


def plugin_env(cfg: Config, info: PluginInfo) -> dict[str, str]:
    home = "/root" if info.root else str(cfg.user_home)
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "HOME": home,
        "USER": "root" if info.root else cfg.user,
        "LOGNAME": "root" if info.root else cfg.user,
        "PYTHONPATH": PACKAGE_PARENT,
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "UNPRIVILEGED_USER": cfg.user,
        "UNPRIVILEGED_PATH": str(cfg.homebrew),
        "PRIVILEGED_PATH": str(cfg.privileged),
        "DECKY_VERSION": cfg.decky_version,
        "DECKY_USER": cfg.user,
        "DECKY_USER_HOME": str(cfg.user_home),
        "DECKY_HOME": str(cfg.homebrew),
        "DECKY_PLUGIN_SETTINGS_DIR": str(cfg.homebrew / "settings" / info.dir_name),
        "DECKY_PLUGIN_RUNTIME_DIR": str(cfg.homebrew / "data" / info.dir_name),
        "DECKY_PLUGIN_LOG_DIR": str(cfg.homebrew / "logs" / info.dir_name),
        "DECKY_PLUGIN_DIR": str(info.path),
        "DECKY_PLUGIN_NAME": info.name,
        "DECKY_PLUGIN_AUTHOR": info.author,
        "REMORA_VERSION": cfg.version,
        "REMORA_CEF_URL": cfg.cef_url,
    }
    if info.version is not None:
        env["DECKY_PLUGIN_VERSION"] = info.version
    for key in ("LC_ALL", "TZ"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


SEND_TIMEOUT = 2.0  # a host whose loop is blocked stops reading; don't wait on it forever
LOG_LINE_LIMIT = 8192  # longer plugin output lines are cut in the log


class PluginProcess:
    def __init__(self, cfg: Config, info: PluginInfo, on_event: EventHandler,
                 on_state: StateHandler) -> None:
        self.cfg = cfg
        self.info = info
        self.on_event = on_event
        self.on_state = on_state
        self.state = State.STOPPED
        self.error: str | None = None
        self.restarts = 0
        self.proc: asyncio.subprocess.Process | None = None
        self.channel: Channel | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._ready: asyncio.Future[None] | None = None
        self._stopped_msg: asyncio.Event = asyncio.Event()
        self._stopping = False
        self._crash_times: list[float] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._restart_task: asyncio.Task[None] | None = None
        self._restart_sleeping = False
        self._restart_delay = 0.0
        self._supervisor: asyncio.Task[None] | None = None
        self._fatal: str | None = None
        self._lock = asyncio.Lock()  # start and stop, one at a time

    @property
    def name(self) -> str:
        return self.info.name

    async def _set_state(self, state: State, error: str | None = None) -> None:
        self.state = state
        self.error = error
        try:
            await self.on_state(self)
        except Exception:
            log.exception("state handler failed")

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- start -------------------------------------------------------------------------------

    async def start(self) -> None:
        async with self._lock:
            await self._start()

    async def _start(self) -> None:
        if self.state in (State.STARTING, State.RUNNING):
            return
        self._stopping = False
        self._fatal = None
        self._stopped_msg = asyncio.Event()
        # before the first await: a call that comes in while the host starts waits for it
        self._ready = asyncio.get_running_loop().create_future()
        await self._set_state(State.STARTING)
        parent_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        spec = {
            "name": self.info.name,
            "dir_name": self.info.dir_name,
            "plugin_dir": str(self.info.path),
            "main_py": str(self.info.main_py),
            "api_version": self.info.api_version,
            "flags": self.info.flags,
            "fd": child_sock.fileno(),
            "compat_dir": COMPAT_DIR,
            "log_level": self.cfg.log_level,
        }
        # -s for root plugins: no user site-packages in a root process (compat/backend.md W6)
        argv = [sys.executable, "-P", *(["-s"] if self.info.root else []),
                "-m", "remora.host", json.dumps(spec)]
        kwargs: dict[str, Any] = {}
        if self.cfg.running_as_root and not self.info.root:
            kwargs.update(user=self.cfg.uid, group=self.cfg.gid,
                          extra_groups=os.getgrouplist(self.cfg.user, self.cfg.gid))
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                pass_fds=[child_sock.fileno()],
                cwd=str(self.info.path),
                env=plugin_env(self.cfg, self.info),
                start_new_session=True,
                **kwargs,
            )
        except OSError as e:
            parent_sock.close()
            self._ready.set_exception(PluginStopped(f"could not start: {e}"))
            self._ready.exception()  # mark retrieved
            await self._set_state(State.CRASHED, f"could not start: {e}")
            return
        finally:
            child_sock.close()  # the host has its own copy
        proc = self.proc
        self.channel = channel = await Channel.from_socket(parent_sock)
        relay = self._spawn(self._relay_output(proc))
        self._supervisor = self._spawn(self._supervise(proc, channel, relay))
        log.info("started %s (pid %d%s)", self.name, proc.pid, ", root" if self.info.root else "")

    async def _relay_output(self, proc: asyncio.subprocess.Process) -> None:
        """The host's stdout and stderr into the loader's log. Reads in chunks and splits lines
        itself: a huge line is cut, never fatal (a dead relay would fill the pipe and block the
        plugin on its next print)."""
        stream = proc.stdout
        assert stream is not None
        plog = logging.getLogger(f"plugin.{self.info.dir_name}")
        buf = bytearray()
        dropped = 0  # bytes of the current line beyond the limit

        def emit(line: bytes, extra: int) -> None:  # noqa: E306
            text = line.decode(errors="replace").rstrip()
            if extra:
                text += f" [... {extra} more bytes]"
            plog.info("%s", text)

        while True:
            try:
                chunk = await stream.read(65536)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never die: keep the pipe drained
                log.debug("%s: output relay: %s", self.name, e)
                await asyncio.sleep(0.1)
                continue
            if not chunk:
                if buf:
                    emit(bytes(buf), dropped)
                return
            buf += chunk
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = bytes(buf[:nl])
                del buf[:nl + 1]
                if dropped:  # the end of a line already cut
                    plog.info("[... %d more bytes]", dropped + len(line))
                    dropped = 0
                else:
                    emit(line[:LOG_LINE_LIMIT], max(0, len(line) - LOG_LINE_LIMIT))
            if dropped:
                dropped += len(buf)
                buf.clear()
            elif len(buf) > LOG_LINE_LIMIT:
                emit(bytes(buf[:LOG_LINE_LIMIT]), 0)
                dropped = len(buf) - LOG_LINE_LIMIT
                buf.clear()

    # -- messages ----------------------------------------------------------------------------

    async def _handle(self, message: dict[str, Any]) -> None:
        kind = message.get("t")
        if kind == "result":
            call_id = message.get("id")
            if not isinstance(call_id, int) or isinstance(call_id, bool):
                raise ValueError(f"result with a bad id {call_id!r}")
            fut = self._pending.pop(call_id, None)
            if fut is not None and not fut.done():
                if message.get("ok"):
                    fut.set_result(message.get("value"))
                else:
                    error = message.get("error")
                    fut.set_exception(PluginCallError(error if isinstance(error, dict) else {}))
        elif kind == "event":
            args = message.get("args")
            if args is None:
                args = []
            if not isinstance(args, list):
                raise ValueError("event args is not a list")
            self._spawn(self.on_event(str(message.get("event")), args))
        elif kind == "ready":
            if self._ready is not None and not self._ready.done():
                self._ready.set_result(None)
            await self._set_state(State.RUNNING)
        elif kind == "fatal":
            err = message.get("error")
            err = err if isinstance(err, dict) else {}
            self._fatal = f"{err.get('name')}: {err.get('message')}"
        elif kind == "stopped":
            self._stopped_msg.set()

    async def _read(self, channel: Channel) -> None:
        while True:
            message = await channel.receive()
            if message is None:
                return
            try:
                await self._handle(message)
            except Exception as e:  # a malformed message must not end exit handling
                log.warning("%s sent a bad message: %s", self.name, e)

    async def _wait_exit(self, proc: asyncio.subprocess.Process) -> None:
        """Until the host process itself exits. Not proc.wait(): on Python 3.11 that also waits
        for stdout to close, which a child the plugin started can keep open for ever."""
        if proc.returncode is not None:
            return
        loop = asyncio.get_running_loop()
        pidfd: int | None = None
        try:
            pidfd = os.pidfd_open(proc.pid)
        except (AttributeError, OSError):
            pass
        if pidfd is not None:
            exited: asyncio.Future[None] = loop.create_future()
            loop.add_reader(pidfd, lambda: exited.done() or exited.set_result(None))
            try:
                while proc.returncode is None and not exited.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(exited), 1.0)
                    except asyncio.TimeoutError:
                        pass
            finally:
                loop.remove_reader(pidfd)
                os.close(pidfd)
        # the child watcher reaps it and sets returncode right after
        while proc.returncode is None:
            await asyncio.sleep(0.01 if pidfd is not None else 0.05)

    async def _supervise(self, proc: asyncio.subprocess.Process, channel: Channel,
                         relay: asyncio.Task[None]) -> None:
        reader = self._spawn(self._read(channel))
        exited = self._spawn(self._wait_exit(proc))
        await asyncio.wait({reader, exited}, return_when=asyncio.FIRST_COMPLETED)
        if not exited.done():
            # the channel closed but the host lives on: give it a moment, then kill it
            try:
                await asyncio.wait_for(asyncio.shield(exited), 5)
            except asyncio.TimeoutError:
                self._kill_group(proc)
                await exited
        else:
            # the host is gone: take what it sent before exiting (a "fatal"), but don't wait on
            # a socket a forked child still holds
            try:
                await asyncio.wait_for(asyncio.shield(reader), 1)
            except asyncio.TimeoutError:
                reader.cancel()
        await self._on_exit(proc, channel, relay)

    async def _on_exit(self, proc: asyncio.subprocess.Process, channel: Channel,
                       relay: asyncio.Task[None]) -> None:
        channel.close()
        self._kill_group(proc)  # leftovers the plugin started
        code = proc.returncode
        # the relay drains what is left, unless a child that escaped the group holds the pipe
        try:
            await asyncio.wait_for(asyncio.shield(relay), 0.5)
        except asyncio.TimeoutError:
            relay.cancel()
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            transport.close()
        if self.channel is channel:
            self.channel = None
        fatal = self._fatal
        deliberate = self._stopping
        failure = PluginStopped(f"{self.name} stopped" if deliberate
                                else f"{self.name} {fatal or f'exited with code {code}'}",
                                deliberate)
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(failure)
        self._pending.clear()
        if self._ready is not None and not self._ready.done():
            self._ready.set_exception(PluginStopped(fatal or f"{self.name} exited ({code})",
                                                    deliberate))
            self._ready.exception()  # mark retrieved
        if self._stopping:
            await self._set_state(State.STOPPED)
            return
        error = fatal or f"exited with code {code}"
        log.error("%s %s", self.name, error)
        now = time.monotonic()
        self._crash_times = [t for t in self._crash_times if now - t < CRASH_WINDOW] + [now]
        if len(self._crash_times) > len(RESTART_DELAYS):
            await self._set_state(State.CRASHED, f"{error}; crashed {len(self._crash_times)} times, "
                                                 "not restarting until it's reloaded")
            return
        delay = RESTART_DELAYS[len(self._crash_times) - 1]
        await self._set_state(State.CRASHED, f"{error}; restarting in {delay:g} s")
        self._restart_delay = delay
        self._restart_sleeping = True
        self._restart_task = self._spawn(self._restart_later(delay))

    async def _restart_later(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        finally:
            self._restart_sleeping = False
        # from here on stop() doesn't cancel this task (that could orphan a host being spawned);
        # it waits for the lock and stops what was started
        async with self._lock:
            if self._stopping or self.state != State.CRASHED:
                return
            self.restarts += 1
            await self._start()

    def _kill_group(self, proc: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def _not_running(self) -> PluginStopped:
        deliberate = self._stopping or self.state == State.STOPPED
        detail = f": {self.error}" if self.error and not deliberate else ""
        return PluginStopped(f"{self.name} is not running ({self.state.value}){detail}", deliberate)

    async def _await_restart(self) -> None:
        """A call to a crashed plugin that is about to restart waits for it, once."""
        task = self._restart_task
        if self.state != State.CRASHED or task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), self._restart_delay + 1)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            if not task.cancelled():
                raise

    async def call(self, method: str, args: list[Any] | None = None,
                   kwargs: dict[str, Any] | None = None, legacy: bool = False) -> Any:
        await self._await_restart()
        if self.state == State.STARTING and self._ready is not None:
            try:
                await asyncio.shield(self._ready)
            except PluginStopped:
                pass
        channel = self.channel
        if channel is None or self.state != State.RUNNING:
            raise self._not_running()
        self._next_id += 1
        call_id = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[call_id] = fut
        try:
            data = encode({"t": "call", "id": call_id, "method": method, "args": args or [],
                           "kwargs": kwargs or {}, "legacy": legacy})
            try:
                await asyncio.wait_for(channel.send_encoded(data), SEND_TIMEOUT)
            except asyncio.TimeoutError:
                raise PluginCallError({"name": "TimeoutError",
                                       "message": f"{self.name} isn't reading its calls (busy?)",
                                       "traceback": None}) from None
            except (ConnectionError, RuntimeError):
                if not fut.done():
                    raise self._not_running() from None
            if self.cfg.call_timeout > 0:
                return await asyncio.wait_for(fut, self.cfg.call_timeout)
            return await fut
        except asyncio.TimeoutError:
            raise PluginCallError({"name": "TimeoutError",
                                   "message": f"{method} took over {self.cfg.call_timeout:g} s",
                                   "traceback": None}) from None
        finally:
            self._pending.pop(call_id, None)

    # -- stop --------------------------------------------------------------------------------

    async def stop(self, uninstall: bool = False) -> None:
        self._stopping = True
        if self._restart_task is not None and self._restart_sleeping:
            self._restart_task.cancel()
        async with self._lock:  # a start in flight finishes first, then is stopped here
            self._stopping = True
            await self._stop(uninstall)
        self._crash_times.clear()

    async def _stop(self, uninstall: bool) -> None:
        proc, channel, supervisor = self.proc, self.channel, self._supervisor
        if proc is None or supervisor is None or supervisor.done():
            if self.state != State.STOPPED:
                await self._set_state(State.STOPPED)
            return
        sent = False
        if channel is not None and proc.returncode is None:
            try:
                await asyncio.wait_for(channel.send({"t": "stop", "uninstall": uninstall}),
                                       SEND_TIMEOUT)
                sent = True
            except (asyncio.TimeoutError, ConnectionError, RuntimeError):
                pass
        if not sent and proc.returncode is None:
            try:  # its loop doesn't read: the host's SIGTERM handler still runs _unload
                os.kill(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(asyncio.shield(supervisor), self.cfg.stop_timeout)
        except asyncio.TimeoutError:
            log.warning("%s did not stop within %g s, killing it", self.name, self.cfg.stop_timeout)
            self._kill_group(proc)
            try:
                await asyncio.wait_for(asyncio.shield(supervisor), 10)
            except asyncio.TimeoutError:
                log.error("%s: exit handling is stuck", self.name)
        if self.state != State.STOPPED:
            await self._set_state(State.STOPPED)

    def status(self) -> dict[str, Any]:
        return {"state": self.state.value, "restarts": self.restarts, "error": self.error,
                "pid": self.proc.pid if self.proc and self.proc.returncode is None else None}
