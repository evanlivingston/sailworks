import json
from pathlib import Path

import pytest

from gweatherrouting.core.gribsources.noaagfswave import (
    GFSWAVE_HOURS,
    NOAAGFSWaveSource,
)
from gweatherrouting.core.gribsources.options import DownloadOptions


_INDEX = """\
1:0:d=2026102312:WIND:surface:3 hour fcst:
2:100:d=2026102312:WDIR:surface:3 hour fcst:
3:200:d=2026102312:UGRD:surface:3 hour fcst:
4:300:d=2026102312:HTSGW:surface:3 hour fcst:
5:400:d=2026102312:PERPW:surface:3 hour fcst:
6:600:d=2026102312:DIRPW:surface:3 hour fcst:
7:800:d=2026102312:WVHGT:surface:3 hour fcst:
"""


def test_gfswave_index_selects_requested_wave_parameters_and_bounded_spans():
    source = NOAAGFSWaveSource(session=object())

    from unittest.mock import patch

    with patch(
        "gweatherrouting.core.gribsources.noaagfswave.fetch_text", return_value=_INDEX
    ):
        spans = source._selected_index(
            "https://example.test/run/",
            "20261023",
            "12",
            3,
            ("wave_height", "wave_period"),
        )

    assert spans == ((300, 100), (400, 200))


@pytest.mark.parametrize(
    "change,match",
    [
        (lambda text: text.replace("d=2026102312", "d=2026102212"), "reference time"),
        (lambda text: text.replace("HTSGW:surface", "HTSGW:10 m"), "unexpected level"),
        (
            lambda text: text + "8:900:d=2026102312:HTSGW:surface:3 hour fcst:\n",
            "duplicates",
        ),
    ],
)
def test_gfswave_index_rejects_incoherent_records(change, match):
    from gweatherrouting.core.gribsources.gfswaveindex import parse_index

    with pytest.raises(ValueError, match=match):
        parse_index(change(_INDEX), "20261023", "12", 3)


def test_gfswave_schedule_and_pinned_identifiers_are_strict():
    options, hours = NOAAGFSWaveSource._requested_options(
        DownloadOptions(126, 3, ("wave_height", "wave_period"), (0, 3, 6, 123))
    )
    assert options.fields == ("wave_height", "wave_period")
    assert hours == (0, 3, 6, 123)
    assert 120 in GFSWAVE_HOURS and 123 in GFSWAVE_HOURS and 121 not in GFSWAVE_HOURS

    identifier = json.dumps(
        {
            "source": "NOAA GFS-Wave",
            "date": "20261023",
            "cycle": "12",
            "hours": 3,
            "steps": [0, 3],
            "fields": ["wave_height", "wave_period"],
        }
    )
    assert NOAAGFSWaveSource._parse_identifier(identifier) == (
        "20261023",
        "12",
        (0, 3),
        ("wave_height", "wave_period"),
    )
    with pytest.raises(ValueError, match="schedule or fields"):
        NOAAGFSWaveSource._parse_identifier(identifier.replace("wave_height", "wind"))


def test_gfswave_catalog_pins_complete_runs_and_selected_wave_fields(monkeypatch):
    source = NOAAGFSWaveSource(session=object())
    root = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/"
    date_url = root + "gfs.20261023/"
    run_url = date_url + "12/wave/gridded/"
    filenames = {source._filename("12", hour) for hour in (0, 3)}
    urls = []

    def catalog(url):
        if url == root:
            return (date_url,)
        if url == date_url:
            return (date_url + "12/",)
        if url == run_url:
            return tuple(
                run_url + name
                for name in filenames | {name + ".idx" for name in filenames}
            )
        raise AssertionError(url)

    def selected_index(url, date, cycle, hour, fields):
        urls.append((url, date, cycle, hour, tuple(fields)))
        return ((100, 25),)

    monkeypatch.setattr(source, "_catalog_links", catalog)
    monkeypatch.setattr(source, "_selected_index", selected_index)
    rows = source.get_download_list(DownloadOptions(3, 3, ("wave_height",), (0, 3)))

    assert len(rows) == 1
    assert rows[0][1] == "NOAA GFS-Wave"
    assert json.loads(rows[0][4]) == {
        "source": "NOAA GFS-Wave",
        "date": "20261023",
        "cycle": "12",
        "hours": 3,
        "steps": [0, 3],
        "fields": ["wave_height"],
    }
    assert [item[3] for item in urls] == [0, 3]


def test_gfswave_download_writes_selected_messages_atomically(tmp_path, monkeypatch):
    source = NOAAGFSWaveSource(session=object())
    identifier = NOAAGFSWaveSource._identifier(
        "20261023", "12", (0, 3), ("wave_height", "wave_period")
    )
    monkeypatch.setattr(
        source, "_selected_index", lambda *_args: ((100, 25), (125, 25))
    )
    monkeypatch.setattr(
        "gweatherrouting.core.gribsources.noaagfswave.message_length",
        lambda *_args, **_kwargs: 25,
    )

    def append(_session, _url, _offset, length, output, **_kwargs):
        output.write(b"x" * length)
        return length

    monkeypatch.setattr(
        "gweatherrouting.core.gribsources.noaagfswave.append_message", append
    )
    progress = []
    result = Path(source.download(identifier, str(tmp_path), progress.append))

    assert result.is_file()
    assert result.stat().st_size == 100
    assert progress == [25, 50, 75, 100]
