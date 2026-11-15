"""Focused checks for ENC mesh geometry construction (encmesh + encstroke).

These cover holes, multipolygons, concave areas, thick lines, area outlines,
mixed polygonal/lineal collections, source/feature order, point marker runs,
float64 seam precision and screen-constant stroke width.  Coverage clipping
and Qt item state live in :mod:`test_enc_mesh_coverage`.  They do not measure
performance or claim chart fidelity.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pytest
import shapely
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiPolygon,
    Point,
    Polygon,
    box,
)

from sailworks.rendering.vectordrawer.encmesh import (
    AREA_FILL,
    AREA_OUTLINE,
    LINE,
    MARKER,
    build_scene_mesh,
    stroke_lines,
    triangulate_area,
)

pytestmark = pytest.mark.skipif(
    not hasattr(shapely, "constrained_delaunay_triangles"),
    reason="GEOS constrained Delaunay triangulation unavailable",
)


@dataclass(frozen=True)
class _Record:
    layer_index: int
    sequence: int
    fid: int
    source_class: str
    wkb: bytes
    attributes: tuple = ()


def _scene(*specs):
    """A minimal stand-in for ``encscene.CellScene``: (layer, class, geometry)."""
    records = []
    for sequence, (layer_index, source_class, geometry) in enumerate(specs):
        records.append(
            _Record(
                layer_index=layer_index,
                sequence=sequence,
                fid=1000 + sequence,
                source_class=source_class,
                wkb=shapely.to_wkb(geometry),
            )
        )
    return SimpleNamespace(records=tuple(records))


def _triangles(source) -> np.ndarray:
    positions = getattr(source, "positions", source)
    assert positions.shape[0] % 3 == 0
    return positions.reshape(-1, 3, 2)


def _area(triangles: np.ndarray) -> float:
    a = triangles[:, 0]
    b = triangles[:, 1]
    c = triangles[:, 2]
    ab = b - a
    ac = c - a
    cross = ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0]
    return float(np.abs(cross).sum() / 2.0)


def _centroids(triangles: np.ndarray) -> np.ndarray:
    return triangles.mean(axis=1)


def test_area_with_hole_leaves_the_hole_empty():
    hole = [(3, 3), (7, 3), (7, 7), (3, 7)]
    polygon = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)], [hole])
    mesh = build_scene_mesh(
        _scene((0, "DEPARE", polygon)), stroke_width=0.1, area_outlines=False
    )
    assert [part.kind for part in mesh.parts] == [AREA_FILL]
    triangles = _triangles(mesh)
    assert len(triangles) > 1
    assert _area(triangles) == pytest.approx(polygon.area, rel=1e-5)
    centroids = _centroids(triangles)
    hole_polygon = Polygon(hole)
    assert not shapely.contains_xy(hole_polygon, centroids[:, 0], centroids[:, 1]).any()


def test_multipolygon_keeps_source_order_and_full_area():
    first = MultiPolygon([box(0, 0, 2, 2), box(5, 0, 7, 2)])
    second = box(0, 5, 2, 7)
    mesh = build_scene_mesh(
        _scene((0, "DEPARE", first), (0, "DEPARE", second)),
        stroke_width=0.1,
        area_outlines=False,
    )
    assert [part.sequence for part in mesh.parts] == [0, 1]
    assert [part.kind for part in mesh.parts] == [AREA_FILL, AREA_FILL]
    assert mesh.parts[0].first_vertex == 0
    assert mesh.parts[1].first_vertex == mesh.parts[0].vertex_count
    assert _area(_triangles(mesh)) == pytest.approx(first.area + second.area, rel=1e-5)
    first_positions = mesh.part_positions(mesh.parts[0])
    assert first_positions[:, 0].max() <= 7.0 + 1e-4
    assert first_positions[:, 1].max() <= 2.0 + 1e-4


def test_concave_area_triangles_stay_inside_the_boundary():
    concave = Polygon([(0, 0), (4, 0), (4, 1), (1, 1), (1, 4), (0, 4)])
    triangles = _triangles(triangulate_area(concave))
    assert _area(triangles) == pytest.approx(concave.area, rel=1e-5)
    for triangle in triangles:
        piece = Polygon(triangle)
        assert piece.difference(concave).area <= 1e-6 * concave.area


def test_thick_line_is_an_ordered_triangle_soup():
    triangles = _triangles(stroke_lines(LineString([(0, 0), (10, 0)]), 2.0))
    assert len(triangles) == 2
    assert triangles[:, :, 1].min() == pytest.approx(-1.0)
    assert triangles[:, :, 1].max() == pytest.approx(1.0)
    assert triangles[0, 0].tolist() == triangles[1, 0].tolist()


def test_area_outline_strokes_the_inner_ring():
    hole = [(3, 3), (7, 3), (7, 7), (3, 7)]
    polygon = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)], [hole])
    mesh = build_scene_mesh(_scene((0, "DEPARE", polygon)), stroke_width=0.2)
    assert [part.kind for part in mesh.parts] == [AREA_FILL, AREA_OUTLINE]
    outline = mesh.part_positions(mesh.parts[1])
    assert outline[:, 0].min() <= 3.1
    assert outline[:, 0].max() >= 6.9
    near_hole = (
        (np.abs(outline[:, 0] - 3.0) <= 0.2)
        & (outline[:, 1] > 3.0)
        & (outline[:, 1] < 7.0)
    )
    assert near_hole.any()


def test_parts_follow_cell_scene_record_order():
    mesh = build_scene_mesh(
        _scene(
            (0, "DEPARE", box(0, 0, 1, 1)),
            (1, "COALNE", LineString([(5, 5), (6, 6)])),
            (0, "DEPARE", box(3, 3, 4, 4)),
        ),
        stroke_width=0.1,
        area_outlines=False,
    )
    observed = [(part.layer_index, part.source_class, part.kind) for part in mesh.parts]
    assert observed == [
        (0, "DEPARE", AREA_FILL),
        (1, "COALNE", LINE),
        (0, "DEPARE", AREA_FILL),
    ]
    assert [part.sequence for part in mesh.parts] == [0, 1, 2]
    assert mesh.parts[1].first_vertex == mesh.parts[0].vertex_count


def test_point_feature_emits_a_marker_run():
    mesh = build_scene_mesh(_scene((0, "BOYLAT", Point(1, 1))), stroke_width=0.1)
    assert [part.kind for part in mesh.parts] == [MARKER]
    assert mesh.unsupported == ()
    assert mesh.is_complete
    assert not mesh.is_empty


def test_mixed_collection_keeps_both_polygonal_and_lineal_parts():
    mixed = GeometryCollection([box(0, 0, 2, 2), LineString([(5, 5), (6, 6)])])
    mesh = build_scene_mesh(
        _scene((0, "MIXED", mixed)), stroke_width=0.1, area_outlines=False
    )
    assert [part.kind for part in mesh.parts] == [AREA_FILL, LINE]
    assert mesh.unsupported == ()
    assert mesh.is_complete


def test_mixed_collection_with_line_first_keeps_source_order():
    mixed = GeometryCollection([LineString([(5, 5), (6, 6)]), box(0, 0, 2, 2)])
    mesh = build_scene_mesh(
        _scene((0, "MIXED", mixed)), stroke_width=0.1, area_outlines=False
    )
    assert [part.kind for part in mesh.parts] == [LINE, AREA_FILL]
    assert mesh.parts[0].first_vertex == 0


def test_mixed_collection_draws_area_then_the_point_marker():
    mixed = GeometryCollection([box(0, 0, 2, 2), Point(9, 9)])
    mesh = build_scene_mesh(
        _scene((0, "MIXED", mixed)), stroke_width=0.1, area_outlines=False
    )
    assert [part.kind for part in mesh.parts] == [AREA_FILL, MARKER]
    assert mesh.unsupported == ()
    assert mesh.is_complete


def test_positions_stay_float64_and_keep_seam_precision():
    seam = 12.345678901234567
    polygon = Polygon([(seam, 0.0), (seam + 1.0, 0.0), (seam + 1.0, 1.0), (seam, 1.0)])
    mesh = build_scene_mesh(
        _scene((0, "DEPARE", polygon)), stroke_width=0.1, area_outlines=False
    )
    assert mesh.positions.dtype == np.float64
    assert mesh.positions[:, 0].min() == seam
    assert mesh.bounds[0] == seam


def test_screen_unit_stroke_requires_a_projection():
    with pytest.raises(ValueError, match="requires a projection"):
        stroke_lines(LineString([(0, 0), (1, 0)]), stroke_pixels=2.0)
    with pytest.raises(ValueError, match="requires a projection"):
        build_scene_mesh(
            _scene((0, "COALNE", LineString([(0, 0), (1, 0)]))),
            stroke_pixels=2.0,
        )


def test_stroke_width_cannot_be_given_twice():
    with pytest.raises(ValueError, match="exactly one"):
        build_scene_mesh(
            _scene((0, "COALNE", LineString([(0, 0), (1, 0)]))),
            stroke_width=0.1,
            stroke_pixels=2.0,
            projection=lambda positions: positions,
        )


def test_screen_unit_stroke_uses_the_projection_scale():
    # The projection halves source coordinates, so a 2 px stroke is 4 source
    # units wide on the ground; it must not be drawn as 2 source units.
    projection = lambda positions: np.asarray(positions) * 0.5  # noqa: E731
    mesh = build_scene_mesh(
        _scene((0, "COALNE", LineString([(0, 0), (10, 0)]))),
        stroke_pixels=2.0,
        projection=projection,
        area_outlines=False,
    )
    positions = mesh.positions
    assert np.ptp(positions[:, 1]) == pytest.approx(4.0)


def test_palette_colours_parts_in_encounter_order():
    mesh = build_scene_mesh(
        _scene(
            (0, "DEPARE", box(0, 0, 2, 2)),
            (1, "COALNE", LineString([(5, 5), (6, 6)])),
            (0, "LNDARE", box(3, 3, 4, 4)),
        ),
        stroke_width=0.1,
        palette="cm93",
    )
    assert [part.kind for part in mesh.parts] == [
        AREA_FILL,
        AREA_OUTLINE,
        LINE,
        AREA_FILL,
        AREA_OUTLINE,
    ]
    # cm93: absent DRVAL1 reads as 0, so the DEPARE fill is the shallow band.
    assert mesh.parts[0].colour == (0x73, 0xB6, 0xEF, 255)
    assert mesh.parts[1].colour == (128, 153, 178, 153)  # contour stroke
    assert mesh.parts[2].colour == (0x52, 0x5A, 0x5C, 255)  # land stroke
    assert mesh.parts[3].colour == (0xC9, 0xB9, 0x7A, 255)  # land fill
    assert mesh.parts[4].colour == (0x52, 0x5A, 0x5C, 255)
    assert [part.sequence for part in mesh.parts] == [0, 0, 1, 2, 2]
    for part in mesh.parts:
        assert part.vertex_count > 0
        assert part.first_vertex + part.vertex_count <= mesh.vertex_count


def test_palette_depth_band_reads_drval_attributes():
    record = _Record(
        layer_index=0,
        sequence=0,
        fid=7,
        source_class="DEPARE",
        wkb=shapely.to_wkb(box(0, 0, 2, 2)),
        attributes=(("DRVAL1", 30.0), ("DRVAL2", 50.0)),
    )
    mesh = build_scene_mesh(
        SimpleNamespace(records=(record,)),
        stroke_width=0.1,
        area_outlines=False,
        palette="cm93",
    )
    assert [part.kind for part in mesh.parts] == [AREA_FILL]
    assert mesh.parts[0].colour == (212, 235, 237, 255)  # deep band


def test_palette_is_opt_in_and_leaves_positions_untouched():
    plain = build_scene_mesh(
        _scene((0, "DEPARE", box(0, 0, 2, 2))),
        stroke_width=0.1,
        area_outlines=False,
    )
    styled = build_scene_mesh(
        _scene((0, "DEPARE", box(0, 0, 2, 2))),
        stroke_width=0.1,
        area_outlines=False,
        palette="cm93",
    )
    assert all(part.colour is None for part in plain.parts)
    assert all(part.colour is not None for part in styled.parts)
    np.testing.assert_array_equal(plain.positions, styled.positions)


def _tri_area(vertices):
    triangles = np.asarray(vertices, dtype=np.float64).reshape(-1, 3, 2)
    first = triangles[:, 1] - triangles[:, 0]
    second = triangles[:, 2] - triangles[:, 0]
    cross = first[:, 0] * second[:, 1] - first[:, 1] * second[:, 0]
    return float(np.abs(cross).sum() * 0.5)


def test_simplify_reduces_triangles_and_preserves_the_hole():
    """The drawer's tolerance is applied before tessellation, not after.

    A many-vertex polygon with a hole keeps its hole under
    ``SimplifyPreserveTopology``, so the triangulated area still excludes the
    hole while the triangle count falls with the tolerance.
    """
    ring = list(Point(0.0, 0.0).buffer(4.0, quad_segs=48).exterior.coords)
    hole = list(Point(0.0, 0.0).buffer(1.0, quad_segs=32).exterior.coords)
    polygon = Polygon(ring, [hole])
    scene = _scene((0, "DEPARE", polygon))

    plain = build_scene_mesh(scene, stroke_width=0.01)
    simplified = build_scene_mesh(scene, stroke_width=0.01, simplify=0.05)

    assert simplified.triangle_count < plain.triangle_count
    assert simplified.vertex_count < plain.vertex_count

    covered = _tri_area(simplified.positions)
    outer_area = Polygon(ring).area
    assert covered < outer_area - 0.5
    assert abs(covered - polygon.area) < 2.0
    assert shapely.is_valid(shapely.simplify(polygon, 0.05, preserve_topology=True))


def test_simplify_zero_is_a_no_op():
    polygon = box(0.0, 0.0, 1.0, 1.0)
    scene = _scene((0, "DEPARE", polygon))
    plain = build_scene_mesh(scene, stroke_width=0.01)
    explicit = build_scene_mesh(scene, stroke_width=0.01, simplify=0.0)
    np.testing.assert_array_equal(plain.positions, explicit.positions)
