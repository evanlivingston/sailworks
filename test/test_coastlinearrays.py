"""Tests for the process-wide raw coastline coordinate cache."""

from osgeo import ogr

from sailworks.rendering.vectordrawer.coastlinearrays import CoastlineArrayCache


def dataset():
    source = ogr.GetDriverByName("Memory").CreateDataSource("coastline-cache-test")
    layer = source.CreateLayer("coast", geom_type=ogr.wkbPolygon)
    feature = ogr.Feature(layer.GetLayerDefn())
    feature.SetGeometry(ogr.CreateGeometryFromWkt("POLYGON((0 0,1 0,1 1,0 0))"))
    layer.CreateFeature(feature)
    return source


def test_cache_reuses_raw_float64_coordinates():
    source = dataset()
    cache = CoastlineArrayCache()
    first = cache.get(source)
    assert cache.get(source) is first
    lon, lat = first.features[0][2][0][0]
    assert str(lon.dtype) == str(lat.dtype) == "float64"
    assert list(lon) == [0, 1, 1, 0]


def test_oversized_dataset_is_not_retried():
    source = dataset()
    cache = CoastlineArrayCache(budget=1)
    assert cache.get(source) is None
    cache.budget = 10000
    assert cache.get(source) is None
    assert len(cache.too_large) == 1
