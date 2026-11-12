"""Native mark rounding constraints."""

import math
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD_PYTHON = Path(
    os.environ.get(
        "SAILWORKS_NATIVE_ROUTING_BUILD",
        str(ROOT / "build/native-routing/python"),
    )
)

START = 1767225600
COVERAGE = {
    "south": 48,
    "north": 50,
    "west": -124,
    "east": -122,
    "start_epoch_s": START,
    "end_epoch_s": START + 6 * 3600,
}
POLAR = [
    {"true_wind_speed_knots": speed, "points": [[30, 3], [90, 3], [180, 3]]}
    for speed in (4, 8, 12, 16)
]
SETTINGS = {
    "start": (48.25, -123.65),
    "destination": (48.29, -123.61),
    "departure_epoch_s": START + 1800,
    "use_currents": False,
    "polar_efficiency": 1.0,
    "tws_max_kn": None,
    "min_sailing_speed_kn": None,
    "motor_speed_kn": 5.0,
    "wave_correct": False,
    "use_waves": False,
    "coastline_checks": False,
    "land_safety_margin_nm": 0.0,
    "waypoints": [],
    "roundings": [],
}


def _bridge():
    if not BUILD_PYTHON.is_dir():
        pytest.fail(
            "native routing module build directory is missing: "
            f"{BUILD_PYTHON}. Configure and build it before running this test."
        )
    sys.path.insert(0, str(BUILD_PYTHON))
    import sailworks_native_routing

    return sailworks_native_routing


def _wind(latitude, longitude, epoch_s):
    return (0.0, 8.0, True)


def _missing_current(latitude, longitude, epoch_s):
    return (0.0, 0.0, False)


def _run(settings):
    return _bridge().route_user_request(
        _wind,
        _missing_current,
        COVERAGE,
        POLAR,
        settings,
        None,
        "selected-forecast",
        "selected-polar",
        "main",
    )


def _distance_nm(left, right):
    latitude1, longitude1 = map(math.radians, left)
    latitude2, longitude2 = map(math.radians, right)
    dlat = latitude2 - latitude1
    dlon = longitude2 - longitude1
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(latitude1) * math.cos(latitude2) * math.sin(dlon / 2) ** 2
    )
    return 3440.065 * 2 * math.asin(math.sqrt(a))


def test_rounding_endpoint_is_within_radius_and_reported():
    rounding = {
        "side": "port",
        "radius_nm": 0.5,
        "exit_bearing_min_deg": None,
        "exit_bearing_max_deg": None,
    }
    run = _run({**SETTINGS, "roundings": [rounding]})
    assert run["accepted"], run["message"]
    assert run["roundings"], run
    report = run["roundings"][0]
    assert report["side"] == "port"
    assert report["radius_nm"] == 0.5
    assert report["satisfied"] is True
    endpoint = (run["legs"][-1]["end"]["latitude"], run["legs"][-1]["end"]["longitude"])
    assert _distance_nm(endpoint, SETTINGS["destination"]) <= 0.5 + 1e-3


def test_rounding_exit_range_is_enforced_and_both_sides_are_available():
    port = _run(
        {
            **SETTINGS,
            "roundings": [
                {
                    "side": "port",
                    "radius_nm": 0.5,
                    "exit_bearing_min_deg": 30,
                    "exit_bearing_max_deg": 60,
                }
            ],
        }
    )
    starboard = _run(
        {
            **SETTINGS,
            "roundings": [
                {
                    "side": "starboard",
                    "radius_nm": 0.5,
                    "exit_bearing_min_deg": 30,
                    "exit_bearing_max_deg": 60,
                }
            ],
        }
    )
    assert port["accepted"], port["message"]
    assert starboard["accepted"], starboard["message"]
    port_report = port["roundings"][0]
    starboard_report = starboard["roundings"][0]
    assert 30 <= port_report["achieved_terminal_course_deg"] <= 60
    assert 30 <= starboard_report["achieved_terminal_course_deg"] <= 60
    assert port_report["achieved_terminal_bearing_deg"] != pytest.approx(
        starboard_report["achieved_terminal_bearing_deg"]
    )


def test_rounding_runs_are_deterministic():
    settings = {
        **SETTINGS,
        "roundings": [
            {
                "side": "port",
                "radius_nm": 0.5,
                "exit_bearing_min_deg": 30,
                "exit_bearing_max_deg": 60,
            }
        ],
    }
    first = _run(settings)
    second = _run(settings)
    assert first["accepted"], first["message"]
    assert first["digest"] == second["digest"]
    assert first["legs"] == second["legs"]
    assert first["metrics"] == second["metrics"]


def test_rounding_too_small_for_an_unambiguous_side_is_unsatisfiable():
    run = _run(
        {
            **SETTINGS,
            "roundings": [
                {
                    "side": "port",
                    "radius_nm": 0.005,
                    "exit_bearing_min_deg": None,
                    "exit_bearing_max_deg": None,
                }
            ],
        }
    )
    assert run["accepted"] is False
    assert run["status"] == "unsatisfiable_rounding"
    assert "side_unreachable" in run["message"]
    assert run["diagnostics"]["rounding_attempted"] is True


def test_rounding_rejects_invalid_specs_before_dispatch():
    for rounding in (
        {"side": "port", "radius_nm": 0.0},
        {"side": "east", "radius_nm": 0.5},
        {
            "side": "port",
            "radius_nm": 0.5,
            "exit_bearing_min_deg": 300,
            "exit_bearing_max_deg": 20,
        },
        {
            "side": "port",
            "radius_nm": 0.5,
            "exit_bearing_min_deg": 20,
        },
    ):
        with pytest.raises(ValueError):
            _run({**SETTINGS, "roundings": [rounding]})


def test_mixed_chain_rounds_only_the_requested_mark():
    waypoint = (48.255, -123.60)
    run = _run(
        {
            **SETTINGS,
            "waypoints": [list(waypoint), list(SETTINGS["destination"])],
            "roundings": [
                {
                    "side": "port",
                    "radius_nm": 0.15,
                    "exit_bearing_min_deg": None,
                    "exit_bearing_max_deg": None,
                },
                None,
            ],
        }
    )
    assert run["accepted"], run["message"]
    assert len(run["roundings"]) == 1
    report = run["roundings"][0]
    assert report["mark"] == {
        "latitude": waypoint[0],
        "longitude": waypoint[1],
    }
    first_leg_end = run["legs"][0]["end"]
    assert (
        _distance_nm((first_leg_end["latitude"], first_leg_end["longitude"]), waypoint)
        <= 0.15 + 1e-3
    )
    destination = run["legs"][-1]["end"]
    assert (
        _distance_nm(
            (destination["latitude"], destination["longitude"]),
            SETTINGS["destination"],
        )
        <= 1e-6
    )


def test_no_rounding_keeps_empty_roundings_payload():
    run = _run(SETTINGS)
    assert run["accepted"], run["message"]
    assert "roundings" not in run
