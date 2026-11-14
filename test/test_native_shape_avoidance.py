"""The frozen avoid-zone contract between the parent and the native worker.

Enabled ``avoid`` polygons cross the process boundary as an ordered, canonical
``avoid_zones`` payload plus one SHA256 digest. Note-only and disabled shapes
stay inert, the ring encoding is independent of click order and winding, and
no worker-local Python polygon predicate is involved.
"""

import json

import pytest

from sailworks.core.geo.mapshapes import (
    AvoidZone,
    MapShape,
    ShapeValidationError,
    active_avoid_zones,
    avoid_zones_digest,
    canonical_vertices,
)

AVOID_VERTICES = (
    (48.25, -123.70),
    (48.25, -123.55),
    (48.35, -123.55),
    (48.35, -123.70),
)


def _avoid(identifier="avoid-1", enabled=True, name="Charted reef", vertices=None):
    return MapShape(
        id=identifier,
        name=name,
        kind="avoid",
        vertices=AVOID_VERTICES if vertices is None else vertices,
        enabled=enabled,
    )


def test_only_enabled_avoid_polygons_become_zones_in_selection_order():
    note = MapShape(id="note-1", name="Note", kind="note", vertices=((48.3, -123.6),))
    shapes = (
        note,
        _avoid("avoid-a"),
        _avoid("avoid-off", enabled=False),
        _avoid("avoid-b"),
    )

    zones = active_avoid_zones(shapes)

    assert [zone.id for zone in zones] == ["avoid-a", "avoid-b"]
    assert [zone.name for zone in zones] == ["Charted reef", "Charted reef"]
    assert all(isinstance(zone, AvoidZone) for zone in zones)


def test_no_active_zone_yields_no_payload_and_no_digest():
    note = MapShape(id="note-1", name="Note", kind="note", vertices=((48.3, -123.6),))

    for shapes in ((), (note,), (_avoid("off", enabled=False),)):
        assert active_avoid_zones(shapes) == ()
        assert avoid_zones_digest(active_avoid_zones(shapes)) == ""


def test_zone_payload_is_the_native_record():
    (zone,) = active_avoid_zones([_avoid()])

    assert zone.to_native() == {
        "id": "avoid-1",
        "name": "Charted reef",
        "vertices": [[lat, lon] for lat, lon in AVOID_VERTICES],
    }
    # Coordinates are floats, never ints or tuples, so JSON is stable.
    for vertex in zone.to_native()["vertices"]:
        assert all(type(value) is float for value in vertex)


def test_canonical_ring_ignores_rotation_and_winding():
    canonical = canonical_vertices(AVOID_VERTICES)

    assert canonical == AVOID_VERTICES
    assert canonical_vertices(AVOID_VERTICES[2:] + AVOID_VERTICES[:2]) == canonical
    assert canonical_vertices(tuple(reversed(AVOID_VERTICES))) == canonical
    # Idempotent: the frozen ring is already canonical.
    assert canonical_vertices(canonical) == canonical


def test_digest_is_deterministic_over_the_canonical_payload():
    zones = active_avoid_zones([_avoid()])
    digest = avoid_zones_digest(zones)

    assert digest == avoid_zones_digest(active_avoid_zones([_avoid()]))
    assert len(digest) == 64
    int(digest, 16)
    # The digest commits to the exact JSON encoding the bridge receives.
    payload = json.dumps(
        [zone.to_native() for zone in zones],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    import hashlib

    assert digest == hashlib.sha256(payload.encode("ascii")).hexdigest()


def test_digest_changes_with_identity_geometry_and_order():
    base = avoid_zones_digest(active_avoid_zones([_avoid()]))

    assert base != avoid_zones_digest(active_avoid_zones([_avoid(name="Other")]))
    assert base != avoid_zones_digest(
        active_avoid_zones(
            [_avoid(vertices=tuple((lat + 0.01, lon) for lat, lon in AVOID_VERTICES))]
        )
    )
    first, second = _avoid("avoid-a"), _avoid("avoid-b")
    assert avoid_zones_digest(active_avoid_zones([first, second])) != (
        avoid_zones_digest(active_avoid_zones([second, first]))
    )


def test_invalid_rings_are_rejected_before_they_can_be_frozen():
    bowtie = ((48.20, -123.70), (48.40, -123.60), (48.22, -123.60), (48.40, -123.70))
    duplicated = (
        (48.25, -123.70),
        (48.25, -123.70),
        (48.25, -123.55),
        (48.35, -123.55),
    )
    dateline = ((48.0, -179.0), (48.0, 10.0), (49.0, 10.0), (49.0, -179.0))

    for vertices, message in (
        (bowtie, "may not cross or touch"),
        (duplicated, "must be distinct"),
        (dateline, "longitude span exceeds 180 degrees"),
    ):
        with pytest.raises(ShapeValidationError, match=message):
            _avoid(vertices=vertices)


def test_canonical_vertices_requires_finite_coordinates():
    with pytest.raises(ShapeValidationError, match="not finite"):
        canonical_vertices(((48.0, float("nan")), (48.1, -123.0), (48.2, -123.1)))


def test_zone_identity_is_required():
    with pytest.raises(ShapeValidationError, match="non-empty id"):
        AvoidZone(id="", name="", vertices=AVOID_VERTICES)
