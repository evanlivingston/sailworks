"""The S57 drawer suppresses only the DEPARE outlines on cell coverage.

Same-scale quilted ENC cells meet exactly along their coverage boundaries,
and every cell strokes its own depth areas right up to that boundary, so
the join shows a doubled outline where no real contour exists.  The fix
suppresses the stroke of the DEPARE segments that lie on the cell's own
coverage boundary and nothing else: real interior contours, the geographic
fill and every non-DEPARE area outline stay exactly as they were.
"""

import math
from types import SimpleNamespace

import cairo
import numpy as np
import pytest
from osgeo import ogr

from sailworks.core.mapviewport import MercatorProjection
from sailworks.rendering.style import Style
from sailworks.rendering.vectordrawer.s57chartdrawer import S57ChartDrawer

#: A frozen Mercator camera over the lon/lat box (0, 0) - (10, 10) at
#: 10 px per degree, so the projected box exactly spans the 100 px drawing
#: surface the path tests draw on: the canvas allocation and the surface
#: are one grid.
ZOOM_10PX = math.log2(10.0 * 360.0 / 256.0)


class _Settings:
    """The settings the drawers read, without the application's manager."""

    chartPalette = "cm93"

    def register_on_change(self, key, callback):
        return None


class _Canvas(MercatorProjection):
    """The projection API the drawer reads, with a fixed camera."""

    def __init__(self, latitude, longitude, zoom, width, height):
        self._center_lat = latitude
        self._center_lon = longitude
        self._zoom = zoom
        self.width = width
        self.height = height

    def get_allocated_width(self):
        return self.width

    def get_allocated_height(self):
        return self.height


CANVAS = _Canvas(5.0, 5.0, ZOOM_10PX, 100, 100)
PALETTE = Style.chart_palettes["cm93"]


def _polygon_ogr(coordinates):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for lon, lat in coordinates:
        ring.AddPoint_2D(lon, lat)
    ring.CloseRings()
    polygon = ogr.Geometry(ogr.wkbPolygon)
    polygon.AddGeometry(ring)
    return polygon


#: One cell whose coverage is the lon/lat box (0, 0) - (10, 10).
COVERAGE = _polygon_ogr([(0, 0), (10, 0), (10, 10), (0, 10)])
EPSILON = 1e-6


def _depare(ring_lat_lon):
    ring = list(ring_lat_lon)
    if ring[0] != ring[-1]:
        ring = ring + [ring[0]]
    return SimpleNamespace(
        obj_name="DEPARE",
        attributes={"DRVAL1": 5.0, "DRVAL2": 9.0},
        geometry=[ring],
    )


def _edge_filter():
    drawer = S57ChartDrawer(_Settings())
    return drawer._coverage_edge_filter([COVERAGE], EPSILON / 2.0)


class _RecordingContext:
    """A context that remembers what it stroked.

    ``stroke()`` consumes the current path in cairo, so the stroke path has
    to be captured before the drawer's own ``stroke()`` clears it.
    """

    def __init__(self, cr):
        self._cr = cr
        self.stroke_path = None

    def __getattr__(self, name):
        return getattr(self._cr, name)

    def stroke(self):
        self.stroke_path = list(self._cr.copy_path())
        return self._cr.stroke()


def _edge_path(feature, edge_filter):
    """The stroke path ``_render_area`` lays down, as cairo path points."""
    surface = cairo.ImageSurface(
        cairo.FORMAT_ARGB32,
        CANVAS.get_allocated_width(),
        CANVAS.get_allocated_height(),
    )
    recorder = _RecordingContext(cairo.Context(surface))
    drawer = S57ChartDrawer(_Settings())
    drawer._render_area(CANVAS, recorder, feature, PALETTE, edge_filter)
    return recorder.stroke_path


def _path_points(path):
    # pycairo path items are ``(PathDataType, points)`` pairs: the points
    # are the segment's ``(x, y)`` coordinates, empty for a close.
    points = []
    for item in path:
        if item[0] not in (cairo.PATH_MOVE_TO, cairo.PATH_LINE_TO):
            continue
        coords = item[1]
        if coords and isinstance(coords[0], (tuple, list)):
            coords = coords[0]
        points.append(tuple(coords))
    return points


