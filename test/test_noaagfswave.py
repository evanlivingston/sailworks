import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sailworks.core.gribsources.noaagfswave import (
    GFSWAVE_HOURS,
    GFSWAVE_ROOT,
    GFSWAVE_S3_ROOT,
    NOAAGFSWaveSource,
    RateLimitError,
)
from sailworks.core.gribsources.options import DownloadOptions

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
        "sailworks.core.gribsources.noaagfswave.fetch_text", return_value=_INDEX
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
    from sailworks.core.gribsources.gfswaveindex import parse_index

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
        "nomads",
    )
    with pytest.raises(ValueError, match="schedule or fields"):
        NOAAGFSWaveSource._parse_identifier(identifier.replace("wave_height", "wind"))


def test_gfswave_catalog_pins_complete_runs_and_selected_wave_fields(monkeypatch):
    source = NOAAGFSWaveSource(session=object())
    monkeypatch.setattr(source, "_s3_runs", lambda *_args: [])
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


def _catalog_with_status(monkeypatch, source, status, *, level):
    root = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/"
    date_url = root + "gfs.20261104/"
    run_url = date_url + "12/wave/gridded/"
    filenames = {source._filename("12", hour) for hour in (0, 3)}

    def unavailable(url):
        from types import SimpleNamespace

        from sailworks.core.gribsources.ranged import HTTPStatusError

        return HTTPStatusError(200, SimpleNamespace(status_code=status), url)

    def catalog(url):
        if url == root:
            return (date_url,)
        if url == date_url:
            if level == "date":
                raise unavailable(url)
            return (date_url + "18/", date_url + "12/")
        if url == date_url + "18/wave/gridded/":
            raise unavailable(url)
        if url == run_url:
            return tuple(
                run_url + name
                for name in filenames | {name + ".idx" for name in filenames}
            )
        raise AssertionError(url)

    monkeypatch.setattr(source, "_s3_runs", lambda *_args: [])
    monkeypatch.setattr(source, "_catalog_links", catalog)
    monkeypatch.setattr(source, "_selected_index", lambda *_args: ((100, 25),))


@pytest.mark.parametrize("status", [403, 404])
def test_gfswave_catalog_skips_unpublished_run_directory(monkeypatch, status):
    source = NOAAGFSWaveSource(session=object())
    _catalog_with_status(monkeypatch, source, status, level="run")

    rows = source.get_download_list(DownloadOptions(3, 3, ("wave_height",), (0, 3)))

    assert len(rows) == 1
    assert json.loads(rows[0][4])["cycle"] == "12"


@pytest.mark.parametrize("status", [403, 404])
def test_gfswave_catalog_skips_unpublished_date_directory(monkeypatch, status):
    source = NOAAGFSWaveSource(session=object())
    _catalog_with_status(monkeypatch, source, status, level="date")

    assert (
        source.get_download_list(DownloadOptions(3, 3, ("wave_height",), (0, 3))) == []
    )


@pytest.mark.parametrize("status", [401, 429, 500, 503])
def test_gfswave_catalog_still_raises_for_fatal_statuses(monkeypatch, status):
    from sailworks.core.gribsources.ranged import HTTPStatusError

    source = NOAAGFSWaveSource(session=object())
    _catalog_with_status(monkeypatch, source, status, level="run")

    with pytest.raises(HTTPStatusError) as raised:
        source.get_download_list(DownloadOptions(3, 3, ("wave_height",), (0, 3)))

    assert raised.value.status_code == status


def test_gfswave_download_writes_selected_messages_atomically(tmp_path, monkeypatch):
    source = NOAAGFSWaveSource(session=object())
    identifier = NOAAGFSWaveSource._identifier(
        "20261023", "12", (0, 3), ("wave_height", "wave_period")
    )
    monkeypatch.setattr(
        source, "_selected_index", lambda *_args: ((100, 25), (125, 25))
    )
    monkeypatch.setattr(
        "sailworks.core.gribsources.noaagfswave.message_length",
        lambda *_args, **_kwargs: 25,
    )

    def append(_session, _url, _offset, length, output, **_kwargs):
        output.write(b"x" * length)
        return length

    monkeypatch.setattr("sailworks.core.gribsources.noaagfswave.append_message", append)
    progress = []
    result = Path(source.download(identifier, str(tmp_path), progress.append))

    assert result.is_file()
    assert result.stat().st_size == 100
    assert progress == [25, 50, 75, 100]


def _s3_candidates(*entries):
    return lambda: iter(entries)


