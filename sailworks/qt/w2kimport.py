"""Dialog that syncs W2K-1 EBL logs into the log library."""

import threading

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from sailworks.core.w2kapi import DEFAULT_HOST, W2KClient, W2KError, base_url
from sailworks.core.w2ksync import ACTIVE, NEW, PRESENT, W2KSync, W2KSyncCancelled

from .backgroundtask import BackgroundTask

SERVICE = "org.sailworks.ActisenseW2K"
_SYNC_LOCK = threading.Lock()
STATE_LABEL = {NEW: "New", PRESENT: "Up to date", ACTIVE: "Recording"}


def load_password(host):
    try:
        import keyring

        return keyring.get_password(SERVICE, base_url(host)) or ""
    except Exception:
        return ""


def save_password(host, password):
    try:
        import keyring

        keyring.set_password(SERVICE, base_url(host), password)
        return True
    except Exception:
        return False


def forget_password(host):
    try:
        import keyring

        keyring.delete_password(SERVICE, base_url(host))
    except Exception:
        pass


def _size(value):
    value = int(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


class W2KImportDialog(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.library = controller.log_library
        self.setWindowTitle("Sync from Actisense W2K-1")
        self.resize(620, 520)
        self._mode = None
        self._client = None
        self._sync = None
        self._cancelled = False

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.host = QLineEdit(DEFAULT_HOST)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.remember = QCheckBox("Remember password")
        self.remember.setChecked(True)
        form.addRow("Device address", self.host)
        form.addRow("Admin password", self.password)
        form.addRow("", self.remember)
        layout.addLayout(form)

        row = QHBoxLayout()
        self.test_button = QPushButton("Test connection")
        self.test_button.clicked.connect(self.test)
        self.sync_button = QPushButton("Sync now")
        self.sync_button.clicked.connect(self.sync)
        self.local_button = QPushButton("Use SD copy…")
        self.local_button.clicked.connect(self.use_local_copy)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.setEnabled(False)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        for button in (
            self.test_button,
            self.sync_button,
            self.local_button,
            self.cancel_button,
            self.close_button,
        ):
            row.addWidget(button)
        layout.addLayout(row)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Log", "Size", "State"])
        self.tree.setRootIsDecorated(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tree.header().setStretchLastSection(False)
        self.tree.setColumnWidth(0, 320)
        self.tree.setColumnWidth(1, 90)
        layout.addWidget(self.tree)

        self.status = QLabel("Choose Test connection to inspect the device.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.hide()
        layout.addWidget(self.progress)

        self.task = BackgroundTask(self)
        self.task.changed.connect(self._busy_changed)
        self.task.finished.connect(self._finished)

        self.host.editingFinished.connect(self._load_saved_password)
        self._load_saved_password()

    # -- credentials ------------------------------------------------------

    def _load_saved_password(self):
        if self.password.text():
            return
        saved = load_password(self.host.text())
        if saved:
            self.password.setText(saved)

    def _save_password(self):
        if self.remember.isChecked() and self.password.text():
            save_password(self.host.text(), self.password.text())
        else:
            forget_password(self.host.text())

    # -- tasks ------------------------------------------------------------

    def _build(self):
        try:
            host = base_url(self.host.text())
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        if not self.password.text():
            raise ValueError("Enter the device admin password")
        self._client = W2KClient(host, password=self.password.text())
        self._sync = W2KSync(self._client, self.library)
        return self._client, self._sync

    @staticmethod
    def _locked(operation):
        def run(progress):
            if not _SYNC_LOCK.acquire(blocking=False):
                raise W2KError("A W2K-1 sync is already running")
            try:
                return operation(progress)
            finally:
                _SYNC_LOCK.release()

        return run

    def test(self):
        try:
            client, sync = self._build()
        except ValueError as exc:
            self._error(str(exc))
            return
        self._mode = "test"
        self._cancelled = False
        self.status.setText("Connecting…")
        self.task.start("Connecting", self._locked(lambda progress: sync.scan()))

    def sync(self):
        try:
            client, sync = self._build()
        except ValueError as exc:
            self._error(str(exc))
            return
        self._mode = "sync"
        self._cancelled = False
        self.status.setText("Syncing…")

        def operation(progress):
            return sync.run(progress, stop=lambda: self._cancelled or self.task.closed)

        self.task.start("Syncing", self._locked(operation))

    def use_local_copy(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose the copied W2K-1 log folder"
        )
        if not folder:
            return
        try:
            client, sync = self._build()
        except ValueError as exc:
            self._error(str(exc))
            return
        self._mode = "sync"
        self._cancelled = False
        self.status.setText("Reading local copy…")

        def operation(progress):
            sync.seed(folder)
            return sync.run(progress, stop=lambda: self._cancelled or self.task.closed)

        self.task.start("Reading local copy", self._locked(operation))

    def _busy_changed(self):
        busy = self.task.busy
        for button in (self.test_button, self.sync_button, self.local_button):
            button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        self.host.setEnabled(not busy)
        self.password.setEnabled(not busy)
        self.remember.setEnabled(not busy)
        if busy:
            self.progress.show()
            if self.task.percent >= 0:
                self.progress.setValue(self.task.percent)
            else:
                self.progress.setRange(0, 0)
        else:
            self.progress.hide()
            self.progress.setRange(0, 100)

    def cancel(self):
        if not self.task.busy:
            return
        self._cancelled = True
        self.status.setText("Cancelling…")
        self.cancel_button.setEnabled(False)

    def _finished(self, result, error):
        if isinstance(error, W2KSyncCancelled):
            self.status.setText("Cancelled")
            return
        if error is not None:
            self._error(str(error))
            self.status.setText("Failed")
            return
        if self._mode == "test":
            self._populate(result)
            counts = result.counts()
            self.status.setText(
                f"Device {result.serial or 'W2K-1'}: "
                f"{counts[NEW]} new, {counts[PRESENT]} up to date, "
                f"{counts[ACTIVE]} recording."
            )
        elif result is not None:
            self.status.setText(result.describe())
            self.controller.log_library_changed.emit()
            self._mode = "test"
            self.task.start("Refreshing", lambda progress: self._sync.scan())
        self._save_password()

    def _populate(self, scan):
        self.tree.clear()
        folders = {}
        for entry in scan.entries:
            parent = folders.get(entry.folder)
            if parent is None:
                parent = QTreeWidgetItem([entry.folder, "", ""])
                self.tree.addTopLevelItem(parent)
                folders[entry.folder] = parent
            item = QTreeWidgetItem(
                [entry.file.name, _size(entry.file.size), STATE_LABEL[entry.state]]
            )
            parent.addChild(item)
        for parent in folders.values():
            parent.setExpanded(True)

    def _error(self, message):
        box = QMessageBox(self)
        box.setWindowTitle("W2K-1 sync")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(message.splitlines()[0] if message else "Unknown error")
        box.setDetailedText(message)
        box.exec()

    def reject(self):
        self._cancelled = True
        self.task.close(wait=False)
        super().reject()
