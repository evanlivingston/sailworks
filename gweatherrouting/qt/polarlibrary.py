"""Polar library and exact-table/curve preview using the routing engine model."""

import math
from typing import cast

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib.projections.polar import PolarAxes
from PySide6.QtCore import Qt, QSortFilterProxyModel
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from gweatherrouting.core.polarfiles import orc_filename
from .theme import SURFACE, TEXT, MUTED, section_label


class PolarPreview(QDialog):
    def __init__(self, name, polar, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Polar · " + name)
        self.resize(850, 650)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)
        figure = Figure(figsize=(7, 5), layout="tight", facecolor=SURFACE)
        canvas = FigureCanvasQTAgg(figure)
        self.axis = cast(PolarAxes, figure.add_subplot(projection="polar"))
        axis = self.axis
        axis.set_facecolor(SURFACE)
        axis.set_theta_zero_location("N")
        axis.set_theta_direction(-1)
        axis.set_thetamin(0)
        axis.set_thetamax(180)
        axis.tick_params(colors=MUTED)
        axis.spines["polar"].set_color(MUTED)
        axis.grid(alpha=0.2, color=MUTED)
        for index, wind in enumerate(polar.tws):
            if wind <= 0:
                continue
            axis.plot(
                polar.twa,
                [row[index] for row in polar.speed_table],
                linewidth=1,
                label=f"{wind:g} kn TWS",
            )
        axis.set_title("Boat speed (kn) by true wind angle", color=TEXT, fontsize=11)
        axis.legend(
            loc="upper left",
            bbox_to_anchor=(1.02, 1),
            fontsize=8,
            facecolor=SURFACE,
            labelcolor=TEXT,
            edgecolor=MUTED,
        )
        tabs.addTab(canvas, "Curves")
        self.table = QTableWidget(len(polar.twa), len(polar.tws) + 1)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setHorizontalHeaderLabels(
            ["TWA / TWS (kn)"] + [f"{wind:g}" for wind in polar.tws]
        )
        for row, angle in enumerate(polar.twa):
            self.table.setItem(row, 0, QTableWidgetItem(f"{math.degrees(angle):g}°"))
            for column, value in enumerate(polar.speed_table[row], 1):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
        tabs.addTab(self.table, "Speed table (kn)")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def make_table(headers):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.horizontalHeader().setStretchLastSection(True)
    table.verticalHeader().hide()
    return table


