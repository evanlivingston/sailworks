"""Immutable public contracts for pinned forecast refresh plans."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json

from .forecastcapabilities import MODEL_CAPABILITIES
from .forecasttranche import ModelConfiguration, shared_requested_region


def _aware_utc(value):
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("planned run time must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class PlannedModel:
    configuration: ModelConfiguration
    run_time: datetime
    provider_name: str
    identifier: str
    native_resolution: str
    field_steps: tuple[tuple[str, tuple[int, ...]], ...]
    member: str = "deterministic"

    def __post_init__(self):
        if not isinstance(self.configuration, ModelConfiguration):
            raise TypeError("configuration must be ModelConfiguration")
        run_time = _aware_utc(self.run_time)
        object.__setattr__(self, "run_time", run_time)
        if (run_time.minute, run_time.second, run_time.microsecond) != (0, 0, 0):
            raise ValueError("planned run must identify an exact UTC cycle hour")
        if not self.provider_name or not isinstance(self.provider_name, str):
            raise ValueError("provider name must be non-empty")
        if not isinstance(self.identifier, str) or not self.identifier:
            raise ValueError("pinned identifier must be non-empty JSON")
        try:
            payload = json.loads(self.identifier)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("pinned identifier must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("pinned identifier must be a JSON object")
        capability = MODEL_CAPABILITIES.get(self.model_id)
        if (
            capability is None
            or self.native_resolution != capability.native_resolution
            or self.provider_name != capability.provider_name
        ):
            raise ValueError("native resolution does not match model capability")
        if self.member != "deterministic":
            raise ValueError("only deterministic members are currently supported")
        if self.configuration.resolution not in (
            "native",
            capability.native_resolution,
        ):
            raise ValueError("configuration resolution does not match model capability")
        if (
            self.model_id in ("GFS", "HRRR")
            and self.configuration.region[1] > self.configuration.region[3]
        ):
            raise ValueError(
                "planned request uses unsupported wrapped longitude bounds"
            )
        if run_time.hour not in capability.cycles:
            raise ValueError("planned run uses unsupported provider cycle")
        if self.configuration.forecast_hours > capability.max_horizon(run_time.hour):
            raise ValueError("planned run does not cover configured horizon")
        raw_steps = tuple(self.field_steps)
        if any(
            not isinstance(item, (tuple, list))
            or len(item) != 2
            or not isinstance(item[1], (tuple, list))
            for item in raw_steps
        ):
            raise ValueError("field schedules must be (field, hours) pairs")
        steps = tuple((field, tuple(hours)) for field, hours in raw_steps)
        if tuple(field for field, _ in steps) != self.configuration.fields:
            raise ValueError("field schedules must match configured fields in order")
        for field, hours in steps:
            if (
                not isinstance(field, str)
                or not hours
                or any(
                    type(hour) is not int
                    or hour < 0
                    or hour > self.configuration.forecast_hours
                    for hour in hours
                )
                or tuple(sorted(set(hours))) != hours
            ):
                raise ValueError(
                    "each configured field needs unique, sorted integer forecast hours"
                )
        supported_union = set()
        field_supported = {}
        for field in self.configuration.fields:
            supported = capability.available_steps(run_time.hour, field)
            field_supported[field] = supported
            supported_union.update(supported)
        if any(hour not in supported_union for hour in self.configuration.steps):
            raise ValueError("configuration requests unsupported provider steps")
        for field, hours in steps:
            supported_hours = field_supported[field]
            expected = tuple(
                hour
                for hour in supported_hours
                if hour <= self.configuration.forecast_hours
                and (
                    not self.configuration.interval
                    or hour % self.configuration.interval == 0
                )
                and (not self.configuration.steps or hour in self.configuration.steps)
            )
            if not expected or hours != expected:
                raise ValueError("field schedule does not match provider capability")
        schedule_union = sorted({hour for _, hours in steps for hour in hours})
        payload_steps = payload.get("steps")
        if (
            payload.get("source") != self.provider_name
            or payload.get("date") != run_time.strftime("%Y%m%d")
            or payload.get("cycle") != run_time.strftime("%H")
            or payload.get("fields") != list(self.configuration.fields)
            or type(payload.get("hours")) is not int
            or payload.get("hours") != max(schedule_union)
            or not isinstance(payload_steps, list)
            or any(type(hour) is not int for hour in payload_steps)
            or payload_steps != schedule_union
        ):
            raise ValueError("pinned identifier does not match planned model metadata")
        object.__setattr__(self, "field_steps", steps)
        if not isinstance(self.member, str) or not self.member:
            raise ValueError("member must be non-empty")

    @property
    def model_id(self):
        return self.configuration.model_id


@dataclass(frozen=True)
class ForecastRefreshPlan:
    models: tuple[PlannedModel, ...]
    selected_model_id: str

    def __post_init__(self):
        models = tuple(self.models)
        if not models or any(not isinstance(model, PlannedModel) for model in models):
            raise ValueError("plan needs non-empty PlannedModel values")
        ids = tuple(model.model_id for model in models)
        if len(set(ids)) != len(ids) or self.selected_model_id not in ids:
            raise ValueError("selected model must identify one unique planned model")
        shared_requested_region(tuple(model.configuration for model in models))
        object.__setattr__(self, "models", models)

    @property
    def configurations(self):
        return tuple(model.configuration for model in self.models)

    @property
    def region(self):
        return shared_requested_region(self.configurations)
