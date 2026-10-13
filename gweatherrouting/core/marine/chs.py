"""CHS station discovery and bounded, cancellable downloads for offline use."""

import hashlib
import json
import threading
import time
from datetime import datetime, timezone

import requests

from .models import Dataset, Sample, Series, Station, canonical_json, utc_time

API_URL = "https://api-sine.dfo-mpo.gc.ca/api/v1/"
LICENCE_URL = "https://www.tides.gc.ca/en/licence-agreement"
SERIES_CODES = {
    "wlp": ("water_level", "m"),
    "wlp-hilo": ("water_level_events", "m"),
    "wcsp1": ("current_speed", "kn"),
    "wcdp1": ("current_direction", "deg"),
    "wcp1-events": ("current_events", "kn"),
}


class DownloadCancelledError(Exception):
    pass


class CHSProvider:
    id = "chs-iwls"
    name = "Canadian Hydrographic Service (IWLS)"

    def __init__(self, session=None):
        self.session = session if session is not None else requests.Session()
        self._lock = threading.Lock()
        self._last_request = None

    def _get(self, endpoint, params, cancel):
        """Pace requests below both CHS rate limits; retain response identity."""
        with self._lock:
            wait = (
                0
                if self._last_request is None
                else max(0, 2.1 - (time.monotonic() - self._last_request))
            )
            if cancel.wait(wait):
                raise DownloadCancelledError()
            self._last_request = time.monotonic()
            with self.session.get(
                API_URL + endpoint,
                params=params,
                timeout=(5, 30),
                stream=True,
                headers={"User-Agent": "gweatherrouting/ marine-stations"},
            ) as response:
                response.raise_for_status()
                chunks = []
                size = 0
                for chunk in response.iter_content(65536):
                    if cancel.is_set():
                        raise DownloadCancelledError()
                    size += len(chunk)
                    if size > 8 * 1024 * 1024:
                        raise ValueError("CHS response exceeded the 8 MiB limit")
                    chunks.append(chunk)
                payload = b"".join(chunks)
                data = json.loads(payload)
                source = dict(
                    url=response.url,
                    sha256=hashlib.sha256(payload).hexdigest(),
                    bytes=size,
                    retrieved_at=datetime.now(timezone.utc).isoformat(),
                )
                return data, source

    def discover(self, cancel=None):
        cancel = cancel or threading.Event()
        rows, _ = self._get("stations", {"chs-region-code": "PAC"}, cancel)
        if not isinstance(rows, list):
            raise ValueError("CHS returned an invalid station listing")
        stations = []
        for row in rows:
            codes = tuple(
                s["code"]
                for s in row.get("timeSeries", ())
                if s["code"] in SERIES_CODES
            )
            if codes:
                stations.append(
                    Station(
                        self.id + ":" + row["code"],
                        row["code"],
                        row["officialName"],
                        float(row["latitude"]),
                        float(row["longitude"]),
                        codes,
                    )
                )
        return tuple(sorted(stations, key=lambda station: station.name))

    def download(self, station, start, end, cancel=None, progress=None):  # noqa: C901
        cancel = cancel or threading.Event()
        start, end = utc_time(start), utc_time(end)
        if end <= start or (end - start).total_seconds() > 7 * 86400:
            raise ValueError("Choose a download interval between zero and seven days")
        if station.id != self.id + ":" + station.code:
            raise ValueError("Station belongs to another provider")
        rows, source = self._get("stations", {"code": station.code}, cancel)
        if (
            not isinstance(rows, list)
            or len(rows) != 1
            or rows[0]["code"] != station.code
        ):
            raise ValueError("CHS station lookup did not match the requested code")
        remote = rows[0]
        endpoint = "stations/" + remote["id"]
        metadata, metadata_source = self._get(endpoint + "/metadata", {}, cancel)
        phenomena, phenomena_source = self._get("phenomena", {}, cancel)
        units = {p["id"]: p["unit"] for p in phenomena}
        sources = [source, metadata_source, phenomena_source]
        selected = [s for s in remote["timeSeries"] if s["code"] in SERIES_CODES]
        series = []
        for index, item in enumerate(selected):
            code, expected_unit = SERIES_CODES[item["code"]]
            unit = units.get(item["phenomenonId"])
            if unit != expected_unit:
                raise ValueError("Unexpected CHS unit for " + item["code"])
            data, data_source = self._get(
                endpoint + "/data",
                {
                    "time-series-code": item["code"],
                    "from": start.isoformat(),
                    "to": end.isoformat(),
                    "resolution": (
                        "ALL" if code.endswith("events") else "FIFTEEN_MINUTES"
                    ),
                },
                cancel,
            )
            if not isinstance(data, list):
                raise ValueError("CHS returned an invalid sample series")
            samples = tuple(
                Sample(
                    utc_time(row["eventDate"]),
                    float(row["value"]) if row.get("value") is not None else None,
                    str(row.get("qcFlagCode", "unknown")),
                    row.get("qualifier"),
                )
                for row in data
            )
            if any(s.time < start or s.time > end for s in samples):
                raise ValueError("CHS returned samples outside the requested interval")
            series.append(Series(code, unit, samples, item["code"]))
            sources.append(data_source)
            if progress:
                progress((index + 1) / len(selected))
        if cancel.is_set():
            raise DownloadCancelledError()
        if not any(s.samples for s in series):
            raise ValueError("No CHS predictions available for the requested interval")
        # Preserve metadata without assigning an unverified sample datum or depth.
        metadata = dict(
            metadata,
            sample_datum="unverified",
            sample_depth_m=None,
            direction_reference="degrees true, flow toward",
            routing_qualified=False,
        )
        current_station = Station(
            station.id,
            station.code,
            remote["officialName"],
            float(remote["latitude"]),
            float(remote["longitude"]),
            tuple(s["code"] for s in selected),
        )
        return Dataset(
            self.id,
            current_station,
            datetime.now(timezone.utc),
            start,
            end,
            tuple(series),
            canonical_json(metadata),
            canonical_json(sources),
            "Data Source: Canadian Hydrographic Service, Fisheries and Oceans Canada",
            LICENCE_URL,
        )
