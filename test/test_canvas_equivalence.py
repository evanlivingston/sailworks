"""Single-backend checks on the Qt canvas carrier.

This module used to record how far the Qt renderer was from a Cairo one. The
Cairo backend is gone (#267), so what remains are the checks that still mean
something on the one backend: the wind colour fill's coverage, the recorded
geometric error of the image approximation, a decoded raster chart rendered
through the Qt carrier, and the byte layout of ``CanvasImage.get_data()``.
"""

import math
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QGuiApplication, QImage  # noqa: E402

from sailworks.rendering.canvas import (  # noqa: E402
    Canvas,
    CanvasImage,
    QPainterCanvas,
)
from sailworks.rendering.windpainter import WindPainter  # noqa: E402

WIDTH = 480
HEIGHT = 320


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QGuiApplication.instance()
    if app is None:
        try:
            app = QGuiApplication([])
        except Exception as exc:  # pragma: no cover - headless without a platform
            pytest.skip(f"Qt platform unavailable: {exc}")
    return app


# --- A decoded raster chart through the Qt carrier ---------------------------


def _raster_chart(tmp_path):
    """A decoded GDAL raster chart: four palette quadrants, no resampling."""
    from osgeo import gdal, osr

    from sailworks.rendering.gdalrasterchart import GDALSingleRasterChart

    path = str(tmp_path / "equivalence-raster.tif")
    width, height = 40, 30
    dataset = gdal.GetDriverByName("GTiff").Create(
        path, width, height, 1, gdal.GDT_Byte
    )
    reference = osr.SpatialReference()
    reference.ImportFromEPSG(4326)
    dataset.SetProjection(reference.ExportToWkt())
    dataset.SetGeoTransform((-124, 0.1, 0, 49, 0, -0.1))
    band = dataset.GetRasterBand(1)
    table = gdal.ColorTable()
    for index, colour in enumerate(
        ((11, 22, 33, 255), (44, 55, 66, 255), (77, 88, 99, 255), (200, 180, 160, 255)),
        start=1,
    ):
        table.SetColorEntry(index, colour)
    band.SetRasterColorTable(table)
    values = np.zeros((height, width), dtype=np.uint8)
    values[: height // 2, : width // 2] = 1
    values[: height // 2, width // 2 :] = 2
    values[height // 2 :, : width // 2] = 3
    values[height // 2 :, width // 2 :] = 4
    band.WriteArray(values)
    dataset = None
    return GDALSingleRasterChart(path)


def _qpainter_raster_scene(chart, scale):
    width, height = chart.width * scale, chart.height * scale
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    canvas = QPainterCanvas(image)
    canvas.draw_image(
        chart.image_for(canvas), 0, 0, width=width, height=height, smooth=None
    )
    canvas.finish()
    return image


def test_raster_chart_image_renders_through_the_qt_carrier(tmp_path):
    chart = _raster_chart(tmp_path)
    image = _qpainter_raster_scene(chart, 3)

    assert image.width() == chart.width * 3
    assert image.height() == chart.height * 3
    assert any(
        image.pixelColor(x, y).alpha()
        for x in range(0, image.width(), 16)
        for y in range(0, image.height(), 16)
    )


# --- Wind colour fill: the image approximation -------------------------------
#
# The field is one RGBA image of the sampled grid, built from the same
# ``wind_color`` node colours and blitted once, so the backend's bilinear
# filter interpolates between nodes. Rows are placed evenly between the first
# and last latitude instead of at their true mercator offsets.

# Mercator row spacing is non-uniform, so a uniformly scaled image misplaces
# interior rows between the first and last latitude. Measured on a 5-row grid
# from 60S to 60N, 1400 px tall: 32.75 px, about 2.3% of the field height.
WIND_APPROX_MERCATOR_ROW_ERROR_PX = 32.75
WIND_APPROX_MERCATOR_ROW_ERROR_MARGIN = 0.5

_WIND_LATS = (-2.0, -1.0, 0.0, 1.0, 2.0)
_WIND_LONS = (-3, -2, -1, 0, 1, 2, 3)


class _WindView:
    """A regular grid under a linear, mercator-like projection."""

    def render_cancelled(self):
        return False

    def get_scale(self):
        return 350.0

    def convert_geographic_to_screen_batch(self, lats, lons):
        lats = np.asarray(lats, dtype=np.float64)
        lons = np.asarray(lons, dtype=np.float64)
        return 40.0 + (lons + 3.0) * 60.0, 40.0 + (lats + 3.0) * 48.0


def _wind_columns():
    columns = []
    lats = np.array(_WIND_LATS, dtype=np.float64)
    for lon in _WIND_LONS:
        # Varying in both directions so a channel or axis swap is visible.
        speeds = np.array(
            [4.0 + 2.0 * abs(lon) + 3.0 * (lat + 2.0) for lat in _WIND_LATS],
            dtype=np.float64,
        )
        columns.append((float(lon), lats, np.zeros(len(lats)), speeds))
    return columns


def _wind_settings():
    return SimpleNamespace(gribArrowOpacity=0.8, gribWindGradient=True)


def _wind_scene():
    columns = _wind_columns()
    painter = WindPainter(_wind_settings())
    view = _WindView()
    image = QImage(WIDTH, HEIGHT, QImage.Format.Format_ARGB32_Premultiplied)
    canvas = QPainterCanvas(image)
    painter._draw_color_fill(view, canvas, columns, 1)
    canvas.finish()
    return image


def test_wind_colour_fill_has_no_transparent_band_seams_in_qt():
    """A vertical line through the fill must not cross a blank row.

    The strips this replaced faded at their own top and bottom edge, leaving a
    transparent line between latitude bands. One image for the field has no
    such edge, so every pixel between the first and last covered pixel of a
    sampled vertical line carries the fill.
    """
    image = _wind_scene().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    checked = 0
    # The grid spans x=40..400; stay inside so the exact edge pixel of the
    # clip is not what the assertion is about.
    for x in range(60, 396, 4):
        column = [image.pixelColor(x, y).alpha() for y in range(image.height())]
        covered = [y for y, alpha in enumerate(column) if alpha]
        assert covered, f"column {x} is entirely transparent"
        assert covered[-1] - covered[0] > 40, f"column {x} coverage is too thin"
        for y in range(covered[0], covered[-1] + 1):
            assert column[y], f"transparent seam at ({x}, {y})"
        checked += 1
    assert checked > 50


class _MercatorWindView(_WindView):
    """A regular grid under a real mercator y mapping."""

    _MERC_LATS = (-60.0, -30.0, 0.0, 30.0, 60.0)

    def convert_geographic_to_screen_batch(self, lats, lons):
        lats = np.asarray(lats, dtype=np.float64)
        lons = np.asarray(lons, dtype=np.float64)
        mercator = np.log(np.tan(math.pi / 4.0 + np.radians(lats) / 2.0))
        return 40.0 + (lons + 3.0) * 60.0, 300.0 - mercator * 300.0


def test_wind_colour_fill_records_mercator_row_placement_error():
    """A uniform image scale cannot follow non-uniform mercator row spacing.

    This is the known geometric error of the image approximation, recorded so a
    change in it is visible rather than silent.
    """
    painter = WindPainter(_wind_settings())
    view = _MercatorWindView()
    columns = []
    for lon, _, _, _ in _wind_columns():
        lats = np.array(_MercatorWindView._MERC_LATS, dtype=np.float64)
        speeds = np.array([4.0 + 2.0 * abs(lon) + 3.0 * (lat + 60.0) for lat in lats])
        columns.append((lon, lats, np.zeros(len(lats)), speeds))

    _, screen_ys = painter._grid_screen_points(view, columns, 1)
    actual = np.asarray(screen_ys[0], dtype=np.float64)
    placed = actual[0] + np.linspace(0.0, 1.0, len(actual)) * (actual[-1] - actual[0])
    error = float(np.max(np.abs(placed - actual)))
    print(f"wind fill mercator row placement max error: {error:.2f} px")
    assert error <= (
        WIND_APPROX_MERCATOR_ROW_ERROR_PX + WIND_APPROX_MERCATOR_ROW_ERROR_MARGIN
    )


# --- The carrier's byte layout -----------------------------------------------


def _filled_probe(device_scale=1.0):
    image = CanvasImage.new(2, 2, device_scale)
    canvas = Canvas.for_image(image)
    canvas.set_source_rgba(0.25, 0.2, 0.1, 1.0)
    canvas.rectangle(0, 0, 2, 2)
    canvas.fill()
    canvas.finish()
    image.flush()
    return image


def test_get_data_is_premultiplied_bgra_with_a_device_pixel_stride():
    """``get_data`` is the packed ``0xAARRGGBB`` word, little-endian BGRA.

    The Qt carrier hands out premultiplied ARGB32 bits, whose bytes on this
    machine run blue, green, red, alpha. An opaque pixel premultiplies to
    itself, so the bytes are the fill colour scaled to 8 bits. Sizes are
    device pixels, because every call site builds a QImage from
    ``get_data()``/``get_stride()``.
    """
    image = _filled_probe()

    assert (image.get_width(), image.get_height()) == (2, 2)
    assert image.get_stride() == 2 * 4
    data = bytes(image.get_data())
    assert len(data) == image.get_height() * image.get_stride()

    blue, green, red, alpha = data[:4]
    assert alpha == 0xFF
    assert blue == pytest.approx(0.1 * 255, abs=2)
    assert green == pytest.approx(0.2 * 255, abs=2)
    assert red == pytest.approx(0.25 * 255, abs=2)
    # A blue/green/red channel swap would still satisfy the values above only
    # by accident of this colour, so the ordering is stated directly.
    assert blue < green < red

    # Logical size and device pixels are the same at scale 1.
    assert (image.width, image.height) == (2, 2)


def test_get_data_size_is_reported_in_device_pixels():
    """A scaled image reports device pixels from ``get_width``/``get_stride``."""
    packed = QImage(4, 4, QImage.Format.Format_ARGB32_Premultiplied)
    packed.fill(0xFF102030)
    packed.setDevicePixelRatio(2.0)
    image = CanvasImage.from_qimage(packed)

    assert image.device_scale() == 2.0
    assert (image.get_width(), image.get_height()) == (4, 4)
    assert (image.width, image.height) == (2, 2)
    assert image.get_stride() == 4 * 4
