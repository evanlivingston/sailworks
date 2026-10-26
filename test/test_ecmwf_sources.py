from datetime import datetime, timezone
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from gweatherrouting.core.gribsources.ecmwf import (
    BASE_URL,
    ECMWFAIFSSource,
    ECMWFIFSSource,
)
from gweatherrouting.core.gribsources.options import DownloadOptions
from gweatherrouting.core.gribsources.ranged import HTTPStatusError


def grib_message(data):
    length = 16 + len(data) + 4
    return b"GRIB\0\0\0\2" + length.to_bytes(8, "big") + data + b"7777"


class Response:
    def __init__(self, body, status=200, headers=None):
        self.body = body
        self.status_code = status
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]

    def close(self):
        self.closed = True


class CatalogSession:
    def __init__(self, source, missing=(), dates=("20250101",)):
        self.source = source
        self.missing = set(missing)
        self.dates = tuple(dates)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url in self.missing:
            return Response(b"", status=404)
        path = urlsplit(url).path
        if path == "/forecasts/":
            text = "".join(f'<a href="{day}/">day</a>' for day in self.dates)
            text += '<a href="../">parent</a>'
        elif path in {f"/forecasts/{day}/" for day in self.dates}:
            text = "".join(f'<a href="{hour:02d}z/">run</a>' for hour in (0, 6, 12, 18))
        elif path.endswith("/0p25/oper/"):
            parts = path.strip("/").split("/")
            date_text, cycle = parts[1], parts[2][:2]
            files = []
            for step in self.source._schedule(cycle):
                stem = f"{date_text}{cycle}0000-{step}h-oper-fc"
                files.extend((stem + ".grib2", stem + ".index"))
            text = "".join(f'<a href="{name}">file</a>' for name in files)
        else:
            raise AssertionError(f"unexpected catalog path {url}")
        return Response(text.encode())

    def close(self):
        pass


class DownloadSession:
    def __init__(
        self,
        *,
        bad_range=False,
        bad_index=False,
        overlap=False,
        missing=False,
        not_found=False,
        ancillary=False,
        bad_step=False,
    ):
        self.calls = []
        self.messages = (grib_message(b"10u"), grib_message(b"10v"))
        extra_messages = (
            (
                grib_message(b"mx2t3"),
                grib_message(b"mn2t3"),
                grib_message(b"2t"),
            )
            if ancillary
            else ()
        )
        chunks = [b"prefix", self.messages[0], b"gap", self.messages[1]]
        extra_offsets = []
        cursor = sum(map(len, chunks))
        for message in extra_messages:
            chunks.extend((b"gap", message))
            extra_offsets.append(cursor + 3)
            cursor += 3 + len(message)
        self.data = b"".join(chunks)
        second_offset = 6 if overlap else 6 + len(self.messages[0]) + 3
        self.offsets = (6, second_offset)
        self.extra_messages = extra_messages
        self.extra_offsets = tuple(extra_offsets)
        self.bad_range = bad_range
        self.bad_index = bad_index
        self.missing = missing
        self.not_found = not_found
        self.bad_step = bad_step

    def _index_record(self, param, offset, length, step=0):
        return {
            "domain": "g",
            "date": "20250102" if self.bad_index else "20250101",
            "time": 0,
            "expver": "0001",
            "class": "od",
            "type": "fc",
            "stream": "oper",
            "step": step,
            "levtype": "sfc",
            "param": param,
            "model": "ifs",
            "_offset": offset,
            "_length": length,
        }

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/oper/"):
            body = b'<a href="20250101000000-0h-oper-fc.grib2">g</a><a href="20250101000000-0h-oper-fc.index">i</a>'
            return Response(body)
        if url.endswith(".index"):
            if self.not_found:
                return Response(b"", status=404)
            lines = [
                self._index_record(
                    "10u",
                    self.offsets[0],
                    len(self.messages[0]),
                    step=3 if self.bad_step else 0,
                ),
                self._index_record("10v", self.offsets[1], len(self.messages[1])),
            ]
            for parameter, offset, message in zip(
                ("mx2t3", "mn2t3", "2t"),
                self.extra_offsets,
                self.extra_messages,
            ):
                ancillary = self._index_record(parameter, offset, len(message), step=3)
                if parameter == "2t":
                    ancillary["model"] = "other-unselected-product"
                    ancillary["levtype"] = "pl"
                    ancillary.pop("expver")
                lines.append(ancillary)
            if self.missing:
                lines.pop()
            return Response("\n".join(json.dumps(line) for line in lines).encode())
        headers = kwargs.get("headers", {})
        if self.not_found:
            return Response(b"", status=404)
        start, end = map(
            int, re.fullmatch(r"bytes=(\d+)-(\d+)", headers["Range"]).groups()
        )
        if self.bad_range:
            start += 1
        content_range = f"bytes {start}-{end}/{len(self.data)}"
        body = self.data[start : end + 1]
        return Response(body, 206, {"Content-Range": content_range})

    def close(self):
        pass


