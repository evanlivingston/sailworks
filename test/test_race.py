# -*- coding: utf-8 -*-
"""Tests for race review import and weather request logic."""

import json
from datetime import datetime, timezone

from gweatherrouting.core.race import (
    parse_race,
    parse_yb_race,
    weather_download_request,
)


def _sample_race():
    return {
        "race": "sample-race",
        "name": "Sample Race",
        "start": 1700000000,
        "end": 1700036000,
        "teams": [
            {
                "id": 1,
                "name": "Alpha",
                "sail": "A1",
                "model": "Foo",
                "status": "RACING",
            },
            {"id": 2, "name": "Bravo", "sail": "B2", "status": "FINISHED"},
        ],
        "positions": [
            {
                "id": 1,
                "moments": [
                    {"at": 1700000000, "lat": 33.0, "lon": -117.0, "speed": 5.2},
                    {"at": 1700003600, "lat": 33.1, "lon": -117.1, "speed": 6.0},
                ],
            },
            {
                "id": 2,
                "moments": [
                    {"at": 1700000000, "lat": 33.05, "lon": -117.05},
                    {"at": 1700036000, "lat": 33.2, "lon": -117.2},
                ],
            },
        ],
        "poi": {
            "lines": [
                {
                    "name": "Course",
                    "nodes": "33.0,-117.0,33.2,-117.2",
                }
            ]
        },
    }


def test_parse_race_basic():
    race = parse_race(_sample_race())
    assert race.race_id == "sample-race"
    assert race.name == "Sample Race"
    assert len(race.boats) == 2
    assert race.boats[0].name == "Alpha"
    assert len(race.boats[0].positions) == 2
    assert race.boats[0].positions[0].speed == 5.2
    assert race.start_time == datetime(
        2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc
    ).replace(tzinfo=None)
    assert race.end_time is not None


def test_parse_race_from_json_string():
    race = parse_yb_race(json.dumps(_sample_race()))
    assert race.race_id == "sample-race"
    assert race.duration_hours == 10.0


def test_course_and_bounds():
    race = parse_race(_sample_race())
    assert len(race.course) == 1
    assert race.course[0].points
    bounds = race.bounds()
    assert bounds is not None
    south, west, north, east = bounds
    assert south <= 33.0
    assert north >= 33.2
    assert west <= -117.2
    assert east >= -117.0


def test_positions_and_track_points():
    race = parse_race(_sample_race())
    boat = race.boat(1)
    assert boat is not None
    assert boat.track_points() == [(33.0, -117.0), (33.1, -117.1)]
    when = race.start_time
    assert race.positions_at(when)[1].latitude == 33.0


def test_weather_request():
    race = parse_race(_sample_race())
    request = weather_download_request(
        race, models=["GFS", "HRRR"], fields=("wind", "pressure")
    )
    assert request["models"] == ["GFS", "HRRR"]
    assert request["fields"] == ("wind", "pressure")
    assert request["duration_hours"] == 10.0
    region = request["region"]
    assert region is not None
    south, west, north, east = region
    assert south < 33.0
    assert north > 33.2
    assert request["start"] == race.start_time
    assert request["end"] == race.end_time


def test_time_range_aliases():
    race = parse_race(_sample_race())
    assert race.time_range()[0] == race.start_time


def test_raceboat_position_at_duplicate_timestamps():
    from gweatherrouting.core.race import RaceBoat, RacePosition
    from datetime import datetime

    positions = [
        RacePosition(datetime(2024, 1, 1, 0, 0), 1.0, 1.0),
        RacePosition(datetime(2024, 1, 1, 1, 0), 2.0, 2.0),
        RacePosition(datetime(2024, 1, 1, 1, 0), 3.0, 3.0),
        RacePosition(datetime(2024, 1, 1, 2, 0), 4.0, 4.0),
    ]
    boat = RaceBoat(boat_id=1, positions=positions)
    # before first
    assert boat.position_at(datetime(2023, 12, 31, 23, 59)) is None
    # exact first
    assert boat.position_at(datetime(2024, 1, 1, 0, 0)).latitude == 1.0
    # between duplicates: returns last duplicate (bisect_right semantics)
    assert boat.position_at(datetime(2024, 1, 1, 1, 0)).latitude == 3.0
    # after last duplicate before next
    assert boat.position_at(datetime(2024, 1, 1, 1, 30)).latitude == 3.0
    # exact last
    assert boat.position_at(datetime(2024, 1, 1, 2, 0)).latitude == 4.0
    # index semantics
    assert boat.position_index_at(datetime(2024, 1, 1, 1, 0)) == 2


import pytest

