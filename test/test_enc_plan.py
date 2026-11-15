"""Ordering and coverage policy of the opt-in multi-cell composition plan.

Each expectation is either derived from the same helper functions the raster
renderer uses (``chart_scale_rank``, ``chart_scale_visible``,
``coverage_from_datasource``) or stated directly by the renderer's loop in
``ENCOverlayRenderer._render``.  Nothing here draws or touches the renderer.
"""

import pytest
import shapely
from osgeo import ogr

from sailworks.core.enccharts import chart_scale_rank, chart_scale_visible
from sailworks.rendering import encplan
from sailworks.rendering.chartlayer import ChartLayer
from sailworks.rendering.vectordrawer import encmesh


class _Point:
    def __init__(self, lat, lon):
        self._degrees = (lat, lon)

    def get_degrees(self):
        return self._degrees


class _Viewport:
    def __init__(self, scale=1.0, first=(50.0, -5.0), second=(51.0, -4.0)):
        self._scale = scale
        self._first = _Point(*first)
        self._second = _Point(*second)

    def get_bbox(self):
        return self._first, self._second

    def get_scale(self):
        return self._scale


class _CancelAfter:
    """Cancel when the callback has been polled ``after`` times."""

    def __init__(self, after):
        self.after = after
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.calls >= self.after


def _polygon(bounds):
    min_x, min_y, max_x, max_y = bounds
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in (
        (min_x, min_y),
        (max_x, min_y),
        (max_x, max_y),
        (min_x, max_y),
        (min_x, min_y),
    ):
        ring.AddPoint_2D(x, y)
    polygon = ogr.Geometry(ogr.wkbPolygon)
    polygon.AddGeometry(ring)
    return polygon


def _cell(path, features):
    """Write a GPKG cell with an M_COVR layer of ``(bounds, catcov)`` entries."""
    datasource = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
    layer = datasource.CreateLayer("M_COVR", geom_type=ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("CATCOV", ogr.OFTInteger))
    for bounds, catcov in features:
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetGeometry(_polygon(bounds))
        if catcov is not None:
            feature.SetField("CATCOV", catcov)
        layer.CreateFeature(feature)
    datasource = None
    return str(path)


def _view(viewport):
    """The view polygon, obtained from the plan's own unknown-coverage path."""
    (entry,) = encplan.plan_cells(
        viewport,
        [("view", 2500, None)],
        coverage_loader=lambda path: None,
        version_of=lambda path: None,
    )
    assert entry.coverage_known is False
    return entry.clip


def _oracle(viewport, charts):
    """The renderer's loop over the same helpers, returning clips in order."""
    scale = viewport.get_scale()
    p1, p2 = viewport.get_bbox()
    wkt = ChartLayer.get_bounding_wkt_of_coords(*p1.get_degrees(), *p2.get_degrees())
    geometry = ogr.CreateGeometryFromWkt(wkt)
    min_lon, max_lon, min_lat, max_lat = geometry.GetEnvelope()
    bounding = shapely.from_wkb(bytes(geometry.ExportToWkb()))
    covered = None
    found = []
    for path, native_scale, envelopes in sorted(
        charts, key=lambda c: (chart_scale_rank(c[1], scale), c[0])
    ):
        if not chart_scale_visible(native_scale, scale):
            continue
        if envelopes and not any(
            a <= max_lon and b >= min_lon and c <= max_lat and d >= min_lat
            for a, b, c, d in envelopes
        ):
            continue
        datasource = ogr.Open(str(path), 0)
        try:
            region = encmesh.coverage_from_datasource(datasource)
        finally:
            datasource = None
        known = region is not None
        region = bounding if region is None else shapely.intersection(region, bounding)
        if covered is not None:
            region = shapely.difference(region, covered)
        if region.is_empty:
            continue
        if known:
            covered = region if covered is None else shapely.union(covered, region)
        found.append((path, known, region))
    return found


def test_plan_uses_the_renderer_sort_order():
    viewport = _Viewport()
    charts = [
        ("c_coarse", 10000, None),
        ("a_detail", 2500, None),
        ("b_mid", 2000, None),
    ]
    ordered = sorted(charts, key=lambda c: (chart_scale_rank(c[1], 1.0), c[0]))
    expected = [path for path, _, _ in ordered]
    plan = encplan.plan_cells(
        viewport, charts, coverage_loader=lambda p: None, version_of=lambda p: None
    )
    assert [entry.path for entry in plan] == expected
    assert [entry.rank_key for entry in plan] == [
        chart_scale_rank(native_scale, 1.0) for _, native_scale, _ in ordered
    ]


def test_overlap_difference_gives_ground_to_the_best_ranked_cell(tmp_path):
    viewport = _Viewport()
    bounds = shapely.bounds(_view(viewport))
    better = _cell(tmp_path / "better.gpkg", [(bounds, 1)])
    worse = _cell(tmp_path / "worse.gpkg", [(bounds, 1)])
    charts = [(worse, 10000, None), (better, 2500, None)]

    plan = encplan.plan_cells(viewport, charts)

    expected = _oracle(viewport, charts)
    assert [(entry.path, entry.coverage_known) for entry in plan] == [
        (path, known) for path, known, _ in expected
    ]
    # The better-ranked cell owns the whole view; the worse one has no ground.
    assert [entry.path for entry in plan] == [better]
    assert plan[0].coverage_known is True
    assert plan[0].clip.equals(_view(viewport))


