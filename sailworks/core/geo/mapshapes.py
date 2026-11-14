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

"""Immutable map shape model and the native avoidance geometry built on it.

Vertices are ``(lat, lon)`` pairs in decimal degrees. Shapes of kind ``avoid``
are always polygons; shapes of kind ``note`` may be a single point or a line.
Polygon validity is strict: at least three distinct vertices, no
self-intersection, and no boundary contact between non-adjacent edges or
collinear overlap between adjacent ones.

Enabled avoid polygons cross the process boundary as one frozen, ordered
``AvoidZone`` snapshot plus a canonical SHA256 digest. The canonical ring keeps
the payload independent of the order a user happened to click the vertices in,
so two equivalent shapes always produce the same digest.
"""

import hashlib
import json
from dataclasses import dataclass
from math import cos, hypot, isfinite, radians

KIND_NOTE = "note"
KIND_AVOID = "avoid"
KINDS = (KIND_NOTE, KIND_AVOID)

MIN_POLYGON_VERTICES = 3
LAT_LIMIT = 90.0
LON_LIMIT = 180.0
NM_PER_DEGREE = 60.0

# Chart overlay colours per kind, as additive-ready RGB triples. The stored
# per-shape ``colour`` overrides these; the defaults live here so the model,
# the overlay and the scene icon all agree.
DEFAULT_COLOURS = {
    KIND_AVOID: (0.86, 0.35, 0.16),
    KIND_NOTE: (0.16, 0.55, 0.86),
}
DEFAULT_COLOUR = (1.0, 1.0, 1.0)

# Distance queries are planar approximations over a local equirectangular
# projection, which is accurate at the scale of a charted hazard.
_MIN_COS = 1e-9


class ShapeValidationError(ValueError):
    """Raised when shape data cannot be stored or used as avoidance geometry."""


def _coerce_vertex(value, index):
    try:
        lat, lon = value
    except (TypeError, ValueError):
        raise ShapeValidationError(
            f"vertex {index} must be a (lat, lon) pair, got {value!r}"
        ) from None

    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        raise ShapeValidationError(
            f"vertex {index} has non-numeric coordinates: {value!r}"
        ) from None

    if not isfinite(lat) or not isfinite(lon):
        raise ShapeValidationError(f"vertex {index} is not finite: {value!r}")
    if not -LAT_LIMIT <= lat <= LAT_LIMIT:
        raise ShapeValidationError(f"vertex {index} latitude out of range: {lat}")
    if not -LON_LIMIT <= lon <= LON_LIMIT:
        raise ShapeValidationError(f"vertex {index} longitude out of range: {lon}")

    return (lat, lon)


def _orient(ax, ay, bx, by, cx, cy):
    """Twice the signed area of triangle abc; zero means collinear."""
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _point_on_segment(px, py, ax, ay, bx, by):
    if _orient(ax, ay, bx, by, px, py) != 0:
        return False
    return min(ax, bx) <= px <= max(ax, bx) and min(ay, by) <= py <= max(ay, by)


def _segments_touch(ax, ay, bx, by, cx, cy, dx, dy):
    """True when two closed segments share at least one point."""
    d1 = _orient(cx, cy, dx, dy, ax, ay)
    d2 = _orient(cx, cy, dx, dy, bx, by)
    d3 = _orient(ax, ay, bx, by, cx, cy)
    d4 = _orient(ax, ay, bx, by, dx, dy)

    if ((d1 > 0 > d2) or (d1 < 0 < d2)) and ((d3 > 0 > d4) or (d3 < 0 < d4)):
        return True

    return (
        (d1 == 0 and _point_on_segment(ax, ay, cx, cy, dx, dy))
        or (d2 == 0 and _point_on_segment(bx, by, cx, cy, dx, dy))
        or (d3 == 0 and _point_on_segment(cx, cy, ax, ay, bx, by))
        or (d4 == 0 and _point_on_segment(dx, dy, ax, ay, bx, by))
    )


