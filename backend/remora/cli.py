"""Command line: `remora` (or `remora serve`) runs the loader."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from . import __version__
from .config import Config


def _setup_logging(level: int) -> None:
    fmt = "%(levelname)s %(name)s: %(message)s"
    if sys.stderr.isatty():
        fmt = "%(asctime)s " + fmt
    logging.basicConfig(level=level, format=fmt, stream=sys.stderr)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="remora", description="Steam Game Mode plugin loader")
    parser.add_argument("--version", action="version", version=f"remora {__version__}")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="run the loader (the default)")
    serve.add_argument("--no-inject", action="store_true", help="don't inject into Steam")
    serve.add_argument("--plugins", type=Path, action="append", default=[],
                       help="also load plugins from this folder (repeatable)")
    args = parser.parse_args(argv)

    cfg = Config.from_env()
    _setup_logging(cfg.log_level)
    if args.command in (None, "serve"):
        if getattr(args, "no_inject", False):
            cfg.inject = False
        cfg.extra_plugin_dirs = list(getattr(args, "plugins", []) or [])
        from .app import run

        asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
