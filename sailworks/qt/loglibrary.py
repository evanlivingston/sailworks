"""Persistent instrument log library and local-file drop validation."""

from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sailworks.core.loglibrary import LOG_SUFFIXES
from .loginfo import LogInfoDialog
from .theme import MenuBuilder, ThemedMenu

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
        self.logs.setColumnCount(5)
        self.logs.setHorizontalHeaderLabels(
            ["Name", "Vessel", "Status", "Time span", "Duration"]
        )
        self.logs.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.logs.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.logs.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.logs.horizontalHeader().setStretchLastSection(True)
        self.logs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.logs.customContextMenuRequested.connect(self._context_menu)
        self.logs.installEventFilter(self)
        layout.addWidget(self.logs)
        row = QHBoxLayout()
        self.actions = {}
        for label, callback in (
            ("Import…", self.import_logs),
            ("Import folder…", self.import_folder),
            ("Sync W2K-1…", self.sync_w2k),
            ("Save details", self.save),
            ("Open", self.open_log),
            ("File Info…", self.show_file_info),
            ("Delete…", self.delete_selected),
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

    def selected_identifiers(self):
        identifiers = []
        for item in self.logs.selectedItems():
            if item.column() != 0:
                continue
            value = item.data(Qt.ItemDataRole.UserRole)
            if value is not None and value not in identifiers:
                identifiers.append(value)
        if not identifiers:
            current = self.logs.currentItem()
            if current is not None:
                value = current.data(Qt.ItemDataRole.UserRole)
                if value is not None:
                    identifiers.append(value)
        return identifiers

    def identifier(self):
        identifiers = self.selected_identifiers()
        return identifiers[0] if identifiers else None

    def refresh(self):
        current_ids = self.selected_identifiers()
        self.logs.blockSignals(True)
        self.logs.setRowCount(0)
        for row, entry in enumerate(self.library.entries()):
            self.logs.insertRow(row)
            info = entry.get("file_info") or {}
            span, duration = self._time_span_and_duration(info)
            values = (
                entry["name"],
                entry["vessel"] or "—",
                entry["status"],
                span,
                duration,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, entry["id"])
                self.logs.setItem(row, column, item)
            if entry["id"] in current_ids:
                self.logs.selectRow(row)
        self.logs.blockSignals(False)
        if not self.selected_identifiers() and self.logs.rowCount():
            self.logs.selectRow(0)
        self.selected()

    @staticmethod
    def _format_duration(seconds):
        if seconds is None:
            return "—"
        try:
            total = int(float(seconds))
        except (TypeError, ValueError):
            return "—"
        if total < 0:
            total = 0
        days, remainder = divmod(total, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _seconds = divmod(remainder, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours or days:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)

    @staticmethod
    def _time_span_and_duration(info):
        origin = info.get("timestamp_origin")
        duration = info.get("duration")
        if not origin:
            return "—", LogLibraryDialog._format_duration(duration)
        try:
            start = datetime.fromisoformat(str(origin))
        except ValueError:
            return "—", LogLibraryDialog._format_duration(duration)
        try:
            end = (
                start + timedelta(seconds=float(duration))
                if duration is not None
                else start
            )
        except (TypeError, ValueError):
            end = start
        span = f"{start:%Y-%m-%d %H:%M} – {end:%Y-%m-%d %H:%M}"
        return span, LogLibraryDialog._format_duration(duration)

    def selected(self, *_):
        self.sync_busy()

    def sync_busy(self):
        busy = self.controller.session.loading
        selected = bool(self.selected_identifiers())
        always = {"Import…", "Import folder…", "Sync W2K-1…"}
        for label, button in self.actions.items():
            button.setEnabled(not busy and (label in always or selected))

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

    def import_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Import a folder of instrument logs"
        )
        if not folder:
            return
        root = Path(folder)
        paths = sorted(
            str(path)
            for path in root.rglob("*")
            if path.suffix.lower() in LOG_SUFFIXES and path.is_file()
        )
        if not paths:
            QMessageBox.information(
                self, "Import folder", "No instrument logs were found in that folder."
            )
            return
        self.controller.load_paths(paths)

    def sync_w2k(self):
        from .w2kimport import W2KImportDialog

        W2KImportDialog(self.controller, self).exec()

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

    def delete_selected(self):
        identifiers = self.selected_identifiers()
        if not identifiers:
            return
        if len(identifiers) == 1:
            question = "Delete this managed copy and its annotations?"
        else:
            question = (
                f"Delete {len(identifiers)} managed copies and their annotations?"
            )
        if (
            QMessageBox.question(
                self,
                "Delete log?",
                question + " The original source files are not removed.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        errors = []
        for identifier in identifiers:
            try:
                self.controller.remove_library_log(identifier)
            except (OSError, ValueError) as exc:
                errors.append(str(exc))
        if errors:
            QMessageBox.critical(self, "Cannot delete log", "\n".join(errors))

    def _context_menu(self, position):
        if not self.selected_identifiers():
            return
        menu = ThemedMenu(self)
        builder = MenuBuilder(menu)
        builder.add("Delete", self.delete_selected, key="D")
        menu.exec(self.logs.viewport().mapToGlobal(position))

    def eventFilter(self, watched, event):
        if watched is self.logs and event.type() == event.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                self.delete_selected()
                return True
        return super().eventFilter(watched, event)

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
