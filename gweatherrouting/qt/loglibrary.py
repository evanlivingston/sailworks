"""Persistent instrument log library and local-file drop validation."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gweatherrouting.core.loglibrary import LOG_SUFFIXES
from .loginfo import LogInfoDialog


LOG_FILTER = "Instrument logs (*.log *.nmea *.txt *.ebl *.EBL);;All files (*)"


class LogImportProgress(QWidget):
    """GUI-thread view shared by the main window and the library dialog."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.bar = QProgressBar()
        self.bar.setMaximumHeight(16)
        layout.addWidget(self.label)
        layout.addWidget(self.bar)
        controller.log_progress_changed.connect(self.sync)
        self.sync()

    def sync(self):
        progress = self.controller.log_progress
        if progress is None:
            self.label.clear()
            self.bar.setRange(0, 1000)
            self.bar.setValue(0)
            self.setToolTip("")
            self.hide()
            return
        phase = "Copying" if progress["phase"] == "copy" else "Decoding"
        self.label.setText(
            f"{progress['index']}/{progress['count']} · {phase} · "
            f"{Path(progress['path']).name}"
        )
        self.setToolTip(progress["path"])
        done, total = progress["done"], progress["total"]
        if total:
            self.bar.setRange(0, 1000)
            self.bar.setValue(min(1000, round(1000 * done / total)))
            self.bar.setFormat(f"%p% · {done:,} / {total:,} bytes")
        else:
            self.bar.setRange(0, 0)
            self.bar.setFormat("Working…")
        self.show()


def dropped_log_paths(mime):
    """Accept only complete selections of supported local files, never URLs."""
    if not mime.hasUrls() or not mime.urls():
        return []
    paths = []
    for url in mime.urls():
        path = Path(url.toLocalFile())
        if (
            not url.isLocalFile()
            or path.suffix.lower() not in LOG_SUFFIXES
            or not path.is_file()
        ):
            return []
        paths.append(str(path))
    return paths


class LogLibraryDialog(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.library = controller.log_library
        self.setWindowTitle("Log library")
        self.resize(680, 560)
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        self.import_progress = LogImportProgress(controller, self)
        layout.addWidget(self.import_progress)
        self.logs = QTableWidget()
        self.logs.setColumnCount(4)
        self.logs.setHorizontalHeaderLabels(["Name", "Vessel", "Status", "Imported"])
        self.logs.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.logs.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.logs.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.logs.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.logs)
        row = QHBoxLayout()
        self.actions = {}
        for label, callback in (
            ("Import…", self.import_logs),
            ("Save details", self.save),
            ("Open", self.open_log),
            ("File Info…", self.show_file_info),
            ("Remove…", self.remove),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            row.addWidget(button)
            self.actions[label] = button
        layout.addLayout(row)
        self.logs.currentCellChanged.connect(lambda *_: self.selected())
        self.logs.doubleClicked.connect(lambda _: self.open_log())
        controller.log_library_changed.connect(self.refresh)
        controller.changed.connect(self.sync_busy)
        self.refresh()

    def identifier(self):
        item = self.logs.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def refresh(self):
        current = self.identifier()
        self.logs.blockSignals(True)
        self.logs.setRowCount(0)
        for row, entry in enumerate(self.library.entries()):
            self.logs.insertRow(row)
            values = (
                entry["name"],
                entry["vessel"] or "—",
                entry["status"],
                entry["imported"],
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, entry["id"])
                self.logs.setItem(row, column, item)
            if entry["id"] == current:
                self.logs.selectRow(row)
        self.logs.blockSignals(False)
        if self.logs.currentRow() < 0 and self.logs.rowCount():
            self.logs.selectRow(0)
        self.selected()

    def selected(self, *_):
        identifier = self.identifier()
        entry = next((e for e in self.library.entries() if e["id"] == identifier), None)
        self.sync_busy()

    def sync_busy(self):
        busy = self.controller.session.loading
        selected = self.identifier() is not None
        for label, button in self.actions.items():
            button.setEnabled(not busy and (label == "Import…" or selected))

    def save(self):
        identifier = self.identifier()
        if identifier is None:
            return False
        try:
            self.library.update(identifier)
        except OSError as exc:
            QMessageBox.critical(self, "Cannot save log details", str(exc))
            return False
        row = self.logs.currentRow()
        item = self.logs.item(row, 0) if row >= 0 else None
        entry = next(e for e in self.library.entries() if e["id"] == identifier)
        item.setText(
            entry["name"] + (f" — {entry['vessel']}" if entry["vessel"] else "")
        )
        return True

    def import_logs(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Import instrument logs", "", LOG_FILTER
        )
        if paths:
            self.controller.load_paths(paths)

    def open_log(self):
        if self.identifier() is not None and self.save():
            self.controller.load_paths([self.library.path(self.identifier())])

    def show_file_info(self):
        identifier = self.identifier()
        if identifier is None:
            return
        path = self.library.path(identifier)
        current = self.controller.session.log
        if current is not None and Path(current.path).resolve() == path.resolve():
            info = current
        else:
            entry = next(
                item for item in self.library.entries() if item["id"] == identifier
            )
            info = entry.get("file_info")
            if info is None:
                QMessageBox.information(
                    self,
                    "File Info unavailable",
                    "Open this log once to decode its device information.",
                )
                return
            info = {**info, "path": str(path)}
        self.file_info_dialog = LogInfoDialog(info, self)
        self.file_info_dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.file_info_dialog.open()

    def remove(self):
        identifier = self.identifier()
        if identifier is None:
            return
        if (
            QMessageBox.question(
                self,
                "Remove log?",
                "Delete this managed copy and its annotations? "
                "The original source file is not removed.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            self.controller.remove_library_log(identifier)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Cannot remove log", str(exc))

    def dragEnterEvent(self, event):  # noqa: N802
        if not self.controller.session.loading and dropped_log_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):  # noqa: N802
        self.dragEnterEvent(event)

    def dropEvent(self, event):  # noqa: N802
        paths = dropped_log_paths(event.mimeData())
        if paths and self.controller.load_paths(paths):
            event.acceptProposedAction()
        else:
            event.ignore()
