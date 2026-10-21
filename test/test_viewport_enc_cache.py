"""The viewport's ENC geometry cache: the same pixels, prepared once.

The prepared geometry of the ENC features is kept in a cache that belongs to
the renderer, so that the eviction of a chart handle does not make the cells
be prepared again.  These checks are headless: synthetic exchange sets are
drawn by the application's own renderer, and the pixels are compared with
the reference renderer's own pixels.
"""

import json
import math
import os
import time
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import cairo
from osgeo import ogr, osr

from gweatherrouting.core.mapviewport import MapViewport
from gweatherrouting.qt import encgeometrycache, viewportenc
from gweatherrouting.rendering.encrenderer import ENCOverlayRenderer
from gweatherrouting.rendering.overlayrenderer import RenderJob
from gweatherrouting.rendering.vectordrawer.s57chartdrawer import S57ChartDrawer
from test.map_helpers import MapCanvas

#: The cells of the synthetic library.
CELLS = 12

#: The vertices of the synthetic land area.
VERTICES = 64


# --------------------------------------------------------------- helpers
def _viewport():
    """The viewport the synthetic cells are drawn in."""
    return MapViewport(MapCanvas())


def _bounds(viewport):
    """The view's own geographic bounds."""
    p1, p2 = viewport.get_bbox()
    lat1, lon1 = p1.get_degrees()
    lat2, lon2 = p2.get_degrees()
    return (min(lon1, lon2), max(lon1, lon2), min(lat1, lat2), max(lat1, lat2))


def _stripe(viewport, index, count):
    """The bounds of one cell's own stripe of the view."""
    west, east, south, north = _bounds(viewport)
    step = (east - west) / count
    return (
        west + index * step,
        west + (index + 1) * step,
        south,
        north,
    )


def _circle(left, right, south, north, count=VERTICES):
    """A closed ring of vertices, inside the given bounds."""
    cx = (left + right) / 2
    cy = (south + north) / 2
    radius = min(right - left, north - south) / 5
    points = []
    for step in range(count):
        angle = 2 * math.pi * step / count
        points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    points.append(points[0])
    return points


def _wkt_ring(points):
    return "POLYGON((" + ",".join(f"{x} {y}" for x, y in points) + "))"


def _write_cell(path, viewport, index, count):
    """A synthetic exchange set: a land area, a coastline and a buoy.

    The cell is an OGR data source with the layer names of an exchange set,
    so the application's own drawer reads it exactly as it reads a cell.
    """
    left, right, south, north = _stripe(viewport, index, count)
    driver = ogr.GetDriverByName("GPKG")
    datasource = driver.CreateDataSource(str(path))
    spatial = osr.SpatialReference()
    spatial.ImportFromEPSG(4326)

    land = datasource.CreateLayer("LNDARE", spatial, ogr.wkbPolygon)
    feature = ogr.Feature(land.GetLayerDefn())
    feature.SetGeometry(
        ogr.CreateGeometryFromWkt(_wkt_ring(_circle(left, right, south, north)))
    )
    land.CreateFeature(feature)

    coast = datasource.CreateLayer("COALNE", spatial, ogr.wkbLineString)
    feature = ogr.Feature(coast.GetLayerDefn())
    feature.SetGeometry(
        ogr.CreateGeometryFromWkt(
            "LINESTRING(%f %f,%f %f)" % (left, south, right, north)
        )
    )
    coast.CreateFeature(feature)

    buoy = datasource.CreateLayer("BOYLAT", spatial, ogr.wkbPoint)
    feature = ogr.Feature(buoy.GetLayerDefn())
    feature.SetGeometry(
        ogr.CreateGeometryFromWkt(
            "POINT(%f %f)" % ((left + right) / 2, (south + north) / 2)
        )
    )
    buoy.CreateFeature(feature)

    coverage = datasource.CreateLayer("M_COVR", spatial, ogr.wkbPolygon)
    coverage.CreateField(ogr.FieldDefn("CATCOV", ogr.OFTInteger))
    feature = ogr.Feature(coverage.GetLayerDefn())
    feature.SetField("CATCOV", 1)
    feature.SetGeometry(
        ogr.CreateGeometryFromWkt(
            "POLYGON((%f %f,%f %f,%f %f,%f %f,%f %f))"
            % (
                left,
                south,
                right,
                south,
                right,
                north,
                left,
                north,
                left,
                south,
            )
        )
    )
    coverage.CreateFeature(feature)

    datasource = None
    return str(path)