def _adjacent_edges_overlap(shared, first_far, second_far):
    """True when two edges leaving ``shared`` collinearly overlap."""
    sx, sy = shared[1], shared[0]
    fx, fy = first_far[1], first_far[0]
    ox, oy = second_far[1], second_far[0]
    if _orient(sx, sy, fx, fy, ox, oy) != 0:
        return False
    # A straight-through vertex keeps both far points outside the other edge.
    return _point_on_segment(fx, fy, sx, sy, ox, oy) or _point_on_segment(
        ox, oy, sx, sy, fx, fy
    )


def _polygon_area(vertices):
    total = 0.0
    count = len(vertices)
    for index in range(count):
        lat1, lon1 = vertices[index]
        lat2, lon2 = vertices[(index + 1) % count]
        total += lon1 * lat2 - lon2 * lat1
    return total / 2.0


def validate_vertices(kind, vertices):
    """Return a normalised vertex tuple, or raise ``ShapeValidationError``."""
    if kind not in KINDS:
        raise ShapeValidationError(f"unknown shape kind: {kind!r}")
    if vertices is None:
        raise ShapeValidationError("a shape needs at least one vertex")
    if isinstance(vertices, (str, bytes)):
        raise ShapeValidationError("vertices must be a sequence of (lat, lon)")

    try:
        raw = list(vertices)
    except TypeError:
        raise ShapeValidationError(
            "vertices must be a sequence of (lat, lon)"
        ) from None

    if not raw:
        raise ShapeValidationError("a shape needs at least one vertex")

    points = tuple(_coerce_vertex(value, index) for index, value in enumerate(raw))

    polygon = kind == KIND_AVOID or len(points) >= MIN_POLYGON_VERTICES
    if kind == KIND_AVOID and len(points) < MIN_POLYGON_VERTICES:
        raise ShapeValidationError(
            f"an avoid polygon needs at least {MIN_POLYGON_VERTICES} vertices"
        )

    if polygon:
        if len(set(points)) != len(points):
            raise ShapeValidationError("polygon vertices must be distinct")
        # Geometry here is planar in raw degrees. A polygon whose longitudes
        # span more than half the world cannot be projected without an
        # antimeridian unwrap, and would otherwise be treated as a global
        # band, so it is refused rather than silently misinterpreted.
        longitudes = [point[1] for point in points]
        if max(longitudes) - min(longitudes) > LON_LIMIT:
            raise ShapeValidationError(
                "polygon longitude span exceeds 180 degrees; split the shape "
                "at the antimeridian"
            )
        if _polygon_area(points) == 0:
            raise ShapeValidationError("polygon has no area")

        count = len(points)
        for start in range(count):
            a = points[start]
            b = points[(start + 1) % count]
            for other in range(start + 1, count):
                c = points[other]
                d = points[(other + 1) % count]
                adjacent_forward = other == start + 1
                adjacent_wrap = start == 0 and other == count - 1
                if adjacent_forward:
                    if _adjacent_edges_overlap(b, a, d):
                        raise ShapeValidationError(
                            "polygon edges may not overlap at a vertex"
                        )
                elif adjacent_wrap:
                    if _adjacent_edges_overlap(a, b, c):
                        raise ShapeValidationError(
                            "polygon edges may not overlap at a vertex"
                        )
                elif _segments_touch(a[1], a[0], b[1], b[0], c[1], c[0], d[1], d[0]):
                    raise ShapeValidationError("polygon edges may not cross or touch")

    return points


