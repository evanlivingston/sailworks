"""Spawn-safe execution of independent routing scenarios.

The application's scenario loop runs in one worker thread and only one route
at a time.  A route is largely independent of the next one, but the routing
objects contain GDAL handles, pyproj state and Qt chart providers, so sharing a
forked process is unsafe.  This module carries only immutable, picklable
inputs to a spawned worker and rebuilds the small routing context there.

The existing serial path remains the fallback and the reference.
"""

from __future__ import annotations

import multiprocessing
import os
import traceback
from dataclasses import dataclass
from threading import Event, Thread
from typing import Any, Optional

from weatherrouting import RoutingNoWindError

from .forecastboundrouting import calculate_bound_route
from .marine.chs import DownloadCancelledError
from .marine.fields import FieldSelection
from .marine.routing import DataGap, RoutingDataGapError
from .routingfactory import build_routing
from .routingjob import (
    CalculatedRoute,
    RouteRequest,
    RoutingCancelledError,
)
from .tranchestore import ResolvedForecast


@dataclass(frozen=True)
class ProviderDescriptor:
    """A chart validity provider that a worker can rebuild without Qt."""

    kind: str
    path: str


@dataclass(frozen=True)
class ScenarioJob:
    """Everything one scenario needs, and nothing bound to a live Qt object."""

    job_id: int
    request: RouteRequest
    binding: ResolvedForecast
    providers: tuple[ProviderDescriptor, ...] = ()
    current_field_ids: tuple[str, ...] = ()
    current_priority: str = "finest"
    marine_directory: Optional[str] = None


@dataclass(frozen=True)
class ScenarioFailure:
    kind: str
    message: str
    gaps: tuple[DataGap, ...] = ()
    traceback: str = ""


@dataclass(frozen=True)
class ScenarioOutcome:
    job_id: int
    result: Optional[CalculatedRoute] = None
    failure: Optional[ScenarioFailure] = None
    needs_confirmation: bool = False
    fallback: bool = False
    downloaded_fields: tuple[Any, ...] = ()


class ScenarioNeedsConfirmation(Exception):
    """The worker reached a user decision that must run on the UI thread."""


class _WorkerCore:
    """The subset of ``Core`` that ``calculate_bound_route`` uses."""

    def __init__(self, marine_directory=None, use_currents=False):
        self.marine_manager = None
        if use_currents and marine_directory:
            from .marine import MarineManager

            # Credentials are not needed before the user confirms a download;
            # a job that needs that confirmation is re-run on the UI thread.
            self.marine_manager = MarineManager(marine_directory, credential_store=None)

    def create_routing(
        self,
        algorithm,
        polar_file,
        track_or_poi,
        start_datetime,
        start_position,
        validity_providers,
        disable_coastline_checks=False,
        grib_override=None,
        polar_efficiency=None,
        use_currents=False,
        wind_sources=None,
        current_selection=None,
        current_priority="finest",
        safety_policy=None,
    ):
        if grib_override is None:
            raise ValueError("A scenario worker requires a bound wind override")
        if (
            use_currents
            and current_selection is None
            and self.marine_manager is not None
        ):
            current_selection = self.marine_manager.fields.snapshot(current_priority)
        return build_routing(
            algorithm,
            polar_file,
            track_or_poi,
            start_datetime,
            start_position,
            validity_providers,
            grib_override,
            wind_override=grib_override,
            disable_coastline_checks=disable_coastline_checks,
            polar_efficiency=polar_efficiency,
            use_currents=use_currents,
            wind_sources=wind_sources,
            current_selection=current_selection,
            safety_policy=safety_policy,
        )


def provider_descriptors(providers):
    """Describe providers a worker can rebuild, or return ``None``.

    An unknown provider keeps the whole group on the serial path; the worker
    must never silently drop a coastline check.
    """
    descriptors = []
    for provider in providers:
        descriptor = getattr(provider, "validity_descriptor", None)
        if descriptor is None:
            return None
        try:
            kind, path = descriptor()
        except (TypeError, ValueError):
            return None
        if not isinstance(kind, str) or not isinstance(path, str):
            return None
        descriptors.append(ProviderDescriptor(kind, path))
    return tuple(descriptors)


