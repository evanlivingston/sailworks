"""Qt adapter for the shared private-GRIB cursor sampler."""

from sailworks.rendering.windcursor import WindCursorSampler as SharedSampler
from sailworks.rendering.windcursor import CursorGrib as DisplayGrib


class WindCursorSampler(SharedSampler):
    def __init__(self, parent, dispatch):
        super().__init__(dispatch, lambda source: DisplayGrib(source))
