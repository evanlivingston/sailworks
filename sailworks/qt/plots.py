"""Native Qt Matplotlib canvas with complete log data and a blitted cursor."""

import logging
from bisect import bisect_left
from datetime import timedelta

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor, QKeySequence, QShortcut
from PySide6.QtWidgets import QMenu
from matplotlib.patches import Rectangle
from matplotlib.ticker import FuncFormatter
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from .theme import SURFACE, TEXT, MUTED

logger = logging.getLogger(__name__)


class LogPlots(FigureCanvasQTAgg):
    def __init__(
        self,
        parent=None,
        *,
        preview=None,
        seek=None,
        delete_range=None,
        undo=None,
        viewport_changed=None,
    ):
        figure = Figure(figsize=(8, 3), layout="tight", facecolor=SURFACE)
        super().__init__(figure)
        self.setParent(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.series = {
            "speed": True,
            "aws": False,
            "tws": True,
            "depth": False,
            "hdg": True,
            "twa": False,
        }
        self.logged_fields = ()
        self.selected_logged_fields = set()
        self.points = ()
        self.axes = []
        self.cursors = []
        self.hover_cursors = []
        self.hover_labels = []
        self._hover_values = []
        self._hover_index = None
        self._hover_position = None
        self.background = None
        self.position = 0.0
        self.playback = False
        self.offsets = ()
        self.preview = preview
        self.seek = seek
        self.delete_range = delete_range
        self.undo = undo
        self.viewport_changed = viewport_changed
        self.time_bounds = (0.0, 0.0)
        self.view_range = None
        self.selection = None
        self._selecting = False
        self._selection_start = None
        self._selection_artists = []
        self.mpl_connect("draw_event", self._drawn)
        self._scrubbing = False
        self._scrub_last_position = None
        self._selecting = False
        self._selection_start = None
        self.mpl_connect("button_press_event", self._scrub_press)
        self.mpl_connect("motion_notify_event", self._scrub_motion)
        self.mpl_connect("motion_notify_event", self._hover_motion)
        self.mpl_connect("button_release_event", self._scrub_release)
        self.mpl_connect("scroll_event", self._zoom)
        self.shortcut_undo = QShortcut(QKeySequence.StandardKey.Undo, self)
        self.shortcut_undo.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.shortcut_undo.activated.connect(self.undo_log_edit)
        self.set_points((), (), False)

    def _scrub_position(self, event):
        if (
            not self.playback
            or not self.points
            or not self.offsets
            or event.inaxes not in self.axes
            or event.xdata is None
        ):
            return None
        return min(float(self.offsets[-1]), max(float(self.offsets[0]), event.xdata))

    def _scrub_press(self, event):
        if event.button == 3:
            if event.inaxes not in self.axes:
                return
            logger.info("Log editor right-click; selection=%r", self.selection)
            self._show_range_menu()
            return
        if event.button == 1 and self._is_shift_pressed(event):
            position = self._range_position(event)
            logger.info(
                "Log editor Shift+left press; playback=%s inaxes=%s x=%r range_position=%r",
                self.playback,
                event.inaxes in self.axes,
                event.xdata,
                position,
            )
            if position is not None:
                self._selecting = True
                self._set_hover(None)
                self._selection_start = position
                self._set_selection((position, position))
            return
        if event.button != 1:
            return
        position = self._scrub_position(event)
        logger.info(
            "Log editor left press; playback=%s inaxes=%s x=%r scrub_position=%r",
            self.playback,
            event.inaxes in self.axes,
            event.xdata,
            position,
        )
        if position is None:
            return
        self._scrubbing = True
        self._set_hover(None)
        self._scrub_last_position = position
        if self.preview is not None:
            self.preview(position)

    def _scrub_motion(self, event):
        if self._selecting:
            position = self._range_position(event)
            if position is not None:
                self._set_selection((self._selection_start, position))
            return
        if not self._scrubbing:
            return
        position = self._scrub_position(event)
        if position is not None:
            self._scrub_last_position = position
            if self.preview is not None:
                self.preview(position)

    def _scrub_release(self, event):
        if self._selecting:
            position = self._range_position(event)
            if position is not None:
                self._set_selection((self._selection_start, position))
            self._selecting = False
            self._selection_start = None
            if self.selection and self.selection[1] - self.selection[0] <= 0:
                self._clear_selection()
            logger.info("Log editor drag selection complete: %r", self.selection)
            return
        if not self._scrubbing:
            return
        position = self._scrub_position(event)
        if position is None:
            position = self._scrub_last_position
        self._scrubbing = False
        self._scrub_last_position = None
        if position is not None and self.seek is not None:
            self.seek(position)

    def set_points(self, points, offsets, playback):
        old_view = self.view_range
        old_points = self.points
        self._scrubbing = False
        self._scrub_last_position = None
        self.points = tuple(points)
        self.playback = playback
        self.offsets = tuple(offsets)
        if self.points is not old_points:
            self._clear_selection(draw=False)
        self.background = None
        self.cursors = []
        self.hover_cursors = []
        self.hover_labels = []
        self._hover_values = []
        self._hover_index = None
        self._hover_position = None
        groups = self._plot_groups()
        selected_fields = [
            field
            for field in self.logged_fields
            if field.key in self.selected_logged_fields
        ]
        self.figure.clear()
        self._selection_artists = []
        self.axes = list(
            self.figure.subplots(
                max(1, len(groups) + len(selected_fields)),
                sharex=True,
                squeeze=False,
            )[:, 0]
        )

        def add_axis_tools(axis, values):
            self._hover_values.append(values)
            self.cursors.append(
                axis.axvline(
                    self.position,
                    color="#ffbf40",
                    linewidth=1.4,
                    animated=True,
                    visible=playback and bool(points),
                )
            )
            self.hover_cursors.append(
                axis.axvline(
                    0,
                    color="#ffffff",
                    linewidth=0.8,
                    alpha=0.75,
                    animated=True,
                    visible=False,
                    zorder=8,
                )
            )
            self.hover_labels.append(
                axis.text(
                    0.01,
                    0.96,
                    "",
                    transform=axis.transAxes,
                    va="top",
                    ha="left",
                    fontsize=7,
                    color=TEXT,
                    bbox={
                        "facecolor": SURFACE,
                        "edgecolor": MUTED,
                        "alpha": 0.9,
                        "pad": 2,
                    },
                    animated=True,
                    visible=False,
                    zorder=9,
                )
            )

        for axis, (label, series) in zip(self.axes, groups):
            axis.clear()
            axis.set_facecolor(SURFACE)
            for spine in axis.spines.values():
                spine.set_color("#454545")
            series_values = []
            for field, legend, color in series:
                values = [self._value(point, field) for point in points]
                series_values.append((legend, self.offsets, values))
                line = axis.plot(
                    offsets, values, color=color, linewidth=0.9, label=legend
                )[0]
                path = line.get_path()
                path.should_simplify = True
                path.simplify_threshold = 0.35
            axis.legend(
                loc="upper right",
                fontsize=7,
                facecolor=SURFACE,
                labelcolor=TEXT,
            )
            axis.set_ylabel(label, fontsize=8, color=TEXT)
            axis.tick_params(labelsize=8, colors=MUTED)
            axis.grid(alpha=0.18, color=MUTED)
            add_axis_tools(axis, series_values)
        for index, field in enumerate(selected_fields, start=len(groups)):
            axis = self.axes[index]
            axis.set_facecolor(SURFACE)
            for spine in axis.spines.values():
                spine.set_color("#454545")
            line = axis.plot(
                field.offsets,
                field.values,
                color="#8dd3c7",
                linewidth=0.9,
                label=field.label,
            )[0]
            path = line.get_path()
            path.should_simplify = True
            path.simplify_threshold = 0.35
            axis.legend(
                loc="upper right",
                fontsize=7,
                facecolor=SURFACE,
                labelcolor=TEXT,
            )
            axis.set_ylabel(field.label, fontsize=8, color=TEXT)
            axis.tick_params(labelsize=8, colors=MUTED)
            axis.grid(alpha=0.18, color=MUTED)
            add_axis_tools(axis, [(field.label, field.offsets, field.values)])
        self.axes[-1].xaxis.set_major_formatter(
            FuncFormatter(lambda value, _: str(timedelta(seconds=max(0, value))))
        )
        self.axes[-1].set_xlabel("Elapsed time", fontsize=8, color=MUTED)
        if not groups and not selected_fields:
            self.axes[len(self.axes) // 2].text(
                0.5,
                0.5,
                "No series selected; right-click to choose data",
                ha="center",
                va="center",
                transform=self.axes[len(self.axes) // 2].transAxes,
                color=MUTED,
            )
        if self.time_bounds[1] <= self.time_bounds[0]:
            self.time_bounds = (
                (float(self.offsets[0]), float(self.offsets[-1]))
                if self.offsets
                else (0.0, 0.0)
            )
        if old_view is not None:
            self._apply_view_range(*old_view, emit=False, draw=False)
        elif self.time_bounds[1] > self.time_bounds[0]:
            self._apply_view_range(*self.time_bounds, emit=False, draw=False)
        self._draw_selection()
        self.draw_idle()

    def set_logged_fields(self, fields, *, reset=False, redraw=True):
        self.logged_fields = tuple(fields)
        available = {field.key for field in self.logged_fields}
        if reset:
            self.selected_logged_fields.clear()
        else:
            self.selected_logged_fields.intersection_update(available)
        if redraw:
            self.set_points(self.points, self.offsets, self.playback)

    def set_logged_field(self, key, enabled):
        if enabled:
            self.selected_logged_fields.add(key)
        else:
            self.selected_logged_fields.discard(key)
        self.set_points(self.points, self.offsets, self.playback)

    @staticmethod
    def _is_shift_pressed(event):
        key = getattr(event, "key", None)
        return key == "shift" or (isinstance(key, str) and "shift" in key.lower())

    def _range_position(self, event):
        if not self.playback or event.inaxes not in self.axes or event.xdata is None:
            return None
        low, high = self.time_bounds
        return min(high, max(low, float(event.xdata)))

    def _set_selection(self, bounds):
        low, high = sorted(map(float, bounds))
        self.selection = (low, high)
        self._draw_selection()

    def _clear_selection(self, draw=True):
        self.selection = None
        self._draw_selection()
        if draw:
            self._paint_cursor()

    def _draw_selection(self):
        if len(self._selection_artists) != len(self.axes):
            self._selection_artists = []
            for axis in self.axes:
                artist = Rectangle(
                    (0, 0),
                    0,
                    1,
                    transform=axis.get_xaxis_transform(),
                    facecolor="#ffbf40",
                    edgecolor="none",
                    alpha=0.18,
                    animated=True,
                    visible=False,
                    zorder=0,
                )
                axis.add_patch(artist)
                self._selection_artists.append(artist)
        for artist in self._selection_artists:
            if self.selection is None:
                artist.set_visible(False)
            else:
                start, end = self.selection
                artist.set_x(start)
                artist.set_width(end - start)
                artist.set_visible(True)
        self._paint_cursor()

    def _show_range_menu(self):
        menu = self._build_context_menu()
        menu.exec(QCursor.pos())

    def _build_context_menu(self):
        menu = QMenu(self)
        if self.selection is not None and self.selection[1] > self.selection[0]:
            delete_action = menu.addAction("Delete")
            delete_action.triggered.connect(self.delete_selected_range)
            menu.addSeparator()
        for key, label in (
            ("speed", "Speed"),
            ("aws", "Apparent wind speed"),
            ("tws", "True wind speed"),
            ("depth", "Water depth"),
            ("hdg", "Heading"),
            ("twa", "Wind angles (AWA / TWA)"),
        ):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(self.series[key])
            action.triggered.connect(
                lambda enabled, series_key=key: self.set_series(series_key, enabled)
            )
        if self.logged_fields:
            menu.addSeparator()
            recorded = menu.addMenu("Recorded log fields")
            for field in self.logged_fields:
                action = recorded.addAction(field.label)
                action.setCheckable(True)
                action.setChecked(field.key in self.selected_logged_fields)
                action.triggered.connect(
                    lambda enabled, field_key=field.key: self.set_logged_field(
                        field_key, enabled
                    )
                )
        return menu

    def delete_selected_range(self, _checked=False):
        if self.selection is None or self.delete_range is None:
            logger.info(
                "Log editor Delete ignored; selection=%r callback_available=%s",
                self.selection,
                self.delete_range is not None,
            )
            return False
        bounds = self.selection
        logger.info("Log editor invoking Delete callback for %r", bounds)
        deleted = self.delete_range(*bounds)
        logger.info("Log editor Delete callback returned %r", deleted)
        if deleted:
            self._clear_selection()
        return bool(deleted)

    def undo_log_edit(self):
        return bool(self.undo is not None and self.undo())

    def set_time_bounds(self, start, end, *, reset=False):
        self.time_bounds = (float(start), max(float(start), float(end)))
        if reset or self.view_range is None:
            self._apply_view_range(*self.time_bounds)
            return
        self._apply_view_range(*self.view_range)

    def set_viewport_fraction(self, fraction):
        if not self.axes or self.time_bounds[1] <= self.time_bounds[0]:
            return
        start, end = self.axes[0].get_xlim()
        span = min(end - start, self.time_bounds[1] - self.time_bounds[0])
        max_start = self.time_bounds[1] - span
        fraction = min(1.0, max(0.0, float(fraction)))
        self._apply_view_range(
            self.time_bounds[0] + fraction * (max_start - self.time_bounds[0]),
            self.time_bounds[0] + fraction * (max_start - self.time_bounds[0]) + span,
        )

    def _apply_view_range(self, start, end, *, emit=True, draw=True):
        if not self.axes or self.time_bounds[1] <= self.time_bounds[0]:
            self.view_range = None
            return
        low, high = self.time_bounds
        total = high - low
        span = min(total, max(min(1.0, total), float(end) - float(start)))
        start = min(high - span, max(low, float(start)))
        end = start + span
        self.view_range = (start, end)
        for axis in self.axes:
            axis.set_xlim(start, end)
        if emit and self.viewport_changed is not None:
            fraction = (start - low) / max(1e-12, total - span)
            self.viewport_changed(fraction)
        if draw:
            self.draw_idle()

    def set_viewport_callback(self, callback):
        self.viewport_changed = callback
        self._apply_view_range(
            *(self.view_range or self.time_bounds), emit=True, draw=False
        )

    def _zoom(self, event):
        if event.inaxes not in self.axes or event.xdata is None:
            return
        low, high = self.time_bounds
        if high <= low:
            return
        start, end = self.axes[0].get_xlim()
        old_span = end - start
        factor = 0.8 if getattr(event, "step", 0) > 0 else 1.25
        new_span = min(high - low, max(min(1.0, high - low), old_span * factor))
        anchor = min(end, max(start, float(event.xdata)))
        fraction = (anchor - start) / max(old_span, 1e-12)
        new_start = anchor - fraction * new_span
        self._apply_view_range(new_start, new_start + new_span)

    @staticmethod
    def _value(point, field):
        if field in ("awd", "twd"):
            angle = getattr(point, "awa" if field == "awd" else "twa", None)
            heading = getattr(point, "hdg", None)
            return (
                (angle + heading) % 360
                if angle is not None and heading is not None
                else np.nan
            )
        return getattr(point, field, None)

    def _plot_groups(self):
        groups = []
        if self.series["speed"]:
            groups.append(("Speed (kn)", [("speed", "Speed", "#8dd3c7")]))
        wind = []
        direction = []
        for key, legend, color in (
            ("aws", "AWS", "#feffb3"),
            ("tws", "TWS", "#bfbbd9"),
        ):
            if self.series[key]:
                wind.append((key, legend, color))
                direction.append(
                    (
                        "awd" if key == "aws" else "twd",
                        "AWD" if key == "aws" else "TWD",
                        color,
                    )
                )
                if self.series["twa"]:
                    direction.append(
                        (
                            "awa" if key == "aws" else "twa",
                            "AWA" if key == "aws" else "TWA",
                            "#fe11b3" if key == "aws" else "#bf11d9",
                        )
                    )
        if wind:
            groups.append(("Wind (kn)", wind))
        if self.series["depth"]:
            groups.append(("Depth (m)", [("depth", "Depth", "#fa8174")]))
        if self.series["hdg"]:
            direction.append(("hdg", "HDG", "#81b1d2"))
        if direction:
            groups.append(("Direction (°)", direction))
        return groups

    def set_series(self, key, enabled):
        self.series[key] = enabled
        self.set_points(self.points, self.offsets, self.playback)

    def _drawn(self, _):
        self.background = self.copy_from_bbox(self.figure.bbox)
        self._paint_cursor(blit=False)

    def _hover_motion(self, event):
        if self._selecting or self._scrubbing:
            self._set_hover(None)
            return
        if (
            event.inaxes not in self.axes
            or event.xdata is None
            or not any(self._hover_values)
        ):
            self._set_hover(None)
            return
        x = float(event.xdata)
        self._set_hover(x)

    @staticmethod
    def _display_value(value):
        if value is None:
            return "—"
        try:
            if not np.isfinite(value):
                return "—"
        except TypeError:
            return "—"
        return (
            f"{value:g}" if isinstance(value, (int, float, np.number)) else str(value)
        )

    def _nearest_index(self, offsets, position):
        if not offsets:
            return None
        right = bisect_left(offsets, position)
        if right == 0:
            return 0
        if right == len(offsets):
            return right - 1
        return min(
            (right - 1, right),
            key=lambda index: abs(float(offsets[index]) - position),
        )

    def _set_hover(self, position):
        if position == self._hover_position:
            return
        self._hover_position = position
        self._hover_index = (
            self._nearest_index(self.offsets, position)
            if position is not None
            else None
        )
        for axis, cursor, label, values in zip(
            self.axes, self.hover_cursors, self.hover_labels, self._hover_values
        ):
            visible = position is not None
            cursor.set_visible(visible)
            label.set_visible(visible)
            if visible:
                nearest = (
                    self.offsets[self._hover_index]
                    if self._hover_index is not None
                    else position
                )
                cursor.set_xdata([nearest, nearest])
                readings = []
                for name, offsets, series_values in values:
                    index = self._nearest_index(offsets, position)
                    value = series_values[index] if index is not None else None
                    readings.append(f"{name}: {self._display_value(value)}")
                label.set_text(f"{position:.1f}s\n" + "\n".join(readings))
        self._paint_cursor()

    def set_position(self, position):
        if position != self.position:
            self.position = position
            self._paint_cursor()

    def _paint_cursor(self, blit=True):
        if self.background is None:
            return
        self.restore_region(self.background)
        if self.playback:
            for axis, cursor in zip(self.axes, self.cursors):
                cursor.set_xdata([self.position, self.position])
                axis.draw_artist(cursor)
        for axis, artist in zip(self.axes, self._selection_artists):
            if artist.get_visible():
                axis.draw_artist(artist)
        for axis, cursor, label in zip(
            self.axes, self.hover_cursors, self.hover_labels
        ):
            if cursor.get_visible():
                axis.draw_artist(cursor)
            if label.get_visible():
                axis.draw_artist(label)
        if blit:
            self.blit(self.figure.bbox)
