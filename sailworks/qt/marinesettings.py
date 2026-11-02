"""Saved marine source selection, provenance and system account controls."""

import json
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from .backgroundtask import BackgroundTask
from .marinepanel import CurrentSources, text_label


class CurrentsAccount(QWidget):
    def __init__(self, planner):
        super().__init__()
        self.closed = False
        self.provider = planner.worldwide
        self.task = BackgroundTask(self)
        self.task.finished.connect(self.finished)
        layout = QVBoxLayout(self)
        layout.addWidget(text_label("Worldwide currents · Copernicus Marine account"))
        self.username = QLineEdit()
        self.username.setPlaceholderText("Username or email")
        if self.provider.credentials:
            self.username.setText(self.provider.credentials[0])
        self.password = QLineEdit()
        self.password.setPlaceholderText("Password")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.username)
        layout.addWidget(self.password)
        self.save = QPushButton("Save account")
        self.forget = QPushButton("Forget account")
        self.save.clicked.connect(self.save_account)
        self.forget.clicked.connect(self.forget_account)
        layout.addWidget(self.save)
        layout.addWidget(self.forget)
        self.status = text_label(
            self.provider.credential_error
            or ("Saved account ready." if self.provider.credentials_saved else "")
        )
        layout.addWidget(self.status)
        from PySide6.QtWidgets import QLabel

        link = QLabel(
            '<a href="https://data.marine.copernicus.eu/register">Create a free account</a>'
        )
        link.setOpenExternalLinks(True)
        layout.addWidget(link)
        layout.addStretch()

    def start(self, operation):
        if self.task.start("Updating account…", operation):
            self.save.setEnabled(False)
            self.forget.setEnabled(False)
            self.status.setText("Updating account…")
        self.password.clear()

    def save_account(self):
        username, password = self.username.text(), self.password.text()

        def operation(_):
            self.provider.configure(username, password)
            return (
                "Account saved. Download data to connect."
                if self.provider.credentials_saved
                else "Account set for this session."
            )

        self.start(operation)

    def forget_account(self):
        def operation(_):
            self.provider.forget_account()
            return "Saved account removed."

        self.start(operation)

    def finished(self, result, error):
        self.save.setEnabled(True)
        self.forget.setEnabled(True)
        self.status.setText(str(error) if error is not None else result)
        if not self.provider.credentials:
            self.username.clear()

    def hideEvent(self, event):  # noqa: N802
        if not self.closed:
            self.password.clear()
        super().hideEvent(event)

    def dispose(self):
        if self.closed:
            return
        self.closed = True
        self.password.clear()
        self.task.close()


