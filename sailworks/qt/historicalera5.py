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

# CDS enforces a per-dataset limit on queued requests and asks clients to
# submit them sequentially: ``Maximum number queued requests for this dataset
# has been reached. Please configure your scripts to request data
# sequentially``. One submission at a time is slower than a worker pool, but a
# long job that submits several plans at once trips that limit and fails.
MAX_CONCURRENT = 1

_POLL_SECONDS = 0.25
# A queued-request rejection means the account's other jobs are still queued,
# so the same plan is retried after a bounded wait before it fails.
_QUEUE_ATTEMPTS = 3
_QUEUE_BACKOFF_SECONDS = (30.0, 60.0)
_QUEUE_MARKERS = (
    "queued requests",
    "queue limit",
    "maximum number queued",
)

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


def queue_limited(error):
    """Whether a CDS error reports the per-dataset queued-request limit.

    The limit is a queue depth, not a failure of the request itself: the same
    plan is submitted again once the queue drains.
    """
    lowered = str(error or "").lower()
    return any(marker in lowered for marker in _QUEUE_MARKERS)


def _sleep_seconds(seconds):
    """Sleep for ``seconds``; tests replace this to stay fast."""
    time.sleep(seconds)


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


class _Cancelled(Exception):
    """Internal signal: the cancel event fired during ERA5 acquisition."""


class Era5Coverage:
    """Ensure every acquisition unit has stored ERA5 wind before routing.

    Acquisition is deliberately sequential: CDS enforces a per-dataset limit on
    queued requests and requires clients to submit requests one at a time. A
    plan is submitted, waited for, and only then is the next plan submitted.
    That is slower than a worker pool, but it respects the CDS queue limit.
    """

    def __init__(self, store, *, service_factory=None, cancel_event=None):
        self.store = store
        self.root = Path(getattr(store, "root", "") or ".")
        self.cancel_event = cancel_event
        factory = service_factory or Era5DownloadService
        self.service = factory(store)

    def _cancelled(self):
        return self.cancel_event is not None and self.cancel_event.is_set()

    def _key(self, plan, thread):
        return getattr(thread, "era5_key", None) or self.service.key_for(plan)

    def _wait(self, plan, thread):
        """Wait for a submitted plan and return ``(key, error)``.

        ``error`` is None once the thread has finished without reporting a
        failure; a reported failure returns as soon as it appears.
        """
        key = self._key(plan, thread)
        while True:
            error = self.service.error_for(key)
            if error:
                return key, error
            if not thread.is_alive():
                # A thread can record its error as it finishes.
                return key, self.service.error_for(key)
            if self._cancelled():
                raise _Cancelled
            _sleep_seconds(_POLL_SECONDS)

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
        submitted = []
        try:
            for index, (unit, plan) in enumerate(pending):
                self._acquire(unit, plan, index, total, submitted, progress)
        except _Cancelled:
            for _unit, cancelled_plan, _thread in submitted:
                self.service.cancel(cancelled_plan)
            raise RuntimeError("ERA5 acquisition cancelled.") from None
        for unit, _plan, _thread in submitted:
            if not covered(self.root, unit):
                raise RuntimeError(
                    f"ERA5 wind coverage is still missing for {unit.start_utc:%Y-%m-%d}."
                )
        report_progress(progress, "acquired", total, total, "ERA5 wind coverage ready")

    def _acquire(self, unit, plan, index, total, submitted, progress):
        """Submit one plan at a time, retrying only when the CDS queue is full."""
        for attempt in range(1, _QUEUE_ATTEMPTS + 1):
            if self._cancelled():
                raise _Cancelled
            thread = self.service.submit(plan, retry=True)
            submitted.append((unit, plan, thread))
            report_progress(
                progress,
                "acquiring",
                index,
                total,
                f"queued {unit.start_utc:%Y-%m-%d} ({index + 1}/{total} submitted)",
            )
            key, error = self._wait(plan, thread)
            if not error:
                report_progress(
                    progress,
                    "acquiring",
                    index + 1,
                    total,
                    f"{index + 1}/{total} finished; downloading ERA5 wind data",
                )
                return
            self.service.clear_error(key)
            if not queue_limited(error):
                # Credentials/licence, transient and request errors stay final
                # for this plan with their existing classifications.
                raise RuntimeError(
                    f"ERA5 download failed for {unit.start_utc:%Y-%m-%d}: "
                    f"{error_message(error)}"
                )
            if attempt >= _QUEUE_ATTEMPTS:
                raise RuntimeError(
                    f"ERA5 download failed for {unit.start_utc:%Y-%m-%d}: the CDS "
                    f"queue limit is still reached after {_QUEUE_ATTEMPTS} attempts "
                    f"({error_message(error)}). Retry the download later."
                )
            delay = _QUEUE_BACKOFF_SECONDS[attempt - 1]
            report_progress(
                progress,
                "acquiring",
                index,
                total,
                f"waiting for the CDS queue ({delay:.0f}s before retry "
                f"{attempt}/{_QUEUE_ATTEMPTS - 1})",
            )
            _sleep_seconds(delay)
