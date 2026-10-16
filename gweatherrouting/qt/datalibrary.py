"""Local forecasts/charts and provider downloads in a native Qt dialog."""

from pathlib import Path

from PySide6.QtCore import Qt, QFileSystemWatcher, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QMenu,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .theme import section_label
from .forecastdownload import ForecastDownloadOptions
from gweatherrouting.core.forecastselection import IMPORTED, forecast_source

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
        self.resize(1000, 620)
        self.setMinimumSize(800, 520)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.mutations = []
        self._forecasts()
        self._charts()
        self._marine()
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
        self.forecast_watcher = QFileSystemWatcher(self)
        self.forecast_watcher.directoryChanged.connect(self._queue_forecast_scan)
        self.forecast_watcher.fileChanged.connect(self._queue_forecast_scan)
        self.forecast_scan_timer = QTimer(self)
        self.forecast_scan_timer.setSingleShot(True)
        self.forecast_scan_timer.timeout.connect(self._scan_visible_forecasts)
        self.data.changed.connect(self._sync_forecast_watches)
        self.tabs.currentChanged.connect(self._forecast_tab_changed)
        self._sync_forecast_watches()

    def button(self, row, label, callback):
        button = QPushButton(label)
        button.clicked.connect(callback)
        row.addWidget(button)
        self.mutations.append(button)
        return button

    def _forecasts(self):
        page = QWidget()
        self.forecast_page = page
        layout = QVBoxLayout(page)
        heading = QHBoxLayout()
        heading.addWidget(section_label("Weather models"))
        heading.addStretch()
        self.button(heading, "Files…", self.show_forecast_files)
        layout.addLayout(heading)
        names = [source.name for source in self.data.manager.sources]
        self.forecast_options = ForecastDownloadOptions([*names, IMPORTED])
        self.forecast_options.set_selection(self.data.selection)
        self.forecast_options.changed.connect(self._options_changed)
        layout.addWidget(self.forecast_options)
        row = QHBoxLayout()
        row.addStretch()
        self.forecast_download = self.button(
            row, "Download forecasts", self.download_forecast
        )
        layout.addLayout(row)
        layout.addStretch()
        self.files_dialog = QDialog(self)
        self.files_dialog.setWindowTitle("Forecast files")
        self.files_dialog.resize(850, 450)
        files = QVBoxLayout(self.files_dialog)
        heading = QHBoxLayout()
        heading.addWidget(section_label("Cached forecasts"))
        heading.addStretch()
        self.import_forecast_button = self.button(
            heading, "Import…", self.import_forecast
        )
        files.addLayout(heading)
        self.forecasts = table(["Used / file", "Model", "Model run", "Forecast hours"])
        self.forecasts.setColumnWidth(0, 300)
        self.forecasts.setColumnWidth(1, 90)
        self.forecasts.setColumnWidth(2, 190)
        files.addWidget(self.forecasts)
        self.forecasts.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.forecasts.customContextMenuRequested.connect(self._forecast_context_menu)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.files_dialog.hide)
        files.addWidget(close)
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

    def _marine(self):
        from .marinelibrary import MarineLibraryTab

        canvas = getattr(self.parent(), "map", None)
        if canvas is None:
            canvas = next(iter(self.controller.charts.maps), None)
        self.marine = MarineLibraryTab(
            self.controller, canvas, self._show_marine_settings, self
        )
        self.marine.jobs.changed.connect(self.refresh_buttons)
        self.tabs.addTab(self.marine, "Tides & Currents")

    def _show_marine_settings(self, kind):
        callback = getattr(self.parent(), "show_marine_settings", None)
        if callable(callback):
            callback(kind)

    def show_marine(self, kind):
        self.show()
        self.raise_()
        self.activateWindow()
        self.tabs.setCurrentWidget(self.marine)
        self.marine.set_kind(kind)

    def dispose_marine(self):
        self.marine.dispose()

    def use_map_center(self):
        if self.controller.charts.maps:
            canvas = self.controller.charts.maps[0]
            self.latitude.setValue(canvas._center_lat)
            self.longitude.setValue(canvas._center_lon)

    def refresh(self):
        entries = self.data.local_forecasts
        enabled = {g.path for g in self.data.manager.gribs}
        selected = self.forecasts.item(self.forecasts.currentRow(), 0)
        selected_path = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        fill_table(
            self.forecasts,
            [
                (
                    g.name,
                    getattr(g, "model_name", "Unknown"),
                    g.start_time,
                    g.last_forecast,
                )
                for g in entries
            ],
            [g.path in enabled for g in entries],
            [False for g in entries],
        )
        self.forecasts.blockSignals(True)
        for row, forecast in enumerate(entries):
            self.forecasts.item(row, 0).setData(Qt.ItemDataRole.UserRole, forecast.path)
            if forecast.path == selected_path:
                self.forecasts.selectRow(row)
        self.forecasts.blockSignals(False)
        show_imported = IMPORTED in self.data.selection.models or any(
            forecast_source(g) == IMPORTED for g in entries
        )
        for widget in self.forecast_options.model_rows[IMPORTED]:
            widget.setVisible(show_imported)
        if self.forecast_options.selection() != self.data.selection:
            self.forecast_options.set_selection(self.data.selection)
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
        self.forecast_options.setEnabled(not busy)
        self.latitude.setEnabled(not busy)
        self.longitude.setEnabled(not busy)
        self.refresh_buttons()

    def refresh_buttons(self):
        blocked = (
            self.data.task.busy
            or self.controller.routing.busy
            or self.controller.polars.task.busy
            or self.marine.jobs.busy
        )
        for button in self.mutations:
            button.setEnabled(not blocked)
        self.forecast_options.setEnabled(not blocked)
        self.forecasts.setEnabled(not blocked)
        self.charts.setEnabled(not blocked)
        self.forecast_download.setEnabled(
            not blocked and any(name != IMPORTED for name in self.data.selection.models)
        )
        self.chart_download.setEnabled(
            not blocked and self.available_charts.currentRow() >= 0
        )
        chart = selected_row(self.charts, getattr(self, "chart_entries", []))
        # Base charts are managed by the installation, not the imported library.
        imported = chart is not None and self.imported_chart(chart)
        self.remove_chart_button.setEnabled(not blocked and imported)
        self.marine.refresh_buttons(blocked)

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

    def remove_forecast(self, metadata=None):
        if metadata is None:
            metadata = selected_row(self.forecasts, self.data.local_forecasts)
        if (
            metadata is not None
            and QMessageBox.question(
                self,
                "Remove forecast from library?",
                f"Delete the library copy of {metadata.name}? "
                "Your original imported file is retained.",
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.attempt(lambda: self.data.remove_forecast(metadata))

    def _forecast_context_menu(self, position):
        item = self.forecasts.itemAt(position)
        if item is None or not self.forecasts.isEnabled():
            return
        self.forecasts.selectRow(item.row())
        metadata = selected_row(self.forecasts, self.data.local_forecasts)
        menu = QMenu(self.forecasts)
        remove = menu.addAction("Remove from library…")
        if menu.exec(self.forecasts.viewport().mapToGlobal(position)) is remove:
            self.remove_forecast(metadata)

    def _options_changed(self):
        self.attempt(lambda: self.data.set_selection(self.forecast_options.selection()))

    def show_forecast_files(self):
        self.files_dialog.show()
        self.files_dialog.raise_()

    def _forecast_bounds(self):
        active = getattr(self.parent(), "_active_map", None)
        canvas = (
            active()
            if callable(active)
            else next(iter(self.controller.charts.maps), None)
        )
        if canvas is None:
            raise ValueError("Open a map before finding forecasts for this area.")
        top_left, bottom_right = canvas.get_bbox()
        north, west = top_left.get_degrees()
        south, east = bottom_right.get_degrees()
        if east - west >= 360:
            west, east = -180, 180
        else:
            west, east = (west + 180) % 360 - 180, (east + 180) % 360 - 180
        if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
            raise ValueError(
                "Move or zoom the map to an area that does not cross the date line."
            )
        return south, west, north, east

    def _forecasts_visible(self):
        return self.isVisible() and self.tabs.currentWidget() is self.forecast_page

    def _sync_forecast_watches(self):
        directory = self.data.forecast_directory()
        directory = directory if directory.is_dir() else directory.parent
        wanted = {str(directory)} | {
            str(g.path) for g in self.data.local_forecasts if Path(g.path).is_file()
        }
        current = set(self.forecast_watcher.directories()) | set(
            self.forecast_watcher.files()
        )
        if current - wanted:
            self.forecast_watcher.removePaths(list(current - wanted))
        if wanted - current:
            self.forecast_watcher.addPaths(list(wanted - current))

    def _queue_forecast_scan(self, *_):
        if self._forecasts_visible():
            self.forecast_scan_timer.start(200)

    def _scan_visible_forecasts(self):
        if self._forecasts_visible() and not self.data.scan_forecasts():
            if not self.data.task.closed:
                self.forecast_scan_timer.start(500)

    def _forecast_tab_changed(self, *_):
        self.forecast_scan_timer.stop()
        self._scan_visible_forecasts()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._scan_visible_forecasts()

    def hideEvent(self, event):  # noqa: N802
        self.files_dialog.hide()
        self.forecast_scan_timer.stop()
        super().hideEvent(event)

    def download_forecast(self):
        def download():
            self.forecast_options.options()
            names = self.data.selection.models
            bounds = (
                self._forecast_bounds()
                if any(name in ("NOAA GFS", "NOAA HRRR") for name in names)
                else None
            )
            self.data.download_selected(bounds)

        self.attempt(download)

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
