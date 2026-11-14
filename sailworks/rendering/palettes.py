# -*- coding: utf-8 -*-
# Copyright (C) 2017-2026 Davide Gessa
"""
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

For detail about GNU see <http://www.gnu.org/licenses/>.
"""

"""Single registry of portraying metrics, ramps and legends.

Every colour table a route, isochrone or client can show is defined here
exactly once. ``style``, ``navigation`` and ``routingoverlay`` resolve colours
through the helpers below, so adding a dimension is a data entry rather than a
new code path and no renderer owns a ramp of its own.
"""

from dataclasses import dataclass, field

# Shared fallback. Unknown and missing values always resolve here; they are
# never mapped onto a plausible band, so a gap cannot read as a data point.
ROUTE_UNKNOWN_COLOUR = (0.55, 0.55, 0.55, 0.9)
ROUTE_UPWIND_COLOUR = (0.2, 0.55, 0.95, 0.9)
ROUTE_DOWNWIND_COLOUR = (0.95, 0.55, 0.15, 0.9)
ROUTE_PORT_COLOUR = (0.9, 0.25, 0.35, 0.9)
ROUTE_STARBOARD_COLOUR = (0.2, 0.7, 0.35, 0.9)

# Oldest to newest. Deep blue, blue, cyan, green, yellow, orange, red: the same
# ordering the eye reads as "later in the forecast".
ISOCHRONE_TIME_GRADIENT = (
    (0.02, 0.05, 0.35),
    (0.00, 0.30, 0.85),
    (0.00, 0.68, 0.75),
    (0.35, 0.82, 0.35),
    (0.95, 0.85, 0.15),
    (0.95, 0.50, 0.05),
    (0.65, 0.05, 0.05),
)

# A categorical legend longer than this is compacted into "N more" rows, so a
# route with many recorded sail plans stays legible.
DEFAULT_LEGEND_LIMIT = 8

# Colours cycled through recorded (dynamic) categories in first-appearance
# order. A plan keeps the same colour across frames and after reopen because
# leg order is recorded in the saved result.
SAIL_PLAN_CYCLE = (
    (0.20, 0.55, 0.95, 0.9),
    (0.95, 0.55, 0.15, 0.9),
    (0.90, 0.25, 0.35, 0.9),
    (0.20, 0.70, 0.35, 0.9),
    (0.65, 0.45, 0.90, 0.9),
    (0.15, 0.75, 0.80, 0.9),
    (0.95, 0.82, 0.20, 0.9),
    (0.55, 0.40, 0.25, 0.9),
)

SAIL_PROPULSION_CATEGORIES = (
    ("sail", (0.15, 0.65, 0.55, 0.9), "Sail"),
    ("motorsail", (0.95, 0.70, 0.15, 0.9), "Motor sail"),
    ("motor", (0.85, 0.30, 0.20, 0.9), "Motor"),
)

# Warm sun, grey-blue twilight, dark grey-blue night: readable on the dark
# chart and ordered like the day/night bar a GRIB viewer shows across a passage.
DAY_NIGHT_CATEGORIES = (
    ("day", (0.98, 0.82, 0.32, 0.9), "Day"),
    ("civil_twilight", (0.62, 0.60, 0.72, 0.9), "Civil twilight"),
    ("night", (0.34, 0.38, 0.48, 0.9), "Night"),
)

# The historical single-colour isochrone look. It has no metric because it does
# not portray a value; it is the first isochrone mode so it stays the default.
ISOCHRONE_UNIFORM_MODE = "uniform"

# The uniform look's colour. The retained routing item and the canvas band fill
# both resolve it here, so the stroke and its interior cannot drift apart. It is
# a light blue rather than the historical dark navy: the fill is translucent
# over satellite imagery, where a dark navy at fill alpha reads as black.
ISOCHRONE_UNIFORM_COLOUR = (0.35, 0.6, 1.0)

# Nested band fills are translucent: enough to separate consecutive frontiers,
# still light enough to read the chart and the route line through them.
ISOCHRONE_FILL_ALPHA = 0.3

# Legibility guard for the nested fill. A very dense result is capped to the
# largest contours instead of painting tens of thousands of points per frame;
# the payload discloses the cap so the caption can say what is shown.
ISOCHRONE_FILL_MAX_CONTOURS = 200
ISOCHRONE_FILL_MAX_POINTS = 50_000

