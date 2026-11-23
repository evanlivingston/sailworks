"""Band-strip geometry between consecutive isochrone layers.

A closed contour can enclose its interior, but engine contours are often open
or land-truncated, so closed-contour fill leaves whole results empty. A band
strip instead joins the matching contours of two consecutive layers: the area
reachable between those frontiers, drawn as one ring per contour pair and
independent of the ``closed`` flag.

Everything here is plain tuples of ``(latitude, longitude)`` points. The payload
and its colours are assembled by :mod:`sailworks.rendering.routingoverlay`; this
module only answers "which ring, and which contours pair".

Two consecutive layers do not always describe the same front: a dense starburst
of candidate arms near the route start can give way to a single long arc, and
pairing those resamples one frontier onto an unrelated one, drawing wedges of
translucent fill across open water. Pairing is therefore gated (see
``ISOCHRONE_BAND_MAX_ARC_RATIO`` and neighbours in :mod:`.palettes`), and a
band is assembled quad by quad so a local gap breaks the strip into pieces
instead of stretching one quad across it.
"""

from __future__ import annotations

from math import floor, hypot
from statistics import median

from .daynight import day_night_colour
from .palettes import (
    ISOCHRONE_BAND_MAX_ARC_RATIO,
    ISOCHRONE_BAND_MAX_BANDS,
    ISOCHRONE_BAND_MAX_EDGE_FACTOR,
    ISOCHRONE_BAND_MAX_OFFSET_FACTOR,
    ISOCHRONE_BAND_MAX_POINTS,
    ISOCHRONE_BAND_MIN_QUAD_FRACTION,
    ISOCHRONE_BAND_MIN_SPACING,
    ISOCHRONE_BAND_SAMPLES,
    ISOCHRONE_FILL_ALPHA,
    ISOCHRONE_UNIFORM_COLOUR,
)


def _as_point(point) -> tuple:
    return (float(point[0]), float(point[1]))


def _centroid(points) -> tuple:
    count = len(points)
    return (
        sum(point[0] for point in points) / count,
        sum(point[1] for point in points) / count,
    )


def _distance(a, b) -> float:
    return hypot(a[0] - b[0], a[1] - b[1])


def _focus(points, samples: int):
    """Centroid, bbox and step bound of one frontier.

    The centroid and the arc use the same arithmetic as the all-pairs scan, so
    a retained candidate carries exactly the distance the greedy sort expects,
    and the bbox supplies the extent bound used to place the search grid. The
    step bound is the largest ``_step`` any pair containing this contour could
    report: a median segment never exceeds the mean, which is
    ``arc / (count - 1)``, and the resampled term is ``arc / (samples - 1)``.
    """
    count = len(points)
    min_lat = max_lat = points[0][0]
    min_lon = max_lon = points[0][1]
    for point in points:
        lat, lon = point[0], point[1]
        if lat < min_lat:
            min_lat = lat
        elif lat > max_lat:
            max_lat = lat
        if lon < min_lon:
            min_lon = lon
        elif lon > max_lon:
            max_lon = lon
    arc = _arc_length(points)
    bound = ISOCHRONE_BAND_MIN_SPACING
    if count > 1:
        bound = max(bound, arc / (count - 1))
    if samples > 1:
        bound = max(bound, arc / (samples - 1))
    return (_centroid(points), (min_lat, min_lon, max_lat, max_lon), bound)


def _segments(points) -> list:
    return [_distance(first, second) for first, second in zip(points, points[1:])]


def _arc_length(points) -> float:
    return sum(_segments(points))


def _bbox(points) -> tuple:
    """``(min_lat, min_lon, max_lat, max_lon)`` of a frontier."""
    lats = [point[0] for point in points]
    lons = [point[1] for point in points]
    return (min(lats), min(lons), max(lats), max(lons))


def _boxes_meet(first, second, margin: float) -> bool:
    """Whether two boxes overlap once each is grown by ``margin``.

    The margin is one typical frontier step: two nested fronts always overlap,
    and a degenerate line-shaped contour (zero height) still meets the front it
    bounds instead of being rejected for having no area of its own.
    """
    return (
        first[0] - margin <= second[2]
        and second[0] - margin <= first[2]
        and first[1] - margin <= second[3]
        and second[1] - margin <= first[3]
    )