class MarineSettings(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.manager = controller.core.marine_manager
        self.closed = False
        self.syncing = False
        self.reload_timer = QTimer(self)
        self.reload_timer.setSingleShot(True)
        self.reload_timer.timeout.connect(self.reload)
        self.setWindowTitle("Marine settings")
        self.resize(760, 620)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.currents = CurrentSources(self.manager.fields, compact=False)
        self.tabs.addTab(self.currents, "Currents")
        station_page = QWidget()
        stations = QVBoxLayout(station_page)
        stations.addWidget(
            text_label("Downloaded sources · priority from top to bottom")
        )
        self.sources = QTableWidget(0, 4)
        self.sources.setHorizontalHeaderLabels(
            ["Use", "Station", "Capability", "Interval (UTC)"]
        )
        self.sources.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.sources.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.sources.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.sources.itemChanged.connect(self.toggle)
        self.sources.itemSelectionChanged.connect(self.details)
        stations.addWidget(self.sources, 1)
        row = QHBoxLayout()
        self.up = QPushButton("Move up")
        self.down = QPushButton("Move down")
        self.remove = QPushButton("Remove selected download")
        self.up.clicked.connect(lambda: self.action(lambda i: self.manager.move(i, -1)))
        self.down.clicked.connect(
            lambda: self.action(lambda i: self.manager.move(i, 1))
        )
        self.remove.clicked.connect(lambda: self.action(self.manager.remove))
        for button in (self.up, self.down, self.remove):
            row.addWidget(button)
        stations.addLayout(row)
        self.fallback = QCheckBox("Allow fallback to a lower priority source")
        self.fallback.toggled.connect(self.set_fallback)
        stations.addWidget(self.fallback)
        self.samples = QTableWidget(0, 6)
        self.samples.setHorizontalHeaderLabels(
            ["Time (UTC)", "Series", "Value", "Unit", "QC", "Qualifier"]
        )
        stations.addWidget(self.samples, 1)
        self.provenance = QPlainTextEdit()
        self.provenance.setReadOnly(True)
        stations.addWidget(self.provenance, 1)
        self.tabs.addTab(station_page, "Tides")
        layout.addWidget(self.tabs)
        self.status = text_label()
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.hide)
        layout.addWidget(buttons)
        self.manager.connect("changed", self.changed)
        self.manager.fields.connect("changed", self.changed)
        self.reload()

    def select_tab(self, tab):
        self.tabs.setCurrentIndex(0 if tab == "currents" else 1)
        self.show()
        self.raise_()
        self.activateWindow()

    def changed(self, *_):
        self.controller.scheduler.submit(self.reload)

    def selected_id(self):
        row = self.sources.currentRow()
        item = self.sources.item(row, 0) if row >= 0 else None
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def reload(self):
        if self.closed:
            return
        self.reload_timer.stop()
        self.currents.reload()
        selected = self.selected_id()
        self.syncing = True
        self.sources.clearContents()
        datasets = self.manager.ordered_datasets()
        self.sources.setRowCount(len(datasets))
        for row, dataset in enumerate(datasets):
            values = (
                "",
                dataset.station.name,
                ", ".join(c.value.replace("_", " ") for c in dataset.capabilities),
                dataset.requested_start.strftime("%d %b %H:%M")
                + " → "
                + dataset.requested_end.strftime("%d %b %H:%M"),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, dataset.id)
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(
                        Qt.CheckState.Checked
                        if self.manager.is_enabled(dataset.id)
                        else Qt.CheckState.Unchecked
                    )
                self.sources.setItem(row, column, item)
            if dataset.id == selected:
                self.sources.selectRow(row)
        self.fallback.blockSignals(True)
        self.fallback.setChecked(self.manager.allow_fallback)
        self.fallback.blockSignals(False)
        self.syncing = False
        self.details()

    def toggle(self, item):
        if not self.syncing and item.column() == 0:
            self.save(
                lambda: self.manager.enable(
                    item.data(Qt.ItemDataRole.UserRole),
                    item.checkState() == Qt.CheckState.Checked,
                )
            )

    def set_fallback(self, value):
        self.save(lambda: self.manager.set_fallback(value))

    def action(self, operation):
        identifier = self.selected_id()
        if identifier:
            self.save(lambda: operation(identifier))

    def save(self, operation):
        try:
            operation()
            self.status.clear()
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))
        self.reload_timer.start(0)

    def details(self):
        if self.syncing:
            return
        dataset = self.manager.datasets.get(self.selected_id())
        for button in (self.up, self.down, self.remove):
            button.setEnabled(dataset is not None)
        self.samples.setRowCount(0)
        self.provenance.clear()
        if dataset is None:
            return
        rows = sorted(
            (
                sample.time,
                series.code,
                str(sample.value),
                series.unit,
                sample.qc_flag or "",
                sample.qualifier or "",
            )
            for series in dataset.series
            for sample in series.samples
        )
        self.samples.setRowCount(len(rows))
        for row, (instant, *values) in enumerate(rows):
            for column, text in enumerate(
                (instant.strftime("%Y-%m-%d %H:%M"), *values)
            ):
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.samples.setItem(row, column, item)
        self.samples.resizeColumnsToContents()
        self.sources.resizeColumnToContents(0)
        self.provenance.setPlainText(
            dataset.attribution
            + "\nTerms: "
            + dataset.licence_url
            + "\nDataset SHA-256: "
            + dataset.id
            + "\nPrediction times are UTC. Current directions are degrees true, flow toward."
            + "\nWater-level datum and current depth remain unverified. "
            "QC 2: prediction quality unevaluated. Null values are missing data.\n"
            + json.dumps(dataset.to_json(), indent=2, ensure_ascii=False)
        )

    def dispose(self):
        if self.closed:
            return
        self.closed = True
        self.manager.disconnect("changed", self.changed)
        self.manager.fields.disconnect("changed", self.changed)
        self.hide()
