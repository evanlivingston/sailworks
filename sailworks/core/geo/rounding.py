# -*- coding: utf-8 -*-
"""Mark rounding values shared by route points and the Qt interface."""

ROUNDING_PORT = 0
ROUNDING_STARBOARD = 1
ROUNDING_SAIL_TO = 2
ROUNDING_GATE = 3
DEFAULT_ROUNDING = ROUNDING_SAIL_TO

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
