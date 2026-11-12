"""Spawned real-request jobs must not become synthetic fixture calculations."""

import json
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest

from sailworks.core.forecasttranche import (
    AnalysisSelection,
    ForecastTranche,
    ModelConfiguration,
    ModelEntry,
)
from sailworks.core.marine.nativerequest import freeze_native_requests
from sailworks.core.marine.nativeworker import NativeRoutingWorker
from sailworks.core.marine.fields import CurrentField, FieldStore
from sailworks.core.marine.routingnative import to_routing_result
from sailworks.core.marine.routingresult import RoutingResult, export_blockers
from sailworks.core.routingjob import RouteRequest
from sailworks.core.tranchestore import TrancheStore
from test.test_native_routing_fixture import START, _forecast_inputs


class LegacyAlgorithm:
    pass


def _selected_forecast(tmp_path, source):
    store = TrancheStore(tmp_path / "published")
    artifact = store.register_file(Path(source.path))
    region = (48.0, -124.0, 50.0, -122.0)
    times = tuple(START + timedelta(hours=hour) for hour in (0, 3, 6))
    configuration = ModelConfiguration(
        "GFS", ("wind",), region, "native", 6, (0, 3, 6), 0
    )
    entry = ModelEntry(
        "GFS",
        "GFS",
        START,
        "deterministic",
        ("wind",),
        ("10-HTGL",),
        region,
        "0.5deg",
        times,
        (artifact,),
        START,
        (("wind", times),),
    )
    tranche = ForecastTranche("selected-batch", START, (configuration,), (entry,))
    store.publish(tranche)
    selection = AnalysisSelection("selected-batch", "GFS")
    store.activate(selection)
    return store.resolve(selection)


def _job(tmp_path, binding):
    request = RouteRequest(
        name="selected-real-request",
        start=(48.25, -123.65),
        destination=(48.29, -123.61),
        departure=START + timedelta(minutes=30),
        polar_file=str(
            Path(__file__).resolve().parents[1] / "sailworks/data/polars/test.pol"
        ),
        algorithm=LegacyAlgorithm,
        coastline_checks=False,
        wind_speed_pct=-15,
        wind_dir_offset=12,
        min_sailing_speed_kn=3.0,
        motor_speed_kn=5.0,
        wave_correct=True,
    )
    return freeze_native_requests(request, (), binding, (), None, engine="main")[0]


