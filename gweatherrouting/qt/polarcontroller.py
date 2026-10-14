"""Background polar files/catalog with shared manager updates on the UI thread."""

from PySide6.QtCore import QObject, Signal

from gweatherrouting.core.storage import POLAR_DIR
from gweatherrouting.core.polarfiles import (
    download_orc_file,
    install_polar,
    orc_catalog,
    read_polar,
)
from .backgroundtask import BackgroundTask


class PolarController(QObject):
    changed = Signal()
    preview_changed = Signal(str, object)
    use_requested = Signal(str)

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.manager = owner.core.polar_manager
        self.task = BackgroundTask(self)
        self.task.finished.connect(self._finish)
        self.task.changed.connect(self.changed.emit)
        self.manager.connect("polars-list-updated", self._model_changed)
        self.catalog: list[tuple[str, str, str]] = []
        self.operation = ""
        self.selected = ""
        self.error = ""

    @property
    def blocked(self):
        return (
            self.task.busy
            or self.task.closed
            or self.owner.routing.busy
            or self.owner.data.task.busy
        )

    def _model_changed(self, _):
        self.changed.emit()

    def run(self, kind, message, operation):
        if self.blocked:
            return False
        self.operation, self.error = kind, ""
        return self.task.start(message, operation)

    def import_file(self, path):
        return self.run(
            "install",
            "Importing polar…",
            lambda _: (install_polar(path, POLAR_DIR), None),
        )

    def fetch_catalog(self):
        return self.run("catalog", "Loading ORC catalog…", lambda _: orc_catalog())

    def download(self, identifier):
        return self.run(
            "install",
            "Downloading ORC polar…",
            lambda _: download_orc_file(identifier, POLAR_DIR),
        )

    def preview(self, name):
        if self.blocked:
            return False
        path = self.manager.get_local_path(name)
        return self.run("preview", "Reading polar…", lambda _: (name, read_polar(path)))

    def set_enabled(self, name, enabled):
        if self.blocked:
            return
        if enabled:
            # Validate manually installed files before exposing them to routing.
            path = self.manager.get_local_path(name)
            self.run("enable", "Checking polar…", lambda _: (name, read_polar(path)))
        else:
            self.manager.disable(name)

    def remove(self, name):
        if not self.blocked:
            self.manager.delete_polar(name)
            if self.selected == name:
                self.selected = ""
            self.changed.emit()

    def use_for_routing(self, name):
        if not self.blocked and self.manager.is_enabled(name):
            self.use_requested.emit(name)

    def _finish(self, result, error):
        try:
            if error is not None:
                raise error
            if self.operation == "catalog":
                self.catalog = result
                self.task.message = f"{len(result):,} ORC boats available"
            elif self.operation == "install":
                name, metadata = result
                self.manager.register_polar_file(name, metadata)
                self.selected = name
                self.task.message = "Polar imported and enabled: " + name
            elif self.operation in ("preview", "enable"):
                name, polar = result
                if self.operation == "enable":
                    self.manager.enable(name)
                self.selected = name
                if self.operation == "preview":
                    self.preview_changed.emit(name, polar)
                self.task.message = name
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
            self.task.message = self.error

    def close(self):
        if self.task.closed:
            return
        self.task.close()
        self.manager.disconnect("polars-list-updated", self._model_changed)
