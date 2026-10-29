"""NOAA GFS-Wave global 0.25-degree wave forecast GRIB2 source."""

from contextlib import contextmanager
from datetime import datetime, timezone
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
from .gfswaveindex import ELEMENTS_BY_FIELD, parse_index
from .options import DownloadOptions, custom_rows
from .ranged import HTTPStatusError, append_message, fetch_text, message_length


GFSWAVE_ROOT = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/"
GFSWAVE_NAME = "NOAA GFS-Wave"
GFSWAVE_FIELDS = tuple(ELEMENTS_BY_FIELD)
GFSWAVE_HOURS = tuple(range(121)) + tuple(range(123, 385, 3))
MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
MAX_CATALOG_DAYS = 3
MAX_COMPLETE_RUNS = 4
_DATE_DIR = re.compile(r"^gfs\.(\d{8})/?$")
_CYCLE_DIR = re.compile(r"^(?:00|06|12|18)/?$")


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


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise DownloadCancelledError()


class NOAAGFSWaveSource(GribSource):
    """Fetch selected wave parameters from NOAA's distinct GFS-Wave product."""

    download_fields = GFSWAVE_FIELDS

    def __init__(self, session=None):
        self._provided_session = session
        self.session = session
        self._bounds = None  # GFS-Wave global files are not region filtered.

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
        return GFSWAVE_NAME

    @property
    def bounds(self):
        return self._bounds

    @bounds.setter
    def bounds(self, value):
        self._bounds = value

    def _catalog_links(self, url):
        return _links(fetch_text(self.session, url, max_bytes=MAX_INDEX_BYTES), url)

    @staticmethod
    def _run_url(date, cycle):
        return urljoin(GFSWAVE_ROOT, f"gfs.{date}/{cycle}/wave/gridded/")

    @staticmethod
    def _filename(cycle, hour):
        return f"gfswave.t{cycle}z.global.0p25.f{hour:03d}.grib2"

    @staticmethod
    def _requested_options(options):
        options = options or DownloadOptions(
            hours=120, interval=3, fields=("wave_height", "wave_period")
        )
        if not isinstance(options, DownloadOptions):
            raise TypeError("expected DownloadOptions")
        if len(set(options.fields)) != len(options.fields):
            raise ValueError("GFS-Wave fields must be unique")
        if set(options.fields) - set(GFSWAVE_FIELDS):
            raise ValueError("GFS-Wave supports wave height, period and direction")
        if tuple(sorted(set(options.steps))) != tuple(options.steps):
            raise ValueError("GFS-Wave forecast steps must be sorted and unique")
        if any(
            type(hour) is not int or hour not in GFSWAVE_HOURS for hour in options.steps
        ):
            raise ValueError("GFS-Wave forecast steps are not on the native schedule")
        hours = tuple(
            hour
            for hour in GFSWAVE_HOURS
            if hour <= options.hours
            and (not options.interval or hour % options.interval == 0)
            and (not options.steps or hour in options.steps)
        )
        if not hours:
            raise ValueError("No supported GFS-Wave forecast hours in this request")
        return options, hours

    def _selected_index(self, run_url, date, cycle, hour, fields):
        filename = self._filename(cycle, hour)
        text = fetch_text(
            self.session,
            urljoin(run_url, filename + ".idx"),
            max_bytes=MAX_INDEX_BYTES,
        )
        records, selected = parse_index(text, date, cycle, hour)
        if set(selected) & set(fields) != set(fields):
            return None
        positions = {record["record_id"]: i for i, record in enumerate(records)}
        spans = []
        for field in fields:
            record = selected[field]
            position = positions[record["record_id"]]
            end = (
                records[position + 1]["offset"] if position + 1 < len(records) else None
            )
            spans.append(
                (record["offset"], end - record["offset"] if end is not None else None)
            )
        return tuple(sorted(spans))

    @staticmethod
    def _identifier(date, cycle, hours, fields):
        return json.dumps(
            {
                "source": GFSWAVE_NAME,
                "date": date,
                "cycle": cycle,
                "hours": max(hours),
                "steps": list(hours),
                "fields": list(fields),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def get_download_list(self, options=None):
        options, hours = self._requested_options(options)
        fields = tuple(options.fields)
        with self._session_scope():
            root_links = self._catalog_links(GFSWAVE_ROOT)
            dates = sorted(
                {
                    match.group(1)
                    for link in root_links
                    if (match := _DATE_DIR.fullmatch(_child_name(link) + "/"))
                },
                reverse=True,
            )[:MAX_CATALOG_DAYS]
            runs = []
            for date in dates:
                date_url = urljoin(GFSWAVE_ROOT, f"gfs.{date}/")
                try:
                    date_links = self._catalog_links(date_url)
                except HTTPStatusError as exc:
                    if exc.status_code == 404:
                        continue
                    raise
                cycles = sorted(
                    {
                        _child_name(link)
                        for link in date_links
                        if _CYCLE_DIR.fullmatch(_child_name(link) + "/")
                    },
                    reverse=True,
                )
                for cycle in cycles:
                    run_url = self._run_url(date, cycle)
                    try:
                        filenames = {
                            _child_name(link) for link in self._catalog_links(run_url)
                        }
                        for hour in hours:
                            filename = self._filename(cycle, hour)
                            if (
                                filename not in filenames
                                or filename + ".idx" not in filenames
                            ):
                                raise LookupError
                            if (
                                self._selected_index(run_url, date, cycle, hour, fields)
                                is None
                            ):
                                raise LookupError
                    except LookupError:
                        continue
                    except HTTPStatusError as exc:
                        if exc.status_code == 404:
                            continue
                        raise
                    run_time = datetime.strptime(date + cycle, "%Y%m%d%H").replace(
                        tzinfo=timezone.utc
                    )
                    runs.append(
                        (
                            date,
                            cycle,
                            run_time.strftime("%Y-%m-%d %H:%M UTC"),
                            max(hours),
                        )
                    )
                    if len(runs) >= MAX_COMPLETE_RUNS:
                        break
                if len(runs) >= MAX_COMPLETE_RUNS:
                    break
            return custom_rows(
                self,
                options,
                runs,
                lambda _cycle, _fields, _cap: GFSWAVE_HOURS,
                self._estimate_custom_size,
            )

    @staticmethod
    def _parse_identifier(identifier):
        try:
            config = json.loads(identifier)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Malformed GFS-Wave pinned identifier") from exc
        expected = {"source", "date", "cycle", "hours", "steps", "fields"}
        if not isinstance(config, dict) or set(config) != expected:
            raise ValueError("GFS-Wave identifier has unknown or missing fields")
        date, cycle = config["date"], config["cycle"]
        if (
            config["source"] != GFSWAVE_NAME
            or not isinstance(date, str)
            or not re.fullmatch(r"\d{8}", date)
            or cycle not in ("00", "06", "12", "18")
        ):
            raise ValueError("Invalid GFS-Wave pinned run")
        try:
            datetime.strptime(date + cycle, "%Y%m%d%H")
        except ValueError as exc:
            raise ValueError("Invalid GFS-Wave pinned date/cycle") from exc
        hours, steps, fields = config["hours"], config["steps"], config["fields"]
        if (
            type(hours) is not int
            or hours not in GFSWAVE_HOURS
            or not isinstance(steps, list)
            or not steps
            or any(type(step) is not int or step not in GFSWAVE_HOURS for step in steps)
            or tuple(sorted(set(steps))) != tuple(steps)
            or max(steps) != hours
            or not isinstance(fields, list)
            or not fields
            or any(not isinstance(field, str) for field in fields)
            or len(set(fields)) != len(fields)
            or set(fields) - set(GFSWAVE_FIELDS)
        ):
            raise ValueError("Invalid GFS-Wave schedule or fields")
        return date, cycle, tuple(steps), tuple(fields)

    @staticmethod
    def _estimate_custom_size(steps, fields):
        # The product is global; observed selected messages are about 0.5 MB each.
        estimate = len(steps) * len(fields) * 0.65
        return f"~{estimate:.1f} MB" if estimate >= 1 else f"~{estimate * 1024:.0f} KB"

    def _download(self, identifier, dest_dir, percentage_callback, cancel=None):
        date, cycle, hours, fields = self._parse_identifier(identifier)
        _check_cancel(cancel)
        run_url = self._run_url(date, cycle)
        digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:12]
        filename = f"gfswave_{date}_{cycle}z_{max(hours)}h_{digest}.grib2"
        output_path = Path(dest_dir) / filename
        if output_path.exists() or output_path.is_symlink():
            raise FileExistsError(f"GFS-Wave output already exists: {filename}")
        temporary = tempfile.NamedTemporaryFile(
            dir=dest_dir, suffix=".part", delete=False
        )
        try:
            total = len(hours) * len(fields)
            completed = 0
            bytes_written = 0
            with temporary as output:
                for hour in hours:
                    _check_cancel(cancel)
                    file_name = self._filename(cycle, hour)
                    url = urljoin(run_url, file_name)
                    spans = self._selected_index(run_url, date, cycle, hour, fields)
                    if spans is None:
                        raise ValueError(
                            f"Pinned GFS-Wave run is missing requested fields at f{hour:03d}"
                        )
                    for offset, indexed_length in spans:
                        length = message_length(
                            self.session,
                            url,
                            offset,
                            cancel=cancel,
                            max_bytes=MAX_MESSAGE_BYTES,
                        )
                        if indexed_length is not None and length != indexed_length:
                            raise ValueError(
                                "GFS-Wave index length differs from GRIB message"
                            )
                        bytes_written += append_message(
                            self.session,
                            url,
                            offset,
                            length,
                            output,
                            cancel=cancel,
                            max_bytes=MAX_MESSAGE_BYTES,
                        )
                        completed += 1
                        percentage_callback(int(100 * completed / total))
            _check_cancel(cancel)
            if bytes_written == 0:
                raise RuntimeError("GFS-Wave download returned empty data")
            os.replace(temporary.name, output_path)
        finally:
            if os.path.exists(temporary.name):
                os.remove(temporary.name)
        return str(output_path)

    def download(self, identifier, dest_dir, percentage_callback, cancel=None):
        with self._session_scope():
            return self._download(identifier, dest_dir, percentage_callback, cancel)

    @staticmethod
    def estimate_size(steps, fields):
        text = NOAAGFSWaveSource._estimate_custom_size(steps, fields)
        value, unit = text.replace("~", "").split()
        multiplier = {"KB": 1024, "MB": 1024**2}.get(unit.upper())
        return max(1, round(float(value) * multiplier)), True
