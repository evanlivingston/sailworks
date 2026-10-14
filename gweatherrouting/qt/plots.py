"""Native Qt Matplotlib canvas with complete log data and a blitted cursor."""

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure


class LogPlots(FigureCanvasQTAgg):
    def __init__(self, parent=None):
        figure = Figure(figsize=(8, 3), layout="tight")
        super().__init__(figure)
        self.setParent(parent)
        self.axes = figure.subplots(3, sharex=True)
        self.cursors = []
        self.background = None
        self.position = 0.0
        self.playback = False
        self.offsets = ()
        self.mpl_connect("draw_event", self._drawn)
        self.set_points((), (), False)

    def set_points(self, points, offsets, playback):
        self.playback = playback
        self.offsets = tuple(offsets)
        self.background = None
        self.cursors = []
        for axis, field, label, color in zip(
            self.axes,
            ("speed", "tws", "hdg"),
            ("Speed (kn)", "Wind (kn)", "Heading (°)"),
            ("#287ca2", "#14856d", "#8662a5"),
        ):
            axis.clear()
            values = [getattr(point, field) or 0 for point in points]
            axis.plot(offsets, values, color=color, linewidth=0.9)
            axis.set_ylabel(label, fontsize=8)
            axis.tick_params(labelsize=8)
            axis.grid(alpha=0.15)
            self.cursors.append(
                axis.axvline(
                    self.position,
                    color="#d8851b",
                    linewidth=1.4,
                    animated=True,
                    visible=playback and bool(points),
                )
            )
        self.axes[-1].set_xlabel("Elapsed time (s)", fontsize=8)
        if not points:
            self.axes[1].text(
                0.5,
                0.5,
                "Load a log or stream instrument data",
                ha="center",
                va="center",
                transform=self.axes[1].transAxes,
            )
        self.draw_idle()

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
