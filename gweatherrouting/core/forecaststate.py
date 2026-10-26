"""Application-owned forecast intent and active-analysis authority."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from threading import RLock
from uuid import uuid4

from .forecastrefresh import RefreshCancelledError, RefreshCoordinator
from .forecastrefreshplan import plan_refresh
from .forecasttranche import (
    AnalysisSelection,
    ModelConfiguration,
    shared_requested_region,
)
from .forecastvalidation import inspect_download
from .tranchestore import ResolvedForecast, TrancheStore


class NoActiveForecastError(RuntimeError):
    """No active analysis selection is available."""


class ForecastUnavailableError(RuntimeError):
    """The active or requested model cannot satisfy analysis requirements."""


@dataclass(frozen=True)
class ForecastConfiguration:
    configurations: tuple[ModelConfiguration, ...]
    refresh_model_id: str
    version: int = 1

    def __post_init__(self):
        configurations = tuple(self.configurations)
        if not configurations or any(
            not isinstance(item, ModelConfiguration) for item in configurations
        ):
            raise ValueError("forecast configuration needs model requests")
        model_ids = tuple(item.model_id for item in configurations)
        if len(set(model_ids)) != len(model_ids):
            raise ValueError("duplicate configured model")
        shared_requested_region(configurations)
        if self.refresh_model_id not in model_ids:
            raise ValueError("refresh model must be configured")
        if type(self.version) is not int or self.version != 1:
            raise ValueError("unsupported forecast configuration version")
        object.__setattr__(self, "configurations", configurations)

    @property
    def region(self):
        return shared_requested_region(self.configurations)

    def to_json(self):
        return {
            "version": self.version,
            "configurations": [item.to_json() for item in self.configurations],
            "refresh_model_id": self.refresh_model_id,
        }

    @classmethod
    def from_json(cls, value):
        if not isinstance(value, dict) or type(value.get("version")) is not int:
            raise ValueError("malformed forecast configuration")
        if set(value) != {"version", "configurations", "refresh_model_id"}:
            raise ValueError("forecast configuration has unknown or missing keys")
        if value["version"] != 1:
            raise ValueError("unsupported forecast configuration version")
        expected_configuration_keys = {
            "model_id",
            "fields",
            "region",
            "resolution",
            "forecast_hours",
            "steps",
            "interval",
        }
        if any(
            not isinstance(item, dict) or set(item) != expected_configuration_keys
            for item in value["configurations"]
        ):
            raise ValueError("model configuration has unknown or missing keys")
        return cls(
            tuple(
                ModelConfiguration.from_json(item) for item in value["configurations"]
            ),
            value["refresh_model_id"],
            value["version"],
        )


class ForecastAnalysisState:
    CONFIG_FILENAME = "configuration.json"

    def __init__(
        self,
        root,
        *,
        factories=None,
        planner=plan_refresh,
        validator=inspect_download,
        clock=lambda: datetime.now(timezone.utc),
        id_factory=lambda: uuid4().hex,
    ):
        self.store = TrancheStore(root)
        self.root = self.store.root
        self.configuration_path = self.root / self.CONFIG_FILENAME
        self._lock = RLock()
        self._configuration = None
        self._configuration_error = ""
        self._selection_generation = 0
        self.coordinator = RefreshCoordinator(
            self.store,
            factories=factories,
            planner=planner,
            validator=validator,
            clock=clock,
            id_factory=id_factory,
            state_lock=self._lock,
        )
        self._load_configuration()

    @property
    def configuration(self):
        with self._lock:
            return self._configuration

    @property
    def configuration_error(self):
        with self._lock:
            return self._configuration_error

    def _load_configuration(self):
        if self.configuration_path.is_symlink():
            self._configuration_error = "Saved forecast configuration is a symlink"
            return
        if not self.configuration_path.exists():
            return
        try:
            configuration = ForecastConfiguration.from_json(
                json.loads(self.configuration_path.read_text(encoding="utf8"))
            )
        except Exception as exc:
            self._configuration_error = (
                f"Saved forecast configuration is invalid: {exc}"
            )
            return
        self._configuration = configuration
        self.coordinator.configure(
            configuration.configurations, configuration.refresh_model_id
        )

    def _write_configuration(self, configuration):
        fd, name = tempfile.mkstemp(dir=self.root, prefix=".configuration-")
        committed = False
        try:
            with os.fdopen(fd, "w", encoding="utf8") as stream:
                json.dump(
                    configuration.to_json(),
                    stream,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                stream.flush()
                os.fsync(stream.fileno())
            TrancheStore._fsync_dir(self.root)
            os.replace(name, self.configuration_path)
            committed = True
        finally:
            if not committed and os.path.exists(name):
                os.unlink(name)

    def configure(self, configurations, refresh_model_id):
        configuration = ForecastConfiguration(tuple(configurations), refresh_model_id)
        with self._lock:
            # All validation is complete before disk mutation. os.replace is the
            # persistence commit point; following assignments are non-failing.
            # Retire work captured from the old revision before a failing write
            # can release the lock to its pointer-commit guard.
            self._selection_generation += 1
            self.coordinator.invalidate()
            self._write_configuration(configuration)
            self._configuration = configuration
            self._configuration_error = ""
            self.coordinator.configure(
                configuration.configurations, configuration.refresh_model_id
            )
        return configuration

    def invalidate(self):
        return self.begin_action()

    def begin_action(self):
        """Reserve a monotonically newer action token before queuing work."""
        with self._lock:
            self._selection_generation += 1
            self.coordinator.invalidate()
            return self._selection_generation

    def _check_action(self, action_token, cancel):
        if type(action_token) is not int or action_token != self._selection_generation:
            raise ForecastUnavailableError("Forecast action token is stale")
        if cancel is not None and cancel.is_set():
            raise RefreshCancelledError("Forecast action cancelled")

    @staticmethod
    def _validate_analysis(resolved, required_fields=()):
        entry = resolved.entry
        if entry.member != "deterministic":
            raise ForecastUnavailableError(
                "Active analysis requires a deterministic run"
            )
        if len(entry.artifacts) != 1:
            raise ForecastUnavailableError(
                "Active analysis requires exactly one artifact per model"
            )
        field_times = dict(entry.field_times)
        for field in required_fields:
            if field not in entry.fields or not field_times.get(field):
                raise ForecastUnavailableError(
                    f"Active model {entry.model_id} has no validated {field} coverage"
                )
        return resolved

    def snapshot(self, *, required_fields=()):
        if isinstance(required_fields, (str, bytes)):
            raise ValueError("required_fields must be a collection of names")
        required = tuple(required_fields)
        if any(not isinstance(field, str) or not field for field in required):
            raise ValueError("required fields must be non-empty names")
        if len(set(required)) != len(required):
            raise ValueError("duplicate required field")
        resolved = self.store.active()
        if resolved is None:
            raise NoActiveForecastError("No active forecast selection")
        return self._validate_analysis(resolved, required)

    def _action_guard(self, generation, expected_selection, cancel=None):
        @contextmanager
        def guard():
            with self._lock:
                if generation != self._selection_generation:
                    raise ForecastUnavailableError("Selection action was superseded")
                if self.store.active_selection() != expected_selection:
                    raise ForecastUnavailableError(
                        "Active tranche changed during selection"
                    )
                if cancel is not None and cancel.is_set():
                    raise RefreshCancelledError("Forecast action cancelled")
                yield

        return guard

    def select_model(self, model_id, *, action_token=None, cancel=None):
        if action_token is None:
            action_token = self.begin_action()
        with self._lock:
            self._check_action(action_token, cancel)
            expected = self.store.active_selection()
            if expected is None:
                raise NoActiveForecastError("No active forecast selection")
            generation = action_token
        current = self.store.resolve(expected)
        tranche = current.tranche
        entry = next(
            (item for item in tranche.entries if item.model_id == model_id), None
        )
        if entry is None:
            raise ForecastUnavailableError(
                f"Model {model_id!r} is not present in the active tranche"
            )
        target = AnalysisSelection(tranche.tranche_id, entry.entry_id)
        resolved = self._validate_analysis(self.store.resolve(target), entry.fields)
        return self.store.activate(
            target,
            commit_guard=self._action_guard(generation, expected, cancel),
        )

    def clear_selection(self, *, action_token=None, cancel=None):
        if action_token is None:
            action_token = self.begin_action()
        with self._lock:
            self._check_action(action_token, cancel)
            expected = self.store.active_selection()
            generation = action_token
            if expected is None:
                return None
        self.store.clear_active(
            commit_guard=self._action_guard(generation, expected, cancel)
        )
        return None

    def refresh(self, *, cancel=None, progress=None, action_token=None):
        if action_token is None:
            action_token = self.begin_action()
        with self._lock:
            self._check_action(action_token, cancel)
            configuration = self._configuration
            if configuration is None:
                message = (
                    self._configuration_error or "Forecast configuration is not set"
                )
                raise ForecastUnavailableError(message)
            active = self.store.active_selection()
            configured_ids = {item.model_id for item in configuration.configurations}
            active_model_id = None
            if active is not None:
                tranche = self.store.load(active.tranche_id)
                active_entry = next(
                    entry
                    for entry in tranche.entries
                    if entry.entry_id == active.entry_id
                )
                active_model_id = active_entry.model_id
            preferred = (
                active_model_id
                if active_model_id in configured_ids
                else configuration.refresh_model_id
            )
            capture = self.coordinator._capture_refresh(preferred)
        return self.coordinator._execute_refresh(
            capture,
            cancel=cancel,
            progress=progress,
            action_guard=lambda: self._check_action(action_token, cancel),
        )
