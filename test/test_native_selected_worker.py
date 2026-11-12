"""Spawned real-request jobs must not become synthetic fixture calculations."""

import json
import math
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

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


def test_missing_artifact_fails_without_fixture_fallback(tmp_path):
    with _forecast_inputs(tmp_path) as source:
        job = _job(tmp_path, _selected_forecast(tmp_path, source))
    worker = NativeRoutingWorker()
    try:
        job.forecast.artifact_paths[0].unlink()
        worker.submit(job)
        status = _wait(worker)
        assert status.state == "failed" and status.payload is None
        assert "missing or unsafe" in status.message
    finally:
        worker.shutdown()


def test_waypoint_chain_delivers_continuous_route_in_order(tmp_path):
    with _forecast_inputs(tmp_path) as source:
        original = _job(tmp_path, _selected_forecast(tmp_path, source))
    waypoint = (48.255, -123.60)
    job = replace(original, waypoints=(waypoint, original.destination))
    worker = NativeRoutingWorker()
    try:
        generation = worker.submit(job)
        status = _wait(worker)
        assert status.state == "completed", status.message
        run = status.payload
        assert run["accepted"], (run["status"], run["message"])
        assert len(run["segments"]) == 2
        assert all(segment["accepted"] for segment in run["segments"])
        assert run["segments"][-1]["independent_validation_passed"]
        assert run["input_fingerprints"]["waypoints"] == [
            list(waypoint),
            list(job.destination),
        ]
        legs = run["legs"]
        assert len(legs) >= 2
        for previous, following in zip(legs, legs[1:]):
            assert previous["end"] == following["start"]
            assert following["start_time"] >= previous["end_time"]
        # The first segment ends at the requested waypoint, and the second
        # segment departs from that delivered position.
        boundary = run["segments"][0]["leg_count"] - 1
        delivered = legs[boundary]["end"]
        distance_nm = (
            math.hypot(
                delivered["latitude"] - waypoint[0],
                (delivered["longitude"] - waypoint[1])
                * math.cos(math.radians(waypoint[0])),
            )
            * 60
        )
        assert distance_nm < 0.5
        assert legs[boundary + 1]["start"] == delivered
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
        assert result.outcome.validation.independent is True
        assert RoutingResult.from_dict(result.to_dict()).legs
    finally:
        worker.shutdown()


def _selected_shoreline(root):
    """Minimal installed-GSHHS layout the worker's shoreline adapter opens."""
    from osgeo import ogr

    directory = Path(root) / "gshhs"
    (directory / "GSHHS_shp" / "h").mkdir(parents=True)
    dataset = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(
        str(directory / "GSHHS_shp" / "h")
    )
    layer = dataset.CreateLayer("GSHHS_h_L1", geom_type=ogr.wkbPolygon)
    feature = ogr.Feature(layer.GetLayerDefn())
    feature.SetGeometry(
        ogr.CreateGeometryFromWkt(
            "POLYGON ((-123.638 48.262, -123.622 48.262, "
            "-123.622 48.276, -123.638 48.276, -123.638 48.262))"
        )
    )
    layer.CreateFeature(feature)
    dataset = None
    return directory


class SelectedChart:
    """Stands in for a Qt chart only at the descriptor boundary."""

    def __init__(self, path):
        self.path = str(path)

    def validity_descriptor(self):
        return ("gshhs", self.path)


def test_spawned_route_uses_selected_shoreline_and_clearance(tmp_path):
    from osgeo import ogr

    shoreline = _selected_shoreline(tmp_path)
    with _forecast_inputs(tmp_path) as source:
        binding = _selected_forecast(tmp_path, source)
    original = _job(tmp_path, binding)
    request = RouteRequest(
        name="selected-shoreline-request",
        start=original.start,
        destination=original.destination,
        departure=START + timedelta(minutes=30),
        polar_file=str(
            Path(__file__).resolve().parents[1] / "sailworks/data/polars/test.pol"
        ),
        algorithm=LegacyAlgorithm,
        coastline_checks=True,
        coastline_clearance_distance_m=370.0,
    )
    job = freeze_native_requests(
        request, (), binding, (SelectedChart(shoreline),), None, engine="main"
    )[0]
    worker = NativeRoutingWorker()
    try:
        worker.submit(job)
        status = _wait(worker)
        assert status.state == "completed", status.message
        run = status.payload
        assert run["accepted"], (run["status"], run["message"])
        assert run["request"]["land_safety_margin_nm"] == 370.0 / 1852.0
        assert run["diagnostics"]["land_checks"] > 0
        assert run["input_fingerprints"]["shoreline"] == [
            {"kind": "gshhs", "path": str(shoreline)}
        ]
        assert run["input_fingerprints"]["coastline_clearance_distance_m"] == 370.0
        # Every delivered leg must satisfy the same selected predicate.
        from sailworks.core.geo.shoreline import ShorelineValidity

        provider = ShorelineValidity(str(shoreline))
        for leg in run["legs"]:
            assert provider.lines_validity(
                [
                    (
                        leg["start"]["latitude"],
                        leg["start"]["longitude"],
                        leg["end"]["latitude"],
                        leg["end"]["longitude"],
                    )
                ],
                370.0,
            ) == [True]
        assert run["independent_validation"]["passed"]
    finally:
        worker.shutdown()


