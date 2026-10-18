"""loader.json: Decky's file and keys, kept verbatim, written atomically (compat/backend.md §7)."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger("remora.settings")


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
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
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {}
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
        self.data[key] = value
        write_json_atomic(self.path, self.data)
        return value

    def update(self, values: dict[str, Any]) -> None:
        self.data.update(values)
        write_json_atomic(self.path, self.data)

    # list helpers for pluginOrder, disabled_plugins and the like
    def list_add(self, key: str, item: str) -> None:
        items = list(self.get(key) or [])
        if item not in items:
            items.append(item)
            self.set(key, items)

    def list_remove(self, key: str, item: str) -> None:
        items = list(self.get(key) or [])
        if item in items:
            items = [i for i in items if i != item]
            self.set(key, items)