def test_gfswave_s3_catalog_probes_newest_complete_run_without_listings(monkeypatch):
    source = NOAAGFSWaveSource(session=object())
    monkeypatch.setattr(
        source,
        "_candidate_runs",
        _s3_candidates(
            ("20261104", "12", datetime(2026, 11, 4, 12, tzinfo=timezone.utc)),
            ("20261104", "06", datetime(2026, 11, 4, 6, tzinfo=timezone.utc)),
        ),
    )

    def catalog(url):
        raise AssertionError(f"directory listing requested: {url}")

    probed = []

    def selected(url, date, cycle, hour, fields):
        probed.append((url, date, cycle, hour))
        return ((100, 25),)

    monkeypatch.setattr(source, "_catalog_links", catalog)
    monkeypatch.setattr(source, "_selected_index", selected)

    rows = source.get_download_list(DownloadOptions(3, 3, ("wave_height",), (0, 3)))

    assert len(rows) == 2
    payload = json.loads(rows[0][4])
    assert payload["date"] == "20261104"
    assert payload["cycle"] == "12"
    assert all(url.startswith(GFSWAVE_S3_ROOT) for url, *_ in probed)
    assert [hour for _, _, _, hour in probed[:2]] == [0, 3]


def test_gfswave_s3_catalog_skips_runs_with_a_missing_hour(monkeypatch):
    source = NOAAGFSWaveSource(session=object())
    monkeypatch.setattr(
        source,
        "_candidate_runs",
        _s3_candidates(
            ("20261104", "18", datetime(2026, 11, 4, 18, tzinfo=timezone.utc)),
            ("20261104", "12", datetime(2026, 11, 4, 12, tzinfo=timezone.utc)),
        ),
    )
    probed = []

    def selected(url, date, cycle, hour, fields):
        probed.append((cycle, hour))
        return None if cycle == "18" and hour == 3 else ((100, 25),)

    monkeypatch.setattr(source, "_selected_index", selected)

    rows = source.get_download_list(DownloadOptions(3, 3, ("wave_height",), (0, 3)))

    assert len(rows) == 1
    assert json.loads(rows[0][4])["cycle"] == "12"
    assert probed[:2] == [("18", 0), ("18", 3)]
    assert ("18", 6) not in probed


def test_gfswave_s3_catalog_does_not_probe_future_cycles():
    now = datetime(2026, 11, 4, 13, 30, tzinfo=timezone.utc)

    candidates = list(NOAAGFSWaveSource._candidate_runs(now))

    assert [(date, cycle) for date, cycle, _ in candidates[:2]] == [
        ("20261104", "12"),
        ("20261104", "06"),
    ]
    assert all(run_time <= now for _, _, run_time in candidates)
    assert ("20261104", "18") not in {(date, cycle) for date, cycle, _ in candidates}
    assert len({date for date, _, _ in candidates}) == 3


def test_gfswave_s3_catalog_skips_unpublished_run(monkeypatch):
    from types import SimpleNamespace

    from sailworks.core.gribsources.ranged import HTTPStatusError

    source = NOAAGFSWaveSource(session=object())
    monkeypatch.setattr(
        source,
        "_candidate_runs",
        _s3_candidates(
            ("20261104", "18", datetime(2026, 11, 4, 18, tzinfo=timezone.utc)),
            ("20261104", "12", datetime(2026, 11, 4, 12, tzinfo=timezone.utc)),
        ),
    )
    probed = []

    def selected(url, date, cycle, hour, fields):
        probed.append((cycle, hour))
        if cycle == "18":
            raise HTTPStatusError(200, SimpleNamespace(status_code=403), url)
        return ((100, 25),)

    monkeypatch.setattr(source, "_selected_index", selected)

    rows = source.get_download_list(DownloadOptions(3, 3, ("wave_height",), (0, 3)))

    assert len(rows) == 1
    assert json.loads(rows[0][4])["cycle"] == "12"
    assert probed[0] == ("18", 0)


def test_gfswave_catalog_falls_back_to_nomads_when_s3_is_empty(monkeypatch):
    source = NOAAGFSWaveSource(session=object())
    date_url = GFSWAVE_ROOT + "gfs.20261023/"
    run_url = date_url + "12/wave/gridded/"
    filenames = {source._filename("12", hour) for hour in (0, 3)}
    monkeypatch.setattr(
        source,
        "_candidate_runs",
        _s3_candidates(
            ("20261104", "12", datetime(2026, 11, 4, 12, tzinfo=timezone.utc)),
        ),
    )

    def catalog(url):
        if url == GFSWAVE_ROOT:
            return (date_url,)
        if url == date_url:
            return (date_url + "12/",)
        if url == run_url:
            return tuple(
                run_url + name
                for name in filenames | {name + ".idx" for name in filenames}
            )
        raise AssertionError(url)

    def selected(url, date, cycle, hour, fields):
        if url.startswith(GFSWAVE_S3_ROOT):
            return None
        return ((100, 25),)

    monkeypatch.setattr(source, "_catalog_links", catalog)
    monkeypatch.setattr(source, "_selected_index", selected)

    rows = source.get_download_list(DownloadOptions(3, 3, ("wave_height",), (0, 3)))

    assert len(rows) == 1
    payload = json.loads(rows[0][4])
    assert payload["date"] == "20261023"
    assert payload["cycle"] == "12"


class _Page:
    def __init__(self, status, body):
        self.status_code = status
        self.headers = {}
        self._body = body
        self.closed = False

    def iter_content(self, chunk_size=1024):
        yield self._body

    def close(self):
        self.closed = True