def test_unknown_coverage_cell_does_not_mask_later_cells(tmp_path):
    viewport = _Viewport()
    view = _view(viewport)
    min_x, min_y, max_x, max_y = shapely.bounds(view)
    middle = (min_x + max_x) / 2
    unknown = _cell(tmp_path / "unknown.gpkg", [((min_x, min_y, max_x, max_y), None)])
    known = _cell(tmp_path / "known.gpkg", [((min_x, min_y, middle, max_y), 1)])
    charts = [(known, 10000, None), (unknown, 2500, None)]

    plan = encplan.plan_cells(viewport, charts)

    assert [entry.path for entry in plan] == [unknown, known]
    assert plan[0].coverage_known is False
    assert plan[0].clip.equals(view)
    assert plan[1].coverage_known is True
    # Unknown coverage contributes no mask, so the known cell keeps its ground.
    expected_clip = shapely.intersection(shapely.box(min_x, min_y, middle, max_y), view)
    assert plan[1].clip.equals(expected_clip)
    assert plan[1].clip.area == pytest.approx((middle - min_x) * (max_y - min_y))


def test_envelope_and_visibility_rejects_match_the_renderer(tmp_path):
    viewport = _Viewport()
    min_x, min_y, max_x, max_y = shapely.bounds(_view(viewport))
    inside = _cell(tmp_path / "inside.gpkg", [((min_x, min_y, max_x, max_y), 1)])
    charts = [
        # Outside the view: rejected before any cell is opened.
        ("missing/off_view.gpkg", 2000, [(max_x + 40, max_x + 50, min_y, max_y)]),
        # Too detailed for the current display scale.
        ("missing/too_detailed.gpkg", 100, None),
        # Too coarse for the current display scale.
        ("missing/too_coarse.gpkg", 5_000_000, None),
        (inside, 2500, [(min_x, max_x, min_y, max_y)]),
    ]
    assert chart_scale_visible(100, viewport.get_scale()) is False
    assert chart_scale_visible(5_000_000, viewport.get_scale()) is False

    plan = encplan.plan_cells(viewport, charts)

    assert [entry.path for entry in plan] == [inside]


def test_cancelled_plan_returns_none_not_a_partial_tuple():
    viewport = _Viewport()
    charts = [("a", 2500, None), ("b", 2000, None), ("c", 10000, None)]
    plan = encplan.plan_cells(
        viewport,
        charts,
        cancelled=_CancelAfter(2),
        coverage_loader=lambda path: None,
        version_of=lambda path: None,
    )
    assert plan is None


def test_repeated_calls_produce_the_same_plan(tmp_path):
    viewport = _Viewport()
    min_x, min_y, max_x, max_y = shapely.bounds(_view(viewport))
    middle = (min_x + max_x) / 2
    first = _cell(tmp_path / "first.gpkg", [((min_x, min_y, middle, max_y), 1)])
    second = _cell(tmp_path / "second.gpkg", [((middle, min_y, max_x, max_y), 1)])
    charts = [(first, 2500, None), (second, 2000, None)]

    def snapshot():
        return [
            (
                entry.path,
                entry.version,
                entry.coverage_known,
                shapely.to_wkb(entry.clip),
            )
            for entry in encplan.plan_cells(viewport, charts)
        ]

    assert snapshot() == snapshot()
    assert [path for path, _, _, _ in snapshot()] == [first, second]


def test_plan_matches_the_helper_sequence(tmp_path):
    viewport = _Viewport()
    min_x, min_y, max_x, max_y = shapely.bounds(_view(viewport))
    middle = (min_x + max_x) / 2
    quarter = (min_y + max_y) / 2
    charts = [
        (_cell(tmp_path / "a.gpkg", [((min_x, min_y, max_x, max_y), 1)]), 2500, None),
        (
            _cell(tmp_path / "b.gpkg", [((min_x, min_y, middle, max_y), 1)]),
            2000,
            None,
        ),
        (
            _cell(
                tmp_path / "c.gpkg",
                [
                    ((min_x, min_y, middle, max_y), 1),
                    ((min_x, quarter, middle, max_y), 2),
                ],
            ),
            10000,
            None,
        ),
        (
            _cell(tmp_path / "d.gpkg", [((min_x, min_y, max_x, max_y), None)]),
            1500,
            None,
        ),
    ]

    plan = encplan.plan_cells(viewport, charts)
    expected = _oracle(viewport, charts)

    assert [entry.path for entry in plan] == [path for path, _, _ in expected]
    for entry, (_, known, region) in zip(plan, expected):
        assert entry.coverage_known is known
        assert entry.clip.equals(region)
        assert entry.clip.area == pytest.approx(region.area)
