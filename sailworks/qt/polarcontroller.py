"""Background polar files/catalog with shared manager updates on the UI thread."""

from datetime import datetime, timezone

from PySide6.QtCore import QObject, Signal

from sailworks.core.storage import POLAR_DIR
from sailworks.core.polarfiles import (
    download_orc_file,
    install_polar,
    read_polar,
)
from sailworks.core.orccatalog import (
    load_orc_catalog,
    orc_boat_identity,
    orc_catalog_merged,
    orc_catalog_updated,
    save_orc_catalog,
)
from .backgroundtask import BackgroundTask


class PolarController(QObject):
    changed = Signal()
    preview_changed = Signal(str, object)

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.manager = owner.core.polar_manager
        self.task = BackgroundTask(self)
        self.task.finished.connect(self._finish)
        self.task.changed.connect(self.changed.emit)
        self.manager.connect("polars-list-updated", self._model_changed)
        self.catalog: list[tuple[str, str, str]] = load_orc_catalog()
        self.catalog_updated = orc_catalog_updated()
        self.identities: dict[str, tuple[str, str]] = {}
        self.operation = ""
        self.selected = ""
        self.error = ""
        self._identity = None

    @property
    def blocked(self):
        return (
            self.task.busy
            or self.task.closed
            or self.owner.routing.busy
            or self.owner.data.task.busy
            or self.owner.map_data.task.busy
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
            "Importing polar",
            lambda _: (install_polar(path, POLAR_DIR), None),
        )

    def update_catalog(self):
        def fetch(_):
            rows = orc_catalog_merged()
            return rows, datetime.now(timezone.utc).isoformat(timespec="seconds")

        return self.run("catalog", "Updating ORC catalog", fetch)

    def lookup_boat(self, identifier, callback):
        known = self.identities.get(identifier)
        if known is not None:
            callback(identifier, *known)
            return True
        self._identity = (identifier, callback)
        started = self.run(
            "identity",
            "Reading ORC boat",
            lambda _: orc_boat_identity(identifier),
        )
        if not started:
            self._identity = None
        return started

    def download(self, identifier):
        return self.run(
            "install",
            "Downloading ORC polar",
            lambda _: download_orc_file(identifier, POLAR_DIR),
        )

    def preview(self, name):
        if self.blocked:
            return False
        path = self.manager.get_local_path(name)
        return self.run("preview", "Reading polar", lambda _: (name, read_polar(path)))

    def set_enabled(self, name, enabled):
        if self.blocked:
            return
        if enabled:
            # Validate manually installed files before exposing them to routing.
            path = self.manager.get_local_path(name)
            self.run("enable", "Checking polar", lambda _: (name, read_polar(path)))
        else:
            self.manager.disable(name)

    def remove(self, name):
        if not self.blocked:
            self.manager.delete_polar(name)
            if self.selected == name:
                self.selected = ""
            self.changed.emit()

    def _finish(self, result, error):
        try:
            if error is not None:
                raise error
            if self.operation == "catalog":
                rows, updated = result
                self.catalog, self.catalog_updated = rows, updated
                # The cache is the only place the full listing survives a restart.
                save_orc_catalog(rows, updated)
                self.task.message = f"{len(rows):,} ORC boats available"
            elif self.operation == "install":
                name, metadata = result
                self.manager.register_polar_file(name, metadata)
                self.selected = name
                self.task.message = "Polar imported and enabled: " + name
            elif self.operation == "identity":
                sail, name, boat_type = result
                pending, self._identity = self._identity, None
                if pending is not None:
                    identifier, callback = pending
                    self.identities[identifier] = (name, boat_type)
                    self.task.message = name
                    callback(identifier, name, boat_type)
            elif self.operation in ("preview", "enable"):
                name, polar = result
                if self.operation == "enable":
                    self.manager.enable(name)
                self.selected = name
                if self.operation == "preview":
                    self.preview_changed.emit(name, polar)
                self.task.message = name
        except Exception as exc:
            self._identity = None
            self.error = str(exc) or type(exc).__name__
            self.task.message = self.error

    def close(self):
        if self.task.closed:
            return
        self.task.close()
        self.manager.disconnect("polars-list-updated", self._model_changed)