# Per-layer isochrone hulls replace the closed-contour fill where a layer
# produces one, because a hull is a handful of points for a whole time step and
# a contour fill is one shape per arm. The caps match the fill's intent: bound
# the per-frame geometry, keep the largest hull even when it alone is over
# budget, and disclose anything left out.
ISOCHRONE_HULL_MAX_HULLS = 120
ISOCHRONE_HULL_MAX_POINTS = 30_000

# Band strips join the corresponding contours of two consecutive layers. Each
# band is resampled to the same point count so the two edges pair one-to-one;
# the count bounds the ring size without visibly rounding the contours.
ISOCHRONE_BAND_SAMPLES = 64
ISOCHRONE_BAND_MAX_BANDS = 200
ISOCHRONE_BAND_MAX_POINTS = 60_000

# Correspondence gate for band strips. Consecutive layers can offer a starburst
# of short candidate arms beside a long arc, and joining those draws large
# translucent wedges across unrelated water. A pair is only banded when it
# looks like the same front, so a missing band is preferred to a wrong one:
#
# - the two frontiers' arc lengths differ by at most this factor (a short arm
#   cannot be stretched along a long arc);
# - their median corresponding-point distance after alignment is at most
#   ISOCHRONE_BAND_MAX_OFFSET_FACTOR typical steps;
# - their bounding boxes meet once each is grown by one typical step (a front
#   that moved between layers still overlaps; an unrelated frontier does not).
#
# A single band is then built quad by quad: a quad is kept only when all four
# of its edges are at most ISOCHRONE_BAND_MAX_EDGE_FACTOR typical steps, so a
# local detour (a spike) breaks the strip instead of stretching it. This factor
# is deliberately tighter than the pair median factor: the pair may be somewhat
# displaced overall, but no single quad may stretch. A pair loses its band
# completely when fewer than ISOCHRONE_BAND_MIN_QUAD_FRACTION of its quads
# survive, because a mostly-broken strip is a failed correspondence.
#
# "Typical step" is the larger of the frontiers' median input segment length
# and their resampled step (arc length / samples): a densely digitised contour
# has tiny input steps, a coarsely digitised one has tiny resampled steps, and
# neither alone is a stable yardstick.
ISOCHRONE_BAND_MAX_ARC_RATIO = 2.0
ISOCHRONE_BAND_MAX_OFFSET_FACTOR = 4.0
ISOCHRONE_BAND_MAX_EDGE_FACTOR = 3.0
ISOCHRONE_BAND_MIN_QUAD_FRACTION = 0.5

# Floor for the typical step so coincident or single-point contours cannot
# divide the gate by zero; far below any chart-relevant separation.
ISOCHRONE_BAND_MIN_SPACING = 1e-6


@dataclass(frozen=True)
class Metric:
    """One portraying dimension.

    ``domain`` is ``continuous`` (numeric bands), ``categorical`` (fixed or
    recorded categories) or ``cyclic`` (an angle split into ranges).
    ``requirement`` records what the value depends on: route ``geometry``,
    recorded ``time``, sampled ``weather`` or recorded ``engine`` readouts.
    ``targets`` says which portrayals may offer the metric.
    """

    id: str
    label: str
    domain: str
    requirement: str
    targets: tuple = ("route",)
    unknown: tuple = ROUTE_UNKNOWN_COLOUR
    # continuous: ((upper limit or None, rgba, label), ...)
    bands: tuple = ()
    # categorical: ((token, rgba, label), ...) matched case-insensitively
    categories: tuple = ()
    # categorical with recorded values: colours cycled by first appearance
    cycle: tuple = ()
    # continuous gradient stops, sampled by a 0..1 position
    gradient: tuple = ()
    # append an explicit unknown row to the legend
    legend_unknown: bool = False
    # 0 disables legend compaction
    legend_limit: int = DEFAULT_LEGEND_LIMIT
    # isochrone colour mode this metric names, empty when it names none
    mode: str = ""
    # menu label of that mode, which reads better than the legend title
    mode_label: str = ""


