"""Purely pinned, all-model forecast refresh planning.

Planning reads provider catalogues but performs no downloads or GRIB validation.
Catalogue freshness is provider-dependent and is not asserted to be latest-published.
"""

from datetime import datetime, timezone
import json

from .forecastcapabilities import MODEL_CAPABILITIES, ModelCapabilities
from .forecastrefreshcontracts import ForecastRefreshPlan, PlannedModel
from .forecasttranche import ModelConfiguration, shared_requested_region


class RefreshPlanningError(ValueError):
    """A complete refresh plan could not be constructed."""


class UnsupportedConfigurationError(RefreshPlanningError):
    """A model request is not supported by the configured provider."""


class RefreshCancelledError(InterruptedError):
    """Planning was cancelled before an immutable plan was returned."""


def default_provider_factories():
    """Return lazy imports of the existing provider classes, keyed by stable ID."""
    from .gribsources.eccchrdps import ECCHRDPSource
    from .gribsources.ecmwf import ECMWFAIFSSource, ECMWFIFSSource
    from .gribsources.noaagfs import NOAAGFSSource
    from .gribsources.noaahrrr import NOAAHRRRSource
    from .gribsources.noaaaigfs import NOAAAIGFSSource
    from .gribsources.noaaaigfs import NOAAAIGFSSource

    return {
        "GFS": NOAAGFSSource,
        "HRRR": NOAAHRRRSource,
        "HRDPS": ECCHRDPSource,
        "IFS": ECMWFIFSSource,
        "AIFS": ECMWFAIFSSource,
        "AIGFS": NOAAAIGFSSource,
        "AIGFS": NOAAAIGFSSource,
    }


def create_source(planned_or_configuration, factories=None):
    planned = (
        planned_or_configuration
        if isinstance(planned_or_configuration, PlannedModel)
        else None
    )
    configuration = (
        planned.configuration if planned is not None else planned_or_configuration
    )
    if not isinstance(configuration, ModelConfiguration):
        raise TypeError("expected PlannedModel or ModelConfiguration")
    capability = _validate_configuration(configuration)
    if planned is not None and (
        planned.provider_name != capability.provider_name
        or planned.native_resolution != capability.native_resolution
    ):
        raise RefreshPlanningError("planned provider capability mismatch")
    factories = default_provider_factories() if factories is None else factories
    factory = factories.get(configuration.model_id)
    if factory is None:
        raise UnsupportedConfigurationError(
            f"No provider factory for {configuration.model_id}"
        )
    source = factory()
    if getattr(source, "name", None) != capability.provider_name:
        raise RefreshPlanningError(
            f"{configuration.model_id}: provider name does not match {capability.provider_name}"
        )
    if hasattr(source, "bounds"):
        source.bounds = tuple(configuration.region)
    return source


def _cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise RefreshCancelledError("Forecast refresh planning cancelled")


def _validate_configuration(configuration):
    if not isinstance(configuration, ModelConfiguration):
        raise UnsupportedConfigurationError("Expected ModelConfiguration values")
    capability = MODEL_CAPABILITIES.get(configuration.model_id)
    if capability is None:
        raise UnsupportedConfigurationError(
            f"{configuration.model_id}: model is not supported"
        )
    if configuration.resolution not in ("native", capability.native_resolution):
        raise UnsupportedConfigurationError(
            f"{configuration.model_id}: unsupported resolution {configuration.resolution!r}"
        )
    if configuration.interval not in capability.intervals:
        raise UnsupportedConfigurationError(
            f"{configuration.model_id}: unsupported native forecast interval {configuration.interval}"
        )
    if (
        configuration.model_id in ("GFS", "HRRR")
        and configuration.region[1] > configuration.region[3]
    ):
        raise UnsupportedConfigurationError(
            f"{configuration.model_id}: wrapped longitude bounds are not supported"
        )
    if configuration.forecast_hours > capability.max_horizon():
        raise UnsupportedConfigurationError(
            f"{configuration.model_id}: requested horizon exceeds provider maximum"
        )
    unsupported = tuple(
        field
        for field in configuration.fields
        if not capability.available_steps(None, field)
    )
    if unsupported:
        raise UnsupportedConfigurationError(
            f"{configuration.model_id}: unsupported forecast field(s): "
            + ", ".join(unsupported)
        )
    supported_union = {
        step
        for field in configuration.fields
        for step in capability.available_steps(None, field)
    }
    if any(step not in supported_union for step in configuration.steps):
        raise UnsupportedConfigurationError(
            f"{configuration.model_id}: explicit steps unsupported for selected fields"
        )
    return capability


def _run_time(payload, model_id):
    try:
        date, cycle = payload["date"], payload["cycle"]
        if (
            not isinstance(date, str)
            or len(date) != 8
            or not date.isdigit()
            or not isinstance(cycle, str)
            or len(cycle) != 2
            or not cycle.isdigit()
        ):
            raise ValueError("invalid date or cycle")
        run = datetime.strptime(date + cycle, "%Y%m%d%H").replace(tzinfo=timezone.utc)
        if int(cycle) > 23:
            raise ValueError("invalid cycle hour")
        return run
    except (KeyError, TypeError, ValueError) as exc:
        raise RefreshPlanningError(f"{model_id}: malformed run date/cycle") from exc


