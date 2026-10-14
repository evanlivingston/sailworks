"""Compatibility import for the shared rendering implementation."""

import sys
from gweatherrouting.rendering.vectordrawer import simplechartdrawer as _implementation
from gweatherrouting.rendering.vectordrawer.simplechartdrawer import *  # noqa: F401,F403

# Preserve module identity for existing imports and patch targets.
sys.modules[__name__] = _implementation
