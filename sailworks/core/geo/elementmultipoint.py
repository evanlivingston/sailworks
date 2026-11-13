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

from sailworks.core import utils
from sailworks.core.geo.element import Element
from sailworks.core.geo.rounding import (
    DEFAULT_ROUNDING,
    DEFAULT_ROUNDING_RADIUS_NM,
    ROUNDING_LABELS,
    ROUNDING_PORT,
    ROUNDING_STARBOARD,
    normalize_rounding_spec,
)

try:
    from pyproj import Geod as _Geod

    _CACHED_GEOD = _Geod(ellps="WGS84")
except Exception:
    _CACHED_GEOD = False


class NamedPoint(tuple):
    """A point tuple that can carry optional user-facing metadata.

    It stays a plain tuple (``lat, lon, ...``) for every existing consumer;
    the name and rounding are extra metadata used by the scene tree, tables
    and exports.
    """

    def __new__(
        cls,
        values,
        name="",
        rounding=None,
        radius_nm=None,
        exit_bearing_min_deg=None,
        exit_bearing_max_deg=None,
    ):
        point = super().__new__(cls, values)
        point.name = name or ""
        point.rounding = rounding
        point.rounding_radius_nm = radius_nm
        point.rounding_exit_bearing_min_deg = exit_bearing_min_deg
        point.rounding_exit_bearing_max_deg = exit_bearing_max_deg
        return point


def _point_name(point):
    return getattr(point, "name", "") or ""


def _point_rounding(point):
    value = getattr(point, "rounding", None)
    return None if value is None else int(value)


def _point_rounding_radius(point):
    value = getattr(point, "rounding_radius_nm", None)
    return None if value is None else float(value)


def _point_rounding_exit_min(point):
    value = getattr(point, "rounding_exit_bearing_min_deg", None)
    return None if value is None else float(value)


def _point_rounding_exit_max(point):
    value = getattr(point, "rounding_exit_bearing_max_deg", None)
    return None if value is None else float(value)


def _point_rounding_spec(point):
    side = _point_rounding(point)
    if side == ROUNDING_PORT:
        side_name = "port"
    elif side == ROUNDING_STARBOARD:
        side_name = "starboard"
    else:
        return None
    return normalize_rounding_spec(
        {
            "side": side_name,
            "radius_nm": _point_rounding_radius(point),
            "exit_bearing_min_deg": _point_rounding_exit_min(point),
            "exit_bearing_max_deg": _point_rounding_exit_max(point),
        }
    )


class _TrackedPoints(list):
    """List of points that bumps its owner's revision on every mutation.

    Mutations are O(1) bookkeeping; no point data is scanned, so cache keys
    can use the owner's revision instead of hashing or walking the points.
    """

    def __init__(self, owner, items=()):
        super().__init__(items)
        self._owner = owner


def _tracked_mutator(name):
    base = getattr(list, name)

    def method(self, *args, **kwargs):
        result = base(self, *args, **kwargs)
        owner = getattr(self, "_owner", None)
        if owner is not None:
            owner.touch()
        return result

    method.__name__ = name
    return method


for _name in (
    "__setitem__",
    "__delitem__",
    "__iadd__",
    "__imul__",
    "append",
    "extend",
    "insert",
    "pop",
    "remove",
    "clear",
    "sort",
    "reverse",
):
    setattr(_TrackedPoints, _name, _tracked_mutator(_name))
del _name


