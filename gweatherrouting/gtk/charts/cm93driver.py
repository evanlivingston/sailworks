"""Compatibility import for the shared rendering implementation."""

import sys
from gweatherrouting.rendering import cm93driver as _implementation
from gweatherrouting.rendering.cm93driver import *  # noqa: F401,F403

# Preserve module identity for existing imports and patch targets.
sys.modules[__name__] = _implementation