def _stats(points):
    """Cheap per-contour summary: bbox, arc length and median input step.

    Computed once per contour so the pairing loop can reject a bad candidate
    without resampling either frontier.
    """
    return (_bbox(points), _arc_length(points), median(_segments(points) or [0.0]))


def _step(prev_stats, next_stats, samples: int) -> float:
    """The pair's typical step: the coarser of input spacing and resampled step.

    A densely digitised contour has tiny input segments, a coarsely digitised
    one has tiny resampled segments, so the larger of the two is the stable
    yardstick for "how far may a corresponding point move".
    """
    steps = [prev_stats[2], next_stats[2]]
    if samples > 1:
        steps.append(prev_stats[1] / (samples - 1))
        steps.append(next_stats[1] / (samples - 1))
    largest = max((step for step in steps if step > 0.0), default=0.0)
    return max(largest, ISOCHRONE_BAND_MIN_SPACING)


def _pair_gate(prev_stats, next_stats, samples: int):
    """Cheap correspondence checks; return the shared step, or ``None``.

    Three independent ways for a candidate pair to be the wrong front: one
    frontier is much longer than the other, their extents do not meet, or the
    pair has no arc at all. The expensive point-by-point check follows only
    when these pass, so a dense starburst does not pay for resampling every
    combination. Longitudes are compared linearly: a frontier that crosses the
    antimeridian looks like a jump here and is skipped rather than drawn wrong.
    """
    prev_span, next_span = prev_stats[1], next_stats[1]
    if prev_span <= 0.0 or next_span <= 0.0:
        return None
    ratio = prev_span / next_span
    if (
        ratio > ISOCHRONE_BAND_MAX_ARC_RATIO
        or ratio < 1.0 / ISOCHRONE_BAND_MAX_ARC_RATIO
    ):
        return None
    step = _step(prev_stats, next_stats, samples)
    if not _boxes_meet(prev_stats[0], next_stats[0], step):
        return None
    return step


def _matching_frontiers(prev_points, next_points, samples: int, step: float):
    """Resample and align a pair, or ``None`` when the points do not correspond.

    The median corresponding-point distance is the shape check that arc length
    and extent cannot make: two equally long, overlapping frontiers can still
    be different fronts, and their aligned points then sit far apart. Distance
    is measured after alignment, so a reversed trace is not a mismatch.
    """
    first = resample(prev_points, samples)
    second = resample(next_points, samples)
    if len(first) < 2 or len(second) < 2:
        return None
    second = align_directions(first, second)
    offsets = [_distance(a, b) for a, b in zip(first, second)]
    if median(offsets) > step * ISOCHRONE_BAND_MAX_OFFSET_FACTOR:
        return None
    return (first, second, step)


def resample(points, count: int):
    """Arc-length resample ``points`` to exactly ``count`` points.

    Interpolation is linear in latitude/longitude, which is what the chart
    projection draws anyway. The first and last points are preserved exactly so
    both band edges share their endpoints and the ring closes. Fewer than two
    input points cannot define an arc, so they yield an empty list.
    """
    source = [_as_point(point) for point in points]
    count = int(count)
    if len(source) < 2 or count < 1:
        return []
    if count == 1:
        return [source[0]]
    lengths = [0.0]
    for first, second in zip(source, source[1:]):
        lengths.append(lengths[-1] + hypot(second[0] - first[0], second[1] - first[1]))
    total = lengths[-1]
    if total <= 0.0:
        return [source[0]] * count

    result = []
    segment = 0
    step = total / (count - 1)
    for index in range(count):
        target = step * index
        while segment < len(lengths) - 2 and lengths[segment + 1] < target:
            segment += 1
        span = lengths[segment + 1] - lengths[segment]
        ratio = 0.0 if span <= 0.0 else (target - lengths[segment]) / span
        ratio = min(max(ratio, 0.0), 1.0)
        first, second = source[segment], source[segment + 1]
        result.append(
            (
                first[0] + (second[0] - first[0]) * ratio,
                first[1] + (second[1] - first[1]) * ratio,
            )
        )
    result[0] = source[0]
    result[-1] = source[-1]
    return result


def _end_to_end(a, b) -> float:
    return hypot(a[0][0] - b[0][0], a[0][1] - b[0][1]) + hypot(
        a[-1][0] - b[-1][0], a[-1][1] - b[-1][1]
    )