METRICS = {
    metric.id: metric
    for metric in (
        Metric(
            id="speed",
            label="Speed",
            domain="continuous",
            requirement="geometry",
            bands=(
                (2.0, (0.3, 0.5, 1.0, 0.9), "0–2 kt"),
                (4.0, (0.2, 0.7, 0.9, 0.9), "2–4 kt"),
                (6.0, (0.2, 0.85, 0.5, 0.9), "4–6 kt"),
                (8.0, (0.5, 0.9, 0.2, 0.9), "6–8 kt"),
                (10.0, (0.9, 0.85, 0.1, 0.9), "8–10 kt"),
                (12.0, (1.0, 0.6, 0.1, 0.9), "10–12 kt"),
                (None, (1.0, 0.3, 0.1, 0.9), "12+ kt"),
            ),
        ),
        Metric(
            id="wind_angle",
            label="Upwind / Downwind",
            domain="continuous",
            requirement="geometry",
            bands=(
                (90.0, ROUTE_UPWIND_COLOUR, "Upwind (<90° TWA)"),
                (None, ROUTE_DOWNWIND_COLOUR, "Downwind (≥90° TWA)"),
            ),
            legend_unknown=True,
        ),
        Metric(
            id="tack",
            label="Port / Starboard",
            domain="categorical",
            requirement="engine",
            categories=(
                ("port", ROUTE_PORT_COLOUR, "Port"),
                ("starboard", ROUTE_STARBOARD_COLOUR, "Starboard"),
            ),
            legend_unknown=True,
        ),
        Metric(
            id="sail_plan",
            label="Sail plan",
            domain="categorical",
            requirement="engine",
            cycle=SAIL_PLAN_CYCLE,
            legend_unknown=True,
        ),
        Metric(
            id="propulsion",
            label="Propulsion",
            domain="categorical",
            requirement="engine",
            categories=SAIL_PROPULSION_CATEGORIES,
            legend_unknown=True,
        ),
        Metric(
            id="layer_time",
            label="Isochrones (UTC)",
            domain="continuous",
            requirement="time",
            targets=("isochrone",),
            gradient=ISOCHRONE_TIME_GRADIENT,
            legend_limit=0,
            mode="time",
            mode_label="Time",
        ),
        Metric(
            id="day_night",
            label="Day / night",
            domain="categorical",
            requirement="time",
            targets=("isochrone",),
            categories=DAY_NIGHT_CATEGORIES,
            legend_unknown=True,
            mode="day_night",
            mode_label="Day / night",
        ),
    )
}


def metric(metric_id):
    """The registered metric, or ``None`` for an unknown id."""
    return METRICS.get(metric_id)


def metric_label(metric_id, fallback="Speed"):
    """Display label for a metric id."""
    found = METRICS.get(metric_id)
    return fallback if found is None else found.label


def route_metrics():
    """Registered metrics a route line may be coloured by, in menu order."""
    return tuple(row for row in METRICS.values() if "route" in row.targets)


def route_metric_ids():
    """Ids of the route metrics, in menu order."""
    return tuple(row.id for row in route_metrics())


def isochrone_metrics():
    """Registered metrics an isochrone portrayal may be coloured by."""
    return tuple(
        row for row in METRICS.values() if "isochrone" in row.targets and row.mode
    )


def isochrone_modes():
    """Isochrone colour modes, uniform first and then the registry order."""
    return (ISOCHRONE_UNIFORM_MODE,) + tuple(row.mode for row in isochrone_metrics())


def isochrone_mode_metric(mode_id):
    """The metric naming an isochrone mode, or ``None`` (uniform has none)."""
    for row in isochrone_metrics():
        if row.mode == mode_id:
            return row
    return None


def isochrone_mode_labels():
    """Display label per isochrone mode."""
    labels = {ISOCHRONE_UNIFORM_MODE: "Uniform"}
    for row in isochrone_metrics():
        labels[row.mode] = row.mode_label or row.label
    return labels


def _token(value):
    return value.strip().lower()


