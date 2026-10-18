"""Command line.

  remora [serve]                      run the loader (what plugin_loader.service starts)
  remora dev <plugin-dir>             a local loader as you, with only that plugin, reloading on change
  remora call <plugin-dir> <method>   start just that plugin's backend, call one method, print the result
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import pwd
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config


def _setup_logging(level: int) -> None:
    fmt = "%(levelname)s %(name)s: %(message)s"
    if sys.stderr.isatty():
        fmt = "%(asctime)s " + fmt
    logging.basicConfig(level=level, format=fmt, stream=sys.stderr)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


# -- dev and call: a loader for one plugin, as the current user -------------------------------

def _plugin_dir(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not (path / "plugin.json").is_file():
        raise argparse.ArgumentTypeError(f"{value} has no plugin.json")
    return path


def _dev_home(plugin: Path, home: Path | None) -> Path:
    """Where the plugin's settings, data and logs go (its DECKY_HOME) in dev and call."""
    if home is not None:
        return home.expanduser().resolve()
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache / "remora-dev" / plugin.name


def _dev_config(plugin: Path, home: Path | None) -> Config:
    cfg = Config.from_env()
    me = pwd.getpwuid(os.getuid())
    hb = _dev_home(plugin, home)
    cfg.user, cfg.uid, cfg.gid, cfg.user_home = me.pw_name, me.pw_uid, me.pw_gid, Path(me.pw_dir)
    cfg.homebrew = cfg.privileged = hb
    cfg.extra_plugin_dirs = [plugin]
    return cfg


def _dev(args: argparse.Namespace) -> None:
    cfg = _dev_config(args.plugin_dir, args.home)
    if args.port is not None:
        cfg.port = args.port
    cfg.inject = bool(args.inject)
    cfg.live_reload = not args.no_reload
    cfg.reload_all = True
    _setup_logging(logging.DEBUG if args.verbose else cfg.log_level)
    log = logging.getLogger("remora.dev")
    log.info("plugin %s, settings/data/logs in %s", args.plugin_dir, cfg.homebrew)
    if not cfg.inject:
        log.info("not injecting into Steam (pass --inject to)")
    from .app import run

    try:
        asyncio.run(run(cfg, show_token=True))
    except OSError as e:
        sys.exit(f"remora dev: {e} (is a loader already on port {cfg.port}? try --port)")


