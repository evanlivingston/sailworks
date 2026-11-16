"""Focused checks for procedural ENC point markers (``encmarkers`` + mesh).

These cover the triangle templates' shape and extents against the Cairo point
symbols, BOYLAT ``COLOUR``/``CATLAM`` remapping, source order, the optional
per-camera scale gate and the generic fallback for unknown point classes.
They are Qt-free and do not measure performance or claim chart fidelity.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pytest
import shapely
from shapely.geometry import GeometryCollection, MultiPoint, Point, box

from sailworks.rendering.vectordrawer import encmarkers, encpalette
from sailworks.rendering.vectordrawer.encmesh import AREA_FILL, MARKER, build_scene_mesh

O = 0.5 / np.sqrt(2.0)
#: Cairo extents from ``cm93chartdrawer``; stroke-derived shapes allow 0.4 px
#: for the filled-bar approximation.
EXTENTS = {
    encmarkers.LIGHT: ((-7.0, -7.0, 7.0, 7.0), 1e-9),
    encmarkers.BEACON: ((-4.0, -5.0, 4.0, 3.0), 1e-9),
    encmarkers.BUOY: ((-3.0, -3.0, 3.0, 3.0), 1e-9),
    encmarkers.BUOY_PORT: ((-3.0, -3.0, 3.0, 3.6), 1e-9),
    encmarkers.BUOY_STARBOARD: ((-3.0, -3.0, 3.0, 3.6), 1e-9),
    encmarkers.WRECK: ((-3.0 - O, -3.0 - O, 3.0 + O, 3.0 + O), 1e-9),
    encmarkers.LANDMARK: ((-2.0, -2.0, 2.0, 2.0), 1e-9),
    encmarkers.DOT: ((-1.5, -1.5, 1.5, 1.5), 1e-9),
}

TRIANGLE_COUNTS = {
    encmarkers.LIGHT: 8 + 8,
    encmarkers.BEACON: 1,
    encmarkers.BUOY: 8,
    encmarkers.BUOY_PORT: 4,
    encmarkers.BUOY_STARBOARD: 4,
    encmarkers.WRECK: 4,
    encmarkers.LANDMARK: 2,
    encmarkers.DOT: 8,
}


@dataclass(frozen=True)
class _Record:
    layer_index: int
    sequence: int
    fid: int
    source_class: str
    wkb: bytes
    attributes: tuple = ()


def _scene(*specs):
    """A minimal ``CellScene`` stand-in: ``(class, geometry[, attributes])``."""
    records = []
    for sequence, spec in enumerate(specs):
        source_class, geometry = spec[0], spec[1]
        attributes = spec[2] if len(spec) > 2 else ()
        records.append(
            _Record(
                layer_index=0,
                sequence=sequence,
                fid=1000 + sequence,
                source_class=source_class,
                wkb=shapely.to_wkb(geometry),
                attributes=attributes,
            )
        )
    return SimpleNamespace(records=tuple(records))


def _template(kind) -> np.ndarray:
    return np.asarray(encmarkers.triangles(kind), dtype=np.float64).reshape(-1, 2)


def _bounds(kind):
    vertices = _template(kind)
    return (
        float(vertices[:, 0].min()),
        float(vertices[:, 1].min()),
        float(vertices[:, 0].max()),
        float(vertices[:, 1].max()),
    )


@pytest.mark.parametrize("kind", encmarkers.KINDS)
def test_every_kind_has_three_point_triangles(kind):
    assert all(len(triangle) == 3 for triangle in encmarkers.triangles(kind))
    assert len(encmarkers.triangles(kind)) == TRIANGLE_COUNTS[kind]


@pytest.mark.parametrize("kind", encmarkers.KINDS)
def test_template_extents_match_the_cairo_shapes(kind):
    expected, tolerance = EXTENTS[kind]
    assert _bounds(kind) == pytest.approx(expected, abs=tolerance)


def test_unknown_template_kind_raises():
    with pytest.raises(ValueError, match="unknown marker kind"):
        encmarkers.triangles("not-a-marker")


@pytest.mark.parametrize(
    ("source_class", "expected"),
    [
        ("LIGHTS", encmarkers.LIGHT),
        ("BCNCAR", encmarkers.BEACON),
        ("BCNSPP", encmarkers.BEACON),
        ("BOYSPP", encmarkers.BUOY),
        ("WRECKS", encmarkers.WRECK),
        ("OBSTRN", encmarkers.WRECK),
        ("LNDMRK", encmarkers.LANDMARK),
        ("TOWERS", encmarkers.LANDMARK),
        ("SOUNDG", None),
        ("SEAARE", encmarkers.DOT),
    ],
)
def test_classify_matches_the_drawer_sets(source_class, expected):
    assert encmarkers.classify(source_class) == expected


def test_boylat_colour_remap_and_generic_fallback():
    assert encmarkers.classify("BOYLAT", ["3"]) == encmarkers.BUOY_PORT
    assert encmarkers.classify("BOYLAT", [4]) == encmarkers.BUOY_STARBOARD
    assert encmarkers.classify("BOYLAT", ["1"]) == encmarkers.BUOY
    assert encmarkers.classify("BOYLAT", []) == encmarkers.BUOY
    assert encmarkers.classify("BOYLAT") == encmarkers.BUOY


def test_point_marker_is_translated_to_the_anchor_in_source_order():
    mesh = build_scene_mesh(
        _scene(
            ("DEPARE", box(0, 0, 2, 2)),
            ("LIGHTS", Point(10, 20)),
            ("BOYLAT", Point(30, 40), (("COLOUR", [3]),)),
        ),
        stroke_width=0.1,
        area_outlines=False,
    )
    assert [part.kind for part in mesh.parts] == [AREA_FILL, MARKER, MARKER]
    assert [part.sequence for part in mesh.parts] == [0, 1, 2]

    light = mesh.part_positions(mesh.parts[1])
    assert light == pytest.approx(_template(encmarkers.LIGHT) + (10.0, 20.0))
    assert mesh.parts[1].triangle_count == TRIANGLE_COUNTS[encmarkers.LIGHT]

    port = mesh.part_positions(mesh.parts[2])
    assert port == pytest.approx(_template(encmarkers.BUOY_PORT) + (30.0, 40.0))


def test_point_first_collection_draws_the_area_then_the_marker():
    mixed = GeometryCollection([Point(9, 9), box(0, 0, 2, 2)])
    mesh = build_scene_mesh(
        _scene(("DEPARE", mixed)), stroke_width=0.1, area_outlines=False
    )
    assert [part.kind for part in mesh.parts] == [AREA_FILL, MARKER]


def test_multipoint_is_one_contiguous_run():
    template = _template(encmarkers.BUOY)
    mesh = build_scene_mesh(
        _scene(("BOYSPP", MultiPoint([(0, 0), (5, 5)]))), stroke_width=0.1
    )
    assert len(mesh.parts) == 1
    triangles = mesh.part_positions(mesh.parts[0]).reshape(-1, 3, 2)
    assert triangles[:8].reshape(-1, 2) == pytest.approx(template)
    assert triangles[8:].reshape(-1, 2) == pytest.approx(template + (5.0, 5.0))


@pytest.mark.parametrize(
    ("source_class", "kind"),
    [
        ("LIGHTS", encmarkers.LIGHT),
        ("BCNLAT", encmarkers.BEACON),
        ("BOYSPP", encmarkers.BUOY),
        ("BOYLAT", encmarkers.BUOY),
        ("WRECKS", encmarkers.WRECK),
        ("LNDMRK", encmarkers.LANDMARK),
        ("NOTACLASS", encmarkers.DOT),
    ],
)
def test_marker_part_carries_the_palette_colour(source_class, kind):
    mesh = build_scene_mesh(
        _scene((source_class, Point(0, 0))), stroke_width=0.1, palette="cm93"
    )
    assert mesh.parts[0].kind == MARKER
    assert mesh.parts[0].colour == encpalette.marker_colour(kind)


def test_marker_colour_is_opt_in():
    mesh = build_scene_mesh(_scene(("LIGHTS", Point(0, 0))), stroke_width=0.1)
    assert mesh.parts[0].colour is None


def test_boylat_mesh_colour_follows_the_remap():
    scene = _scene(("BOYLAT", Point(0, 0), (("COLOUR", [3]),)))
    mesh = build_scene_mesh(scene, stroke_width=0.1, palette="cm93")
    assert mesh.parts[0].colour == encpalette.marker_colour(encmarkers.BUOY_PORT)


def test_generic_dot_is_gated_only_when_a_scale_is_supplied():
    scene = _scene(("SEAARE", Point(1, 1)))
    assert build_scene_mesh(scene, stroke_width=0.1).parts[0].kind == MARKER
    assert build_scene_mesh(scene, stroke_width=0.1, scale=50.0).parts[0].kind == MARKER
    assert build_scene_mesh(scene, stroke_width=0.1, scale=200.0).parts == ()


def test_other_marker_kinds_are_not_scale_gated():
    scene = _scene(("LIGHTS", Point(1, 1)))
    mesh = build_scene_mesh(scene, stroke_width=0.1, scale=5000.0)
    assert [part.kind for part in mesh.parts] == [MARKER]


def test_unknown_point_class_renders_the_generic_dot_without_crashing():
    mesh = build_scene_mesh(_scene(("NOTACLASS", Point(3, 4))), stroke_width=0.1)
    assert mesh.unsupported == ()
    part = mesh.parts[0]
    assert part.kind == MARKER
    assert part.triangle_count == TRIANGLE_COUNTS[encmarkers.DOT]
    vertices = mesh.part_positions(part)
    assert any(np.allclose(vertex, (3.0, 4.0)) for vertex in vertices)


def test_soundings_are_reported_not_drawn():
    mesh = build_scene_mesh(_scene(("SOUNDG", Point(1, 1))), stroke_width=0.1)
    assert mesh.parts == ()
    assert "sounding" in mesh.unsupported[0].reason


def test_marker_scale_converts_template_pixels_to_source_units():
    scene = _scene(("LIGHTS", Point(0, 0)))
    mesh = build_scene_mesh(scene, stroke_width=0.1, marker_scale=2.0)
    assert mesh.positions == pytest.approx(_template(encmarkers.LIGHT) * 2.0)


def test_non_positive_marker_scale_is_rejected():
    with pytest.raises(ValueError, match="marker_scale"):
        build_scene_mesh(
            _scene(("LIGHTS", Point(0, 0))), stroke_width=0.1, marker_scale=0.0
        )