def _number(value):
    """A finite numeric value, excluding booleans and non-numeric text."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _band(row, value):
    number = _number(value)
    if number is None:
        return None
    for limit, colour, label in row.bands:
        if limit is None or number < limit:
            return colour, label
    return None


def _category(row, value):
    if not isinstance(value, str) or not value.strip():
        return None
    token = _token(value)
    for key, colour, label in row.categories:
        if token == key:
            return colour, label
    # Recorded readouts vary ("port tack", "motor-sail"): a prefix match keeps
    # the category while an unrecognised value stays unknown.
    for key, colour, label in row.categories:
        if token.startswith(key):
            return colour, label
    return None


def _recorded_order(row, values):
    """Distinct known values in first-appearance order, keyed by token."""
    order = []
    seen = set()
    for value in values or ():
        if not isinstance(value, str) or not value.strip():
            continue
        token = _token(value)
        if token in seen:
            continue
        seen.add(token)
        order.append((token, value.strip()))
    return order


def colour_map(metric_id, values):
    """Map each recorded value's token to the colour it is drawn with.

    Fixed categories resolve by name. A metric with a ``cycle`` palette assigns
    colours in first-appearance order, which is stable across frames and after
    a JSON roundtrip because leg order is recorded. Unrecognised values are
    absent from the map and resolve to the unknown colour.
    """
    row = METRICS.get(metric_id)
    if row is None:
        return {}
    if row.cycle:
        mapping = {}
        index = 0
        for token, _value in _recorded_order(row, values):
            matched = _category(row, token)
            if matched is not None:
                mapping[token] = matched[0]
                continue
            mapping[token] = row.cycle[index % len(row.cycle)]
            index += 1
        return mapping
    mapping = {}
    for token, _value in _recorded_order(row, values):
        matched = _category(row, token)
        if matched is not None:
            mapping[token] = matched[0]
    return mapping


def colour_for(metric_id, value, values=()):
    """Resolve one value to ``(r, g, b, a)``; missing values stay unknown."""
    row = METRICS.get(metric_id)
    if row is None:
        return ROUTE_UNKNOWN_COLOUR
    if row.gradient:
        return tuple(gradient_colour(metric_id, value)) + (0.9,)
    if row.bands:
        matched = _band(row, value)
        return row.unknown if matched is None else matched[0]
    if isinstance(value, str) and value.strip():
        mapping = colour_map(metric_id, values)
        colour = mapping.get(_token(value))
        if colour is not None:
            return colour
        matched = _category(row, value)
        return row.unknown if matched is None else matched[0]
    return row.unknown


def label_for(metric_id, value):
    """Human label for one value, or ``"Unknown"`` when it has no bucket."""
    row = METRICS.get(metric_id)
    if row is None:
        return "Unknown"
    if row.bands:
        matched = _band(row, value)
        return "Unknown" if matched is None else matched[1]
    if isinstance(value, str) and value.strip():
        matched = _category(row, value)
        if matched is not None:
            return matched[1]
        # A recorded identity such as a sail plan is its own label.
        if row.cycle:
            return value.strip()
    return "Unknown"


def legend_for(metric_id, values=(), limit=None):
    """Ordered ``(label, rgba)`` legend entries for a metric.

    Continuous metrics list every band. Categorical metrics list the values
    present in the route in first-appearance order, compacted to ``limit`` rows
    with a ``"… N more"`` row when the recorded list is longer.
    """
    row = METRICS.get(metric_id)
    if row is None:
        return ()
    if row.gradient:
        return ()
    entries = []
    if row.bands:
        entries = [(label, colour) for _limit, colour, label in row.bands]
    elif values:
        mapping = colour_map(metric_id, values)
        for token, value in _recorded_order(row, values):
            colour = mapping.get(token)
            if colour is None:
                continue
            matched = _category(row, value)
            label = matched[1] if matched is not None else value
            entries.append((label, colour))
    else:
        entries = [(label, colour) for _key, colour, label in row.categories]
    if row.legend_unknown:
        entries.append(("Unknown", row.unknown))
    cap = row.legend_limit if limit is None else limit
    if cap and len(entries) > cap:
        omitted = len(entries) - (cap - 1)
        entries = entries[: cap - 1] + [(f"… {omitted} more", row.unknown)]
    return tuple(entries)


def gradient_colour(metric_id, position):
    """Sample a gradient metric at ``position``; 0 is oldest, 1 is newest."""
    row = METRICS.get(metric_id)
    if row is None or not row.gradient:
        return list(ROUTE_UNKNOWN_COLOUR[:3])
    position = min(1.0, max(0.0, float(position)))
    stops = row.gradient
    scaled = position * (len(stops) - 1)
    index = min(int(scaled), len(stops) - 2)
    fraction = scaled - index
    first, second = stops[index], stops[index + 1]
    return [
        round(first[channel] + (second[channel] - first[channel]) * fraction, 4)
        for channel in range(3)
    ]
