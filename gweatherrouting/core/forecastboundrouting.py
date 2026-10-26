"""Route using only the immutable weather binding captured by analysis state."""

from datetime import timedelta
import hashlib
from threading import Event, RLock
from types import SimpleNamespace

from .forecastcapabilities import MODEL_CAPABILITIES
from .gribmanager import GribManager
from .grib import Grib, forecast_model_name
from .gribsampling import model_key, number
from .routingjob import (
    RoutingCancelledError,
    WindFile,
    WorkerGrib,
    create_requested_routing,
    run_requested_routing,
    close_requested_routing,
)
from .tranchestore import ResolvedForecast


class BoundForecastError(ValueError):
    """The immutable analysis binding is missing or outside its route capability."""


class BoundWorkerGrib(WorkerGrib):
    """Private GDAL reader with a second, exact run/model/member wind-scope check."""

    def __init__(self, binding):
        entry = binding.entry
        if (
            binding.selection.tranche_id != binding.tranche.tranche_id
            or binding.selection.entry_id != entry.entry_id
            or not any(
                declared.entry_id == entry.entry_id and declared == entry
                for declared in binding.tranche.entries
            )
        ):
            raise BoundForecastError("Bound entry is not the selected manifest entry")
        if entry.member != "deterministic" or len(entry.artifacts) != 1:
            raise BoundForecastError(
                "Bound routing requires one deterministic artifact"
            )
        if (
            "wind" not in entry.fields
            or "10-HTGL" not in entry.levels
            or not dict(entry.field_times).get("wind")
        ):
            raise BoundForecastError("Bound forecast has no validated wind coverage")
        if len(binding.artifact_paths) != 1:
            raise BoundForecastError("Bound routing requires exactly one artifact path")
        path = binding.artifact_paths[0]
        if not path.is_file() or path.is_symlink():
            raise BoundForecastError("Bound forecast artifact is missing or unsafe")
        artifact = entry.artifacts[0]
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        if (
            path.parent.name != "artifacts"
            or path.name != artifact.sha256
            or digest.hexdigest() != artifact.sha256
            or size != artifact.size
        ):
            raise BoundForecastError("Bound forecast artifact identity changed")
        capability = MODEL_CAPABILITIES.get(entry.model_id)
        if capability is None:
            raise BoundForecastError("Bound forecast model is unsupported")
        wind_times = dict(entry.field_times)["wind"]
        hours = []
        for instant in wind_times:
            seconds = (instant - entry.run_time).total_seconds()
            if seconds < 0 or seconds % 3600:
                raise BoundForecastError(
                    "Bound wind times must be exact run-relative hours"
                )
            hours.append(int(seconds // 3600))
        last = max(hours)
        centre = capability.provider_name.split(" ", 1)[0]
        south, west, north, east = entry.region
        source = WindFile(
            str(path),
            capability.provider_name,
            centre,
            (west, south, east, north),
            entry.run_time,
            last,
            entry.model_id,
        )
        try:
            super().__init__(source)
            if self.dataset is None:
                raise BoundForecastError("Unable to open bound forecast artifact")
            self._validate_records(binding)
        except BaseException:
            if hasattr(self, "dataset"):
                cache = getattr(self, "_index_data", None)
                if cache is not None:
                    cache.cache_clear()
                Grib.close(self)
            raise

    def _validate_records(self, binding):
        entry = binding.entry
        expected_times = tuple(dict(entry.field_times)["wind"])
        expected_hours = {
            int((instant - entry.run_time).total_seconds() // 3600): instant
            for instant in expected_times
        }
        if not expected_hours or len(expected_hours) != len(expected_times):
            raise BoundForecastError("Bound wind schedule is invalid")
        identity_source = SimpleNamespace(
            centre="", model=entry.model_id, path=self.path
        )
        identities = set()
        seen = {}
        for band_id in range(1, self.dataset.RasterCount + 1):
            metadata = self.dataset.GetRasterBand(band_id).GetMetadata() or {}
            element = metadata.get("GRIB_ELEMENT")
            if element not in ("UGRD", "VGRD"):
                continue
            if metadata.get("GRIB_PDS_PDTN") != "0":
                raise BoundForecastError(
                    "Bound wind contains a non-deterministic member"
                )
            if forecast_model_name(self.dataset, metadata) != entry.model_id:
                raise BoundForecastError("Bound wind contains a different model")
            try:
                reference = number(metadata["GRIB_REF_TIME"])
                seconds = number(metadata["GRIB_FORECAST_SECONDS"])
                valid = number(metadata["GRIB_VALID_TIME"])
            except (KeyError, TypeError, ValueError) as exc:
                raise BoundForecastError(
                    "Bound wind record has invalid time metadata"
                ) from exc
            if (
                reference != entry.run_time.timestamp()
                or not seconds.is_integer()
                or seconds % 3600
                or valid != reference + seconds
            ):
                raise BoundForecastError(
                    "Bound wind contains a different run or valid time"
                )
            hour = int(seconds // 3600)
            if hour not in expected_hours:
                raise BoundForecastError(
                    "Bound wind contains an unplanned forecast time"
                )
            if metadata.get("GRIB_SHORT_NAME") != "10-HTGL":
                raise BoundForecastError("Bound wind contains an unsupported level")
            identity = model_key(metadata, identity_source)
            identities.add(identity)
            key = (hour, element)
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                raise BoundForecastError("Bound wind contains duplicate components")
        if len(identities) != 1:
            raise BoundForecastError("Bound wind contains mixed model identities")
        for hour in expected_hours:
            if seen.get((hour, "UGRD")) != 1 or seen.get((hour, "VGRD")) != 1:
                raise BoundForecastError(
                    f"Bound wind lacks a coherent U/V pair at forecast hour {hour}"
                )
        index = self.sampling
        indexed = [
            record for record in index.records if record.element in ("UGRD", "VGRD")
        ]
        if len(indexed) != sum(seen.values()) or any(
            record.reference != entry.run_time.timestamp()
            or record.identity[1] not in identities
            or record.valid not in {instant.timestamp() for instant in expected_times}
            for record in indexed
        ):
            raise BoundForecastError(
                "Bound grid index contains out-of-scope wind records"
            )


class BoundRoutingWind:
    """One strict source and no recovery path, while sharing routing wind behavior."""

    def __init__(self, binding, cancel):
        self.cancel = cancel
        self._query_lock = RLock()
        self.manager = GribManager.__new__(GribManager)
        self.manager.gribs = []
        self.recover_missing = None
        source = BoundWorkerGrib(binding)
        self.manager.gribs.append(source)

    def get_wind_at(self, instant, latitude, longitude):
        if self.cancel.is_set():
            raise RoutingCancelledError()
        with self._query_lock:
            if self.cancel.is_set():
                raise RoutingCancelledError()
            value = self.manager.get_wind_at(instant, latitude, longitude)
        if self.cancel.is_set():
            raise RoutingCancelledError()
        return value

    def check_cancelled(self):
        if self.cancel.is_set():
            raise RoutingCancelledError()

    def close(self):
        for source in self.manager.gribs:
            source.close()
        self.manager.gribs.clear()


def calculate_bound_route(
    core,
    request,
    binding: ResolvedForecast,
    providers,
    cancel: Event,
    progress,
    *,
    report=lambda message: None,
    confirm=lambda report: False,
    save_fields=None,
    current_selection=None,
):
    """Use only one captured tranche entry for the complete route/scenario."""
    if not isinstance(binding, ResolvedForecast):
        raise TypeError("binding must be ResolvedForecast")
    if (
        binding.selection.tranche_id != binding.tranche.tranche_id
        or binding.selection.entry_id != binding.entry.entry_id
        or binding.entry.member != "deterministic"
        or len(binding.entry.artifacts) != 1
        or len(binding.artifact_paths) != 1
        or "wind" not in binding.entry.fields
        or "10-HTGL" not in binding.entry.levels
        or not dict(binding.entry.field_times).get("wind")
    ):
        raise BoundForecastError("Selected tranche entry is not routable")
    wind = None
    routing = None
    try:
        if cancel.is_set():
            raise RoutingCancelledError()
        wind = BoundRoutingWind(binding, cancel)
        routing = create_requested_routing(
            core, request, providers, wind, current_selection
        )
        from .routingcoverage import prepare_coverage

        prepare_coverage(
            routing,
            core.marine_manager if request.use_currents else None,
            request.auto_download,
            cancel,
            report,
            confirm,
        )
        result = run_requested_routing(
            core, routing, request, wind, progress, report, save_fields
        )
        artifact = binding.entry.artifacts[0]
        result.provenance["weather_models"] = [binding.entry.model_id]
        result.provenance["weather_selection_mode"] = "tranche"
        result.provenance["weather_binding"] = {
            "tranche_id": binding.tranche.tranche_id,
            "entry_id": binding.entry.entry_id,
            "model_id": binding.entry.model_id,
            "run_time": binding.entry.run_time.isoformat(),
            "member": binding.entry.member,
            "artifact": {"sha256": artifact.sha256, "size": artifact.size},
            "field_times": {
                field: [time.isoformat() for time in times]
                for field, times in binding.entry.field_times
            },
        }
        return result
    finally:
        try:
            close_requested_routing(routing)
        finally:
            if wind is not None:
                wind.close()
