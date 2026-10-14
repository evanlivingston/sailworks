"""GTK scheduling adapter for the shared debounced wind sampler."""

from gi.repository import GLib
from gweatherrouting.rendering.windcursor import WindCursorSampler as SharedSampler
from .windrenderer import DisplayGrib


class WindCursorSampler(SharedSampler):
    def __init__(self):
        super().__init__(
            GLib.timeout_add,
            GLib.source_remove,
            GLib.idle_add,
            lambda source: DisplayGrib(source),
        )
