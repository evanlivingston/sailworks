import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from gweatherrouting.core.gribsources.noaaaigfs import (
    AIGFS_ROOT,
    AIGFS_NAME,
    NOAAAIGFSSource,
    _parse_index,
)
import gweatherrouting.core.gribsources.noaaaigfs as noaaaigfs
from gweatherrouting.core.gribsources.options import DownloadOptions
from gweatherrouting.core.gribsources.ranged import HTTPStatusError
from gweatherrouting.core.marine.chs import DownloadCancelledError
from test.aigfs_fixtures import index_for_hours, lead_fixture


def options(hours=6, fields=("wind", "pressure"), steps=()):
    return DownloadOptions(hours, 0, fields, steps)


def index_fetch(indexes):
    def fetch(_session, url, *, cancel=None, max_bytes=None):
        match = re.search(r"\.f(\d{3})\.grib2\.idx$", url)
        assert match is not None
        hour = int(match.group(1))
        return indexes[hour][0]

    return fetch


def test_catalog_discovers_pinned_complete_runs_and_skips_partial_latest(
    monkeypatch,
):
    source = NOAAAIGFSSource(session=object())
    indexes = index_for_hours((0, 6), date="20261026", cycle="06", apcp_hours=(6,))
    run_url = source._run_url("20261026", "06")

    def fetch(_session, url, *, cancel=None, max_bytes=None):
        if url == AIGFS_ROOT:
            return '<a href="aigfs.20261026/">aigfs.20261026/</a>'
        if url.endswith("aigfs.20261026/"):
            return '<a href="12/">12/</a><a href="06/">06/</a>'
        if url.endswith("/12/model/atmos/grib2/"):
            return '<a href="aigfs.t12z.sfc.f000.grib2">f000</a>'
        if url.endswith("aigfs.t12z.sfc.f000.grib2.idx"):
            return lead_fixture("20261026", "12", 0)[0]
        if url == run_url:
            return "".join(
                f'<a href="aigfs.t06z.sfc.f{hour:03d}.grib2">grib</a>'
                f'<a href="aigfs.t06z.sfc.f{hour:03d}.grib2.idx">idx</a>'
                for hour in (0, 6)
            )
        match = re.search(r"\.f(\d{3})\.grib2\.idx$", url)
        assert match is not None
        hour = int(match.group(1))
        return indexes[hour][0]

    monkeypatch.setattr("gweatherrouting.core.gribsources.noaaaigfs.fetch_text", fetch)
    rows = source.get_download_list(options())
    assert len(rows) == 1
    assert rows[0][1] == AIGFS_NAME
    pinned = json.loads(rows[0][4])
    assert (pinned["date"], pinned["cycle"]) == ("20261026", "06")
    assert pinned["steps"] == [0, 6]
    assert pinned["fields"] == ["wind", "pressure"]


@pytest.mark.parametrize("status,expected_cycles", [(404, ["06"]), (503, None)])
def test_catalog_treats_only_structured_not_found_as_incomplete_run(
    monkeypatch, status, expected_cycles
):
    source = NOAAAIGFSSource(session=object())
    index = lead_fixture("20261026", "06", 0)[0]

    def fetch(_session, url, *, cancel=None, max_bytes=None):
        if url == AIGFS_ROOT:
            return '<a href="aigfs.20261026/">run</a>'
        if url.endswith("aigfs.20261026/"):
            return '<a href="12/">12</a><a href="06/">06</a>'
        if url.endswith("/model/atmos/grib2/"):
            cycle = re.search(r"/(00|06|12|18)/model/atmos/grib2/$", url).group(1)
            return (
                f'<a href="aigfs.t{cycle}z.sfc.f000.grib2">grib</a>'
                f'<a href="aigfs.t{cycle}z.sfc.f000.grib2.idx">idx</a>'
            )
        if "/12/" in url:
            response = SimpleNamespace(status_code=status)
            raise HTTPStatusError(200, response, url)
        return index

    monkeypatch.setattr(noaaaigfs, "fetch_text", fetch)
    if status == 503:
        with pytest.raises(HTTPStatusError) as caught:
            source.get_download_list(options(0))
        assert caught.value.status_code == 503
    else:
        rows = source.get_download_list(options(0))
        assert [json.loads(row[4])["cycle"] for row in rows] == expected_cycles