class ElementMultiPoint(Element):
    def __init__(self, name, points=None, visible=True, collection=None):
        super().__init__(name, visible, collection)
        self.points = [] if points is None else points
        self._length_cache = None
        self._length_version = -1

    @property
    def points(self):
        return self._points

    @points.setter
    def points(self, value):
        self._points = _TrackedPoints(self, [] if value is None else value)
        self.touch()

    def to_gpx_object(self):
        raise Exception("Not implemented")

    def to_json(self):
        c = super().to_json()
        c["points"] = self.points
        names = [_point_name(point) for point in self.points]
        # Keep the legacy payload byte-for-byte identical when no point has
        # been named. The parallel list stays index-aligned with ``points``.
        if any(names):
            c["point_names"] = names
        roundings = [_point_rounding(point) for point in self.points]
        if any(
            rounding is not None and rounding != DEFAULT_ROUNDING
            for rounding in roundings
        ):
            c["point_roundings"] = [
                DEFAULT_ROUNDING if rounding is None else rounding
                for rounding in roundings
            ]
        specs = [_point_rounding_spec(point) for point in self.points]
        if any(
            spec is not None
            and (
                spec["radius_nm"] != DEFAULT_ROUNDING_RADIUS_NM
                or spec["exit_bearing_min_deg"] is not None
            )
            for spec in specs
        ):
            c["point_rounding_radius_nm"] = [
                None if spec is None else spec["radius_nm"] for spec in specs
            ]
            if any(
                spec is not None and spec["exit_bearing_min_deg"] is not None
                for spec in specs
            ):
                c["point_rounding_exit_min_deg"] = [
                    None if spec is None else spec["exit_bearing_min_deg"]
                    for spec in specs
                ]
                c["point_rounding_exit_max_deg"] = [
                    None if spec is None else spec["exit_bearing_max_deg"]
                    for spec in specs
                ]
        return c

    @staticmethod
    def from_json(j):
        d = Element.from_json(j)
        points = list(j["points"])
        names = j.get("point_names") or []
        roundings = j.get("point_roundings") or []
        radii = j.get("point_rounding_radius_nm") or []
        exit_min = j.get("point_rounding_exit_min_deg") or []
        exit_max = j.get("point_rounding_exit_max_deg") or []
        for index in range(len(points)):
            name = names[index] if index < len(names) else ""
            rounding = roundings[index] if index < len(roundings) else None
            radius = radii[index] if index < len(radii) else None
            minimum = exit_min[index] if index < len(exit_min) else None
            maximum = exit_max[index] if index < len(exit_max) else None
            if rounding not in (ROUNDING_PORT, ROUNDING_STARBOARD):
                radius = minimum = maximum = None
            if (
                name
                or rounding is not None
                or radius is not None
                or minimum is not None
                or maximum is not None
            ):
                points[index] = NamedPoint(
                    tuple(points[index]),
                    name or "",
                    rounding,
                    radius,
                    minimum,
                    maximum,
                )
        return ElementMultiPoint(d.name, points, d.visible)

    def point_name(self, index):
        """Return the user-facing name for a point, or an empty string."""
        if 0 <= index < len(self.points):
            return _point_name(self.points[index])
        return ""

    def point_rounding(self, index):
        """Return the mark rounding value, or None when not applicable."""
        if 0 <= index < len(self.points):
            return _point_rounding(self.points[index])
        return None

    def point_rounding_spec(self, index):
        """Return the routing rounding spec for a point, or None for exact."""
        if 0 <= index < len(self.points):
            return _point_rounding_spec(self.points[index])
        return None

    def rename_point(self, index, name):
        """Set a point's name, keeping the point tuple itself unchanged."""
        if not 0 <= index < len(self.points):
            return False
        point = self.points[index]
        name = "" if name is None else str(name).strip()
        if _point_name(point) == name:
            return False
        try:
            values = tuple(point)
        except TypeError:
            return False
        self.points[index] = NamedPoint(
            values,
            name,
            _point_rounding(point),
            _point_rounding_radius(point),
            _point_rounding_exit_min(point),
            _point_rounding_exit_max(point),
        )
        self.touch()
        if self.collection is not None:
            self.collection.save()
        return True

    def set_point_rounding(self, index, rounding):
        """Set a mark's rounding side, keeping any radius and exit range."""
        if not 0 <= index < len(self.points):
            return False
        try:
            value = int(rounding)
        except (TypeError, ValueError):
            return False
        if value not in ROUNDING_LABELS:
            return False
        point = self.points[index]
        radius = _point_rounding_radius(point)
        minimum = _point_rounding_exit_min(point)
        maximum = _point_rounding_exit_max(point)
        if value not in (ROUNDING_PORT, ROUNDING_STARBOARD):
            radius = minimum = maximum = None
        if (
            _point_rounding(point) == value
            and _point_rounding_radius(point) == radius
            and _point_rounding_exit_min(point) == minimum
            and _point_rounding_exit_max(point) == maximum
        ):
            return False
        self.points[index] = NamedPoint(
            tuple(point),
            _point_name(point),
            value,
            radius,
            minimum,
            maximum,
        )
        self.touch()
        if self.collection is not None:
            self.collection.save()
        return True

    def set_point_rounding_spec(self, index, spec):
        """Set a mark's full rounding spec, or clear it with None."""
        if not 0 <= index < len(self.points):
            return False
        normalized = normalize_rounding_spec(spec)
        point = self.points[index]
        if normalized == _point_rounding_spec(point):
            return False
        if normalized is None:
            side = DEFAULT_ROUNDING
            radius = minimum = maximum = None
        else:
            side = ROUNDING_PORT if normalized["side"] == "port" else ROUNDING_STARBOARD
            radius = (
                None
                if normalized["radius_nm"] == DEFAULT_ROUNDING_RADIUS_NM
                else normalized["radius_nm"]
            )
            minimum = normalized["exit_bearing_min_deg"]
            maximum = normalized["exit_bearing_max_deg"]
        self.points[index] = NamedPoint(
            tuple(point),
            _point_name(point),
            side,
            radius,
            minimum,
            maximum,
        )
        self.touch()
        if self.collection is not None:
            self.collection.save()
        return True

    def __len__(self):
        return len(self.points)

    def __getitem__(self, key):
        return self.points[key]

    def __setitem__(self, key, value):
        self.points[key] = value
        self.touch()
        self.collection.save()

    def __delitem__(self, key):
        del self.points[key]
        self.touch()
        self.collection.save()

    def __iter__(self):
        return iter(self.points)

    def length(self):
        # Cache the (expensive) length computation; invalidate when the
        # element is touched (point edits bump the revision).
        if self._length_cache is not None and self._length_version == self._revision:
            return self._length_cache
        if len(self.points) <= 1:
            self._length_cache = 0.0
            self._length_version = self._revision
            return 0.0

        d = 0.0
        prev = self.points[0]
        geod = _CACHED_GEOD
        for x in self.points[1::]:
            if geod:
                _, _, meters = geod.inv(prev[1], prev[0], x[1], x[0])
                d += meters * 0.000539957
            else:
                d += utils.point_distance(prev[0], prev[1], x[0], x[1])
            prev = x

        self._length_cache = d
        self._length_version = self._revision
        return d

    def move_up(self, i):
        if i > 0 and i < len(self):
            sw = self.points[i - 1]
            self.points[i - 1] = self.points[i]
            self.points[i] = sw
            self.touch()
            self.collection.save()

    def move_down(self, i):
        if i < len(self) - 1 and i >= 0:
            sw = self.points[i + 1]
            self.points[i + 1] = self.points[i]
            self.points[i] = sw
            self.touch()
            self.collection.save()

    def remove(self, i):
        if i >= 0 and i < len(self):
            del self.points[i]
            self.touch()
            self.collection.save()

    def duplicate(self, i):
        point = self.points[i]
        try:
            values = tuple(point)
        except TypeError:
            self.points.append(point)
        else:
            self.points.append(
                NamedPoint(
                    values,
                    _point_name(point),
                    _point_rounding(point),
                    _point_rounding_radius(point),
                    _point_rounding_exit_min(point),
                    _point_rounding_exit_max(point),
                )
            )
        self.touch()
        self.collection.save()

    def add(self, lat, lon, t=None):
        self.points.append(NamedPoint((lat, lon, t)))
        self.touch()
        self.collection.save()

    def move(self, i, lat, lon):
        point = self.points[i]
        self.points[i] = NamedPoint(
            (lat, lon, None),
            _point_name(point),
            _point_rounding(point),
            _point_rounding_radius(point),
            _point_rounding_exit_min(point),
            _point_rounding_exit_max(point),
        )
        self.touch()
        self.collection.save()

    def clear(self):
        self.points = []
        self.touch()
        self.collection.save()
