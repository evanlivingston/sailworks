"""Blocking ERA5 wind-coverage adapter for historical routing."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import re
import time

from sailworks.core.era5 import Era5DownloadService, plan_request
from sailworks.core.historicaljob import JobProgress
from sailworks.core.historicaltranche import read_era5_catalog, select_window_entries
from sailworks.core.historicalweather import era5_area

# CDS queues every submitted request, so a long job that submits all of its
# plans at once trips its per-user request limit and fails with HTTP errors.
MAX_CONCURRENT = 4

_CREDENTIAL_STATUS = (401, 403)
_TRANSIENT_STATUS = (429, 500, 502, 503, 504)
_BAD_REQUEST_STATUS = (400, 422)
_TRANSIENT_MARKERS = (
    "httperror",
    "rate limit",
    "too many requests",
    "service unavailable",
    "temporarily",
    "timed out",
    "timeout",
    "connection reset",
    "bad gateway",
)
_CREDENTIAL_MARKERS = (
    "unauthor",
    "forbidden",
    "credential",
    "api key",
    "token",
    "licence",
    "license",
    "dataset terms",
)


def http_status(error):
    """The HTTP status embedded in a CDS error string, if one is present."""
    for status in _CREDENTIAL_STATUS + _TRANSIENT_STATUS + _BAD_REQUEST_STATUS:
        if re.search(rf"(?<!\d){status}(?!\d)", str(error or "")):
            return status
    return None


def error_message(error):
    """Explain an ERA5 failure honestly instead of blaming the credentials."""
    text = str(error or "").strip()
    status = http_status(text)
    if status in _CREDENTIAL_STATUS:
        return f"CDS credentials or dataset licence (HTTP {status}): {text}"
    if status in _TRANSIENT_STATUS:
        # Do not repeat the raw text: older service messages blamed the
        # credentials for what was actually a rate limit or an outage.
        return (
            f"CDS temporarily refused the request (HTTP {status}: rate limit or "
            "service error); retry shortly."
        )
    if status in _BAD_REQUEST_STATUS:
        return f"CDS rejected the request (HTTP {status}): {text}"
    lowered = text.lower()
    if any(marker in lowered for marker in _TRANSIENT_MARKERS):
        return (
            "CDS temporarily refused the request (rate limit or service error); "
            "retry shortly."
        )
    if any(marker in lowered for marker in _CREDENTIAL_MARKERS):
        return f"CDS credentials or dataset licence: {text}"
    return text


def report_progress(progress, stage, completed, total, message=""):
    """Call a progress callback in either accepted shape.

    The coordinator forwards a ``JobProgress`` callback; downloader-style
    adapters and tests pass four positional values.
    """
    if progress is None:
        return
    try:
        progress(JobProgress(str(stage), int(completed), int(total), str(message)))
    except TypeError:
        progress(str(stage), int(completed), int(total), str(message))


def covered(root, unit):
    """Whether the ERA5 catalog already supplies this unit's wind window.

    ``unit.region`` is our ``(west, south, east, north)`` box; catalog entries
    and :func:`select_window_entries` use ERA5's ``(north, west, south,
    east)`` order.
    """
    try:
        entries = read_era5_catalog(Path(root))
        chosen = select_window_entries(
            entries,
            unit.start_utc,
            unit.end_utc,
            era5_area(unit.region),
            fields=("wind",),
        )
    except Exception:
        return False
    return bool(chosen)


class Era5Coverage:
    """Ensure every acquisition unit has stored ERA5 wind before routing."""

    def __init__(self, store, *, service_factory=None, cancel_event=None):
        self.store = store
        self.root = Path(getattr(store, "root", "") or ".")
        self.cancel_event = cancel_event
        factory = service_factory or Era5DownloadService
        self.service = factory(store)

    def _cancelled(self):
        return self.cancel_event is not None and self.cancel_event.is_set()

    def ensure(self, units, progress=None):
        pending = []
        for unit in tuple(units or ()):
            if covered(self.root, unit):
                continue
            plan = plan_request(
                unit.start_utc,
                unit.end_utc - timedelta(hours=1),
                era5_area(unit.region),
                fields=("wind",),
            )
            pending.append((unit, plan))
        total = len(pending)
        report_progress(progress, "acquiring", 0, total, "ERA5 wind coverage")
        remaining = list(pending)
        active = []
        submitted = []

        def fill():
            # Keep only MAX_CONCURRENT plans queued at CDS; each new plan starts
            # as an earlier one finishes.
            while remaining and len(active) < MAX_CONCURRENT:
                unit, plan = remaining.pop(0)
                thread = self.service.submit(plan, retry=True)
                active.append((unit, plan, thread))
                submitted.append((unit, plan, thread))
                report_progress(
                    progress,
                    "acquiring",
                    total - len(remaining) - len(active),
                    total,
                    f"queued {unit.start_utc:%Y-%m-%d} "
                    f"({len(submitted)}/{total} submitted)",
                )

        fill()
        while active:
            if self._cancelled():
                for _unit, plan, _thread in submitted:
                    self.service.cancel(plan)
                raise RuntimeError("ERA5 acquisition cancelled.")
            for entry in tuple(active):
                unit, plan, thread = entry
                self._raise_error(unit, plan, thread)
                if not thread.is_alive():
                    active.remove(entry)
            completed = total - len(remaining) - len(active)
            report_progress(
                progress,
                "acquiring",
                completed,
                total,
                f"{len(submitted)}/{total} submitted, "
                f"{completed}/{total} finished; downloading ERA5 wind data",
            )
            fill()
            if active:
                time.sleep(0.25)
        for unit, plan, thread in submitted:
            self._raise_error(unit, plan, thread)
            if not covered(self.root, unit):
                raise RuntimeError(
                    f"ERA5 wind coverage is still missing for {unit.start_utc:%Y-%m-%d}."
                )
        report_progress(progress, "acquired", total, total, "ERA5 wind coverage ready")

    def _raise_error(self, unit, plan, thread):
        key = getattr(thread, "era5_key", None) or self.service.key_for(plan)
        error = self.service.error_for(key)
        if error:
            self.service.clear_error(key)
            raise RuntimeError(
                f"ERA5 download failed for {unit.start_utc:%Y-%m-%d}: "
                f"{error_message(error)}"
            )
