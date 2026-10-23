"""Read-only chart inspector: live zoom/scale and per-chart window status.

Shows why each loaded chart does or does not contribute to the current view
(scale window, coverage overlap, disabled), mirroring the paint decisions in
ChartCollection/ENCOverlayRenderer without touching the render path.
"""

import logging
import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHeaderView,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gweatherrouting.core.enccharts import chart_scale_visible
from gweatherrouting.rendering.chartclasses import CHART_CLASSES, chart_class_enabled
from gweatherrouting.rendering.gdalrasterchart import GDALRasterChart
from gweatherrouting.rendering.gdalvectorchart import GDALVectorChart
from gweatherrouting.rendering.gshhs import GSHHSVectorChart
from gweatherrouting.rendering.mbtilesrenderer import MBTilesChart
from gweatherrouting.rendering.vectordrawer.cm93chartdrawer import CM93ChartDrawer
from gweatherrouting.rendering.vectordrawer.osmchartdrawer import OSMChartDrawer
from gweatherrouting.rendering.vectordrawer.s57chartdrawer import S57ChartDrawer

logger = logging.getLogger("gweatherrouting")

# Same nominal 96 dpi convention as core.enccharts.
METERS_PER_PIXEL_TO_SCALE = 3779.527559

# OSM seamarks are dropped wholesale past this display scale.
SEAMARKS_MAX_METERS_PER_PIXEL = 300


def viewport_window(map_widget):
    """(min_lat, max_lat, min_lon, max_lon) of the visible viewport."""
    p1, p2 = map_widget.get_bbox()
    lat1, lon1 = p1.get_degrees()
    lat2, lon2 = p2.get_degrees()
    return min(lat1, lat2), max(lat1, lat2), min(lon1, lon2), max(lon1, lon2)


def envelopes_overlap(envelopes, window):
    """OGR envelopes are (min_x, max_x, min_y, max_y) = (lon, lat)."""
    min_lat, max_lat, min_lon, max_lon = window
    return any(
        a <= max_lon and b >= min_lon and c <= max_lat and d >= min_lat
        for a, b, c, d in envelopes
    )


def raster_overlaps_window(metadata, window):
    """Raster metadata bboxes are [(lat, lon), (lat, lon)] corners."""
    min_lat, max_lat, min_lon, max_lon = window
    for _, bbox, *_ in metadata or []:
        if (
            min(bbox[0][0], bbox[1][0]) <= max_lat
            and max(bbox[0][0], bbox[1][0]) >= min_lat
            and min(bbox[0][1], bbox[1][1]) <= max_lon
            and max(bbox[0][1], bbox[1][1]) >= min_lon
        ):
            return True
    return False


def chart_kind(chart):
    if isinstance(chart, MBTilesChart):
        return "MBTiles"
    if isinstance(chart, GSHHSVectorChart):
        return "coastlines"
    if isinstance(chart, GDALRasterChart):
        return "raster"
    if isinstance(chart, GDALVectorChart):
        if isinstance(chart.drawer, S57ChartDrawer):
            return "ENC"
        if isinstance(chart.drawer, CM93ChartDrawer):
            return "CM93"
        if isinstance(chart.drawer, OSMChartDrawer):
            return "seamarks"
        return "vector"
    return type(chart).__name__


def chart_status(chart, window, scale, seamarks_layer=None):
    """Why this chart does or does not paint into the current window.

    Returns one of: "drawing", "available" (off but would draw), "disabled",
    "hidden by scale", "outside window".
    """
    is_enc = isinstance(chart, GDALVectorChart) and isinstance(
        chart.drawer, S57ChartDrawer
    )

    def would_draw():
        if isinstance(chart, MBTilesChart):
            west, south, east, north = chart.info.bounds
            envelopes = (
                ((west, east, south, north),)
                if west <= east
                else ((west, 180, south, north), (-180, east, south, north))
            )
            return envelopes_overlap(envelopes, window)
        if is_enc:
            if not chart_scale_visible(chart._native_scale, scale):
                return False
            if chart._coverage_envelopes and not envelopes_overlap(
                chart._coverage_envelopes, window
            ):
                return False
            return True
        if isinstance(chart, GDALRasterChart):
            return raster_overlaps_window(chart.metadata, window)
        if chart is seamarks_layer:
            return scale <= SEAMARKS_MAX_METERS_PER_PIXEL
        # Coastlines, CM93 and simple vectors paint wherever they have data;
        # their per-feature scale filters are not chart-level decisions.
        return True

    if would_draw():
        return "drawing" if chart.enabled else "available"
    if is_enc and not chart_scale_visible(chart._native_scale, scale):
        return "hidden by scale"
    return "disabled" if not chart.enabled else "outside window"


class ChartDebugPanel(QWidget):
    """Readout of map scale plus every loaded chart's role in the window."""

    REFRESH_MS = 150

    def __init__(self, controller, active_map, maps=(), parent=None):
        """active_map is a callable returning the currently shown map host."""
        super().__init__(parent)
        self.controller = controller
        self._active_map = active_map
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.charts_tree = QTreeWidget()
        self.charts_tree.setHeaderLabels(["Chart", "Kind", "Scale", "Status"])
        self.charts_tree.setRootIsDecorated(False)
        header = self.charts_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.summary)
        layout.addWidget(self.charts_tree)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        for map_widget in maps:
            map_widget.view_changed.connect(self._schedule)
        controller.data.changed.connect(self._schedule)
        for key in ["canadianCharts", *(key for _, key, _ in CHART_CLASSES)]:
            controller.settings.register_on_change(key, lambda _: self._schedule())
        self.refresh()

    def _schedule(self):
        # view_changed fires on every pan frame; coalesce to a cheap poll.
        self._timer.start()

    def refresh(self):
        collection = self.controller.charts
        map_widget = self._active_map()
        if map_widget is None or map_widget.width() <= 0:
            return
        scale = map_widget.get_scale()
        window = viewport_window(map_widget)
        camera_state = map_widget.camera_state
        center_lat, center_lon = camera_state.center_lat, camera_state.center_lon
        active = collection.detailed_chart_at(
            center_lat, center_lon, scale, enabled=True
        )
        self.summary.setText(
            "Zoom {zoom:.2f} · {mpp:.2f} m/px · ≈1:{nominal:,.0f}\n"
            "Center {lat:.4f}, {lon:.4f}\n"
            "Active at center: {active}\nCHS tiles: {chs}\n"
            "Manage charts in Data Library → Charts".format(
                zoom=camera_state.zoom,
                mpp=scale,
                nominal=scale * METERS_PER_PIXEL_TO_SCALE,
                lat=center_lat,
                lon=center_lon,
                active=os.path.basename(active.path) if active else "none",
                chs="on" if self.controller.settings.canadianCharts else "off",
            )
        )
        self.charts_tree.clear()
        for chart in collection.charts:
            native = getattr(chart, "_native_scale", 0) or 0
            native_label = (
                f"z{min(chart.info.zooms)}–{max(chart.info.zooms)}"
                if isinstance(chart, MBTilesChart)
                else f"1:{native:,}" if native else "—"
            )
            item = QTreeWidgetItem(
                [
                    os.path.basename(chart.path.rstrip("/")),
                    chart_kind(chart),
                    native_label,
                    (
                        chart_status(chart, window, scale, collection.osmLayer)
                        if chart_class_enabled(self.controller.settings, chart)
                        else "class disabled"
                    ),
                ]
            )
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            item.setToolTip(0, chart.path)
            self.charts_tree.addTopLevelItem(item)