class PolarLibrary(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.polars = controller.polars
        self.manager = self.polars.manager
        self.preview_dialog = None
        layout = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(splitter)
        local = QWidget()
        local_layout = QVBoxLayout(local)
        local_layout.addWidget(section_label("Local polars"))
        self.local = make_table(["Enabled / file", "Boat", "Type"])
        self.local.setColumnWidth(0, 270)
        self.local.setColumnWidth(1, 200)
        self.local.itemChanged.connect(self.toggle)
        self.local.itemSelectionChanged.connect(self.selection_changed)
        local_layout.addWidget(self.local)
        actions = QHBoxLayout()
        self.import_button = QPushButton("Import .pol…")
        self.preview_button = QPushButton("Preview…")
        self.use_button = QPushButton("Use for routing")
        self.remove_button = QPushButton("Remove copy…")
        for button, callback in (
            (self.import_button, self.import_file),
            (self.preview_button, self.preview),
            (self.use_button, self.use_for_routing),
            (self.remove_button, self.remove),
        ):
            button.clicked.connect(callback)
            actions.addWidget(button)
        local_layout.addLayout(actions)
        splitter.addWidget(local)
        remote = QWidget()
        remote_layout = QVBoxLayout(remote)
        remote_layout.addWidget(section_label("ORC polars"))
        search = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search boat, type or sail number")
        self.search.textChanged.connect(self.filter_catalog)
        search.addWidget(self.search, 1)
        self.fetch_button = QPushButton("Load catalog")
        self.fetch_button.clicked.connect(self.polars.fetch_catalog)
        search.addWidget(self.fetch_button)
        self.download_button = QPushButton("Download selected")
        self.download_button.clicked.connect(self.download)
        search.addWidget(self.download_button)
        remote_layout.addLayout(search)
        self.remote = QTableView()
        self.remote.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.remote.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.remote.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.remote.setAlternatingRowColors(True)
        self.catalog_model = QStandardItemModel(self)
        self.catalog_model.setHorizontalHeaderLabels(
            ["Sail / identifier", "Boat", "Type", "Installed"]
        )
        self.catalog_filter = QSortFilterProxyModel(self)
        self.catalog_filter.setSourceModel(self.catalog_model)
        self.catalog_filter.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.catalog_filter.setFilterKeyColumn(-1)
        self.remote.setModel(self.catalog_filter)
        self.remote.horizontalHeader().setStretchLastSection(True)
        self.remote.setColumnWidth(0, 150)
        self.remote.setColumnWidth(1, 240)
        self.remote.setColumnWidth(2, 180)
        self.remote.selectionModel().selectionChanged.connect(self.refresh_buttons)
        remote_layout.addWidget(self.remote)
        splitter.addWidget(remote)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        layout.addWidget(self.progress)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.polars.changed.connect(self.refresh)
        self.polars.preview_changed.connect(self.show_preview)
        controller.routing.changed.connect(self.refresh_buttons)
        controller.data.changed.connect(self.refresh_buttons)
        self.catalog_version: list[tuple[str, str, str]] | None = None
        self.installed = set()
        self.names = []
        self.refresh()

    def selected_name(self):
        row = self.local.currentRow()
        return self.names[row] if 0 <= row < len(self.names) else ""

    @staticmethod
    def populate(table, rows, checks=None, selected=0):
        table.blockSignals(True)
        table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                if checks is not None and column == 0:
                    item.setCheckState(
                        Qt.CheckState.Checked
                        if checks[row]
                        else Qt.CheckState.Unchecked
                    )
                table.setItem(row, column, item)
        if rows and selected >= 0:
            table.selectRow(min(selected, len(rows) - 1))
        table.blockSignals(False)

    def refresh(self):
        selected = self.polars.selected or self.selected_name()
        self.names = list(self.manager.polars_files)
        rows = []
        for name in self.names:
            metadata = self.manager.get_metadata(name)
            rows.append((name, metadata.get("name", ""), metadata.get("type", "")))
        self.populate(
            self.local,
            rows,
            [self.manager.is_enabled(n) for n in self.names],
            self.names.index(selected) if selected in self.names else 0,
        )
        self.update_catalog()
        self.filter_catalog()
        self.progress.setVisible(self.polars.task.busy)
        self.status.setText(self.polars.task.message)
        self.refresh_buttons()

    def selection_changed(self):
        self.polars.selected = self.selected_name()
        self.refresh_buttons()

    def update_catalog(self):
        installed = set(self.names)
        if id(self.catalog_version) != id(self.polars.catalog):
            self.catalog_model.removeRows(0, self.catalog_model.rowCount())
            self.catalog_filter.setDynamicSortFilter(False)
            for row in self.polars.catalog:
                items = [
                    QStandardItem(value)
                    for value in (
                        *row,
                        "Yes" if orc_filename(row[0]) in installed else "",
                    )
                ]
                items[0].setData(row, Qt.ItemDataRole.UserRole)
                self.catalog_model.appendRow(items)
            self.catalog_filter.setDynamicSortFilter(True)
            self.catalog_version = self.polars.catalog
        elif installed != self.installed:
            for index, row in enumerate(self.polars.catalog):
                self.catalog_model.item(index, 3).setText(
                    "Yes" if orc_filename(row[0]) in installed else ""
                )
        self.installed = installed

    def filter_catalog(self, *_):
        self.catalog_filter.setFilterFixedString(self.search.text().strip())
        self.refresh_buttons()

    def selected_remote(self):
        index = self.remote.currentIndex()
        if not index.isValid():
            return None
        source = self.catalog_filter.mapToSource(index)
        return self.catalog_model.item(source.row(), 0).data(Qt.ItemDataRole.UserRole)

    def refresh_buttons(self, *_):
        blocked = self.polars.blocked
        name = self.selected_name()
        self.local.setEnabled(not blocked)
        self.import_button.setEnabled(not blocked)
        self.preview_button.setEnabled(not blocked and bool(name))
        self.remove_button.setEnabled(not blocked and bool(name))
        self.use_button.setEnabled(
            not blocked and bool(name) and self.manager.is_enabled(name)
        )
        self.fetch_button.setEnabled(not blocked)
        row = self.selected_remote()
        available = row is not None and orc_filename(row[0]) not in self.names
        self.download_button.setEnabled(not blocked and available)

    def attempt(self, operation):
        try:
            operation()
        except Exception as exc:
            QMessageBox.warning(self, "Unable to update polars", str(exc))
            self.refresh()

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import polar", "", "Polar files (*.pol);;All files (*)"
        )
        if path:
            self.polars.import_file(path)

    def toggle(self, item):
        if item.column() == 0:
            self.attempt(
                lambda: self.polars.set_enabled(
                    self.names[item.row()], item.checkState() == Qt.CheckState.Checked
                )
            )

    def preview(self):
        name = self.selected_name()
        if name:
            self.attempt(lambda: self.polars.preview(name))

    def show_preview(self, name, polar):
        if self.preview_dialog is not None:
            self.preview_dialog.close()
        self.preview_dialog = PolarPreview(name, polar, self)
        self.preview_dialog.show()
        self.preview_dialog.raise_()

    def use_for_routing(self):
        self.polars.use_for_routing(self.selected_name())

    def remove(self):
        name = self.selected_name()
        if (
            name
            and QMessageBox.question(
                self, "Remove polar copy?", f"Delete {name} from the polar library?"
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.attempt(lambda: self.polars.remove(name))

    def download(self):
        row = self.selected_remote()
        if row is not None:
            self.polars.download(row[0])
