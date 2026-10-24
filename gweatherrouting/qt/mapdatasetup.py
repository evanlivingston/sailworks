"""Qt map-data setup, with worker installation and UI-thread chart attachment."""

from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QHBoxLayout,
)

from gweatherrouting.core.mapdata import install_map_data
from gweatherrouting.core.storage import DATA_DIR
from .backgroundtask import BackgroundTask


class MapDataController(QObject):
    changed = Signal()
    _reported = Signal(str, object)

    def __init__(self, owner, directory=DATA_DIR):
        super().__init__(owner)
        self.owner = owner
        self.directory = Path(directory)
        self.task = BackgroundTask(self)
        self.task.finished.connect(self._finish)
        self.task.changed.connect(self.changed.emit)
        self._reported.connect(self._report)
        self.cancel_event = Event()
        self.message = ""

    def missing(self):
        charts = self.owner.charts
        return dict(
            coastlines=charts.gshhsLayer is None, seamarks=charts.osmLayer is None
        )

    def start(self, kinds):
        if self.task.busy or self.task.closed:
            return False
        if (
            self.owner.routing.busy
            or self.owner.data.task.busy
            or self.owner.polars.task.busy
        ):
            self.message = (
                "Wait for the current routing or library operation to finish."
            )
            self.changed.emit()
            return False
        missing = self.missing()
        kinds = tuple(kind for kind in kinds if missing.get(kind, False))
        if not kinds:
            self.message = "Select missing map data to download."
            self.changed.emit()
            return False
        self.cancel_event = Event()
        cancel = self.cancel_event
        self.message = "Preparing download…"
        return self.task.start(
            self.message,
            lambda _: install_map_data(
                kinds, self.directory, cancel, self._reported.emit
            ),
        )

    @Slot(str, object)
    def _report(self, message, fraction):
        if self.task.closed or not self.task.busy or self.cancel_event.is_set():
            return
        self.message = message
        self.task.percent = -1 if fraction is None else round(fraction * 100)
        self.changed.emit()

    def _finish(self, result, error):
        installed, failure = result if result is not None else ((), error)
        try:
            if installed:
                self.owner.charts.reload_base_chart()
                for canvas in self.owner.charts.maps:
                    if "coastlines" in installed:
                        canvas.marine.forget(canvas)
                self.owner.data.redraw()
            self.message = str(failure) if failure else "Map data installed and ready"
            if installed and failure:
                self.message += "; completed datasets are available."
        except Exception as exc:
            self.message = "Data saved; unable to load map: " + str(exc)
        self.changed.emit()

    def cancel(self):
        if self.task.busy:
            self.cancel_event.set()
            self.message = "Cancelling…"
            self.changed.emit()

    def close(self):
        self.cancel_event.set()
        self.task.close()


class MapDataSetup(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        layout = QVBoxLayout(self)
        self.downloads = {}
        for kind, label in (
            ("coastlines", "World Coastlines"),
            ("seamarks", "OpenSeaMap"),
        ):
            button = QPushButton("Download " + label)
            button.clicked.connect(lambda _, kind=kind: controller.start((kind,)))
            layout.addWidget(button)
            self.downloads[kind] = button
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        row = QHBoxLayout()
        self.cancel = QPushButton("Cancel download")
        self.cancel.clicked.connect(controller.cancel)
        row.addWidget(self.cancel)
        layout.addLayout(row)
        layout.addStretch()
        controller.changed.connect(self.refresh)
        controller.owner.data.task.changed.connect(self.refresh)
        controller.owner.routing.changed.connect(self.refresh)
        controller.owner.polars.task.changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        controller = self.controller
        missing = controller.missing()
        busy = controller.task.busy
        blocked = (
            busy
            or controller.owner.data.task.busy
            or controller.owner.routing.busy
            or controller.owner.polars.task.busy
        )
        for kind, button in self.downloads.items():
            label = "World Coastlines" if kind == "coastlines" else "OpenSeaMap"
            button.setText(("Download " if missing[kind] else "Installed: ") + label)
            button.setEnabled(
                missing[kind] and not blocked and not controller.task.closed
            )
        self.cancel.setEnabled(busy and not controller.cancel_event.is_set())
        self.progress.setVisible(busy)
        self.progress.setRange(0, 0 if controller.task.percent < 0 else 100)
        self.progress.setValue(max(0, controller.task.percent))
        self.status.setText(controller.message)
        self.status.setVisible(bool(controller.message))
        self.cancel.setVisible(busy)