def validate_colour(value):
    """Return a normalised ``#RRGGBB`` colour, or ``""`` when unset.

    The empty string means "use the kind default", which keeps stored shapes
    from before per-shape colours loadable without a migration.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ShapeValidationError(f"shape colour must be a string: {value!r}")
    value = value.strip().upper()
    if not value:
        return ""
    if len(value) != 7 or not value.startswith("#"):
        raise ShapeValidationError(f"shape colour must be #RRGGBB or empty: {value!r}")
    try:
        int(value[1:], 16)
    except ValueError:
        raise ShapeValidationError(
            f"shape colour must be #RRGGBB or empty: {value!r}"
        ) from None
    return value


def colour_rgb(colour, kind):
    """Overlay RGB for a stored colour, falling back to the kind default."""
    text = validate_colour(colour)
    if text:
        return (
            int(text[1:3], 16) / 255.0,
            int(text[3:5], 16) / 255.0,
            int(text[5:7], 16) / 255.0,
        )
    return DEFAULT_COLOURS.get(kind, DEFAULT_COLOUR)


def _polygon_contains(vertices, lat, lon):
    """Inclusive point-in-polygon test; the boundary counts as inside."""
    inside = False
    count = len(vertices)
    for index in range(count):
        lat1, lon1 = vertices[index]
        lat2, lon2 = vertices[(index + 1) % count]
        if _point_on_segment(lon, lat, lon1, lat1, lon2, lat2):
            return True
        if (lat1 > lat) != (lat2 > lat):
            crossing = lon1 + (lat - lat1) * (lon2 - lon1) / (lat2 - lat1)
            if lon < crossing:
                inside = not inside
    return inside


def canonical_vertices(vertices):
    """Return a polygon ring in a position-independent canonical order.

    The ring is normalised to a positive (counter-clockwise in lon/lat) signed
    area and rotated so the lexicographically smallest ``(lat, lon)`` vertex
    comes first. Geometrically identical rings that differ only by rotation or
    winding therefore share one encoding, which is what the native digest
    commits to. Vertices are already validated by ``validate_vertices``; this
    only checks that every coordinate is finite before coercing to ``float``.
    """
    ring = []
    for index, value in enumerate(vertices):
        try:
            latitude, longitude = value
            point = (float(latitude), float(longitude))
        except (TypeError, ValueError):
            raise ShapeValidationError(
                f"vertex {index} must be a finite (lat, lon) pair"
            ) from None
        if not (isfinite(point[0]) and isfinite(point[1])):
            raise ShapeValidationError(f"vertex {index} is not finite: {value!r}")
        ring.append(point)

    ring = tuple(ring)
    if len(ring) < MIN_POLYGON_VERTICES:
        return ring
    if _polygon_area(ring) < 0:
        ring = tuple(reversed(ring))
    start = min(range(len(ring)), key=lambda index: ring[index])
    return ring[start:] + ring[:start]


@dataclass(frozen=True)
class AvoidZone:
    """One frozen avoid polygon exactly as the native boundary consumes it."""

    id: str
    name: str
    vertices: tuple

    def __post_init__(self):
        if not isinstance(self.id, str) or not self.id:
            raise ShapeValidationError("an avoid zone needs a non-empty id")
        if not isinstance(self.name, str):
            raise ShapeValidationError("avoid zone name must be a string")
        object.__setattr__(self, "vertices", canonical_vertices(self.vertices))

    def contains(self, latitude, longitude):
        """Inclusive point-in-polygon test over the frozen ring."""
        return _polygon_contains(self.vertices, float(latitude), float(longitude))

    def to_native(self):
        return {
            "id": self.id,
            "name": self.name,
            "vertices": [[lat, lon] for lat, lon in self.vertices],
        }


def active_avoid_zones(shapes=()):
    """The ordered active avoid-zone snapshot for a shape selection.

    Only enabled ``avoid`` polygons are planning input. Note-only and disabled
    shapes are inert and never reach the native boundary, so a selection with
    no active avoid polygon yields an empty tuple.
    """
    zones = []
    for value in shapes:
        shape = _as_map_shape(value)
        if shape.kind != KIND_AVOID or not shape.enabled:
            continue
        zones.append(AvoidZone(shape.id, shape.name, shape.vertices))
    return tuple(zones)


def avoid_zones_digest(zones):
    """Canonical SHA256 over the ordered zone payload; empty when inert.

    The encoding is fixed: JSON with sorted keys, no whitespace and ASCII
    escapes, over the same ``to_native`` records the bridge receives. Float
    coordinates use the shortest round-trip representation, so the digest is
    reproducible across processes and platforms.
    """
    if not zones:
        return ""
    payload = [zone.to_native() for zone in zones]
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def avoid_zones_digest_from_native(zones):
    """Recompute the canonical digest from a parsed native zone payload.

    ``zones`` is the ``avoid_zones`` list exactly as it reaches the native
    boundary: an ordered sequence of ``{id, name, vertices}`` mappings. Each
    ring is re-normalised with the same rule ``AvoidZone`` applies, so the
    result commits to the geometry actually supplied rather than to a caller's
    claim about it. The native bridge calls this while holding the GIL instead
    of re-implementing the float encoding in C++.
    """
    if not zones:
        return ""
    records = []
    for zone in zones:
        try:
            zone_id = zone["id"]
            name = zone["name"]
            vertices = zone["vertices"]
        except (KeyError, TypeError):
            raise ShapeValidationError(
                "avoid zone payload must be {id, name, vertices} mappings"
            ) from None
        records.append(AvoidZone(zone_id, name, tuple(vertices)))
    return avoid_zones_digest(tuple(records))


def _segment_touches_polygon(vertices, start, end):
    lat1, lon1 = start
    lat2, lon2 = end
    count = len(vertices)
    for index in range(count):
        edge_lat1, edge_lon1 = vertices[index]
        edge_lat2, edge_lon2 = vertices[(index + 1) % count]
        if _segments_touch(
            lon1,
            lat1,
            lon2,
            lat2,
            edge_lon1,
            edge_lat1,
            edge_lon2,
            edge_lat2,
        ):
            return True
    return False


def _distance_nm_to_segment(lat, lon, start, end):
    scale = max(abs(cos(radians(lat))), _MIN_COS)
    ax = (start[1] - lon) * NM_PER_DEGREE * scale
    ay = (start[0] - lat) * NM_PER_DEGREE
    bx = (end[1] - lon) * NM_PER_DEGREE * scale
    by = (end[0] - lat) * NM_PER_DEGREE

    dx = bx - ax
    dy = by - ay
    denominator = dx * dx + dy * dy
    if denominator == 0:
        return hypot(ax, ay)

    t = -(ax * dx + ay * dy) / denominator
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return hypot(ax + t * dx, ay + t * dy)


def _bbox_distance_nm(lat, lon, bbox):
    """Safe lower bound on the distance from a point to a bbox, in nm."""
    min_lat, min_lon, max_lat, max_lon = bbox
    dlat = max(min_lat - lat, 0.0, lat - max_lat)
    dlon = max(min_lon - lon, 0.0, lon - max_lon)
    # Use the highest latitude involved so the bound never overestimates the
    # east-west distance at the polygon.
    scale = max(abs(cos(radians(max(abs(lat), abs(min_lat), abs(max_lat))))), _MIN_COS)
    return hypot(dlat, dlon * scale) * NM_PER_DEGREE


def _polygon_bbox(vertices):
    lats = [vertex[0] for vertex in vertices]
    lons = [vertex[1] for vertex in vertices]
    return (min(lats), min(lons), max(lats), max(lons))


def _bbox_intersects(a, b):
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


@dataclass(frozen=True)
class MapShape:
    """Validated, immutable shape data owned by the shape collection.

    ``colour`` is presentation only. It is deliberately absent from
    ``AvoidZone`` so the frozen avoidance payload and its digest stay
    independent of styling.
    """

    id: str
    name: str = ""
    note: str = ""
    kind: str = KIND_NOTE
    vertices: tuple = ()
    enabled: bool = True
    colour: str = ""

    def __post_init__(self):
        if not isinstance(self.id, str) or not self.id:
            raise ShapeValidationError("a map shape needs a non-empty id")
        if self.kind not in KINDS:
            raise ShapeValidationError(f"unknown shape kind: {self.kind!r}")
        if not isinstance(self.name, str):
            raise ShapeValidationError("shape name must be a string")
        if not isinstance(self.note, str):
            raise ShapeValidationError("shape note must be a string")

        object.__setattr__(
            self, "vertices", validate_vertices(self.kind, self.vertices)
        )
        object.__setattr__(self, "enabled", bool(self.enabled))
        object.__setattr__(self, "colour", validate_colour(self.colour))

    @property
    def is_polygon(self):
        return len(self.vertices) >= MIN_POLYGON_VERTICES

    @property
    def rgb(self):
        """Overlay colour: the custom ``#RRGGBB`` or the kind default."""
        return colour_rgb(self.colour, self.kind)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "note": self.note,
            "kind": self.kind,
            "vertices": [[lat, lon] for lat, lon in self.vertices],
            "enabled": self.enabled,
            "colour": self.colour,
        }

    @classmethod
    def from_dict(cls, value):
        """Build a shape from persisted data, validating it again."""
        if not isinstance(value, dict):
            raise ShapeValidationError("a stored shape must be an object")
        if "id" not in value:
            raise ShapeValidationError("a stored shape needs an id")
        return cls(
            id=value["id"],
            name=str(value.get("name") or ""),
            note=str(value.get("note") or ""),
            kind=value.get("kind", KIND_NOTE),
            vertices=value.get("vertices", ()),
            enabled=bool(value.get("enabled", True)),
            colour=value.get("colour", ""),
        )