def _has_close_path(path):
    return any(item[0] == cairo.PATH_CLOSE_PATH for item in path)


def _screen(lats, lons, canvas=CANVAS):
    xs, ys = canvas.convert_geographic_to_screen_batch(
        np.asarray(lats, dtype=np.float64), np.asarray(lons, dtype=np.float64)
    )
    return list(zip(xs.tolist(), ys.tolist()))


def test_without_coverage_every_depare_outline_is_drawn():
    """No coverage supplied: the drawer behaves exactly as before."""
    ring = [(0, 0), (0, 10), (5, 10), (5, 0)]
    path = _edge_path(_depare(ring), None)
    assert _has_close_path(path)
    # The closed square draws four corners plus the repeated first point.
    assert len(_path_points(path)) == 5


def test_edges_on_the_coverage_boundary_are_suppressed():
    """A ring whose south and west edges lie on the cell boundary keeps
    only its interior L: the boundary edges lose their stroke, the interior
    edges chain into one open polyline."""
    ring = [(0, 0), (0, 5), (5, 5), (5, 0)]
    path = _edge_path(_depare(ring), _edge_filter())
    assert not _has_close_path(path)
    np.testing.assert_allclose(
        _path_points(path), _screen([0.0, 5.0, 5.0], [5.0, 5.0, 0.0]), atol=1 / 256
    )


def test_a_ring_along_the_whole_boundary_is_silent():
    """A DEPARE whose every edge lies on the cell boundary strokes nothing."""
    ring = [(0, 0), (0, 10), (10, 10), (10, 0)]
    path = _edge_path(_depare(ring), _edge_filter())
    assert _path_points(path) == []


def test_an_edge_touching_the_boundary_corners_keeps_its_stroke():
    """The middle edge spans the cell and touches the boundary at both
    corners, but only an edge lying along the boundary is suppressed, so
    the real contour stays."""
    ring = [(0, 0), (0, 10), (5, 10), (5, 0)]
    path = _edge_path(_depare(ring), _edge_filter())
    assert not _has_close_path(path)
    np.testing.assert_allclose(
        _path_points(path), _screen([5.0, 5.0], [10.0, 0.0]), atol=1 / 256
    )


def test_a_diagonal_chord_between_cell_corners_keeps_its_stroke():
    """A triangle spanning the cell: its hypotenuse runs corner to corner
    and touches the coverage boundary at both ends, yet is never a
    coverage edge, so it keeps its stroke."""
    ring = [(0, 0), (0, 10), (10, 0)]
    path = _edge_path(_depare(ring), _edge_filter())
    assert not _has_close_path(path)
    np.testing.assert_allclose(
        _path_points(path), _screen([0.0, 10.0], [10.0, 0.0]), atol=1 / 256
    )


def test_a_depare_far_from_the_boundary_keeps_its_full_outline():
    """An interior depth area keeps its closed outline."""
    ring = [(2, 2), (2, 8), (8, 8), (8, 2)]
    path = _edge_path(_depare(ring), _edge_filter())
    assert _has_close_path(path)
    assert len(_path_points(path)) == 5


def test_non_depare_areas_keep_their_outline_at_the_boundary():
    """Land on the cell edge keeps its outline: only DEPARE is suppressed."""
    feature = SimpleNamespace(
        obj_name="LNDARE",
        attributes={},
        geometry=[[(0, 0), (0, 10), (5, 10), (5, 0), (0, 0)]],
    )
    path = _edge_path(feature, _edge_filter())
    assert _has_close_path(path)
    assert len(_path_points(path)) == 5


def test_the_coverage_edge_filter_needs_coverage():
    drawer = S57ChartDrawer(_Settings())
    assert drawer._coverage_edge_filter(None, EPSILON) is None
    assert drawer._coverage_edge_filter([], EPSILON) is None
    # One simplification tolerance: the boundaries are simplified exactly
    # like the DEPARE rings, so coincidence needs no wider window.
    boundaries, epsilon = drawer._coverage_edge_filter([COVERAGE], 0.25)
    assert epsilon == pytest.approx(0.25)
    assert len(boundaries) == 1


