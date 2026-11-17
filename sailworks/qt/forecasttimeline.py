"""Forecast transport and day timeline scrubber.

Shows month/year bounds at the top edges, a day-by-day cell strip
in the middle, and vertical markers for the current forecast time.
"""

import logging
import math
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QDateTime, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygon
from PySide6.QtWidgets import (
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sailworks.core.forecasttimeline import (
    TIME_UNITS,
    forecast_bounds,
    step_forecast_time,
)
from .theme import symbol_icon
from .forecastoverview import ForecastSourceDrawer, source_rows

logger = logging.getLogger("sailworks")


# Playback runs at twenty updates per second while keeping the user-facing
# speed at two forecast hours per real second (50 ms x 360 s).
PLAY_INTERVAL_MS = 50
PLAY_STEP_SECONDS = 360


def _set_text(label, text):
    """Set label text only when it changes.

    Assigning identical text still queues a relayout and repaint, and startup
    refreshes the timeline once per added source.
    """
    if label.text() != text:
        label.setText(text)


class DayStripWidget(QWidget):
    """Horizontal day-by-day timeline strip.

    Draws one equal-width cell per day across the forecast range, each
    labelled ``Mon 16``.  Vertical markers indicate the current
    forecast time (blue) and an optional reference time (red).  Clicking
    and dragging seeks continuously through the forecast range.
    """

    time_selected = Signal(float)
    now_changed = Signal(float)

    CELL_HEIGHT = 22
    TRACK_HEIGHT = 4

    # Palette. One source of truth for the paint path.
    STRIP_BG = "#1e1e1e"
    CELL_BG = "#242424"
    CELL_SEPARATOR = "#3f3f3f"
    LABEL = "#b0b0ad"
    LABEL_HOVER = "#e0e0dd"
    CELL_HOVER = "#2f2f2f"
    TRACK = "#4a4a4a"
    DOT = "#9a9a95"
    NOW = "#e6c229"
    PLAYHEAD = "#3584e4"
    PLAYHEAD_HALO = "#141414"
    MARKER = "#c05252"

    DEFAULT_STEP_SECONDS = 900

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bounds = None  # (start, end) timestamps
        self.position = None  # current forecast time (blue marker)
        self.marker = None  # optional reference time (red marker)
        self.now = datetime.now(timezone.utc).timestamp()
        self.valid_times = ()  # forecast valid-time timestamps (dot markers)
        self.axis_left_margin = 0
        self.axis_right_margin = 0
        # Blender-style timeline type: small, fixed, independent of the app font.
        self._label_font = QFont(self.font())
        self._label_font.setPixelSize(10)
        self.setMinimumHeight(self.CELL_HEIGHT + self.TRACK_HEIGHT + 6)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self._cell_rects = []  # list of (QRect, noon_timestamp)
        self._hover_index = None  # index into _cell_rects, or None
        self._saved_cursor = None  # cursor restored after hovering
        self.step_seconds = self.DEFAULT_STEP_SECONDS
        self._dragging = False
        self._now_timer = QTimer(self)
        self._now_timer.setInterval(30_000)
        self._now_timer.timeout.connect(self._update_now)

    def showEvent(self, event):  # noqa: N802 - Qt virtual method name
        super().showEvent(event)
        self._update_now()
        self._now_timer.start()

    def stop_clock(self):
        self._now_timer.stop()

    def _update_now(self):
        self.now = datetime.now(timezone.utc).timestamp()
        self.update()
        self.now_changed.emit(self.now)

    def set_bounds(self, start, end):
        if start is None or end is None:
            self.bounds = None
            self._stop_dragging()
        else:
            self.bounds = (start, end)
        self._cell_rects = []
        self.update()

    def set_position(self, stamp):
        self.position = stamp
        self.update()

    def set_marker(self, stamp):
        self.marker = stamp
        self.update()

    def set_valid_times(self, stamps):
        self.valid_times = tuple(stamps or ())
        self.update()

    def set_step_seconds(self, seconds):
        """Set the arrow-key step; fall back to the default on bad input."""
        try:
            value = int(seconds)
        except (TypeError, ValueError):
            value = 0
        self.step_seconds = value if value > 0 else self.DEFAULT_STEP_SECONDS

    def keyPressEvent(self, event):
        bounds = self.bounds
        if not bounds or bounds[0] is None or bounds[1] is None:
            super().keyPressEvent(event)
            return
        start, end = bounds
        key = event.key()
        if key in (Qt.Key.Key_Home, Qt.Key.Key_End):
            self.time_selected.emit(start if key == Qt.Key.Key_Home else end)
            event.accept()
            return
        if key not in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            super().keyPressEvent(event)
            return
        if self.position is None:
            super().keyPressEvent(event)
            return
        step = self.step_seconds
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            step *= 4
        delta = step if key == Qt.Key.Key_Right else -step
        self.time_selected.emit(min(max(self.position + delta, start), end))
        event.accept()

    def _day_cells(self, start, end):
        """Return [(rect, noon_timestamp), ...] for each day in [start, end]."""
        start_dt = datetime.fromtimestamp(start, tz=timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        days = []
        day = start_dt
        while day.timestamp() <= end:
            days.append(day.timestamp())
            day += timedelta(days=1)
        if not days:
            return []
        width = self.width() - self.axis_left_margin - self.axis_right_margin
        if width <= 0:
            return []
        cell_width = width / len(days)
        cells = []
        for i, day_ts in enumerate(days):
            rect = QRect(
                self.axis_left_margin + int(i * cell_width),
                0,
                max(1, int(cell_width)),
                self.CELL_HEIGHT,
            )
            cells.append((rect, day_ts + 12 * 3600))
        return cells

    def paintEvent(self, event):
        if not self.bounds:
            return
        start, end = self.bounds
        if start is None or end is None:
            return
        try:
            span = end - start
            painter = QPainter(self)

            # Fill the whole strip: the cells no longer cover it edge to edge.
            painter.fillRect(self.rect(), QColor("#1e1e1e"))

            self._cell_rects = self._day_cells(start, end)

            # Draw day cells.
            painter.setPen(QPen(QColor("#333333"), 1))
            for rect, _noon in self._cell_rects:
                painter.fillRect(rect, QColor("#242424"))
                painter.drawRect(rect)

            # Draw day labels.
            painter.setPen(QPen(QColor("#b0b0ad"), 1))
            painter.setFont(self._label_font)
            fm = painter.fontMetrics()
            for rect, noon in self._cell_rects:
                day = datetime.fromtimestamp(noon - 12 * 3600, tz=timezone.utc)
                label = day.strftime("%d - %a")
                tw = fm.horizontalAdvance(label)
                painter.drawText(
                    rect.center().x() - tw // 2,
                    rect.center().y() + fm.ascent() // 2 - 1,
                    label,
                )

            # Separator line under the cells.
            track_y = self.CELL_HEIGHT + 2
            painter.setPen(QPen(QColor("#3a3a3a"), 1))
            axis_right = self.width() - self.axis_right_margin
            painter.drawLine(self.axis_left_margin, track_y, axis_right, track_y)

            # Tick marks below the track, one per day boundary.
            if self._cell_rects:
                width = self.width() - self.axis_left_margin - self.axis_right_margin
                cell_width = width / len(self._cell_rects)
                x = self.axis_left_margin + cell_width
                while x < self.width() - self.axis_right_margin:
                    painter.drawLine(
                        int(x), track_y, int(x), track_y + self.TRACK_HEIGHT
                    )
                    x += cell_width

            # Small dots identify actual forecast valid times.  Keep them on
            # the bottom edge, independently of the day-cell boundaries.
            if self.width() > 0:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#555555"))
                radius = 2
                dot_y = self.height() - radius - 1
                for stamp in self.valid_times:
                    if not math.isfinite(stamp) or stamp < start or stamp > end:
                        continue
                    plot_width = max(
                        0,
                        self.width()
                        - self.axis_left_margin
                        - self.axis_right_margin
                        - 1,
                    )
                    px = (
                        self.axis_left_margin
                        + round((stamp - start) / span * plot_width)
                        if span > 0
                        else (self.width() - 1) / 2
                    )
                    painter.drawEllipse(
                        px - radius, dot_y - radius, radius * 2, radius * 2
                    )

            # Vertical markers.
            if self.now is not None and start <= self.now <= end and span > 0:
                now_x = self.axis_left_margin + round(
                    (self.now - start)
                    / span
                    * max(
                        0,
                        self.width()
                        - self.axis_left_margin
                        - self.axis_right_margin
                        - 1,
                    )
                )
                painter.setPen(QPen(QColor("#e6c229"), 1))
                painter.drawLine(now_x, 0, now_x, self.height())

            if self.position is not None and span > 0:
                px = self.axis_left_margin + round(
                    (self.position - start)
                    / span
                    * max(
                        0,
                        self.width()
                        - self.axis_left_margin
                        - self.axis_right_margin
                        - 1,
                    )
                )
                painter.setPen(QPen(QColor("#3584e4"), 2))
                painter.drawLine(px, 0, px, self.height())

            if self.marker is not None and span > 0:
                mx = self.axis_left_margin + round(
                    (self.marker - start)
                    / span
                    * max(
                        0,
                        self.width()
                        - self.axis_left_margin
                        - self.axis_right_margin
                        - 1,
                    )
                )
                painter.setPen(QPen(QColor("#8f3b3b"), 2))
                painter.drawLine(mx, 0, mx, self.height())
        except Exception as exc:  # Never let a paint error crash the app
            logger.warning("DayStrip paint error: %s", exc)

    def _scrub_to(self, x):
        """Seek proportionally, independently of the painted day cells."""
        try:
            if not self.bounds:
                return False
            start, end = self.bounds
            width = self.width()
            if (
                width <= 0
                or start is None
                or end is None
                or not math.isfinite(start)
                or not math.isfinite(end)
                or end < start
            ):
                return False
            plot_width = width - self.axis_left_margin - self.axis_right_margin - 1
            if plot_width <= 0:
                return False
            fraction = max(0.0, min(1.0, (x - self.axis_left_margin) / plot_width))
            stamp = start + fraction * (end - start)
            self.time_selected.emit(stamp)
            return True
        except Exception as exc:  # Never let scrubbing crash the app
            logger.warning("DayStrip scrub error: %s", exc)
            return False

    def _stop_dragging(self):
        self._dragging = False
        if QWidget.mouseGrabber() is self:
            self.releaseMouse()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self.grabMouse()
            if not self._scrub_to(event.position().x()):
                self._stop_dragging()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging:
            if not self._scrub_to(event.position().x()):
                self._stop_dragging()
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._stop_dragging()
            self._scrub_to(event.position().x())
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def hideEvent(self, event):
        self._stop_dragging()
        self.stop_clock()
        super().hideEvent(event)


class ForecastTimeline(QWidget):
    step_changed = Signal()
    expansion_changed = Signal()
    region_changed = Signal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setObjectName("forecast-timeline-overlay")
        self.controller = controller
        self.clock = controller.time_control
        self.marine = controller.core.marine_manager
        self.bounds = None
        # User-owned time region; selecting a model never rewrites it.
        self.region = None
        self._model_intervals = ()
        self._signature = None
        self._syncing = False
        self._pending = None
        self._expanded = False
        self.closed = False
        self.seconds = 900
        self.setMinimumWidth(520)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        # The overlay paints its own background from the stylesheet rule.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(2)

        # ── Top transport row ──────────────────────────────────────
        row = QHBoxLayout()
        row.setSpacing(2)

        self.start_month_label = QLabel()
        self.start_month_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.start_month_label.setMinimumWidth(0)
        self.start_month_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
        )
        row.addWidget(self.start_month_label)

        self.time_label = QLabel()
        self.time_label.setMinimumWidth(0)
        self.time_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.time_label.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        )
        row.addWidget(self.time_label, 1)

        self.backward_button = self._button(
            row,
            "media-seek-backward-symbolic",
            "Step forecast backward",
            lambda: self.step(-self.seconds),
        )
        self.pause_button = self._button(
            row, "media-playback-pause-symbolic", "Pause forecast", self.pause
        )
        self.play_button = self._button(
            row, "media-playback-start-symbolic", "Play forecast", self.play
        )
        self.play_button.setCheckable(True)
        self.forward_button = self._button(
            row,
            "media-seek-forward-symbolic",
            "Step forecast forward",
            lambda: self.step(self.seconds),
        )

        # Kept (off-panel) for workspace step persistence and tests.
        self.step_combo = QComboBox()
        self.step_combo.addItems([label for label in TIME_UNITS if label != "1d"])
        self.step_combo.setCurrentText("15m")
        self.step_combo.setAccessibleName("Forecast step interval")
        self.step_combo.setToolTip(
            "Forecast time advanced by each manual step; playback advances 2 hours per second"
        )
        self.step_combo.currentTextChanged.connect(self._step_changed)
        self.select_button = self._button(
            row,
            "preferences-system-time-symbolic",
            "Set forecast date and time",
            self.select_time,
        )

        self.now_button = self._button(
            row, "go-bottom-symbolic", "Select now", self.now
        )

        self.end_month_label = QLabel()
        self.end_month_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.end_month_label.setMinimumWidth(0)
        self.end_month_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
        )
        row.addWidget(self.end_month_label)

        self.overview_button = QToolButton()
        self.overview_button.setText("⌃")
        self.overview_button.setAccessibleName("Expand forecast source overview")
        self.overview_button.setToolTip("Show valid times by forecast source")
        self.overview_button.setCheckable(True)
        self.overview_button.setAutoRaise(True)
        self.overview_button.toggled.connect(self._set_expanded)
        row.addWidget(self.overview_button)

        layout.addLayout(row)

        # The track drawer follows the scrubber; the viewport pins this widget
        # to its bottom edge, so expanding the drawer grows the full overlay up.
        self.source_overview = ForecastSourceDrawer(self)
        self.source_overview.geometry_changed.connect(self.expansion_changed)
        self.source_overview.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )

        # Caption / status line.
        self.caption = QLabel()
        self.caption.setProperty("secondary", True)
        caption_font = self.caption.font()
        caption_font.setPixelSize(10)
        self.caption.setFont(caption_font)
        self.caption.setFixedHeight(12)
        self.caption.setMinimumWidth(0)
        self.caption.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.caption)

        # ── Day timeline strip ─────────────────────────────────────
        self.day_strip = DayStripWidget()
        self.day_strip.setAccessibleName("Forecast day timeline")
        self.day_strip.setToolTip("Click and drag to scrub forecast time")
        self.day_strip.time_selected.connect(self._seek_to_stamp)
        self.day_strip.now_changed.connect(self.source_overview.set_now_cursor)
        self.source_overview.plot.axis_width_changed.connect(
            self._sync_time_axis_geometry
        )
        layout.addWidget(self.day_strip)
        layout.addWidget(self.source_overview)

        # Hidden slider keeps the value model for steps/playback.
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1)
        self.slider.setFixedHeight(0)
        self.slider.valueChanged.connect(self._slide)
        self.slider.sliderPressed.connect(self.pause)
        self.slider.sliderReleased.connect(self.commit)

        self.play_timer = QTimer(self)
        # Twenty updates per second, about two forecast hours per real second.
        self.play_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.play_timer.setInterval(PLAY_INTERVAL_MS)
        self.play_timer.timeout.connect(self.tick)
        self.commit_timer = QTimer(self)
        self.commit_timer.setSingleShot(True)
        self.commit_timer.setInterval(16)
        self.commit_timer.timeout.connect(self.commit)
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(500)
        self.poll_timer.timeout.connect(self.refresh)
        self.clock.connect("time_change", self._time_changed)
        self.marine.connect("changed", self._marine_changed)
        self.marine.fields.connect("changed", self._marine_changed)
        self.controller.display_forecast.changed.connect(self.refresh)
        self.pause()
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        if not self.closed:
            self.poll_timer.start()
            self.refresh()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "day_strip") and hasattr(self, "source_overview"):
            QTimer.singleShot(0, self._sync_time_axis_geometry)

    def _sync_time_axis_geometry(self, *_):
        if not hasattr(self, "day_strip") or not hasattr(self, "source_overview"):
            return
        # The drawer may be hidden and keep stale child geometry. Reserve the
        # scrollbar width from its content height and the drawer's max height,
        # both of which remain valid while collapsed.
        scroll_width = 0
        if self.source_overview.plot.height() > 240:
            scroll_width = (
                self.source_overview.scroll.verticalScrollBar().sizeHint().width()
            )
        self.day_strip.axis_right_margin = max(0, scroll_width)
        self.day_strip.update()

    @staticmethod
    def _button(row, icon, title, callback):
        button = QToolButton()
        button.setIcon(symbol_icon(icon))
        button.setAccessibleName(title)
        button.setToolTip(title)
        button.setAutoRaise(True)
        button.setFixedSize(24, 24)
        button.setIconSize(QSize(14, 14))
        button.clicked.connect(callback)
        row.addWidget(button)
        return button

    @property
    def playing(self):
        return self.play_timer.isActive()

    def _step_changed(self, label):
        if label in TIME_UNITS:
            self.seconds = TIME_UNITS[label]
            self.step_changed.emit()

    def _set_expanded(self, expanded):
        self._expanded = bool(expanded)
        self.source_overview.set_expanded(self._expanded)
        _set_text(self.overview_button, "⌄" if self._expanded else "⌃")
        self.overview_button.setAccessibleName(
            "Collapse forecast source overview"
            if self._expanded
            else "Expand forecast source overview"
        )
        self.expansion_changed.emit()

    def _marine_changed(self, *_):
        self.controller.scheduler.submit(self.refresh)

    def refresh(self, *_):
        if self.closed:
            return
        # Window construction refreshes the timeline once per added source, and
        # every label assignment runs a full layout pass. Disable updates for
        # the batch so layout and repaint run once, on re-enable.
        was_updating = self.updatesEnabled()
        if was_updating:
            self.setUpdatesEnabled(False)
        try:
            # Use only the selected display forecast's time range (weather model).
            # Marine (tides/currents) intervals are intentionally excluded so the
            # slider reflects the loaded weather model's range only.
            sources = self.controller.display_forecast.sources()
            intervals = tuple(
                sorted((source.first_valid, source.last_valid) for source in sources)
            )
            valid_times = tuple(
                sorted({stamp for source in sources for stamp in source.valid_times})
            )
            forecast_range = forecast_bounds(intervals)
            # The user's region wins; the model range is only a fallback, so a
            # model selection can never move the time window on its own.
            bounds = self.region or forecast_range
            signature = (intervals, valid_times, self.region)
            if signature != self._signature:
                self._signature = signature
                self._model_intervals = intervals
                self.bounds = bounds
                self.cancel_commit()
                self._syncing = True
                if self.bounds:
                    start, end = self.bounds
                    # Relative seconds avoid Qt's signed-32-bit epoch limit after 2038.
                    self.slider.setRange(
                        0, max(1, min(2**31 - 1, math.ceil(end - start)))
                    )
                    self.slider.setSingleStep(60)
                    self.slider.setPageStep(3600)
                    self.slider.setEnabled(end > start)
                    # Month/year labels at the edges.
                    start_local = datetime.fromtimestamp(start).astimezone()
                    end_local = datetime.fromtimestamp(end).astimezone()
                    _set_text(self.start_month_label, start_local.strftime("%b %Y"))
                    _set_text(self.end_month_label, end_local.strftime("%b %Y"))
                    if self.start_month_label.toolTip() != start_local.isoformat():
                        self.start_month_label.setToolTip(start_local.isoformat())
                    if self.end_month_label.toolTip() != end_local.isoformat():
                        self.end_month_label.setToolTip(end_local.isoformat())
                    self.day_strip.set_bounds(start, end)
                    self.day_strip.set_valid_times(valid_times)
                else:
                    self.slider.setEnabled(False)
                    self.start_month_label.clear()
                    self.end_month_label.clear()
                    self.day_strip.set_bounds(None, None)
                    self.day_strip.set_valid_times(())
                self._syncing = False
                self._time_changed(self.clock.time)

            # Overview is optional: a marine/catalog failure must never abort
            # the selected-model timeline refresh above.
            try:
                self.source_overview.set_rows(
                    source_rows(self.controller.display_forecast, self.marine),
                    self.bounds,
                )
                self.day_strip.axis_left_margin = self.source_overview.plot.label_width
                self._sync_time_axis_geometry()
                self.day_strip.update()
            except Exception as exc:
                logger.warning("Forecast source overview refresh error: %s", exc)
        except Exception as exc:  # Never let a refresh crash the app
            logger.warning("ForecastTimeline refresh error: %s", exc)
            self._syncing = False
        finally:
            if was_updating:
                self.setUpdatesEnabled(True)

    def set_region(self, start, end):
        """Set the user-owned time region the scrubber spans.

        The region survives model selections; only an explicit region change
        (this call, "Select now", or a race window) rewrites it.
        """
        region = (float(start), float(end))
        if region == self.region:
            return
        self.region = region
        self._signature = None
        self.region_changed.emit()
        self.refresh()

    def seek(self, stamp):
        """Move the cursor to an epoch stamp and stop playback."""
        self._seek_to_stamp(stamp)

    def clear_region(self):
        """Drop the user region and fall back to the loaded model range."""
        if self.region is None:
            return
        self.region = None
        self._signature = None
        self.region_changed.emit()
        self.refresh()

    def _caption_for(self, stamp):
        """Explain why the selected time is not usable, or return ""."""
        if not self.bounds:
            return "Choose a display forecast in Weather, or download tides / currents"
        start, end = self.bounds
        if not (start <= stamp <= end):
            return "Selected time is outside the selected time range"
        if not any(
            first - 1 <= stamp <= last + 1
            for first, last in getattr(self, "_model_intervals", ())
        ):
            return "No model data for the selected time"
        return ""

    def _time_changed(self, instant):
        if self.closed:
            return
        try:
            self.cancel_commit()
            _set_text(self.time_label, "<b>%s</b>" % (instant.strftime("%H:%M"),))
            self.time_label.setToolTip(instant.astimezone().isoformat())
            self._syncing = True
            if self.bounds:
                start, end = self.bounds
                span = end - start
                value = (
                    0
                    if not span
                    else round(
                        (instant.timestamp() - start) * self.slider.maximum() / span
                    )
                )
                self.slider.setValue(max(0, min(self.slider.maximum(), value)))
                self.day_strip.set_position(instant.timestamp())
                self.day_strip.set_marker(start)
                self.source_overview.set_cursors(
                    self.day_strip.now, instant.timestamp()
                )
            else:
                self.day_strip.set_position(None)
                self.day_strip.set_marker(None)
                self.source_overview.set_cursors(self.day_strip.now, None)
            caption = self._caption_for(instant.timestamp())
            _set_text(self.caption, caption)
            if self.caption.toolTip() != caption:
                self.caption.setToolTip(caption)
            self._syncing = False
        except Exception as exc:  # Never let a time change crash the app
            logger.warning("ForecastTimeline time change error: %s", exc)
            self._syncing = False

    def _seek_to_stamp(self, stamp):
        if self.closed:
            return
        try:
            self.pause()
            self.cancel_commit()
            self.clock.set_time(datetime.fromtimestamp(stamp))
        except Exception as exc:  # Never let a seek crash the app
            logger.warning("ForecastTimeline seek error: %s", exc)

    def _slide(self, value):
        if self._syncing or not self.bounds or not self.slider.isEnabled():
            return
        try:
            self.pause()
            start, end = self.bounds
            self._pending = start + value * (end - start) / self.slider.maximum()
            if not self.commit_timer.isActive():
                self.commit_timer.start()
        except Exception as exc:  # Never let a slide crash the app
            logger.warning("ForecastTimeline slide error: %s", exc)

    def cancel_commit(self):
        self.commit_timer.stop()
        self._pending = None

    def commit(self):
        stamp = self._pending
        self.cancel_commit()
        if stamp is not None and not self.closed:
            try:
                self.pause()
                self.clock.set_time(datetime.fromtimestamp(stamp))
            except Exception as exc:  # Never let a commit crash the app
                logger.warning("ForecastTimeline commit error: %s", exc)

    def step(self, seconds):
        if self.closed:
            return
        try:
            self.cancel_commit()
            step_forecast_time(self.clock, seconds, self.bounds)
        except Exception as exc:  # Never let a step crash the app
            logger.warning("ForecastTimeline step error: %s", exc)

    def play(self):
        if self.closed:
            return
        try:
            self.cancel_commit()
            if not self.playing:
                self.play_timer.start()
            self.play_button.setChecked(True)
            self.pause_button.setEnabled(True)
        except Exception as exc:  # Never let a play crash the app
            logger.warning("ForecastTimeline play error: %s", exc)

    def pause(self):
        try:
            self.play_timer.stop()
            self.play_button.setChecked(False)
            self.pause_button.setEnabled(False)
        except Exception as exc:  # Never let a pause crash the app
            logger.warning("ForecastTimeline pause error: %s", exc)

    def _playback_limit(self):
        """Last playable instant: the last model valid time, else the region end."""
        if not self.bounds:
            return None
        start, end = self.bounds
        valid = tuple(
            stamp for stamp in self.day_strip.valid_times if start <= stamp <= end
        )
        return max(valid) if valid else end

    def tick(self):
        if self.closed or not self.playing:
            return False
        try:
            limit = self._playback_limit()
            if (
                limit is not None
                and self.clock.time.timestamp() + PLAY_STEP_SECONDS >= limit
            ):
                self.clock.set_time(datetime.fromtimestamp(limit))
                self.pause()
                return self.playing
            self.step(PLAY_STEP_SECONDS)
            if limit is not None and self.clock.time.timestamp() >= limit:
                self.pause()
        except Exception as exc:  # Never let a tick crash the app
            logger.warning("ForecastTimeline tick error: %s", exc)
        return self.playing

    def now(self):
        if self.closed:
            return
        try:
            self.pause()
            self.cancel_commit()
        except Exception as exc:  # Never let a now crash the app
            logger.warning("ForecastTimeline now error: %s", exc)
            return
        try:
            wall = datetime.now().timestamp()
            # "Now" is an explicit user region request: now-24h .. now+48h.
            self.set_region(wall - 24 * 3600, wall + 48 * 3600)
            # The wall clock is frequently outside the downloaded model window
            # (a stale download, or a first step after the current hour). Seek
            # to the nearest loaded valid time inside the region instead;
            # without model coverage there the wall clock itself stands, so an
            # empty model still shows an explicit no-data state.
            self.clock.set_time(datetime.fromtimestamp(self._now_stamp()))
        except Exception as exc:  # Never let a now crash the app
            logger.warning("ForecastTimeline now error: %s", exc)

    def _now_stamp(self):
        """Nearest loaded valid time to the wall clock inside the region.

        Without a region or model coverage the wall clock stands, clamped into
        the region.
        """
        now = datetime.now().timestamp()
        if not self.bounds:
            return now
        start, end = self.bounds
        valid = tuple(
            stamp for stamp in self.day_strip.valid_times if start <= stamp <= end
        )
        if valid:
            return min(valid, key=lambda stamp: abs(stamp - now))
        return max(start, min(end, now))

    def select_time(self):
        try:
            self.pause()
            self.cancel_commit()
            dialog = QDialog(self)
            dialog.setWindowTitle("Time Picker")
            layout = QVBoxLayout(dialog)
            editor = QDateTimeEdit(QDateTime(self.clock.time), dialog)
            editor.setCalendarPopup(True)
            editor.setDisplayFormat("yyyy-MM-dd HH:mm")
            editor.setAccessibleName("Forecast date and time (local)")
            layout.addWidget(editor)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
            buttons.addButton("Set", QDialogButtonBox.ButtonRole.AcceptRole)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if dialog.exec() == QDialog.DialogCode.Accepted and not self.closed:
                self.clock.set_time(editor.dateTime().toPython())
            dialog.deleteLater()
        except Exception as exc:  # Never let a time picker crash the app
            logger.warning("ForecastTimeline select_time error: %s", exc)

    def hideEvent(self, event):  # noqa: N802 - Qt virtual method name
        # QWidgetAction can hide its widget again while the toolbar is being
        # destroyed, after Qt has already deleted the transport's children.
        try:
            if not self.closed:
                self.pause()
                self.cancel_commit()
                self.poll_timer.stop()
        except Exception as exc:  # Never let a hide crash the app
            logger.warning("ForecastTimeline hide error: %s", exc)
        super().hideEvent(event)

    def dispose(self):
        if self.closed:
            return
        try:
            self.closed = True
            self.source_overview.stop()
            self.pause()
            self.cancel_commit()
            self.poll_timer.stop()
            self.day_strip.stop_clock()
            self.clock.disconnect("time_change", self._time_changed)
            self.marine.disconnect("changed", self._marine_changed)
            self.marine.fields.disconnect("changed", self._marine_changed)
        except Exception as exc:  # Never let a dispose crash the app
            logger.warning("ForecastTimeline dispose error: %s", exc)
