"""Focused checks for the exact stroke fast path and area triangulation.

The stroke fast path must not change clipped geometry: an interior quad is
emitted unchanged, a quad that crosses the region edge is still clipped
exactly.  Ear clipping must satisfy the same contract as the GEOS constrained
Delaunay path (holes excluded, area reconciled), and the area gate must still
fire when a triangulation under-covers.  Qt-free; no performance claims.
"""

from __future__ import annotations

import numpy as np
import pytest
import shapely
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Polygon, box
from shapely.ops import unary_union

from sailworks.rendering.vectordrawer import _earcut, encmesh, encstroke
from sailworks.rendering.vectordrawer.encstroke import (
    UnsupportedGeometry,
    stroke_lines,
)


def _area(vertices) -> float:
    vertices = np.asarray(vertices, dtype=np.float64)
    total = 0.0
    for index in range(0, len(vertices), 3):
        a, b, c = vertices[index], vertices[index + 1], vertices[index + 2]
        total += abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
    return total / 2.0


def _triangles(vertices) -> list:
    return [
        Polygon([vertices[index], vertices[index + 1], vertices[index + 2]])
        for index in range(0, len(vertices), 3)
    ]


def _winding_is_ccw(vertices) -> bool:
    for index in range(0, len(vertices), 3):
        a, b, c = vertices[index], vertices[index + 1], vertices[index + 2]
        cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if cross <= 0.0:
            return False
    return True


# --- A: exact stroke fast path -------------------------------------------


def test_fully_interior_segment_emits_the_unclipped_two_triangle_soup():
    line = LineString([(2.0, 5.0), (8.0, 5.0)])
    plain = stroke_lines(line, 0.5)
    clipped = stroke_lines(line, 0.5, region=box(0.0, 0.0, 10.0, 10.0))

    assert plain.shape == (6, 2)  # one quad, two triangles
    assert clipped.shape == (6, 2)  # vertex count unchanged by the fast path
    assert np.array_equal(clipped, plain)
    assert _area(clipped) == pytest.approx(3.0)  # 6 long, 0.5 wide


def test_quad_touching_the_region_edge_takes_the_fast_path():
    # The expanded quad lies along the x = 5 line and its top edge is exactly
    # on the region boundary: ``covers`` accepts it, ``contains`` would not.
    region = box(0.0, 0.0, 10.0, 10.0)
    vertices = stroke_lines(LineString([(5.0, 10.0), (5.0, 6.0)]), 2.0, region=region)

    assert vertices.shape == (6, 2)
    assert _area(vertices) == pytest.approx(8.0)  # 4 long, 2 wide
    drawn = unary_union(_triangles(vertices))
    assert drawn.difference(region).area == pytest.approx(0.0, abs=1e-12)


def test_boundary_straddling_segment_is_still_clipped_exactly():
    region = box(0.0, 0.0, 10.0, 10.0)
    vertices = stroke_lines(LineString([(5.0, 5.0), (12.0, 5.0)]), 0.5, region=region)

    # Only x = 5..10 of the stroke survives: 5 long, 0.5 wide.
    assert _area(vertices) == pytest.approx(2.5)
    drawn = unary_union(_triangles(vertices))
    assert drawn.difference(region).area == pytest.approx(0.0, abs=1e-12)
    assert drawn.area == pytest.approx(2.5)


def test_shapely_2_0_unprepared_fallback_matches_the_prepared_path(monkeypatch):
    region = box(0.0, 0.0, 10.0, 10.0)
    line = LineString([(2.0, 5.0), (8.0, 5.0)])
    prepared = stroke_lines(line, 0.5, region=region)

    monkeypatch.setattr(encstroke, "_prepare_region", lambda geometry: None)
    unprepared = stroke_lines(line, 0.5, region=region)

    # Same quad, possibly a different CDT vertex order: compare the drawn
    # geometry, not the vertex array.  An interior quad stays two triangles and
    # must not leave the region either way.
    assert prepared.shape == (6, 2)
    assert unprepared.shape == (6, 2)
    assert _area(unprepared) == pytest.approx(_area(prepared))
    assert _area(prepared) == pytest.approx(3.0)  # 6 long, 0.5 wide
    prepared_drawn = unary_union(_triangles(prepared))
    unprepared_drawn = unary_union(_triangles(unprepared))
    assert unprepared_drawn.symmetric_difference(prepared_drawn).area == pytest.approx(
        0.0, abs=1e-12
    )
    assert unprepared_drawn.difference(region).area == pytest.approx(0.0, abs=1e-12)

    crossing = LineString([(5.0, 5.0), (12.0, 5.0)])
    exact = stroke_lines(crossing, 0.5, region=region)
    monkeypatch.undo()
    assert _area(stroke_lines(crossing, 0.5, region=region)) == pytest.approx(
        _area(exact)
    )


# --- B: earclipped area triangulation ------------------------------------


def test_earcut_matches_geos_for_a_polygon_with_a_hole(monkeypatch):
    polygon = Polygon(
        [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)],
        holes=[[(2.0, 2.0), (4.0, 2.0), (4.0, 4.0), (2.0, 4.0)]],
    )
    geos = encmesh.triangulate_area(polygon)
    monkeypatch.setattr(encmesh, "_EARCLIP_MAX_VERTICES", 10**9)
    earcut = encmesh.triangulate_area(polygon)

    assert _winding_is_ccw(geos)
    assert _winding_is_ccw(earcut)
    assert _area(geos) == pytest.approx(polygon.area)
    assert _area(earcut) == pytest.approx(polygon.area)
    assert _area(earcut) == pytest.approx(_area(geos), rel=1e-9)
    # The hole is excluded, not merely covered by overlapping triangles.
    hole = Polygon([(2.2, 2.2), (3.8, 2.2), (3.8, 3.8), (2.2, 3.8)])
    for triangulation in (geos, earcut):
        assert unary_union(_triangles(triangulation)).intersection(hole).area == 0.0


