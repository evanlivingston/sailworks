"""Closed isochrone hulls derived from the routing contour points.

The solver expands every heading, so one time step arrives as a set of
contours radiating from a common origin. Drawn as separate polylines that
reads as a starburst: every arm, including the ones heading away from the
goal, is visible. This module derives one closed ring per spatial cluster from
the same points instead. Interior arms fall inside the hull and disappear, so
only the reachable envelope is painted, which is the look LuckGrib and QtVLM
present.

Everything here is pure geometry: no Qt, no payload, no routing calculation.
The rendering payload and the painter live in ``routingoverlay``.
"""

from __future__ import annotations

from math import cos, floor, radians
from typing import Dict, Iterable, List, Sequence, Tuple

Point = Tuple[float, float]

# Clusters are built on a grid whose cell is this multiple of the median
# consecutive point spacing. Arms radiating from a shared origin are joined by
# adjacent cells, so a starburst stays one hull. Patches separated by more
# than this many cell widths (open water, another island) stay apart instead of
# spanning water nobody can reach. Too small a factor splits a single front
# into strips; too large bridges unrelated water.
CLUSTER_CELL_FACTOR = 3.0

# A cell never shrinks below this many degrees. Dense sampling (a few hundred
# metres between points) would otherwise produce thousands of cells for one
# front without changing the clusters.
MIN_CLUSTER_CELL_DEGREES = 0.02

# A hull needs three points to enclose anything; two points are a degenerate
# sliver. Clusters below this count are dropped, which also discards isolated
# solver origins that produced no measurable expansion.
MIN_HULL_POINTS = 3

# A ring spanning more than half the world is not a real envelope: it is what
# a point sequence straddling the antimeridian produces when longitudes jump
# from +179 to -179. Such a hull is dropped instead of smeared across the
# chart. Antimeridian-aware splitting is deliberately not implemented, so a
# cluster that really crosses the dateline shows no hull and keeps its branch
# lines.
MAX_HULL_LONGITUDE_SPAN = 180.0


def _coords(point) -> Point:
    """Normalise one point to an ``(lat, lon)`` tuple.

    Contours reach this module as plain tuples, but the routing record's
    ``Point`` is a frozen dataclass with ``lat``/``lon`` attributes and is not
    iterable. Accepting both keeps the geometry helpers usable from either
    boundary instead of requiring the caller to convert first.
    """
    lat = getattr(point, "lat", None)
    lon = getattr(point, "lon", None)
    if lat is not None and lon is not None:
        return (float(lat), float(lon))
    return (float(point[0]), float(point[1]))


def _unique(points: Iterable[Sequence[float]]) -> List[Point]:
    seen = set()
    unique = []
    for point in points:
        key = _coords(point)
        if key in seen:
            continue
        seen.add(key)
        unique.append(key)
    return unique


