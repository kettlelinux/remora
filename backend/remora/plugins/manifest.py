"""Reading a plugin folder: plugin.json and package.json (compat/backend.md §2)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOAD_LEGACY_IIFE = 0
LOAD_ESM = 1


class ManifestError(Exception):
    pass


@dataclass
class PluginInfo:
    name: str
    author: str
    flags: list[str]
    api_version: int
    dir_name: str
    path: Path  # <plugins>/<dir_name>, possibly a link (Kettle links image-shipped plugins in)
    version: str | None
    load_type: int
    publish: dict[str, Any] = field(default_factory=dict)

    @property
    def main_py(self) -> Path:
        return self.path / "main.py"

    @property
    def passive(self) -> bool:
        return not self.main_py.is_file()

    @property
    def root(self) -> bool:
        return "root" in self.flags

    @property
    def debug(self) -> bool:
        return "debug" in self.flags


def _read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_plugin(path: Path) -> PluginInfo:
    plugin_json = path / "plugin.json"
    if not plugin_json.is_file():
        raise ManifestError(f"{path} has no plugin.json")
    try:
        data = _read_json(plugin_json)
    except (OSError, ValueError) as e:
        raise ManifestError(f"{plugin_json}: {e}") from e
    if not isinstance(data, dict):
        raise ManifestError(f"{plugin_json}: not an object")
    try:
        name = data["name"]
        author = data["author"]
        flags = data["flags"]
    except KeyError as e:
        raise ManifestError(f"{plugin_json}: missing {e.args[0]!r}") from e
    if not isinstance(name, str) or not name.strip():
        raise ManifestError(f"{plugin_json}: bad name")
    if not isinstance(flags, list):
        raise ManifestError(f"{plugin_json}: flags is not a list")
    try:
        api_version = int(data.get("api_version", 0) or 0)
    except (TypeError, ValueError):
        api_version = 0

    version: str | None = None
    load_type = LOAD_LEGACY_IIFE
    package_json = path / "package.json"
    if package_json.is_file():
        try:
            package = _read_json(package_json)
        except (OSError, ValueError) as e:
            raise ManifestError(f"{package_json}: {e}") from e
        if isinstance(package, dict):
            if "version" not in package:
                raise ManifestError(f"{package_json}: missing 'version'")
            version = str(package["version"])
            if package.get("type") == "module":
                load_type = LOAD_ESM

    return PluginInfo(
        name=name,
        author=str(author),
        flags=[str(f) for f in flags],
        api_version=api_version,
        dir_name=path.name,
        path=path,
        version=version,
        load_type=load_type,
        publish=data.get("publish") if isinstance(data.get("publish"), dict) else {},
    )


def scan(plugins_dir: Path) -> tuple[list[PluginInfo], list[tuple[Path, str]]]:
    """All plugins in the folder, in directory-name order, and the folders that failed to read."""
    found: list[PluginInfo] = []
    errors: list[tuple[Path, str]] = []
    try:
        names = sorted(os.listdir(plugins_dir))
    except FileNotFoundError:
        return found, errors
    for entry in names:
        if entry.startswith("."):
            continue
        path = plugins_dir / entry
        if not path.is_dir() or not (path / "plugin.json").is_file():
            continue
        try:
            found.append(read_plugin(path))
        except ManifestError as e:
            errors.append((path, str(e)))
    return found, errors
