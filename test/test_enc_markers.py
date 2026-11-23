"""Focused checks for procedural ENC point markers (``encmarkers`` + mesh).

These cover the triangle templates' shape and extents against the Cairo point
symbols, the ink of the stroked shapes (half the line width beyond each path),
the lateral-buoy disc and its port/starboard colour, BOYLAT ``COLOUR``/
``CATLAM`` remapping, source order, the optional per-camera scale gate and the
generic fallback for unknown point classes.  They are Qt-free, apart from one
cross-check of the palette against the drawer's own styles, and do not measure
performance or claim chart fidelity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pytest
import shapely
from shapely.geometry import GeometryCollection, MultiPoint, Point, box

from sailworks.rendering.vectordrawer import encmarkers, encpalette
from sailworks.rendering.vectordrawer.encmesh import AREA_FILL, MARKER, build_scene_mesh

O = 0.5 / np.sqrt(2.0)
#: Cairo extents from ``cm93chartdrawer``.  Stroked shapes include the stroke's
#: half width (beacon 1.5, wreck 1.0, landmark 1.0); the beacon's mitered
#: corners are computed from the path inradius, so only its extents are
#: approximated here.
EXTENTS = {
    encmarkers.LIGHT: ((-7.0, -7.0, 7.0, 7.0), 1e-9),
    encmarkers.BEACON: ((-5.2135, -6.6770, 5.2135, 3.75), 1e-3),
    encmarkers.BUOY: ((-3.0, -3.0, 3.0, 3.0), 1e-9),
    encmarkers.BUOY_PORT: ((-3.0, -3.0, 3.0, 3.0), 1e-9),
    encmarkers.BUOY_STARBOARD: ((-3.0, -3.0, 3.0, 3.0), 1e-9),
    encmarkers.WRECK: ((-3.0 - O, -3.0 - O, 3.0 + O, 3.0 + O), 1e-9),
    encmarkers.LANDMARK: ((-2.5, -2.5, 2.5, 2.5), 1e-9),
    encmarkers.DOT: ((-1.5, -1.5, 1.5, 1.5), 1e-9),
}

TRIANGLE_COUNTS = {
    encmarkers.LIGHT: 8 + 8,
    encmarkers.BEACON: 6,
    encmarkers.BUOY: 8,
    encmarkers.BUOY_PORT: 8,
    encmarkers.BUOY_STARBOARD: 8,
    encmarkers.WRECK: 4,
    encmarkers.LANDMARK: 8,
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
def test_template_extents_match_the_drawer_shapes(kind):
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


def test_multipoint_collapses_to_its_first_point():
    """Cairo draws ``geometry[0]`` only: a MultiPoint gets one symbol."""
    template = _template(encmarkers.BUOY)
    mesh = build_scene_mesh(
        _scene(("BOYSPP", MultiPoint([(0, 0), (5, 5)]))), stroke_width=0.1
    )
    assert len(mesh.parts) == 1
    positions = mesh.part_positions(mesh.parts[0]).reshape(-1, 2)
    assert positions == pytest.approx(template)
    assert mesh.parts[0].triangle_count == TRIANGLE_COUNTS[encmarkers.BUOY]


def test_collapsed_multipoint_keeps_the_marker_scale():
    """Collapsing to the first point must not skip ``marker_scale``."""
    template = _template(encmarkers.BUOY)
    mesh = build_scene_mesh(
        _scene(("BOYSPP", MultiPoint([(0, 0), (5, 5)]))),
        stroke_width=0.1,
        marker_scale=2.0,
    )
    assert len(mesh.parts) == 1
    assert mesh.part_positions(mesh.parts[0]) == pytest.approx(template * 2.0)


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


def _inradius(triangle) -> float:
    (ax, ay), (bx, by), (cx, cy) = triangle
    side_a = math.hypot(bx - cx, by - cy)
    side_b = math.hypot(ax - cx, ay - cy)
    side_c = math.hypot(ax - bx, ay - by)
    area = abs((bx - ax) * (cy - ay) - (cx - ax) * (by - ay)) / 2.0
    return 2.0 * area / (side_a + side_b + side_c)


def _scaled_outline(path, factor):
    """``path`` scaled about its incenter by ``factor``, as a tuple."""
    (ax, ay), (bx, by), (cx, cy) = path
    opposite_a = math.hypot(bx - cx, by - cy)
    opposite_b = math.hypot(ax - cx, ay - cy)
    opposite_c = math.hypot(ax - bx, ay - by)
    perimeter = opposite_a + opposite_b + opposite_c
    center_x = (opposite_a * ax + opposite_b * bx + opposite_c * cx) / perimeter
    center_y = (opposite_a * ay + opposite_b * by + opposite_c * cy) / perimeter
    return tuple(
        (
            center_x + (x - center_x) * factor,
            center_y + (y - center_y) * factor,
        )
        for x, y in path
    )


def _covers_point(template, point) -> bool:
    """Whether any triangle of ``template`` contains ``point``."""
    px, py = point
    for (ax, ay), (bx, by), (cx, cy) in template.reshape(-1, 3, 2):
        denominator = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if denominator == 0.0:
            continue
        first = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / denominator
        second = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / denominator
        third = 1.0 - first - second
        if first >= 0.0 and second >= 0.0 and third >= 0.0:
            return True
    return False


def test_lateral_buoys_are_the_generic_disc_in_their_side_colour():
    """Cairo fills the r=3 disc for BOYLAT, in red or green, not a chevron."""
    disc = _template(encmarkers.BUOY)
    assert _template(encmarkers.BUOY_PORT) == pytest.approx(disc)
    assert _template(encmarkers.BUOY_STARBOARD) == pytest.approx(disc)
    assert _bounds(encmarkers.BUOY_PORT) == pytest.approx((-3.0, -3.0, 3.0, 3.0))
    assert encpalette.marker_colour(encmarkers.BUOY_PORT) != encpalette.marker_colour(
        encmarkers.BUOY_STARBOARD
    )


def test_light_rays_are_the_half_pixel_stroke():
    template = _template(encmarkers.LIGHT)
    rays = template[encmarkers.DISC_SEGMENTS * 3 :].reshape(-1, 3, 2)
    assert len(rays) == 8
    angles = []
    for base_a, base_b, tip in rays:
        # Cairo draws from r=4 to r=7 with ``set_line_width(0.5)``: the two
        # base corners straddle radius 4, and the tip is the stroke's end.
        base_midpoint = (base_a + base_b) / 2.0
        assert np.hypot(*base_midpoint) == pytest.approx(4.0)
        assert np.hypot(*tip) == pytest.approx(7.0)
        assert np.hypot(*(base_a - base_b)) == pytest.approx(0.5)
        angles.append(math.degrees(math.atan2(tip[1], tip[0])) % 360.0)
    assert angles == pytest.approx([0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0])


def test_beacon_is_the_hollow_stroke():
    """Cairo strokes the 1.5 px triangle: a 0.75 px band either side, hollow."""
    template = _template(encmarkers.BEACON)
    # The Cairo path: apex (0, -5), base corners (+/-4, 3), closed and stroked.
    path_inradius = 2.0 * 32.0 / (8.0 + 2.0 * math.hypot(4.0, 8.0))
    xs = template[:, 0]
    ys = template[:, 1]
    assert max(xs) == pytest.approx(-min(xs))
    assert max(ys) == pytest.approx(3.75)
    assert min(ys) == pytest.approx(-6.677, abs=1e-3)
    # The centre is unpainted; the band covers both stroke boundaries.
    assert not _covers_point(template, (0.0, 0.0))
    assert _covers_point(template, (0.0, -5.0))
    assert _covers_point(template, (0.0, 3.0))
    assert _covers_point(template, (-4.0, 3.0))
    # The ring's unique vertices are exactly Cairo's two stroke boundaries:
    # the path dilated and eroded by half the 1.5 px line width.
    path = ((0.0, -5.0), (-4.0, 3.0), (4.0, 3.0))
    inner = _scaled_outline(path, 1.0 - 0.75 / path_inradius)
    outer = _scaled_outline(path, 1.0 + 0.75 / path_inradius)
    unique = np.unique(template, axis=0)
    edges = np.unique(np.asarray(inner + outer), axis=0)
    assert unique.shape == edges.shape == (6, 2)
    assert np.allclose(unique, edges, atol=1e-9)


def test_landmark_is_the_hollow_square():
    """Cairo strokes a 4x4 square 1 px wide: a ring, not a filled block."""
    template = _template(encmarkers.LANDMARK)
    assert _bounds(encmarkers.LANDMARK) == pytest.approx((-2.5, -2.5, 2.5, 2.5))
    assert not _covers_point(template, (0.0, 0.0))
    top_bar = template[:6]
    assert float(top_bar[:, 1].max() - top_bar[:, 1].min()) == pytest.approx(1.0)
    # The corners belong to one bar only, so the four bars tile the ring.
    assert _covers_point(template, (2.0, -2.0))
    assert _covers_point(template, (2.0, 0.0))
    assert not _covers_point(template, (0.5, 0.5))


def test_marker_colours_match_the_point_styles():
    """``encpalette`` must mirror the drawer's module-level point styles."""
    from sailworks.rendering.vectordrawer import cm93chartdrawer

    mirrored = {
        encmarkers.LIGHT: cm93chartdrawer.LIGHT_STYLE,
        encmarkers.BEACON: cm93chartdrawer.BEACON_STYLE,
        encmarkers.BUOY: cm93chartdrawer.DEFAULT_POINT,
        encmarkers.BUOY_PORT: cm93chartdrawer.BUOY_RED,
        encmarkers.BUOY_STARBOARD: cm93chartdrawer.BUOY_GREEN,
        encmarkers.WRECK: cm93chartdrawer.WRECK_STYLE,
        encmarkers.LANDMARK: cm93chartdrawer.BEACON_STYLE,
        encmarkers.DOT: cm93chartdrawer.DEFAULT_POINT,
    }
    for kind, style in mirrored.items():
        assert encpalette.marker_colour(kind) == encpalette.rgba(style), kind
