"""loader.json: Decky's file and keys, kept verbatim, written atomically (compat/backend.md §7)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger("remora.settings")


def _encode(data: Any) -> str:
    return json.dumps(data, indent=4, ensure_ascii=False)


def write_json_atomic(path: Path, data: Any) -> None:
    write_text_atomic(path, _encode(data))


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class Settings:
    """loader.json in memory. Changes are encoded at once (a value that isn't JSON raises in
    set()), and written atomically: synchronously when no event loop runs, else off the loop in
    a writer task that coalesces bursts and always writes the newest state last."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {}
        self._text: str | None = None  # encoded and not written yet
        self._writer: asyncio.Task[None] | None = None
        self.read()

    def read(self) -> None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self.data = {}
            return
        if not text.strip():
            self.data = {}
            return
        try:
            data = json.loads(text)
        except ValueError:
            # keep the broken file for inspection instead of overwriting it on the next write
            backup = self.path.with_name(self.path.name + ".broken")
            os.replace(self.path, backup)
            log.error("%s is not valid JSON, moved it to %s", self.path, backup)
            data = {}
        self.data = data if isinstance(data, dict) else {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> Any:
        self.update({key: value})
        return value

    def update(self, values: dict[str, Any]) -> None:
        new = {**self.data, **values}
        text = _encode(new)  # raises before anything changes
        self.data = new
        self._save(text)

    def _save(self, text: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            self._text = None
            write_text_atomic(self.path, text)
            return
        self._text = text
        if self._writer is None or self._writer.done():
            self._writer = loop.create_task(self._write_loop())

    async def _write_loop(self) -> None:
        while self._text is not None:
            text, self._text = self._text, None
            try:
                await asyncio.to_thread(write_text_atomic, self.path, text)
            except Exception:
                log.exception("writing %s failed", self.path)

    async def flush(self) -> None:
        """Wait until every change is on disk."""
        while self._writer is not None and not self._writer.done():
            await asyncio.shield(self._writer)
        if self._text is not None:
            text, self._text = self._text, None
            await asyncio.to_thread(write_text_atomic, self.path, text)

    # list helpers for pluginOrder, disabled_plugins and the like
    def list_add(self, key: str, item: str) -> None:
        items = list(self.get(key) or [])
        if item not in items:
            items.append(item)
            self.set(key, items)

    def list_remove(self, key: str, item: str) -> None:
        items = list(self.get(key) or [])
        # also entries like {"name": item} (remora.rootApproved)
        kept = [i for i in items if i != item and not (isinstance(i, dict) and i.get("name") == item)]
        if len(kept) != len(items):
            self.set(key, kept)
