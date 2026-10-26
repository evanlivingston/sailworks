"""NOAA operational deterministic AI-GFS 0.25-degree GRIB2 source.

Public product directory: https://nomads.ncep.noaa.gov/pub/data/nccf/com/aigfs/prod/
The forecast files are global; ``bounds`` records user intent but does not
pretend to filter or crop the downloaded product.
"""

from datetime import datetime, timezone
from contextlib import contextmanager
from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urljoin, urlparse

import requests

from gweatherrouting.core.gribsources.base import GribSource
from gweatherrouting.core.marine.chs import DownloadCancelledError
from .aigfsindex import parse_index as _parse_index
from .options import DownloadOptions
from .ranged import HTTPStatusError, append_message, fetch_text, message_length

AIGFS_ROOT = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/aigfs/prod/"
AIGFS_MODEL_ID = "AIGFS"
AIGFS_NAME = "NOAA AI-GFS"
AIGFS_NATIVE_HOURS = tuple(range(0, 385, 6))
_DATE_DIR = re.compile(r"^aigfs\.(\d{8})/?$")
_CYCLE_DIR = re.compile(r"^(\d{2})/?$")
_FILE = re.compile(r"^aigfs\.t(\d{2})z\.sfc\.f(\d{3})\.grib2$")
MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
MAX_CATALOG_DAYS = 3
MAX_COMPLETE_RUNS = 4


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def _links(page, base_url):
    parser = _Links()
    parser.feed(page)
    return tuple(urljoin(base_url, href) for href in parser.hrefs)


def _child_name(url):
    return urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]


def _requested_options(options):
    options = options or DownloadOptions(
        hours=120, interval=0, fields=("wind", "pressure")
    )
    if not isinstance(options, DownloadOptions):
        raise TypeError("expected DownloadOptions")
    if len(set(options.fields)) != len(options.fields):
        raise ValueError("AI-GFS fields must be unique")
    if tuple(sorted(set(options.steps))) != tuple(options.steps):
        raise ValueError("AI-GFS explicit forecast hours must be sorted and unique")
    if any(type(hour) is not int or hour % 6 for hour in options.steps):
        raise ValueError("AI-GFS explicit forecast hours must be multiples of 6")
    selected = tuple(options.select(AIGFS_NATIVE_HOURS))
    if not selected:
        raise ValueError("No supported AI-GFS forecast hours in this request")
    return options, selected


