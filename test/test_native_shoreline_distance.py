"""The native coastal distance uses the existing GSHHS clearance geometry."""

from collections import OrderedDict

import pytest
from osgeo import ogr

from sailworks.core.geo.shoreline import ShorelineValidity


def _shoreline(wkts):
    data = ogr.GetDriverByName("Memory").CreateDataSource("")
    layer = data.CreateLayer("selected-GSHHS", geom_type=ogr.wkbPolygon)
    for wkt in wkts:
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
        layer.CreateFeature(feature)
    provider = ShorelineValidity.__new__(ShorelineValidity)
    provider.lpvFile = data
    provider._routing_clips = OrderedDict()
    provider._routing_sources = OrderedDict()
    provider._routing_native_clips = OrderedDict()
    return provider


def test_metric_distance_matches_existing_clearance_threshold():
    provider = _shoreline(
        ("POLYGON ((0 -0.01, 0.01 -0.01, 0.01 0.01, 0 0.01, 0 -0.01))",)
    )
    latitude, longitude = 0.005, -0.01
    distance_m = provider.distance_to_forbidden_nm(latitude, longitude) * 1852
    assert 1100 < distance_m < 1125
    point_segment = [(latitude, longitude, latitude, longitude)]
    assert provider.lines_validity(point_segment, distance_m - 2) == [True]
    assert provider.lines_validity(point_segment, distance_m + 2) == [False]
    assert provider.distance_to_forbidden_nm(-0.005, 0.005) == 0


def test_expanding_search_finds_farther_shore_without_clip_edge_false_positive():
    provider = _shoreline(("POLYGON ((1 1, 2 1, 2 2, 1 2, 1 1))",))
    distance_m = provider.distance_to_forbidden_nm(0, 0) * 1852
    assert 150_000 < distance_m < 160_000
    point_segment = [(0, 0, 0, 0)]
    assert provider.lines_validity(point_segment, distance_m - 100) == [True]
    assert provider.lines_validity(point_segment, distance_m + 100) == [False]


def test_high_latitude_and_dateline_distance_uses_selected_geometry():
    provider = _shoreline(
        (
            "POLYGON ((14.9 82, 15.1 82, 15.1 82.1, 14.9 82.1, 14.9 82))",
            "POLYGON ((179.9 0, 179.99 0, 179.99 0.1, 179.9 0.1, 179.9 0))",
        )
    )
    polar = provider.distance_to_forbidden_nm(81.99, 15.0)
    dateline = provider.distance_to_forbidden_nm(0.05, 179.89)
    assert 0 < polar < 1
    assert 0 < dateline < 1


def test_invalid_position_fails_instead_of_assuming_open_water():
    provider = _shoreline(("POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))",))
    with pytest.raises(ValueError, match="Invalid shoreline distance"):
        provider.distance_to_forbidden_nm(float("nan"), 0)
