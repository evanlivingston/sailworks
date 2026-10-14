"""GTK scheduling adapter for the shared debounced wind sampler."""

from gi.repository import GLib
from gweatherrouting.rendering.windcursor import WindCursorSampler as SharedSampler
from gweatherrouting.rendering.windcursor import CursorGrib as DisplayGrib


class WindCursorSampler(SharedSampler):
    def __init__(self):
        super().__init__(
            GLib.timeout_add,
            GLib.source_remove,
            GLib.idle_add,
            lambda source: DisplayGrib(source),
        )
