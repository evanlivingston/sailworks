"""Viewport clipping retains exact geometry and reuses repeated work."""

from osgeo import ogr

from sailworks.core.geo.shoreline import ShorelineGeometryCache


def polygon(west, east):
    return ogr.CreateGeometryFromWkt(
        f"POLYGON(({west} 0,{east} 0,{east} 1,{west} 1,{west} 0))"
    )


def test_clips_reused_and_distinct_bounds_remain_exact():
    cache = ShorelineGeometryCache()
    source = polygon(0, 2)
    first = cache.clip("feature", source, polygon(0, 1))
    assert cache.clip("feature", source, polygon(0, 1)) is first
    second = cache.clip("feature", source, polygon(0, 1.0000000001))
    assert second is not first
    assert second.GetEnvelope()[1] > first.GetEnvelope()[1]


def test_inside_and_empty_results_are_explicit():
    cache = ShorelineGeometryCache()
    source = polygon(0, 1)
    assert cache.clip("feature", source, polygon(-1, 2)) is cache.sources["feature"]
    assert cache.clip("feature", source, polygon(2, 3)).IsEmpty()


def test_clip_cache_is_bounded():
    cache = ShorelineGeometryCache()
    source = polygon(0, 1)
    for i in range(260):
        cache.clip("feature", source, polygon(i + 2, i + 3))
    assert len(cache.clips) == 256