class AvoidancePolygonProvider:
    """Avoidance queries over enabled ``avoid`` shapes, using only the stdlib.

    ``point_forbidden`` and ``segment_forbidden`` treat the polygon boundary as
    forbidden. Every query short-circuits on the polygon bounding box before
    doing any edge work.
    """

    def __init__(self, shapes=()):
        prepared = []
        for value in shapes:
            shape = _as_map_shape(value)
            if shape.kind != KIND_AVOID or not shape.enabled:
                continue
            prepared.append((shape, _polygon_bbox(shape.vertices)))
        self._polygons = tuple(prepared)

    def __len__(self):
        return len(self._polygons)

    @property
    def shapes(self):
        return tuple(shape for shape, _bbox in self._polygons)

    def containing_shape(self, lat, lon):
        """The first enabled avoid shape containing the point, or ``None``.

        Used for user-facing diagnostics; ``point_forbidden`` remains the
        boolean search predicate.
        """
        lat = float(lat)
        lon = float(lon)
        for shape, bbox in self._polygons:
            if not (bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]):
                continue
            if _polygon_contains(shape.vertices, lat, lon):
                return shape
        return None

    def point_forbidden(self, lat, lon):
        return self.containing_shape(lat, lon) is not None

    def segment_forbidden(self, start, end):
        """True when the segment touches, crosses or lies inside a polygon."""
        start = _coerce_vertex(start, 0)
        end = _coerce_vertex(end, 1)
        segment_bbox = (
            min(start[0], end[0]),
            min(start[1], end[1]),
            max(start[0], end[0]),
            max(start[1], end[1]),
        )
        for shape, bbox in self._polygons:
            if not _bbox_intersects(bbox, segment_bbox):
                continue
            if _segment_touches_polygon(shape.vertices, start, end):
                return True
            if _polygon_contains(shape.vertices, start[0], start[1]):
                return True
            if _polygon_contains(shape.vertices, end[0], end[1]):
                return True
        return False

    def distance_nm(self, lat, lon):
        """Distance to the nearest avoid boundary, or 0 inside a polygon.

        Returns ``inf`` when there are no enabled avoid polygons.
        """
        lat = float(lat)
        lon = float(lon)
        best = float("inf")
        for shape, bbox in self._polygons:
            if bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]:
                if _polygon_contains(shape.vertices, lat, lon):
                    return 0.0
            if _bbox_distance_nm(lat, lon, bbox) >= best:
                continue
            count = len(shape.vertices)
            for index in range(count):
                candidate = _distance_nm_to_segment(
                    lat,
                    lon,
                    shape.vertices[index],
                    shape.vertices[(index + 1) % count],
                )
                if candidate < best:
                    best = candidate
        return best


def _as_map_shape(value):
    if isinstance(value, MapShape):
        return value
    to_map_shape = getattr(value, "to_map_shape", None)
    if callable(to_map_shape):
        return to_map_shape()
    raise ShapeValidationError(f"not a map shape: {value!r}")
