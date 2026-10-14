"""Compatibility import for the shared marine rendering implementation."""

import sys
from gweatherrouting.rendering import marinelandmask as _implementation
from gweatherrouting.rendering.marinelandmask import *  # noqa: F401,F403

sys.modules[__name__] = _implementation
