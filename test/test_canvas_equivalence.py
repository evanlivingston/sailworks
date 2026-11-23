"""Measure how far QPainter's dashboard output is from Cairo's (#267 Phase 2).

The seam in ``sailworks.rendering.canvas`` lets the same dashboard render
through either backend. This test records the distance between them so the
Phase 2 tolerance is decided from data, not guessed.

The backends differ in text metrics (different font stacks) and in
antialiasing rounding. Pixel equality is therefore not asserted; the recorded
distance is, together with a ceiling that keeps a real regression visible.
"""

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cairo
import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QGuiApplication, QImage  # noqa: E402

from sailworks.core.core import BoatInfo  # noqa: E402
from sailworks.rendering.canvas import CairoCanvas, QPainterCanvas  # noqa: E402
from sailworks.rendering.dashboard import DashboardRenderer  # noqa: E402

WIDTH = 480
HEIGHT = 320

# Recorded when the seam was added. max/mean are per-channel 8-bit values;
# fraction is the share of pixels whose RGBA differs at all. Most of the
# residual is glyph placement from the two font stacks.
BASELINE_MAX_DIFF = 232
BASELINE_MEAN_DIFF = 1.63
BASELINE_DIFF_FRACTION = 0.0324

MAX_DIFF_MARGIN = 16
MEAN_DIFF_MARGIN = 1.0
FRACTION_MARGIN = 0.01

# Above these the difference is no longer explainable by text metrics and
# antialiasing; fail and report rather than widening the assertion.
MEAN_DIFF_CEILING = 8.0
DIFF_FRACTION_CEILING = 0.20

MIN_NON_BLANK_FRACTION = 0.10


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QGuiApplication.instance()
    if app is None:
        try:
            app = QGuiApplication([])
        except Exception as exc:  # pragma: no cover - headless without a platform
            pytest.skip(f"Qt platform unavailable: {exc}")
    return app


def _renderer():
    renderer = DashboardRenderer()
    boat = BoatInfo()
    boat.sog, boat.cog, boat.aws, boat.awa = 5.4, 213.0, 14.2, 47.0
    boat.tws, boat.twa, boat.depth = 11.3, 38.0, 7.8
    renderer.boat_info = boat
    return renderer


def _view():
    return SimpleNamespace(
        get_allocated_width=lambda: WIDTH,
        get_allocated_height=lambda: HEIGHT,
    )


def _cairo_image():
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, WIDTH, HEIGHT)
    _renderer()._draw_dashboard(_view(), CairoCanvas(cairo.Context(surface)))
    surface.flush()
    return QImage(
        surface.get_data(),
        surface.get_width(),
        surface.get_height(),
        surface.get_stride(),
        QImage.Format.Format_ARGB32_Premultiplied,
    ).copy()


def _qpainter_image():
    image = QImage(WIDTH, HEIGHT, QImage.Format.Format_ARGB32_Premultiplied)
    canvas = QPainterCanvas(image)
    _renderer()._draw_dashboard(_view(), canvas)
    canvas.finish()
    return image


def _compare(cairo_image, painter_image):
    # Compare unpremultiplied so the numbers are channel colours, not
    # premultiplied products.
    left = cairo_image.convertToFormat(QImage.Format.Format_ARGB32)
    right = painter_image.convertToFormat(QImage.Format.Format_ARGB32)
    pixels = left.width() * left.height()
    max_diff = 0
    total_diff = 0
    differing = 0
    non_blank = 0
    for y in range(left.height()):
        for x in range(left.width()):
            a = left.pixelColor(x, y)
            b = right.pixelColor(x, y)
            if a.alpha() or a.red() or a.green() or a.blue():
                non_blank += 1
            diff = max(
                abs(a.red() - b.red()),
                abs(a.green() - b.green()),
                abs(a.blue() - b.blue()),
                abs(a.alpha() - b.alpha()),
            )
            total_diff += diff
            if diff:
                differing += 1
                max_diff = max(max_diff, diff)
    return {
        "max": max_diff,
        "mean": total_diff / pixels,
        "differing": differing,
        "fraction": differing / pixels,
        "non_blank_fraction": non_blank / pixels,
    }


def test_qpainter_dashboard_stays_within_recorded_distance_from_cairo():
    cairo_image = _cairo_image()
    painter_image = _qpainter_image()

    assert (cairo_image.width(), cairo_image.height()) == (
        painter_image.width(),
        painter_image.height(),
    )

    measured = _compare(cairo_image, painter_image)
    print(
        "canvas equivalence:"
        f" max={measured['max']}"
        f" mean={measured['mean']:.3f}"
        f" differing={measured['differing']}px"
        f" ({measured['fraction']:.2%})"
        f" non-blank={measured['non_blank_fraction']:.2%}"
    )

    assert measured["non_blank_fraction"] > MIN_NON_BLANK_FRACTION
    assert measured["mean"] <= MEAN_DIFF_CEILING
    assert measured["fraction"] <= DIFF_FRACTION_CEILING

    assert abs(measured["max"] - BASELINE_MAX_DIFF) <= MAX_DIFF_MARGIN
    assert abs(measured["mean"] - BASELINE_MEAN_DIFF) <= MEAN_DIFF_MARGIN
    assert abs(measured["fraction"] - BASELINE_DIFF_FRACTION) <= FRACTION_MARGIN