def _library(tmp_path, viewport, count=CELLS):
    """The user's own library of synthetic cells."""
    paths = []
    for index in range(count):
        path = Path(tmp_path) / ("US5TEST%02d.000" % index)
        paths.append(_write_cell(path, viewport, index, count))
    settings = {
        "vectorCharts": [
            {"path": path, "enabled": True, "metadata": {}} for path in paths
        ],
        "chartENCVisible": True,
    }
    (Path(tmp_path) / "settings.json").write_text(json.dumps(settings))
    return paths


def _job(viewport, charts=()):
    """A render job for the application's own ENC renderer."""
    job = RenderJob(("cm93", 1, ()), viewport, tuple(charts), 1, "cm93")
    viewport.render_cancelled = job.cancel.is_set
    return job


def _source(tmp_path, palette="cm93"):
    """The viewport's own ENC source, on the user's own library."""
    return viewportenc.ViewportEncSource(tmp_path, palette)


def _compose(source, job):
    """The viewport's own composition of the library."""
    surface = cairo.ImageSurface(
        cairo.FORMAT_ARGB32, job.viewport.width, job.viewport.height
    )
    surface.set_device_scale(1, 1)
    result, metadata = source.compose(job, surface)
    return result, metadata


def _pixels(surface):
    surface.flush()
    return bytes(surface.get_data())


def _drawn(surface):
    """How many pixels of the surface carry something."""
    pixels = _pixels(surface)
    return sum(1 for index in range(3, len(pixels), 4) if pixels[index])


class _Feature:
    """A stand-in for a feature, with the feature's own id."""

    def __init__(self, fid):
        self._fid = fid

    def GetFID(self):  # noqa: N802 - the OGR feature interface
        return self._fid


def _payload(vertices=VERTICES):
    """A prepared feature, of the shape the drawer collects."""
    ring = [(47.6 + index * 1e-5, -122.4) for index in range(vertices)]
    feature = SimpleNamespace(obj_name="LNDARE", attributes={}, geometry=[ring])
    return ([feature], [], [])


# ------------------------------------------------------------ the pixels
def test_the_viewport_pixels_are_the_reference_pixels_with_the_cache(tmp_path):
    """The shared cache changes what is kept, never what is drawn."""
    viewport = _viewport()
    _library(tmp_path, viewport)
    source = _source(tmp_path)
    job = _job(viewport)

    surface, metadata = _compose(source, job)
    assert metadata["state"] == viewportenc.STATE_READY
    assert len(metadata["selected"]) == CELLS
    # The cells are drawn: the comparison below is about a real picture.
    assert _drawn(surface) > 0

    # The same composition, with the shared cache disabled.
    plain = _source(tmp_path)
    plain._renderer = viewportenc.ViewportEncRenderer(
        viewportenc.schedule_idle,
        encgeometrycache.PreparedFeatureCache(enabled=False),
    )
    uncached, _metadata = _compose(plain, job)

    # The reference renderer's own pixels.
    charts = tuple(
        (source.path, source.native_scale, source.envelopes)
        for source in plain._catalog.sources
    )
    reference = ENCOverlayRenderer()._render(_job(viewport, charts))

    assert _pixels(surface) == _pixels(uncached)
    assert _pixels(surface) == _pixels(reference)


def test_the_cached_composition_is_the_uncached_composition(tmp_path):
    """A second composition of the same view is the same picture."""
    viewport = _viewport()
    _library(tmp_path, viewport)
    source = _source(tmp_path)
    job = _job(viewport)

    first, _metadata = _compose(source, job)
    second, _metadata = _compose(source, job)
    assert _pixels(first) == _pixels(second)


