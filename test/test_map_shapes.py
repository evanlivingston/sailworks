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

from dataclasses import FrozenInstanceError
from math import cos, radians

import pytest

from sailworks.core.geo.collection import CollectionStorage
from sailworks.core.geo.mapshapes import (
    KIND_AVOID,
    KIND_NOTE,
    AvoidancePolygonProvider,
    MapShape,
    ShapeValidationError,
)
from sailworks.core.geo.shapecollection import Shape, ShapeCollection
from sailworks.core.scene import KIND_SHAPE, SceneCollection, SceneEntry

# Clockwise square in (lat, lon): 2 degrees on each side at the origin.
SQUARE = ((0.0, 0.0), (0.0, 2.0), (2.0, 2.0), (2.0, 0.0))
BOWTIE = ((0.0, 0.0), (2.0, 2.0), (0.0, 2.0), (2.0, 0.0))


def _collection(monkeypatch):
    """In-memory shape collection: storage I/O is patched out."""
    monkeypatch.setattr(CollectionStorage, "load_or_save_default", lambda self: None)
    monkeypatch.setattr(CollectionStorage, "save", lambda self: None)
    return ShapeCollection()


def _avoid(**kwargs):
    options = {"id": "a1", "name": "Hazard", "kind": KIND_AVOID, "vertices": SQUARE}
    options.update(kwargs)
    return MapShape(**options)


# ----------------------------------------------------------------------
# MapShape validation


def test_avoid_shape_is_immutable_and_normalised():
    shape = _avoid()
    assert shape.vertices == SQUARE
    assert shape.enabled is True
    assert shape.is_polygon
    with pytest.raises(FrozenInstanceError):
        shape.name = "changed"


def test_avoid_polygon_requires_three_distinct_vertices():
    with pytest.raises(ShapeValidationError):
        MapShape(id="a", kind=KIND_AVOID, vertices=((0.0, 0.0), (1.0, 1.0)))
    with pytest.raises(ShapeValidationError):
        MapShape(
            id="a",
            kind=KIND_AVOID,
            vertices=((0.0, 0.0), (1.0, 1.0), (0.0, 0.0)),
        )


def test_rejects_nonfinite_and_out_of_bounds_vertices():
    for bad in ((float("nan"), 0.0), (float("inf"), 0.0), (0.0, float("inf"))):
        with pytest.raises(ShapeValidationError):
            _avoid(vertices=(bad, (0.0, 1.0), (1.0, 1.0)))
    with pytest.raises(ShapeValidationError):
        _avoid(vertices=((91.0, 0.0), (0.0, 1.0), (1.0, 1.0)))
    with pytest.raises(ShapeValidationError):
        _avoid(vertices=((0.0, 181.0), (0.0, 1.0), (1.0, 1.0)))


def test_rejects_self_intersection_and_degenerate_polygons():
    with pytest.raises(ShapeValidationError):
        _avoid(vertices=BOWTIE)
    with pytest.raises(ShapeValidationError):
        _avoid(vertices=((0.0, 0.0), (0.0, 1.0), (0.0, 2.0)))


