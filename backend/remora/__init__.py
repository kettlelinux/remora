"""Remora: a plugin loader for Steam's Game Mode UI that runs Decky Loader plugins unchanged."""

__version__ = "0.1.0"

# The Decky Loader version Remora is compatible with. Plugins read it as DECKY_VERSION, and the
# Decky store refuses clients that report an older one.
DECKY_COMPAT_VERSION = "v3.3.0"
