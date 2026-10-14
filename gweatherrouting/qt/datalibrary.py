"""Local forecasts/charts and provider downloads in a native Qt dialog."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QGridLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .theme import section_label

FORECAST_FILTER = "GRIB forecasts (*.grib *.grb *.grib2 *.grb2);;All files (*)"


def table(headers):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setAlternatingRowColors(True)
    widget.horizontalHeader().setStretchLastSection(True)
    widget.verticalHeader().hide()
    return widget


def selected_row(widget, entries):
    row = widget.currentRow()
    return entries[row] if 0 <= row < len(entries) else None


def fill_table(widget, rows, checked=None, editable_checks=None):
    previous = widget.currentRow()
    widget.blockSignals(True)
    widget.setRowCount(len(rows))
    for row, values in enumerate(rows):
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            item.setToolTip(str(value))
            if column == 0 and checked is not None:
                if editable_checks is None or editable_checks[row]:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                else:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked if checked[row] else Qt.CheckState.Unchecked
                )
            widget.setItem(row, column, item)
    if rows:
        widget.selectRow(max(0, min(previous, len(rows) - 1)))
    widget.blockSignals(False)


def coordinate(value, latitude):
    spin = QDoubleSpinBox()
    spin.setRange(-90 if latitude else -180, 90 if latitude else 180)
    spin.setDecimals(4)
    spin.setValue(value)
    return spin


class DataLibrary(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.data = controller.data
        self.setWindowTitle("Data Library")
        self.resize(850, 700)
        self.setMinimumSize(620, 520)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.mutations = []
        self._forecasts()
        self._charts()
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.data.changed.connect(self.refresh)
        controller.routing.changed.connect(self.refresh)
        controller.polars.changed.connect(self.refresh_buttons)
        self.refresh()

    def button(self, row, label, callback):
        button = QPushButton(label)
        button.clicked.connect(callback)
        row.addWidget(button)
        self.mutations.append(button)
        return button

    def _forecasts(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(section_label("Local wind forecasts"))
        self.forecasts = table(["Enabled / file", "Model run", "Forecast hours"])
        self.forecasts.setColumnWidth(0, 330)
        self.forecasts.setColumnWidth(1, 190)
        self.forecasts.setColumnWidth(2, 100)
        layout.addWidget(self.forecasts, 2)
        self.forecasts.itemChanged.connect(self.toggle_forecast)
        self.forecasts.itemSelectionChanged.connect(self.refresh_buttons)
        row = QHBoxLayout()
        self.button(row, "Import…", self.import_forecast)
        self.button(row, "Refresh", self.data.scan_forecasts)
        self.remove_forecast_button = self.button(
            row, "Remove copy…", self.remove_forecast
        )
        layout.addLayout(row)
        layout.addWidget(section_label("Download wind forecasts"))
        options = QHBoxLayout()
        self.source = QComboBox()
        self.source.addItems(["NOAA GFS", "OpenSkiron"])
        self.source.currentTextChanged.connect(self.source_changed)
        options.addWidget(self.source)
        self.button(options, "Find forecasts", self.find_forecasts)
        self.forecast_download = self.button(
            options, "Download selected", self.download_forecast
        )
        layout.addLayout(options)
        self.bounds_panel = QWidget()
        bounds = QGridLayout(self.bounds_panel)
        bounds.setContentsMargins(0, 0, 0, 0)
        self.south = coordinate(40, True)
        self.north = coordinate(55, True)
        self.west = coordinate(-130, False)
        self.east = coordinate(-115, False)
        for index, (label, widget) in enumerate(
            (
                ("South", self.south),
                ("West", self.west),
                ("North", self.north),
                ("East", self.east),
            )
        ):
            row_index, column = divmod(index, 2)
            bounds.addWidget(QLabel(label), row_index, column * 2)
            bounds.addWidget(widget, row_index, column * 2 + 1)
        layout.addWidget(self.bounds_panel)
        self.downloads = table(["Forecast", "Source", "Size", "Model run"])
        self.downloads.setColumnWidth(0, 230)
        self.downloads.setColumnWidth(1, 140)
        self.downloads.setColumnWidth(2, 80)
        self.downloads.itemSelectionChanged.connect(self.refresh_buttons)
        layout.addWidget(self.downloads, 2)
        self.tabs.addTab(page, "Forecasts")

    def _charts(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(section_label("Local charts"))
        self.charts = table(["Enabled / chart", "Type", "Path"])
        self.charts.setColumnWidth(0, 180)
        self.charts.setColumnWidth(1, 80)
        self.charts.itemChanged.connect(self.toggle_chart)
        self.charts.itemSelectionChanged.connect(self.refresh_buttons)
        layout.addWidget(self.charts, 2)
        row = QHBoxLayout()
        self.button(row, "Vector file…", self.import_vector)
        self.button(row, "Vector folder…", self.import_vector_folder)
        self.button(row, "Raster folder…", self.import_raster)
        self.remove_chart_button = self.button(row, "Remove", self.remove_chart)
        layout.addLayout(row)
        note = QLabel(
            "Chart imports reference your files. Removing a chart keeps its files on disk."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addWidget(section_label("Download NOAA ENC charts"))
        row = QHBoxLayout()
        self.latitude = coordinate(48.3, True)
        self.longitude = coordinate(-123.4, False)
        row.addWidget(QLabel("Latitude"))
        row.addWidget(self.latitude)
        row.addWidget(QLabel("Longitude"))
        row.addWidget(self.longitude)
        self.button(row, "Use map center", self.use_map_center)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.button(row, "Find charts", self.find_charts)
        self.chart_download = self.button(row, "Download selected", self.download_chart)
        layout.addLayout(row)
        self.available_charts = table(["Cell", "Title", "Scale", "Size"])
        self.available_charts.setColumnWidth(0, 110)
        self.available_charts.horizontalHeader().setStretchLastSection(False)
        self.available_charts.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.available_charts.setColumnWidth(2, 100)
        self.available_charts.setColumnWidth(3, 90)
        self.available_charts.itemSelectionChanged.connect(self.refresh_buttons)
        layout.addWidget(self.available_charts, 2)
        self.tabs.addTab(page, "Charts")

    def use_map_center(self):
        if self.controller.charts.maps:
            canvas = self.controller.charts.maps[0]
            self.latitude.setValue(canvas._center_lat)
            self.longitude.setValue(canvas._center_lon)

    def refresh(self):
        entries = self.data.local_forecasts
        enabled = {g.path for g in self.data.manager.gribs}
        fill_table(
            self.forecasts,
            [(g.name, g.start_time, g.last_forecast) for g in entries],
            [g.path in enabled for g in entries],
        )
        fill_table(self.downloads, [row[:4] for _, row in self.data.remote_forecasts])
        self.chart_entries = list(self.controller.charts.charts)
        fill_table(
            self.charts,
            [
                (c.path.rstrip("/").split("/")[-1], c.ctype, c.path)
                for c in self.chart_entries
            ],
            [c.enabled for c in self.chart_entries],
            [self.imported_chart(c) for c in self.chart_entries],
        )
        fill_table(
            self.available_charts,
            [
                (c.name, c.title, f"1:{c.scale:,}", f"{c.size / 1024:.0f} KB")
                for c in self.data.remote_charts
            ],
        )
        busy = self.data.task.busy
        self.progress.setVisible(busy)
        self.progress.setRange(0, 0 if self.data.task.percent < 0 else 100)
        if self.data.task.percent >= 0:
            self.progress.setValue(self.data.task.percent)
        self.status.setText(self.data.task.message)
        self.source.setEnabled(not busy)
        self.bounds_panel.setEnabled(not busy)
        self.latitude.setEnabled(not busy)
        self.longitude.setEnabled(not busy)
        self.refresh_buttons()

    def refresh_buttons(self):
        blocked = (
            self.data.task.busy
            or self.controller.routing.busy
            or self.controller.polars.task.busy
        )
        for button in self.mutations:
            button.setEnabled(not blocked)
        self.forecasts.setEnabled(not blocked)
        self.charts.setEnabled(not blocked)
        self.forecast_download.setEnabled(
            not blocked and self.downloads.currentRow() >= 0
        )
        self.chart_download.setEnabled(
            not blocked and self.available_charts.currentRow() >= 0
        )
        self.remove_forecast_button.setEnabled(
            not blocked and self.forecasts.currentRow() >= 0
        )
        chart = selected_row(self.charts, getattr(self, "chart_entries", []))
        # Base charts are managed by the installation, not the imported library.
        imported = chart is not None and self.imported_chart(chart)
        self.remove_chart_button.setEnabled(not blocked and imported)

    def imported_chart(self, chart):
        return any(
            e["path"] == chart.path
            for e in self.controller.settings[chart.ctype + "Charts"]
        )

    def attempt(self, operation):
        try:
            operation()
        except Exception as exc:
            QMessageBox.warning(self, "Unable to update library", str(exc))
            self.refresh()

    def import_forecast(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import wind forecast", "", FORECAST_FILTER
        )
        if path:
            self.data.import_forecast(path)

    def toggle_forecast(self, item):
        if item.column() == 0:
            metadata = self.data.local_forecasts[item.row()]
            self.attempt(
                lambda: self.data.set_forecast_enabled(
                    metadata, item.checkState() == Qt.CheckState.Checked
                )
            )

    def remove_forecast(self):
        metadata = selected_row(self.forecasts, self.data.local_forecasts)
        if (
            metadata is not None
            and QMessageBox.question(
                self,
                "Remove forecast copy?",
                f"Delete {metadata.name} from the forecast library? Original imports are retained.",
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.attempt(lambda: self.data.remove_forecast(metadata))

    def source_changed(self):
        self.bounds_panel.setVisible(self.source.currentText() == "NOAA GFS")
        self.data.remote_forecasts = []
        self.refresh()

    def find_forecasts(self):
        bounds = (
            self.south.value(),
            self.west.value(),
            self.north.value(),
            self.east.value(),
        )
        if self.source.currentText() == "NOAA GFS" and (
            bounds[0] >= bounds[2] or bounds[1] >= bounds[3]
        ):
            self.status.setText(
                "South must be below north; west must be below east. Split areas crossing 180°."
            )
            return
        self.data.list_forecasts(self.source.currentText(), bounds)

    def download_forecast(self):
        entry = selected_row(self.downloads, self.data.remote_forecasts)
        if entry is not None:
            self.data.download_forecast(entry)

    def import_vector(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import vector chart",
            "",
            "Vector charts (*.000 *.geojson *.shp *.pbf *.osm);;All files (*)",
        )
        if path:
            self.data.import_chart(path, "vector")

    def import_vector_folder(self):
        path = QFileDialog.getExistingDirectory(
            self, "Import vector chart folder (CM93)"
        )
        if path:
            self.data.import_chart(path + "/", "vector")

    def import_raster(self):
        path = QFileDialog.getExistingDirectory(self, "Import raster chart folder")
        if path:
            self.data.import_chart(path, "raster")

    def toggle_chart(self, item):
        if item.column() == 0:
            chart = self.chart_entries[item.row()]
            if any(
                e["path"] == chart.path
                for e in self.controller.settings[chart.ctype + "Charts"]
            ):
                self.attempt(
                    lambda: self.data.set_chart_enabled(
                        chart, item.checkState() == Qt.CheckState.Checked
                    )
                )
            else:
                chart.enabled = item.checkState() == Qt.CheckState.Checked
                self.data.redraw()

    def remove_chart(self):
        chart = selected_row(self.charts, self.chart_entries)
        if chart is not None:
            self.attempt(lambda: self.data.remove_chart(chart))

    def find_charts(self):
        self.data.list_charts(self.latitude.value(), self.longitude.value())

    def download_chart(self):
        cell = selected_row(self.available_charts, self.data.remote_charts)
        if cell is not None:
            self.data.download_chart(cell)
