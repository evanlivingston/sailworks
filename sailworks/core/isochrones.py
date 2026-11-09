"""Immutable isochrone contour sidecars and sampled-envelope construction.

The routing frontier is deliberately pruned: it keeps the best candidate per
bearing bin.  That makes adjacent frontier points unrelated lateral samples, so
a renderer cannot reconstruct a truthful boundary from them after the fact.

This module keeps the extra evidence while candidates are still local to one
time slice: each accepted sample remembers its parent frontier point and its
heading ordinal. Adjacent accepted samples at the same time form observed
arcs. Local fan cells can occlude arcs, but their union cannot add new contour
edges. The exposed arcs are a sampled search envelope, not a claim about the
complete physically reachable set.

The data is immutable and JSON-serialisable.  A missing sidecar means a legacy
route, and an unknown sidecar version is reported as unavailable rather than
preventing the route from loading.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass

import numpy as np

SAMPLED_ENVELOPE = "sampled-envelope"
LEGACY_FRONTIER = "legacy-frontier"
UNAVAILABLE = "unavailable"

CONTOURS_VERSION = 4

_EARTH_RADIUS_M = 6_371_008.8


def contours_enabled():
    """Contour capture is on by default and never changes route selection."""
    return os.environ.get("SAILWORKS_ISOCHRONE_CONTOURS", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _position(value):
    """Return a finite ``(latitude, longitude)`` pair from a point or tuple."""
    if hasattr(value, "pos"):
        value = value.pos
    latitude = float(value[0])
    longitude = float(value[1])
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        raise ValueError("Contour coordinates must be finite")
    return latitude, longitude


def _valid_position(value):
    try:
        return _position(value)
    except (TypeError, ValueError, IndexError):
        return None


def _iso_time(value):
    if value is None:
        return ""
    formatter = getattr(value, "isoformat", None)
    return formatter() if callable(formatter) else str(value)


def _wrap180(value):
    return (float(value) + 180.0) % 360.0 - 180.0


def _circular_mean_longitude(longitudes):
    radians = np.radians(np.asarray(longitudes, dtype=float))
    return math.degrees(
        math.atan2(float(np.sin(radians).mean()), float(np.cos(radians).mean()))
    )


@dataclass(frozen=True)
class CandidateSample:
    """One candidate destination observed before bearing-bin publication."""

    latitude: float
    longitude: float
    parent: int = -1
    heading_index: int = 0
    heading: float = 0.0
    accepted: bool = True

    @property
    def pos(self):
        return self.latitude, self.longitude

    def to_json(self):
        return [
            self.latitude,
            self.longitude,
            int(self.parent),
            int(self.heading_index),
            float(self.heading),
            bool(self.accepted),
        ]

    @staticmethod
    def from_json(value):
        return CandidateSample(
            float(value[0]),
            float(value[1]),
            int(value[2]) if len(value) > 2 else -1,
            int(value[3]) if len(value) > 3 else 0,
            float(value[4]) if len(value) > 4 else 0.0,
            bool(value[5]) if len(value) > 5 else True,
        )


@dataclass(frozen=True)
class ContourRun:
    """One ordered, continuous part of a contour.

    ``sources`` has one entry per point and records the nearest observed
    candidate as ``(parent_index, heading_index)`` when that evidence exists.
    A run never spans an invalid segment or an unsampled heading slot; callers
    therefore render the points in order and do not reconnect runs.
    """

    points: tuple = ()
    closed: bool = False
    sources: tuple = ()

    def __post_init__(self):
        object.__setattr__(
            self,
            "points",
            tuple(_position(point) for point in (self.points or ())),
        )
        object.__setattr__(self, "closed", bool(self.closed))
        object.__setattr__(self, "sources", tuple(self.sources or ()))

    def __bool__(self):
        return bool(self.points)

    def to_json(self):
        return {
            "points": [[latitude, longitude] for latitude, longitude in self.points],
            "closed": self.closed,
            "sources": [
                list(source) if isinstance(source, (tuple, list)) else source
                for source in self.sources
            ],
        }

    @staticmethod
    def from_json(value):
        if not isinstance(value, dict):
            raise ValueError("Contour run must be an object")
        return ContourRun(
            tuple(tuple(point) for point in value.get("points", ())),
            bool(value.get("closed", False)),
            tuple(
                tuple(source) if isinstance(source, (list, tuple)) else source
                for source in value.get("sources", ())
            ),
        )


@dataclass(frozen=True)
class IsochroneSlice:
    """Contours for one routing leg and one time step."""

    leg_id: int = 0
    step: int = 0
    valid_time: str = ""
    runs: tuple = ()
    representation: str = SAMPLED_ENVELOPE
    frontier: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, "leg_id", int(self.leg_id))
        object.__setattr__(self, "step", int(self.step))
        object.__setattr__(self, "valid_time", str(self.valid_time or ""))
        object.__setattr__(
            self,
            "runs",
            tuple(
                run if isinstance(run, ContourRun) else ContourRun(**run)
                for run in (self.runs or ())
            ),
        )
        object.__setattr__(self, "representation", str(self.representation))
        object.__setattr__(
            self,
            "frontier",
            tuple(_position(point) for point in (self.frontier or ())),
        )

    @property
    def time(self):
        return self.valid_time

    def __bool__(self):
        return bool(self.runs)

    def to_json(self):
        return {
            "leg_id": self.leg_id,
            "step": self.step,
            "valid_time": self.valid_time,
            "representation": self.representation,
            "runs": [run.to_json() for run in self.runs],
            "frontier": [
                [latitude, longitude] for latitude, longitude in self.frontier
            ],
        }

    @staticmethod
    def from_json(value):
        if not isinstance(value, dict):
            raise ValueError("Isochrone slice must be an object")
        return IsochroneSlice(
            int(value.get("leg_id", 0)),
            int(value.get("step", 0)),
            str(value.get("valid_time", "")),
            tuple(ContourRun.from_json(run) for run in value.get("runs", ())),
            str(value.get("representation", SAMPLED_ENVELOPE)),
            tuple(tuple(point) for point in value.get("frontier", ())),
        )


@dataclass(frozen=True)
class ContourHit:
    """A hit-test result against exactly the rendered contour edges."""

    slice_index: int
    run_index: int
    segment_index: int
    distance_m: float
    leg_id: int = 0
    step: int = 0
    valid_time: str = ""

    @property
    def slice_key(self):
        return self.leg_id, self.step


@dataclass(frozen=True)
class IsochroneContours:
    """Versioned sidecar carried with a calculated route."""

    slices: tuple = ()
    version: int = CONTOURS_VERSION
    representation: str = SAMPLED_ENVELOPE
    _unsupported_json: str = ""

    def __post_init__(self):
        object.__setattr__(
            self,
            "slices",
            tuple(
                (
                    item
                    if isinstance(item, IsochroneSlice)
                    else IsochroneSlice.from_json(item)
                )
                for item in (self.slices or ())
            ),
        )
        try:
            version = int(self.version)
        except (TypeError, ValueError):
            version = 0
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "representation", str(self.representation))

    def __bool__(self):
        return bool(self.slices)

    def __iter__(self):
        return iter(self.slices)

    def __len__(self):
        return len(self.slices)

    def for_leg(self, leg_id):
        return tuple(item for item in self.slices if item.leg_id == int(leg_id))

    @property
    def active_leg(self):
        """The most recent leg, which is the one the live map displays."""
        if not self.slices:
            return None
        return max(item.leg_id for item in self.slices)

    def active(self):
        leg = self.active_leg
        if leg is None:
            return IsochroneContours((), self.version, self.representation)
        return IsochroneContours(self.for_leg(leg), self.version, self.representation)

    def displayed(self):
        """Keep the geometry already shown while each leg was calculated.

        Collapsing a completed leg to just its last step abruptly replaced its
        previously visible contours with a single, often irregular envelope.
        The saved route should display the same steps as the live preview.
        """
        return self

    @property
    def runs(self):
        return tuple(run for item in self.slices for run in item.runs)

    def to_json(self):
        if self._unsupported_json:
            return json.loads(self._unsupported_json)
        return {
            "version": self.version,
            "representation": self.representation,
            "slices": [item.to_json() for item in self.slices],
        }

    @staticmethod
    def from_json(value):
        """Load a sidecar; unknown versions degrade instead of failing."""
        if value is None:
            return None
        if isinstance(value, IsochroneContours):
            return value
        if not isinstance(value, dict):
            return IsochroneContours((), version=0, representation=UNAVAILABLE)
        try:
            version = int(value.get("version", 0))
        except (TypeError, ValueError):
            version = 0
        if version != CONTOURS_VERSION:
            return IsochroneContours(
                (),
                version=version,
                representation=UNAVAILABLE,
                _unsupported_json=json.dumps(value),
            )
        try:
            slices = tuple(
                IsochroneSlice.from_json(item) for item in value.get("slices", ())
            )
        except (TypeError, ValueError, KeyError, IndexError):
            return IsochroneContours((), version=version, representation=UNAVAILABLE)
        return IsochroneContours(
            slices,
            version=version,
            representation=str(value.get("representation", SAMPLED_ENVELOPE)),
        )


def normalize_contours(value):
    """Return an ``IsochroneContours`` sidecar or ``None``."""
    if value is None:
        return None
    if isinstance(value, IsochroneContours):
        return value
    return IsochroneContours.from_json(value)


def legacy_contours(frontiers, times=None):
    """Describe legacy frontiers as point samples, never as invented lines."""
    times = tuple(times or ())
    slices = []
    for index, frontier in enumerate(frontiers or ()):
        points = []
        for point in frontier or ():
            position = _valid_position(point)
            if position is not None:
                points.append(position)
        slices.append(
            IsochroneSlice(
                leg_id=0,
                step=index,
                valid_time=_iso_time(times[index]) if index < len(times) else "",
                runs=tuple(
                    ContourRun((point,), False, ((None, None),)) for point in points
                ),
                representation=LEGACY_FRONTIER,
                frontier=tuple(points),
            )
        )
    return IsochroneContours(tuple(slices), representation=LEGACY_FRONTIER)


def _project(points, reference_latitude, reference_longitude):
    """Project local points to a tangent-plane metric approximation."""
    values = np.asarray(points, dtype=float)
    latitude = np.radians(values[:, 0] - reference_latitude)
    longitude = np.radians(
        np.asarray([_wrap180(value - reference_longitude) for value in values[:, 1]])
    )
    x = longitude * math.cos(math.radians(reference_latitude)) * _EARTH_RADIUS_M
    y = latitude * _EARTH_RADIUS_M
    return np.column_stack((x, y))


def _unproject(values, reference_latitude, reference_longitude):
    values = np.asarray(values, dtype=float)
    longitude_scale = max(abs(math.cos(math.radians(reference_latitude))), 1e-9)
    longitudes = reference_longitude + np.degrees(
        values[:, 0] / (_EARTH_RADIUS_M * longitude_scale)
    )
    latitudes = reference_latitude + np.degrees(values[:, 1] / _EARTH_RADIUS_M)
    return [
        (float(latitude), float(_wrap180(longitude)))
        for latitude, longitude in zip(latitudes, longitudes)
    ]


def _dedupe(points):
    result = []
    for point in points:
        if not result or point != result[-1]:
            result.append(point)
    return result


def _valid_edges(points, closed, lines_validity, line_validity):
    count = len(points)
    last = count if closed else count - 1
    pairs = [
        (points[index], points[(index + 1) % count]) for index in range(max(0, last))
    ]
    if not pairs:
        return []
    if lines_validity is not None:
        try:
            answers = [
                bool(value)
                for value in np.asarray(
                    lines_validity(
                        [[start[0], start[1], end[0], end[1]] for start, end in pairs]
                    ),
                    dtype=bool,
                ).ravel()
            ]
        except Exception:
            answers = [False] * len(pairs)
    else:
        answers = [True] * len(pairs)
    if line_validity is not None:
        for index, (start, end) in enumerate(pairs):
            try:
                answers[index] = answers[index] and bool(
                    line_validity(start[0], start[1], end[0], end[1])
                )
            except Exception:
                answers[index] = False
    return answers


def _split_antimeridian(points, closed):
    """Split runs so consecutive points never imply a world-spanning edge."""
    if len(points) < 2:
        return [(tuple(points), False)]
    runs = []
    current = [points[0]]
    for point in points[1:]:
        raw_jump = abs(point[1] - current[-1][1]) > 180.0
        long_way = abs(_wrap180(point[1] - current[-1][1])) > 180.0
        if raw_jump or long_way:
            if current:
                runs.append((tuple(current), False))
            current = [point]
        else:
            current.append(point)
    if current:
        # A piece of a split ring is never itself a closed ring; closing it
        # would invent an edge. Check the omitted last-to-first edge too.
        keep_closed = (
            bool(closed) and not runs and abs(current[-1][1] - current[0][1]) <= 180.0
        )
        runs.append((tuple(current), keep_closed))
    return runs


def _approx_distance_m(first, second):
    latitude = math.radians((first[0] + second[0]) / 2.0)
    delta_latitude = math.radians(second[0] - first[0])
    delta_longitude = math.radians(_wrap180(second[1] - first[1])) * math.cos(latitude)
    return _EARTH_RADIUS_M * math.hypot(delta_latitude, delta_longitude)


def _ring_on_observed_arcs(coordinates, arcs, arc_index):
    """Trace only boundary edges supported by adjacent same-parent samples.

    Unioning fan cells can subdivide an ancestry spoke at an intersection.
    Checking for parent vertices misses the exposed remainder of that spoke;
    requiring coverage by a sampled candidate arc removes it regardless of
    how the union subdivided it. Missing heading slots cannot become edges.
    """
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    points = list(coordinates)
    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]
    if len(points) < 3:
        return []
    count = len(points)
    supported = []
    for index in range(count):
        start = points[index]
        end = points[(index + 1) % count]
        edge = LineString((start, end))
        nearby = [arcs[i] for i in arc_index.query(edge)] if start != end else []
        supported.append(
            any(arc.covers(edge) for arc in nearby)
            or (len(nearby) > 1 and unary_union(nearby).covers(edge))
        )
    if all(supported):
        return [(points, True)]

    # Start after a break so a continuous arc crossing the ring's first
    # coordinate remains a single run, without joining across any gap.
    first_break = supported.index(False)
    runs = []
    current = []
    for offset in range(1, count + 1):
        index = (first_break + offset) % count
        if supported[index]:
            if not current:
                current = [points[index]]
            current.append(points[(index + 1) % count])
        elif current:
            runs.append((current, False))
            current = []
    return runs


def _ring_runs(points, closed, lines_validity, line_validity):
    points = _dedupe(points)
    if len(points) < 2:
        return [(tuple(points), False)]
    edges = _valid_edges(points, closed, lines_validity, line_validity)
    if closed and edges and all(edges):
        return [(tuple(points), True)]
    if not edges:
        return [(tuple(points), False)]
    if all(edges):
        return [(tuple(points), False)]

    # Treat the ring as a path that closes.  Split it at every invalid or
    # missing edge so a run never bridges a land/barrier/unsampled gap.
    count = len(points)
    sequence = points + [points[0]] if closed else points
    runs = []
    current = []
    total = len(edges)
    for index in range(total):
        if edges[index]:
            if not current:
                current = [sequence[index]]
            current.append(sequence[index + 1])
        else:
            if len(current) > 1 or (len(current) == 1 and not runs):
                runs.append((tuple(_dedupe(current)), False))
            current = []
    if len(current) > 1:
        runs.append((tuple(_dedupe(current)), False))
    filtered = [(run, flag) for run, flag in runs if run]
    if filtered:
        return filtered
    # Every edge is invalid: keep the samples visible without drawing a line
    # across the rejected geometry.
    return [((point,), False) for point in points]


def _sources_for(points, candidates):
    if not candidates:
        return tuple((None, None) for _ in points)
    sources = []
    for latitude, longitude in points:
        best = None
        best_distance = float("inf")
        for candidate in candidates:
            distance = (candidate.latitude - latitude) ** 2 + (
                _wrap180(candidate.longitude - longitude)
            ) ** 2
            if distance < best_distance:
                best_distance = distance
                best = (candidate.parent, candidate.heading_index)
        sources.append(best if best is not None else (None, None))
    return tuple(sources)


def _fan_groups(candidates):
    """Adjacent accepted samples in heading order, split at missing slots."""
    groups = []
    current = []
    previous_parent = None
    previous_slot = None
    for candidate in sorted(
        candidates, key=lambda item: (item.parent, item.heading_index)
    ):
        if not candidate.accepted:
            continue
        split = candidate.parent != previous_parent
        if not split and previous_slot is not None:
            split = candidate.heading_index != previous_slot + 1
        if split:
            if current:
                groups.append(current)
            current = [candidate]
        else:
            current.append(candidate)
        previous_parent = candidate.parent
        previous_slot = candidate.heading_index
    if current:
        groups.append(current)
    return groups


def _polygons(geometry):
    from shapely.geometry import GeometryCollection, MultiPolygon, Polygon

    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    if isinstance(geometry, GeometryCollection):
        return [item for item in geometry.geoms if isinstance(item, Polygon)]
    return []


def _run_from_points(points, closed, candidates, lines_validity, line_validity):
    points = list(points)
    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]
    parts = []
    for part, flag in _split_antimeridian(points, closed):
        parts.extend(_ring_runs(part, flag, lines_validity, line_validity))
    return [
        ContourRun(part, flag, _sources_for(part, candidates))
        for part, flag in parts
        if part
    ]


def _path_between(start, end, ring, edge_midpoint):
    """Choose the hull path between two points that detours farthest in."""
    count = len(ring)
    start_index = min(range(count), key=lambda i: _distance_xy(ring[i], start))
    end_index = min(range(count), key=lambda i: _distance_xy(ring[i], end))
    if start_index == end_index:
        return None

    def walk(first, last):
        if first <= last:
            return ring[first : last + 1]
        return ring[first:] + ring[: last + 1]

    candidates = [walk(start_index, end_index), walk(end_index, start_index)]
    best = None
    for path in candidates:
        middle = path[len(path) // 2]
        distance = _distance_xy(middle, edge_midpoint)
        if best is None or distance > best[0]:
            best = (distance, path)
    return best[1] if best else None


def _distance_xy(first, second):
    return math.hypot(first[0] - second[0], first[1] - second[1])


def _concave_boundary_runs(
    points,
    lines_validity,
    line_validity,
    max_edge_m=5000.0,
    min_spacing_m=200.0,
):
    """LuckGrib-style boundary: convex hull refined by concave long edges."""
    from shapely.geometry import MultiPoint, Polygon

    if len(points) < 3:
        return []
    reference_latitude = float(np.mean([point[0] for point in points]))
    reference_longitude = _circular_mean_longitude([point[1] for point in points])
    projected = _project(points, reference_latitude, reference_longitude)
    local = [tuple(point) for point in projected]
    xs = np.asarray([point[0] for point in local], dtype=float)
    ys = np.asarray([point[1] for point in local], dtype=float)
    hull = MultiPoint(local).convex_hull
    if not isinstance(hull, Polygon):
        return []
    ring = list(hull.exterior.coords)
    if ring and ring[0] == ring[-1]:
        ring = ring[:-1]

    for _ in range(6):
        refined = []
        changed = False
        count = len(ring)
        for index in range(count):
            start = ring[index]
            end = ring[(index + 1) % count]
            length = _distance_xy(start, end)
            if length <= max_edge_m:
                refined.append(start)
                continue
            dx = end[0] - start[0]
            dy = end[1] - start[1]
            denominator = dx * dx + dy * dy
            if denominator == 0:
                refined.append(start)
                continue
            t = ((xs - start[0]) * dx + (ys - start[1]) * dy) / denominator
            projection_x = start[0] + t * dx
            projection_y = start[1] + t * dy
            distance = np.hypot(xs - projection_x, ys - projection_y)
            mask = (t > 0.0) & (t < 1.0) & (distance <= max_edge_m)
            support = list(zip(xs[mask].tolist(), ys[mask].tolist()))
            if len(support) < 2:
                refined.append(start)
                continue
            if len(support) > 400:
                step = int(np.ceil(len(support) / 400))
                support = support[::step]
            try:
                local_hull = MultiPoint([start, end] + support).concave_hull(ratio=0.55)
            except Exception:
                refined.append(start)
                continue
            if not isinstance(local_hull, Polygon) or local_hull.is_empty:
                refined.append(start)
                continue
            local_ring = list(local_hull.exterior.coords)
            if local_ring and local_ring[0] == local_ring[-1]:
                local_ring = local_ring[:-1]
            path = _path_between(
                start,
                end,
                local_ring,
                ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2),
            )
            if not path or len(path) < 2:
                refined.append(start)
                continue
            if _distance_xy(path[0], start) > _distance_xy(path[-1], start):
                path = list(reversed(path))
            refined.extend(path[:-1])
            changed = True
        ring = _dedupe_xy(refined)
        if not changed:
            break

    if len(ring) < 3:
        return []
    filtered = [ring[0]]
    for point in ring[1:]:
        if _distance_xy(point, filtered[-1]) >= min_spacing_m:
            filtered.append(point)
    if len(filtered) > 2 and _distance_xy(filtered[-1], filtered[0]) < min_spacing_m:
        filtered.pop()
    if len(filtered) < 3:
        return []

    coordinates = _unproject(filtered, reference_latitude, reference_longitude)
    runs = []
    for part, flag in _split_antimeridian(coordinates, True):
        for run_points, run_closed in _ring_runs(
            part, flag, lines_validity, line_validity
        ):
            runs.append(ContourRun(run_points, run_closed))
    return runs


def _dedupe_xy(points):
    result = []
    for point in points:
        if not result or point != result[-1]:
            result.append(point)
    return result


def _ordered_frontier_runs(
    frontier_points,
    frontier_bearings,
    accepted,
    lines_validity,
    line_validity,
    gap_m=2200.0,
):
    """Chain the published frontier in bearing order, breaking real gaps."""
    ordered = [
        point
        for _, point in sorted(
            zip(frontier_bearings, frontier_points), key=lambda item: item[0]
        )
    ]
    source_by_position = {
        candidate.pos: (candidate.parent, candidate.heading_index)
        for candidate in accepted
    }
    chains = []
    current = [ordered[0]] if ordered else []
    for first, second in zip(ordered, ordered[1:]):
        if _approx_distance_m(first, second) <= gap_m:
            current.append(second)
        else:
            if current:
                chains.append(current)
            current = [second]
    if current:
        chains.append(current)
    # A singleton is only worth drawing when the whole slice is sparse.
    if any(len(chain) >= 2 for chain in chains):
        chains = [chain for chain in chains if len(chain) >= 2]

    edges = []
    bounds = []
    offset = 0
    for chain in chains:
        for first, second in zip(chain, chain[1:]):
            edges.append([first[0], first[1], second[0], second[1]])
        bounds.append((offset, offset + max(0, len(chain) - 1)))
        offset += max(0, len(chain) - 1)

    if lines_validity is not None and edges:
        try:
            answers = [
                bool(value)
                for value in np.asarray(lines_validity(edges), dtype=bool).ravel()
            ]
        except Exception:
            answers = [False] * len(edges)
    else:
        answers = [True] * len(edges)

    if line_validity is not None:
        for (start, _), chain in zip(bounds, chains):
            for index, (first, second) in enumerate(zip(chain, chain[1:])):
                try:
                    answers[start + index] = answers[start + index] and bool(
                        line_validity(first[0], first[1], second[0], second[1])
                    )
                except Exception:
                    answers[start + index] = False

    runs = []
    for chain, (start, stop) in zip(chains, bounds):
        pieces = []
        current = [chain[0]]
        for valid, point in zip(answers[start:stop], chain[1:]):
            if valid:
                current.append(point)
            else:
                if current:
                    pieces.append(current)
                current = [point]
        if current:
            pieces.append(current)
        for piece in pieces:
            for part, flag in _split_antimeridian(piece, False):
                runs.append(
                    ContourRun(
                        part,
                        flag,
                        tuple(
                            source_by_position.get(point, (None, None))
                            for point in part
                        ),
                    )
                )
    return runs


def _envelope_runs(accepted, parent_points, lines_validity, line_validity):
    """Fallback for callers that do not publish frontier bearings."""
    parents = [_valid_position(point) for point in (parent_points or ())]
    groups = _fan_groups(accepted)
    polygons = []
    observed_arcs = []
    from shapely.geometry import LineString, Polygon
    from shapely.ops import unary_union

    reference_latitude = float(np.mean([candidate.latitude for candidate in accepted]))
    reference_longitude = _circular_mean_longitude(
        [candidate.longitude for candidate in accepted]
    )

    for group in groups:
        parent = group[0].parent
        if parent < 0 or parent >= len(parents) or parents[parent] is None:
            continue
        ring = [parents[parent]] + [candidate.pos for candidate in group]
        ring = _dedupe(ring)
        if len(ring) < 3:
            continue
        projected = _project(ring, reference_latitude, reference_longitude)
        try:
            polygon = Polygon(projected)
            if not polygon.is_valid:
                polygon = polygon.buffer(0)
        except Exception:
            continue
        if polygon.is_empty or polygon.area <= 0:
            continue
        polygons.append(polygon)
        for first, second in zip(projected[1:-1], projected[2:]):
            if not np.array_equal(first, second):
                observed_arcs.append(LineString((first, second)))

    runs = []
    if polygons:
        merged = unary_union(polygons)
        from shapely.strtree import STRtree

        arc_index = STRtree(observed_arcs)
        for polygon in _polygons(merged):
            rings = [polygon.exterior] + list(polygon.interiors)
            for ring in rings:
                for coordinates, flag in _ring_on_observed_arcs(
                    list(ring.coords), observed_arcs, arc_index
                ):
                    points = _unproject(
                        coordinates, reference_latitude, reference_longitude
                    )
                    runs.extend(
                        _run_from_points(
                            points, flag, accepted, lines_validity, line_validity
                        )
                    )
    return runs


def build_contour_slice(
    parent_points,
    candidates,
    *,
    leg_id=0,
    step=0,
    valid_time=None,
    frontier=(),
    frontier_bearings=(),
    gap_m=2200.0,
    boundary=True,
    lines_validity=None,
    line_validity=None,
    fallback=True,
) -> IsochroneSlice:
    """Build one ordered contour slice from the published frontier.

    ``frontier_bearings`` chains the pruned frontier in lateral bearing order.
    Without them, the captured candidate fans provide a conservative envelope.
    """
    accepted = []
    boundary_points = []
    for candidate in candidates or ():
        if isinstance(candidate, CandidateSample):
            if candidate.accepted:
                accepted.append(candidate)
                boundary_points.append(candidate.pos)
            continue
        if isinstance(candidate, (tuple, list)) and len(candidate) == 2:
            try:
                boundary_points.append((float(candidate[0]), float(candidate[1])))
            except (TypeError, ValueError):
                pass
            continue
        try:
            sample = CandidateSample.from_json(candidate)
        except (TypeError, ValueError, IndexError):
            continue
        if sample.accepted:
            accepted.append(sample)
            boundary_points.append(sample.pos)

    frontier_points = tuple(
        point for point in (_valid_position(item) for item in frontier or ()) if point
    )

    if boundary_points and boundary:
        runs = _concave_boundary_runs(boundary_points, lines_validity, line_validity)
        if runs:
            runs = [run for run in runs if run.points]
            runs.sort(key=lambda run: (not run.closed, -len(run.points)))
            return IsochroneSlice(
                leg_id,
                step,
                _iso_time(valid_time),
                tuple(runs),
                SAMPLED_ENVELOPE,
                frontier_points,
            )

    if frontier_bearings and len(frontier_bearings) == len(frontier_points):
        runs = _ordered_frontier_runs(
            frontier_points,
            frontier_bearings,
            accepted,
            lines_validity,
            line_validity,
            gap_m=gap_m,
        )
        runs = [run for run in runs if run.points]
        runs.sort(key=lambda run: (not run.closed, -len(run.points)))
        return IsochroneSlice(
            leg_id,
            step,
            _iso_time(valid_time),
            tuple(runs),
            SAMPLED_ENVELOPE if runs else UNAVAILABLE,
            frontier_points,
        )

    if not accepted:
        if fallback and frontier_points:
            return IsochroneSlice(
                leg_id,
                step,
                _iso_time(valid_time),
                tuple(
                    ContourRun((point,), False, ((None, None),))
                    for point in frontier_points
                ),
                UNAVAILABLE,
                frontier_points,
            )
        return IsochroneSlice(
            leg_id, step, _iso_time(valid_time), (), SAMPLED_ENVELOPE, frontier_points
        )

    runs = _envelope_runs(accepted, parent_points, lines_validity, line_validity)

    if not runs:
        # Sparse steps have no exposed cell to join.  Keep the observed
        # samples visible as dots instead of inventing radial connections.
        runs = [
            ContourRun(
                (candidate.pos,),
                False,
                ((candidate.parent, candidate.heading_index),),
            )
            for candidate in accepted
        ]

    # Stable output: same leg/step, then larger runs first.
    runs = [run for run in runs if run.points]
    runs.sort(key=lambda run: (not run.closed, -len(run.points)))
    return IsochroneSlice(
        leg_id,
        step,
        _iso_time(valid_time),
        tuple(runs),
        SAMPLED_ENVELOPE if runs else UNAVAILABLE,
        frontier_points,
    )


def _point_segment_distance_m(latitude, longitude, start, end):
    """Distance from a point to a short geodesic segment, in metres."""
    lat0 = math.radians(latitude)
    lon0 = math.radians(longitude)
    start_latitude = math.radians(start[0])
    start_longitude = math.radians(start[1])
    end_latitude = math.radians(end[0])
    end_longitude = math.radians(end[1])

    def project(lat, lon):
        return (
            (lon - lon0) * math.cos(lat0) * _EARTH_RADIUS_M,
            (lat - lat0) * _EARTH_RADIUS_M,
        )

    ax, ay = project(start_latitude, start_longitude)
    bx, by = project(end_latitude, end_longitude)
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(ax, ay)
    t = max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
    return math.hypot(ax + t * dx, ay + t * dy)


def hit_test_contours(contours, latitude, longitude, tolerance_m=2500.0):
    """Return the nearest displayed contour edge, or ``None``.

    Only edges that a renderer actually draws are tested.  A closed run tests
    its closing edge; an open run never tests across the break between two
    runs or across an unsampled slot.
    """
    if not contours:
        return None
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return None
    best = None
    for slice_index, item in enumerate(getattr(contours, "slices", ())):
        for run_index, run in enumerate(item.runs):
            points = run.points
            if not points:
                continue
            if len(points) == 1:
                distance = _point_segment_distance_m(
                    latitude, longitude, points[0], points[0]
                )
                if distance <= tolerance_m and (
                    best is None or distance < best.distance_m
                ):
                    best = ContourHit(
                        slice_index,
                        run_index,
                        -1,
                        distance,
                        item.leg_id,
                        item.step,
                        item.valid_time,
                    )
                continue
            edges = len(points) if run.closed else len(points) - 1
            for segment_index in range(edges):
                start = points[segment_index]
                end = points[(segment_index + 1) % len(points)]
                distance = _point_segment_distance_m(latitude, longitude, start, end)
                if distance <= tolerance_m and (
                    best is None or distance < best.distance_m
                ):
                    best = ContourHit(
                        slice_index,
                        run_index,
                        segment_index,
                        distance,
                        item.leg_id,
                        item.step,
                        item.valid_time,
                    )
    return best


def append_contour_slice(router, item):
    """Append one slice, replacing a retried slice for the same leg and step."""
    slices = getattr(router, "contour_slices", None)
    if slices is None:
        slices = router.contour_slices = []
    key = (item.leg_id, item.step)
    slices[:] = [
        existing for existing in slices if (existing.leg_id, existing.step) != key
    ]
    slices.append(item)
    return item


def collect_contours(router):
    """Return the sidecar accumulated by a routing algorithm instance."""
    slices = getattr(router, "contour_slices", ())
    return IsochroneContours(tuple(slices))
