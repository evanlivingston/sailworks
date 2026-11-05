"""Satellite basemap source and lifecycle checks that need no GPU."""

from types import SimpleNamespace
import sqlite3
import struct

import pytest
from PySide6.QtGui import QImage

from sailworks.qt import satellite as satellite_module
from sailworks.qt.satellite import (
    DEFAULT_ATTRIBUTION,
    DEFAULT_TILE_URL,
    SatelliteError,
    SatelliteRenderer,
    SatelliteSource,
    SatelliteWorker,
    available,
    configured_source,
    online_source,
    pack_source,
)


def test_online_source_builds_one_raster_layer():
    style = online_source().style()

    assert style["version"] == 8
    source = style["sources"]["satellite"]
    assert source["type"] == "raster"
    assert source["tiles"] == [DEFAULT_TILE_URL]
    assert source["attribution"] == DEFAULT_ATTRIBUTION
    assert [layer["type"] for layer in style["layers"]] == ["raster"]
    assert style["layers"][0]["source"] == "satellite"


def test_pack_source_uses_the_protocol_the_format_needs(tmp_path):
    mbtiles = tmp_path / "area.mbtiles"
    mbtiles.write_bytes(b"")
    pmtiles = tmp_path / "area.pmtiles"
    pmtiles.write_bytes(b"")

    assert pack_source(str(mbtiles)).tiles == (f"mbtiles://{mbtiles}",)
    assert pack_source(str(pmtiles)).tiles == (f"pmtiles://file://{pmtiles}",)


def test_pack_source_rejects_unknown_format(tmp_path):
    other = tmp_path / "area.gpkg"
    other.write_bytes(b"")
    with pytest.raises(ValueError):
        pack_source(str(other))


def test_pack_source_rejects_a_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        pack_source(str(tmp_path / "missing.pmtiles"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tiles": ()},
        {"tiles": ("",)},
        {"tiles": ("https://example/{z}/{x}/{y}.png",), "min_zoom": 5, "max_zoom": 4},
        {"tiles": ("https://example/{z}/{x}/{y}.png",), "tile_size": 0},
    ],
)
def test_source_rejects_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        SatelliteSource(**kwargs)


def test_online_source_requires_network_but_a_pack_does_not(tmp_path):
    assert online_source().requires_network is True
    pack = tmp_path / "area.pmtiles"
    pack.write_bytes(b"")
    assert pack_source(str(pack)).requires_network is False


def test_available_reports_a_boolean():
    assert isinstance(available(), bool)


def test_pack_source_reads_pmtiles_bounds(tmp_path):
    header = bytearray(127)
    header[:7] = b"PMTiles"
    header[7] = 3
    struct.pack_into("<4i", header, 102, -1246000000, 480000000, -1226000000, 488000000)
    pack = tmp_path / "area.pmtiles"
    pack.write_bytes(bytes(header))

    source = pack_source(str(pack))

    assert source.bounds == (-124.6, 48.0, -122.6, 48.8)
    assert source.style()["sources"]["satellite"]["bounds"] == [
        -124.6,
        48.0,
        -122.6,
        48.8,
    ]


def test_pack_source_reads_mbtiles_bounds(tmp_path):
    pack = tmp_path / "area.mbtiles"
    database = sqlite3.connect(pack)
    database.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    database.execute(
        "INSERT INTO metadata VALUES ('bounds', '-124.6,48.0,-122.6,48.8')"
    )
    database.commit()
    database.close()

    assert pack_source(str(pack)).bounds == (-124.6, 48.0, -122.6, 48.8)


def test_configured_source_prefers_the_configured_pack(tmp_path):
    pack = tmp_path / "configured.pmtiles"
    pack.write_bytes(b"")

    source = configured_source(str(pack), str(tmp_path))

    assert source.tiles == (f"pmtiles://file://{pack}",)
    assert source.underlay is not None
    assert source.underlay.requires_network
    assert source.offline_capable


def test_pack_source_is_hybrid_in_the_style(tmp_path):
    pack = tmp_path / "area.pmtiles"
    pack.write_bytes(b"")
    style = configured_source(str(pack), str(tmp_path)).style()

    assert list(style["sources"]) == ["satellite-underlay", "satellite"]
    assert [layer["id"] for layer in style["layers"]] == [
        "satellite-underlay",
        "satellite",
    ]
    assert style["sources"]["satellite-underlay"]["type"] == "raster"
    assert style["sources"]["satellite"]["type"] == "raster"
    assert "USGS" in style["sources"]["satellite-underlay"]["attribution"]
    assert "Offline imagery pack" in style["sources"]["satellite"]["attribution"]


def test_configured_source_discovers_pmtiles_before_mbtiles(tmp_path):
    folder = tmp_path / "satellite"
    folder.mkdir()
    (folder / "area.mbtiles").write_bytes(b"")
    (folder / "area.pmtiles").write_bytes(b"")

    source = configured_source("", str(tmp_path))

    assert source.tiles == (f"pmtiles://file://{folder / 'area.pmtiles'}",)


def test_configured_source_falls_back_to_online_imagery(tmp_path):
    source = configured_source("", str(tmp_path))

    assert source.tiles == (DEFAULT_TILE_URL,)


def test_configured_source_reports_a_broken_pack(tmp_path):
    with pytest.raises(FileNotFoundError):
        configured_source(str(tmp_path / "missing.pmtiles"), str(tmp_path))


def test_renderer_rejects_an_empty_frame():
    renderer = SatelliteRenderer(online_source())
    with pytest.raises(SatelliteError):
        renderer.render(48.0, -123.0, 10.0, 0, 100)
    renderer.close()


def test_worker_renders_the_latest_request(qtbot, monkeypatch):
    class FakeRenderer:
        instances = []

        def __init__(self, source: SatelliteSource, cache_dir: str = ""):
            self.source = source
            self.cache_dir = cache_dir
            self.calls = []
            FakeRenderer.instances.append(self)

        def render(self, lat, lon, zoom, width, height, scale=1.0, timeout=12.0):
            self.calls.append((lat, lon, zoom, width, height, scale))
            image = QImage(
                round(width * scale),
                round(height * scale),
                QImage.Format.Format_RGBA8888,
            )
            image.fill(0)
            return image

        def close(self):
            pass

    monkeypatch.setattr(satellite_module, "SatelliteRenderer", FakeRenderer)
    worker = SatelliteWorker(online_source())
    camera = SimpleNamespace(
        center_lat=48.5,
        center_lon=-123.0,
        zoom=11.0,
        width=40,
        height=30,
    )
    try:
        with qtbot.waitSignal(worker.rendered, timeout=5000) as blocker:
            worker.submit(7, camera, 2.0)
        generation, image = blocker.args
        assert generation == 7
        assert isinstance(image, QImage)
        assert image.size().width() == 80
        assert image.size().height() == 60
        assert FakeRenderer.instances[0].calls[-1][-1] == 2.0
    finally:
        worker.stop()
        worker._thread.join(5)
    assert not worker._thread.is_alive()


def test_worker_stops_without_a_request():
    worker = SatelliteWorker(online_source())
    worker.stop()
    worker._thread.join(5)
    assert not worker._thread.is_alive()
