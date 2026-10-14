"""Compatibility import for shared wind rendering."""

import sys
from gweatherrouting.rendering import windrenderer as _implementation
from gweatherrouting.rendering.windrenderer import *  # noqa: F401,F403

sys.modules[__name__] = _implementation