def align_directions(a, b):
    """Reverse ``b`` when that makes its ends meet ``a``'s ends.

    Two contours of the same layer pair can be traced in opposite directions.
    Walking both forward would braid the band into a bow tie, so ``b`` is
    reversed whenever its end-to-end distance with ``a`` is smaller reversed.
    """
    first = list(a)
    second = list(b)
    if len(first) < 2 or len(second) < 2:
        return second
    if _end_to_end(first, second) <= _end_to_end(first, list(reversed(second))):
        return second
    return list(reversed(second))


def band_polygon(a, b):
    """One closed ring walking ``a`` forward and ``b`` backward.

    For aligned contours with a monotone point correspondence this traces a
    single quad strip and does not self-intersect. The first point is repeated
    at the end so the ring is closed even for consumers that do not close paths.
    """
    first = list(a)
    second = list(b)
    if len(first) < 2 or len(second) < 2:
        return []
    return _run_ring(first, second, 0, min(len(first), len(second)) - 2)


def _run_ring(first, second, start: int, end: int) -> list:
    """Close the quads from ``start`` to ``end`` (inclusive) into one ring."""
    ring = list(first[start : end + 2]) + list(reversed(second[start : end + 2]))
    ring.append(ring[0])
    return ring


def band_polygons(a, b, spacing=None) -> list:
    """Closed rings for the contiguous runs of acceptable quads.

    Two aligned frontiers are joined quad by quad. A quad is accepted only when
    all four of its edges are at most
    ``ISOCHRONE_BAND_MAX_EDGE_FACTOR`` typical steps long: a narrow well-matched
    strip passes, while a spike or a detour in one frontier lengthens the cross
    edges and fails. Accepted quads are then grouped into runs, so a gap in the
    middle yields two polygons instead of one ring spanning the gap.

    A pair whose run would cover fewer than ``ISOCHRONE_BAND_MIN_QUAD_FRACTION``
    of its quads keeps nothing: past that point the two frontiers are not the
    same front, and fragments of a mis-match read as noise rather than fill.
    """
    first = list(a)
    second = list(b)
    count = min(len(first), len(second))
    if count < 2:
        return []
    first = first[:count]
    second = second[:count]
    if spacing is None:
        spacing = _step(_stats(first), _stats(second), count)
    limit = spacing * ISOCHRONE_BAND_MAX_EDGE_FACTOR
    fits = []
    for index in range(count - 1):
        fits.append(
            _distance(first[index], first[index + 1]) <= limit
            and _distance(second[index], second[index + 1]) <= limit
            and _distance(first[index], second[index]) <= limit
            and _distance(first[index + 1], second[index + 1]) <= limit
        )
    if fits.count(True) < ISOCHRONE_BAND_MIN_QUAD_FRACTION * len(fits):
        return []
    rings = []
    start = None
    for index, fits_here in enumerate(fits + [False]):
        if fits_here and start is None:
            start = index
        elif not fits_here and start is not None:
            rings.append(_run_ring(first, second, start, index - 1))
            start = None
    return rings


