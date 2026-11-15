"""Edit a route's saved defaults: departure time and drawn colour."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QPushButton,
    QWidget,
)


class RouteEditDialog(QDialog):
    """Edit the values a route carries into the routing panel.

    The departure saved here seeds the routing panel; the panel field still
    overrides it for a single run.
    """

    def __init__(self, parent=None, *, departure=None, colour=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Route")
        self._colour = str(colour) if colour else None

        self.use_departure = QCheckBox("Default departure")
        self.departure = QDateTimeEdit()
        self.departure.setCalendarPopup(True)
        self.departure.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.departure.setAccessibleName("Default departure")
        self.departure.setEnabled(False)

        start = QDateTime.currentDateTime()
        if departure:
            try:
                start = QDateTime(datetime.fromisoformat(str(departure)))
            except ValueError:
                pass
        self.departure.setDateTime(start)
        self.use_departure.setChecked(bool(departure))
        self.departure.setEnabled(bool(departure))
        self.use_departure.toggled.connect(self.departure.setEnabled)

        self.colour_button = QPushButton()
        self.colour_button.setAccessibleName("Route colour")
        self.colour_button.clicked.connect(self._pick_colour)
        self._paint_swatch()

        form = QFormLayout(self)
        form.addRow(self.use_departure, self.departure)
        form.addRow("Colour", self._colour_row())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _colour_row(self):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.colour_button)
        clear = QPushButton("Use automatic")
        clear.clicked.connect(self._clear_colour)
        layout.addWidget(clear)
        return row

    def _pick_colour(self):
        initial = QColor(self._colour) if self._colour else QColor(Qt.GlobalColor.blue)
        chosen = QColorDialog.getColor(initial, self, "Route colour")
        if chosen.isValid():
            self._colour = chosen.name()
            self._paint_swatch()

    def _clear_colour(self):
        self._colour = None
        self._paint_swatch()

    def _paint_swatch(self):
        if self._colour:
            self.colour_button.setText(self._colour)
            self.colour_button.setStyleSheet(
                f"background-color: {self._colour}; color: white;"
            )
        else:
            self.colour_button.setText("Automatic")
            self.colour_button.setStyleSheet("")

    def values(self):
        """The edited values, with ``None`` meaning "leave automatic"."""
        departure = None
        if self.use_departure.isChecked():
            departure = (
                self.departure.dateTime()
                .toPython()
                .replace(second=0, microsecond=0)
                .isoformat()
            )
        return {"default_departure": departure, "color": self._colour}
