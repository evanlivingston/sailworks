"""Blocking ERA5 wind-coverage adapter for historical routing."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import time

from sailworks.core.era5 import Era5DownloadService, plan_request
from sailworks.core.historicaljob import JobProgress
from sailworks.core.historicaltranche import read_era5_catalog, select_window_entries


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
    """Whether the ERA5 catalog already supplies this unit's wind window."""
    try:
        entries = read_era5_catalog(Path(root))
        chosen = select_window_entries(
            entries,
            unit.start_utc,
            unit.end_utc,
            unit.region,
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
                unit.region,
                fields=("wind",),
            )
            pending.append((unit, plan))
        total = len(pending)
        report_progress(progress, "acquiring", 0, total, "ERA5 wind coverage")
        threads = []
        for index, (unit, plan) in enumerate(pending):
            thread = self.service.submit(plan)
            threads.append((unit, plan, thread))
            report_progress(
                progress,
                "acquiring",
                index + 1,
                total,
                f"queued {unit.start_utc:%Y-%m-%d}",
            )
        while True:
            alive = [thread for _unit, _plan, thread in threads if thread.is_alive()]
            if not alive:
                break
            if self._cancelled():
                for _unit, plan, _thread in threads:
                    self.service.cancel(plan)
                raise RuntimeError("ERA5 acquisition cancelled.")
            for unit, plan, thread in threads:
                self._raise_error(unit, plan, thread)
            done = total - len(alive)
            report_progress(
                progress, "acquiring", done, total, "downloading ERA5 wind data"
            )
            time.sleep(0.25)
        for unit, plan, thread in threads:
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
                f"ERA5 download failed for {unit.start_utc:%Y-%m-%d}: {error}"
            )
