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
from ..ipc import Channel
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
    pass


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
        if self.state in (State.STARTING, State.RUNNING):
            return
        self._stopping = False
        self._stopped_msg = asyncio.Event()
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
            await self._set_state(State.CRASHED, f"could not start: {e}")
            return
        finally:
            child_sock.close()  # the host has its own copy
        self.channel = await Channel.from_socket(parent_sock)
        self._ready = asyncio.get_running_loop().create_future()
        self._spawn(self._relay_output(self.proc))
        self._spawn(self._read(self.channel, self.proc))
        log.info("started %s (pid %d%s)", self.name, self.proc.pid, ", root" if self.info.root else "")

    async def _relay_output(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        plog = logging.getLogger(f"plugin.{self.info.dir_name}")
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            plog.info("%s", line.decode(errors="replace").rstrip())

    # -- messages ----------------------------------------------------------------------------

    async def _read(self, channel: Channel, proc: asyncio.subprocess.Process) -> None:
        fatal: str | None = None
        while True:
            message = await channel.receive()
            if message is None:
                break
            kind = message.get("t")
            if kind == "result":
                fut = self._pending.pop(message.get("id"), None)  # type: ignore[arg-type]
                if fut is not None and not fut.done():
                    if message.get("ok"):
                        fut.set_result(message.get("value"))
                    else:
                        fut.set_exception(PluginCallError(message.get("error") or {}))
            elif kind == "event":
                self._spawn(self.on_event(str(message.get("event")), list(message.get("args") or [])))
            elif kind == "ready":
                if self._ready is not None and not self._ready.done():
                    self._ready.set_result(None)
                await self._set_state(State.RUNNING)
            elif kind == "fatal":
                err = message.get("error") or {}
                fatal = f"{err.get('name')}: {err.get('message')}"
            elif kind == "stopped":
                self._stopped_msg.set()
        await self._on_exit(channel, proc, fatal)

    async def _on_exit(self, channel: Channel, proc: asyncio.subprocess.Process,
                       fatal: str | None) -> None:
        channel.close()
        try:
            code = await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            self._kill_group(proc)
            code = await proc.wait()
        self._kill_group(proc)  # leftovers the plugin started
        if self.channel is channel:
            self.channel = None
        failure: Exception = PluginStopped(f"{self.name} stopped")
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(failure)
        self._pending.clear()
        if self._ready is not None and not self._ready.done():
            self._ready.set_exception(PluginStopped(fatal or f"{self.name} exited ({code})"))
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
        await self._set_state(State.CRASHED, f"{error}; restarting in {delay} s")
        self._restart_task = self._spawn(self._restart_later(delay))

    async def _restart_later(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if self._stopping or self.state != State.CRASHED:
            return
        self.restarts += 1
        await self.start()

    def _kill_group(self, proc: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    async def call(self, method: str, args: list[Any] | None = None,
                   kwargs: dict[str, Any] | None = None, legacy: bool = False) -> Any:
        if self.state == State.STARTING and self._ready is not None:
            try:
                await asyncio.shield(self._ready)
            except PluginStopped:
                pass
        channel = self.channel
        if channel is None or self.state != State.RUNNING:
            raise PluginStopped(f"{self.name} is not running ({self.state.value})")
        self._next_id += 1
        call_id = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[call_id] = fut
        await channel.send({"t": "call", "id": call_id, "method": method, "args": args or [],
                            "kwargs": kwargs or {}, "legacy": legacy})
        try:
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
        if self._restart_task is not None:
            self._restart_task.cancel()
        proc, channel = self.proc, self.channel
        if proc is None or proc.returncode is not None:
            await self._set_state(State.STOPPED)
            return
        if channel is not None:
            try:
                await channel.send({"t": "stop", "uninstall": uninstall})
            except (ConnectionError, RuntimeError):
                pass
        try:
            await asyncio.wait_for(proc.wait(), self.cfg.stop_timeout)
        except asyncio.TimeoutError:
            log.warning("%s did not stop within %g s, killing it", self.name, self.cfg.stop_timeout)
            self._kill_group(proc)
            await proc.wait()
        # let _read finish its exit handling
        for _ in range(50):
            if self.state == State.STOPPED:
                break
            await asyncio.sleep(0.02)
        self._crash_times.clear()

    def status(self) -> dict[str, Any]:
        return {"state": self.state.value, "restarts": self.restarts, "error": self.error,
                "pid": self.proc.pid if self.proc and self.proc.returncode is None else None}
