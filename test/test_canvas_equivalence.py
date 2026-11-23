"""Measure how far QPainter's dashboard output is from Cairo's (#267 Phase 2).

The seam in ``sailworks.rendering.canvas`` lets the same dashboard render
through either backend. This test records the distance between them so the
Phase 2 tolerance is decided from data, not guessed.

The backends differ in text metrics (different font stacks) and in
antialiasing rounding. Pixel equality is therefore not asserted; the recorded
distance is, together with a ceiling that keeps a real regression visible.
"""

import io
import math
import os
import threading
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cairo
import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QGuiApplication, QImage  # noqa: E402

from sailworks.core.core import BoatInfo  # noqa: E402
from osgeo import ogr  # noqa: E402

from sailworks.rendering.ais import AISRenderer  # noqa: E402
from sailworks.rendering.canvas import (  # noqa: E402
    FILL_EVEN_ODD,
    SLANT_NORMAL,
    WEIGHT_BOLD,
    CairoCanvas,
    Canvas,
    CanvasImage,
    QPainterCanvas,
)
from sailworks.rendering.dashboard import DashboardRenderer  # noqa: E402
from sailworks.rendering.isochrones import (  # noqa: E402
    IsochroneFrame,
    render_isochrones,
)
from sailworks.rendering.style import Style  # noqa: E402
from sailworks.rendering.windpainter import WindPainter  # noqa: E402
from sailworks.rendering.vectordrawer.cm93chartdrawer import (  # noqa: E402
    CM93ChartDrawer,
)
from sailworks.rendering.vectordrawer.s57chartdrawer import S57ChartDrawer  # noqa: E402

WIDTH = 480
HEIGHT = 320

# Recorded when the seam was added. max/mean are per-channel 8-bit values;
# fraction is the share of pixels whose RGBA differs at all. Most of the
# residual is glyph placement from the two font stacks.
BASELINE_MAX_DIFF = 232
BASELINE_MEAN_DIFF = 1.61
BASELINE_DIFF_FRACTION = 0.0239

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


def _compare_composited(cairo_image, painter_image):
    """Compare the premultiplied pixels both backends actually produce.

    Unpremultiplying turns Cairo's almost-transparent antialias fringe into
    saturated colour (alpha 1 can carry red 255), which reports a 255 channel
    difference for a pixel that contributes almost nothing. The composited
    value is what a viewer sees, so the non-text ceiling is measured on it.
    Opaque content is unaffected by the choice.
    """
    left = cairo_image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    right = painter_image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    left_bytes = bytes(left.constBits())
    right_bytes = bytes(right.constBits())
    pixels = left.width() * left.height()
    max_diff = 0
    total_diff = 0
    differing = 0
    non_blank = 0
    for index in range(0, pixels * 4, 4):
        a = left_bytes[index : index + 4]
        b = right_bytes[index : index + 4]
        if any(a):
            non_blank += 1
        diff = max(abs(a[channel] - b[channel]) for channel in range(4))
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


# The tolerance agreed for #272: geometry alone must be nearly identical, while
# a composite that carries glyphs is judged on its mean and coverage because
# the two font stacks cannot place text identically.
NON_TEXT_MAX_DIFF_CEILING = 2
TEXT_MEAN_DIFF_CEILING = 2.0
TEXT_DIFF_FRACTION_CEILING = 0.05

# Recorded with the isochrone and AIS scenes. The isochrone mean and coverage
# stay far inside the text-bearing tolerance; its max is set by the routing
# track's 1 px circles, where both rasterisers distribute the same sub-pixel
# coverage differently (230/255 on Cairo against 190/255 on Qt at one fringe
# pixel). The dash mapping alone measures max 1 and is asserted against the
# strict non-text ceiling.
ISO_BASELINE_MAX_DIFF = 76
ISO_BASELINE_MEAN_DIFF = 0.106
ISO_BASELINE_DIFF_FRACTION = 0.0099

DASH_BASELINE_MAX_DIFF = 1

# The label background is sized from each backend's own text metrics, so the
# residual is glyph placement and those metrics, not the ship geometry.
AIS_BASELINE_MAX_DIFF = 243
AIS_BASELINE_MEAN_DIFF = 0.298
AIS_BASELINE_DIFF_FRACTION = 0.0043

ISO_MAX_MARGIN = 8
AIS_MAX_MARGIN = 32
SCENE_MEAN_MARGIN = 0.05
SCENE_FRACTION_MARGIN = 0.004


class _FixedView:
    """Linear projection at a fixed scale, enough for a synthetic overlay."""

    def __init__(self, width, height, lat0, lon0, scale):
        self.width = width
        self.height = height
        self.lat0 = lat0
        self.lon0 = lon0
        self.scale = scale

    def _xy(self, lat, lon):
        return (
            self.width / 2.0 + (lon - self.lon0) * self.scale,
            self.height / 2.0 - (lat - self.lat0) * self.scale,
        )

    def convert_geographic_to_screen(self, point):
        lat, lon = point.get_degrees()
        return self._xy(lat, lon)

    def convert_geographic_to_screen_batch(self, lats, lons):
        xs = self.width / 2.0 + (np.asarray(lons) - self.lon0) * self.scale
        ys = self.height / 2.0 - (np.asarray(lats) - self.lat0) * self.scale
        return xs, ys


def _iso_view():
    return _FixedView(WIDTH, HEIGHT, 43.0, 5.0, 4000.0)


