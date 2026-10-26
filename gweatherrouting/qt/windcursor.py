"""Qt adapter for the shared private-GRIB cursor sampler."""

from gweatherrouting.rendering.windcursor import WindCursorSampler as SharedSampler
from gweatherrouting.rendering.windcursor import CursorGrib as DisplayGrib


class WindCursorSampler(SharedSampler):
    def __init__(self, parent, dispatch):
        super().__init__(dispatch, lambda source: DisplayGrib(source))
