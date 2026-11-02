"""Bounded current downloads that run ahead of the routing worker.

The download worker never touches GTK, shoreline datasets or the live routing
selection. Completed immutable fields are installed at the next step boundary.
One I/O worker overlaps the calculation without concurrent NetCDF parsing or
multiplying provider request rates.
"""

import threading
from collections import deque
from dataclasses import dataclass
from typing import Optional

from .chs import DownloadCancelledError
from .fields import FieldSelection
from .routing import download_gap_coverage, gap_request_key, routing_utc


@dataclass
class CoverageJob:
    gaps: tuple
    result: Optional[tuple] = None
    delivered: bool = False
    downloaded: bool = False


class CoveragePrefetch:
    MAX_AHEAD = 32

    def __init__(self, manager, environment, gaps):
        self.manager = manager
        self.parent_cancel = environment.cancel
        self._stop = threading.Event()
        self._condition = threading.Condition()
        self._jobs: dict = {}
        self._queue: deque = deque()
        self._known = list(environment.selection.fields)
        self._thread = None
        groups = self._group(gaps)
        for key, values in list(groups.items())[: self.MAX_AHEAD]:
            self._jobs[key] = CoverageJob(tuple(values))
            self._queue.append(key)
        self._start()

    @staticmethod
    def _group(gaps):
        groups: dict = {}
        for gap in gaps:
            if gap.kind == "current" and gap.reason != "masked_or_missing":
                groups.setdefault(gap_request_key(gap), []).append(gap)
        return groups

    def _start(self):
        if self._thread is None and self._queue:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def is_set(self):
        return self._stop.is_set() or self.parent_cancel.is_set()

    def wait(self, seconds):
        # Provider cancellation interface, including callers that use Event.wait.
        from time import monotonic

        deadline = monotonic() + seconds
        while not self.is_set() and monotonic() < deadline:
            self._stop.wait(min(0.1, deadline - monotonic()))
        return self.is_set()

    def close(self):
        self._stop.set()
        with self._condition:
            self._condition.notify_all()

    @property
    def pending(self):
        with self._condition:
            return (
                sum(job.result is None for job in self._jobs.values())
                if not self.is_set()
                else 0
            )

    def _covered(self, gaps):
        selection = FieldSelection(tuple(self._known))
        return all(
            selection.sample(g.position[0], g.position[1], routing_utc(g.time)).status
            == "available"
            for g in gaps
        )

    def _run(self):
        while not self.is_set():
            with self._condition:
                while not self._queue and not self.is_set():
                    self._condition.wait(0.1)
                if self.is_set():
                    return
                key = self._queue.popleft()
                job = self._jobs[key]
            result: tuple
            try:
                if self._covered(job.gaps):
                    result = ((), ())
                else:
                    job.downloaded = True
                    result = download_gap_coverage(self.manager, job.gaps, self)
                with self._condition:
                    self._known.extend(
                        f for f in result[0] if f.id not in self.manager.fields.excluded
                    )
            except DownloadCancelledError:
                self.close()
                return
            except Exception as error:
                result = ((), (str(error),))
            with self._condition:
                job.result = result
                self._condition.notify_all()

    def ready(self):
        """Deliver finished fields once; never wait for speculative downloads."""
        return self._collect(())

    def _collect(self, requested):
        fields, issues = [], []
        with self._condition:
            for key, job in self._jobs.items():
                if job.result is not None and (not job.delivered or key in requested):
                    values, errors = job.result
                    fields.extend(values)
                    issues.extend(errors)
                    job.delivered = True
        return fields, issues

    def wait_for(self, gaps):
        """Join existing requests and prioritize newly encountered gaps."""
        groups = self._group(gaps)
        if len(groups) > self.MAX_AHEAD:
            raise ValueError(
                "Missing coverage spans more than 32 tiles. Download the route area first."
            )
        with self._condition:
            for key, values in reversed(list(groups.items())):
                job = self._jobs.get(key)
                if job is None:
                    job = self._jobs[key] = CoverageJob(tuple(values))
                    self._queue.appendleft(key)
                elif (
                    job.result is not None
                    and not job.downloaded
                    and not self._covered(values)
                ):
                    # A previous job could be skipped after an overlapping field
                    # filled its sampled positions. An actual gap elsewhere in
                    # the tile still gets one download attempt.
                    job.gaps = tuple(values)
                    job.result = None
                    job.delivered = False
                    self._queue.appendleft(key)
                elif key in self._queue:
                    self._queue.remove(key)
                    self._queue.appendleft(key)
            self._start()
            self._condition.notify_all()
            while any(self._jobs[key].result is None for key in groups):
                if self.is_set():
                    raise DownloadCancelledError()
                self._condition.wait(0.1)
            if self.is_set():
                raise DownloadCancelledError()
            return self._collect(groups)
