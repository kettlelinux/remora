"""Type stub for the `decky` module a plugin's main.py imports (also importable as `decky_plugin`).

It matches what Remora's loader provides (backend/remora/compat/decky.py), which is the module
Decky Loader plugins already use. Put this folder on your type checker's path, for example in
pyrightconfig.json: {"extraPaths": ["/path/to/remora/sdk"]}, or in mypy: mypy_path = sdk.

The constants are read from the environment when the module is imported. The loader sets them
for each plugin's process before main.py runs.
"""

import logging
from typing import Any

__version__: str
"""The `decky` module's own API version, "1.0.0"."""

HOME: str
"""The home folder of the user the plugin runs as: root's for a root plugin, else the Game Mode user's."""

USER: str
"""The user the plugin runs as: "root" for a root plugin, else the Game Mode user."""

DECKY_VERSION: str
"""The Decky Loader version the loader is compatible with, like "v3.3.0"."""

DECKY_USER: str
"""The Game Mode user's name, even for a root plugin ("deck" on a Steam Deck)."""

DECKY_USER_HOME: str
"""The Game Mode user's home folder, even for a root plugin."""

DECKY_HOME: str
"""The loader's home for per-plugin files, usually ~/homebrew."""

DECKY_PLUGIN_SETTINGS_DIR: str
"""Where this plugin keeps its settings: DECKY_HOME/settings/<plugin folder name>. It exists."""

DECKY_PLUGIN_RUNTIME_DIR: str
"""Where this plugin keeps runtime data: DECKY_HOME/data/<plugin folder name>. It exists.
Files here are served at http://127.0.0.1:1337/plugins/<plugin name>/data/<path>."""

DECKY_PLUGIN_LOG_DIR: str
"""Where this plugin's logs go: DECKY_HOME/logs/<plugin folder name>. It exists."""

DECKY_PLUGIN_DIR: str
"""The plugin's own folder (where main.py and plugin.json are). It may be read-only."""

DECKY_PLUGIN_NAME: str
"""The plugin's name from plugin.json."""

DECKY_PLUGIN_VERSION: str
"""The version from package.json, or "" when the plugin has none."""

DECKY_PLUGIN_AUTHOR: str
"""The author from plugin.json."""

DECKY_PLUGIN_LOG: str
"""This run's log file: DECKY_PLUGIN_LOG_DIR/<YYYY-MM-DD HH.MM.SS>.log."""

logger: logging.Logger
"""The root logger, set up to write to DECKY_PLUGIN_LOG and stderr at INFO. Libraries the
plugin uses log there too. Only the newest few log files are kept."""


def migrate_any(target_dir: str, *files_or_directories: str) -> dict[str, str]:
    """Move files, or the contents of folders, into target_dir (made if missing), removing the
    sources. Returns {old path: new path}; a source that doesn't exist maps to ""."""


def migrate_settings(*files_or_directories: str) -> dict[str, str]:
    """migrate_any into DECKY_PLUGIN_SETTINGS_DIR. Meant for `_migration`."""


def migrate_runtime(*files_or_directories: str) -> dict[str, str]:
    """migrate_any into DECKY_PLUGIN_RUNTIME_DIR. Meant for `_migration`."""


def migrate_logs(*files_or_directories: str) -> dict[str, str]:
    """migrate_any into DECKY_PLUGIN_LOG_DIR. Meant for `_migration`."""


async def emit(event: str, *args: Any) -> None:
    """Send an event to this plugin's frontend: listeners added with
    `addEventListener(event, (...args) => ...)` from @decky/api are called with args.
    The args must be JSON-serializable. Must be awaited."""