class _ScriptedRequests:
    """Serve one response per request and record every requested URL."""

    def __init__(self, *pages):
        self.pages = list(pages)
        self.urls = []

    def __call__(self, _session, url, **_kwargs):
        self.urls.append(url)
        return self.pages.pop(0)


def test_gfswave_nomads_rate_limit_reads_the_failing_response(monkeypatch):
    failing = _Page(302, b"<html><body>Over Rate Limit</body></html>")
    requests = _ScriptedRequests(failing, _Page(200, b"<html>Moved</html>"))
    source = NOAAGFSWaveSource(session=object())
    monkeypatch.setattr(source, "_s3_runs", lambda *_args: [])
    monkeypatch.setattr("sailworks.core.gribsources.ranged._request", requests)

    with pytest.raises(RateLimitError) as raised:
        source.get_download_list(DownloadOptions(3, 3, ("wave_height",), (0, 3)))

    assert "NOMADS rate limit reached" in str(raised.value)
    assert "HTTP 302" not in str(raised.value)
    assert requests.urls == [GFSWAVE_ROOT]
    assert failing.closed


def test_gfswave_nomads_redirect_without_rate_limit_marker_stays_a_status_error(
    monkeypatch,
):
    from sailworks.core.gribsources.ranged import HTTPStatusError

    failing = _Page(302, b"<html><body>Moved</body></html>")
    requests = _ScriptedRequests(failing, _Page(200, b"Over Rate Limit"))
    source = NOAAGFSWaveSource(session=object())
    monkeypatch.setattr(source, "_s3_runs", lambda *_args: [])
    monkeypatch.setattr("sailworks.core.gribsources.ranged._request", requests)

    with pytest.raises(HTTPStatusError) as raised:
        source.get_download_list(DownloadOptions(3, 3, ("wave_height",), (0, 3)))

    assert raised.value.status_code == 302
    assert requests.urls == [GFSWAVE_ROOT]
    assert failing.closed


def test_gfswave_s3_redirect_is_never_probed_for_a_rate_limit(monkeypatch):
    from sailworks.core.gribsources.ranged import HTTPStatusError

    failing = _Page(302, b"<html><body>Over Rate Limit</body></html>")
    requests = _ScriptedRequests(failing, _Page(200, b"Over Rate Limit"))
    source = NOAAGFSWaveSource(session=object())
    monkeypatch.setattr("sailworks.core.gribsources.ranged._request", requests)

    with pytest.raises(HTTPStatusError) as raised:
        source._catalog_text(GFSWAVE_S3_ROOT + "gfs.20261104/12/wave/gridded/x.idx")

    assert raised.value.status_code == 302
    assert requests.urls == [GFSWAVE_S3_ROOT + "gfs.20261104/12/wave/gridded/x.idx"]


@pytest.mark.parametrize(
    "transport,root", [(None, GFSWAVE_ROOT), ("s3", GFSWAVE_S3_ROOT)]
)
def test_gfswave_pinned_download_keeps_its_identifier_transport(
    tmp_path, monkeypatch, transport, root
):
    source = NOAAGFSWaveSource(session=object())
    identifier = NOAAGFSWaveSource._identifier(
        "20261023", "12", (0, 3), ("wave_height",), transport=transport
    )
    loaded = []
    download_urls = []

    def selected(run_url, date, cycle, hour, fields):
        loaded.append(run_url)
        return ((100, 25),)

    def length(_session, url, *_args, **_kwargs):
        download_urls.append(url)
        return 25

    def append(_session, url, _offset, length, output, **_kwargs):
        download_urls.append(url)
        output.write(b"x" * length)
        return length

    monkeypatch.setattr(source, "_selected_index", selected)
    monkeypatch.setattr("sailworks.core.gribsources.noaagfswave.message_length", length)
    monkeypatch.setattr("sailworks.core.gribsources.noaagfswave.append_message", append)

    Path(source.download(identifier, str(tmp_path), lambda _value: None))

    assert len(loaded) == 2
    assert all(url.startswith(root) for url in loaded)
    assert download_urls and all(url.startswith(root) for url in download_urls)


def test_gfswave_nomads_catalog_skips_future_runs(monkeypatch):
    source = NOAAGFSWaveSource(session=object())
    date_url = GFSWAVE_ROOT + "gfs.20261104/"
    run_url = date_url + "12/wave/gridded/"
    probes = []

    def catalog(url):
        if url == GFSWAVE_ROOT:
            return (date_url,)
        if url == date_url:
            return (date_url + "18/", date_url + "12/")
        if url == run_url:
            probes.append(url)
            filenames = {source._filename("12", hour) for hour in (0, 3)}
            return tuple(
                run_url + name
                for name in filenames | {name + ".idx" for name in filenames}
            )
        raise AssertionError(url)

    monkeypatch.setattr(source, "_catalog_links", catalog)
    monkeypatch.setattr(source, "_selected_index", lambda *_args: ((100, 25),))

    now = datetime(2026, 11, 4, 13, 30, tzinfo=timezone.utc)
    runs = source._nomads_runs((0, 3), ("wave_height",), now=now)

    assert [entry[1] for entry in runs] == ["12"]
    assert probes == [run_url]