def test_native_schedules_and_catalogue_discovery_are_product_specific():
    ifs = ECMWFIFSSource()
    aifs = ECMWFAIFSSource()
    assert ifs._schedule("00")[-1] == 360
    assert ifs._schedule("06")[-1] == 144
    assert ifs._schedule("00")[:3] == (0, 3, 6)
    assert ifs._schedule("00")[-2:] == (354, 360)
    assert aifs._schedule("18") == tuple(range(0, 361, 6))

    ifs._session = CatalogSession(ifs)
    rows = ifs.get_download_list(DownloadOptions(200, 0, ("wind",), ()))
    identifiers = [json.loads(row[4]) for row in rows]
    assert {row["cycle"] for row in identifiers} == {"00", "12"}
    assert all(max(row["steps"]) == 198 for row in identifiers)
    assert all(row["source"] == "ECMWF IFS" for row in identifiers)
    assert all("bounds" not in row for row in identifiers)

    aifs = ECMWFAIFSSource()
    aifs._session = CatalogSession(aifs)
    aifs_rows = aifs.get_download_list(DownloadOptions(360, 6, ("wind",), ()))
    assert len(aifs_rows) == 4
    assert all(json.loads(row[4])["source"] == "ECMWF AIFS Single" for row in aifs_rows)

    all_cycles = ifs.get_download_list(DownloadOptions(100, 0, ("wind",), ()))
    assert {json.loads(row[4])["cycle"] for row in all_cycles} == {
        "00",
        "06",
        "12",
        "18",
    }


def test_catalogue_skips_only_missing_404_candidate_directories():
    source = ECMWFIFSSource()
    source._session = CatalogSession(
        source,
        missing=(BASE_URL + "20250101/06z/ifs/0p25/oper/",),
    )
    rows = source.get_download_list(DownloadOptions(100, 0, ("wind",), ()))
    assert {json.loads(row[4])["cycle"] for row in rows} == {"00", "12", "18"}


def test_catalogue_stops_after_four_newest_complete_runs():
    source = ECMWFAIFSSource()
    session = CatalogSession(source, dates=("20250104", "20250103", "20250102"))
    source._session = session
    rows = source.get_download_list(DownloadOptions(360, 6, ("wind",), ()))
    assert len(rows) == 4
    stream_requests = [url for url in session.calls if url.endswith("/0p25/oper/")]
    assert len(stream_requests) == 4
    assert all("/20250104/" in url for url in stream_requests)


def test_explicit_steps_are_rejected_not_trimmed_and_horizon_is_capped():
    source = ECMWFIFSSource()
    with pytest.raises(ValueError, match="Explicit forecast steps"):
        source._steps(DownloadOptions(12, 1, ("wind",), (1,)), "00")
    with pytest.raises(ValueError, match="maximum"):
        source._steps(DownloadOptions(200, 0, ("wind",), ()), "06")
    with pytest.raises(ValueError, match="product maximum"):
        source._discover(DownloadOptions(384, 0, ("wind",), ()), None)
    assert len(source._output_filename("20251231", "18")) < 255


def test_download_concatenates_only_verified_selected_bytes(tmp_path):
    session = DownloadSession()
    source = ECMWFIFSSource(session=session)
    identifier = json.dumps(
        {
            "source": source.name,
            "date": "20250101",
            "cycle": "00",
            "hours": 0,
            "steps": [0],
            "fields": ["wind"],
        }
    )
    progress = []
    path = source.download(identifier, str(tmp_path), progress.append)
    assert Path(path).read_bytes() == b"".join(session.messages)
    assert progress[-1] == 100
    assert all(
        request[1]["headers"].get("Range")
        for request in session.calls
        if ".grib2" in request[0]
    )
    assert Path(path).name.startswith("ecmwf-ifs-20250101-00z-")
    assert len(source._output_filename("20251231", "18")) < 255


def test_other_index_leads_and_ancillary_parameters_do_not_block_pinned_fields(
    tmp_path,
):
    session = DownloadSession(ancillary=True)
    source = ECMWFIFSSource(session=session)
    identifier = json.dumps(
        {
            "source": source.name,
            "date": "20250101",
            "cycle": "00",
            "hours": 0,
            "steps": [0],
            "fields": ["wind"],
        }
    )
    path = source.download(identifier, str(tmp_path), lambda percent: None)
    assert Path(path).read_bytes() == b"".join(session.messages)
    assert not any(
        message in Path(path).read_bytes() for message in session.extra_messages
    )


@pytest.mark.parametrize(
    "failure", ["range", "index", "step", "overlap", "missing", "404"]
)
def test_download_failures_leave_no_installed_or_partial_file(tmp_path, failure):
    session = DownloadSession(
        bad_range=failure == "range",
        bad_index=failure == "index",
        overlap=failure == "overlap",
        missing=failure == "missing",
        not_found=failure == "404",
        bad_step=failure == "step",
    )
    source = ECMWFIFSSource(session=session)
    identifier = json.dumps(
        {
            "source": source.name,
            "date": "20250101",
            "cycle": "00",
            "hours": 0,
            "steps": [0],
            "fields": ["wind"],
        }
    )
    error = HTTPStatusError if failure == "404" else ValueError
    with pytest.raises(error):
        source.download(identifier, str(tmp_path), lambda percent: None)
    assert not list(tmp_path.glob("ecmwf-*.grib2"))
    assert not list(tmp_path.glob(".ecmwf-*.part"))
    if failure == "404":
        assert session.calls[-1][0].endswith(".index")
        assert all("/20250101/00z/ifs/" in call[0] for call in session.calls)


def test_ecmwf_size_estimate_sums_selected_indexed_ranges(monkeypatch):
    import gweatherrouting.core.gribsources.ecmwf as module

    source = ECMWFIFSSource(session=CatalogSession(None))
    identifier = json.dumps(
        {
            "source": source.name,
            "date": "20250101",
            "cycle": "00",
            "hours": 3,
            "steps": [0, 3],
            "fields": ["wind"],
        }
    )
    monkeypatch.setattr(
        module,
        "read_selected_index",
        lambda *_args: (((0, 120), (120, 80)), "0001"),
    )

    assert source.estimate_download_size(identifier) == (400, False)
