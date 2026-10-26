"""Display-source adapter bound only to an analysis controller snapshot."""

from dataclasses import dataclass
from datetime import timezone
from threading import Thread

from PySide6.QtCore import QObject, Signal, Slot

from gweatherrouting.rendering.displayforecast import inspect_forecasts


@dataclass(frozen=True)
class BoundDisplayForecast:
    metadata: object
    tranche_id: str
    entry_id: str
    artifact_sha256: str

    @property
    def key(self):
        return f"{self.tranche_id}:{self.entry_id}:{self.artifact_sha256}"

    @property
    def analysis_identity(self):
        return self.tranche_id, self.entry_id, self.artifact_sha256

    @property
    def display_identity(self):
        return self.metadata.display_identity

    @property
    def label(self):
        return f"{self.metadata.label} · {self.entry_id}"

    def __getattr__(self, name):
        return getattr(self.metadata, name)


class BoundDisplayForecastSelection(QObject):
    changed = Signal()
    _scanned = Signal(int, object, object)

    def __init__(self, analysis, parent=None):
        super().__init__(parent)
        self.analysis = analysis
        self.choices = ()
        self._sources = ()
        self._selected_key = ""
        self.loading = False
        self.error = ""
        self.status = "No published forecast selected"
        self._closed = False
        self._generation = 0
        self._binding = None
        self._scan_pending = False
        self._scanned.connect(self._finish_scan)
        analysis.changed.connect(self._analysis_changed)
        self._analysis_changed()

    @property
    def selected_key(self):
        return self._selected_key

    def sources(self):
        return self._sources

    def _binding_for(self, resolved):
        if resolved is None:
            return None
        entry = resolved.entry
        if len(entry.artifacts) != 1:
            return (resolved.tranche.tranche_id, entry.entry_id, "unsupported")
        return (
            resolved.tranche.tranche_id,
            entry.entry_id,
            entry.artifacts[0].sha256,
        )

    @Slot()
    def _analysis_changed(self):
        if self._closed:
            return
        resolved = self.analysis.active
        binding = self._binding_for(resolved)
        self.loading = self.analysis.loading or self._scan_pending
        self.error = self.analysis.error
        self.status = self.analysis.status
        if binding == self._binding:
            self.changed.emit()
            return
        self._generation += 1
        generation = self._generation
        self._binding = binding
        self.choices = ()
        self._sources = ()
        self._selected_key = ""
        self._scan_pending = False
        if resolved is None:
            self.loading = self.analysis.loading
            self.status = self.analysis.status
            self.changed.emit()
            return
        tranche = resolved.tranche
        store = self.analysis.state.store
        items = []
        errors = []
        for entry in tranche.entries:
            if len(entry.artifacts) != 1:
                errors.append(f"{entry.model_id}: multiple artifacts are unsupported")
                continue
            artifact = entry.artifacts[0]
            path = store.root / artifact.path
            items.append((entry, path, artifact))
        self.loading = True
        self._scan_pending = True
        self.status = "Inspecting published forecast…"
        self.changed.emit()

        def scan():
            results, scan_errors = [], list(errors)
            for entry, path, artifact in items:
                try:
                    forecasts, error = inspect_forecasts(
                        ((str(path), (artifact.sha256, artifact.size)),)
                    )
                    scan_errors.extend(error.split("; ") if error else ())
                    matches = [
                        forecast
                        for forecast in forecasts
                        if forecast.model_name == entry.model_id
                        and int(forecast.start_time.timestamp())
                        == int(entry.run_time.timestamp())
                        and not forecast.display_identity[1][2]
                        and int(forecast.first_valid)
                        == int(min(entry.time_steps).timestamp())
                        and int(forecast.last_valid)
                        == int(max(entry.time_steps).timestamp())
                    ]
                    if len(matches) != 1:
                        scan_errors.append(
                            f"{entry.model_id}: no unique matching deterministic run"
                        )
                        continue
                    results.append(
                        (
                            entry.entry_id,
                            BoundDisplayForecast(
                                matches[0],
                                tranche.tranche_id,
                                entry.entry_id,
                                artifact.sha256,
                            ),
                        )
                    )
                except Exception as exc:
                    scan_errors.append(f"{entry.model_id}: {exc}")
            if not self._closed:
                self._scanned.emit(
                    generation, (tuple(results), tuple(scan_errors)), binding
                )

        Thread(target=scan, name="qt-analysis-display", daemon=True).start()

    @Slot(int, object, object)
    def _finish_scan(self, generation, result, binding):
        if self._closed or generation != self._generation or binding != self._binding:
            return
        entries, errors = result
        self._scan_pending = False
        self.choices = tuple(source for _, source in entries)
        active = self.analysis.active
        if active is not None and self._binding_for(active) == binding:
            self._sources = tuple(
                source
                for entry_id, source in entries
                if entry_id == active.entry.entry_id
            )
            self._selected_key = self._sources[0].key if self._sources else ""
        self.loading = False
        self.error = "; ".join(errors) or self.analysis.error
        self.status = self.error or (
            "Forecast ready" if self._sources else "Weather unavailable"
        )
        self.changed.emit()

    def choose(self, key):
        if not key:
            return self.analysis.clear_selection()
        choice = next((item for item in self.choices if item.key == key), None)
        if choice is None:
            self.error = "Selected forecast is no longer available"
            self.changed.emit()
            return False
        return self.analysis.select_model(choice.model_name)

    def refresh(self):
        if self._closed:
            return False
        if self.analysis.active is None:
            self._analysis_changed()
            return False
        # This is a metadata rescan of the immutable active tranche, not a
        # request to download or publish another forecast.
        self._binding = object()
        self._analysis_changed()
        return True

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        try:
            self.analysis.changed.disconnect(self._analysis_changed)
        except (RuntimeError, TypeError):
            pass