def _parse_value(text: str) -> Any:
    """A command-line argument as JSON, or as a plain string when it isn't JSON."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def _legacy_kwargs(values: list[str]) -> dict[str, Any]:
    """--legacy arguments: JSON objects merged in order, or key=value pairs."""
    kwargs: dict[str, Any] = {}
    for text in values:
        value = _parse_value(text)
        if isinstance(value, dict):
            kwargs.update(value)
        elif "=" in text:
            key, _, raw = text.partition("=")
            kwargs[key] = _parse_value(raw)
        else:
            raise SystemExit(f"remora call: --legacy takes JSON objects or key=value, not {text!r}")
    return kwargs


async def _call_once(args: argparse.Namespace) -> int:
    from .fsutil import makedirs_as_user
    from .plugins.manifest import ManifestError, read_plugin
    from .plugins.process import PluginCallError, PluginProcess, PluginStopped

    cfg = _dev_config(args.plugin_dir, args.home)
    cfg.call_timeout = args.timeout
    try:
        info = read_plugin(args.plugin_dir)
    except ManifestError as e:
        print(f"remora call: {e}", file=sys.stderr)
        return 2
    if info.passive:
        print(f"remora call: {info.name} has no main.py", file=sys.stderr)
        return 2
    legacy = args.legacy or info.api_version < 1
    kwargs = _legacy_kwargs(args.args) if legacy else {}
    for sub in ("settings", "data", "logs"):
        makedirs_as_user(cfg.homebrew / sub / info.dir_name, cfg.user_home, cfg.uid, cfg.gid)

    async def on_event(event: str, event_args: list[Any]) -> None:
        print(f"event {event} {json.dumps(event_args, ensure_ascii=False)}", file=sys.stderr)

    async def on_state(proc: PluginProcess) -> None:
        pass

    proc = PluginProcess(cfg, info, on_event, on_state)
    await proc.start()
    try:
        if args.wait > 0:  # let _main get going first
            await asyncio.sleep(args.wait)
        if legacy:
            result = await proc.call(args.method, [], kwargs, legacy=True)
        else:
            result = await proc.call(args.method, [_parse_value(a) for a in args.args])
    except PluginCallError as e:
        err = e.error
        print((err.get("traceback") or f"{err.get('name')}: {err.get('message')}").rstrip(),
              file=sys.stderr)
        return 1
    except PluginStopped as e:
        print(f"remora call: {(proc.error or str(e)).split('; restarting')[0]}", file=sys.stderr)
        return 1
    finally:
        await proc.stop()
    print(json.dumps(result, indent=None if args.compact else 2, ensure_ascii=False))
    return 0


# -- main ---------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="remora", description="Steam Game Mode plugin loader")
    parser.add_argument("--version", action="version", version=f"remora {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the loader (the default)")
    serve.add_argument("--no-inject", action="store_true", help="don't inject into Steam")
    serve.add_argument("--plugins", type=Path, action="append", default=[],
                       help="also load plugins from this folder (repeatable)")

    dev = sub.add_parser("dev", help="run a local loader with only this plugin, reloading it on change",
                         description="Run a loader as the current user with only this plugin. "
                                     "Its backend and frontend reload when main.py, py_modules or "
                                     "dist/index.js change, debug flag or not.")
    dev.add_argument("plugin_dir", type=_plugin_dir, help="the plugin's folder (with plugin.json)")
    dev.add_argument("--port", type=int, default=None,
                     help="HTTP/WebSocket port (default: SERVER_PORT or 1337; plugin bundles "
                          "hardcode 1337 for their assets)")
    dev.add_argument("--home", type=Path, default=None,
                     help="DECKY_HOME for the plugin's settings, data and logs "
                          "(default: ~/.cache/remora-dev/<folder>)")
    dev.add_argument("--inject", action="store_true",
                     help="inject into Steam through CEF devtools (stop plugin_loader.service first)")
    dev.add_argument("--no-reload", action="store_true", help="don't reload on file changes")
    dev.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    call = sub.add_parser("call", help="call one backend method without Steam or the server",
                          description="Start only this plugin's backend, call METHOD, print the "
                                      "result as JSON on stdout, and stop it (running _unload). "
                                      "Plugin output and events go to stderr.")
    call.add_argument("plugin_dir", type=_plugin_dir, help="the plugin's folder (with plugin.json)")
    call.add_argument("method")
    call.add_argument("args", nargs="*",
                      help="arguments, each parsed as JSON (a bare word is a string); with "
                           "--legacy, JSON objects or key=value pairs, passed as keyword arguments")
    call.add_argument("--legacy", action="store_true",
                      help="call the api_version 0 way, with keyword arguments (automatic for "
                           "api_version 0 plugins)")
    call.add_argument("--home", type=Path, default=None,
                      help="DECKY_HOME (default: ~/.cache/remora-dev/<folder>, shared with dev)")
    call.add_argument("--timeout", type=float, default=60.0, help="seconds to wait (0: no limit)")
    call.add_argument("--wait", type=float, default=0.0,
                      help="seconds to let _main run before the call")
    call.add_argument("--compact", action="store_true", help="print the JSON on one line")

    args = parser.parse_args(argv)

    if args.command == "dev":
        _dev(args)
        return
    if args.command == "call":
        _setup_logging(Config.from_env().log_level)
        sys.exit(asyncio.run(_call_once(args)))

    cfg = Config.from_env()
    _setup_logging(cfg.log_level)
    if getattr(args, "no_inject", False):
        cfg.inject = False
    cfg.extra_plugin_dirs = list(getattr(args, "plugins", []) or [])
    from .app import run

    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
