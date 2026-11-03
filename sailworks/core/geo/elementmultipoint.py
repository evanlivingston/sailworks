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

try:
    from pyproj import Geod as _Geod

    _CACHED_GEOD = _Geod(ellps="WGS84")
except Exception:
    _CACHED_GEOD = False


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
        return c

    @staticmethod
    def from_json(j):
        d = Element.from_json(j)
        return ElementMultiPoint(d.name, j["points"], d.visible)

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
        self.points.append(self.points[i])
        self.collection.save()

    def add(self, lat, lon, t=None):
        self.points.append((lat, lon, t))
        self.touch()
        self.collection.save()

    def move(self, i, lat, lon):
        self.points[i] = (lat, lon, None)
        self.touch()
        self.collection.save()

    def clear(self):
        self.points = []
        self.touch()
        self.collection.save()
