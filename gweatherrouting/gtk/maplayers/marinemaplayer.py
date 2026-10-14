"""Compatibility import for the shared marine rendering implementation."""

import sys
from gweatherrouting.rendering import marinemaplayer as _implementation
from gweatherrouting.rendering.marinemaplayer import *  # noqa: F401,F403

sys.modules[__name__] = _implementation
