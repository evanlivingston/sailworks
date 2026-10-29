"""ECMWF IFS/AIFS Single open-data GRIB sources (CC BY 4.0).

Catalogues are discovered from ECMWF's public HTTP listings. The request bounds
are retained as metadata; these providers do not claim server-side subsetting.
Source attribution: ECMWF, licensed under CC BY 4.0
(https://creativecommons.org/licenses/by/4.0/).
"""

from datetime import date, datetime, timezone
import json
import os
import tempfile
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from .base import GribSource
from .ecmwfcatalog import (
    CYCLE_PATTERN,
    DATE_PATTERN,
    cycle_hour,
    listed_names,
    optional_catalog_text,
    parse_pinned_identifier,
    session_for,
)
from .ecmwfindex import read_selected_index
from .ranged import HTTPStatusError, _check_cancel, append_message, fetch_text
from .parallel import MAX_CONCURRENT_FORECAST_REQUESTS, write_ordered_payloads

BASE_URL = "https://data.ecmwf.int/forecasts/"
_FIELDS = {
    "wind": ("10u", "10v"),
    "pressure": ("msl",),
}
_PRODUCTS = {
    "ifs": ("IFS", "ECMWF IFS", "od"),
    "aifs-single": ("AIFS Single", "ECMWF AIFS Single", "ai"),
}


