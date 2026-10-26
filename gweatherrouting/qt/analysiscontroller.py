"""Qt-thread boundary for immutable published forecast-analysis snapshots."""

from threading import Event, Thread

from PySide6.QtCore import QObject, Signal, Slot

from gweatherrouting.core.forecaststate import NoActiveForecastError
from gweatherrouting.core.forecastrefresh import RefreshCancelledError
from gweatherrouting.core.forecasttranche import ModelConfiguration


class AnalysisController(QObject):
    changed = Signal()
    _finished = Signal(int, str, object, object)
    _progressed = Signal(int, object, object)

    def __init__(self, root=None, *, state=None, parent=None, thread_factory=Thread):
        super().__init__(parent)
        if (root is None) == (state is None):
            raise ValueError("provide exactly one analysis root or state")
        if state is None:
            from gweatherrouting.core.forecaststate import ForecastAnalysisState

            state = ForecastAnalysisState(root)
        self.state = state
        self._thread_factory = thread_factory
        self._active = None
        self._closed = False
        self._generation = 0
        self._cancel = None
        self._thread = None
        self._progress_token = None
        self.busy = False
        self.loading = True
        self.progress = 0
        self.error = ""
        self.status = "Loading published forecast…"
        self._finished.connect(self._finish)
        self._progressed.connect(self._progress)
        self._start(
            "startup",
            lambda event, report, token: self.state.snapshot(),
            preserve=True,
        )

    @property
    def active(self):
        return self._active

    @property
    def configuration(self):
        return self.state.configuration

    @property
    def configuration_error(self):
        return getattr(self.state, "configuration_error", "")

    @property
    def operation_id(self):
        """Read-only generation for UI correlation of async operations."""
        return self._generation

    def snapshot(self, required_fields=()):
        active = self._active
        if active is None:
            raise RuntimeError(
                self.error or self.status or "No published forecast is active"
            )
        if isinstance(required_fields, (str, bytes)):
            raise ValueError("required_fields must be a collection of field names")
        required_fields = tuple(required_fields)
        if any(not isinstance(field, str) or not field for field in required_fields):
            raise ValueError("required fields must be non-empty names")
        if len(set(required_fields)) != len(required_fields):
            raise ValueError("duplicate required field")
        required = set(required_fields)
        if not required <= set(active.entry.fields):
            raise ValueError("published forecast does not contain all required fields")
        if required:
            field_times = dict(getattr(active.entry, "field_times", ()))
            if any(not field_times.get(field) for field in required):
                raise ValueError(
                    "published forecast has unknown required field coverage"
                )
        return active

    def _start(
        self,
        action,
        operation,
        *,
        preserve=False,
        reserve_action=False,
    ):
        if self._closed:
            return False
        if self._cancel is not None:
            self._cancel.set()
        event = Event()
        self._cancel = event
        self._generation += 1
        generation = self._generation
        action_token = self.state.begin_action() if reserve_action else None
        self._progress_token = action_token
        if not preserve:
            self._active = None
        self.busy = True
        self.loading = True
        self.progress = 0
        self.error = ""
        self.status = (
            "Loading published forecast…"
            if action == "startup"
            else "Updating forecast…"
        )
        self.changed.emit()

        def worker():
            result, error = None, None
            reconciliation_error = None
            try:
                if event.is_set():
                    raise RefreshCancelledError("Forecast analysis operation cancelled")
                result = operation(
                    event,
                    lambda *value: self._progressed.emit(
                        generation, action_token, value
                    ),
                    action_token,
                )
            except Exception as exc:
                error = exc
                try:
                    result = self.state.snapshot()
                except NoActiveForecastError:
                    result = None
                except Exception as snapshot_error:
                    result = None
                    reconciliation_error = snapshot_error
            if not self._closed:
                self._finished.emit(
                    generation, action, result, (error, reconciliation_error)
                )

        try:
            self._thread = self._thread_factory(
                target=worker, name="qt-analysis", daemon=True
            )
            self._thread.start()
        except Exception as exc:
            self.busy = self.loading = False
            self.error = str(exc) or type(exc).__name__
            self._active = None
            self.status = (
                "Forecast unavailable"
                if self._active is None
                else "Using previous published forecast"
            )
            self.changed.emit()
            return False
        return True

    @Slot(int, object, object)
    def _progress(self, generation, action_token, value):
        if (
            self._closed
            or generation != self._generation
            or action_token != self._progress_token
        ):
            return
        if (
            isinstance(value, tuple)
            and len(value) == 2
            and isinstance(value[0], str)
            and isinstance(value[1], int)
        ):
            model_id, percent = value
            self.progress = min(100, max(0, percent))
            self.status = f"Downloading {model_id}…"
        else:
            self.status = str(value)
        self.changed.emit()

    @Slot(int, str, object, object)
    def _finish(self, generation, action, result, details):
        if self._closed or generation != self._generation:
            return
        error, reconciliation_error = details
        self.busy = self.loading = False
        self.progress = 100 if error is None else self.progress
        if error is not None:
            self.error = str(error) or type(error).__name__
            if reconciliation_error is not None:
                self.error += "; current published forecast could not be resolved: " + (
                    str(reconciliation_error) or type(reconciliation_error).__name__
                )
            self._active = result
            self.status = (
                "Forecast unavailable"
                if self._active is None
                else "Using currently published forecast"
            )
        else:
            self._active = result
            self.error = getattr(self.state, "configuration_error", "")
            self.status = (
                "Forecast ready"
                if result is not None
                else "No published forecast selected"
            )
            if self.error:
                self.status += "; saved refresh configuration is invalid"
        self.changed.emit()

    def configure(self, configurations, refresh_model_id):
        if self._closed:
            raise RuntimeError("analysis controller is closed")
        configs = tuple(configurations)
        if not configs or any(
            not isinstance(item, ModelConfiguration) for item in configs
        ):
            raise ValueError("configuration requires model configurations")
        try:
            self.state.configure(configs, refresh_model_id)
        except Exception as exc:
            self.report_error(exc)
            raise
        self._progress_token = None
        self.error = ""
        if not self.busy:
            self.status = "Refresh configured; published forecast unchanged"
        else:
            self.status = "Refresh configuration saved; operation finishing"
        self.changed.emit()

    def refresh(self):
        return self._start(
            "refresh",
            lambda event, report, token: self.state.refresh(
                cancel=event, progress=report, action_token=token
            ),
            preserve=True,
            reserve_action=True,
        )

    def select_model(self, model_id):
        def select(event, report, token):
            return self.state.select_model(model_id, action_token=token, cancel=event)

        return self._start("select", select, reserve_action=True)

    def clear_selection(self):
        def clear(event, report, token):
            return self.state.clear_selection(action_token=token, cancel=event)

        return self._start("clear", clear, reserve_action=True)

    def cancel(self):
        if self._cancel is not None:
            self._cancel.set()
        self.state.invalidate()
        self._generation += 1
        self._progress_token = None
        self.busy = self.loading = False
        if not self._closed:
            self.error = ""
            self.status = (
                "Forecast ready"
                if self._active is not None
                else "No published forecast selected"
            )
            self.changed.emit()

    def report_error(self, error):
        self.error = str(error)
        self.status = (
            "Forecast unavailable"
            if self._active is None
            else "Using previous published forecast"
        )
        self.changed.emit()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        if self._cancel is not None:
            self._cancel.set()
        self.state.invalidate()
