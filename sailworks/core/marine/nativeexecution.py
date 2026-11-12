"""Rebuild a frozen native input in the spawned worker, never on the UI thread."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sailworks.core.forecastboundrouting import BoundRoutingWind, BoundRoutingWaves
from sailworks.core.marine.fields import FieldSelection, FieldStore

from .nativerequest import NativeRequest
from .routingnative import current_callable_from_selection


class SelectedShoreline:
    """Worker-local adapter over the selected GSHHS descriptor.

    Search, departure clearance and independent replay all call this one
    object, so they share one reviewed predicate set. ``margin_nm`` from the
    engine is converted to metres once and passed to the established
    ``lines_validity`` clearance check.
    """

    def __init__(self, path):
        from sailworks.core.geo.shoreline import ShorelineValidity

        self.provider = ShorelineValidity(path)

    def point_forbidden(self, latitude, longitude):
        return not self.provider.points_validity([(latitude, longitude)])[0]

    def segment_forbidden(self, start_lat, start_lon, end_lat, end_lon, margin_nm):
        if margin_nm < 0:
            raise ValueError("Shoreline margin must be non-negative")
        return not self.provider.lines_validity(
            [(start_lat, start_lon, end_lat, end_lon)], margin_nm * 1852.0
        )[0]

    def distance_nm(self, latitude, longitude):
        return self.provider.distance_to_forbidden_nm(latitude, longitude)


def _shoreline(job):
    if not job.provider_descriptors:
        raise ValueError("Native coastline checks require a selected shoreline")
    (descriptor,) = job.provider_descriptors
    if descriptor.kind != "gshhs":
        raise ValueError("Unsupported native shoreline provider: " + descriptor.kind)
    return SelectedShoreline(descriptor.path)


def run_native_request(job: NativeRequest, progress=None, cancel_event=None) -> dict:
    """Run only inputs the real bridge currently honors; reject all others.

    ``progress`` receives the engine's ``RoutingProgressUpdate`` dictionaries
    unchanged. It is an observer only: a failing callback must not alter the
    solve or its accepted result.

    ``cancel_event`` is polled at each engine progress checkpoint. Setting it
    stops the search cooperatively, so the worker process stays reusable and a
    cancelled run reports status ``cancelled`` without a route.

    No network or live process objects enter this path. BoundRoutingWind checks
    the selected artifact's digest, run, model, member and coherent wind bands.
    CurrentField IDs are content hashes verified again when the store loads.
    """
    if not isinstance(job, NativeRequest) or job.version != 1:
        raise ValueError("Unsupported frozen native request")
    if job.coastline_checks and not job.provider_descriptors:
        raise ValueError("Native coastline checks require a selected shoreline")
    if job.provider_descriptors and not job.coastline_checks:
        raise ValueError("Shoreline providers were supplied with coastline checks off")
    land = _shoreline(job) if job.coastline_checks else None
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
        use_waves=job.wave_forecast is not None,
        coastline_checks=job.coastline_checks,
        waypoints=list(job.waypoints),
        # The keep-off control is disabled with coastline checks off; the
        # legacy path ignores it then, so native does not apply it either. The
        # fingerprint below records whether it was applied.
        land_safety_margin_nm=(
            job.coastline_clearance_distance_m / 1852.0 if job.coastline_checks else 0.0
        ),
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
    waves = None
    try:
        if job.wave_forecast is not None:
            waves = BoundRoutingWaves(job.wave_forecast, Event())

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

        def selected_waves(latitude, longitude, epoch_s):
            if waves is None:
                return (0.0, 0.0, 0.0, False)
            sample = waves.get_wave_at(
                datetime.fromtimestamp(epoch_s, timezone.utc), latitude, longitude
            )
            return sample if sample is not None else (0.0, 0.0, 0.0, False)

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
            land,
            job.forecast.entry.artifacts[0].sha256,
            job.polar_sha256,
            job.engine,
            progress=progress,
            cancel_event=cancel_event,
            waves=selected_waves if waves is not None else None,
            wave_coverage=waves.coverage() if waves is not None else None,
        )
        if run.get("status") == "wave_data_required":
            provenance = run.get("wave_provenance") or {}
            coverage_data = provenance.get("coverage") or {}
            begins = coverage_data.get("begins")
            ends = coverage_data.get("ends")
            dataset = provenance.get("dataset_identity") or getattr(
                job.wave_forecast.entry, "model_id", "selected wave data"
            )
            raise ValueError(
                f"Wave data {dataset} does not cover the route window or route "
                f"endpoints (coverage: {begins or 'unknown'} to "
                f"{ends or 'unknown'}). No route was calculated and the wind "
                "proxy was not substituted."
            )
        run["request_kind"] = "selected-user-input"
        run["calculation_id"] = job.calculation_id
        run["engine_identity"] = bridge.identity()
        run["input_fingerprints"] = dict(
            forecast=job.forecast.entry.artifacts[0].sha256,
            polar=job.polar_sha256,
            waypoints=[list(point) for point in job.waypoints],
            start=job.start,
            destination=job.destination,
            current_field_ids=list(job.current_field_ids),
            current_priority=job.current_priority if job.use_currents else None,
            auto_download=job.auto_download,
            shoreline=(
                [
                    dict(kind=item.kind, path=item.path)
                    for item in job.provider_descriptors
                ]
                if job.coastline_checks
                else None
            ),
            coastline_clearance_distance_m=(
                job.coastline_clearance_distance_m if job.coastline_checks else None
            ),
            wave=(
                {
                    "entry_id": job.wave_forecast.entry.entry_id,
                    "model_id": job.wave_forecast.entry.model_id,
                    "run_time": job.wave_forecast.entry.run_time.isoformat(),
                    "artifact": job.wave_forecast.entry.artifacts[0].sha256,
                    "fields": list(job.wave_forecast.entry.fields),
                    "field_times": {
                        field: [instant.isoformat() for instant in times]
                        for field, times in job.wave_forecast.entry.field_times
                    },
                }
                if job.wave_forecast is not None
                else None
            ),
        )
        run["scenario"] = dict(
            name=job.name,
            wind_speed_pct=job.wind_speed_pct,
            wind_dir_offset=job.wind_dir_offset,
            polar_efficiency=job.polar_efficiency,
        )
        run["legacy_algorithm"] = dict(
            selected=job.legacy_algorithm,
            unchanged_parameters=dict(job.unchanged_legacy_parameters),
            note=(
                "the native engine runs its own search options; these legacy "
                "algorithm defaults were not executed and changed values are "
                "rejected before the calculation starts"
            ),
        )
        return run
    finally:
        wind.close()
        if waves is not None:
            waves.close()
