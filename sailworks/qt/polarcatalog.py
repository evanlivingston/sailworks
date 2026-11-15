"""Browsing the cached ORC catalog and adding one boat from the Polars panel."""

from PySide6.QtCore import Qt, QSortFilterProxyModel
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from sailworks.core.polarfiles import orc_filename
from .tables import PANEL_MARGINS, fit_columns, make_view
from .theme import section_label

EMPTY_TEXT = "No catalog yet. Update it in File → Data Library → Polars."


class PolarCatalogDialog(QDialog):
    def __init__(self, polars, parent=None):
        super().__init__(parent)
        self.polars = polars
        self.manager = polars.manager
        self.version = None
        self.installed = None
        self.index = {}
        self.setWindowTitle("Add from ORC")
        self.resize(760, 520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*PANEL_MARGINS)
        layout.setSpacing(6)
        layout.addWidget(section_label("ORC polar catalog"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search boat, type or identifier")
        self.search.textChanged.connect(self.filter_rows)
        layout.addWidget(self.search)
        self.table = make_view()
        self.model = QStandardItemModel(self)
        self.model.setHorizontalHeaderLabels(
            ["Sail / identifier", "Boat", "Type", "Installed"]
        )
        self.filter = QSortFilterProxyModel(self)
        self.filter.setSourceModel(self.model)
        self.filter.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.filter.setFilterKeyColumn(-1)
        self.table.setModel(self.filter)
        fit_columns(self.table, stretch=(0,), contents=(1, 2, 3))
        self.table.selectionModel().selectionChanged.connect(self.selection_changed)
        layout.addWidget(self.table)
        self.empty = QLabel(EMPTY_TEXT)
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)
        actions = QHBoxLayout()
        self.add_button = QPushButton("Add")
        self.add_button.clicked.connect(self.add)
        actions.addWidget(self.add_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        layout.addWidget(self.progress)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.polars.changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        polars = self.polars
        installed = set(self.manager.polars_files)
        if id(self.version) != id(polars.catalog):
            self.model.removeRows(0, self.model.rowCount())
            self.filter.setDynamicSortFilter(False)
            self.index = {}
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
                self.model.appendRow(items)
                self.index[identifier] = row
            self.filter.setDynamicSortFilter(True)
            self.version = polars.catalog
        elif installed != self.installed:
            for identifier, row in self.index.items():
                name = "Yes" if orc_filename(identifier) in installed else ""
                self.model.item(row, 3).setText(name)
        self.installed = installed
        self.empty.setVisible(not polars.catalog)
        self.table.setVisible(bool(polars.catalog))
        self.search.setEnabled(bool(polars.catalog))
        self.progress.setVisible(polars.task.busy)
        if polars.task.busy:
            self.progress.setRange(0, 0 if polars.task.percent < 0 else 100)
            if polars.task.percent >= 0:
                self.progress.setValue(polars.task.percent)
        self.status.setText(polars.task.message)
        self.refresh_buttons()

    def filter_rows(self, *_):
        self.filter.setFilterFixedString(self.search.text().strip())
        self.refresh_buttons()

    def selected_identifier(self):
        index = self.table.currentIndex()
        if not index.isValid():
            return ""
        item = self.model.item(self.filter.mapToSource(index).row(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else ""

    def selection_changed(self, *_):
        self.refresh_buttons()
        identifier = self.selected_identifier()
        if not identifier:
            return
        if orc_filename(identifier) in self.manager.polars_files:
            return
        known = self.polars.identities.get(identifier)
        if known is not None:
            self.apply_identity(identifier, *known)
        elif not self.polars.task.busy:
            self.polars.lookup_boat(identifier, self.apply_identity)

    def apply_identity(self, identifier, name, boat_type):
        row = self.index.get(identifier)
        if row is None:
            return
        self.model.item(row, 1).setText(name)
        self.model.item(row, 2).setText(boat_type)

    def refresh_buttons(self):
        identifier = self.selected_identifier()
        available = (
            bool(identifier)
            and orc_filename(identifier) not in self.manager.polars_files
        )
        self.add_button.setEnabled(not self.polars.blocked and available)

    def add(self):
        identifier = self.selected_identifier()
        if identifier:
            self.polars.download(identifier)
