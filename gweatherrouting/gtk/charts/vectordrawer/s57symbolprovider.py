"""Compatibility import for the shared rendering implementation."""

import sys
from gweatherrouting.rendering.vectordrawer import s57symbolprovider as _implementation
from gweatherrouting.rendering.vectordrawer.s57symbolprovider import *  # noqa: F401,F403

# Preserve module identity for existing imports and patch targets.
sys.modules[__name__] = _implementation