def test_catalog_skips_only_structured_404_for_incomplete_recent_run(monkeypatch):
    source = NOAAAIGFSSource(session=object())
    indexes = index_for_hours((0,), date="20261026", cycle="06")

    def run_listing(cycle):
        return "".join(
            f'<a href="aigfs.t{cycle}z.sfc.f000.grib2">grib</a>'
            f'<a href="aigfs.t{cycle}z.sfc.f000.grib2.idx">idx</a>'
        )

    def fetch(_session, url, *, cancel=None, max_bytes=None):
        if url == AIGFS_ROOT:
            return '<a href="aigfs.20261026/">aigfs.20261026/</a>'
        if url.endswith("aigfs.20261026/"):
            return '<a href="12/">12/</a><a href="06/">06/</a>'
        if url.endswith("/model/atmos/grib2/"):
            cycle = re.search(r"/(00|06|12|18)/model/atmos/grib2/$", url).group(1)
            return run_listing(cycle)
        if "/12/" in url:
            raise HTTPStatusError(200, SimpleNamespace(status_code=404), url)
        return indexes[0][0]

    monkeypatch.setattr(noaaaigfs, "fetch_text", fetch)
    rows = source.get_download_list(options(0))
    assert [json.loads(row[4])["cycle"] for row in rows] == ["06"]


def test_catalog_does_not_hide_non404_http_errors(monkeypatch):
    source = NOAAAIGFSSource(session=object())

    def fetch(_session, url, *, cancel=None, max_bytes=None):
        if url == AIGFS_ROOT:
            return '<a href="aigfs.20261026/">aigfs.20261026/</a>'
        if url.endswith("aigfs.20261026/"):
            return '<a href="12/">12/</a>'
        if url.endswith("/model/atmos/grib2/"):
            return '<a href="aigfs.t12z.sfc.f000.grib2">grib</a><a href="aigfs.t12z.sfc.f000.grib2.idx">idx</a>'
        raise HTTPStatusError(200, SimpleNamespace(status_code=503), url)

    monkeypatch.setattr(noaaaigfs, "fetch_text", fetch)
    with pytest.raises(HTTPStatusError) as caught:
        source.get_download_list(options(0))
    assert caught.value.status_code == 503


def test_index_requires_run_surface_levels_unique_offsets_and_selected_times(
    monkeypatch,
):
    text, _, _ = lead_fixture(date="20261025", cycle="12", hour=6, include_apcp=True)
    records, selected = _parse_index(text, "20261025", "12", 6)
    assert {key for key in selected} == {"UGRD", "VGRD", "PRMSL"}
    assert records[-1]["element"] == "APCP"  # retained as the selected span boundary
    source = NOAAAIGFSSource(session=object())
    # Relative length, not the next record's absolute byte offset.
    monkeypatch.setattr(noaaaigfs, "fetch_text", lambda *_a, **_k: text)
    spans = source._selected_index(
        "https://example.invalid/run/", "20261025", "12", 6, ("wind", "pressure")
    )
    assert spans == ((0, 64), (64, 64), (192, 64))
    with pytest.raises(ValueError, match="reference time"):
        _parse_index(text.replace("d=2026102512", "d=2026102506"), "20261025", "12", 6)
    bad_level = text.replace("10 m above ground", "100 m above ground", 1)
    with pytest.raises(ValueError, match="unexpected level"):
        _parse_index(bad_level, "20261025", "12", 6)
    duplicate = text + "\n" + text.splitlines()[0]
    with pytest.raises(ValueError, match="increase|duplicate"):
        _parse_index(duplicate, "20261025", "12", 6)


def test_explicit_non_native_hour_is_rejected_not_trimmed():
    source = NOAAAIGFSSource(session=object())
    with pytest.raises(ValueError, match="multiples of 6"):
        source.get_download_list(options(12, ("wind",), (0, 7)))


def test_download_concatenates_only_requested_bands_byte_for_byte(
    tmp_path, monkeypatch
):
    source = NOAAAIGFSSource(session=object())
    indexes = index_for_hours((0, 6), date="20261025", cycle="12", apcp_hours=(6,))
    files = {}
    expected = bytearray()
    for hour, (index, data, bands) in indexes.items():
        files[hour] = data
        wanted = {"UGRD", "VGRD", "PRMSL"}
        for element, _level, _tag in bands:
            if element in wanted:
                # Each NCEP index record's offset is retained in order.
                line = next(
                    row for row in index.splitlines() if row.split(":")[3] == element
                )
                offset = int(line.split(":")[1])
                length = int.from_bytes(data[offset + 8 : offset + 16], "big")
                expected.extend(data[offset : offset + length])

    run_url = source._run_url("20261025", "12")
    monkeypatch.setattr(
        "gweatherrouting.core.gribsources.noaaaigfs.fetch_text",
        index_fetch(indexes),
    )

    def length(_session, url, offset, *, cancel=None, max_bytes=None):
        hour = int(Path(url).name.rsplit("f", 1)[1].split(".")[0])
        payload = files[hour]
        assert payload[offset : offset + 4] == b"GRIB"
        return int.from_bytes(payload[offset + 8 : offset + 16], "big")

    def append(
        _session, url, offset, message_size, output, *, cancel=None, progress=None
    ):
        hour = int(Path(url).name.rsplit("f", 1)[1].split(".")[0])
        payload = files[hour][offset : offset + message_size]
        output.write(payload)
        if progress is not None:
            progress(len(payload))
        return len(payload)

    monkeypatch.setattr(
        "gweatherrouting.core.gribsources.noaaaigfs.message_length", length
    )
    monkeypatch.setattr(
        "gweatherrouting.core.gribsources.noaaaigfs.append_message", append
    )
    identifier = json.dumps(
        {
            "source": AIGFS_NAME,
            "date": "20261025",
            "cycle": "12",
            "hours": 6,
            "steps": [0, 6],
            "fields": ["wind", "pressure"],
        }
    )
    progress = []
    output = Path(source.download(identifier, str(tmp_path), progress.append))
    assert output.read_bytes() == bytes(expected)
    assert progress[-1] == 100
    assert output.name.startswith("aigfs_20261025_12z_6h_")