from gweatherrouting.core.race import (
    YBRaceError,
    fetch_yb_positions,
    download_yb_race,
    normalize_yb_race_slug,
    load_saved_races,
)


class _FakeResponse:
    def __init__(self, status_code=200, content=b"", reason="OK"):
        self.status_code = status_code
        self.reason = reason
        self._content = content

    def iter_content(self, chunk_size=65536):
        yield self._content

    def close(self):
        pass

    def raise_for_status(self):
        if self.status_code is not None and self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, timeout=None, stream=None, allow_redirects=True, **kwargs):
        merged = dict(kwargs)
        merged["allow_redirects"] = allow_redirects
        self.calls.append((url, timeout, stream, merged))
        return self.response


def _valid_race_payload():
    return {
        "race": "abc",
        "name": "Downloaded Race",
        "teams": [{"id": 1, "name": "Boat"}],
        "positions": [
            {
                "id": 1,
                "moments": [
                    {"gpsAt": 1700000000, "lat": 33.0, "lon": -117.0},
                    {"gpsAt": 1700003600000, "lat": 33.1, "lon": -117.1},
                ],
            }
        ],
    }


def test_gpsAt_utc_seconds_and_milliseconds():
    payload = _valid_race_payload()
    from gweatherrouting.core.race import parse_race

    race = parse_race(payload)
    boat = race.boat(1)
    assert boat is not None
    assert len(boat.positions) == 2
    assert boat.positions[0].timestamp == datetime(2023, 11, 14, 22, 13, 20)
    assert boat.positions[1].timestamp == datetime(2023, 11, 14, 23, 13, 20)
    # epoch is interpreted as UTC, not local time.
    assert boat.positions[0].epoch == 1700000000


def test_gpsAt_utc_timezone_offset():
    payload = {
        "race": "utc-race",
        "name": "UTC Race",
        "teams": [{"id": 1, "name": "Boat"}],
        "positions": [
            {
                "id": 1,
                "moments": [
                    {"gpsAt": "2023-11-14T22:13:20+02:00", "lat": 33.0, "lon": -117.0}
                ],
            }
        ],
    }
    from gweatherrouting.core.race import parse_race

    race = parse_race(payload)
    pos = race.boat(1).positions[0]
    assert pos.timestamp == datetime(2023, 11, 14, 20, 13, 20)
    assert pos.epoch == int(
        datetime(2023, 11, 14, 20, 13, 20, tzinfo=timezone.utc).timestamp()
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "a/b",
        "https://example.com/abc",
        "http://yb.tl/abc",
        "https://yb.tl/a/b",
        "https://yb.tl/abc?x=1",
        "https://yb.tl/abc#frag",
        "https://yb.tl.evil.test/abc",
        "https://evil.yb.tl/abc",
        "../secret",
        "abc def",
        "<script>",
    ],
)
def test_normalize_yb_slug_invalid(value):
    assert normalize_yb_race_slug(value) is None


@pytest.mark.parametrize("value", ["abc-123", "rmsr2014", "https://yb.tl/abc-123"])
def test_normalize_yb_slug_valid(value):
    slug = normalize_yb_race_slug(value)
    assert slug is not None
    assert slug == "abc-123" if "abc-123" in value else slug == value


def test_fetch_http_failure():
    response = _FakeResponse(status_code=404, content=b"", reason="Not Found")
    session = _FakeSession(response)
    with pytest.raises(YBRaceError, match="HTTP 404"):
        fetch_yb_positions("abc", session=session)
    assert session.calls


def test_fetch_raise_for_status_failure():
    class _RaisingResponse(_FakeResponse):
        def raise_for_status(self):
            raise RuntimeError("boom status")

    response = _RaisingResponse(status_code=200, content=b"{}")
    session = _FakeSession(response)
    with pytest.raises(YBRaceError, match="boom status"):
        fetch_yb_positions("abc", session=session)


def test_fetch_oversized_response():
    content = b"x" * 100
    response = _FakeResponse(content=content)
    session = _FakeSession(response)
    with pytest.raises(YBRaceError, match="exceeds maximum"):
        fetch_yb_positions("abc", session=session, max_bytes=10)


def test_fetch_invalid_json():
    response = _FakeResponse(content=b"not json")
    session = _FakeSession(response)
    with pytest.raises(YBRaceError, match="not valid JSON"):
        fetch_yb_positions("abc", session=session)


def test_fetch_empty_response():
    response = _FakeResponse(content=b"")
    session = _FakeSession(response)
    with pytest.raises(YBRaceError, match="empty response"):
        fetch_yb_positions("abc", session=session)


def test_fetch_invalid_slug_no_network():
    session = _FakeSession(_FakeResponse(content=b"{}"))
    with pytest.raises(YBRaceError):
        fetch_yb_positions("bad/slug", session=session)
    assert not session.calls


