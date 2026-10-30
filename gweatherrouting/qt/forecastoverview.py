"""Read-only source availability plot and a height-animated overlay drawer."""

import math
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QEasingCurve, QPointF, QPropertyAnimation, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QLayout, QScrollArea, QVBoxLayout, QWidget


def source_rows(selection, marine):
    """Use published metadata/sample clocks, never requested coverage endpoints."""
    rows = []
    selected = tuple(selection.sources())
    choices = getattr(selection, "choices", ())
    choices = choices if isinstance(choices, (tuple, list)) else ()
    for source in choices or selected:
        key = getattr(source, "key", id(source))
        label = getattr(source, "label", None) or getattr(source, "name", "Forecast")
        model = getattr(source, "model_name", "")
        if model:
            label = f"{model} · {label}"
        active = any(getattr(item, "key", id(item)) == key for item in selected)
        rows.append((str(key), str(label), tuple(source.valid_times), active,
                     "Weather models", str(model or "Other")))

    datasets = marine.ordered_datasets()
    # Lightweight controllers used by embedders may have no marine catalog.
    if not isinstance(datasets, (tuple, list)):
        datasets = ()
    for dataset in datasets:
        identifier = dataset.id
        enabled = marine.is_enabled(identifier)
        for title, codes in (
            ("Tides", ("water_level",)),
            ("Station currents", ("current_speed", "current_direction")),
            ("Passage events", ("current_events",)),
        ):
            series = [dataset.get_series(code) for code in codes]
            if any(item is None for item in series):
                continue
            clocks = [
                {
                    sample.time.timestamp()
                    for sample in item.samples
                    if sample.value is not None and sample.qc_flag in ("1", "2")
                }
                for item in series
            ]
            stamps = set.intersection(*clocks)
            if len(series) == 2 and tuple(s.time for s in series[0].samples) != tuple(
                s.time for s in series[1].samples
            ):
                # The station-current sampler requires paired component clocks.
                stamps.clear()
            label = f"{title} · {dataset.provider} · {dataset.station.name}"
            rows.append((f"{identifier}:{codes[0]}", label, tuple(stamps), enabled,
                         title, ""))

    fields = marine.fields.fields
    if isinstance(fields, Mapping):
        for key, field in fields.items():
            metadata = field.manifest()
            label = f"Current field · {metadata['provider']} · {metadata['model']}"
            rows.append(
                (str(key), label, tuple(field.times), key not in marine.fields.excluded,
                 "Currents", "")
            )
    return tuple(
        (key, label, tuple(sorted({float(t) for t in times if math.isfinite(t)})),
         active, *metadata)
        for key, label, times, active, *metadata in rows
    )


