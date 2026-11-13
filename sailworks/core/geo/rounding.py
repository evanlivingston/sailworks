# -*- coding: utf-8 -*-
"""Mark rounding values shared by route points and the Qt interface."""

import math
from collections.abc import Mapping

ROUNDING_PORT = 0
ROUNDING_STARBOARD = 1
ROUNDING_SAIL_TO = 2
ROUNDING_GATE = 3
DEFAULT_ROUNDING = ROUNDING_SAIL_TO
DEFAULT_ROUNDING_RADIUS_NM = 0.5

ROUNDING_LABELS = {
    ROUNDING_PORT: "Leave to port",
    ROUNDING_STARBOARD: "Leave to starboard",
    ROUNDING_SAIL_TO: "Sail to mark",
    ROUNDING_GATE: "Gate",
}

ROUNDING_SHORT = {
    ROUNDING_PORT: "Port",
    ROUNDING_STARBOARD: "Stbd",
    ROUNDING_SAIL_TO: "Sail",
    ROUNDING_GATE: "Gate",
}


def normalize_rounding(value):
    """Return a valid rounding value, defaulting to sail-to."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return DEFAULT_ROUNDING
    return value if value in ROUNDING_LABELS else DEFAULT_ROUNDING


def rounding_label(value, short=False):
    value = normalize_rounding(value)
    return (ROUNDING_SHORT if short else ROUNDING_LABELS)[value]


def normalize_rounding_spec(spec):
    """Return a routing-ready rounding spec, or None for an exact mark.

    A spec is ``{"side": "port"|"starboard", "radius_nm": float,
    "exit_bearing_min_deg": float|None, "exit_bearing_max_deg": float|None}``.
    The radius is inert legacy metadata: an explicit number is echoed exactly,
    including zero and negative values, and only an absent or non-numeric value
    falls back to the default. Incomplete or invalid exit ranges are cleared
    rather than partially applied.
    """
    if not isinstance(spec, Mapping):
        return None
    side = str(spec.get("side", "")).strip().lower()
    if side not in ("port", "starboard"):
        return None
    radius = spec.get("radius_nm")
    if type(radius) not in (int, float) or not math.isfinite(radius):
        radius = DEFAULT_ROUNDING_RADIUS_NM
    minimum = spec.get("exit_bearing_min_deg")
    maximum = spec.get("exit_bearing_max_deg")
    if minimum is None or maximum is None:
        minimum = maximum = None
    elif (
        type(minimum) not in (int, float)
        or type(maximum) not in (int, float)
        or not math.isfinite(minimum)
        or not math.isfinite(maximum)
        or not 0 <= minimum <= maximum < 360
    ):
        minimum = maximum = None
    else:
        minimum = float(minimum)
        maximum = float(maximum)
    return dict(
        side=side,
        radius_nm=float(radius),
        exit_bearing_min_deg=minimum,
        exit_bearing_max_deg=maximum,
    )
