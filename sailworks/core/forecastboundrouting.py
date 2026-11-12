"""Route using only the immutable weather binding captured by analysis state."""

import hashlib
import math
from threading import Event, RLock

from .forecastcapabilities import MODEL_CAPABILITIES
from .gribmanager import GribManager
from .grib import Grib, forecast_model_name
from .gribsampling import FIELDS, model_key, number
from .routingjob import (
    RoutingCancelledError,
    WindFile,
    WorkerGrib,
    create_requested_routing,
    run_requested_routing,
    close_requested_routing,
)
from .tranchestore import ResolvedForecast

_WIND_U = {"UGRD", "10U"}
_WIND_V = {"VGRD", "10V"}
_WAVE_ROUTING_FIELDS = ("wave_height", "wave_period", "wave_direction")


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
        # Display-only models (for example imported ERA5 history) are not in
        # the forecast catalogue but still provide valid wind fields.
        provider_name = (
            capability.provider_name if capability is not None else entry.model_id
        )
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
        centre = provider_name.split(" ", 1)[0]
        south, west, north, east = entry.region
        source = WindFile(
            str(path),
            provider_name,
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
        display_only = entry.model_id not in MODEL_CAPABILITIES
        expected_times = tuple(dict(entry.field_times)["wind"])
        expected_hours = {
            int((instant - entry.run_time).total_seconds() // 3600): instant
            for instant in expected_times
        }
        if not expected_hours or len(expected_hours) != len(expected_times):
            raise BoundForecastError("Bound wind schedule is invalid")
        identities = set()
        seen = {}
        for band_id in range(1, self.dataset.RasterCount + 1):
            metadata = self.dataset.GetRasterBand(band_id).GetMetadata() or {}
            element = metadata.get("GRIB_ELEMENT")
            if element in _WIND_U:
                component = "UGRD"
            elif element in _WIND_V:
                component = "VGRD"
            else:
                continue
            if metadata.get("GRIB_PDS_PDTN", "0") != "0":
                raise BoundForecastError(
                    "Bound wind contains a non-deterministic member"
                )
            detected = forecast_model_name(self.dataset, metadata)
            if detected != entry.model_id and not (
                detected == "Unknown" and entry.model_id not in MODEL_CAPABILITIES
            ):
                raise BoundForecastError("Bound wind contains a different model")
            try:
                reference = number(metadata["GRIB_REF_TIME"])
                seconds = number(metadata["GRIB_FORECAST_SECONDS"])
                valid = number(metadata["GRIB_VALID_TIME"])
            except (KeyError, TypeError, ValueError) as exc:
                raise BoundForecastError(
                    "Bound wind record has invalid time metadata"
                ) from exc
            normal_reference = (
                reference == entry.run_time.timestamp()
                and seconds.is_integer()
                and seconds % 3600 == 0
                and valid == reference + seconds
            )
            # Reanalysis (ERA5) marks every valid time as its own reference.
            reanalysis_reference = (
                display_only and seconds == 0 and abs(reference - valid) <= 1e-6
            )
            if not (normal_reference or reanalysis_reference):
                raise BoundForecastError(
                    "Bound wind contains a different run or valid time"
                )
            hour = int(
                (valid - entry.run_time.timestamp()) // 3600
                if display_only
                else seconds // 3600
            )
            if hour not in expected_hours:
                raise BoundForecastError(
                    "Bound wind contains an unplanned forecast time"
                )
            short_name = metadata.get("GRIB_SHORT_NAME")
            if short_name != "10-HTGL" and not (display_only and short_name == "0-SFC"):
                raise BoundForecastError("Bound wind contains an unsupported level")
            identity = model_key(metadata, self)
            identities.add(identity)
            key = (hour, component)
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
            record
            for record in index.records
            if record.element in _WIND_U or record.element in _WIND_V
        ]
        expected_stamps = {instant.timestamp() for instant in expected_times}
        if len(indexed) != sum(seen.values()) or any(
            (
                record.reference != entry.run_time.timestamp()
                if not display_only
                else abs(record.reference - record.valid) > 1e-6
            )
            or record.identity[1] not in identities
            or record.valid not in expected_stamps
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


class BoundWaveGrib(WorkerGrib):
    """Private reader for one published wave artifact, with exact scope checks."""

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
            raise BoundForecastError("Bound wave entry is not the selected entry")
        if entry.member != "deterministic" or len(entry.artifacts) != 1:
            raise BoundForecastError(
                "Bound wave routing requires one deterministic artifact"
            )
        field_times = dict(entry.field_times)
        missing = [
            field
            for field in _WAVE_ROUTING_FIELDS
            if field not in entry.fields or not field_times.get(field)
        ]
        if missing:
            raise BoundForecastError(
                "Bound wave forecast lacks validated fields: " + ", ".join(missing)
            )
        if len(binding.artifact_paths) != 1:
            raise BoundForecastError("Bound wave routing requires one artifact path")
        path = binding.artifact_paths[0]
        if not path.is_file() or path.is_symlink():
            raise BoundForecastError("Bound wave artifact is missing or unsafe")
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
            raise BoundForecastError("Bound wave artifact identity changed")
        capability = MODEL_CAPABILITIES.get(entry.model_id)
        provider_name = (
            capability.provider_name if capability is not None else entry.model_id
        )
        wave_times = field_times["wave_height"]
        hours = []
        for instant in wave_times:
            seconds = (instant - entry.run_time).total_seconds()
            if seconds < 0 or seconds % 3600:
                raise BoundForecastError(
                    "Bound wave times must be exact run-relative hours"
                )
            hours.append(int(seconds // 3600))
        centre = provider_name.split(" ", 1)[0]
        south, west, north, east = entry.region
        source = WindFile(
            str(path),
            provider_name,
            centre,
            (west, south, east, north),
            entry.run_time,
            max(hours),
            entry.model_id,
        )
        try:
            super().__init__(source)
            if self.dataset is None:
                raise BoundForecastError("Unable to open bound wave artifact")
            self._validate_wave_records(binding)
        except BaseException:
            if hasattr(self, "dataset"):
                cache = getattr(self, "_index_data", None)
                if cache is not None:
                    cache.cache_clear()
                Grib.close(self)
            raise

    def _validate_wave_records(self, binding):
        entry = binding.entry
        display_only = entry.model_id not in MODEL_CAPABILITIES
        expected = {
            field: {
                int(instant.timestamp()) for instant in dict(entry.field_times)[field]
            }
            for field in _WAVE_ROUTING_FIELDS
        }
        if any(not stamps for stamps in expected.values()):
            raise BoundForecastError("Bound wave schedule is invalid")
        elements = {field: set(FIELDS[field]) for field in _WAVE_ROUTING_FIELDS}
        seen = {}
        identities = set()
        for record in self.sampling.records:
            field = next(
                (name for name, names in elements.items() if record.element in names),
                None,
            )
            if field is None:
                continue
            valid = int(round(record.valid))
            if valid not in expected[field]:
                raise BoundForecastError(
                    "Bound wave contains an unplanned forecast time"
                )
            reference = record.reference
            normal_reference = reference == entry.run_time.timestamp()
            # Reanalysis (ERA5) marks every valid time as its own reference.
            reanalysis_reference = (
                display_only and abs(reference - record.valid) <= 1e-6
            )
            if not (normal_reference or reanalysis_reference):
                raise BoundForecastError(
                    "Bound wave contains a different run or valid time"
                )
            identities.add(record.identity[1])
            key = (field, valid)
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                raise BoundForecastError("Bound wave contains duplicate records")
        if len(identities) != 1:
            raise BoundForecastError("Bound wave contains mixed model identities")
        for field, stamps in expected.items():
            for stamp in stamps:
                if seen.get((field, stamp)) != 1:
                    raise BoundForecastError(
                        f"Bound wave lacks {field} at forecast hour "
                        f"{int((stamp - entry.run_time.timestamp()) // 3600)}"
                    )


class BoundRoutingWaves:
    """One strict wave source for a native calculation, with real coverage."""

    def __init__(self, binding, cancel):
        if not isinstance(binding, ResolvedForecast):
            raise TypeError("wave binding must be ResolvedForecast")
        self.cancel = cancel
        self._query_lock = RLock()
        self.binding = binding
        self.source = BoundWaveGrib(binding)
        self.sampler = self.source.sampler()
        entry = binding.entry
        artifact = entry.artifacts[0]
        self.dataset_identity = f"{entry.model_id}:{artifact.sha256}"
        self.model_identity = entry.model_id

    def coverage(self):
        entry = self.binding.entry
        times = tuple(dict(entry.field_times)["wave_height"])
        south, west, north, east = entry.region
        return {
            "south": float(south),
            "west": float(west),
            "north": float(north),
            "east": float(east),
            "start_epoch_s": int(times[0].timestamp()),
            "end_epoch_s": int(times[-1].timestamp()),
            "dataset_identity": self.dataset_identity,
        }

    def get_wave_at(self, instant, latitude, longitude):
        """Return (height_m, period_s, direction_from_deg, available, ...)."""
        if self.cancel.is_set():
            raise RoutingCancelledError()
        with self._query_lock:
            if self.cancel.is_set():
                raise RoutingCancelledError()
            values = [
                float(
                    self.sampler.sample(field, instant, [latitude], [longitude])[0, 0]
                )
                for field in _WAVE_ROUTING_FIELDS
            ]
        if self.cancel.is_set():
            raise RoutingCancelledError()
        height, period, direction = values
        if (
            not math.isfinite(height)
            or height < 0.0
            or not math.isfinite(period)
            or period <= 0.0
            or not math.isfinite(direction)
        ):
            return None
        return (
            height,
            period,
            direction % 360.0,
            True,
            self.dataset_identity,
            self.model_identity,
        )

    def close(self):
        self.source.close()


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
