"""The native input boundary must preserve every selected user setting."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from pickle import dumps, loads
import sys
from types import SimpleNamespace
from uuid import UUID

import pytest

from sailworks.core.marine.nativerequest import Rounding, freeze_native_requests
from sailworks.core.routingjob import RouteRequest
from sailworks.core.routingoptions import RouteScenario, scenario_request
from sailworks.core.tranchestore import ResolvedForecast


class Algorithm:
    pass


def _request(polar):
    return RouteRequest(
        name="selected-route",
        start=(48.2, -123.7),
        destination=(48.4, -123.5),
        departure=datetime(2026, 1, 1, tzinfo=timezone.utc),
        polar_file=str(polar),
        algorithm=Algorithm,
        coastline_checks=False,
        parameters=(),
        waypoints=((48.3, -123.6), (48.4, -123.5)),
        polar_efficiency=0.9,
        use_currents=True,
        current_priority="newest",
        wind_speed_pct=10,
        wind_dir_offset=12,
        tws_max_kn=24,
        min_sailing_speed_kn=3,
        motor_speed_kn=6,
        wave_correct=True,
    )


@pytest.fixture
def polar():
    return Path(__file__).resolve().parents[1] / "sailworks/data/polars/test.pol"


def _forecast():
    return ResolvedForecast(
        selection=SimpleNamespace(tranche_id="selected", entry_id="run"),
        tranche=SimpleNamespace(tranche_id="selected"),
        entry=SimpleNamespace(artifacts=(SimpleNamespace(sha256="abc"),)),
        artifact_paths=(Path("/local/selected/artifacts/abc"),),
    )


def test_all_selected_inputs_survive_spawn_serialization(polar):
    request = _request(polar)
    scenario = scenario_request(
        request,
        RouteScenario(
            time_offset_hours=6,
            wind_speed_pct=-20,
            wind_dir_offset=-15,
            polar_efficiency_pct=85,
        ),
    )
    selection = SimpleNamespace(
        fields=[SimpleNamespace(id="regional"), SimpleNamespace(id="global")],
        priority="newest",
    )
    jobs = freeze_native_requests(
        request,
        (scenario,),
        _forecast(),
        (),
        selection,
        marine_directory="/local/marine",
        engine="main",
    )
    (job,) = loads(dumps(jobs))
    assert job.version == 1
    assert job.engine == "main"
    assert str(UUID(job.calculation_id)) == job.calculation_id
    assert job.start == request.start
    assert job.destination == request.destination
    assert job.waypoints == request.waypoints
    assert job.departure_epoch_s == int(scenario.departure.timestamp())
    assert job.forecast.selection.entry_id == "run"
    assert job.current_field_ids == ("regional", "global")
    assert job.current_priority == "newest"
    assert job.polar_rows and len(job.polar_sha256) == 64
    assert job.polar_rows[0][0] == 4.0  # file-backed polar is already in knots
    assert job.polar_rows[0][1][0] == pytest.approx((30.0, 6.7))
    assert job.polar_efficiency == 0.85
    assert job.wind_speed_pct == -20
    assert job.wind_dir_offset == -15
    assert job.tws_max_kn == 24
    assert job.min_sailing_speed_kn == 3
    assert job.motor_speed_kn == 6
    assert job.wave_correct is True


def test_roundings_align_with_chain_and_drop_start_point(polar):
    rounding = Rounding("port", 0.5)
    request = replace(
        _request(polar),
        use_currents=False,
        start=None,
        waypoints=((48.3, -123.6), (48.35, -123.55), (48.4, -123.5)),
        roundings=(None, rounding, None),
    )
    (job,) = freeze_native_requests(request, (), _forecast(), (), None, engine="main")
    assert job.roundings == (rounding, None)


def test_rounding_on_start_or_skipped_duplicate_is_rejected(polar):
    base = replace(_request(polar), use_currents=False)
    with pytest.raises(ValueError, match="first waypoint is the start"):
        freeze_native_requests(
            replace(
                base,
                start=None,
                waypoints=((48.3, -123.6), (48.4, -123.5)),
                roundings=(Rounding("port", 0.5), None),
            ),
            (),
            _forecast(),
            (),
            None,
            engine="main",
        )
    with pytest.raises(ValueError, match="duplicate waypoint"):
        freeze_native_requests(
            replace(
                base,
                waypoints=((48.3, -123.6), (48.3, -123.6), (48.4, -123.5)),
                roundings=(None, Rounding("port", 0.5), None),
            ),
            (),
            _forecast(),
            (),
            None,
            engine="main",
        )


def test_legacy_radius_no_longer_rejects_containment_or_overlap(polar):
    # Radius is inert metadata: a previous point inside it and two overlapping
    # legacy radii are no longer planning constraints, so freezing succeeds.
    first = Rounding("port", 5.0)
    second = Rounding("starboard", 5.0)
    request = replace(
        _request(polar),
        use_currents=False,
        waypoints=((48.3005, -123.6), (48.301, -123.6), (48.4, -123.5)),
        roundings=(first, second, None),
    )
    (job,) = freeze_native_requests(request, (), _forecast(), (), None, engine="main")
    assert job.roundings == (first, second, None)
    for rounding in (first, second):
        payload = rounding.to_native()
        assert "radius_nm" not in payload
        assert payload["side"] == rounding.side


def test_rounding_name_survives_freeze_and_native_settings(polar):
    rounding = Rounding("port", 0.5, name="Duntze Rock")
    request = replace(
        _request(polar),
        use_currents=False,
        roundings=(rounding, None),
    )
    (job,) = freeze_native_requests(request, (), _forecast(), (), None, engine="main")
    assert job.roundings[0].name == "Duntze Rock"
    assert job.roundings[0].to_native()["name"] == "Duntze Rock"


def test_rounding_report_gets_the_mark_name():
    from types import SimpleNamespace

    from sailworks.core.marine.nativeexecution import _attach_rounding_names

    mark = (48.255, -123.6)
    job = SimpleNamespace(
        start=(48.25, -123.65),
        waypoints=(mark,),
        destination=mark,
        roundings=(Rounding("starboard", 0.15, name="Duntze Rock"),),
    )
    run = {
        "roundings": [
            {
                "mark": {"latitude": mark[0], "longitude": mark[1]},
                "side": "starboard",
                "radius_nm": 0.15,
                "satisfied": True,
                "failure_code": "none",
            }
        ]
    }
    _attach_rounding_names(run, job)
    assert run["roundings"][0]["mark_name"] == "Duntze Rock"


def test_rounding_requires_the_native_engine(polar):
    request = replace(
        _request(polar),
        use_currents=False,
        roundings=(Rounding("port", 0.5), None),
    )
    with pytest.raises(ValueError, match="main or professional"):
        freeze_native_requests(request, (), _forecast(), (), None, engine="standard")


def test_unknown_shoreline_provider_is_not_silently_dropped(polar):
    request = replace(_request(polar), use_currents=False, land_avoidance=True)
    with pytest.raises(ValueError, match="shoreline provider"):
        freeze_native_requests(
            request, (), _forecast(), (object(),), None, engine="main"
        )


def test_missing_source_and_start_are_explicit(polar):
    request = _request(polar)
    with pytest.raises(ValueError, match="selected local fields"):
        freeze_native_requests(
            request, (), _forecast(), (), None, "/local/marine", engine="main"
        )
    with pytest.raises(ValueError, match="explicit start"):
        freeze_native_requests(
            replace(request, start=None, waypoints=(), use_currents=False),
            (),
            _forecast(),
            (),
            None,
            engine="main",
        )


def test_first_waypoint_start_matches_the_legacy_track_workflow(polar):
    request = replace(
        _request(polar), start=None, use_currents=False, waypoints=((48.3, -123.6),)
    )
    with pytest.raises(ValueError, match="explicit start"):
        freeze_native_requests(request, (), _forecast(), (), None, engine="main")

    chain = ((48.3, -123.6), (48.4, -123.5))
    (job,) = freeze_native_requests(
        replace(request, waypoints=chain), (), _forecast(), (), None, engine="main"
    )

    # "First waypoint" means the track's first point is the start, exactly as
    # the legacy track route treats it, so it is not repeated in the chain.
    assert job.start == chain[0]
    assert job.waypoints == chain[1:]
    assert job.destination == chain[-1]


def test_legacy_algorithm_parameters_cannot_be_ignored(polar):
    request = replace(_request(polar), parameters=(("legacy_step", 5),))
    with pytest.raises(ValueError, match="changed legacy algorithm parameters"):
        freeze_native_requests(
            request,
            (),
            _forecast(),
            (),
            SimpleNamespace(fields=(SimpleNamespace(id="field"),), priority="newest"),
            "/local/marine",
            engine="main",
        )


def test_alternatives_have_independent_calculation_identities(polar):
    request = replace(_request(polar), use_currents=False)
    early = scenario_request(request, RouteScenario(time_offset_hours=0))
    later = scenario_request(request, RouteScenario(time_offset_hours=3))
    jobs = freeze_native_requests(
        request, (early, later), _forecast(), (), None, engine="original"
    )
    assert len(jobs) == 2
    assert [job.engine for job in jobs] == ["original", "original"]
    assert jobs[0].calculation_id != jobs[1].calculation_id
    assert jobs[1].departure_epoch_s - jobs[0].departure_epoch_s == 3 * 3600


def _panel_algorithm_and_parameters():
    """Mirror RoutingPanel: the selected class plus every PARAMS widget value."""
    import weatherrouting

    algorithm = next(
        entry["class"]
        for entry in weatherrouting.list_routing_algorithms()
        if entry["name"] == "LinearBestIsoRouter"
    )
    parameters = tuple(
        (key, parameter.value) for key, parameter in algorithm.PARAMS.items()
    )
    return algorithm, parameters


def test_default_panel_request_is_accepted_and_records_unapplied_defaults(polar):
    """The panel always sends its algorithm PARAMS; defaults must not block it."""
    from datetime import datetime, timezone

    algorithm, parameters = _panel_algorithm_and_parameters()
    assert len(parameters) == 3  # subdiv, concurrent, min_increase
    request = RouteRequest(
        name="routing-test",
        start=(48.2, -123.7),
        destination=(48.4, -123.5),
        departure=datetime(2026, 1, 1, tzinfo=timezone.utc),
        polar_file=str(polar),
        algorithm=algorithm,
        coastline_checks=False,
        parameters=parameters,
    )
    (job,) = freeze_native_requests(request, (), _forecast(), (), None, engine="main")
    assert job.legacy_algorithm == algorithm.__name__
    assert dict(job.unchanged_legacy_parameters) == dict(parameters)


def test_changed_panel_parameter_is_rejected_with_its_name(polar):
    from datetime import datetime, timezone

    algorithm, parameters = _panel_algorithm_and_parameters()
    changed = tuple(
        (key, 42.0 if key == "min_increase" else value) for key, value in parameters
    )
    request = RouteRequest(
        name="routing-test",
        start=(48.2, -123.7),
        destination=(48.4, -123.5),
        departure=datetime(2026, 1, 1, tzinfo=timezone.utc),
        polar_file=str(polar),
        algorithm=algorithm,
        coastline_checks=False,
        parameters=changed,
    )
    with pytest.raises(ValueError, match="min_increase"):
        freeze_native_requests(request, (), _forecast(), (), None, engine="main")


def test_unknown_parameter_is_rejected(polar):
    algorithm, _ = _panel_algorithm_and_parameters()
    request = replace(
        _request(polar),
        algorithm=algorithm,
        use_currents=False,
        parameters=(("not_a_parameter", 1),),
    )
    with pytest.raises(ValueError, match="not_a_parameter"):
        freeze_native_requests(request, (), _forecast(), (), None, engine="main")


def test_naive_local_departure_is_interpreted_like_the_legacy_panel(polar):
    local = datetime(2026, 1, 1, 9, 30)
    (job,) = freeze_native_requests(
        replace(_request(polar), departure=local, use_currents=False),
        (),
        _forecast(),
        (),
        None,
        engine="main",
    )

    # The panel's departure has no offset and legacy routes read it as local
    # wall time, so native attaches the local offset instead of refusing.
    assert job.departure_epoch_s == int(local.astimezone().timestamp())


def _wave_forecast(
    *,
    fields=("wave_height", "wave_period", "wave_direction"),
    region=(40, -130, 50, -120),
    start=None,
    end=None,
):
    wind = _forecast()
    start = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = end or datetime(2026, 1, 1, 6, tzinfo=timezone.utc)
    entry = SimpleNamespace(
        entry_id="wave",
        model_id="GFSWAVE",
        member="deterministic",
        artifacts=(SimpleNamespace(sha256="wave-sha"),),
        fields=fields,
        field_times=tuple((field, (start, end)) for field in fields),
        region=region,
        run_time=start,
    )
    return ResolvedForecast(
        selection=SimpleNamespace(tranche_id="selected", entry_id="wave"),
        tranche=wind.tranche,
        entry=entry,
        artifact_paths=(Path("/local/selected/artifacts/wave-sha"),),
    )


def test_wave_selection_is_frozen_with_its_binding(polar):
    request = replace(_request(polar), use_currents=False, wave_entry_id="wave")
    wave = _wave_forecast()
    (job,) = freeze_native_requests(
        request,
        (),
        _forecast(),
        (),
        None,
        engine="main",
        wave_forecast=wave,
    )
    restored = loads(dumps(job))
    assert restored.wave_forecast.entry.model_id == "GFSWAVE"
    assert restored.wave_forecast.entry.entry_id == "wave"


def test_wave_selection_requires_height_period_and_direction(polar):
    request = replace(_request(polar), use_currents=False, wave_entry_id="wave")
    wave = _wave_forecast(fields=("wave_height", "wave_period"))
    with pytest.raises(ValueError, match="wave_direction"):
        freeze_native_requests(
            request,
            (),
            _forecast(),
            (),
            None,
            engine="main",
            wave_forecast=wave,
        )


def test_wave_selection_must_cover_the_route_window(polar):
    request = replace(_request(polar), use_currents=False, wave_entry_id="wave")
    wave = _wave_forecast(
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    with pytest.raises(ValueError, match="does not cover the route window"):
        freeze_native_requests(
            request,
            (),
            _forecast(),
            (),
            None,
            engine="main",
            wave_forecast=wave,
        )


def test_wave_selection_cannot_change_between_scenarios(polar):
    request = replace(_request(polar), use_currents=False, wave_entry_id="wave")
    scenario = replace(request, wave_entry_id=None)
    with pytest.raises(ValueError, match="frozen source selection"):
        freeze_native_requests(
            request,
            (scenario,),
            _forecast(),
            (),
            None,
            engine="main",
            wave_forecast=_wave_forecast(),
        )


def _wind_routable_forecast():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    wind = _forecast()
    entry = SimpleNamespace(
        entry_id="wind",
        model_id="GFS",
        member="deterministic",
        artifacts=wind.entry.artifacts,
        field_times=(("wind", (start, start.replace(hour=6))),),
        region=(40, -130, 50, -120),
        run_time=start,
    )
    return ResolvedForecast(
        selection=wind.selection,
        tranche=wind.tranche,
        entry=entry,
        artifact_paths=wind.artifact_paths,
    )


class _FakeWind:
    def __init__(self, *_args):
        self.closed = False

    def get_wind_at(self, instant, latitude, longitude):
        return (270.0, 8.0)

    def close(self):
        self.closed = True


class _FakeWaves:
    def __init__(self, *_args):
        self.closed = False

    def coverage(self):
        return {
            "south": 40.0,
            "west": -130.0,
            "north": 50.0,
            "east": -120.0,
            "start_epoch_s": 1767225600,
            "end_epoch_s": 1767247200,
            "dataset_identity": "GFSWAVE:wave-sha",
        }

    def get_wave_at(self, instant, latitude, longitude):
        return (1.4, 8.0, 250.0, True, "GFSWAVE:wave-sha", "GFSWAVE")

    def close(self):
        self.closed = True


def test_native_execution_passes_wave_sampler_and_coverage_to_the_bridge(
    polar, monkeypatch
):
    from sailworks.core.marine import nativeexecution

    request = replace(_request(polar), use_currents=False, wave_entry_id="wave")
    (job,) = freeze_native_requests(
        request,
        (),
        _wind_routable_forecast(),
        (),
        None,
        engine="main",
        wave_forecast=_wave_forecast(),
    )
    captured = {}

    def route_user_request(
        wind, current, coverage, polar_rows, settings, *args, **kwargs
    ):
        captured.update(settings=settings, kwargs=kwargs)
        return {"status": "complete"}

    bridge = SimpleNamespace(
        route_user_request=route_user_request,
        identity=lambda: {
            "source": {
                "repository": "test",
                "commit": "0" * 40,
                "tree_sha256": "a" * 64,
            },
            "versions": {"engine": "test"},
        },
    )
    monkeypatch.setitem(sys.modules, "sailworks_native_routing", bridge)
    monkeypatch.setattr(nativeexecution, "BoundRoutingWind", _FakeWind)
    monkeypatch.setattr(nativeexecution, "BoundRoutingWaves", _FakeWaves)

    run = nativeexecution.run_native_request(job)
    assert run["status"] == "complete"
    assert captured["settings"]["use_waves"] is True
    assert captured["kwargs"]["wave_coverage"]["dataset_identity"] == (
        "GFSWAVE:wave-sha"
    )
    sample = captured["kwargs"]["waves"](48.0, -123.0, 1767225600)
    assert sample == (1.4, 8.0, 250.0, True, "GFSWAVE:wave-sha", "GFSWAVE")
    assert run["input_fingerprints"]["wave"]["model_id"] == "GFSWAVE"


def test_native_execution_turns_wave_coverage_failure_into_an_explicit_error(
    polar, monkeypatch
):
    from sailworks.core.marine import nativeexecution

    request = replace(_request(polar), use_currents=False, wave_entry_id="wave")
    (job,) = freeze_native_requests(
        request,
        (),
        _wind_routable_forecast(),
        (),
        None,
        engine="main",
        wave_forecast=_wave_forecast(),
    )

    def route_user_request(*_args, **_kwargs):
        return {
            "status": "wave_data_required",
            "wave_provenance": {
                "dataset_identity": "GFSWAVE:wave-sha",
                "coverage": {"begins": 1767225600, "ends": 1767229200},
            },
        }

    bridge = SimpleNamespace(
        route_user_request=route_user_request,
        identity=lambda: {},
    )
    monkeypatch.setitem(sys.modules, "sailworks_native_routing", bridge)
    monkeypatch.setattr(nativeexecution, "BoundRoutingWind", _FakeWind)
    monkeypatch.setattr(nativeexecution, "BoundRoutingWaves", _FakeWaves)

    with pytest.raises(ValueError, match="wind proxy was not substituted"):
        nativeexecution.run_native_request(job)