def test_earcut_matches_geos_for_a_multipolygon(monkeypatch):
    multipolygon = MultiPolygon([box(0.0, 0.0, 2.0, 2.0), box(5.0, 5.0, 7.0, 7.0)])
    geos = encmesh.triangulate_area(multipolygon)
    monkeypatch.setattr(encmesh, "_EARCLIP_MAX_VERTICES", 10**9)
    earcut = encmesh.triangulate_area(multipolygon)

    assert _area(geos) == pytest.approx(multipolygon.area)
    assert _area(earcut) == pytest.approx(multipolygon.area)
    assert _area(earcut) == pytest.approx(_area(geos), rel=1e-9)


def test_earcut_module_triangulates_a_square():
    indices = _earcut.earcut([0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0])
    assert len(indices) == 6
    vertices = np.asarray([0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0]).reshape(-1, 2)
    assert _area(vertices[np.asarray(indices)]) == pytest.approx(1.0)


def test_self_intersecting_polygon_is_repaired_not_drawn_wrong():
    bow_tie = Polygon([(0.0, 0.0), (2.0, 2.0), (2.0, 0.0), (0.0, 2.0)])
    repaired = shapely.make_valid(bow_tie)
    assert repaired.area == pytest.approx(2.0)

    try:
        vertices = encmesh.triangulate_area(bow_tie)
    except UnsupportedGeometry:
        return  # reporting the feature as unsupported is also acceptable

    assert _area(vertices) == pytest.approx(repaired.area)
    drawn = unary_union(_triangles(vertices))
    assert drawn.difference(repaired).area == pytest.approx(0.0, abs=1e-9)


def test_area_gate_still_fires_when_a_triangulation_under_covers(monkeypatch):
    square = box(0.0, 0.0, 4.0, 4.0)  # four vertices: the ear-clip path
    monkeypatch.setattr(
        encmesh._earcut, "earcut", lambda data, holes=None, dim=2: [0, 1, 2]
    )

    with pytest.raises(UnsupportedGeometry, match="does not match the polygon area"):
        encmesh.triangulate_area(square)


# --- C: dashed extraction matches shapely.ops.substring ------------------


def _identity(values):
    return values


def _substring_parts(line, on, off):
    """The pre-optimisation dash split, verbatim, as a reference oracle."""
    from shapely.ops import substring

    coordinates = np.asarray(line.coords, dtype=np.float64)
    screen = _identity(coordinates)
    screen_length = float(np.hypot(*np.diff(screen, axis=0).T).sum())
    source_length = float(line.length)
    ratio = source_length / screen_length
    runs = []
    position = 0.0
    while position < source_length:
        end = min(position + on * ratio, source_length)
        piece = substring(line, position, end)
        if not piece.is_empty and len(piece.coords) >= 2:
            runs.append(piece)
        position = end + off * ratio
    return MultiLineString(runs)


@pytest.mark.parametrize(
    "coordinates",
    [
        [(0.0, 0.0), (3.0, 0.0), (6.0, 0.0), (9.0, 0.0)],
        [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)],
        [(0.0, 0.0), (1.0, 1.0), (2.0, 0.0), (3.0, 1.0), (4.0, 0.0)],
        [(2.5, 7.5), (12.5, 3.25), (4.0, -6.0)],
    ],
)
@pytest.mark.parametrize("dash", [(3.0, 2.0), (1.0, 0.5), (0.7, 0.0), (100.0, 1.0)])
def test_dashed_stroke_matches_the_substring_reference_exactly(coordinates, dash):
    line = LineString(coordinates)
    reference = stroke_lines(_substring_parts(line, *dash), 0.5)
    actual = stroke_lines(line, 0.5, projection=_identity, dash=dash)
    assert actual.shape == reference.shape
    assert np.array_equal(actual, reference)


def test_dash_cut_on_a_vertex_is_emitted_once():
    # Dash length 3 and vertices every 3 units: every cut lands on a vertex,
    # and the shared vertex belongs to the first run only.
    line = LineString([(0.0, 0.0), (3.0, 0.0), (6.0, 0.0), (9.0, 0.0)])
    parts = encstroke._dashed_parts(line, (3.0, 3.0), _identity)
    assert parts is not None
    assert [part.shape[0] for part in parts] == [2, 2]  # no repeated cut vertex
    assert np.array_equal(
        np.concatenate(parts),
        np.asarray(((0, 0), (3, 0), (6, 0), (9, 0)), dtype=np.float64),
    )


def test_stroke_clipping_handles_a_concave_region():
    region = Polygon(
        [
            (0.0, 0.0),
            (10.0, 0.0),
            (10.0, 10.0),
            (6.0, 10.0),
            (5.0, 4.0),
            (4.0, 10.0),
            (0.0, 10.0),
        ]
    )
    line = LineString([(0.5, 7.0), (9.5, 7.0)])
    vertices = stroke_lines(line, 0.4, region=region)

    drawn = unary_union(_triangles(vertices))
    expected = unary_union(_triangles(stroke_lines(line, 0.4))).intersection(region)
    assert drawn.area == pytest.approx(expected.area)
    assert drawn.difference(region).area == pytest.approx(0.0, abs=1e-12)
    # The notch splits the clipped stroke into more than one piece.
    assert len(_triangles(vertices)) > 2
