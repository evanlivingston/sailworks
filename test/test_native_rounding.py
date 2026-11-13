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


def test_rounding_pass_is_reported_without_a_radius_gate():
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
    # The radius is inert metadata; the report is the measured closest pass.
    assert report["radius_nm"] == 0.5
    assert report["satisfied"] is True
    assert report["achieved_side"] == "port"
    assert report["passage_count"] == 1
    assert report["achieved_closest_distance_nm"] > 0.0


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


def test_tiny_legacy_radius_does_not_make_a_rounding_unsatisfiable():
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
    # The passing distance is a solver decision, so a legacy radius far below
    # the unambiguous pass distance is inert rather than unsatisfiable.
    assert run["accepted"], run["message"]
    report = run["roundings"][0]
    assert report["satisfied"] is True
    assert report["achieved_side"] == "port"
    assert report["passage_count"] == 1


def test_wide_correct_side_pass_is_accepted_without_a_detour():
    # A destination rounding is accepted however wide the pass is; distance is
    # never a gate, so the solver must not add legs to shrink it.
    rounding = {
        "side": "starboard",
        "radius_nm": 0.01,
        "exit_bearing_min_deg": None,
        "exit_bearing_max_deg": None,
    }
    run = _run({**SETTINGS, "roundings": [rounding]})
    assert run["accepted"], run["message"]
    report = run["roundings"][0]
    assert report["satisfied"] is True
    assert report["achieved_side"] == "starboard"
    assert report["passage_count"] == 1
    assert run["legs"]


def test_no_rounding_chain_is_byte_identical_across_runs():
    waypoint = (48.255, -123.60)
    settings = {
        **SETTINGS,
        "waypoints": [list(waypoint), list(SETTINGS["destination"])],
    }
    first = _run(settings)
    second = _run(settings)
    assert first["accepted"], first["message"]
    assert "roundings" not in first
    assert first["digest"] == second["digest"]
    assert first["legs"] == second["legs"]


def test_rounding_rejects_invalid_specs_before_dispatch():
    # The radius is inert metadata under the amended contract: zero, negative,
    # tiny and huge values are all accepted and never shape the route. The
    # native payload differs only in the echoed radius.
    payloads = []
    for radius in (0, -1, 0.001, 1000):
        run = _run(
            {
                **SETTINGS,
                "roundings": [
                    {
                        "side": "port",
                        "radius_nm": radius,
                        "exit_bearing_min_deg": None,
                        "exit_bearing_max_deg": None,
                    }
                ],
            }
        )
        assert run["accepted"], run["message"]
        report = dict(run["roundings"][0])
        assert report.pop("radius_nm") == float(radius)
        payloads.append((run["legs"], run["digest"], report))
    assert all(payload == payloads[0] for payload in payloads)

    # Genuine spec errors are still rejected before dispatch: an unknown side,
    # an inverted exit range, a range outside 0..360, and a half-specified
    # range (the parser requires both bounds).
    for rounding in (
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
            "exit_bearing_min_deg": -10,
            "exit_bearing_max_deg": 20,
        },
        {
            "side": "port",
            "radius_nm": 0.5,
            "exit_bearing_min_deg": 0,
            "exit_bearing_max_deg": 360,
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
    # The interior mark is judged over the concatenated arriving and departing
    # sub-track; the passing distance is a solver decision, not the radius.
    assert report["satisfied"] is True
    assert report["achieved_side"] == "port"
    assert report["passage_count"] == 1
    assert report["achieved_closest_distance_nm"] > 0.0
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


def test_native_payload_never_sends_the_radius_as_a_constraint():
    from sailworks.core.marine.nativerequest import Rounding

    for side in ("port", "starboard"):
        payload = Rounding(side, 2.5).to_native()
        assert "radius_nm" not in payload
        assert payload["side"] == side
        assert payload["exit_bearing_min_deg"] is None
        assert payload["exit_bearing_max_deg"] is None


def test_legacy_radius_metadata_still_round_trips():
    from sailworks.core.geo.rounding import normalize_rounding_spec

    spec = normalize_rounding_spec({"side": "port", "radius_nm": 2.5})
    assert spec["side"] == "port"
    assert spec["radius_nm"] == 2.5


def test_radius_no_longer_rejects_nearby_or_overlapping_marks():
    from types import SimpleNamespace

    from sailworks.core.marine.nativerequest import Rounding, _resolve_roundings

    start = (48.0, -123.0)
    near_mark = (48.0005, -123.0)
    in_radius = Rounding("port", 5.0, name="near")
    item = SimpleNamespace(
        start=None,
        waypoints=[start, near_mark],
        destination=near_mark,
        roundings=[None, in_radius],
    )
    assert _resolve_roundings(item, start, (near_mark,)) == (in_radius,)

    first_mark = (48.001, -123.0)
    second_mark = (48.002, -123.0)
    first = Rounding("port", 5.0, name="first")
    second = Rounding("starboard", 5.0, name="second")
    overlapping = SimpleNamespace(
        start=None,
        waypoints=[start, first_mark, second_mark],
        destination=second_mark,
        roundings=[None, first, second],
    )
    resolved = _resolve_roundings(overlapping, start, (first_mark, second_mark))
    assert resolved == (first, second)
