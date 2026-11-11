"""The native input boundary must preserve every selected user setting."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from pickle import dumps, loads
from types import SimpleNamespace

import pytest

from sailworks.core.marine.nativerequest import freeze_native_requests
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
        parameters=(("step", 5),),
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
    )
    (job,) = loads(dumps(jobs))
    assert job.version == 1
    assert job.start == request.start
    assert job.destination == request.destination
    assert job.waypoints == request.waypoints
    assert job.departure_epoch_s == int(scenario.departure.timestamp())
    assert job.forecast.selection.entry_id == "run"
    assert job.current_field_ids == ("regional", "global")
    assert job.current_priority == "newest"
    assert job.polar_rows and len(job.polar_sha256) == 64
    assert job.polar_efficiency == 0.85
    assert job.wind_speed_pct == -20
    assert job.wind_dir_offset == -15
    assert job.tws_max_kn == 24
    assert job.min_sailing_speed_kn == 3
    assert job.motor_speed_kn == 6
    assert job.wave_correct is True
    assert job.algorithm == "Algorithm"
    assert job.parameters == (("step", 5),)


def test_unknown_shoreline_provider_is_not_silently_dropped(polar):
    request = replace(_request(polar), use_currents=False, land_avoidance=True)
    with pytest.raises(ValueError, match="shoreline provider"):
        freeze_native_requests(request, (), _forecast(), (object(),), None)


def test_missing_source_and_start_are_explicit(polar):
    request = _request(polar)
    with pytest.raises(ValueError, match="selected local fields"):
        freeze_native_requests(request, (), _forecast(), (), None, "/local/marine")
    with pytest.raises(ValueError, match="explicit start"):
        freeze_native_requests(
            replace(request, start=None, use_currents=False), (), _forecast(), (), None
        )