class _ECMWFSource(GribSource):
    model_path = ""

    def __init__(self, session=None):
        self._session = session
        self.bounds = None

    @property
    def name(self):
        return _PRODUCTS[self.model_path][1]

    @property
    def display_name(self):
        return _PRODUCTS[self.model_path][0]

    @property
    def product_class(self):
        return _PRODUCTS[self.model_path][2]

    def _schedule(self, cycle):
        hour = int(cycle)
        if self.model_path == "aifs-single":
            return tuple(range(0, 361, 6))
        maximum = 360 if hour in (0, 12) else 144
        return tuple(range(0, 145, 3)) + (
            tuple(range(150, 361, 6)) if maximum == 360 else ()
        )

    @staticmethod
    def _options(options):
        from .options import DownloadOptions

        if options is None:
            return DownloadOptions(120, 0, ("wind", "pressure"), ())
        if not isinstance(options, DownloadOptions):
            raise TypeError("options must be DownloadOptions")
        if (
            type(options.hours) is not int
            or type(options.interval) is not int
            or isinstance(options.fields, (str, bytes))
            or isinstance(options.steps, (str, bytes))
        ):
            raise ValueError("Invalid ECMWF download options")
        fields, steps = tuple(options.fields), tuple(options.steps)
        if (
            not fields
            or len(set(fields)) != len(fields)
            or set(fields) - set(_FIELDS)
            or any(type(step) is not int for step in steps)
            or tuple(sorted(set(steps))) != steps
        ):
            raise ValueError("ECMWF supports wind and pressure fields")
        return DownloadOptions(options.hours, options.interval, fields, steps)

    def _steps(self, options, cycle):
        schedule = self._schedule(cycle)
        if options.hours > max(schedule):
            raise ValueError("Requested horizon exceeds the ECMWF run maximum")
        if options.steps:
            if any(
                step not in schedule or (options.interval and step % options.interval)
                for step in options.steps
            ):
                raise ValueError(
                    "Explicit forecast steps are unsupported by this ECMWF run"
                )
            return tuple(options.steps)
        steps = tuple(
            step
            for step in schedule
            if step <= options.hours
            and (not options.interval or step % options.interval == 0)
        )
        if not steps:
            raise ValueError("No ECMWF forecast steps satisfy the request")
        return steps

    def _discover(self, options, cancel):
        maximum_schedule = self._schedule("00")
        if options.hours > max(maximum_schedule):
            raise ValueError("Requested horizon exceeds the ECMWF product maximum")
        if any(step not in maximum_schedule for step in options.steps):
            raise ValueError("Explicit steps are unsupported by the ECMWF product")
        session, close_session = session_for(self._session)
        try:
            root_text = fetch_text(session, BASE_URL, cancel=cancel)
            today = datetime.now(timezone.utc).date()
            dates = sorted(
                (
                    value
                    for value in listed_names(BASE_URL, root_text)
                    if DATE_PATTERN.fullmatch(value)
                    and datetime.strptime(value, "%Y%m%d").date() <= today
                ),
                reverse=True,
            )[:3]
            found = []
            for date_text in dates:
                _check_cancel(cancel)
                date_url = urljoin(BASE_URL, date_text + "/")
                date_text_listing = self._catalog_text(session, date_url, cancel)
                if date_text_listing is None:
                    continue
                cycles = sorted(
                    (
                        name
                        for name in listed_names(date_url, date_text_listing)
                        if CYCLE_PATTERN.fullmatch(name)
                    ),
                    reverse=True,
                )
                for cycle_directory in cycles:
                    _check_cancel(cancel)
                    cycle = cycle_hour(cycle_directory)
                    if options.hours > max(self._schedule(cycle)) or any(
                        step not in self._schedule(cycle) for step in options.steps
                    ):
                        continue
                    steps = self._steps(options, cycle)
                    stream_url = urljoin(
                        BASE_URL,
                        f"{date_text}/{cycle}z/{self.model_path}/0p25/oper/",
                    )
                    listing = self._catalog_text(session, stream_url, cancel)
                    if listing is None:
                        continue
                    listed = listed_names(stream_url, listing)
                    prefix = date_text + cycle[:2] + "0000-"
                    complete = True
                    for step in steps:
                        stem = f"{prefix}{step}h-oper-fc"
                        if (
                            stem + ".grib2" not in listed
                            or stem + ".index" not in listed
                        ):
                            complete = False
                            break
                    if complete:
                        found.append((date_text, cycle[:2], steps))
                        if len(found) == 4:
                            return found
            found.sort(key=lambda item: (item[0], item[1]), reverse=True)
            return found[:4]
        finally:
            if close_session:
                session.close()

    @staticmethod
    def _catalog_text(session, url, cancel):
        try:
            return fetch_text(session, url, cancel=cancel)
        except HTTPStatusError as exc:
            if exc.status_code == 404:
                return None
            raise

    def get_download_list(self, options=None):
        selected = self._options(options)
        rows = []
        for date_text, cycle, steps in self._discover(selected, None):
            config = {
                "source": self.name,
                "date": date_text,
                "cycle": cycle,
                "hours": max(steps),
                "steps": list(steps),
                "fields": list(selected.fields),
            }
            rows.append(
                [
                    f"{self.display_name} · {max(steps)} h · {len(steps)} times",
                    self.name,
                    "ECMWF open data",
                    f"{date_text} {cycle}Z",
                    json.dumps(config, sort_keys=True, separators=(",", ":")),
                ]
            )
        return rows

    def _parse_identifier(self, identifier):
        return parse_pinned_identifier(identifier, self.name, _FIELDS, self._steps)

    def estimate_download_size(self, identifier, *, cancel=None):
        """Count selected indexed GRIB messages without downloading the fields."""
        date_text, cycle, fields, steps = self._parse_identifier(identifier)
        session, close_session = session_for(self._session)
        try:
            total = 0
            for step in steps:
                _check_cancel(cancel)
                stem = f"{date_text}{cycle}0000-{step}h-oper-fc"
                index_url = urljoin(
                    BASE_URL,
                    f"{date_text}/{cycle}z/{self.model_path}/0p25/oper/{stem}.index",
                )
                ranges, _expver = read_selected_index(
                    session,
                    index_url,
                    date_text,
                    cycle,
                    step,
                    fields,
                    _FIELDS,
                    self.model_path,
                    self.product_class,
                    cancel,
                )
                total += sum(length for _offset, length in ranges)
            if total <= 0:
                raise ValueError("ECMWF size estimate contains no selected messages")
            return total, False
        finally:
            if close_session:
                session.close()

    def download(self, identifier, dest_dir, percentage_callback, cancel=None):
        date_text, cycle, fields, steps = self._parse_identifier(identifier)
        if not os.path.isdir(dest_dir):
            raise ValueError("destination directory must exist")
        session, close_session = session_for(self._session)
        part_name = None
        try:
            _check_cancel(cancel)
            stream_url = urljoin(
                BASE_URL,
                f"{date_text}/{cycle}z/{self.model_path}/0p25/oper/",
            )
            listing = fetch_text(session, stream_url, cancel=cancel)
            listed = listed_names(stream_url, listing)
            messages = []
            selected_expver = None
            for step in steps:
                stem = f"{date_text}{cycle}0000-{step}h-oper-fc"
                if stem + ".grib2" not in listed or stem + ".index" not in listed:
                    raise ValueError(
                        "Pinned ECMWF run is missing a selected GRIB/index"
                    )
                index_url = urljoin(stream_url, stem + ".index")
                grib_url = urljoin(stream_url, stem + ".grib2")
                ranges, expver = read_selected_index(
                    session,
                    index_url,
                    date_text,
                    cycle,
                    step,
                    fields,
                    _FIELDS,
                    self.model_path,
                    self.product_class,
                    cancel,
                )
                if not ranges:
                    raise ValueError(
                        f"ECMWF index contains no selected messages at step {step}"
                    )
                if selected_expver is not None and expver != selected_expver:
                    raise ValueError("ECMWF run mixes experiment versions across leads")
                selected_expver = expver
                for offset, length in ranges:
                    messages.append((step, grib_url, offset, length))
            total_bytes = sum(length for _, _, _, length in messages)
            if not messages or total_bytes <= 0:
                raise ValueError(
                    "Pinned ECMWF request contains no selected GRIB messages"
                )
            fd, part_name = tempfile.mkstemp(
                dir=dest_dir, prefix=".ecmwf-", suffix=".part"
            )
            by_step = {step: [] for step in steps}
            for step, url, offset, length in messages:
                by_step[step].append((url, offset, length))

            def fetch(step):
                from io import BytesIO

                payload = BytesIO()
                worker_session, close_worker_session = session_for(self._session)
                try:
                    for url, offset, length in by_step[step]:
                        _check_cancel(cancel)
                        actual = append_message(
                            worker_session,
                            url,
                            offset,
                            length,
                            payload,
                            cancel=cancel,
                        )
                        if actual != length:
                            raise ValueError("ECMWF ranged message byte count mismatch")
                    return payload.getvalue()
                finally:
                    if close_worker_session:
                        worker_session.close()

            with os.fdopen(fd, "wb") as output:
                write_ordered_payloads(
                    output,
                    steps,
                    fetch,
                    lambda: _check_cancel(cancel),
                    lambda value: percentage_callback(min(99, value)),
                    max_workers=(
                        1
                        if self._session is not None
                        else MAX_CONCURRENT_FORECAST_REQUESTS
                    ),
                )
                output.flush()
                os.fsync(output.fileno())
            _check_cancel(cancel)
            final_name = self._output_filename(date_text, cycle)
            final_path = os.path.join(dest_dir, final_name)
            os.replace(part_name, final_path)
            part_name = None
            percentage_callback(100)
            return final_path
        finally:
            if part_name is not None and os.path.exists(part_name):
                os.unlink(part_name)
            if close_session:
                session.close()

    def _output_filename(self, date_text, cycle):
        return f"ecmwf-{self.model_path}-{date_text}-{cycle}z-{uuid4().hex}.grib2"


class ECMWFIFSSource(_ECMWFSource):
    model_path = "ifs"


class ECMWFAIFSSource(_ECMWFSource):
    model_path = "aifs-single"