def test_rejects_boundary_touch_between_edges():
    # The last vertex sits on the first edge, so the boundary overlaps.
    touching = ((0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (2.0, 0.0))
    with pytest.raises(ShapeValidationError):
        _avoid(vertices=touching)


def test_rejects_polygon_spanning_more_than_half_the_world():
    # Raw-degree geometry cannot represent an antimeridian crossing: the
    # bounding box would cover the globe, so the shape is refused instead of
    # being misinterpreted as a worldwide forbidden band.
    crossing = ((10.0, 179.0), (10.0, -179.0), (12.0, -179.0), (12.0, 179.0))
    with pytest.raises(ShapeValidationError, match="longitude span exceeds 180"):
        _avoid(vertices=crossing)

    # A polygon that stays on one side remains valid.
    narrow = ((10.0, 179.0), (10.0, 179.5), (10.5, 179.5), (10.5, 179.0))
    assert _avoid(vertices=narrow).vertices == narrow


def test_note_shape_accepts_point_and_line():
    point = MapShape(id="n1", kind=KIND_NOTE, vertices=((10.0, 20.0),))
    assert point.vertices == ((10.0, 20.0),)
    assert not point.is_polygon
    line = MapShape(id="n2", kind=KIND_NOTE, vertices=((0.0, 0.0), (0.0, 1.0)))
    assert line.is_polygon is False


def test_map_shape_dict_round_trip():
    shape = _avoid(note="coral", enabled=False)
    assert MapShape.from_dict(shape.to_dict()) == shape


def test_shape_colour_defaults_to_empty_and_validates_hex():
    shape = _avoid()
    assert shape.colour == ""
    assert shape.rgb == (0.86, 0.35, 0.16)
    assert _avoid(colour="#ff8800").colour == "#FF8800"
    assert _avoid(colour=" ").colour == ""
    for bad in ("red", "#FFF", "#GGGGGG", "ff8800", 12):
        with pytest.raises(ShapeValidationError, match="colour"):
            _avoid(colour=bad)


def test_shape_colour_round_trips_and_old_shapes_load_without_it():
    shape = _avoid(colour="#00A0FF")
    restored = MapShape.from_dict(shape.to_dict())
    assert restored == shape
    assert restored.colour == "#00A0FF"

    legacy = shape.to_dict()
    legacy.pop("colour")
    assert MapShape.from_dict(legacy).colour == ""

    note = MapShape(id="n1", kind=KIND_NOTE, vertices=((10.0, 20.0),))
    assert note.rgb == (0.16, 0.55, 0.86)


def test_colour_never_reaches_the_avoidance_digest():
    from sailworks.core.geo.mapshapes import active_avoid_zones, avoid_zones_digest

    plain = _avoid()
    coloured = _avoid(colour="#123456")
    assert avoid_zones_digest(active_avoid_zones([plain])) == avoid_zones_digest(
        active_avoid_zones([coloured])
    )
    zone = active_avoid_zones([coloured])[0]
    assert set(zone.to_native()) == {"id", "name", "vertices"}


def test_shape_json_round_trip_keeps_colour_and_old_json_loads():
    shape = Shape("Reef", SQUARE, kind=KIND_AVOID, colour="#abcdef")
    value = shape.to_json()
    assert value["colour"] == "#ABCDEF"
    assert Shape.from_json(value).colour == "#ABCDEF"

    value.pop("colour")
    assert Shape.from_json(value).colour == ""


def test_shape_colour_setter_bumps_the_revision():
    shape = Shape("Reef", SQUARE, kind=KIND_AVOID)
    before = shape.revision
    shape.colour = "#112233"
    assert shape.revision > before
    untouched = shape.revision
    shape.colour = "#112233"
    assert shape.revision == untouched
    assert shape.to_map_shape().colour == "#112233"


# ----------------------------------------------------------------------
# Avoidance geometry


def test_point_forbidden_inside_outside_and_on_boundary():
    provider = AvoidancePolygonProvider([_avoid()])
    assert provider.point_forbidden(1.0, 1.0) is True
    assert provider.point_forbidden(0.0, 1.0) is True  # on an edge
    assert provider.point_forbidden(0.0, 0.0) is True  # on a vertex
    assert provider.point_forbidden(3.0, 1.0) is False
    assert provider.point_forbidden(-1.0, -1.0) is False


def test_provider_ignores_disabled_and_note_shapes():
    provider = AvoidancePolygonProvider(
        [
            _avoid(id="off", enabled=False),
            MapShape(id="n1", kind=KIND_NOTE, vertices=SQUARE),
        ]
    )
    assert len(provider) == 0
    assert provider.point_forbidden(1.0, 1.0) is False


def test_containing_shape_names_the_shape_that_blocks_a_point():
    reef = _avoid(id="reef", name="Charted reef")
    provider = AvoidancePolygonProvider([reef])

    assert provider.containing_shape(1.0, 1.0) is reef
    assert provider.containing_shape(3.0, 3.0) is None


def test_segment_forbidden_crossing_inside_and_outside():
    provider = AvoidancePolygonProvider([_avoid()])
    assert provider.segment_forbidden((-1.0, 1.0), (3.0, 1.0)) is True  # crossing
    assert provider.segment_forbidden((0.5, 0.5), (1.5, 1.5)) is True  # inside
    assert provider.segment_forbidden((0.0, 0.0), (0.0, 2.0)) is True  # on boundary
    assert provider.segment_forbidden((-1.0, -1.0), (3.0, -1.0)) is False
    assert provider.segment_forbidden((3.0, 1.0), (5.0, 1.0)) is False


def test_distance_nm_inside_and_outside():
    provider = AvoidancePolygonProvider([_avoid()])
    assert provider.distance_nm(1.0, 1.0) == 0.0
    assert provider.distance_nm(3.0, 1.0) == pytest.approx(60.0, rel=1e-6)
    assert provider.distance_nm(1.0, 3.0) == pytest.approx(
        60.0 * cos(radians(1.0)), rel=1e-6
    )
    assert AvoidancePolygonProvider([]).distance_nm(0.0, 0.0) == float("inf")


# ----------------------------------------------------------------------
# Element and collection persistence


def test_shape_to_map_shape_is_a_validated_snapshot():
    shape = Shape("Reef", SQUARE, kind=KIND_AVOID, note="coral", enabled=False)
    snapshot = shape.to_map_shape()
    assert isinstance(snapshot, MapShape)
    assert snapshot.id == shape.uid
    assert snapshot.kind == KIND_AVOID
    assert snapshot.vertices == SQUARE
    assert snapshot.note == "coral"
    assert snapshot.enabled is False

    shape.vertices = ((0.0, 0.0), (0.0, 1.0), (1.0, 1.0))
    assert snapshot.vertices == SQUARE


def test_shape_mutation_bumps_revision():
    shape = Shape("Reef", SQUARE, kind=KIND_AVOID)
    revision = shape.revision
    shape.vertices = ((0.0, 0.0), (0.0, 1.0), (1.0, 1.0))
    assert shape.revision > revision
    revision = shape.revision
    shape.enabled = False
    shape.note = "shoal"
    assert shape.revision > revision


def test_shape_requires_geometry_and_valid_kind_change():
    with pytest.raises(ShapeValidationError):
        Shape("Empty")
    line = Shape("Line", ((0.0, 0.0), (0.0, 1.0)), kind=KIND_NOTE)
    with pytest.raises(ShapeValidationError):
        line.kind = KIND_AVOID
    assert line.kind == KIND_NOTE


def test_collection_create_persists_element_data(monkeypatch):
    collection = _collection(monkeypatch)
    shape = collection.create(SQUARE, kind=KIND_AVOID, name="Reef", note="coral")

    assert len(collection) == 1
    assert collection.get_by_name("Reef") is shape
    stored = collection.storage.data["elements"][0]
    assert stored["uid"] == shape.uid
    assert stored["kind"] == KIND_AVOID
    assert stored["note"] == "coral"
    assert stored["enabled"] is True
    assert stored["vertices"] == [list(vertex) for vertex in SQUARE]

    provider = collection.avoidance_provider()
    assert len(provider) == 1
    assert provider.point_forbidden(1.0, 1.0) is True


def test_collection_round_trip_restores_identity_and_values(monkeypatch):
    collection = _collection(monkeypatch)
    shape = collection.create(
        SQUARE, kind=KIND_AVOID, name="Reef", note="coral", enabled=False
    )
    payload = collection.to_json()

    reloaded = _collection(monkeypatch)
    reloaded.load_json(payload)

    assert len(reloaded) == 1
    restored = reloaded[0]
    assert restored.uid == shape.uid
    assert restored.name == "Reef"
    assert restored.to_map_shape() == shape.to_map_shape()


def test_collection_skips_invalid_stored_shape(monkeypatch):
    collection = _collection(monkeypatch)
    collection.load_json(
        {
            "elements": [
                {"uid": "bad", "name": "bad", "kind": KIND_AVOID, "vertices": [[0, 0]]},
                {
                    "uid": "good",
                    "name": "ok",
                    "kind": KIND_NOTE,
                    "vertices": [[1.0, 2.0]],
                    "enabled": True,
                },
            ]
        }
    )
    assert [shape.uid for shape in collection] == ["good"]


def test_collection_provider_excludes_note_and_disabled_shapes(monkeypatch):
    collection = _collection(monkeypatch)
    collection.create(SQUARE, kind=KIND_AVOID, name="Reef")
    collection.create(((0.0, 0.0), (0.0, 1.0)), kind=KIND_NOTE, name="Note")
    collection.create(SQUARE, kind=KIND_AVOID, name="Off", enabled=False)

    provider = collection.avoidance_provider()
    assert len(provider) == 1
    assert provider.segment_forbidden((-1.0, 1.0), (3.0, 1.0)) is True


# ----------------------------------------------------------------------
# Scene registration


def test_scene_registers_shapes_in_default_group(tmp_path):
    scene = SceneCollection(tmp_path / "scene.json")
    shape = Shape("Reef", SQUARE, kind=KIND_AVOID)

    scene.reconcile(
        [
            SceneEntry(
                kind=KIND_SHAPE,
                key=shape.uid,
                name=shape.name,
                visible=shape.visible,
                element=shape,
            )
        ]
    )

    item = scene.item(KIND_SHAPE, shape.uid)
    assert item is not None
    assert item.group == "default-shapes"
    assert scene.group("default-shapes").name == "Shapes"
    assert scene.live_entry(KIND_SHAPE, shape.uid).element is shape


def test_scene_prunes_deleted_shapes_and_applies_visibility(tmp_path):
    scene = SceneCollection(tmp_path / "scene.json")
    shape = Shape("Reef", SQUARE, kind=KIND_AVOID)
    entry = SceneEntry(
        kind=KIND_SHAPE,
        key=shape.uid,
        name=shape.name,
        visible=shape.visible,
        element=shape,
    )
    scene.reconcile([entry])

    scene.set_item_visible(KIND_SHAPE, shape.uid, False)
    scene.apply_visibility()
    assert shape.visible is False

    scene.reconcile([])
    assert scene.item(KIND_SHAPE, shape.uid) is None