def _provider(descriptor):
    if descriptor.kind == "gshhs":
        from .geo.shoreline import ShorelineValidity

        return ShorelineValidity(descriptor.path)
    raise ValueError("Unsupported scenario validity provider: " + descriptor.kind)


def _failure(error):
    kind = type(error).__name__
    gaps = (
        tuple(getattr(error, "gaps", ()))
        if isinstance(error, RoutingDataGapError)
        else ()
    )
    return ScenarioFailure(
        kind=kind,
        message=str(error),
        gaps=gaps,
        traceback=traceback.format_exc(),
    )


def error_from_failure(failure):
    """Rebuild the exception type the UI already knows how to report."""
    if failure.kind == "RoutingCancelledError":
        return RoutingCancelledError(failure.message)
    if failure.kind == "DownloadCancelledError":
        return DownloadCancelledError(failure.message)
    if failure.kind == "RoutingNoWindError":
        return RoutingNoWindError(failure.message)
    if failure.kind == "RoutingDataGapError":
        return RoutingDataGapError(failure.gaps)
    return ValueError(failure.message or failure.kind)


def run_scenario(job, progress_queue=None, cancel_event=None, calculate=None):
    """Run one job.  This is the spawn target; keep it module-level."""
    shared_cancel = cancel_event if cancel_event is not None else Event()
    if calculate is None:
        calculate = calculate_bound_route

    def send(message, *values):
        if progress_queue is not None:
            progress_queue.put((message, job.job_id, *values))

    def progress(percent, points):
        send(
            "progress",
            float(percent),
            tuple(points),
            tuple(getattr(points, "isochrones", ())),
            getattr(points, "contours", None),
        )

    def report(message):
        send("message", str(message))

    def confirm(_report):
        raise ScenarioNeedsConfirmation()

    downloaded = []

    def save_fields(fields):
        downloaded.extend(fields)

    try:
        core = _WorkerCore(
            marine_directory=job.marine_directory,
            use_currents=bool(job.request.use_currents),
        )
        providers = tuple(_provider(descriptor) for descriptor in job.providers)
        selection = None
        if job.current_field_ids:
            fields = (
                core.marine_manager.fields.fields
                if core.marine_manager is not None
                else {}
            )
            missing = [
                identifier
                for identifier in job.current_field_ids
                if identifier not in fields
            ]
            if missing:
                # The frozen field snapshot is not readable here; let the
                # parent re-run the group with its own handles rather than
                # silently selecting a different field.
                return ScenarioOutcome(job.job_id, fallback=True)
            selection = FieldSelection(
                tuple(fields[identifier] for identifier in job.current_field_ids),
                priority=job.current_priority,
            )
        # The shared event is a manager proxy.  Checking it on every routing
        # candidate would turn each cancel check into an IPC round trip; keep a
        # local event and refresh it from the shared one at a bounded interval.
        local_cancel = Event()
        if shared_cancel.is_set():
            local_cancel.set()
        stop_watch = Event()

        def watch_cancel():
            while not stop_watch.wait(0.05):
                if shared_cancel.is_set():
                    local_cancel.set()
                    return

        watcher = Thread(target=watch_cancel, name="scenario-cancel", daemon=True)
        watcher.start()
        try:
            result = calculate(
                core,
                job.request,
                job.binding,
                providers,
                local_cancel,
                progress,
                report=report,
                confirm=confirm,
                save_fields=save_fields,
                current_selection=selection,
            )
        finally:
            stop_watch.set()
            watcher.join(timeout=0.2)
        return ScenarioOutcome(
            job.job_id,
            result=result,
            downloaded_fields=tuple(downloaded),
        )
    except ScenarioNeedsConfirmation:
        return ScenarioOutcome(job.job_id, needs_confirmation=True)
    except BaseException as error:  # the worker must always return a result
        return ScenarioOutcome(job.job_id, failure=_failure(error))


def default_workers():
    """Bounded process count; one scenario never needs a pool."""
    raw = os.environ.get("SAILWORKS_SCENARIO_WORKERS")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return max(1, min(4, os.cpu_count() or 1))