# ------------------------------------------------------- the eviction
def test_the_cache_is_hit_after_the_chart_handles_are_evicted(tmp_path):
    """The prepared geometry outlives the chart handle it was prepared for."""
    viewport = _viewport()
    _library(tmp_path, viewport)
    source = _source(tmp_path)
    job = _job(viewport)

    first, _metadata = _compose(source, job)
    renderer = source._renderer
    cache = renderer.feature_cache
    assert cache.entries > 0
    hits = cache.hits

    # The handles are evicted: the renderer keeps eight, and the library has
    # more cells than that, so the first cell is reopened.
    renderer._charts.clear()
    assert len(renderer._charts) == 0

    second, _metadata = _compose(source, job)
    assert cache.hits > hits
    assert cache.entries > 0
    assert _pixels(first) == _pixels(second)


def test_the_cache_is_hit_after_more_than_eight_cells_were_touched(tmp_path):
    """A view that touches more cells than the renderer keeps still hits."""
    viewport = _viewport()
    paths = _library(tmp_path, viewport, count=12)
    assert len(paths) > 8
    source = _source(tmp_path)
    job = _job(viewport)

    first, _metadata = _compose(source, job)
    renderer = source._renderer
    cache = renderer.feature_cache
    hits = cache.hits
    misses = cache.misses
    assert misses > 0

    # The handles were opened for every cell, and the renderer keeps eight.
    assert len(renderer._charts) == 8

    second, _metadata = _compose(source, job)
    # Every cell that was prepared is prepared once and reused once.
    assert cache.hits == hits + misses
    assert _pixels(first) == _pixels(second)


def test_a_replaced_cell_is_never_drawn_from_what_was_prepared(tmp_path):
    """A cell that changed releases the geometry prepared for it."""
    viewport = _viewport()
    paths = _library(tmp_path, viewport, count=2)
    source = _source(tmp_path)
    job = _job(viewport)

    _surface, _metadata = _compose(source, job)
    renderer = source._renderer
    cache = renderer.feature_cache
    assert cache.entries > 0
    assert cache.hits == 0

    # The cell's own file changed: the geometry prepared for it is released,
    # and the cell is prepared again rather than drawn from what it was.
    stamp = time.time() + 10
    os.utime(paths[0], (stamp, stamp))
    _surface, metadata = _compose(source, job)
    assert metadata["state"] == viewportenc.STATE_READY
    assert cache.hits == 0
    assert cache.entries > 0


# ------------------------------------------------------------ the bounds
def test_the_source_release_keeps_the_prepared_geometry(tmp_path):
    """The worker's own release drops the handles, not the geometry."""
    viewport = _viewport()
    _library(tmp_path, viewport, count=2)
    source = _source(tmp_path)
    job = _job(viewport)

    first, _metadata = _compose(source, job)
    cache = source._renderer.feature_cache
    assert cache.entries > 0
    hits = cache.hits

    source.release()
    assert cache.entries > 0

    second, _metadata = _compose(source, job)
    assert cache.hits > hits
    assert _pixels(first) == _pixels(second)


def test_the_chart_worker_keeps_the_enc_source_across_compositions(tmp_path):
    """The worker's own release keeps the source, so the geometry survives."""
    from gweatherrouting.qt import gribviewportchart

    layer = gribviewportchart.ViewportChartLayer(
        paths=(), data_dir=tmp_path, enc_enabled=True
    )
    try:
        released = []
        source = SimpleNamespace(release=lambda: released.append(True))
        layer._enc_source = source
        layer._release()
        assert released == [True]
        # The source survives the composition: the prepared geometry it
        # holds survives the redraw.
        assert layer._enc_source is source
        # A closed worker drops it with the worker.
        layer._closed = True
        layer._release()
        assert layer._enc_source is None
    finally:
        layer.close()


def test_the_cache_drops_the_least_recently_used_entries():
    """The cache is bounded by bytes, and it drops the oldest entries."""
    cache = encgeometrycache.PreparedFeatureCache(limit_bytes=20000)
    payload = _payload()
    cache.put(("a", 0, "LNDARE", 1), payload)
    cache.put(("b", 0, "LNDARE", 2), payload)
    assert cache.entries == 2
    assert 0 < cache.bytes <= cache.limit

    # The first entry is used again, so it is no longer the oldest one.
    assert cache.get(("a", 0, "LNDARE", 1)) is payload
    cache.put(("c", 0, "LNDARE", 3), payload)
    assert cache.entries == 2
    assert cache.bytes <= cache.limit
    assert cache.get(("a", 0, "LNDARE", 1)) is payload
    assert cache.get(("b", 0, "LNDARE", 2)) is None
    assert cache.get(("c", 0, "LNDARE", 3)) is payload