def _candidate(row, configuration, capability):
    if (
        not isinstance(row, (list, tuple))
        or len(row) < 5
        or not isinstance(row[4], str)
    ):
        raise RefreshPlanningError(f"{configuration.model_id}: malformed catalogue row")
    try:
        payload = json.loads(row[4])
    except (TypeError, json.JSONDecodeError) as exc:
        raise RefreshPlanningError(
            f"{configuration.model_id}: malformed catalogue identifier"
        ) from exc
    if not isinstance(payload, dict):
        raise RefreshPlanningError(
            f"{configuration.model_id}: catalogue identifier is not an object"
        )
    run = _run_time(payload, configuration.model_id)
    if run.hour not in capability.cycles:
        raise RefreshPlanningError(
            f"{configuration.model_id}: unsupported catalogue cycle"
        )
    if payload.get("source") != capability.provider_name:
        raise RefreshPlanningError(
            f"{configuration.model_id}: catalogue provider mismatch"
        )
    fields = payload.get("fields")
    if not isinstance(fields, list) or tuple(fields) != configuration.fields:
        raise RefreshPlanningError(
            f"{configuration.model_id}: catalogue fields mismatch"
        )
    hours, steps = payload.get("hours"), payload.get("steps")
    if (
        type(hours) is not int
        or hours < 0
        or type(steps) is not list
        or any(type(h) is not int for h in steps)
    ):
        raise RefreshPlanningError(
            f"{configuration.model_id}: malformed catalogue steps"
        )
    if tuple(sorted(set(steps))) != tuple(steps) or any(
        h < 0 or h > hours for h in steps
    ):
        raise RefreshPlanningError(
            f"{configuration.model_id}: invalid catalogue step ordering or horizon"
        )
    if configuration.forecast_hours > capability.max_horizon(run.hour):
        return None
    field_available = {
        field: capability.available_steps(run.hour, field)
        for field in configuration.fields
    }
    available = tuple(
        sorted({h for schedule in field_available.values() for h in schedule})
    )
    if any(step not in available for step in steps):
        raise RefreshPlanningError(
            f"{configuration.model_id}: provider advertised unsupported steps"
        )
    field_steps = []
    for field in configuration.fields:
        selected = tuple(
            h
            for h in field_available[field]
            if h <= configuration.forecast_hours
            and (not configuration.interval or h % configuration.interval == 0)
            and (not configuration.steps or h in configuration.steps)
        )
        if not selected:
            return None
        if not set(selected) <= set(steps):
            return None
        field_steps.append((field, selected))
    desired = tuple(sorted({h for _, selected in field_steps for h in selected}))
    normalized = {
        "source": capability.provider_name,
        "date": payload["date"],
        "cycle": payload["cycle"],
        "hours": max(desired),
        "steps": list(desired),
        "fields": list(configuration.fields),
    }
    return PlannedModel(
        configuration,
        run,
        capability.provider_name,
        json.dumps(normalized, sort_keys=True, separators=(",", ":")),
        capability.native_resolution,
        tuple(field_steps),
    )


def plan_refresh(configurations, selected_model_id, *, factories=None, cancel=None):
    """Pin a complete all-model refresh plan or raise without returning partial work."""
    configs = tuple(configurations)
    if not configs:
        raise UnsupportedConfigurationError("Select at least one model")
    try:
        shared_requested_region(configs)
    except ValueError as exc:
        raise UnsupportedConfigurationError(str(exc)) from exc
    # Validate every request before constructing sources or querying catalogues.
    capabilities = tuple(_validate_configuration(config) for config in configs)
    ids = tuple(config.model_id for config in configs)
    if len(set(ids)) != len(ids):
        raise UnsupportedConfigurationError("Duplicate model configuration")
    if selected_model_id not in ids:
        raise UnsupportedConfigurationError(
            "Selected model is not in the refresh configuration"
        )
    plans = []
    provider_factories = (
        default_provider_factories() if factories is None else factories
    )
    for configuration in configs:
        if not callable(provider_factories.get(configuration.model_id)):
            raise UnsupportedConfigurationError(
                f"{configuration.model_id}: no provider factory configured"
            )
    sources = []
    for configuration in configs:
        _cancelled(cancel)
        try:
            sources.append(create_source(configuration, provider_factories))
        except Exception as exc:
            raise RefreshPlanningError(f"{configuration.model_id}: {exc}") from exc
    for configuration, capability, source in zip(configs, capabilities, sources):
        _cancelled(cancel)
        try:
            from .gribsources.options import DownloadOptions

            options = DownloadOptions(
                configuration.forecast_hours,
                configuration.interval,
                configuration.fields,
                configuration.steps,
            )
            rows = source.get_download_list(options)
            if not isinstance(rows, (list, tuple)):
                raise RefreshPlanningError("catalogue result is not a sequence")
            candidates = [_candidate(row, configuration, capability) for row in rows]
            candidates = [
                candidate for candidate in candidates if candidate is not None
            ]
            if not candidates:
                raise RefreshPlanningError(
                    "no run supports the complete requested forecast"
                )
            plans.append(max(candidates, key=lambda candidate: candidate.run_time))
        except RefreshCancelledError:
            raise
        except Exception as exc:
            try:
                from .marine.chs import DownloadCancelledError
            except ImportError:
                pass
            else:
                if isinstance(exc, DownloadCancelledError):
                    raise
            if isinstance(exc, RefreshPlanningError):
                detail = str(exc)
            else:
                detail = str(exc)
            raise RefreshPlanningError(f"{configuration.model_id}: {detail}") from exc
        _cancelled(cancel)
    return ForecastRefreshPlan(tuple(plans), selected_model_id)
