"""Legacy name of the `decky` module: the same module object."""

import sys

import decky

sys.modules[__name__] = decky