def test_the_accounted_bytes_never_exceed_the_limit():
    """The accounted bytes are the bytes the cache retains."""
    cache = encgeometrycache.PreparedFeatureCache(limit_bytes=20000)
    payload = _payload()
    for index in range(40):
        cache.put(("cell", 0, "LNDARE", index), payload)
        assert cache.bytes <= cache.limit
    assert cache.bytes > 0
    assert cache.entries < 40


def test_an_entry_that_would_break_the_limit_is_never_kept():
    """A single entry that does not fit is not kept at all."""
    cache = encgeometrycache.PreparedFeatureCache(limit_bytes=64)
    cache.put(("a", 0, "LNDARE", 1), _payload())
    assert cache.entries == 0
    assert cache.bytes == 0


def test_the_cache_is_cleared_when_the_cells_change():
    """The cache never describes a cell that is no longer there."""
    cache = encgeometrycache.PreparedFeatureCache()
    cache.put(("a", 0, "LNDARE", 1), _payload())
    assert cache.entries == 1
    cache.clear()
    assert cache.entries == 0
    assert cache.bytes == 0


# --------------------------------------------------------- the keys
def test_the_viewport_key_is_the_cell_own_path():
    """The key survives the reopening of the cell."""
    cache = encgeometrycache.PreparedFeatureCache()
    drawer = encgeometrycache.ViewportS57ChartDrawer("/charts/US5TEST.000", cache)
    handle = object()
    key = drawer._feature_cache_key(handle, 0.5, "LNDARE", _Feature(7))
    assert key == ("/charts/US5TEST.000", 0.5, "LNDARE", 7)


def test_the_legacy_key_is_the_open_handle():
    """The application's own drawer keeps the key it has always used."""
    drawer = S57ChartDrawer.__new__(S57ChartDrawer)
    drawer._ensure_feature_cache()
    handle = object()
    key = drawer._feature_cache_key(handle, 0.5, "LNDARE", _Feature(7))
    assert key == (id(handle), 0.5, "LNDARE", 7)


# ------------------------------------------------- the legacy path
def test_the_legacy_drawer_keeps_its_own_cache_and_its_own_cap():
    """The application's own drawer is untouched: same cache, same cap."""
    assert S57ChartDrawer.FEATURE_CACHE_ENTRIES == 4096
    drawer = S57ChartDrawer.__new__(S57ChartDrawer)
    drawer._ensure_feature_cache()
    assert isinstance(drawer._feature_cache, OrderedDict)

    payload = _payload(4)
    for index in range(4096):
        drawer._feature_cache_put(("key", index), payload)
    assert len(drawer._feature_cache) == 4096
    drawer._feature_cache_put(("key", 4096), payload)
    assert len(drawer._feature_cache) == 4096
    # The oldest entry is the one that is dropped.
    assert ("key", 0) not in drawer._feature_cache
    assert ("key", 4096) in drawer._feature_cache


def test_the_legacy_path_still_draws_with_its_own_drawer(tmp_path):
    """The application's own renderer keeps the application's own drawer."""
    viewport = _viewport()
    _library(tmp_path, viewport, count=2)
    source = _source(tmp_path)
    source._catalog.refresh()
    charts = tuple(
        (entry.path, entry.native_scale, entry.envelopes)
        for entry in source._catalog.sources
    )
    renderer = ENCOverlayRenderer()
    renderer._render(_job(viewport, charts))
    for path, chart in renderer._charts.items():
        assert type(chart.drawer) is S57ChartDrawer
        assert isinstance(chart.drawer._feature_cache, OrderedDict)
        assert len(chart.drawer._feature_cache) <= 4096


def test_the_viewport_drawer_is_the_applications_own_drawer():
    """The viewport's drawer adds a cache, never a portrayal."""
    assert issubclass(encgeometrycache.ViewportS57ChartDrawer, S57ChartDrawer)
    drawer = encgeometrycache.ViewportS57ChartDrawer()
    assert drawer._ensure_feature_cache() is None
    assert not hasattr(drawer, "_feature_cache")
