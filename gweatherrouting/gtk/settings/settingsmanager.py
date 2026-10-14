"""Compatibility import for the shared rendering implementation."""

import sys
from gweatherrouting.core import settings as _implementation
from gweatherrouting.core.settings import *  # noqa: F401,F403

# Preserve module identity for existing imports and patch targets.
sys.modules[__name__] = _implementation