def _wait(worker, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = worker.poll(0.1)
        if status is not None:
            return status
    pytest.fail("native selected request worker timed out")


def test_spawned_worker_uses_published_selected_request(tmp_path):
    with _forecast_inputs(tmp_path) as source:
        job = _job(tmp_path, _selected_forecast(tmp_path, source))
    # The keep-off spinbox may retain a value while coastline checks are off.
    # Disabled clearance must not block an otherwise explicit open-water run.
    job = replace(job, coastline_clearance_distance_m=1852.0)
    worker = NativeRoutingWorker()
    try:
        generation = worker.submit(job)
        status = _wait(worker)
        assert status.generation == generation
        assert status.state == "completed", status.message
        run = status.payload
        assert run["request_kind"] == "selected-user-input"
        assert run["calculation_id"] == job.calculation_id
        assert run["request"]["start"] == {"latitude": 48.25, "longitude": -123.65}
        assert run["request"]["destination"] == {
            "latitude": 48.29,
            "longitude": -123.61,
        }
        assert run["request"]["departure"] == int(job.departure_epoch_s)
        assert (
            run["input_fingerprints"]["forecast"]
            == job.forecast.entry.artifacts[0].sha256
        )
        assert run["accepted"], (
            run["status"],
            run["message"],
            run["validation"],
            run["independent_validation"],
        )
        assert run["independent_validation"]["passed"]
        assert (
            run["legs"][0]["wind_source"]["dataset_identity"]
            == job.forecast.entry.artifacts[0].sha256
        )
        import sailworks_native_routing as bridge

        result = to_routing_result(
            run,
            calculation_id=job.calculation_id,
            generation=generation,
            sequence=0,
            created_epoch_s=int(time.time()),
            identity=bridge.identity(),
        )
        assert export_blockers(result) == ()
        reloaded = RoutingResult.from_dict(result.to_dict())
        assert reloaded.calculation_id == result.calculation_id
        assert reloaded.outcome.diagnostics["input_fingerprints"]["forecast"] == (
            job.forecast.entry.artifacts[0].sha256
        )
    finally:
        worker.shutdown()


def test_unimplemented_waypoint_and_missing_artifact_fail_without_fixture_fallback(
    tmp_path,
):
    with _forecast_inputs(tmp_path) as source:
        job = _job(tmp_path, _selected_forecast(tmp_path, source))
    from dataclasses import replace

    worker = NativeRoutingWorker()
    try:
        worker.submit(replace(job, waypoints=((48.28, -123.62),)))
        status = _wait(worker)
        assert status.state == "failed" and status.payload is None
        assert "waypoint" in status.message
        job.forecast.artifact_paths[0].unlink()
        worker.submit(job)
        status = _wait(worker)
        assert status.state == "failed" and status.payload is None
        assert "missing or unsafe" in status.message
    finally:
        worker.shutdown()


def test_spawned_current_field_keeps_selected_dataset_identity(tmp_path):
    with _forecast_inputs(tmp_path) as source:
        binding = _selected_forecast(tmp_path, source)
    marine_directory = tmp_path / "marine"
    fields = FieldStore(marine_directory / "fields")
    times = np.array([START.timestamp() + 3600 * hour for hour in range(7)])
    metadata = json.dumps(
        dict(
            schema_version=1,
            provider="test",
            model="selected-current",
            composition="total",
            vector_reference="true_east_north",
            sources=[],
            resolution_m=1000,
            run=START.isoformat(),
        ),
        sort_keys=True,
    )
    field = CurrentField(
        metadata,
        times,
        np.array([48.0, 50.0]),
        np.array([-124.0, -122.0]),
        np.full((7, 2, 2), 0.25),
        np.zeros((7, 2, 2)),
    )
    fields.add(field)
    selected = fields.snapshot("newest")
    original = _job(tmp_path, binding)
    request = RouteRequest(
        name="selected-current-request",
        start=original.start,
        destination=original.destination,
        departure=START + timedelta(minutes=30),
        polar_file=str(
            Path(__file__).resolve().parents[1] / "sailworks/data/polars/test.pol"
        ),
        algorithm=LegacyAlgorithm,
        coastline_checks=False,
        use_currents=True,
        current_priority="newest",
    )
    job = freeze_native_requests(
        request,
        (),
        binding,
        (),
        selected,
        marine_directory=marine_directory,
        engine="main",
    )[0]
    worker = NativeRoutingWorker()
    try:
        worker.submit(job)
        status = _wait(worker)
        assert status.state == "completed", status.message
        run = status.payload
        assert run["accepted"], (run["status"], run["message"])
        assert run["input_fingerprints"]["current_field_ids"] == [field.id]
        assert run["legs"][0]["current_source"]["dataset_identity"] == field.id
    finally:
        worker.shutdown()


def test_real_job_cancel_supersede_crash_and_close_never_publish_stale(tmp_path):
    with _forecast_inputs(tmp_path) as source:
        job = _job(tmp_path, _selected_forecast(tmp_path, source))
    worker = NativeRoutingWorker()
    try:
        first = worker.submit(job)
        cancelled = worker.cancel()
        assert cancelled.generation == first
        assert cancelled.state == "cancelled" and cancelled.payload is None
        assert worker.poll(0) is None

        superseded = worker.submit(job)
        latest = worker.submit(replace(job, destination=(48.28, -123.60)))
        assert worker.superseded_generation == superseded
        status = _wait(worker)
        assert status.generation == latest and status.state == "completed"
        assert status.payload["request"]["destination"] == {
            "latitude": 48.28,
            "longitude": -123.60,
        }
        assert worker.poll(0) is None

        worker.submit(job)
        worker._process.terminate()  # simulate native failure before delivery
        status = _wait(worker)
        assert status.state == "failed" and status.payload is None
        worker.submit(job)
        worker.shutdown()
        assert not worker.running and worker.poll(0) is None
    finally:
        worker.shutdown()
