"""Published model forecasts, charts, and marine data in a native Qt dialog."""

from types import SimpleNamespace

from PySide6.QtCore import Qt, QSortFilterProxyModel
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .theme import section_label
from .dropdown import Dropdown
from .chartlayerspanel import ChartLayersPanel
from .mapdatasetup import MapDataSetup
from sailworks.core.polarfiles import orc_filename
from sailworks.rendering.style import Style
from .tables import fit_columns, make_table, make_view
from .text import qt_text


def selected_row(widget, entries):
    row = widget.currentRow()
    return entries[row] if 0 <= row < len(entries) else None


def local_chart_entries(charts, settings):
    entries = list(charts)
    loaded = {(c.ctype, c.path) for c in entries}
    for kind in ("vector", "raster", "mbtiles"):
        for entry in settings[kind + "Charts"]:
            if (kind, entry["path"]) not in loaded:
                entries.append(
                    SimpleNamespace(
                        path=entry["path"], ctype=kind, enabled=False, deferred=True
                    )
                )
    return entries


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
    def __init__(self, controller, parent=None, capture_region=None):
        super().__init__(parent)
        self.controller = controller
        self.data = controller.data
        self.polars = controller.polars
        self.capture_region = capture_region
        self.forecast_options = None
        self.setWindowTitle("Data Library")
        self.resize(1000, 620)
        self.setMinimumSize(800, 520)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.mutations = []
        self._forecasts()
        self._history()
        self._charts()
        self._marine()
        self._polars()
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
        controller.polars.changed.connect(self.refresh_polars)
        controller.map_data.changed.connect(self.refresh_buttons)
        self.refresh()
        self.tabs.currentChanged.connect(self._forecast_tab_changed)

    def button(self, row, label, callback):
        button = QPushButton(qt_text(label))
        button.clicked.connect(callback)
        row.addWidget(button)
        self.mutations.append(button)
        return button

    def _forecasts(self):
        self.forecast_options = None
        page = QWidget()
        self.forecast_page = page
        layout = QVBoxLayout(page)
        heading = QHBoxLayout()
        heading.addWidget(section_label("Weather models"))
        heading.addStretch()
        layout.addLayout(heading)
        if self.capture_region is not None:
            from .forecastdownload import AnalysisForecastDownloadOptions

            self.forecast_options = AnalysisForecastDownloadOptions(
                self.controller, self.capture_region, page
            )
        if self.forecast_options is not None:
            layout.addWidget(self.forecast_options)
            self.forecast_download = self.forecast_options.refresh_button
        else:
            self.forecast_download = None
        layout.addStretch()
        self.tabs.addTab(page, "Forecasts")

    def _charts(self):
        self.charts_page = QWidget()
        outer = QVBoxLayout(self.charts_page)
        self.chart_sections = QTabWidget()
        outer.addWidget(self.chart_sections)
        sources = QWidget()
        source_layout = QVBoxLayout(sources)
        self.chart_layers_panel = ChartLayersPanel(self.controller)
        source_layout.addWidget(self.chart_layers_panel)
        self.palette_combo = Dropdown("Chart colors")
        self.palette_combo.setAccessibleName("Chart palette")
        self.palette_combo.addItems(list(Style.chart_palettes))
        self.palette_combo.setCurrentText(self.controller.settings.chartPalette)
        self.palette_combo.setToolTip(
            "Vector chart colors; PNG/MBTiles imagery keeps its original colors"
        )
        self.palette_combo.currentTextChanged.connect(self._set_palette)
        self.controller.settings.register_on_change("chartPalette", self._sync_palette)
        source_layout.addWidget(self.palette_combo)
        self.chart_sections.addTab(sources, "Sources && appearance")

        local = QWidget()
        layout = QVBoxLayout(local)
        layout.addWidget(section_label("Local charts"))
        self.charts = make_table(["Enabled / chart", "Type", "Path"])
        self.charts.setColumnWidth(0, 180)
        self.charts.setColumnWidth(1, 80)
        self.charts.itemChanged.connect(self.toggle_chart)
        self.charts.itemSelectionChanged.connect(self.refresh_buttons)
        layout.addWidget(self.charts, 2)
        row = QHBoxLayout()
        self.button(row, "Vector file", self.import_vector)
        self.button(row, "Vector folder", self.import_vector_folder)
        self.button(row, "Raster folder", self.import_raster)
        self.button(row, "MBTiles", self.import_mbtiles)
        self.remove_chart_button = self.button(row, "Remove", self.remove_chart)
        layout.addLayout(row)
        note = QLabel(
            "Chart imports reference your files. Removing a chart keeps its files on disk."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.chart_sections.addTab(local, "Local files")

        downloads = QWidget()
        layout = QVBoxLayout(downloads)
        layout.addWidget(section_label("Base map data"))
        self.map_data = MapDataSetup(self.controller.map_data)
        layout.addWidget(self.map_data)
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
        self.available_charts = make_table(["Cell", "Title", "Scale", "Size"])
        self.available_charts.setColumnWidth(0, 110)
        self.available_charts.horizontalHeader().setStretchLastSection(False)
        self.available_charts.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.available_charts.setColumnWidth(2, 100)
        self.available_charts.setColumnWidth(3, 90)
        self.available_charts.itemSelectionChanged.connect(self.refresh_buttons)
        layout.addWidget(self.available_charts, 2)
        self.chart_sections.addTab(downloads, "Downloads && base maps")
        self.tabs.addTab(self.charts_page, "Charts")

    def _history(self):
        from .era5download import Era5DownloadTab

        self.history = Era5DownloadTab(self.controller, self.capture_region, self)
        self.tabs.addTab(self.history, "History")

    def show_charts(self, section=0):
        self.tabs.setCurrentWidget(self.charts_page)
        self.chart_sections.setCurrentIndex(section)

    def _set_palette(self, value):
        self.controller.settings["chartPalette"] = value
        self.data.redraw()

    def _sync_palette(self, value):
        blocked = self.palette_combo.blockSignals(True)
        self.palette_combo.setCurrentText(value)
        self.palette_combo.blockSignals(blocked)

    def _marine(self):
        from .marinelibrary import MarineLibraryTab

        canvas = getattr(self.parent(), "map", None)
        if canvas is None:
            canvas = next(iter(self.controller.charts.maps), None)
        self.marine = MarineLibraryTab(
            self.controller, canvas, self._show_marine_settings, self
        )
        self.marine.jobs.changed.connect(self.refresh_buttons)
        self.tabs.addTab(self.marine, "Tides && Currents")

    def _polars(self):
        self.polar_version = None
        self.polar_installed = None
        self.polar_index = {}
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QHBoxLayout()
        heading.addWidget(section_label("ORC polar catalog"))
        heading.addStretch()
        self.catalog_button = self.button(
            heading, "Update catalog", self.update_catalog
        )
        layout.addLayout(heading)
        details = QHBoxLayout()
        self.catalog_updated = QLabel()
        self.catalog_count = QLabel()
        details.addWidget(self.catalog_updated)
        details.addWidget(self.catalog_count)
        details.addStretch(1)
        layout.addLayout(details)
        self.polar_search = QLineEdit()
        self.polar_search.setPlaceholderText("Search boat, type or identifier")
        self.polar_search.textChanged.connect(self.filter_polars)
        layout.addWidget(self.polar_search)
        self.polar_table = make_view()
        self.polar_model = QStandardItemModel(self)
        self.polar_model.setHorizontalHeaderLabels(
            ["Sail / identifier", "Boat", "Type", "Installed"]
        )
        self.polar_filter = QSortFilterProxyModel(self)
        self.polar_filter.setSourceModel(self.polar_model)
        self.polar_filter.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.polar_filter.setFilterKeyColumn(-1)
        self.polar_table.setModel(self.polar_filter)
        fit_columns(self.polar_table, stretch=(0,), contents=(1, 2, 3))
        self.polar_table.selectionModel().selectionChanged.connect(self.polar_selected)
        layout.addWidget(self.polar_table)
        actions = QHBoxLayout()
        self.add_polar_button = self.button(
            actions, "Add to polar library", self.add_polar
        )
        actions.addStretch(1)
        layout.addLayout(actions)
        self.polar_empty = QLabel(
            "No catalog yet. Use Update catalog to download it."
        )
        self.polar_empty.setWordWrap(True)
        layout.addWidget(self.polar_empty)
        self.tabs.addTab(page, "Polars")

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
            self.latitude.setValue(canvas.camera_state.center_lat)
            self.longitude.setValue(canvas.camera_state.center_lon)

    def refresh(self):
        self.chart_entries = local_chart_entries(
            self.controller.charts.charts, self.controller.settings
        )
        fill_table(
            self.charts,
            [
                (c.path.rstrip("/").split("/")[-1], c.ctype, c.path)
                for c in self.chart_entries
            ],
            [c.enabled for c in self.chart_entries],
            [
                self.imported_chart(c) and not getattr(c, "deferred", False)
                for c in self.chart_entries
            ],
        )
        fill_table(
            self.available_charts,
            [
                (c.name, c.title, f"1:{c.scale:,}", f"{c.size / 1024:.0f} KB")
                for c in self.data.remote_charts
            ],
        )
        busy = self.data.task.busy
        self.sync_progress()
        if self.forecast_options is not None:
            self.forecast_options.setEnabled(not busy)
        self.latitude.setEnabled(not busy)
        self.longitude.setEnabled(not busy)
        self.refresh_polars()
        self.refresh_buttons()

    def refresh_polars(self):
        polars = self.polars
        installed = set(polars.manager.polars_files)
        if id(self.polar_version) != id(polars.catalog):
            self.polar_model.removeRows(0, self.polar_model.rowCount())
            self.polar_filter.setDynamicSortFilter(False)
            self.polar_index = {}
            for row, entry in enumerate(polars.catalog):
                identifier, boat, boat_type = entry
                known = polars.identities.get(identifier)
                if known is not None:
                    boat, boat_type = known
                items = [
                    QStandardItem(str(identifier)),
                    QStandardItem(str(boat)),
                    QStandardItem(str(boat_type)),
                    QStandardItem(
                        "Yes" if orc_filename(identifier) in installed else ""
                    ),
                ]
                items[0].setData(identifier, Qt.ItemDataRole.UserRole)
                self.polar_model.appendRow(items)
                self.polar_index[identifier] = row
            self.polar_filter.setDynamicSortFilter(True)
            self.polar_version = polars.catalog
        elif installed != self.polar_installed:
            for identifier, row in self.polar_index.items():
                name = "Yes" if orc_filename(identifier) in installed else ""
                self.polar_model.item(row, 3).setText(name)
        self.polar_installed = installed
        count = len(polars.catalog)
        self.catalog_count.setText(f"{count:,} boats" if count else "")
        updated = polars.catalog_updated
        if updated:
            self.catalog_updated.setText("Last updated " + updated[:10])
        else:
            self.catalog_updated.setText("Not downloaded yet")
        self.polar_empty.setVisible(not count)
        self.polar_table.setVisible(bool(count))
        self.polar_search.setEnabled(bool(count))
        self.sync_progress()
        self.refresh_buttons()

    def sync_progress(self):
        polars = self.polars.task
        task = polars if polars.busy else self.data.task
        self.progress.setVisible(task.busy)
        self.progress.setRange(0, 0 if task.percent < 0 else 100)
        if task.percent >= 0:
            self.progress.setValue(task.percent)
        self.status.setText(task.message if task.busy else self.data.task.message)

    def filter_polars(self, *_):
        self.polar_filter.setFilterFixedString(self.polar_search.text().strip())
        self.refresh_buttons()

    def selected_polar(self):
        index = self.polar_table.currentIndex()
        if not index.isValid():
            return ""
        item = self.polar_model.item(
            self.polar_filter.mapToSource(index).row(), 0
        )
        return item.data(Qt.ItemDataRole.UserRole) if item else ""

    def polar_selected(self, *_):
        self.refresh_buttons()
        identifier = self.selected_polar()
        if not identifier:
            return
        if orc_filename(identifier) in self.polars.manager.polars_files:
            return
        known = self.polars.identities.get(identifier)
        if known is not None:
            self.apply_identity(identifier, *known)
        elif not self.polars.task.busy:
            self.polars.lookup_boat(identifier, self.apply_identity)

    def apply_identity(self, identifier, name, boat_type):
        row = self.polar_index.get(identifier)
        if row is None:
            return
        self.polar_model.item(row, 1).setText(name)
        self.polar_model.item(row, 2).setText(boat_type)

    def update_catalog(self):
        self.polars.update_catalog()

    def add_polar(self):
        identifier = self.selected_polar()
        if identifier:
            self.attempt(lambda: self.polars.download(identifier))

    def refresh_buttons(self):
        blocked = (
            self.data.task.busy
            or self.controller.routing.busy
            or self.controller.polars.task.busy
            or self.controller.map_data.task.busy
            or self.marine.jobs.busy
        )
        for button in self.mutations:
            button.setEnabled(not blocked)
        if self.forecast_options is not None:
            self.forecast_options.setEnabled(not blocked)
        self.charts.setEnabled(not blocked)
        if self.forecast_download is not None:
            self.forecast_download.setEnabled(not blocked)
        self.chart_download.setEnabled(
            not blocked and self.available_charts.currentRow() >= 0
        )
        chart = selected_row(self.charts, getattr(self, "chart_entries", []))
        # Base charts are managed by the installation, not the imported library.
        imported = (
            chart is not None
            and not getattr(chart, "deferred", False)
            and self.imported_chart(chart)
        )
        self.remove_chart_button.setEnabled(not blocked and imported)
        button = getattr(self, "add_polar_button", None)
        if button is not None:
            identifier = self.selected_polar()
            available = bool(identifier) and (
                orc_filename(identifier) not in self.polars.manager.polars_files
            )
            button.setEnabled(not blocked and available)
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

    def _forecast_tab_changed(self, *_):
        self._show_model_configuration_if_visible()

    def _show_model_configuration_if_visible(self):
        if (
            self.forecast_options is not None
            and self.tabs.currentWidget() is self.forecast_page
        ):
            self.forecast_options.on_shown()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._show_model_configuration_if_visible()

    def hideEvent(self, event):  # noqa: N802
        super().hideEvent(event)

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

    def import_mbtiles(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import offline raster MBTiles", "", "MBTiles charts (*.mbtiles)"
        )
        if path:
            self.data.import_chart(path, "mbtiles")

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