def _median_spacing(points: Sequence[Point]) -> float:
    """Median gap between latitude-sorted neighbours, an O(n log n) proxy.

    Exact nearest-neighbour spacing is quadratic and the hull only needs an
    order of magnitude, so consecutive gaps in the sorted point list are used
    instead. Along a contour that neighbours its own samples; across separate
    arms it is the gap between arms, exactly the scale that must not merge.
    """
    if len(points) < 2:
        return 0.0
    ordered = sorted(points, key=lambda point: (point[0], point[1]))
    gaps = []
    for first, second in zip(ordered, ordered[1:]):
        gap = max(abs(first[0] - second[0]), abs(first[1] - second[1]))
        if gap > 0.0:
            gaps.append(gap)
    if not gaps:
        return 0.0
    gaps.sort()
    return gaps[len(gaps) // 2]


def cluster_cell(points: Iterable[Sequence[float]]) -> float:
    """Cluster cell size in degrees for one layer's point set."""
    unique = _unique(points)
    spacing = _median_spacing(unique)
    if spacing <= 0.0:
        return MIN_CLUSTER_CELL_DEGREES
    return max(CLUSTER_CELL_FACTOR * spacing, MIN_CLUSTER_CELL_DEGREES)


def cluster_points(
    points: Iterable[Sequence[float]], cell: float | None = None
) -> List[List[Point]]:
    """Split points into spatially separate clusters using an 8-cell grid.

    Two occupied cells belong to the same cluster when they touch, including
    diagonally. Grid neighbours are joined transitively, so a chain of
    adjacent cells is one cluster rather than a per-cell split. ``cell`` may be
    supplied by a caller that wants a specific scale; otherwise it is derived
    from the point spacing by :func:`cluster_cell`.
    """
    unique = _unique(points)
    if not unique:
        return []
    if cell is None or cell <= 0.0:
        cell = cluster_cell(unique)
    cell_index: Dict[Tuple[int, int], int] = {}
    cells: List[Tuple[int, int]] = []
    members: List[List[Point]] = []
    for point in unique:
        key = (int(floor(point[0] / cell)), int(floor(point[1] / cell)))
        index = cell_index.get(key)
        if index is None:
            index = len(cells)
            cell_index[key] = index
            cells.append(key)
            members.append([])
        members[index].append(point)
    parent = list(range(len(cells)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parent[second_root] = first_root

    neighbours = (
        (-1, -1),
        (-1, 0),
        (-1, 1),
        (0, -1),
        (0, 1),
        (1, -1),
        (1, 0),
        (1, 1),
    )
    for (row, column), index in cell_index.items():
        for row_step, column_step in neighbours:
            other = cell_index.get((row + row_step, column + column_step))
            if other is not None:
                union(index, other)
    groups: Dict[int, List[Point]] = {}
    for index, members_of_cell in enumerate(members):
        groups.setdefault(find(index), []).extend(members_of_cell)
    return [groups[root] for root in sorted(groups)]


def _cross(origin: Point, first: Point, second: Point) -> float:
    """Cross product in x=longitude, y=latitude coordinates."""
    return (first[1] - origin[1]) * (second[0] - origin[0]) - (first[0] - origin[0]) * (
        second[1] - origin[1]
    )


def convex_hull(points: Iterable[Sequence[float]]) -> Tuple[Point, ...]:
    """Monotone-chain convex hull as a counter-clockwise ring of (lat, lon).

    Returns an empty tuple for fewer than three distinct points and for
    collinear input: neither encloses an area, and a degenerate ring would
    paint a zero-width sliver. No point is invented, so a hull never covers
    more than the convex envelope of the points the engine recorded.
    """
    unique = sorted(_unique(points), key=lambda point: (point[1], point[0]))
    if len(unique) < 3:
        return ()

    def build(sequence: Sequence[Point]) -> List[Point]:
        ring: List[Point] = []
        for point in sequence:
            while len(ring) >= 2 and _cross(ring[-2], ring[-1], point) <= 0.0:
                ring.pop()
            ring.append(point)
        return ring

    lower = build(unique)
    upper = build(list(reversed(unique)))
    ring = lower[:-1] + upper[:-1]
    if len(ring) < 3:
        return ()
    return tuple(ring)


def hull_area(ring: Iterable[Sequence[float]]) -> float:
    """Shoelace area in projected degrees, used only to order the shapes.

    Longitude is scaled by cos(mean latitude) so a degree of longitude shrinks
    with latitude like it does on the chart. The value is never displayed: it
    exists so the largest hull is painted first and each smaller one covers its
    interior, leaving the annular bands between layers.
    """
    points = [_coords(point) for point in ring]
    if len(points) < 3:
        return 0.0
    mean_latitude = sum(point[0] for point in points) / len(points)
    scale = max(cos(radians(mean_latitude)), 1e-6)
    total = 0.0
    for index, (lat1, lon1) in enumerate(points):
        lat2, lon2 = points[(index + 1) % len(points)]
        total += (lon1 * scale) * lat2 - (lon2 * scale) * lat1
    return abs(total) / 2.0


def _longitude_span(ring: Sequence[Point]) -> float:
    longitudes = [_coords(point)[1] for point in ring]
    return max(longitudes) - min(longitudes)


def layer_hulls(
    points: Iterable[Sequence[float]],
    cell: float | None = None,
    min_points: int = MIN_HULL_POINTS,
) -> Tuple[Tuple[Point, ...], ...]:
    """Cluster one layer's contour points and hull each usable cluster.

    Clusters below ``min_points``, collinear clusters (no enclosing ring) and
    rings wider than :data:`MAX_HULL_LONGITUDE_SPAN` are dropped. The result is
    unordered; the payload sorts by :func:`hull_area`.
    """
    hulls = []
    for cluster in cluster_points(points, cell):
        if len(cluster) < min_points:
            continue
        ring = convex_hull(cluster)
        if not ring:
            continue
        if _longitude_span(ring) > MAX_HULL_LONGITUDE_SPAN:
            continue
        hulls.append(ring)
    return tuple(hulls)
