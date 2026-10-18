"""Decky's `settings` module as legacy plugins import it: `from settings import SettingsManager`."""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any


class SettingsManager:
    def __init__(self, name: str, settings_directory: str | None = None) -> None:
        if settings_directory is None:
            settings_directory = os.path.join(os.environ.get("DECKY_HOME", ""), "settings")
        os.makedirs(settings_directory, exist_ok=True)
        self.path = os.path.join(settings_directory, name + ".json")
        self.settings: dict[str, Any] = {}
        try:
            open(self.path, "x", encoding="utf-8").close()
        except FileExistsError:
            self.read()

    def read(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.settings = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self.settings = {}

    def commit(self) -> None:
        directory = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(prefix=".settings.", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=4, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def getSetting(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    def setSetting(self, key: str, value: Any) -> Any:
        self.settings[key] = value
        self.commit()
        return value
