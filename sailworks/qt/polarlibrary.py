"""Polar library and exact-table/curve preview using the routing engine model."""

import math
from typing import cast

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .polarcatalog import PolarCatalogDialog
from .tables import PANEL_MARGINS, fit_columns, make_table
from .theme import SURFACE, TEXT, MUTED, ThemedMenu


class PolarPreview(QDialog):
    def __init__(self, name, polar, parent=None):
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        from matplotlib.projections.polar import PolarAxes

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
        # Full speed matrix, not a list table, so it stays off tables.make_table().
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


class PolarLibrary(QWidget):
    polar_chosen = Signal(str)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.polars = controller.polars
        self.manager = self.polars.manager
        self.preview_dialog = None
        self.catalog_dialog = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*PANEL_MARGINS)
        layout.setSpacing(6)
        splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(splitter)
        local = QWidget()
        local_layout = QVBoxLayout(local)
        local_layout.setContentsMargins(0, 0, 0, 0)
        self.local = make_table(["Enabled / file", "Sail", "Name", "Type"])
        fit_columns(self.local, stretch=(0, 2), contents=(1, 3))
        self.local.itemChanged.connect(self.toggle)
        self.local.itemSelectionChanged.connect(self.selection_changed)
        self.local.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.local.customContextMenuRequested.connect(self._local_menu)
        self.local.itemDoubleClicked.connect(self._local_double_click)
        local_layout.addWidget(self.local)
        actions = QHBoxLayout()
        self.import_button = QPushButton("Import .pol")
        self.import_button.clicked.connect(self.import_file)
        actions.addWidget(self.import_button)
        self.catalog_button = QPushButton("Add from ORC")
        self.catalog_button.clicked.connect(self.open_catalog)
        actions.addWidget(self.catalog_button)
        actions.addStretch(1)
        local_layout.addLayout(actions)
        splitter.addWidget(local)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        layout.addWidget(self.progress)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        # Refresh after a checkbox event has finished using its table item.
        self.polars.changed.connect(self.refresh, Qt.ConnectionType.QueuedConnection)
        self.polars.preview_changed.connect(self.show_preview)
        controller.routing.changed.connect(self.refresh_buttons)
        controller.data.changed.connect(self.refresh_buttons)
        self.installed = set()
        self.names = []
        self.refresh()

    def selected_name(self):
        row = self.local.currentRow()
        return self.names[row] if 0 <= row < len(self.names) else ""

    def _local_menu(self, position):
        index = self.local.indexAt(position)
        if not index.isValid():
            return
        self.local.setCurrentCell(index.row(), 0)
        if self.polars.blocked or not self.selected_name():
            return
        menu = ThemedMenu(self)
        menu.addAction("Preview").triggered.connect(self.preview)
        menu.addAction("Delete").triggered.connect(self.remove)
        menu.exec(self.local.viewport().mapToGlobal(position))

    def _local_double_click(self, item):
        if item is None or item.column() == 0 or self.polars.blocked:
            return
        if self.selected_name():
            self.preview()

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
            rows.append(
                (
                    name,
                    metadata.get("sail", ""),
                    metadata.get("name", ""),
                    metadata.get("type", ""),
                )
            )
        self.populate(
            self.local,
            rows,
            [self.manager.is_enabled(n) for n in self.names],
            self.names.index(selected) if selected in self.names else 0,
        )
        self.progress.setVisible(self.polars.task.busy)
        self.status.setText(self.polars.task.message)
        self.refresh_buttons()

    def selection_changed(self):
        name = self.selected_name()
        self.polars.selected = name
        self.refresh_buttons()
        if name:
            self.polar_chosen.emit(name)

    def open_catalog(self):
        if self.catalog_dialog is not None:
            self.catalog_dialog.close()
        self.catalog_dialog = PolarCatalogDialog(self.polars, self)
        self.catalog_dialog.show()
        self.catalog_dialog.raise_()

    def refresh_buttons(self, *_):
        blocked = self.polars.blocked
        self.local.setEnabled(not blocked)
        self.import_button.setEnabled(not blocked)

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
