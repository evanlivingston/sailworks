"""Time bounds for geographic scene elements.

Pure helpers so the scene panel and the timeline agree on what a selection
spans in time. Nothing here touches Qt.
"""

from datetime import datetime

# Tracks with a departure but no per-point times need a duration; 2 km/h is a
# slow sail and only ever sets the end of the fitted region.
FALLBACK_SPEED_KMH = 2.0

# A zero-width region leaves the timeline scrubber unusable.
MINIMUM_SPAN_SECONDS = 3600.0

# A selection spanning years is not a usable timeline region; the window shows
# a notice instead of fitting it.
MAXIMUM_FIT_SPAN_SECONDS = 366 * 86400

METRES_PER_NAUTICAL_MILE = 1852.0


def to_epoch(value) -> float | None:
    """Convert a datetime, epoch number or ISO string to epoch seconds."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
        # `routingjob.serialize_routing_points` writes this local-time format
        # when currents are disabled.
        try:
            return datetime.strptime(text, "%m/%d/%Y, %H:%M:%S").timestamp()
        except ValueError:
            return None
    return None


def point_time(point) -> float | None:
    """Return the epoch time carried at point index 2, when there is one."""
    try:
        if len(point) > 2:
            return to_epoch(point[2])
    except TypeError:
        return None
    return None


def _element_points(element):
    points = getattr(element, "points", None)
    if points is None:
        return ()
    try:
        return list(points)
    except TypeError:
        return ()


def _minimum_span(start, end):
    if end > start:
        return (start, end)
    # Keep the scratch region usable when a departure has no length.
    return (start, start + MINIMUM_SPAN_SECONDS)


def element_time_bounds(
    element, speed_kmh: float = FALLBACK_SPEED_KMH
) -> tuple[float, float] | None:
    """Return the earliest and latest epoch time an element covers."""
    stamps = [
        stamp
        for stamp in (point_time(point) for point in _element_points(element))
        if stamp is not None
    ]
    if stamps:
        return _minimum_span(min(stamps), max(stamps))

    stamps = [
        stamp
        for stamp in (
            to_epoch(value) for value in getattr(element, "timestamps", None) or ()
        )
        if stamp is not None
    ]
    if stamps:
        return _minimum_span(min(stamps), max(stamps))

    start = to_epoch(getattr(element, "default_departure", None))
    if start is None:
        return None
    length = getattr(element, "length", None)
    length_nm = float(length()) if callable(length) else 0.0
    duration = 0.0
    if speed_kmh > 0:
        duration = length_nm * 3600.0 * METRES_PER_NAUTICAL_MILE / (speed_kmh * 1000.0)
    return _minimum_span(start, start + duration)


def clamp_bounds(bounds, limit):
    """Intersect a fitted region with the coverage a forecast can show.

    ``None`` means there is nothing to show: either the region is unknown, or
    it lies entirely outside the available coverage. Callers should then leave
    their view untouched rather than install an empty region. A missing limit
    leaves the region unchanged, because there is nothing better to clamp to.
    """
    if bounds is None:
        return None
    if limit is None:
        return bounds
    start = max(bounds[0], limit[0])
    end = min(bounds[1], limit[1])
    if end < start:
        return None
    return (start, end)


def selection_time_bounds(
    elements, speed_kmh: float = FALLBACK_SPEED_KMH
) -> tuple[float, float] | None:
    """Union the time bounds of several elements, or None when none have time."""
    bounds = [
        element_time_bounds(element, speed_kmh)
        for element in elements
        if element is not None
    ]
    present = [entry for entry in bounds if entry is not None]
    if not present:
        return None
    return (min(start for start, _ in present), max(end for _, end in present))