def _paired_contours(prev, nxt, samples: int):
    """Greedy one-to-one pairing behind the correspondence gate.

    Every candidate pair is sorted by centroid distance, then taken when
    neither index is used and both gate checks pass, so the result is
    one-to-one and as near as greedy allows. Leftover contours on the larger
    side are skipped: a band needs both edges, and guessing a correspondence
    would draw a strip across unrelated water. ``skipped`` counts the matches
    the gate refused, so a fully rejected layer pair counts once per available
    frontier rather than once per rejected combination.

    Candidates come from a spatial grid rather than every combination: a layer
    pair can hold hundreds of frontiers, and only pairs whose boxes meet can
    pass the gate. Each previous contour searches the cells around a radius
    that any gate-passing partner must fall inside, or every partner when that
    rectangle holds more cells than there are next contours. The radius uses
    the largest extent on each side plus an upper bound on the gate's margin,
    so it never drops a pair the gate would accept; because the greedy loop
    only ever sees a superset of the acceptable pairs in the same sorted order,
    the result is identical to the all-pairs scan.

    Returns ``(pairs, skipped)`` where each pair is
    ``(prev_index, next_index, first, second, step)`` with both frontiers
    already resampled and aligned, so the caller does not repeat that work.
    """
    usable_prev = [
        (index, _focus(points, samples)) for index, points in enumerate(prev) if points
    ]
    usable_next = [
        (index, _focus(points, samples)) for index, points in enumerate(nxt) if points
    ]
    if not usable_prev or not usable_next:
        return [], 0
    step_limit = max(focus[2] for _index, focus in usable_prev + usable_next)

    # Largest centroid-to-box extent on the next side, per direction. A pair
    # within margin m has |centroid delta| <= max(u_p + v_n, u_n + v_p) + m, so
    # these maxima turn the per-pair bound into a per-prev search radius.
    north_next = south_next = east_next = west_next = 0.0
    for _index, (centroid, box, _bound) in usable_next:
        north_next = max(north_next, box[2] - centroid[0])
        south_next = max(south_next, centroid[0] - box[0])
        east_next = max(east_next, box[3] - centroid[1])
        west_next = max(west_next, centroid[1] - box[1])

    sizes = [
        max(box[2] - box[0], box[3] - box[1])
        for _index, (_centroid, box, _bound) in usable_prev + usable_next
    ]
    # Cells near the typical contour size keep a search to the same and adjacent
    # cells; the floor stops a layer of degenerate contours from making cells
    # smaller than one gate step.
    cell = max(median(sizes), 2.0 * step_limit, ISOCHRONE_BAND_MIN_SPACING)
    buckets = {}
    for next_index, (centroid, _box, _bound) in usable_next:
        key = (floor(centroid[0] / cell), floor(centroid[1] / cell))
        buckets.setdefault(key, []).append((next_index, centroid))

    candidates = []
    for prev_index, (centroid, box, _bound) in usable_prev:
        lat, lon = centroid
        lat_radius = max(box[2] - lat + south_next, north_next + lat - box[0])
        lon_radius = max(box[3] - lon + west_next, east_next + lon - box[1])
        # A tiny relative pad keeps a float on the bound inside the rectangle.
        lat_radius = (lat_radius + step_limit) * (1.0 + 1e-9) + 1e-12
        lon_radius = (lon_radius + step_limit) * (1.0 + 1e-9) + 1e-12
        lat_low = floor((lat - lat_radius) / cell)
        lat_high = floor((lat + lat_radius) / cell)
        lon_low = floor((lon - lon_radius) / cell)
        lon_high = floor((lon + lon_radius) / cell)
        if (lat_high - lat_low + 1) * (lon_high - lon_low + 1) > len(usable_next):
            pool = ((index, focus[0]) for index, focus in usable_next)
        else:
            # Each centroid lives in exactly one cell, so the union has no repeats.
            pool = []
            for lat_cell in range(lat_low, lat_high + 1):
                for lon_cell in range(lon_low, lon_high + 1):
                    for next_index, next_centroid in buckets.get(
                        (lat_cell, lon_cell), ()
                    ):
                        pool.append((next_index, next_centroid))
        for next_index, (next_lat, next_lon) in pool:
            candidates.append(
                (hypot(lat - next_lat, lon - next_lon), prev_index, next_index)
            )
    candidates.sort()
    prev_stats = {}
    next_stats = {}
    used_prev = set()
    used_next = set()
    pairs = []
    for _distance, prev_index, next_index in candidates:
        if prev_index in used_prev or next_index in used_next:
            continue
        if prev_index not in prev_stats:
            prev_stats[prev_index] = _stats(prev[prev_index])
        if next_index not in next_stats:
            next_stats[next_index] = _stats(nxt[next_index])
        step = _pair_gate(prev_stats[prev_index], next_stats[next_index], samples)
        if step is None:
            continue
        frontiers = _matching_frontiers(
            prev[prev_index], nxt[next_index], samples, step
        )
        if frontiers is None:
            continue
        used_prev.add(prev_index)
        used_next.add(next_index)
        pairs.append((prev_index, next_index) + frontiers)
    pairs.sort()
    skipped = max(0, min(len(usable_prev), len(usable_next)) - len(pairs))
    return pairs, skipped