class NOAAAIGFSSource(GribSource):
    """Downloader for NOAA's deterministic operational AI-GFS product."""

    def __init__(self, session=None):
        self._provided_session = session
        self.session = session
        self._bounds = None  # (south, west, north, east); product remains global.

    @contextmanager
    def _session_scope(self):
        if self._provided_session is not None:
            self.session = self._provided_session
            yield self.session
            return
        with requests.Session() as session:
            self.session = session
            try:
                yield session
            finally:
                self.session = None

    @property
    def name(self):
        return AIGFS_NAME

    @property
    def bounds(self):
        return self._bounds

    @bounds.setter
    def bounds(self, value):
        self._bounds = value

    @staticmethod
    def _cycle_from_url(url):
        cycle = _child_name(url)
        if not re.fullmatch(r"(?:00|06|12|18)", cycle):
            raise ValueError("Unsupported AI-GFS cycle")
        return cycle

    def _run_url(self, date, cycle):
        return urljoin(AIGFS_ROOT, f"aigfs.{date}/{cycle}/model/atmos/grib2/")

    def _catalog_links(self, url):
        page = fetch_text(self.session, url, max_bytes=MAX_INDEX_BYTES)
        return _links(page, url)

    def _selected_index(self, run_url, date, cycle, hour, fields):
        filename = f"aigfs.t{cycle}z.sfc.f{hour:03d}.grib2"
        index_url = urljoin(run_url, filename + ".idx")
        text = fetch_text(self.session, index_url, max_bytes=MAX_INDEX_BYTES)
        records, selected = _parse_index(text, date, cycle, hour)
        required = set()
        if "wind" in fields:
            required.update(("UGRD", "VGRD"))
        if "pressure" in fields:
            required.add("PRMSL")
        if set(selected) & required != required:
            return None
        positions = {record["record_id"]: i for i, record in enumerate(records)}
        chosen = []
        for element in ("UGRD", "VGRD", "PRMSL"):
            if element not in required:
                continue
            record = selected[element]
            position = positions[record["record_id"]]
            end = (
                records[position + 1]["offset"] if position + 1 < len(records) else None
            )
            chosen.append(
                (record["offset"], end - record["offset"] if end is not None else None)
            )
        return tuple(sorted(chosen))

    def _complete_run(self, date, cycle, hours, fields):
        run_url = self._run_url(date, cycle)
        try:
            links = self._catalog_links(run_url)
        except HTTPStatusError as exc:
            if exc.status_code == 404:
                return None
            raise
        except requests.HTTPError as exc:
            if getattr(exc.response, "status_code", None) == 404:
                return None
            raise
        available = {_child_name(link) for link in links}
        for hour in hours:
            filename = f"aigfs.t{cycle}z.sfc.f{hour:03d}.grib2"
            if filename not in available or filename + ".idx" not in available:
                return None
            try:
                if self._selected_index(run_url, date, cycle, hour, fields) is None:
                    return None
            except HTTPStatusError as exc:
                if exc.status_code == 404:
                    return None
                raise
            except requests.HTTPError as exc:
                if getattr(exc.response, "status_code", None) == 404:
                    return None
                raise
            except ValueError:
                return None
        return run_url

    def get_download_list(self, options=None):
        with self._session_scope():
            return self._get_download_list(options)

    def _get_download_list(self, options=None):
        options, hours = _requested_options(options)
        fields = tuple(options.fields)
        root_links = self._catalog_links(AIGFS_ROOT)
        dates = sorted(
            {
                match.group(1)
                for link in root_links
                if (match := _DATE_DIR.fullmatch(_child_name(link) + "/")) is not None
            },
            reverse=True,
        )[:MAX_CATALOG_DAYS]
        candidates = []
        for date in dates:
            date_url = urljoin(AIGFS_ROOT, f"aigfs.{date}/")
            try:
                date_links = self._catalog_links(date_url)
            except HTTPStatusError as exc:
                if exc.status_code == 404:
                    continue
                raise
            cycles = sorted(
                {
                    match.group(1)
                    for link in date_links
                    if (match := _CYCLE_DIR.fullmatch(_child_name(link) + "/"))
                    is not None
                    and match.group(1) in ("00", "06", "12", "18")
                },
                reverse=True,
            )
            for cycle in cycles:
                run_url = self._complete_run(date, cycle, hours, fields)
                if run_url is None:
                    continue
                run_time = datetime.strptime(date + cycle, "%Y%m%d%H").replace(
                    tzinfo=timezone.utc
                )
                identifier = json.dumps(
                    {
                        "source": self.name,
                        "date": date,
                        "cycle": cycle,
                        "hours": max(hours),
                        "steps": list(hours),
                        "fields": list(fields),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                candidates.append(
                    [
                        f"AI-GFS 0.25° · {max(hours)} h ({len(hours)} times)",
                        self.name,
                        "global files; size varies",
                        run_time.strftime("%Y-%m-%d %H:%M UTC"),
                        identifier,
                    ]
                )
                if len(candidates) == MAX_COMPLETE_RUNS:
                    return candidates
        return candidates

    @staticmethod
    def _parse_identifier(identifier):
        try:
            config = json.loads(identifier)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Malformed AI-GFS pinned identifier") from exc
        expected_keys = {"source", "date", "cycle", "hours", "steps", "fields"}
        if not isinstance(config, dict) or set(config) != expected_keys:
            raise ValueError("AI-GFS identifier has unknown or missing fields")
        if config["source"] != AIGFS_NAME:
            raise ValueError("AI-GFS identifier source mismatch")
        date, cycle = config["date"], config["cycle"]
        if (
            not isinstance(date, str)
            or not re.fullmatch(r"\d{8}", date)
            or cycle not in ("00", "06", "12", "18")
        ):
            raise ValueError("Invalid AI-GFS pinned run")
        try:
            datetime.strptime(date + cycle, "%Y%m%d%H")
        except ValueError as exc:
            raise ValueError("Invalid AI-GFS pinned date/cycle") from exc
        hours, steps, fields = config["hours"], config["steps"], config["fields"]
        if (
            type(hours) is not int
            or hours < 0
            or hours > 384
            or not isinstance(steps, list)
            or any(
                type(step) is not int or step < 0 or step > hours or step % 6
                for step in steps
            )
            or tuple(sorted(set(steps))) != tuple(steps)
            or not isinstance(fields, list)
            or any(not isinstance(field, str) for field in fields)
            or not fields
            or len(set(fields)) != len(fields)
            or set(fields) - {"wind", "pressure"}
            or not steps
            or max(steps) != hours
        ):
            raise ValueError("Invalid AI-GFS schedule or fields")
        return date, cycle, tuple(steps), tuple(fields)

    @staticmethod
    def _output_name(identifier, date, cycle, hours, fields):
        digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:12]
        tag = "-".join(fields)
        return f"aigfs_{date}_{cycle}z_{max(hours)}h_{tag}_{digest}.grib2"

    @staticmethod
    def _check_cancel(cancel):
        if cancel is not None and cancel.is_set():
            raise DownloadCancelledError()

    def download(self, identifier, dest_dir, percentage_callback, cancel=None):
        with self._session_scope():
            return self._download(identifier, dest_dir, percentage_callback, cancel)

    def _download(self, identifier, dest_dir, percentage_callback, cancel=None):
        date, cycle, hours, fields = self._parse_identifier(identifier)
        self._check_cancel(cancel)
        run_url = self._run_url(date, cycle)
        output_path = Path(dest_dir) / self._output_name(
            identifier, date, cycle, hours, fields
        )
        if output_path.exists() or output_path.is_symlink():
            raise FileExistsError(f"AI-GFS output already exists: {output_path.name}")
        messages = []
        for hour in hours:
            self._check_cancel(cancel)
            spans = self._selected_index(run_url, date, cycle, hour, fields)
            if spans is None:
                raise ValueError(
                    f"Pinned AI-GFS run is missing requested fields at f{hour:03d}"
                )
            filename = f"aigfs.t{cycle}z.sfc.f{hour:03d}.grib2"
            grib_url = urljoin(run_url, filename)
            for offset, indexed_length in spans:
                length = message_length(
                    self.session,
                    grib_url,
                    offset,
                    cancel=cancel,
                    max_bytes=MAX_MESSAGE_BYTES,
                )
                if indexed_length is not None and length != indexed_length:
                    raise ValueError(
                        "AI-GFS index span does not match GRIB message length"
                    )
                messages.append((grib_url, offset, length))

        total_bytes = sum(length for _, _, length in messages)
        if total_bytes <= 0:
            raise ValueError("AI-GFS download has no selected GRIB messages")
        temporary = tempfile.NamedTemporaryFile(
            dir=dest_dir, suffix=".part", delete=False
        )
        written_total = 0
        installed = False
        try:
            with temporary as output:
                for url, offset, length in messages:
                    self._check_cancel(cancel)

                    def report(message_bytes):
                        percentage_callback(
                            min(
                                100,
                                int(
                                    100 * (written_total + message_bytes) / total_bytes
                                ),
                            )
                        )

                    actual = append_message(
                        self.session,
                        url,
                        offset,
                        length,
                        output,
                        cancel=cancel,
                        progress=report,
                    )
                    if actual != length:
                        raise ValueError("AI-GFS ranged message byte count mismatch")
                    written_total += actual
            self._check_cancel(cancel)
            if written_total != total_bytes or written_total == 0:
                raise ValueError("AI-GFS staged output is incomplete")
            percentage_callback(100)
            self._check_cancel(cancel)
            if output_path.exists() or output_path.is_symlink():
                raise FileExistsError(
                    f"AI-GFS output already exists: {output_path.name}"
                )
            os.link(temporary.name, output_path)
            installed = True
            try:
                os.unlink(temporary.name)
            except OSError:
                # The atomic no-clobber install succeeded; cleanup must not
                # report a failed download after publishing the final path.
                pass
        finally:
            if not installed and os.path.exists(temporary.name):
                os.unlink(temporary.name)
        return str(output_path)

    def estimate_download_size(self, identifier, *, cancel=None):
        """Sum selected GRIB message lengths from the pinned AI-GFS indexes."""
        date, cycle, hours, fields = self._parse_identifier(identifier)
        self._check_cancel(cancel)
        run_url = self._run_url(date, cycle)
        total = 0
        with self._session_scope():
            for hour in hours:
                self._check_cancel(cancel)
                spans = self._selected_index(run_url, date, cycle, hour, fields)
                if spans is None:
                    raise ValueError(
                        f"Pinned AI-GFS run is missing selected fields at f{hour:03d}"
                    )
                filename = f"aigfs.t{cycle}z.sfc.f{hour:03d}.grib2"
                grib_url = urljoin(run_url, filename)
                for offset, indexed_length in spans:
                    length = message_length(
                        self.session,
                        grib_url,
                        offset,
                        cancel=cancel,
                        max_bytes=MAX_MESSAGE_BYTES,
                    )
                    if indexed_length is not None and length != indexed_length:
                        raise ValueError(
                            "AI-GFS index span does not match GRIB message length"
                        )
                    total += length
        if total <= 0:
            raise ValueError("AI-GFS size estimate contains no selected messages")
        return total, False