def test_a_contour_just_outside_the_coincidence_window_keeps_its_outline():
    """A real contour that only passes near the boundary is not masked.

    The west edge sits one and a half tolerances off the coverage boundary:
    outside the one-tolerance coincidence window, so the whole outline
    survives.  Under a wider window this interior contour lost its stroke.
    """
    tolerance = EPSILON / 2.0
    offset = 1.5 * tolerance
    ring = [(2, offset), (2, 8), (8, 8), (8, offset)]
    path = _edge_path(_depare(ring), _edge_filter())
    # The ring is near enough to be checked segment by segment, but no
    # segment lies on the boundary, so the whole outline survives as the
    # original closed path, join included.
    points = _path_points(path)
    assert _has_close_path(path)
    assert len(points) == 5
    assert points[0] == pytest.approx(points[-1])


def test_quilted_cell_draw_end_to_end_leaves_the_interior_contour():
    """Through ``draw`` itself, on a real GPKG cell: a DEPARE whose outer
    edge is the cell's coverage edge stops being outlined there, while the
    interior DEPARE inside the same cell keeps its outline exactly."""
    import tempfile
    from pathlib import Path

    from test.enc_scene_fixtures import _feature, _polygon

    with tempfile.TemporaryDirectory() as tmp:
        source = ogr.GetDriverByName("GPKG").CreateDataSource(
            str(Path(tmp) / "cell.gpkg")
        )
        depare = source.CreateLayer("DEPARE", None, ogr.wkbMultiPolygon)
        for name in ("DRVAL1", "DRVAL2"):
            depare.CreateField(ogr.FieldDefn(name, ogr.OFTReal))
        areas = ogr.Geometry(ogr.wkbMultiPolygon)
        areas.AddGeometry(_polygon([(0, 0), (10, 0), (10, 10), (0, 10)]))
        areas.AddGeometry(_polygon([(2, 2), (8, 2), (8, 8), (2, 8)]))
        _feature(depare, areas, {"DRVAL1": 5.0, "DRVAL2": 9.0})
        mcovr = source.CreateLayer("M_COVR", None, ogr.wkbMultiPolygon)
        mcovr.CreateField(ogr.FieldDefn("CATCOV", ogr.OFTInteger))
        _feature(
            mcovr,
            _polygon([(0, 0), (10, 0), (10, 10), (0, 10)]),
            {"CATCOV": 1},
        )
        mcovr.ResetReading()
        coverage = [
            feature.GetGeometryRef().Clone()
            for feature in mcovr
            if feature.GetField("CATCOV") == 1
        ]
        canvas = _Canvas(5.0, 5.0, math.log2(10.0 * 360.0 / 256.0), 140, 140)
        bounding = ogr.CreateGeometryFromWkt("POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))")
        drawer = S57ChartDrawer(_Settings())

        def render(with_coverage):
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 140, 140)
            cr = cairo.Context(surface)
            drawer.draw(
                canvas,
                cr,
                source,
                bounding,
                coverage=coverage if with_coverage else None,
            )
            surface.flush()
            data = surface.get_data()
            return np.frombuffer(data, dtype=np.uint32).reshape(140, 140)

        suppressed = render(True)
        unsuppressed = render(False)

    # Both renders painted the depth fills.
    assert (suppressed != 0).any()
    assert (unsuppressed != 0).any()

    def window(at, radius=2):
        x, y = at
        x, y = int(x), int(y)
        return (
            slice(max(0, y - radius), y + radius + 1),
            slice(max(0, x - radius), x + radius + 1),
        )

    # The coverage edge (west cell edge, mid latitude) lost its stroke.
    edge = window(_screen([5.0], [0.0], canvas)[0])
    assert (suppressed[edge] != unsuppressed[edge]).any()
    # The interior contour (the inner DEPARE's west edge) is untouched.
    contour = window(_screen([5.0], [2.0], canvas)[0])
    assert (suppressed[contour] == unsuppressed[contour]).all()
