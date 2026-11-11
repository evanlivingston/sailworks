"""Minimal leg-series plot linked to the shared routing selection."""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from sailworks.core.marine.routinginspection import RoutingInspector
from sailworks.core.marine.routingresult import RoutingResult
from sailworks.core.marine.routingselection import RoutingSelection

SERIES = (
    ("speed_through_water_knots", QColor(40, 90, 200)),
    ("true_wind_speed_knots", QColor(200, 120, 30)),
)


class RoutingLegPlot(QWidget):
    """Plot recorded per-leg speeds; highlight the selected leg."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self._result = None
        self._inspector = None
        self._selection = None
        self._unsubscribe = None

    def set_result(self, result: RoutingResult, selection: RoutingSelection):
        if not isinstance(result, RoutingResult):
            raise TypeError("result must be a RoutingResult")
        if not isinstance(selection, RoutingSelection):
            raise TypeError("selection must be a RoutingSelection")
        if self._unsubscribe is not None:
            self._unsubscribe()
        self._result = result
        self._inspector = RoutingInspector(result)
        self._selection = selection
        self._unsubscribe = selection.subscribe(self.update)
        self.update()

    def clear(self):
        """Drop the retained result so no stale plot survives a failed load."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._result = None
        self._inspector = None
        self._selection = None
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(250, 250, 250))
        if self._result is None or not self._result.legs:
            painter.setPen(QColor(90, 90, 90))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No legs")
            return
        left, top, right, bottom = 36, 10, 10, 20
        width = max(1, self.width() - left - right)
        height = max(1, self.height() - top - bottom)
        legs = self._result.legs
        step = width / max(1, len(legs) - 1) if len(legs) > 1 else 0

        values = [self._inspector.leg_readout(leg.leg_id)["values"] for leg in legs]
        maximum = max(
            (
                value
                for row in values
                for key, _ in SERIES
                for value in [row.get(key)]
                if isinstance(value, (int, float))
            ),
            default=1.0,
        )
        maximum = max(maximum, 1.0)

        def position(index, value):
            x = left + (index * step if len(legs) > 1 else 0)
            y = top + height - (value / maximum) * height
            return QPointF(x, y)

        for key, colour in SERIES:
            painter.setPen(QPen(colour, 1.5))
            previous = None
            for index, row in enumerate(values):
                value = row.get(key)
                if not isinstance(value, (int, float)):
                    previous = None
                    continue
                point = position(index, value)
                if previous is not None:
                    painter.drawLine(previous, point)
                previous = point

        selected = self._selection.leg_id if self._selection else None
        for index, leg in enumerate(legs):
            if leg.leg_id != selected:
                continue
            painter.setPen(QPen(QColor(220, 40, 40), 2))
            x = left + (index * step if len(legs) > 1 else 0)
            painter.drawLine(QPointF(x, top), QPointF(x, top + height))

        painter.setPen(QColor(90, 90, 90))
        painter.drawText(4, self.height() - 6, f"STW / wind (0–{maximum:.1f} kn)")