def pair_contours(prev, nxt, samples: int = ISOCHRONE_BAND_SAMPLES):
    """Greedy one-to-one pairing by centroid distance, gated by correspondence.

    Indices are returned as ``(prev_index, next_index)`` pairs. A candidate is
    refused when the two frontiers do not look like the same front: very
    different arc lengths, disjoint extents, or aligned points that sit far
    apart relative to the frontier's typical step. A refused pair leaves both
    contours free for a better match later in the sorted candidate list.
    """
    pairs, _skipped = _paired_contours(prev, nxt, max(int(samples), 2))
    return [(prev_index, next_index) for prev_index, next_index, *_rest in pairs]


def build_bands(
    layers,
    count: int = ISOCHRONE_BAND_SAMPLES,
    max_bands: int = ISOCHRONE_BAND_MAX_BANDS,
    max_points: int = ISOCHRONE_BAND_MAX_POINTS,
) -> dict:
    """Bands between every consecutive pair in ``layers``.

    ``layers`` is a sequence of mappings with ``layer_id``, ``epoch_s`` and
    ``contours`` (a sequence of point sequences). Each band carries both layer
    identities so the caller can colour it from the layer palette, and the
    later layer's epoch so its band is keyed to the frontier that bounds it.

    The caps bound the frame cost on a dense result. The first band is always
    kept even if it alone exceeds the point budget: an empty fill after a user
    asked for one is a worse failure than one heavy ring. ``capped`` reports
    whether the caps refused any ring, and ``capped_rings`` counts those rings,
    so the caller can disclose what was left out without mistaking a pair for
    its rings. ``rings`` counts every ring the pairs produced, including the
    ones the caps refused, and ``paired`` counts the accepted pairs. ``skipped``
    counts contour pairs the correspondence gate refused, or dropped because
    too few quads survived, so a missing band is disclosed rather than silent.

    A pair can yield more than one band when a gap or spike splits its strip;
    each run is its own ring with the same layer identities, so one pair may
    produce several bands and the cap is applied per ring, not per pair.
    """
    samples = max(int(count), 2)
    ring_size = 2 * samples + 1
    bands = []
    paired = 0
    skipped = 0
    rings_made = 0
    capped_rings = 0
    used_points = 0
    previous = None
    for layer in layers:
        contours = [points for points in layer.get("contours", ()) if len(points) >= 2]
        if previous is not None:
            prev_contours = [
                points for points in previous.get("contours", ()) if len(points) >= 2
            ]
            pairs, refused = _paired_contours(prev_contours, contours, samples)
            skipped += refused
            for prev_index, next_index, first, second, step in pairs:
                paired += 1
                rings = band_polygons(first, second, step)
                if not rings:
                    skipped += 1
                    continue
                for ring in rings:
                    rings_made += 1
                    if len(bands) >= max_bands or (
                        bands and used_points + ring_size > max_points
                    ):
                        capped_rings += 1
                        continue
                    bands.append(
                        {
                            "points": ring,
                            "layer_id": layer["layer_id"],
                            "prev_layer_id": previous["layer_id"],
                            "epoch_s": layer.get("epoch_s"),
                            "prev_epoch_s": previous.get("epoch_s"),
                            "samples": samples,
                        }
                    )
                    used_points += len(ring)
        previous = layer
    return {
        "bands": bands,
        "paired": paired,
        "rings": rings_made,
        "capped_rings": capped_rings,
        "skipped": skipped,
        "capped": capped_rings > 0,
    }


def band_colour(band, mode: str, layer_colours) -> list:
    """Registry RGBA at fill alpha for one band under the active mode.

    A band belongs to the later of its two layers, so uniform uses the mode's
    base colour, time uses that layer's gradient colour, and day/night samples
    the band's midpoint in time at its centroid. The centroid is the area the
    band actually covers; a band can straddle a terminator, and one fill cannot
    show both sides.
    """
    if mode == "time":
        base = layer_colours.get(band.get("layer_id"))
        if base is None:
            return []
        rgb = list(base)[:3]
    elif mode == "day_night":
        points = band.get("points") or []
        if not points:
            return []
        latitude, longitude = _centroid(points)
        epoch = band.get("epoch_s")
        previous = band.get("prev_epoch_s")
        if epoch is not None and previous is not None:
            epoch = (int(epoch) + int(previous)) / 2.0
        rgb = list(day_night_colour(epoch, (latitude, longitude)))[:3]
    else:
        rgb = list(ISOCHRONE_UNIFORM_COLOUR)[:3]
    return rgb + [ISOCHRONE_FILL_ALPHA]