def test_scenario_jobs_keep_identity_and_modifiers_separate(tmp_path):
    from sailworks.core.routingoptions import RouteScenario, scenario_request

    with _forecast_inputs(tmp_path) as source:
        binding = _selected_forecast(tmp_path, source)
        base = _job(tmp_path, binding)
        request = RouteRequest(
            name="scenario-base",
            start=base.start,
            destination=base.destination,
            departure=datetime.fromtimestamp(base.departure_epoch_s, timezone.utc),
            polar_file=str(
                Path(__file__).resolve().parents[1] / "sailworks/data/polars/test.pol"
            ),
            algorithm=LegacyAlgorithm,
            coastline_checks=False,
        )
        scenarios = (
            scenario_request(request, RouteScenario()),
            scenario_request(
                request,
                RouteScenario(
                    wind_speed_pct=20,
                    wind_dir_offset=15,
                ),
            ),
            scenario_request(
                request,
                RouteScenario(polar_efficiency_pct=85),
            ),
        )
        jobs = freeze_native_requests(
            request, scenarios, binding, (), None, engine="main"
        )
    assert len(jobs) == 3
    assert len({job.calculation_id for job in jobs}) == 3

    runs = []
    worker = NativeRoutingWorker()
    try:
        for job in jobs:
            worker.submit(job)
            status = _wait(worker)
            assert status.state == "completed", status.message
            assert status.payload["accepted"], status.payload["message"]
            assert status.payload["calculation_id"] == job.calculation_id
            runs.append(status.payload)
    finally:
        worker.shutdown()

    assert len({run["calculation_id"] for run in runs}) == 3
    assert runs[1]["scenario"] == {
        "name": scenarios[1].name,
        "wind_speed_pct": 20,
        "wind_dir_offset": 15,
        "polar_efficiency": 1.0,
    }
    # Wind modifiers reach the sampled weather, not only the record.
    assert (
        runs[1]["legs"][0]["wind_east_knots"] != runs[0]["legs"][0]["wind_east_knots"]
    ) or (
        runs[1]["legs"][0]["wind_north_knots"] != runs[0]["legs"][0]["wind_north_knots"]
    )
    # Polar efficiency scales delivered boat speed. The tolerance allows the
    # search to choose a slightly different first leg under the lower speeds;
    # the exact per-sample scaling is asserted in the bridge checks.
    ratio = (
        runs[2]["legs"][0]["speed_through_water_knots"]
        / runs[0]["legs"][0]["speed_through_water_knots"]
    )
    assert 0.84 < ratio < 0.86
    assert runs[2]["scenario"]["polar_efficiency"] == 0.85


def test_diagnostic_result_is_saved_but_not_export_eligible(tmp_path):
    """A diagnostic native payload can be stored but never becomes exportable."""
    import sailworks_native_routing as bridge
    from sailworks.core.geo.routingcollection import RoutingCollection
    from sailworks.core.marine.routingresultstore import RoutingResultStore

    # Pre-cancelled fixture run: a diagnostic, never a user calculation.
    run = bridge.synthetic_cancelled_route("main")
    result = to_routing_result(
        run,
        calculation_id=str(uuid4()),
        generation=1,
        sequence=0,
        created_epoch_s=int(time.time()),
        identity=bridge.identity(),
    )
    assert export_blockers(result)
    collection = RoutingCollection(RoutingResultStore(tmp_path / "results"))
    route = collection.save_result("diagnostic", result)
    assert route.export_eligible is False
    assert collection.load_result(route).outcome.delivered is False
