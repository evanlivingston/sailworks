"""Cairo-independent marine values and portrayal helpers."""

import math
from dataclasses import dataclass
from datetime import timezone

from gweatherrouting.core.marine.manager import Selection


def chart_instant(value):
    """Convert the chart's local wall time to aware UTC for marine data."""
    return value.astimezone(timezone.utc)


CURRENT_DENSITIES = (
    ("sparse", "Sparse", 128),
    ("standard", "Standard", 64),
    ("dense", "Dense", 32),
    ("maximum", "Maximum", 16),
)


def current_grid_spacing(density):
    return next(
        (spacing for key, _label, spacing in CURRENT_DENSITIES if key == density),
        64,
    )


def flow(east, north):
    """Speed in knots and bearing TOWARD true north, clockwise."""
    return (
        math.hypot(east, north) * 3600 / 1852,
        math.degrees(math.atan2(east, north)) % 360,
    )


def current_style(speed):
    """Fixed knots scale: green, yellow (1), orange (2), red (3+)."""
    stops = (
        (0, (0.12, 0.70, 0.25)),
        (1, (0.98, 0.85, 0.08)),
        (2, (1.0, 0.45, 0.08)),
        (3, (0.94, 0.12, 0.10)),
    )
    color = stops[-1][1]
    for (low, a), (high, b) in zip(stops, stops[1:]):
        if speed <= high:
            weight = max(0, (speed - low) / (high - low))
            color = tuple(x + (y - x) * weight for x, y in zip(a, b))
            break
    length = 10 + min(max(speed, 0), 5) * 5
    return length, color


@dataclass(frozen=True, eq=False)
class MarineSources:
    fields: tuple
    levels: Selection
    currents: Selection
    density: str = "standard"
