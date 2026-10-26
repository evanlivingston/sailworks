"""Coordinated, immutable forecast refresh execution."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path
import logging
from threading import RLock
from uuid import uuid4

from .forecastrefreshplan import (
    RefreshCancelledError,
    create_source,
    plan_refresh,
)
from .forecasttranche import (
    AnalysisSelection,
    ForecastTranche,
    ModelConfiguration,
    ModelEntry,
)
from .forecastvalidation import inspect_download

logger = logging.getLogger(__name__)


class RefreshExecutionError(RuntimeError):
    """A model could not be downloaded, validated, or published."""


class RefreshSupersededError(RuntimeError):
    """A newer refresh or configuration retired this refresh ticket."""


def _utc(value, label):
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


class _CombinedCancel:
    def __init__(self, coordinator, revision, ticket, external):
        self.coordinator = coordinator
        self.revision = revision
        self.ticket = ticket
        self.external = external

    def is_set(self):
        with self.coordinator._lock:
            stale = (
                self.revision != self.coordinator._revision
                or self.ticket != self.coordinator._ticket
            )
        return stale or (self.external is not None and self.external.is_set())


class RefreshCoordinator:
    def __init__(
        self,
        store,
        *,
        factories=None,
        planner=plan_refresh,
        validator=inspect_download,
        clock=lambda: datetime.now(timezone.utc),
        id_factory=lambda: uuid4().hex,
        state_lock=None,
    ):
        self.store = store
        self.factories = None if factories is None else dict(factories)
        self.planner = planner
        self.validator = validator
        self.clock = clock
        self.id_factory = id_factory
        self._lock = state_lock if state_lock is not None else RLock()
        self._revision = 0
        self._ticket = 0
        self._configurations = ()
        self._selected_model_id = None

    def configure(self, configurations, selected_model_id):
        configs = tuple(configurations)
        if not configs or any(not isinstance(c, ModelConfiguration) for c in configs):
            raise ValueError("configure requires non-empty ModelConfiguration values")
        ids = tuple(c.model_id for c in configs)
        if len(set(ids)) != len(ids) or selected_model_id not in ids:
            raise ValueError("models must be unique and selection must be a member")
        with self._lock:
            self._configurations = configs
            self._selected_model_id = selected_model_id
            self._revision += 1
            self._ticket += 1
            return self._revision

    def invalidate(self):
        with self._lock:
            self._revision += 1
            self._ticket += 1
            return self._revision

    def estimate_download(self, configurations, selected_model_id, *, cancel=None):
        """Estimate transfer bytes for latest compatible runs, excluding local hits."""
        configurations = tuple(configurations)
        plan = self.planner(
            configurations,
            selected_model_id,
            factories=self.factories,
            cancel=cancel,
        )
        total_bytes = 0
        estimated_models, reusable_models = [], []
        for planned in plan.models:
            if cancel is not None and cancel.is_set():
                raise RefreshCancelledError("Forecast size estimate cancelled")
            if self.store.reusable_entry(planned) is not None:
                reusable_models.append(planned.model_id)
                continue
            source = create_source(planned, self.factories)
            estimate = getattr(source, "estimate_download_size", None)
            if callable(estimate):
                size, approximate = estimate(planned.identifier, cancel=cancel)
            else:
                size, approximate = self._estimate_legacy_size(planned)
            if type(size) is not int or size <= 0:
                raise ValueError(f"{planned.model_id}: download size unavailable")
            total_bytes += size
            estimated_models.append((planned.model_id, size, approximate))
        return total_bytes, tuple(estimated_models), tuple(reusable_models)

    @staticmethod
    def _estimate_legacy_size(planned):
        from .gribsources.eccchrdps import ECCHRDPSource
        from .gribsources.noaagfs import NOAAGFSSource
        from .gribsources.noaahrrr import NOAAHRRRSource

        config = planned.configuration
        selected_steps = tuple(
            sorted({hour for _field, hours in planned.field_steps for hour in hours})
        )
        if planned.model_id == "HRDPS":
            text = ECCHRDPSource._estimate_custom_size(selected_steps, config.fields)
        else:
            source_class = {
                "GFS": NOAAGFSSource,
                "HRRR": NOAAHRRRSource,
            }.get(planned.model_id)
            if source_class is None:
                raise ValueError(f"{planned.model_id}: no size estimator available")
            source = source_class()
            source.bounds = config.region
            text = source._estimate_custom_size(selected_steps, config.fields)
        value, unit = text.replace("~", "").split()
        multiplier = {"KB": 1024, "MB": 1024**2, "GB": 1024**3}.get(unit.upper())
        if multiplier is None:
            raise ValueError("unrecognized provider size estimate")
        return max(1, round(float(value) * multiplier)), True

    def _is_current(self, revision, ticket):
        with self._lock:
            return revision == self._revision and ticket == self._ticket

    def _raise_if_stale_or_cancelled(self, revision, ticket, cancel):
        if not self._is_current(revision, ticket):
            raise RefreshSupersededError("Forecast refresh was superseded")
        if cancel is not None and cancel.is_set():
            raise RefreshCancelledError("Forecast refresh cancelled")

    def _commit_guard(self, revision, ticket, cancel, action_guard=None):
        @contextmanager
        def guard():
            with self._lock:
                if revision != self._revision or ticket != self._ticket:
                    raise RefreshSupersededError("Forecast refresh was superseded")
                if cancel is not None and cancel.is_set():
                    raise RefreshCancelledError("Forecast refresh cancelled")
                if action_guard is not None:
                    action_guard()
                yield

        return guard

    def _progress(self, callback, model_id, value):
        if callback is not None:
            callback(model_id, min(99, max(0, int(value))))

    def _download_one(self, planned, stage, cancel, progress):
        model_stage = stage / planned.model_id
        model_stage.mkdir()
        source = create_source(planned, self.factories)
        result = source.download(
            planned.identifier,
            str(model_stage),
            lambda value: self._progress(progress, planned.model_id, value),
            cancel=cancel,
        )
        path = Path(result)
        if path.is_symlink():
            raise ValueError("provider returned a symlinked path")
        stage_root = stage.resolve()
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(stage_root)
        except ValueError as exc:
            raise ValueError(
                "provider returned a path outside its owned stage"
            ) from exc
        if not resolved.is_file():
            raise ValueError("provider did not return a regular file")
        return resolved

    @staticmethod
    def _validated_entry(planned, metadata, artifact, acquired_at):
        if metadata.model_id != planned.model_id:
            raise ValueError("validator returned a different model")
        if _utc(metadata.run_time, "validated run time") != planned.run_time:
            raise ValueError("validator returned a different model run")
        if metadata.member != planned.member:
            raise ValueError("validator returned a different ensemble member")
        if tuple(metadata.fields) != planned.configuration.fields:
            raise ValueError("validator fields do not match the plan")
        actual_field_times = tuple(
            (field, tuple(times)) for field, times in metadata.field_times
        )
        expected_field_times = tuple(
            (
                field,
                tuple(planned.run_time + timedelta(hours=hour) for hour in hours),
            )
            for field, hours in planned.field_steps
        )
        if actual_field_times != expected_field_times:
            raise ValueError(
                "validator field coverage does not match the pinned schedule"
            )
        expected_times = tuple(
            sorted({time for _, times in expected_field_times for time in times})
        )
        if tuple(metadata.time_steps) != expected_times:
            raise ValueError("validator time coverage does not match field coverage")
        if not metadata.levels:
            raise ValueError("validator returned no actual levels")
        return ModelEntry(
            planned.model_id,
            planned.model_id,
            planned.run_time,
            planned.member,
            tuple(metadata.fields),
            tuple(metadata.levels),
            tuple(metadata.region),
            metadata.resolution,
            tuple(metadata.time_steps),
            (artifact,),
            acquired_at,
            actual_field_times,
        )

    def _capture_refresh(self, selected_model_id=None):
        with self._lock:
            if not self._configurations:
                raise RefreshExecutionError("refresh configuration is not set")
            configurations = self._configurations
            selected = (
                self._selected_model_id
                if selected_model_id is None
                else selected_model_id
            )
            if selected not in {
                configuration.model_id for configuration in configurations
            }:
                raise ValueError("selected refresh model is not configured")
            revision = self._revision
            self._ticket += 1
            ticket = self._ticket
            return configurations, selected, revision, ticket

    def refresh(self, *, cancel=None, progress=None):
        capture = self._capture_refresh()
        return self._execute_refresh(capture, cancel=cancel, progress=progress)

    def _execute_refresh(
        self, capture, *, cancel=None, progress=None, action_guard=None
    ):
        configurations, selected_model_id, revision, ticket = capture
        combined = _CombinedCancel(self, revision, ticket, cancel)
        self._raise_if_stale_or_cancelled(revision, ticket, cancel)
        try:
            plan = self.planner(
                configurations,
                selected_model_id,
                factories=self.factories,
                cancel=combined,
            )
        except RefreshCancelledError:
            self._raise_if_stale_or_cancelled(revision, ticket, cancel)
            raise
        except Exception as exc:
            self._raise_if_stale_or_cancelled(revision, ticket, cancel)
            logger.exception("Forecast refresh planning failed")
            raise RefreshExecutionError(f"planning failed: {exc}") from exc
        if (
            plan.configurations != configurations
            or plan.selected_model_id != selected_model_id
        ):
            raise RefreshExecutionError(
                "planner returned a plan for different configuration"
            )
        logger.info(
            "Forecast refresh planned models=%s selected=%s",
            ",".join(item.model_id for item in plan.models),
            selected_model_id,
        )

        entries = []
        try:
            with tempfile.TemporaryDirectory(
                dir=self.store.root, prefix=".refresh-"
            ) as tmp:
                stage = Path(tmp)
                for index, planned in enumerate(plan.models):
                    self._raise_if_stale_or_cancelled(revision, ticket, cancel)
                    reusable = self.store.reusable_entry(planned)
                    if reusable is not None:
                        entry, _path = reusable
                        logger.info(
                            "Reusing local forecast model=%s run=%s artifact=%s",
                            planned.model_id,
                            planned.run_time.isoformat(),
                            entry.artifacts[0].sha256,
                        )
                        entries.append(entry)
                        self._progress(
                            progress,
                            planned.model_id,
                            90 + int(9 * (index + 1) / len(plan.models)),
                        )
                        continue
                    phase = "download"
                    try:
                        logger.info(
                            "Downloading forecast model=%s run=%s",
                            planned.model_id,
                            planned.run_time.isoformat(),
                        )
                        downloaded = self._download_one(
                            planned, stage, combined, progress
                        )
                        self._raise_if_stale_or_cancelled(revision, ticket, cancel)
                        acquired_at = _utc(self.clock(), "acquisition time")
                        phase = "artifact registration"
                        artifact = self.store.register_file(downloaded)
                        registered = self.store.root / artifact.path
                        self._raise_if_stale_or_cancelled(revision, ticket, cancel)
                        phase = "validation"
                        metadata = self.validator(registered, planned, combined)
                        logger.info(
                            "Validated forecast model=%s run=%s",
                            planned.model_id,
                            planned.run_time.isoformat(),
                        )
                        self._raise_if_stale_or_cancelled(revision, ticket, cancel)
                        entries.append(
                            self._validated_entry(
                                planned, metadata, artifact, acquired_at
                            )
                        )
                        self._progress(
                            progress,
                            planned.model_id,
                            90 + int(9 * (index + 1) / len(plan.models)),
                        )
                    except (RefreshCancelledError, RefreshSupersededError):
                        raise
                    except Exception as exc:
                        self._raise_if_stale_or_cancelled(revision, ticket, cancel)
                        logger.exception(
                            "Forecast model %s failed during %s",
                            planned.model_id,
                            phase,
                        )
                        raise RefreshExecutionError(
                            f"{planned.model_id} {phase} failed: {exc}"
                        ) from exc
                self._raise_if_stale_or_cancelled(revision, ticket, cancel)
                refreshed_at = _utc(self.clock(), "refresh time")
                tranche = ForecastTranche(
                    self.id_factory(), refreshed_at, plan.configurations, tuple(entries)
                )
                self._progress(progress, selected_model_id, 99)
                self._raise_if_stale_or_cancelled(revision, ticket, cancel)
                self.store.publish(tranche)
                logger.info(
                    "Published forecast tranche=%s models=%s selected=%s",
                    tranche.tranche_id,
                    ",".join(entry.model_id for entry in tranche.entries),
                    selected_model_id,
                )
                selection = AnalysisSelection(tranche.tranche_id, selected_model_id)
            # The owned stage is removed before activation; cleanup failure cannot
            # occur after the active pointer has committed.
        except (RefreshCancelledError, RefreshSupersededError):
            raise
        except Exception as exc:
            self._raise_if_stale_or_cancelled(revision, ticket, cancel)
            logger.exception("Forecast refresh preparation failed")
            raise RefreshExecutionError(f"refresh preparation failed: {exc}") from exc
        self._raise_if_stale_or_cancelled(revision, ticket, cancel)
        try:
            return self.store.activate(
                selection,
                commit_guard=self._commit_guard(revision, ticket, cancel, action_guard),
            )
        except (RefreshCancelledError, RefreshSupersededError):
            raise
        except Exception as exc:
            self._raise_if_stale_or_cancelled(revision, ticket, cancel)
            logger.exception("Forecast tranche activation failed")
            raise RefreshExecutionError(f"activation failed: {exc}") from exc