def _iso_frame():
    # One connected contour (adjacent samples under 3 km), one frontier whose
    # two points are over 3 km apart, then the routing track and exclusions.
    contour = [
        SimpleNamespace(pos=(43.001 + 0.0004 * i, 5.000 + 0.0012 * i)) for i in range(6)
    ]
    gap = (
        SimpleNamespace(pos=(43.004, 5.002)),
        SimpleNamespace(pos=(43.004, 5.050)),
    )
    path = (
        SimpleNamespace(pos=(42.996, 4.996)),
        SimpleNamespace(pos=(43.000, 5.006)),
        SimpleNamespace(pos=(43.004, 5.016)),
        SimpleNamespace(pos=(43.008, 5.026)),
    )
    exclusions = (
        {"latitude": 43.002, "longitude": 5.010},
        {"latitude": 43.006, "longitude": 5.018},
    )
    return IsochroneFrame((tuple(contour), gap), path, exclusions)


def _draw_dashed_track(canvas):
    # A dashed pass through the seam, after a style that sets the width before
    # the dash so the Qt mapping divides by the intended width. The lines are
    # axis-aligned so this measures the dash mapping itself rather than each
    # rasteriser's different path-length measurement on a slope.
    Style.Track.TrackActive.apply_canvas(canvas)
    canvas.move_to(40, 60)
    canvas.line_to(WIDTH - 40, 60)
    canvas.stroke()
    canvas.move_to(40, 170)
    canvas.line_to(WIDTH - 40, 170)
    canvas.stroke()


def _draw_isochrones(canvas):
    render_isochrones(_iso_view(), canvas, _iso_frame(), threading.Event())
    _draw_dashed_track(canvas)


def _cairo_scene(draw):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, WIDTH, HEIGHT)
    draw(CairoCanvas(cairo.Context(surface)))
    surface.flush()
    return QImage(
        surface.get_data(),
        surface.get_width(),
        surface.get_height(),
        surface.get_stride(),
        QImage.Format.Format_ARGB32_Premultiplied,
    ).copy()


def _qpainter_scene(draw):
    image = QImage(WIDTH, HEIGHT, QImage.Format.Format_ARGB32_Premultiplied)
    canvas = QPainterCanvas(image)
    draw(canvas)
    canvas.finish()
    return image


class _AisTarget:
    def __init__(self, latitude, longitude, cog, sog, name, mmsi):
        self.latitude = latitude
        self.longitude = longitude
        self.cog = cog
        self.heading = None
        self.sog = sog
        self.name = name
        self.mmsi = mmsi
        self.category = None

    def has_valid_position(self):
        return True


def _draw_ais(canvas):
    target = _AisTarget(43.0, 5.0, cog=35.0, sog=8.0, name="ALPHA", mmsi=123456789)
    AISRenderer().draw_targets(
        _FixedView(WIDTH, HEIGHT, 43.0, 5.0, 3000.0), canvas, (target,)
    )


def _ais_cairo_image():
    return _cairo_scene(_draw_ais)


def _ais_qpainter_image():
    return _qpainter_scene(_draw_ais)


def _dashed_cairo_image():
    return _cairo_scene(_draw_dashed_track)


def _dashed_qpainter_image():
    return _qpainter_scene(_draw_dashed_track)


def _report(name, measured):
    print(
        f"canvas equivalence ({name}):"
        f" max={measured['max']}"
        f" mean={measured['mean']:.3f}"
        f" differing={measured['differing']}px"
        f" ({measured['fraction']:.2%})"
        f" non-blank={measured['non_blank_fraction']:.2%}"
    )


def test_qpainter_isochrones_stay_within_recorded_distance_from_cairo():
    cairo_image = _cairo_scene(_draw_isochrones)
    painter_image = _qpainter_scene(_draw_isochrones)

    assert (cairo_image.width(), cairo_image.height()) == (
        painter_image.width(),
        painter_image.height(),
    )

    measured = _compare_composited(cairo_image, painter_image)
    _report("isochrones", measured)

    assert measured["non_blank_fraction"] > 0.01
    assert measured["mean"] <= TEXT_MEAN_DIFF_CEILING
    assert measured["fraction"] <= TEXT_DIFF_FRACTION_CEILING
    # Recorded rather than widened: see ISO_BASELINE_MAX_DIFF for the cause.
    assert abs(measured["max"] - ISO_BASELINE_MAX_DIFF) <= ISO_MAX_MARGIN
    assert abs(measured["mean"] - ISO_BASELINE_MEAN_DIFF) <= SCENE_MEAN_MARGIN
    assert (
        abs(measured["fraction"] - ISO_BASELINE_DIFF_FRACTION) <= SCENE_FRACTION_MARGIN
    )


def test_qpainter_dash_mapping_matches_cairo_user_space_lengths():
    measured = _compare_composited(_dashed_cairo_image(), _dashed_qpainter_image())
    _report("dashed track", measured)

    assert measured["non_blank_fraction"] > 0.001
    assert abs(measured["max"] - DASH_BASELINE_MAX_DIFF) <= NON_TEXT_MAX_DIFF_CEILING
    assert measured["max"] <= NON_TEXT_MAX_DIFF_CEILING


