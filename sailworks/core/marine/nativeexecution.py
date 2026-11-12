"""Rebuild a frozen native input in the spawned worker, never on the UI thread."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sailworks.core.forecastboundrouting import BoundRoutingWind
from sailworks.core.marine.fields import FieldSelection, FieldStore

from .nativerequest import NativeRequest
from .routingnative import current_callable_from_selection


def run_native_request(job: NativeRequest) -> dict:
    """Run only inputs the real bridge currently honors; reject all others.

    No network or live process objects enter this path. BoundRoutingWind checks
    the selected artifact's digest, run, model, member and coherent wind bands.
    CurrentField IDs are content hashes verified again when the store loads.
    """
    if not isinstance(job, NativeRequest) or job.version != 1:
        raise ValueError("Unsupported frozen native request")
    if job.waypoints:
        raise ValueError("Native waypoint chaining is not implemented")
    if job.coastline_checks or job.provider_descriptors:
        raise ValueError("Native shoreline and clearance adapter is not implemented")
    if job.use_currents and (not job.marine_directory or not job.current_field_ids):
        raise ValueError("Selected local current inputs are missing")
    if job.current_field_ids and not job.use_currents:
        raise ValueError("Current inputs were supplied for wind-only routing")
    if not 1 + job.wind_speed_pct / 100 > 0:
        raise ValueError("Wind intensity must be greater than -100%")

    import sailworks_native_routing as bridge

    region = job.forecast.entry.region  # south, west, north, east
    wind_times = dict(job.forecast.entry.field_times)["wind"]
    coverage = dict(
        south=region[0],
        west=region[1],
        north=region[2],
        east=region[3],
        start_epoch_s=int(min(wind_times).timestamp()),
        end_epoch_s=int(max(wind_times).timestamp()),
    )
    polar = [
        {"true_wind_speed_knots": tws, "points": list(points)}
        for tws, points in job.polar_rows
    ]
    settings = dict(
        start=job.start,
        destination=job.destination,
        departure_epoch_s=job.departure_epoch_s,
        use_currents=job.use_currents,
        polar_efficiency=job.polar_efficiency,
        tws_max_kn=job.tws_max_kn,
        min_sailing_speed_kn=job.min_sailing_speed_kn,
        motor_speed_kn=job.motor_speed_kn,
        wave_correct=job.wave_correct,
        coastline_checks=job.coastline_checks,
    )
    selection = None
    if job.use_currents:
        field_directory = Path(job.marine_directory) / "fields"
        if not field_directory.is_dir():
            raise ValueError("Selected local current store is missing")
        store = FieldStore(field_directory)
        missing = [
            identifier
            for identifier in job.current_field_ids
            if identifier not in store.fields
        ]
        if missing:
            raise ValueError("Selected local current fields changed or are missing")
        selection = FieldSelection(
            tuple(store.fields[identifier] for identifier in job.current_field_ids),
            priority=job.current_priority,
        )

    from threading import Event

    wind = BoundRoutingWind(job.forecast, Event())
    try:

        def selected_wind(latitude, longitude, epoch_s):
            sample = wind.get_wind_at(
                datetime.fromtimestamp(epoch_s, timezone.utc), latitude, longitude
            )
            if sample is None:
                return 0.0, 0.0, False
            direction, speed = sample
            return (
                (direction + job.wind_dir_offset) % 360,
                speed * (1 + job.wind_speed_pct / 100),
                True,
            )

        current = (
            current_callable_from_selection(selection, include_provenance=True)
            if selection is not None
            else lambda latitude, longitude, epoch_s: (0.0, 0.0, False)
        )
        run = bridge.route_user_request(
            selected_wind,
            current,
            coverage,
            polar,
            settings,
            job.forecast.entry.artifacts[0].sha256,
            job.polar_sha256,
            job.engine,
        )
        run["request_kind"] = "selected-user-input"
        run["calculation_id"] = job.calculation_id
        run["input_fingerprints"] = dict(
            forecast=job.forecast.entry.artifacts[0].sha256,
            polar=job.polar_sha256,
            current_field_ids=list(job.current_field_ids),
            current_priority=job.current_priority if job.use_currents else None,
        )
        run["scenario"] = dict(
            name=job.name,
            wind_speed_pct=job.wind_speed_pct,
            wind_dir_offset=job.wind_dir_offset,
            polar_efficiency=job.polar_efficiency,
        )
        return run
    finally:
        wind.close()
