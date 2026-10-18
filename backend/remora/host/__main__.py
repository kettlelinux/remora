"""A plugin host: one fresh interpreter per plugin, started by the supervisor, already running as
the plugin's user. It imports main.py, runs the lifecycle hooks and serves method calls over the
socket it inherited (compat/backend.md §3, §4; remora/ipc.py for the messages).

Invoked as: python -m remora.host '<spec json>'
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import logging
import os
import signal
import socket
import sys
import traceback
from typing import Any

from remora.ipc import Channel, encode

log = logging.getLogger("remora.host")


def _error(e: BaseException) -> dict[str, Any]:
    return {"name": type(e).__name__, "message": str(e),
            "traceback": "".join(traceback.format_exception(e))}


class Host:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.name: str = spec["name"]
        self.api_version: int = spec["api_version"]
        self.legacy = self.api_version < 1
        self.channel: Channel | None = None
        self.target: Any = None  # the Plugin instance, or the class itself for api_version 0
        self.ready = asyncio.Event()
        self.stopping = False
        self.tasks: set[asyncio.Task[Any]] = set()

    # -- the plugin --------------------------------------------------------------------------

    def _setup_paths(self) -> None:
        plugin_dir = self.spec["plugin_dir"]
        # Decky's own modules were importable by bare name in its plugin processes (settings,
        # helpers, ...). The compat folder goes first so those names resolve the same way.
        sys.path.insert(0, self.spec["compat_dir"])
        sys.path.append(os.path.join(plugin_dir, "py_modules"))

    def _import(self) -> None:
        import decky  # remora/compat/decky.py

        decky._remora_set_emit(self.emit)
        sys.modules["decky_plugin"] = decky
        spec = importlib.util.spec_from_file_location("_", self.spec["main_py"])
        if spec is None or spec.loader is None:
            raise ImportError(f"can't load {self.spec['main_py']}")
        module = importlib.util.module_from_spec(spec)
        sys.modules["_"] = module
        spec.loader.exec_module(module)
        plugin = getattr(module, "Plugin")
        self.target = plugin if self.legacy else plugin()

    def _bound(self, attr: str) -> Any:
        fn = getattr(self.target, attr)
        if self.legacy:
            # api_version 0: methods are called on the class, with the class as self
            return lambda *a, **kw: fn(self.target, *a, **kw)
        return fn

    async def _invoke(self, attr: str, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self.target, attr)
        call = self._bound(attr)
        if inspect.iscoroutinefunction(fn):
            return await call(*args, **kwargs)
        # Decky refuses sync methods; running them in a thread makes them work instead of
        # blocking every other call
        result = await asyncio.to_thread(call, *args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def _hook(self, attr: str) -> None:
        if getattr(self.target, attr, None) is None:
            return
        try:
            await self._invoke(attr)
        except Exception:
            log.exception("%s.%s failed", self.name, attr)

    # -- messages ----------------------------------------------------------------------------

    async def emit(self, event: str, *args: Any) -> None:
        # encode here: a value that isn't JSON raises in the plugin, not in the channel
        data = encode({"t": "event", "event": str(event), "args": list(args)})
        if self.channel is not None:
            await self.channel.send_encoded(data)

    async def _call(self, message: dict[str, Any]) -> None:
        call_id = message.get("id")
        method = str(message.get("method", ""))
        try:
            await self.ready.wait()
            if method.startswith("_"):
                raise PermissionError(f"{method} is private")
            if not hasattr(self.target, method):
                raise AttributeError(f"{self.name} has no method {method}")
            if message.get("legacy"):
                if not self.legacy:
                    raise RuntimeError("Legacy methods may not be used on api_version > 0")
                value = await self._invoke(method, **(message.get("kwargs") or {}))
            else:
                if self.legacy:
                    raise RuntimeError("api_version 1 or newer is required to call plugin "
                                       "methods with this API")
                value = await self._invoke(method, *(message.get("args") or []))
            try:  # serialized once, here: big results aren't encoded twice
                data = encode({"t": "result", "id": call_id, "ok": True, "value": value})
            except (TypeError, ValueError) as e:
                raise TypeError(f"{method} returned a value that isn't JSON: {e}") from e
        except Exception as e:
            data = encode({"t": "result", "id": call_id, "ok": False, "error": _error(e)})
        assert self.channel is not None
        await self.channel.send_encoded(data)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def stop(self, uninstall: bool) -> None:
        if self.stopping:
            return
        self.stopping = True
        if self.target is not None:
            await self._hook("_unload")
            if uninstall:
                await self._hook("_uninstall")
        if self.channel is not None:
            try:
                await self.channel.send({"t": "stopped"})
            except (ConnectionError, RuntimeError):
                pass
        logging.shutdown()
        # os._exit: threads the plugin started must not keep the process alive
        os._exit(0)

    async def _serve(self) -> None:
        assert self.channel is not None
        while True:
            message = await self.channel.receive()
            if message is None:  # the supervisor went away
                await self.stop(False)
                return
            kind = message.get("t")
            if kind == "call":
                self._spawn(self._call(message))
            elif kind == "stop":
                self._spawn(self.stop(bool(message.get("uninstall"))))

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: self._spawn(self.stop(False)))
        # not inherited by what the plugin runs (os.system, subprocess): a child holding the
        # socket would hide this host's exit from the supervisor
        os.set_inheritable(self.spec["fd"], False)
        sock = socket.socket(fileno=self.spec["fd"])
        self.channel = await Channel.from_socket(sock)
        # serve first, so stop requests and queued calls work while _migration runs
        self._spawn(self._serve())
        try:
            self._setup_paths()
            self._import()
            if getattr(self.target, "_migration", None) is not None:
                await self._invoke("_migration")
        except BaseException as e:
            log.error("%s failed to start:\n%s", self.name, traceback.format_exc())
            await self.channel.send({"t": "fatal", "error": _error(e)})
            logging.shutdown()
            os._exit(1)
        self.ready.set()
        await self.channel.send({"t": "ready"})
        if getattr(self.target, "_main", None) is not None:
            self._spawn(self._hook("_main"))
        await asyncio.Event().wait()


def main() -> None:
    spec = json.loads(sys.argv[1])
    logging.basicConfig(level=spec.get("log_level", logging.INFO),
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        import setproctitle  # type: ignore[import-not-found]

        setproctitle.setproctitle(f"{spec['name']} ({spec['main_py']})")
    except ImportError:
        pass
    asyncio.run(Host(spec).run())


if __name__ == "__main__":
    main()
