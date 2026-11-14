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

"""Mutable, Element-compatible shape stored through the collection manager."""

import logging

from sailworks.core.geo.collection import Collection
from sailworks.core.geo.element import Element
from sailworks.core.geo.mapshapes import (
    KIND_NOTE,
    KINDS,
    AvoidancePolygonProvider,
    MapShape,
    ShapeValidationError,
    validate_colour,
    validate_vertices,
)

logger = logging.getLogger("sailworks")


class Shape(Element):
    """A user-authored note or avoidance polygon.

    Instances are mutable so the existing editing surfaces can change them;
    every edit bumps ``Element.revision`` and ``to_map_shape`` hands out the
    validated immutable snapshot used by geometry consumers and persistence.
    Collection JSON stays the single owner of shape data.
    """

    def __init__(
        self,
        name,
        vertices=None,
        kind=KIND_NOTE,
        note="",
        enabled=True,
        visible=True,
        collection=None,
        uid=None,
        colour="",
    ):
        Element.__init__(self, name, visible, collection, uid)

        self._kind = self._validate_kind(kind)
        self._vertices = ()
        self._note = ""
        self._enabled = True
        self._colour = ""

        self._vertices = validate_vertices(
            self._kind, () if vertices is None else vertices
        )
        self.note = note
        self.enabled = enabled
        self.colour = colour
        self.touch()

    # ------------------------------------------------------------------
    # Validated fields

    @staticmethod
    def _validate_kind(value):
        if value not in KINDS:
            raise ShapeValidationError(f"unknown shape kind: {value!r}")
        return value

    @property
    def kind(self):
        return self._kind

    @kind.setter
    def kind(self, value):
        value = self._validate_kind(value)
        if value != self._kind:
            # Revalidate the stored geometry against the new kind.
            self._vertices = validate_vertices(
                value, () if not self._vertices else self._vertices
            )
            self._kind = value
            self.touch()

    @property
    def vertices(self):
        return list(self._vertices)

    @vertices.setter
    def vertices(self, value):
        self._vertices = validate_vertices(self._kind, value)
        self.touch()

    @property
    def note(self):
        return self._note

    @note.setter
    def note(self, value):
        value = "" if value is None else str(value)
        if value != self._note:
            self._note = value
            self.touch()

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        value = bool(value)
        if value != self._enabled:
            self._enabled = value
            self.touch()

    @property
    def colour(self):
        return self._colour

    @colour.setter
    def colour(self, value):
        value = validate_colour(value)
        if value != self._colour:
            self._colour = value
            self.touch()

    @property
    def is_polygon(self):
        return len(self._vertices) >= 3

    # ------------------------------------------------------------------
    # Model and persistence

    def to_map_shape(self) -> MapShape:
        """Immutable, validated snapshot for geometry and scene consumers."""
        return MapShape(
            id=self.uid,
            name=self.name,
            note=self._note,
            kind=self._kind,
            vertices=self._vertices,
            enabled=self._enabled,
            colour=self._colour,
        )

    def to_json(self):
        value = super().to_json()
        value["kind"] = self._kind
        value["note"] = self._note
        value["vertices"] = [[lat, lon] for lat, lon in self._vertices]
        value["enabled"] = self._enabled
        value["colour"] = self._colour
        return value

    @staticmethod
    def from_json(j):
        shape = Shape(
            j.get("name") or "shape",
            j.get("vertices") or (),
            kind=j.get("kind", KIND_NOTE),
            note=j.get("note") or "",
            enabled=bool(j.get("enabled", True)),
            visible=bool(j.get("visible", True)),
            uid=j.get("uid"),
            colour=j.get("colour") or "",
        )
        return shape


class _ShapeCollection(Collection):
    """Shape data owner; scene entries hold references only."""

    def __init__(self):
        super().__init__(Shape, "shape")

    def create(
        self,
        vertices,
        kind=KIND_NOTE,
        name=None,
        note="",
        enabled=True,
        save=True,
        colour="",
    ):
        shape = Shape(
            name or self.get_unique_name(),
            vertices,
            kind=kind,
            note=note,
            enabled=enabled,
            collection=self,
            colour=colour,
        )
        self.elements.append(shape)
        self.touch()
        if save:
            self.save()
        return shape

    def avoidance_provider(self) -> AvoidancePolygonProvider:
        """Provider over the enabled avoid polygons currently stored."""
        return AvoidancePolygonProvider(self.elements)

    def load_json(self, j):
        # One invalid stored shape must not stop startup: skip it and keep the
        # rest of the collection usable.
        elements = []
        for value in j.get("elements", []):
            try:
                element = Shape.from_json(value)
            except (ShapeValidationError, KeyError, TypeError, ValueError) as error:
                logger.warning("Skipping invalid stored shape: %s", error)
                continue
            element.uid = value.get("uid") or element.uid
            element.collection = self
            elements.append(element)
        self.elements = elements
        self.touch()


ShapeCollection = _ShapeCollection
