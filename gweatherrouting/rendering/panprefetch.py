"""Bounded pan prediction from successful weather preparation times."""

from dataclasses import dataclass
import math


@dataclass
class PanPreparationTime:
    key: object = None
    seconds: float | None = None
    samples: int = 0

    def observe(self, key, seconds):
        if not math.isfinite(seconds) or seconds <= 0:
            return
        if key != self.key or self.seconds is None:
            self.key, self.seconds, self.samples = key, seconds, 1
        else:
            self.seconds = 0.75 * self.seconds + 0.25 * seconds
            self.samples += 1

    def lookahead(self, key):
        if key != self.key or self.seconds is None:
            return 1.0
        # Allow for preparation variability and the next UI delivery/repaint.
        # Keep both slow-machine travel and very fast-job predictions bounded.
        return max(0.25, min(2.0, 1.25 * self.seconds + 0.1))


def pan_prefetch_offset(vx, vy, width, height, seconds=1.0, max_pixels=None):
    """Return a bounded pixel displacement, or None inside the reuse margin."""
    if not all(math.isfinite(value) for value in (vx, vy, width, height, seconds)):
        return None
    travel = math.hypot(vx, vy) * seconds
    if travel < 192 or seconds <= 0 or min(width, height) <= 0:
        return None
    limit = 0.75 * max(width, height)
    if max_pixels is not None:
        limit = min(limit, max_pixels)
    scale = min(1.0, limit / travel) * seconds
    return vx * scale, vy * scale