def test_qpainter_ais_target_with_label_stays_within_text_tolerance():
    cairo_image = _ais_cairo_image()
    painter_image = _ais_qpainter_image()

    assert (cairo_image.width(), cairo_image.height()) == (
        painter_image.width(),
        painter_image.height(),
    )

    measured = _compare_composited(cairo_image, painter_image)
    _report("ais", measured)

    assert measured["non_blank_fraction"] > 0.001
    # The label background is sized from each backend's own text metrics.
    assert measured["mean"] <= TEXT_MEAN_DIFF_CEILING
    assert measured["fraction"] <= TEXT_DIFF_FRACTION_CEILING
    assert abs(measured["max"] - AIS_BASELINE_MAX_DIFF) <= AIS_MAX_MARGIN
    assert abs(measured["mean"] - AIS_BASELINE_MEAN_DIFF) <= SCENE_MEAN_MARGIN
    assert (
        abs(measured["fraction"] - AIS_BASELINE_DIFF_FRACTION) <= SCENE_FRACTION_MARGIN
    )


def _draw_fill_rule(canvas):
    """Sea background plus a land ring with a piercing ring, filled even-odd."""
    canvas.set_source_rgba(0.0, 0.0, 1.0, 1.0)
    canvas.rectangle(0, 0, WIDTH, HEIGHT)
    canvas.fill()

    canvas.new_path()
    for x0, y0, x1, y1 in (
        (80, 60, 400, 260),
        (180, 110, 300, 210),
    ):
        canvas.move_to(x0, y0)
        canvas.line_to(x1, y0)
        canvas.line_to(x1, y1)
        canvas.line_to(x0, y1)
        canvas.close_path()
    # Set after building the path, as the chart drawer does: the rule applies
    # at fill time, not when the rings are added.
    canvas.set_fill_rule(FILL_EVEN_ODD)
    canvas.set_source_rgba(1.0, 0.0, 0.0, 1.0)
    canvas.fill()