def test_download_success_persistence_and_reload(tmp_path):
    payload = _valid_race_payload()
    response = _FakeResponse(content=json.dumps(payload).encode("utf-8"))
    session = _FakeSession(response)
    race = download_yb_race("abc", session=session, directory=tmp_path)
    assert race.name == "Downloaded Race"
    assert len(race.boats) == 1
    assert len(race.boats[0].positions) == 2
    path = tmp_path / "abc.json"
    assert path.exists()
    reloaded = load_saved_races(tmp_path)
    assert len(reloaded) == 1
    assert reloaded[0].race_id == race.race_id
    assert (
        reloaded[0].boats[0].positions[0].timestamp
        == race.boats[0].positions[0].timestamp
    )


def test_download_failure_preserves_previous_file(tmp_path):
    path = tmp_path / "abc.json"
    previous = {"race": "old", "name": "Old Race", "teams": []}
    path.write_text(json.dumps(previous), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    response = _FakeResponse(status_code=500, content=b"")
    session = _FakeSession(response)
    with pytest.raises(YBRaceError):
        download_yb_race("abc", session=session, directory=tmp_path)
    assert path.read_text(encoding="utf-8") == before


def test_fetch_disables_redirects():
    response = _FakeResponse(status_code=200, content=b"{}")
    session = _FakeSession(response)
    fetch_yb_positions("abc", session=session)
    assert session.calls
    assert len(session.calls) == 1
    _url, _timeout, _stream, kwargs = session.calls[0]
    assert kwargs.get("allow_redirects") is False


def test_fetch_rejects_redirect():
    response = _FakeResponse(status_code=302, content=b"", reason="Found")
    session = _FakeSession(response)
    with pytest.raises(YBRaceError, match="302"):
        fetch_yb_positions("abc", session=session)
    assert len(session.calls) == 1


def test_raceposition_epoch_non_utc_timezone():
    import os
    import time

    from gweatherrouting.core.race import RacePosition

    old_tz = os.environ.get("TZ")
    try:
        if hasattr(time, "tzset"):
            os.environ["TZ"] = "America/New_York"
            time.tzset()
        position = RacePosition(
            timestamp=datetime(2023, 11, 14, 22, 13, 20),
            latitude=33.0,
            longitude=-117.0,
        )
        assert position.epoch == 1700000000
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        if hasattr(time, "tzset"):
            time.tzset()


def test_fetch_does_not_follow_redirect_to_other_host():
    redirected_host = "evil.example"

    class _RedirectingSession:
        def __init__(self):
            self.calls = []

        def get(self, url, timeout=None, stream=None, allow_redirects=True, **kwargs):
            merged = dict(kwargs)
            merged["allow_redirects"] = allow_redirects
            self.calls.append((url, timeout, stream, merged))
            if allow_redirects:
                # Simulate requests following a redirect to a different host.
                redirected_url = "https://evil.example/secret"
                self.calls.append((redirected_url, timeout, stream, merged))
                return _FakeResponse(status_code=200, content=b"{}")
            return _FakeResponse(status_code=302, content=b"", reason="Found")

    session = _RedirectingSession()
    with pytest.raises(YBRaceError, match="302"):
        fetch_yb_positions("abc", session=session)
    assert len(session.calls) == 1
    assert all(redirected_host not in call[0] for call in session.calls)
    _url, _timeout, _stream, kwargs = session.calls[0]
    assert kwargs.get("allow_redirects") is False


def test_raceboat_position_at_unsorted_preserves_caller_and_last_in_list_order():
    from gweatherrouting.core.race import RaceBoat, RacePosition
    from datetime import datetime

    positions = [
        RacePosition(datetime(2024, 1, 1, 2, 0), 4.0, 4.0),
        RacePosition(datetime(2024, 1, 1, 0, 0), 1.0, 1.0),
        RacePosition(datetime(2024, 1, 1, 1, 0), 2.0, 2.0),
    ]
    original = [p.latitude for p in positions]
    boat = RaceBoat(boat_id=1, positions=positions)
    # Original report order is retained and the caller's list is not mutated.
    assert [p.latitude for p in boat.positions] == original
    assert [p.latitude for p in positions] == original
    when = datetime(2024, 1, 1, 1, 30)
    assert boat.position_at(when).latitude == 2.0
    assert boat.position_index_at(when) == 2
    # Last qualifying entry in original list order wins, not max timestamp:
    # all three qualify at 02:00 and index 2 (lat 2.0) is last in list order.
    assert boat.position_at(datetime(2024, 1, 1, 2, 0)).latitude == 2.0
    assert boat.position_index_at(datetime(2024, 1, 1, 2, 0)) == 2


def _linear_position_at(positions, when):
    candidates = [p for p in positions if p.timestamp <= when]
    return candidates[-1] if candidates else None


def _linear_index_at(positions, when):
    indices = [i for i, p in enumerate(positions) if p.timestamp <= when]
    return indices[-1] if indices else -1


def test_raceboat_position_at_matches_linear_reference_under_mutations():
    import random
    from gweatherrouting.core.race import RaceBoat, RacePosition
    from datetime import datetime, timedelta

    base = datetime(2024, 1, 1)

    def make(minute, tag):
        return RacePosition(base + timedelta(minutes=minute), float(tag), 0.0)

    rng = random.Random(1234)
    for _trial in range(300):
        boat = RaceBoat(
            boat_id=1,
            positions=[make(rng.randint(0, 8), i) for i in range(rng.randint(0, 10))],
        )
        for step in range(10):
            for minute in range(-1, 11):
                when = base + timedelta(minutes=minute)
                assert boat.position_at(when) is _linear_position_at(
                    boat.positions, when
                )
                assert boat.position_index_at(when) == _linear_index_at(
                    boat.positions, when
                )
            positions = boat.positions
            op = rng.choice(
                [
                    "append",
                    "set_time",
                    "replace",
                    "reassign",
                    "swap",
                    "sort",
                    "reverse",
                    "pop",
                    "same_time_replace",
                ]
            )
            if op == "append":
                positions.append(make(rng.randint(0, 8), 100 + step))
            elif op == "set_time" and positions:
                positions[rng.randrange(len(positions))].timestamp = base + timedelta(
                    minutes=rng.randint(0, 8)
                )
            elif op == "replace" and positions:
                positions[rng.randrange(len(positions))] = make(
                    rng.randint(0, 8), 200 + step
                )
            elif op == "reassign":
                boat.positions = [
                    make(rng.randint(0, 8), 300 + step)
                    for _ in range(rng.randint(0, 5))
                ]
            elif op == "swap" and len(positions) > 1:
                i, j = rng.sample(range(len(positions)), 2)
                positions[i], positions[j] = positions[j], positions[i]
            elif op == "sort":
                positions.sort(key=lambda p: p.timestamp)
            elif op == "reverse":
                positions.reverse()
            elif op == "pop" and positions:
                positions.pop(rng.randrange(len(positions)))
            elif op == "same_time_replace" and positions:
                i = rng.randrange(len(positions))
                positions[i] = RacePosition(positions[i].timestamp, 999.0, 0.0)


def test_raceboat_position_at_unsorted_duplicate_timestamps():
    from gweatherrouting.core.race import RaceBoat, RacePosition
    from datetime import datetime

    positions = [
        RacePosition(datetime(2024, 1, 1, 1, 0), 2.0, 2.0),
        RacePosition(datetime(2024, 1, 1, 1, 0), 3.0, 3.0),
        RacePosition(datetime(2024, 1, 1, 0, 0), 1.0, 1.0),
    ]
    boat = RaceBoat(boat_id=1, positions=positions)
    # Original linear semantics: last qualifying position in original list order.
    assert boat.position_at(datetime(2024, 1, 1, 1, 0)).latitude == 1.0
    assert boat.position_index_at(datetime(2024, 1, 1, 1, 0)) == 2
    # Positions list itself stays in the supplied order.
    assert [p.latitude for p in boat.positions] == [2.0, 3.0, 1.0]


def test_raceboat_position_index_supports_list_mutations():
    from gweatherrouting.core.race import RaceBoat, RacePosition
    from datetime import datetime

    boat = RaceBoat(
        boat_id=1,
        positions=[RacePosition(datetime(2024, 1, 1, 0, 0), 1.0, 1.0)],
    )
    boat.positions.append(RacePosition(datetime(2024, 1, 1, 0, 30), 2.0, 2.0))
    assert boat.position_at(datetime(2024, 1, 1, 0, 30)).latitude == 2.0
    assert boat.position_index_at(datetime(2024, 1, 1, 0, 30)) == 1

    # Replacing an element with a changed timestamp rebuilds the index.
    boat.positions[0] = RacePosition(datetime(2024, 1, 1, 0, 15), 3.0, 3.0)
    assert boat.position_at(datetime(2024, 1, 1, 0, 20)).latitude == 3.0

    # Replacing the whole positions list rebuilds the index.
    boat.positions = [RacePosition(datetime(2024, 1, 1, 0, 45), 4.0, 4.0)]
    assert boat.position_at(datetime(2024, 1, 1, 0, 45)).latitude == 4.0
    assert boat.position_index_at(datetime(2024, 1, 1, 0, 45)) == 0
