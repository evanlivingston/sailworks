"""Native Qt Matplotlib canvas with complete log data and a blitted cursor."""

from datetime import timedelta

import numpy as np
from matplotlib.ticker import FuncFormatter
from gweatherrouting.core.timecontrol import naive_wall_time
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from .theme import SURFACE, TEXT, MUTED


class LogPlots(FigureCanvasQTAgg):
    def __init__(self, parent=None):
        figure = Figure(figsize=(8, 3), layout="tight", facecolor=SURFACE)
        super().__init__(figure)
        self.setParent(parent)
        self.series = {
            "speed": True,
            "aws": False,
            "tws": True,
            "depth": False,
            "hdg": True,
            "twa": False,
        }
        self.points = ()
        self.axes = []
        self.cursors = []
        self.background = None
        self.position = 0.0
        self.playback = False
        self.offsets = ()
        self.mpl_connect("draw_event", self._drawn)
        self.set_points((), (), False)

    def set_points(self, points, offsets, playback):
        self.points = tuple(points)
        self.playback = playback
        self.offsets = tuple(offsets)
        self.background = None
        self.cursors = []
        groups = self._plot_groups()
        self.figure.clear()
        self.axes = list(
            self.figure.subplots(max(1, len(groups)), sharex=True, squeeze=False)[:, 0]
        )
        for axis, (label, series) in zip(self.axes, groups):
            axis.clear()
            axis.set_facecolor(SURFACE)
            for spine in axis.spines.values():
                spine.set_color("#454545")
            for field, legend, color in series:
                values = [self._value(point, field) for point in points]
                axis.plot(offsets, values, color=color, linewidth=0.9, label=legend)
            axis.legend(fontsize=7, facecolor=SURFACE, labelcolor=TEXT)
            axis.set_ylabel(label, fontsize=8, color=TEXT)
            axis.tick_params(labelsize=8, colors=MUTED)
            axis.grid(alpha=0.18, color=MUTED)
            self.cursors.append(
                axis.axvline(
                    self.position,
                    color="#ffbf40",
                    linewidth=1.4,
                    animated=True,
                    visible=playback and bool(points),
                )
            )
        if points and len(offsets):
            anchor = naive_wall_time(points[0].time) - timedelta(
                seconds=float(offsets[0])
            )
            fmt = (
                "%H:%M:%S"
                if float(offsets[-1]) - float(offsets[0]) < 86400
                else "%m-%d %H:%M"
            )
            self.axes[-1].xaxis.set_major_formatter(
                FuncFormatter(
                    lambda value, _: (anchor + timedelta(seconds=value)).strftime(fmt)
                )
            )
        self.axes[-1].set_xlabel("Time (local)", fontsize=8, color=MUTED)
        if not points:
            self.axes[len(self.axes) // 2].text(
                0.5,
                0.5,
                "Load a log or stream instrument data",
                ha="center",
                va="center",
                transform=self.axes[len(self.axes) // 2].transAxes,
                color=MUTED,
            )
        self.draw_idle()

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
        return getattr(point, field, None) or 0

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

    def set_position(self, position):
        if position != self.position:
            self.position = position
            self._paint_cursor()

    def _paint_cursor(self, blit=True):
        if self.background is None or not self.playback:
            return
        self.restore_region(self.background)
        for axis, cursor in zip(self.axes, self.cursors):
            cursor.set_xdata([self.position, self.position])
            axis.draw_artist(cursor)
        if blit:
            self.blit(self.figure.bbox)


def elapsed_offsets(points):
    if not points:
        return ()
    first = points[0].time
    return np.array([(point.time - first).total_seconds() for point in points])