def test_qpainter_fill_rule_punches_the_same_hole_as_cairo():
    cairo_image = _cairo_scene(_draw_fill_rule)
    painter_image = _qpainter_scene(_draw_fill_rule)

    measured = _compare_composited(cairo_image, painter_image)
    _report("fill rule", measured)
    assert measured["max"] <= NON_TEXT_MAX_DIFF_CEILING

    # Semantics, not tolerance: the inner ring must be a hole in both
    # backends, so the sea shows through, while the outer ring stays land.
    for name, image in (("cairo", cairo_image), ("qt", painter_image)):
        hole = image.pixelColor(WIDTH // 2, HEIGHT // 2)
        assert (hole.red(), hole.green(), hole.blue()) == (0, 0, 255), name
        land = image.pixelColor(120, HEIGHT // 2)
        assert (land.red(), land.green(), land.blue()) == (255, 0, 0), name


# --- The chart drawers through the seam -------------------------------------
#
# The CM93 and S57 drawers now draw only through ``Canvas``. These scenes put
# their real paths through both backends: the even-odd DEPARE hole, the
# exposed-edge filter, a sounding's text, procedural point symbols and the
# CM93 light arcs and contour line.
#
# Their maxima are set by partial-coverage fringe pixels and by sub-pixel pen
# widths rather than by the geometry: Cairo paints a faint coverage sample
# where Qt's analytic rasteriser paints nothing, and a 1 px diagonal crossing
# gets a different union coverage. Every solid fill and every symbol centre
# is identical, which is asserted directly below. The agreed #272 mean
# ceiling holds everywhere; the maxima are recorded with their cause, the way
# the isochrone scene records its 76, rather than widened silently.
DEPARE_FRINGE_MAX_DIFF = 10
EDGES_FRINGE_MAX_DIFF = 10
POINTS_FRINGE_MAX_DIFF = 53
ARCS_FRINGE_MAX_DIFF = 34
SOUNDINGS_MAX_DIFF = 204

CHART_FRINGE_MARGIN = 8
CHART_TEXT_MAX_MARGIN = 16


class _ChartView(_FixedView):
    """A fixed camera with the allocation and scale the drawers read."""

    def get_allocated_width(self):
        return self.width

    def get_allocated_height(self):
        return self.height

    def get_scale(self):
        return 40.0


def _chart_view():
    return _ChartView(WIDTH, HEIGHT, 50.0, 5.0, 30.0)


def _colour_diff(left, right, x, y):
    a, b = left.pixelColor(x, y), right.pixelColor(x, y)
    return max(
        abs(a.red() - b.red()),
        abs(a.green() - b.green()),
        abs(a.blue() - b.blue()),
        abs(a.alpha() - b.alpha()),
    )


def _draw_depare_island(canvas):
    """A DEPARE whose interior ring must be punched out by even-odd fill."""
    feature = SimpleNamespace(
        obj_name="DEPARE",
        attributes={"DRVAL1": 5.0, "DRVAL2": 9.0},
        geometry=[
            [(48.0, 1.0), (48.0, 9.0), (52.0, 9.0), (52.0, 1.0), (48.0, 1.0)],
            [(49.3, 4.0), (49.3, 6.0), (50.7, 6.0), (50.7, 4.0), (49.3, 4.0)],
        ],
    )
    S57ChartDrawer.__new__(S57ChartDrawer)._render_area(
        _chart_view(), canvas, feature, Style.chart_palettes["cm93"]
    )


def test_qpainter_depare_island_is_a_hole_in_both_backends():
    cairo_image = _cairo_scene(_draw_depare_island)
    painter_image = _qpainter_scene(_draw_depare_island)

    measured = _compare_composited(cairo_image, painter_image)
    _report("depare island", measured)
    assert measured["non_blank_fraction"] > 0.10
    assert measured["mean"] <= TEXT_MEAN_DIFF_CEILING
    assert abs(measured["max"] - DEPARE_FRINGE_MAX_DIFF) <= CHART_FRINGE_MARGIN

    # The island is background in both backends; the ring around it is filled
    # with the same solid colour, so the two must agree exactly inside it.
    for name, image in (("cairo", cairo_image), ("qt", painter_image)):
        assert image.pixelColor(WIDTH // 2, HEIGHT // 2).alpha() == 0, name
        assert image.pixelColor(130, HEIGHT // 2).alpha() == 255, name
    # A pixel well inside the fill carries the converted colour itself rather
    # than a blend, so it pins the float-to-8-bit conversion to Cairo's.
    assert _colour_diff(cairo_image, painter_image, 150, 110) == 0
    assert _colour_diff(cairo_image, painter_image, 130, HEIGHT // 2) <= (
        NON_TEXT_MAX_DIFF_CEILING
    )


def _coverage_filter(drawer, tolerance):
    coverage = [ogr.CreateGeometryFromWkt("POLYGON((0 45, 10 45, 10 55, 0 55, 0 45))")]
    return drawer._coverage_edge_filter(coverage, tolerance)


def _draw_exposed_edges(canvas):
    """A DEPARE along two coverage edges: only the interior L is stroked."""
    drawer = S57ChartDrawer.__new__(S57ChartDrawer)
    feature = SimpleNamespace(
        obj_name="DEPARE",
        attributes={"DRVAL1": 5.0, "DRVAL2": 9.0},
        geometry=[[(45.0, 0.0), (45.0, 5.0), (50.0, 5.0), (50.0, 0.0), (45.0, 0.0)]],
    )
    drawer._render_area(
        _chart_view(),
        canvas,
        feature,
        Style.chart_palettes["cm93"],
        _coverage_filter(drawer, 0.25),
    )


def _ink_bounds(image):
    """The pixel box that carries ink, in both backends' output."""
    plain = image.convertToFormat(QImage.Format.Format_ARGB32)
    xs, ys = [], []
    for y in range(plain.height()):
        for x in range(plain.width()):
            if plain.pixelColor(x, y).alpha() > 8:
                xs.append(x)
                ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys))


def test_qpainter_exposed_edge_stroke_has_the_same_extent_as_cairo():
    cairo_image = _cairo_scene(_draw_exposed_edges)
    painter_image = _qpainter_scene(_draw_exposed_edges)

    measured = _compare_composited(cairo_image, painter_image)
    _report("exposed edges", measured)
    assert measured["non_blank_fraction"] > 0.10
    assert measured["mean"] <= TEXT_MEAN_DIFF_CEILING
    assert abs(measured["max"] - EDGES_FRINGE_MAX_DIFF) <= CHART_FRINGE_MARGIN

    # The stroked edges reach exactly the same pixels in both backends.
    assert _ink_bounds(cairo_image) == _ink_bounds(painter_image)
    assert _colour_diff(cairo_image, painter_image, 130, HEIGHT // 2) <= (
        NON_TEXT_MAX_DIFF_CEILING
    )


def _draw_soundings(canvas):
    drawer = S57ChartDrawer.__new__(S57ChartDrawer)
    drawer._sounding_boxes = set()
    feature = SimpleNamespace(
        geometry=[(50.0, 3.0 + 0.5 * index, 7.5 + index) for index in range(6)]
    )
    drawer._render_soundings(_chart_view(), canvas, feature)


def test_qpainter_soundings_stay_within_text_tolerance():
    cairo_image = _cairo_scene(_draw_soundings)
    painter_image = _qpainter_scene(_draw_soundings)

    measured = _compare_composited(cairo_image, painter_image)
    _report("soundings", measured)
    assert measured["non_blank_fraction"] > 0.0002
    assert measured["mean"] <= TEXT_MEAN_DIFF_CEILING
    assert measured["fraction"] <= TEXT_DIFF_FRACTION_CEILING
    assert abs(measured["max"] - SOUNDINGS_MAX_DIFF) <= CHART_TEXT_MAX_MARGIN


def _draw_chart_points(canvas):
    """A lateral buoy (procedural arc) and a wreck (procedural lines)."""
    drawer = CM93ChartDrawer.__new__(CM93ChartDrawer)
    palette = Style.chart_palettes["cm93"]
    for feature in (
        SimpleNamespace(
            obj_name="BOYLAT", attributes={"CATLAM": 1}, geometry=[(50.0, 3.0)]
        ),
        SimpleNamespace(obj_name="WRECKS", attributes={}, geometry=[(50.0, 7.0)]),
    ):
        drawer._render_point(_chart_view(), canvas, feature, palette, 40.0)


def test_qpainter_point_symbols_stay_within_recorded_distance_from_cairo():
    cairo_image = _cairo_scene(_draw_chart_points)
    painter_image = _qpainter_scene(_draw_chart_points)

    measured = _compare_composited(cairo_image, painter_image)
    _report("chart points", measured)
    assert measured["non_blank_fraction"] > 0.0002
    assert measured["mean"] <= TEXT_MEAN_DIFF_CEILING
    assert measured["fraction"] <= TEXT_DIFF_FRACTION_CEILING
    assert abs(measured["max"] - POINTS_FRINGE_MAX_DIFF) <= CHART_FRINGE_MARGIN

    # The disc and the X land on the same pixels with the same colours.
    for name, image in (("cairo", cairo_image), ("qt", painter_image)):
        buoy = image.pixelColor(180, 160)
        assert (buoy.red(), buoy.green(), buoy.blue()) == (204, 26, 26), name
        wreck = image.pixelColor(300, 160)
        assert (wreck.red(), wreck.green(), wreck.blue()) == (76, 76, 76), name


def _draw_cm93_arcs_and_contour(canvas):
    """The CM93 contour line plus the light symbol's arcs and rays."""
    drawer = CM93ChartDrawer.__new__(CM93ChartDrawer)
    drawer._render_line(
        _chart_view(),
        canvas,
        SimpleNamespace(
            obj_name="DEPCNT",
            attributes={},
            geometry=[(47.0, 2.0), (48.5, 4.0), (50.0, 6.0), (51.5, 8.0)],
        ),
        Style.chart_palettes["cm93"],
    )
    drawer._draw_light(canvas, WIDTH / 2, HEIGHT / 2)


def test_qpainter_cm93_arcs_and_contour_stay_within_recorded_distance():
    cairo_image = _cairo_scene(_draw_cm93_arcs_and_contour)
    painter_image = _qpainter_scene(_draw_cm93_arcs_and_contour)

    measured = _compare_composited(cairo_image, painter_image)
    _report("cm93 arcs", measured)
    assert measured["non_blank_fraction"] > 0.0002
    assert measured["mean"] <= TEXT_MEAN_DIFF_CEILING
    assert measured["fraction"] <= TEXT_DIFF_FRACTION_CEILING
    assert abs(measured["max"] - ARCS_FRINGE_MAX_DIFF) <= CHART_FRINGE_MARGIN

    # The arc fill is the same yellow disc in both backends.
    for name, image in (("cairo", cairo_image), ("qt", painter_image)):
        light = image.pixelColor(WIDTH // 2, HEIGHT // 2)
        assert (light.red(), light.green(), light.blue()) == (255, 255, 0), name


# --- Image blits through the seam (Phase 2 slice 5) ------------------------
#
# A scaled image blit has solid interiors and sharp colour boundaries; the
# solid interior must match exactly enough to catch a regressed sampler,
# while the boundary fringe is reported, not hidden behind a wider ceiling.

BLIT_SOURCE_W = 48
BLIT_SOURCE_H = 32
BLIT_INTERIOR_MARGIN = 4
IMAGE_INTERIOR_MAX_DIFF = 2


def _blit_source_array():
    """Four solid quadrants: flat interiors with sharp edges between them."""
    array = np.empty((BLIT_SOURCE_H, BLIT_SOURCE_W), dtype=np.uint32)
    colours = (0xFF3366AA, 0xFFAA6633, 0xFF223311, 0xFFCCCCCC)
    for y in range(BLIT_SOURCE_H):
        for x in range(BLIT_SOURCE_W):
            index = (1 if x >= BLIT_SOURCE_W // 2 else 0) + (
                2 if y >= BLIT_SOURCE_H // 2 else 0
            )
            array[y, x] = colours[index]
    return array


def _quadrant_interior_regions(width, height, scale, margin):
    """Destination rects ``margin`` source pixels inside every quadrant."""
    half_w, half_h = width // 2, height // 2
    regions = []
    for x0, x1 in ((0, half_w), (half_w, width)):
        for y0, y1 in ((0, half_h), (half_h, height)):
            regions.append(
                (
                    (x0 + margin) * scale,
                    (y0 + margin) * scale,
                    (x1 - margin) * scale,
                    (y1 - margin) * scale,
                )
            )
    return regions


def _image_from_surface(surface):
    surface.flush()
    return QImage(
        surface.get_data(),
        surface.get_width(),
        surface.get_height(),
        surface.get_stride(),
        QImage.Format.Format_ARGB32_Premultiplied,
    ).copy()


def _cairo_blit(array, scale, smooth, pad):
    width, height = BLIT_SOURCE_W * scale, BLIT_SOURCE_H * scale
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    source = cairo.ImageSurface.create_for_data(
        array,
        cairo.Format.ARGB32,
        BLIT_SOURCE_W,
        BLIT_SOURCE_H,
        4 * BLIT_SOURCE_W,
    )
    canvas = CairoCanvas(cairo.Context(surface))
    canvas.draw_image(
        CanvasImage.from_cairo_surface(source),
        0,
        0,
        width=width,
        height=height,
        smooth=smooth,
        pad=pad,
    )
    return _image_from_surface(surface)


def _qpainter_blit(array, scale, smooth, pad):
    width, height = BLIT_SOURCE_W * scale, BLIT_SOURCE_H * scale
    source = QImage(
        array,
        BLIT_SOURCE_W,
        BLIT_SOURCE_H,
        4 * BLIT_SOURCE_W,
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    canvas = QPainterCanvas(image)
    canvas.draw_image(
        CanvasImage.from_qimage(source),
        0,
        0,
        width=width,
        height=height,
        smooth=smooth,
        pad=pad,
    )
    canvas.finish()
    return image


def _interior_max_diff(left, right, regions):
    left = left.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    right = right.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    left_bytes = bytes(left.constBits())
    right_bytes = bytes(right.constBits())
    stride = left.bytesPerLine()
    worst = 0
    for x0, y0, x1, y1 in regions:
        for y in range(y0, y1):
            for x in range(x0, x1):
                offset = y * stride + x * 4
                a = left_bytes[offset : offset + 4]
                b = right_bytes[offset : offset + 4]
                worst = max(worst, max(abs(a[c] - b[c]) for c in range(4)))
    return worst


@pytest.mark.parametrize("scale", [1, 3])
def test_scaled_image_blit_with_padding_stays_inside_solid_interior(scale):
    array = _blit_source_array()
    cairo_image = _cairo_blit(array, scale, smooth=True, pad=True)
    painter_image = _qpainter_blit(array, scale, smooth=True, pad=True)

    measured = _compare_composited(cairo_image, painter_image)
    _report(f"image blit {scale}x smooth+pad", measured)
    regions = _quadrant_interior_regions(
        BLIT_SOURCE_W, BLIT_SOURCE_H, scale, BLIT_INTERIOR_MARGIN
    )
    interior = _interior_max_diff(cairo_image, painter_image, regions)
    print(
        f"canvas equivalence (image blit {scale}x smooth+pad): interior max={interior}"
    )
    assert interior <= IMAGE_INTERIOR_MAX_DIFF


@pytest.mark.parametrize("scale", [1, 3])
def test_nearest_image_blit_stays_inside_solid_interior(scale):
    array = _blit_source_array()
    cairo_image = _cairo_blit(array, scale, smooth=False, pad=False)
    painter_image = _qpainter_blit(array, scale, smooth=False, pad=False)

    measured = _compare_composited(cairo_image, painter_image)
    _report(f"image blit {scale}x nearest", measured)
    regions = _quadrant_interior_regions(
        BLIT_SOURCE_W, BLIT_SOURCE_H, scale, BLIT_INTERIOR_MARGIN
    )
    interior = _interior_max_diff(cairo_image, painter_image, regions)
    print(f"canvas equivalence (image blit {scale}x nearest): interior max={interior}")
    assert interior <= IMAGE_INTERIOR_MAX_DIFF


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


def _cairo_raster_scene(chart, scale):
    width, height = chart.width * scale, chart.height * scale
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    canvas = CairoCanvas(cairo.Context(surface))
    canvas.draw_image(
        chart.image_for(canvas), 0, 0, width=width, height=height, smooth=None
    )
    return _image_from_surface(surface)


def _qpainter_raster_scene(chart, scale):
    width, height = chart.width * scale, chart.height * scale
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    canvas = QPainterCanvas(image)
    canvas.draw_image(
        chart.image_for(canvas), 0, 0, width=width, height=height, smooth=None
    )
    canvas.finish()
    return image


def test_raster_chart_image_matches_on_both_backends(tmp_path):
    chart = _raster_chart(tmp_path)
    scale = 3
    cairo_image = _cairo_raster_scene(chart, scale)
    painter_image = _qpainter_raster_scene(chart, scale)

    measured = _compare_composited(cairo_image, painter_image)
    _report("raster chart image", measured)
    regions = _quadrant_interior_regions(
        chart.width, chart.height, scale, BLIT_INTERIOR_MARGIN
    )
    interior = _interior_max_diff(cairo_image, painter_image, regions)
    print(f"canvas equivalence (raster chart image): interior max={interior}")
    assert interior <= IMAGE_INTERIOR_MAX_DIFF


# --- PNG tile decode and labels through the seam (#267 Phase 2 slice 6) ----
#
# The raster tile renderers decode PNG bytes into a carrier and blit them
# scaled with edge padding; the CHS label and the MBTiles attribution each set
# a font face before they draw. Both are measured here, with the blit's solid
# interior kept separate from its boundary fringe.

LABEL_TEXT = "Offline imagery · © CHS / Fisheries and Oceans Canada"

# The label rectangle is fixed, so the residual is glyph placement from the
# two font stacks; the mean and coverage allow for it the same way the AIS
# label does. The max is recorded, not widened.
PNG_LABEL_BASELINE_MAX_DIFF = 179
PNG_LABEL_MAX_MARGIN = 16


def _png_tile_bytes():
    """Encode the four-quadrant blit source as PNG, as a tile archive would."""
    array = _blit_source_array()
    surface = cairo.ImageSurface.create_for_data(
        array,
        cairo.Format.ARGB32,
        BLIT_SOURCE_W,
        BLIT_SOURCE_H,
        4 * BLIT_SOURCE_W,
    )
    stream = io.BytesIO()
    surface.write_to_png(stream)
    return stream.getvalue()


def _cairo_png_scene(data, scale):
    source = CanvasImage.from_png_bytes(data)
    width, height = int(source.width) * scale, int(source.height) * scale
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    canvas = CairoCanvas(cairo.Context(surface))
    canvas.draw_image(source, 0, 0, width=width, height=height, smooth=True, pad=True)
    return _image_from_surface(surface)


def _qpainter_png_scene(data, scale):
    source = CanvasImage.from_png_bytes(data, backend="qt")
    width, height = int(source.width) * scale, int(source.height) * scale
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    canvas = QPainterCanvas(image)
    canvas.draw_image(source, 0, 0, width=width, height=height, smooth=True, pad=True)
    canvas.finish()
    return image


@pytest.mark.parametrize("scale", [1, 3])
def test_png_tile_decode_blit_stays_inside_solid_interior(scale):
    data = _png_tile_bytes()
    cairo_image = _cairo_png_scene(data, scale)
    painter_image = _qpainter_png_scene(data, scale)

    assert (cairo_image.width(), cairo_image.height()) == (
        painter_image.width(),
        painter_image.height(),
    )
    measured = _compare_composited(cairo_image, painter_image)
    _report(f"png tile {scale}x smooth+pad", measured)
    regions = _quadrant_interior_regions(
        BLIT_SOURCE_W, BLIT_SOURCE_H, scale, BLIT_INTERIOR_MARGIN
    )
    interior = _interior_max_diff(cairo_image, painter_image, regions)
    print(f"canvas equivalence (png tile {scale}x smooth+pad): interior max={interior}")
    assert interior <= IMAGE_INTERIOR_MAX_DIFF


def _draw_label(canvas):
    canvas.set_source_rgba(1, 1, 1, 0.85)
    canvas.rectangle(20, 20, 300, 18)
    canvas.fill()
    canvas.set_source_rgb(0.15, 0.15, 0.15)
    canvas.set_font_face("Sans")
    canvas.set_font_size(10)
    canvas.move_to(24, 33)
    canvas.show_text(LABEL_TEXT)


def test_png_label_after_set_font_face_stays_within_text_tolerance():
    cairo_image = _cairo_scene(_draw_label)
    painter_image = _qpainter_scene(_draw_label)

    measured = _compare_composited(cairo_image, painter_image)
    _report("png label", measured)

    assert measured["non_blank_fraction"] > 0.0002
    assert measured["mean"] <= TEXT_MEAN_DIFF_CEILING
    assert measured["fraction"] <= TEXT_DIFF_FRACTION_CEILING
    assert abs(measured["max"] - PNG_LABEL_BASELINE_MAX_DIFF) <= PNG_LABEL_MAX_MARGIN


# --- Wind colour fill: the image approximation against the cairo mesh -------

# The mesh is cairo-only. Every other backend gets one RGBA image of the
# sampled grid, built from the same ``wind_color`` node colours and blitted
# once, so the backend's bilinear filter interpolates between nodes. That is an
# intentional approximation of ``cairo.MeshPattern`` (see
# ``WindPainter._draw_color_fill_image``): the same colours over the same
# cells, but filter arithmetic instead of mesh arithmetic, and rows placed
# evenly between the first and last latitude instead of at their true mercator
# offsets. Production keeps the cairo mesh, so its own output is unaffected.
#
# The numbers below are measured, not a design target. Each backend records its
# own measurement: the two use different filter implementations by design, so
# there is no cross-backend agreement check. For comparison, the strips this
# replaced measured max=61 mean=2.343 fraction=0.4446 on Qt.
WIND_APPROX_BASELINE_MAX_DIFF = 61
WIND_APPROX_BASELINE_MEAN_DIFF = 0.6444
WIND_APPROX_BASELINE_FRACTION = 0.3958
WIND_APPROX_MAX_MARGIN = 8
WIND_APPROX_MEAN_MARGIN = 0.35
WIND_APPROX_FRACTION_MARGIN = 0.03

# The same approximation measured on the Cairo backend as well. It is recorded
# separately because Cairo and Qt filter independently; it is not expected to
# match the Qt numbers and nothing asserts that it does.
WIND_APPROX_CAIRO_MAX_DIFF = 61
WIND_APPROX_CAIRO_MEAN_DIFF = 0.4794
WIND_APPROX_CAIRO_FRACTION = 0.2634

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


def _wind_scene(backend, path):
    columns = _wind_columns()
    painter = WindPainter(_wind_settings())
    view = _WindView()
    if backend == "qt":
        image = QImage(WIDTH, HEIGHT, QImage.Format.Format_ARGB32_Premultiplied)
        canvas = QPainterCanvas(image)
        painter._draw_color_fill(view, canvas, columns, 1)
        canvas.finish()
        return image
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, WIDTH, HEIGHT)
    canvas = CairoCanvas(cairo.Context(surface))
    if path == "mesh":
        painter._draw_color_fill(view, canvas, columns, 1)
    else:
        projected = painter._grid_screen_points(view, columns, 1)
        opacity = painter.settings_manager.gribArrowOpacity * 0.3
        painter._draw_color_fill_image(view, canvas, columns, 1, opacity, *projected)
    surface.flush()
    return QImage(
        surface.get_data(),
        surface.get_width(),
        surface.get_height(),
        surface.get_stride(),
        QImage.Format.Format_ARGB32_Premultiplied,
    ).copy()


def test_wind_colour_fill_approximation_distance_is_recorded():
    mesh = _wind_scene("cairo", "mesh")
    cairo_image = _wind_scene("cairo", "image")
    painter_image = _wind_scene("qt", "image")

    mesh_measured = _compare_composited(mesh, mesh)
    assert mesh_measured["non_blank_fraction"] > MIN_NON_BLANK_FRACTION

    cairo_measured = _compare_composited(mesh, cairo_image)
    painter_measured = _compare_composited(mesh, painter_image)
    _report("wind fill image on cairo vs mesh", cairo_measured)
    _report("wind fill image on qt vs cairo mesh", painter_measured)

    # Each backend is recorded against the mesh with its own measured numbers.
    # The two backends filter independently by design, so nothing requires
    # them to agree with each other.
    for measured, max_diff, mean_diff, fraction in (
        (
            painter_measured,
            WIND_APPROX_BASELINE_MAX_DIFF,
            WIND_APPROX_BASELINE_MEAN_DIFF,
            WIND_APPROX_BASELINE_FRACTION,
        ),
        (
            cairo_measured,
            WIND_APPROX_CAIRO_MAX_DIFF,
            WIND_APPROX_CAIRO_MEAN_DIFF,
            WIND_APPROX_CAIRO_FRACTION,
        ),
    ):
        assert abs(measured["max"] - max_diff) <= WIND_APPROX_MAX_MARGIN
        assert abs(measured["mean"] - mean_diff) <= WIND_APPROX_MEAN_MARGIN
        assert abs(measured["fraction"] - fraction) <= WIND_APPROX_FRACTION_MARGIN


def test_wind_colour_fill_has_no_transparent_band_seams_in_qt():
    """A vertical line through the fill must not cross a blank row.

    The strips this replaced faded at their own top and bottom edge, leaving a
    transparent line between latitude bands. One image for the field has no
    such edge, so every pixel between the first and last covered pixel of a
    sampled vertical line carries the fill.
    """
    image = _wind_scene("qt", "image").convertToFormat(
        QImage.Format.Format_ARGB32_Premultiplied
    )
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


# --- Outlined text: the isobar label's stroke-then-fill of a text path ------


def _draw_outlined_label(canvas):
    canvas.set_font_face("Sans", SLANT_NORMAL, WEIGHT_BOLD)
    canvas.set_font_size(28)
    canvas.move_to(60, 170)
    canvas.text_path("1016")
    canvas.set_source_rgba(0, 0, 0, 0.85)
    canvas.set_line_width(3)
    canvas.stroke_preserve()
    canvas.set_source_rgba(1, 1, 1, 0.85)
    canvas.fill()


def test_outlined_text_path_label_stays_within_text_tolerance():
    cairo_image = _cairo_scene(_draw_outlined_label)
    painter_image = _qpainter_scene(_draw_outlined_label)

    measured = _compare_composited(cairo_image, painter_image)
    _report("outlined label", measured)

    assert measured["non_blank_fraction"] > 0.0005
    assert measured["mean"] <= TEXT_MEAN_DIFF_CEILING
    assert measured["fraction"] <= TEXT_DIFF_FRACTION_CEILING


# --- The RGBA carrier's channel order ----------------------------------------


def _rgba_probe_cairo():
    image = CanvasImage.from_rgba(4, 1, _RGBA_PROBE, backend="cairo")
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 4, 1)
    canvas = CairoCanvas(cairo.Context(surface))
    canvas.draw_image(image, 0, 0)
    surface.flush()
    return QImage(
        surface.get_data(),
        4,
        1,
        surface.get_stride(),
        QImage.Format.Format_ARGB32_Premultiplied,
    ).copy()


def _rgba_probe_qt():
    image = CanvasImage.from_rgba(4, 1, _RGBA_PROBE, backend="qt")
    target = QImage(4, 1, QImage.Format.Format_ARGB32_Premultiplied)
    canvas = QPainterCanvas(target)
    canvas.draw_image(image, 0, 0)
    canvas.finish()
    return target


# Row-major premultiplied RGBA: red, green, blue, then red at 50% alpha.
_RGBA_PROBE = bytes(
    (
        255,
        0,
        0,
        255,
        0,
        255,
        0,
        255,
        0,
        0,
        255,
        255,
        128,
        0,
        0,
        128,
    )
)


def test_rgba_carrier_preserves_channel_order_on_both_backends():
    expected = (
        (255, 0, 0, 255),
        (0, 255, 0, 255),
        (0, 0, 255, 255),
        (255, 0, 0, 128),
    )
    for backend, image in (
        ("cairo", _rgba_probe_cairo()),
        ("qt", _rgba_probe_qt()),
    ):
        observed = tuple(
            (
                image.pixelColor(x, 0).red(),
                image.pixelColor(x, 0).green(),
                image.pixelColor(x, 0).blue(),
                image.pixelColor(x, 0).alpha(),
            )
            for x in range(4)
        )
        assert observed == expected, (backend, observed)


def _filled_probe(backend):
    image = CanvasImage.new(2, 2, 1.0, backend=backend)
    canvas = Canvas.for_image(image)
    canvas.set_source_rgba(0.25, 0.2, 0.1, 1.0)
    canvas.rectangle(0, 0, 2, 2)
    canvas.fill()
    canvas.finish()
    image.flush()
    return image


def test_get_data_byte_order_and_stride_agree_across_backends():
    """``get_data`` must be the same premultiplied BGRA buffer for both.

    The Qt carrier hands out premultiplied ARGB32 bits and cairo hands out
    ``FORMAT_ARGB32`` bytes; both are the packed ``0xAARRGGBB`` word, so the
    bytes run blue, green, red, alpha. An opaque pixel premultiplies to
    itself, so the two backends agree to within the one rounding step where
    they convert ``0.1 * 255`` differently (25 versus 26).
    """
    cairo_image = _filled_probe("cairo")
    qt_image = _filled_probe("qt")

    assert (cairo_image.get_width(), cairo_image.get_height()) == (2, 2)
    assert (qt_image.get_width(), qt_image.get_height()) == (2, 2)
    assert cairo_image.get_stride() == qt_image.get_stride() == 8

    cairo_px = bytes(cairo_image.get_data())[:4]
    qt_px = bytes(qt_image.get_data())[:4]
    assert cairo_px[3] == qt_px[3] == 0xFF
    # Cairo and Qt round 0.1 * 255 differently (25 or 26), so the blue
    # channel may differ by one; green and red agree byte for byte.
    for px in (cairo_px, qt_px):
        assert px[0] < px[1] < px[2]
    for left, right in zip(cairo_px, qt_px):
        assert abs(left - right) <= 1, (cairo_px, qt_px)