def test_pinned_download_fails_on_missing_index_without_fallback_or_final_file(
    tmp_path, monkeypatch
):
    source = NOAAAIGFSSource(session=object())
    requested = []

    def missing(_session, url, *, cancel=None, max_bytes=None):
        requested.append(url)
        response = SimpleNamespace(status_code=404)
        raise HTTPStatusError(200, response, url)

    monkeypatch.setattr(
        "gweatherrouting.core.gribsources.noaaaigfs.fetch_text", missing
    )
    identifier = json.dumps(
        {
            "source": AIGFS_NAME,
            "date": "20261025",
            "cycle": "12",
            "hours": 0,
            "steps": [0],
            "fields": ["wind", "pressure"],
        }
    )
    with pytest.raises(HTTPStatusError) as caught:
        source.download(identifier, str(tmp_path), lambda _: None)
    assert caught.value.status_code == 404
    assert requested == [
        source._run_url("20261025", "12") + "aigfs.t12z.sfc.f000.grib2.idx"
    ]
    assert not list(tmp_path.glob("*.grib2"))
    assert not list(tmp_path.glob("*.part"))


def test_existing_destination_is_never_overwritten(tmp_path, monkeypatch):
    source = NOAAAIGFSSource(session=object())
    identifier = json.dumps(
        {
            "source": AIGFS_NAME,
            "date": "20261025",
            "cycle": "12",
            "hours": 0,
            "steps": [0],
            "fields": ["wind", "pressure"],
        }
    )
    destination = tmp_path / source._output_name(
        identifier, "20261025", "12", (0,), ("wind", "pressure")
    )
    destination.write_bytes(b"prior installed bytes")

    def unexpected_network(*_args, **_kwargs):
        pytest.fail("destination collision must be rejected before network access")

    monkeypatch.setattr(
        "gweatherrouting.core.gribsources.noaaaigfs.fetch_text", unexpected_network
    )
    with pytest.raises(FileExistsError):
        source.download(identifier, str(tmp_path), lambda _: None)
    assert destination.read_bytes() == b"prior installed bytes"


def test_maximum_horizon_output_filename_fits_filesystem_component_limit():
    source = NOAAAIGFSSource(session=object())
    identifier = json.dumps(
        {
            "source": AIGFS_NAME,
            "date": "20261025",
            "cycle": "12",
            "hours": 384,
            "steps": list(range(0, 385, 6)),
            "fields": ["wind", "pressure"],
        }
    )
    date, cycle, hours, fields = source._parse_identifier(identifier)
    filename = source._output_name(identifier, date, cycle, hours, fields)
    assert len(filename.encode("utf-8")) <= 255


def test_cancel_during_append_removes_stage_and_preserves_existing_file(
    tmp_path, monkeypatch
):
    source = NOAAAIGFSSource(session=object())
    indexes = index_for_hours((0,), date="20261025", cycle="12")
    _, payload, _ = indexes[0]
    existing = tmp_path / "installed-existing.grib2"
    existing.write_bytes(b"unchanged installed forecast")
    monkeypatch.setattr(
        "gweatherrouting.core.gribsources.noaaaigfs.fetch_text",
        index_fetch(indexes),
    )
    monkeypatch.setattr(
        "gweatherrouting.core.gribsources.noaaaigfs.message_length",
        lambda *_a, **_k: 64,
    )
    import threading

    cancel = threading.Event()

    def append(
        _session, _url, offset, message_size, output, *, cancel=None, progress=None
    ):
        output.write(payload[offset : offset + message_size])
        if progress:
            progress(message_size)
        return message_size

    monkeypatch.setattr(
        "gweatherrouting.core.gribsources.noaaaigfs.append_message", append
    )
    identifier = json.dumps(
        {
            "source": AIGFS_NAME,
            "date": "20261025",
            "cycle": "12",
            "hours": 0,
            "steps": [0],
            "fields": ["wind", "pressure"],
        }
    )

    def progress(_):
        cancel.set()

    with pytest.raises(DownloadCancelledError):
        source.download(identifier, str(tmp_path), progress, cancel=cancel)
    assert existing.read_bytes() == b"unchanged installed forecast"
    assert {path.name for path in tmp_path.glob("*.grib2")} == {existing.name}
    assert not list(tmp_path.glob("*.part"))
