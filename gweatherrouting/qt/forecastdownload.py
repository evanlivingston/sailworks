"""Custom GRIB selections, with provider limits visible before downloading."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QGridLayout,
    QVBoxLayout,
    QLabel,
    QCheckBox,
    QComboBox,
    QTableWidget,
    QTableWidgetItem,
    QAbstractItemView,
    QPushButton,
    QGroupBox,
)

from gweatherrouting.core.gribsources.options import DownloadOptions
from gweatherrouting.core.forecastselection import ForecastSelection, IMPORTED
from gweatherrouting.core.gribsources.noaagfs import GFS_CUSTOM_HOURS

MODELS = (
    ("NOAA GFS", "GFS", "0.25°", "10 days", "Global"),
    ("NOAA HRRR", "HRRR", "3 km", "18 / 48 h", "CONUS"),
    ("CMC HRDPS", "HRDPS", "2.5 km", "48 h", "Canada / northern US"),
    (
        IMPORTED,
        "Imported",
        "—",
        "From file",
        "Imported forecasts without a recognized download model",
    ),
)


class ForecastDownloadOptions(QWidget):
    changed = Signal()

    def __init__(self, sources, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        model_box = QGroupBox("Models")
        models = QGridLayout(model_box)
        models.setHorizontalSpacing(18)
        for column, heading in enumerate(("Model", "Resolution", "Forecast")):
            models.addWidget(QLabel(heading), 0, column)
        self.models = {}
        self.model_rows = {}
        model_row = 1
        for name, label, resolution, forecast, region in MODELS:
            if name not in sources:
                continue
            check = QCheckBox(label)
            check.setChecked(name == "NOAA GFS")
            check.setToolTip(region)
            self.models[name] = check
            models.addWidget(check, model_row, 0)
            resolution_label, forecast_label = QLabel(resolution), QLabel(forecast)
            models.addWidget(resolution_label, model_row, 1)
            models.addWidget(forecast_label, model_row, 2)
            self.model_rows[name] = (check, resolution_label, forecast_label)
            check.toggled.connect(self.rebuild_hours)
            model_row += 1
        models.setRowStretch(model_row, 1)
        layout.addWidget(model_box, 3)
        parameter_box = QGroupBox("Parameters")
        parameters = QVBoxLayout(parameter_box)
        self.fields = {}
        for name, label in (
            ("wind", "Wind at 10 m"),
            ("pressure", "Sea-level pressure"),
        ):
            check = QCheckBox(label)
            check.setChecked(True)
            check.toggled.connect(self.rebuild_hours)
            self.fields[name] = check
            parameters.addWidget(check)
        parameters.addStretch()
        layout.addWidget(parameter_box, 2)
        time_box = QGroupBox("Forecast hours")
        times = QVBoxLayout(time_box)
        row = QHBoxLayout()
        row.addWidget(QLabel("Every"))
        self.interval = QComboBox()
        for label, value in (
            ("Model default", 0),
            ("1 hour", 1),
            ("3 hours", 3),
            ("6 hours", 6),
            ("12 hours", 12),
        ):
            self.interval.addItem(label, value)
        self.interval.setCurrentIndex(2)
        row.addWidget(self.interval)
        row.addWidget(QLabel("for"))
        self.duration = QComboBox()
        for label, value in (
            ("12 hours", 12),
            ("1 day", 24),
            ("2 days", 48),
            ("3 days", 72),
            ("5 days", 120),
            ("7 days", 168),
            ("10 days", 240),
        ):
            self.duration.addItem(label, value)
        self.duration.setCurrentIndex(4)
        row.addWidget(self.duration)
        times.addLayout(row)
        self.hours = QTableWidget(0, 3)
        self.hours.horizontalHeader().hide()
        self.hours.verticalHeader().hide()
        self.hours.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.hours.horizontalHeader().setStretchLastSection(True)
        for column in range(3):
            self.hours.setColumnWidth(column, 65)
        self.hours.setMinimumHeight(140)
        self.hours.setMaximumHeight(190)
        self.hours.setToolTip(
            "Hours after the model run. Only checked hours are requested; "
            "each model uses the supported subset."
        )
        times.addWidget(self.hours)
        row = QHBoxLayout()
        for label, callback in (
            ("Clear", lambda: self.check_hours(False)),
            ("All", lambda: self.check_hours(True)),
            ("Defaults", self.defaults),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            row.addWidget(button)
        times.addLayout(row)
        layout.addWidget(time_box, 3)
        self.interval.currentIndexChanged.connect(self.rebuild_hours)
        self.duration.currentIndexChanged.connect(self.rebuild_hours)
        self.hours.itemChanged.connect(self.changed.emit)
        self.rebuild_hours()

    def set_selection(self, selection):
        self.blockSignals(True)
        for name, check in self.models.items():
            check.blockSignals(True)
            check.setChecked(name in selection.models)
            check.blockSignals(False)
        for name, check in self.fields.items():
            check.blockSignals(True)
            check.setChecked(name in selection.fields)
            check.blockSignals(False)
        for combo, value in (
            (self.duration, selection.hours),
            (self.interval, selection.interval),
        ):
            combo.blockSignals(True)
            combo.setCurrentIndex(combo.findData(value))
            combo.blockSignals(False)
        self.rebuild_hours()
        if selection.steps:
            for row in range(self.hours.rowCount()):
                for column in range(3):
                    item = self.hours.item(row, column)
                    if item is not None:
                        item.setCheckState(
                            Qt.CheckState.Checked
                            if item.data(Qt.ItemDataRole.UserRole) in selection.steps
                            else Qt.CheckState.Unchecked
                        )
        self.blockSignals(False)

    def selection(self):
        steps = tuple(
            item.data(Qt.ItemDataRole.UserRole)
            for row in range(self.hours.rowCount())
            for column in range(3)
            if (item := self.hours.item(row, column)) is not None
            and item.checkState() == Qt.CheckState.Checked
        )
        return ForecastSelection(
            self.names(),
            self.duration.currentData(),
            self.interval.currentData(),
            tuple(name for name, check in self.fields.items() if check.isChecked()),
            steps,
        )

    def names(self):
        return tuple(name for name, check in self.models.items() if check.isChecked())

    def available(self):
        hours: set[int] = set()
        fields = tuple(name for name, check in self.fields.items() if check.isChecked())
        for name in self.names():
            if name == "NOAA GFS":
                hours.update(GFS_CUSTOM_HOURS)
            else:
                step = 3 if name == "CMC HRDPS" and "wind" not in fields else 1
                hours.update(range(0, 49, step))
        return sorted(hours)

    def rebuild_hours(self, *_):
        options = DownloadOptions(
            self.duration.currentData(), self.interval.currentData()
        )
        values = options.available_steps(self.available())
        self.hours.blockSignals(True)
        self.hours.clearContents()
        self.hours.setRowCount((len(values) + 2) // 3)
        for index, hour in enumerate(values):
            item = QTableWidgetItem(str(hour))
            item.setData(Qt.ItemDataRole.UserRole, hour)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.hours.setItem(index // 3, index % 3, item)
        self.hours.blockSignals(False)
        self.changed.emit()

    def check_hours(self, checked):
        self.hours.blockSignals(True)
        for row in range(self.hours.rowCount()):
            for column in range(3):
                item = self.hours.item(row, column)
                if item is not None:
                    item.setCheckState(
                        Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
                    )
        self.hours.blockSignals(False)
        self.changed.emit()

    def defaults(self):
        self.interval.setCurrentIndex(2)
        self.rebuild_hours()

    def options(self):
        if not self.names():
            raise ValueError("Select at least one model")
        steps = tuple(
            item.data(Qt.ItemDataRole.UserRole)
            for row in range(self.hours.rowCount())
            for column in range(3)
            if (item := self.hours.item(row, column)) is not None
            and item.checkState() == Qt.CheckState.Checked
        )
        if not steps:
            raise ValueError("Select at least one forecast hour")
        return DownloadOptions(
            self.duration.currentData(),
            self.interval.currentData(),
            tuple(name for name, check in self.fields.items() if check.isChecked()),
            steps,
        )
