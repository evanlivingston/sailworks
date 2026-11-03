"""Source switches defer saved charts without changing their settings."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from sailworks.core.settings import SettingsManager
from sailworks.core.storage import Storage
from sailworks.rendering.chartclasses import CHART_CLASSES, saved_chart_enabled
from sailworks.rendering.chartcollection import ChartCollection
from sailworks.rendering.gdalvectorchart import GDALVectorChart
from sailworks.rendering.vectordrawer.s57chartdrawer import S57ChartDrawer
from sailworks.qt.datalibrary import local_chart_entries


def test_saved_source_classification():
    settings = {key: False for _, key, _ in CHART_CLASSES}
    paths = [
        ("vector", "x.000", "chartENCVisible"),
        ("vector", "x.geojson", "chartVectorVisible"),
        ("vector", "Cm93/", "chartCM93Visible"),
        ("vector", "x.pbf", "chartSeamarksVisible"),
        ("raster", "x", "chartRasterVisible"),
        ("mbtiles", "x", "chartMBTilesVisible"),
    ]
    for kind, path, key in paths:
        assert not saved_chart_enabled(settings, kind, {"path": path, "enabled": True})
        settings[key] = True
        assert saved_chart_enabled(settings, kind, {"path": path, "enabled": False})
        settings[key] = False


@patch.object(Storage, "save")
@patch.object(Storage, "load_or_save_default")
def test_deferred_loading_and_off_queries(_load, _save):
    settings = SettingsManager()
    object.__setattr__(settings, "_Storage__init", True)
    for _, key, _ in CHART_CLASSES:
        settings[key] = False
    settings.vectorCharts = [{"path": "harbor.000", "enabled": False}]
    settings.rasterCharts = [{"path": "raster", "enabled": True}]
    settings.mbtilesCharts = [{"path": "imagery", "enabled": True}]
    before = settings.to_dict()
    collection = ChartCollection(settings)
    collection.load_saved_on_change = True

    def load_vector(path, metadata, enabled):
        chart = GDALVectorChart.__new__(GDALVectorChart)
        chart.path, chart.ctype, chart.enabled = path, "vector", enabled
        chart.drawer = S57ChartDrawer.__new__(S57ChartDrawer)
        collection.charts.append(chart)

    collection.load_vector_layer = Mock(side_effect=load_vector)
    collection.load_raster_layer = Mock()
    collection.load_mbtiles_layer = Mock()
    with (
        patch.object(collection, "_load_base_gshhs") as world,
        patch.object(collection, "_load_base_osm") as osm,
        patch("sailworks.rendering.chartcollection.GDALVectorChart") as base,
    ):
        collection.load_deferred_charts()
        world.assert_not_called()
        osm.assert_not_called()
        base.assert_not_called()
    assert collection.charts == []
    assert collection.depth_at(0, 0, 1) is None
    assert collection.query_point(0, 0) == []
    assert settings.to_dict() == before
    settings.chartENCVisible = True
    assert len(collection.charts) == 1
    assert not collection.charts[0].enabled
    collection.on_chart_classes_changed("chartENCVisible", True)
    assert collection.load_vector_layer.call_count == 1
    settings.chartENCVisible = False
    assert collection.charts == []


def test_loaded_enc_is_removed_when_switched_off():
    with patch.object(Storage, "save"), patch.object(Storage, "load_or_save_default"):
        settings = SettingsManager()
        object.__setattr__(settings, "_Storage__init", True)
        collection = ChartCollection(settings)
        collection.load_saved_on_change = True
        chart = GDALVectorChart.__new__(GDALVectorChart)
        chart.path = "harbor.000"
        chart.drawer = S57ChartDrawer.__new__(S57ChartDrawer)
        chart.enabled = True
        collection.charts = [chart]
        settings.chartENCVisible = False
        assert collection.charts == []
        assert collection.query_point(0, 0) == []


def test_local_files_includes_unloaded_entries():
    configured = {
        "vectorCharts": [{"path": "harbor.000", "enabled": True}],
        "rasterCharts": [],
        "mbtilesCharts": [],
    }
    entries = local_chart_entries([], configured)
    assert [(c.path, c.ctype, c.enabled, c.deferred) for c in entries] == [
        ("harbor.000", "vector", False, True)
    ]
    assert configured["vectorCharts"][0]["enabled"]


def test_staged_loading_skips_off_sources_and_preserves_errors():
    from PySide6.QtCore import QCoreApplication, QObject
    from sailworks.qt.chartstartup import ChartStartup

    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None
    with patch.object(Storage, "save"), patch.object(Storage, "load_or_save_default"):
        settings = SettingsManager()
        object.__setattr__(settings, "_Storage__init", True)
        for _, key, _ in CHART_CLASSES:
            settings[key] = False
        settings.chartENCVisible = True
        settings.vectorCharts = [
            {"path": "first.000"},
            {"path": "bad.000"},
            {"path": "off.geojson"},
            {"path": "last.000"},
        ]
        collection = ChartCollection(settings)
        collection.load_saved_on_change = True
        collection.load_base_chart = Mock()

        def load_vector_layer(path, metadata, enabled):
            if path == "bad.000":
                raise ValueError("bad chart")
            chart = GDALVectorChart.__new__(GDALVectorChart)
            chart.path, chart.ctype, chart.enabled = path, "vector", enabled
            chart.drawer = S57ChartDrawer.__new__(S57ChartDrawer)
            collection.charts.append(chart)

        collection.load_vector_layer = load_vector_layer
        owner = QObject()
        owner.charts, owner.settings, owner.closed = collection, settings, False
        owner.chart_errors = []
        owner.map_data = SimpleNamespace(
            task=SimpleNamespace(changed=Mock()), message=""
        )
        owner.data = SimpleNamespace(redraw=Mock(), changed=Mock())
        startup = ChartStartup(owner)
        startup.jobs = [
            (e["path"], load_vector_layer, e) for e in settings.vectorCharts
        ]
        while startup.state != "ready":
            startup.advance()
        startup.timer.stop()
        assert [c.path for c in collection.charts] == ["first.000", "last.000"]
        assert owner.chart_errors == ["bad.000: bad chart"]
