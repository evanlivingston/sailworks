"""Immutable isochrone contour sidecars and sampled-envelope construction.

The routing frontier is deliberately pruned: it keeps the best candidate per
bearing bin.  That makes adjacent frontier points unrelated lateral samples, so
a renderer cannot reconstruct a truthful boundary from them after the fact.

This module keeps the extra evidence while candidates are still local to one
time slice: each accepted sample remembers its parent frontier point and its
heading ordinal.  Adjacent accepted samples at the same time form a local fan;
the union of those fans, extracted in a local metric projection, is the
sampled search envelope.  It is explicitly a search envelope, not a claim
about the complete physically reachable set.

The data is immutable and JSON-serialisable.  A missing sidecar means a legacy
route, and an unknown sidecar version is reported as unavailable rather than
preventing the route from loading.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

import numpy as np

SAMPLED_ENVELOPE = "sampled-envelope"
LEGACY_FRONTIER = "legacy-frontier"
UNAVAILABLE = "unavailable"

CONTOURS_VERSION = 1

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
        """All steps of the active leg plus each older leg's latest step.

        Older legs remain visible on the route, but showing every one of their
        intermediate steps stacks dozens of crossing envelopes on top of the
        current search.
        """
        active = self.active_leg
        if active is None:
            return IsochroneContours((), self.version, self.representation)
        latest = {}
        for item in self.slices:
            if item.leg_id == active:
                continue
            current = latest.get(item.leg_id)
            if current is None or item.step >= current.step:
                latest[item.leg_id] = item
        shown = [latest[leg] for leg in sorted(latest)]
        shown.extend(item for item in self.slices if item.leg_id == active)
        return IsochroneContours(tuple(shown), self.version, self.representation)

    @property
    def runs(self):
        return tuple(run for item in self.slices for run in item.runs)

    def to_json(self):
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
            return IsochroneContours((), version=version, representation=UNAVAILABLE)
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
    edges = []
    count = len(points)
    last = count if closed else count - 1
    for index in range(max(0, last)):
        start = points[index]
        end = points[(index + 1) % count]
        valid = True
        if lines_validity is not None:
            try:
                answer = lines_validity([[start[0], start[1], end[0], end[1]]])
                valid = bool(np.asarray(answer, dtype=bool).ravel()[0])
            except Exception:
                valid = False
        if valid and line_validity is not None:
            try:
                valid = bool(line_validity(start[0], start[1], end[0], end[1]))
            except Exception:
                valid = False
        edges.append(valid)
    return edges


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
        runs.append((tuple(current), bool(closed)))
    return runs


def _parent_key(point):
    return round(float(point[0]), 6), round(float(point[1]), 6)


def _ring_without_parent_edges(coordinates, parent_keys):
    """Split a union boundary into arcs, removing radial spokes to parents.

    A fan polygon contributes the parent-to-first and parent-to-last edges as
    part of its boundary.  Those spokes are search ancestry, not contour
    geometry: keeping them drew radial spikes from every previous frontier
    point.  Removing every boundary edge incident to a parent leaves the
    exposed candidate-to-candidate envelope.
    """
    points = list(coordinates)
    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]
    if len(points) < 3:
        return []
    if not any(_parent_key(point) in parent_keys for point in points):
        return [(points, True)]

    runs = []
    current = []
    count = len(points)
    for index in range(count):
        start = points[index]
        end = points[(index + 1) % count]
        clean = (
            _parent_key(start) not in parent_keys
            and _parent_key(end) not in parent_keys
        )
        if clean:
            if not current:
                current = [start]
            current.append(end)
        else:
            if len(current) > 1:
                runs.append((current, False))
            current = []
    if len(current) > 1:
        runs.append((current, False))
    return runs


def _approx_distance_m(first, second):
    latitude = math.radians((first[0] + second[0]) / 2.0)
    delta_latitude = math.radians(second[0] - first[0])
    delta_longitude = math.radians(_wrap180(second[1] - first[1])) * math.cos(latitude)
    return _EARTH_RADIUS_M * math.hypot(delta_latitude, delta_longitude)


def _simplify_run(run, tolerance_m, accepted):
    if len(run.points) < 3 or run.closed:
        return run
    from shapely.geometry import LineString

    latitude = math.radians(sum(point[0] for point in run.points) / len(run.points))
    scale = max(abs(math.cos(latitude)), 1e-9)
    coordinates = [
        (
            math.radians(point[1]) * scale * _EARTH_RADIUS_M,
            math.radians(point[0]) * _EARTH_RADIUS_M,
        )
        for point in run.points
    ]
    simplified = LineString(coordinates).simplify(
        float(tolerance_m), preserve_topology=False
    )
    points = tuple(
        (
            math.degrees(y / _EARTH_RADIUS_M),
            math.degrees(x / (scale * _EARTH_RADIUS_M)),
        )
        for x, y in simplified.coords
    )
    if len(points) < 2:
        return run
    return ContourRun(points, False, _sources_for(points, accepted))


def _merge_open_runs(runs, lines_validity, line_validity, accepted):
    """Join contiguous pieces using the local sample spacing, not a fixed gap.

    Fan extraction can leave one short arc per parent even when those arcs are
    the same boundary.  Nearby endpoints are joined, but only when the
    connector passes the configured geographic predicates.  The merge
    tolerance is derived from the slice's own sample spacing.
    """
    open_runs = [run for run in runs if not run.closed and len(run.points) >= 2]
    untouched = [run for run in runs if run.closed or len(run.points) < 2]
    if len(open_runs) < 2:
        return runs

    edges = [
        _approx_distance_m(first, second)
        for run in open_runs
        for first, second in zip(run.points, run.points[1:])
    ]
    if not edges:
        return runs
    spacing = float(np.median(edges))
    tolerance = max(spacing * 10.0, 1500.0)

    changed = True
    while changed:
        changed = False
        best = None
        for left_index, left in enumerate(open_runs):
            for right_index in range(left_index + 1, len(open_runs)):
                right = open_runs[right_index]
                combinations = (
                    (
                        _approx_distance_m(left.points[-1], right.points[0]),
                        "append",
                    ),
                    (
                        _approx_distance_m(left.points[-1], right.points[-1]),
                        "append_reverse",
                    ),
                    (
                        _approx_distance_m(left.points[0], right.points[0]),
                        "reverse_append",
                    ),
                    (
                        _approx_distance_m(left.points[0], right.points[-1]),
                        "reverse_both",
                    ),
                )
                distance, mode = min(combinations)
                if distance > tolerance:
                    continue
                if best is None or distance < best[0]:
                    best = (distance, left_index, right_index, mode)
        if best is None:
            break
        distance, left_index, right_index, mode = best
        left = open_runs[left_index]
        right = open_runs[right_index]
        if mode == "append":
            start, end = left.points[-1], right.points[0]
            points = left.points + right.points
            sources = left.sources + right.sources
        elif mode == "append_reverse":
            start, end = left.points[-1], right.points[-1]
            points = left.points + tuple(reversed(right.points))
            sources = left.sources + tuple(reversed(right.sources))
        elif mode == "reverse_append":
            start, end = left.points[0], right.points[0]
            points = tuple(reversed(left.points)) + right.points
            sources = tuple(reversed(left.sources)) + right.sources
        else:
            start, end = left.points[0], right.points[-1]
            points = tuple(reversed(left.points)) + tuple(reversed(right.points))
            sources = tuple(reversed(left.sources)) + tuple(reversed(right.sources))
        if not all(_valid_edges([start, end], False, lines_validity, line_validity)):
            continue
        merged = ContourRun(points, False, sources)
        open_runs = [
            run
            for index, run in enumerate(open_runs)
            if index not in (left_index, right_index)
        ] + [merged]
        changed = True

    simplify_tolerance = max(spacing * 0.6, 10.0)
    merged_runs = []
    for run in open_runs:
        simplified = _simplify_run(run, simplify_tolerance, accepted)
        for points, flag in _split_antimeridian(simplified.points, False):
            merged_runs.append(ContourRun(points, flag, _sources_for(points, accepted)))
    return merged_runs + untouched


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


def _run_from_points(points, closed, candidates, lines_validity, line_validity):
    parts = []
    for part, flag in _split_antimeridian(points, closed):
        parts.extend(_ring_runs(part, flag, lines_validity, line_validity))
    return [
        ContourRun(part, flag, _sources_for(part, candidates))
        for part, flag in parts
        if part
    ]


def build_contour_slice(
    parent_points,
    candidates,
    *,
    leg_id=0,
    step=0,
    valid_time=None,
    frontier=(),
    lines_validity=None,
    line_validity=None,
    fallback=True,
) -> IsochroneSlice:
    """Build one ordered contour slice from observed candidates.

    ``parent_points`` are the frontier points the candidates were advanced
    from.  ``candidates`` may include rejected slots; only accepted samples
    contribute geometry, and a missing slot always breaks a run.
    """
    parents = [_valid_position(point) for point in (parent_points or ())]
    accepted = []
    for candidate in candidates or ():
        if isinstance(candidate, CandidateSample):
            if candidate.accepted:
                accepted.append(candidate)
        else:
            try:
                sample = CandidateSample.from_json(candidate)
            except (TypeError, ValueError, IndexError):
                continue
            if sample.accepted:
                accepted.append(sample)

    frontier_points = tuple(
        point for point in (_valid_position(item) for item in frontier or ()) if point
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

    groups = _fan_groups(accepted)
    polygons = []
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    reference_latitude = float(np.mean([candidate.latitude for candidate in accepted]))
    reference_longitude = _circular_mean_longitude(
        [candidate.longitude for candidate in accepted]
    )

    parent_keys = set()
    for parent in parents:
        if parent is None:
            continue
        projected_parent = _project([parent], reference_latitude, reference_longitude)[
            0
        ]
        parent_keys.add(_parent_key(projected_parent))

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

    runs = []
    if polygons:
        merged = unary_union(polygons)
        from shapely.geometry import GeometryCollection, MultiPolygon, Polygon

        polygons_out = []
        if isinstance(merged, Polygon):
            polygons_out.append(merged)
        elif isinstance(merged, MultiPolygon):
            polygons_out.extend(merged.geoms)
        elif isinstance(merged, GeometryCollection):
            polygons_out.extend(
                geometry for geometry in merged.geoms if isinstance(geometry, Polygon)
            )
        for polygon in polygons_out:
            rings = [polygon.exterior] + list(polygon.interiors)
            for ring in rings:
                for coordinates, closed in _ring_without_parent_edges(
                    list(ring.coords), parent_keys
                ):
                    points = _unproject(
                        coordinates, reference_latitude, reference_longitude
                    )
                    runs.extend(
                        _run_from_points(
                            points, closed, accepted, lines_validity, line_validity
                        )
                    )

    if runs:
        runs = _merge_open_runs(runs, lines_validity, line_validity, accepted)

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