class SourceTimePlot(QWidget):
    """Rows share a UTC axis; only real timestamp dots are painted."""

    axis_width_changed = Signal(int)

    ROW_HEIGHT = 32
    AXIS_HEIGHT = 30

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = ()
        self.bounds = None
        self.now_cursor = None
        self.time_cursor = None
        self.expanded_groups = {"Weather models", "Tides", "Currents", "Station currents", "Passage events"}
        self._known_model_types = set()
        self.tree_rows = ()
        self.label_width = 240
        self.setAccessibleName("Forecast and marine source valid times (UTC)")

    def set_rows(self, rows, bounds=None):
        self.rows = rows
        model_types = {row[5] for row in rows if len(row) > 5 and row[4] == "Weather models"}
        for model_type in model_types - self._known_model_types:
            self.expanded_groups.add(f"Weather models/{model_type}")
        self._known_model_types.update(model_types)
        self.bounds = bounds
        if self.bounds is None:
            stamps = [t for row in rows for t in row[2]]
            self.bounds = (min(stamps), max(stamps)) if stamps else None
        self.tree_rows = self._visible_tree_rows()
        self.setFixedHeight(max(1, len(self.tree_rows)) * self.ROW_HEIGHT + self.AXIS_HEIGHT)
        self.update()

    def set_cursors(self, now, selected_time):
        self.now_cursor = now
        self.time_cursor = selected_time
        self.update()

    def set_now_cursor(self, now):
        self.now_cursor = now
        self.update()

    def _visible_tree_rows(self):
        groups = {}
        for row in self.rows:
            category = row[4] if len(row) > 4 else "Weather models"
            subgroup = row[5] if len(row) > 5 else "Other"
            groups.setdefault(category, {}).setdefault(subgroup, []).append(row)
        visible = []
        for category, subgroups in groups.items():
            category_times = tuple(
                sorted(
                    {
                        stamp
                        for leaves in subgroups.values()
                        for row in leaves
                        for stamp in row[2]
                    }
                )
            )
            visible.append(("group", category, category_times))
            if category not in self.expanded_groups:
                continue
            if category == "Weather models":
                for subgroup, leaves in subgroups.items():
                    group_key = f"Weather models/{subgroup}"
                    subgroup_times = tuple(
                        sorted({stamp for row in leaves for stamp in row[2]})
                    )
                    visible.append(("subgroup", subgroup, subgroup_times))
                    if group_key in self.expanded_groups:
                        visible.extend(("leaf", row[1], row) for row in leaves)
            else:
                for leaves in subgroups.values():
                    visible.extend(("leaf", row[1], row) for row in leaves)
        return tuple(visible)

    def _plot_left(self):
        return self.label_width

    def dot_points(self, times, row):
        if not self.bounds:
            return ()
        return tuple(
            QPointF(
                self._time_to_x(stamp),
                row * self.ROW_HEIGHT + self.ROW_HEIGHT / 2,
            )
            for stamp in times
        )

    def _time_to_x(self, stamp):
        if not self.bounds:
            return self._plot_left()
        start, end = self.bounds
        width = max(0, self.width() - self._plot_left() - 1)
        fraction = (stamp - start) / (end - start) if end > start else 0.5
        return self._plot_left() + round(fraction * width)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.axis_width_changed.emit(self.width())

    def _day_tick_positions(self):
        """Match DayStripWidget's equal-width UTC day cells exactly."""
        if not self.bounds:
            return ()
        start, end = self.bounds
        day = datetime.fromtimestamp(start, timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        days = []
        while day.timestamp() <= end:
            days.append(day.timestamp())
            day += timedelta(days=1)
        if len(days) < 2:
            return ()
        left = self._plot_left()
        width = max(0, self.width() - left)
        cell_width = width / len(days)
        return tuple(left + int(index * cell_width) for index in range(1, len(days)))

    def _paint_time_grid(self, painter):
        if not self.bounds:
            return
        start, end = self.bounds
        bottom = len(self.tree_rows) * self.ROW_HEIGHT
        if end <= start:
            return

        day_ticks = self._day_tick_positions()
        day_tick_set = set(day_ticks)

        first_hour = math.ceil(start / 3600) * 3600
        hour = first_hour
        while hour < end:
            x = self._time_to_x(hour)
            if x not in day_tick_set:
                painter.setPen(QPen(QColor("#414141"), 1))
                painter.drawLine(x, 0, x, bottom)
            hour += 3600
        painter.setPen(QPen(QColor("#666666"), 2))
        for x in day_ticks:
            painter.drawLine(x, 0, x, bottom)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = painter.font()
        font.setPointSizeF(max(7.5, font.pointSizeF() - 2.0))
        painter.setFont(font)
        palette = self.palette()
        border = (
            palette.mid().color().lighter(175)
            if palette.window().color().lightness() < 128
            else palette.mid().color().darker(125)
        )
        label_width = self._plot_left() - 8
        if not self.tree_rows:
            painter.setPen(palette.text().color())
            painter.drawText(4, 22, "No forecast or marine sources available")
            return
        self._paint_time_grid(painter)
        for index, (kind, label, row) in enumerate(self.tree_rows):
            top = index * self.ROW_HEIGHT
            painter.setPen(QPen(border, 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(0, top, max(0, self.width() - 1), self.ROW_HEIGHT - 1)
            painter.setPen(palette.text().color())
            active = row[3] if kind == "leaf" else True
            group_key = f"Weather models/{label}" if kind == "subgroup" else label
            text = ("▾ " if group_key in self.expanded_groups else "▸ ") + label if kind != "leaf" else label + ("" if active else " (not selected)")
            indent = 4 if kind == "group" else 20 if kind == "subgroup" else 36
            painter.drawText(
                indent,
                top + 21,
                painter.fontMetrics().elidedText(
                    text,
                    Qt.TextElideMode.ElideRight,
                    max(0, label_width - indent - 4),
                ),
            )
            point_color = QColor("#777777")
            painter.setPen(QPen(point_color, 1))
            painter.setBrush(point_color)
            times = row[2] if kind == "leaf" else row
            for point in self.dot_points(times, index):
                painter.drawEllipse(point, 1.5, 1.5)
            if kind == "leaf" and not times:
                painter.setPen(palette.text().color())
                painter.drawText(label_width + 12, top + 21, "No valid times")
        if self.bounds:
            painter.setPen(palette.text().color())
            labels = [
                datetime.fromtimestamp(t, timezone.utc).strftime("%b %d %H:%M UTC")
                for t in self.bounds
            ]
            y = len(self.tree_rows) * self.ROW_HEIGHT + 21
            painter.drawText(label_width + 8, y, labels[0])
            painter.drawText(
                max(
                    label_width + 8,
                    self.width()
                    - painter.fontMetrics().horizontalAdvance(labels[1])
                    - 4,
                ),
                y,
                labels[1],
            )
            painter.setPen(QPen(QColor("#999999"), 1))
            start_x = self._plot_left()
            painter.drawLine(start_x, 0, start_x, len(self.tree_rows) * self.ROW_HEIGHT)
            track_bottom = len(self.tree_rows) * self.ROW_HEIGHT
            for stamp, color, width in (
                (self.now_cursor, QColor("#e6c229"), 2),
                (self.time_cursor, QColor("#0066cc"), 2),
            ):
                if stamp is None or not self.bounds[0] <= stamp <= self.bounds[1]:
                    continue
                painter.setPen(QPen(color, width))
                cursor_x = self._time_to_x(stamp)
                painter.drawLine(cursor_x, 0, cursor_x, track_bottom)

    def mousePressEvent(self, event):
        index = int(event.position().y() // self.ROW_HEIGHT)
        if not 0 <= index < len(self.tree_rows) or event.position().x() >= self._plot_left():
            return super().mousePressEvent(event)
        kind, label, _ = self.tree_rows[index]
        key = f"Weather models/{label}" if kind == "subgroup" else label
        if kind != "leaf":
            if key in self.expanded_groups:
                self.expanded_groups.remove(key)
            else:
                self.expanded_groups.add(key)
            self.tree_rows = self._visible_tree_rows()
            self.setFixedHeight(max(1, len(self.tree_rows)) * self.ROW_HEIGHT + self.AXIS_HEIGHT)
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)


class ForecastSourceDrawer(QWidget):
    geometry_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("forecast-source-overview")
        self.expanded = False
        self._rows = None
        self._bounds = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        # Children keep their full height and are clipped during the slide.
        # Do not let the scroll area's minimum size clamp the animation.
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.scroll = QScrollArea(self)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.plot = SourceTimePlot()
        self.scroll.setWidget(self.plot)
        layout.addWidget(self.scroll)
        self.setMaximumHeight(0)
        self.hide()
        self.animation = QPropertyAnimation(self, b"maximumHeight", self)
        self.animation.setDuration(240)
        self.animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.animation.valueChanged.connect(self._geometry_changed)
        self.animation.finished.connect(self._finished)

    def sizeHint(self):
        return QSize(520, min(self.maximumHeight(), self.target_height()))

    def minimumSizeHint(self):
        return QSize(0, 0)

    def target_height(self):
        return min(240, self.plot.height() + 4)

    def _geometry_changed(self, *_):
        self.updateGeometry()
        if self.parentWidget() is not None:
            self.parentWidget().layout().invalidate()
        self.geometry_changed.emit()

    def set_expanded(self, expanded):
        self.expanded = bool(expanded)
        self.animation.stop()
        self.show()
        self.animation.setStartValue(self.maximumHeight())
        self.animation.setEndValue(self.target_height() if self.expanded else 0)
        self.animation.start()
        self._geometry_changed()

    def _finished(self):
        if not self.expanded:
            self.hide()
        self._geometry_changed()

    def set_rows(self, rows, bounds=None):
        if rows == self._rows and bounds == self._bounds:
            return
        self._rows = rows
        self._bounds = bounds
        self.plot.set_rows(rows, bounds)
        self.plot.setToolTip("")
        if self.expanded:
            self.set_expanded(True)

    def set_cursors(self, now, selected_time):
        self.plot.set_cursors(now, selected_time)

    def set_now_cursor(self, now):
        self.plot.set_now_cursor(now)

    def stop(self):
        self.animation.stop()
