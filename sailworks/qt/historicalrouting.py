"""Historical-years routing panel: widgets, spec building and actions wiring."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import threading
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from sailworks.core.historicalcoordinator import HistoricalRoutingCoordinator
from sailworks.core.historicalrouting import HistoricalRoutingSpec, reference_year
from sailworks.core.historicalroutingstore import HistoricalRoutingStore
from sailworks.core.storage import DATA_DIR
from .historicalroutingactions import HistoricalRoutingActions
from .historicalroutingoptions import HistoricalRoutingOptions
from .historicalroutingpresenter import apply as apply_results
from .historicalroutingresults import HistoricalRoutingResults
from .historicalroutingseams import HistoricalRoutingSeams

__all__ = [
    "HistoricalRoutingActions",
    "HistoricalRoutingPanel",
    "RECHECK_UNAVAILABLE",
    "local_timezone_id",
]

#: Shown on the recheck button until the coordinator grows a recheck seam.
RECHECK_UNAVAILABLE = "Not available yet"


def local_timezone_id() -> str:
    """Best-effort IANA key for the process's local zone.

    The Qt frontend works in local wall clock, and the core validates specs with
    ``ZoneInfo``, so an IANA key is required rather than a fixed offset.
    """
    key = getattr(datetime.now().astimezone().tzinfo, "key", None)
    if isinstance(key, str) and key:
        return key
    try:
        resolved = Path("/etc/localtime").resolve()
    except OSError:
        return "UTC"
    parts = resolved.parts
    if "zoneinfo" in parts:
        suffix = parts[parts.index("zoneinfo") + 1 :]
        if suffix:
            return "/".join(suffix)
    return "UTC"


class HistoricalRoutingPanel(HistoricalRoutingSeams, QWidget):
    """The Compare page's Historical years view."""

    progress_reported = Signal(str, int, int, str)
    job_finished = Signal(str, str)
    outcome_reported = Signal(object)
    # Emitted after the panel changes a route's item visibility, so the window
    # can repaint the map overlay and the scene tree exactly as the tree's own
    # checkbox does. There is no scene/panel reference held on purpose.
    visibility_changed = Signal()

    def __init__(
        self,
        routing_controller,
        *,
        scene_provider=None,
        store_factory=None,
        request_provider=None,
        service_factory=None,
        parent=None,
    ):
        super().__init__(parent)
        self.routing_controller = routing_controller
        self.request_provider = request_provider
        self._service_factory = service_factory
        self._cancel_event = threading.Event()
        self._thread = None
        self._record = None
        self._outcomes = {}
        self._retrying = False

        root = Path(DATA_DIR) / "historical-routing"
        factory = store_factory or HistoricalRoutingStore
        self.store = factory(root)
        self.coordinator = HistoricalRoutingCoordinator(
            store=self.store,
            scene=scene_provider() if scene_provider is not None else None,
        )
        self.actions = HistoricalRoutingActions(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        selector = QHBoxLayout()
        selector.addWidget(QLabel("Job"))
        self.job_selector = QComboBox()
        self.job_selector.setAccessibleName("Historical job")
        self.job_selector.currentIndexChanged.connect(self._job_selected)
        selector.addWidget(self.job_selector, 1)
        layout.addLayout(selector)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.options = HistoricalRoutingOptions()
        self.results = HistoricalRoutingResults()
        splitter.addWidget(self.options)
        splitter.addWidget(self.results)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        self.options.run_requested.connect(lambda *_: self.start_job())
        self.options.cancel_requested.connect(self.cancel_job)
        self.options.edit_region_requested.connect(self.edit_region)
        self.options.values_changed.connect(self._values_changed)
        self.results.member_selected.connect(self._member_selected)
        self.results.delete_requested.connect(self.delete_job)
        self.results.retry_failed_requested.connect(self.retry_failed)
        self.results.recheck_requested.connect(self.recheck_excluded)
        self.results.representatives_requested.connect(self._representatives)
        self.results.show_all_requested.connect(self._show_all)
        self.results.hide_all_requested.connect(self._hide_all)

        self.progress_reported.connect(self._progress)
        self.job_finished.connect(self._finished)
        self.outcome_reported.connect(self._outcome)
        self._sync_recheck()
        self._refresh_jobs()

    # -- persistence -----------------------------------------------------
    def values(self):
        return self.options.values()

    def set_values(self, values):
        if isinstance(values, dict):
            self.options.set_values(values)

    def set_region(self, region):
        self.options.set_region(region)

    def edit_region(self):
        """Region capture belongs to the map; the window may connect to this."""

    def set_scene(self, scene):
        """Attach the live scene collection the window owns.

        The coordinator and its publisher are fixed to the scene at
        construction time, so both have to be re-pointed here; publishing the
        job's routes into its scene group otherwise silently does nothing.
        """
        self.coordinator.scene = scene
        self.coordinator.publisher.scene = scene

    # -- spec ------------------------------------------------------------
    def _base_request(self):
        if self.request_provider is None:
            raise ValueError("Historical routing needs a route request provider.")
        request = self.request_provider()
        if request is None:
            raise ValueError(
                "Build a route request before starting historical routing."
            )
        return request

    def _departure_local(self, departure):
        zone = ZoneInfo(local_timezone_id())
        if departure.tzinfo is None:
            return departure
        return departure.astimezone(zone).replace(tzinfo=None)

    @staticmethod
    def _region_from_request(request):
        points = [
            point
            for point in (request.start, request.destination, *request.waypoints)
            if point is not None
        ]
        if not points:
            return None
        west = min(float(point[0]) for point in points) - 1.0
        east = max(float(point[0]) for point in points) + 1.0
        south = min(float(point[1]) for point in points) - 1.0
        north = max(float(point[1]) for point in points) + 1.0
        return (west, south, east, north)

    def spec(self):
        """Build the spec from the options values and the form's departure."""
        values = self.options.values()
        base = self._base_request()
        departure = self._departure_local(base.departure)
        zone_id = local_timezone_id()
        region = values.get("region") or self._region_from_request(base)
        if region is None:
            raise ValueError("Select a region for the historical comparison.")
        region = tuple(float(value) for value in region)
        if values.get("region") != region:
            self.options.set_region(region)
        return HistoricalRoutingSpec(
            job_id="",
            created_at_utc=datetime.now(timezone.utc),
            selected_departure_local=departure.replace(second=0, microsecond=0),
            timezone_id=zone_id,
            reference_year=reference_year(departure, zone_id),
            years=int(values["years"]),
            offset_days=int(values["offset_days"]),
            max_duration_hours=int(values["max_duration_hours"]),
            region=region,
            backend="native",
            requested_backend="native",
        )

    # -- job lifecycle ---------------------------------------------------
    def start_job(self):
        self.actions.start_job()

    def _run_job(self, spec, base):
        self.actions.run_job(spec, base)

    def cancel_job(self):
        self.actions.cancel_job()

    def retry_failed(self):
        """Re-run the indeterminate members of the current job."""
        self.actions.retry_failed()

    def _run_retry(self, job_id, base):
        self.actions.run_retry(job_id, base)

    def recheck_excluded(self):
        """Deliberately inert: the coordinator has no recheck seam yet.

        The button stays disabled, so this only guards a stray signal and never
        raises inside the UI thread.
        """

    def _sync_recheck(self):
        """Keep recheck honestly unavailable rather than a raising stub."""
        button = self.results.recheck_button
        button.setEnabled(False)
        button.setToolTip(RECHECK_UNAVAILABLE)

    # -- UI thread slots -------------------------------------------------
    def _progress(self, stage, completed, total, message):
        self.actions.progress(stage, completed, total, message)

    def _outcome(self, outcome):
        self.actions.outcome(outcome)

    def _finished(self, job_id, error):
        self.actions.finished(job_id, error)

    def _values_changed(self):
        self.options.set_status("")

    def _job_selected(self, _index):
        job_id = self.job_selector.currentData()
        if not job_id:
            return
        try:
            self._record = self.store.load(str(job_id))
        except Exception as error:
            self.options.set_status(f"Could not load job: {error}")
            return
        self._show_record(self._record)

    def _refresh_jobs(self):
        list_jobs = getattr(self.store, "list_jobs", None)
        records = []
        if callable(list_jobs):
            try:
                records = list(list_jobs())
            except Exception:
                records = []
        self.job_selector.blockSignals(True)
        self.job_selector.clear()
        self.job_selector.addItem("Select a job…", "")
        for record in records[-20:][::-1]:
            label = f"{str(record.created_at_utc)[:10]} · {record.state}"
            self.job_selector.addItem(label, record.job_id)
        self.job_selector.blockSignals(False)

    # -- results ---------------------------------------------------------
    def _show_record(self, record):
        apply_results(self.results, record, self._outcomes)
        self._sync_recheck()

    def _publish_routes(self, record):
        self.actions.publish_routes(record)

    # -- route actions ---------------------------------------------------
    def _collection(self):
        return self.actions.collection()

    def _routes_for_record(self, record):
        return self.actions.routes_for_record(record)

    def delete_job(self):
        self.actions.delete_job()

    def _member_selected(self, member_id):
        self.actions.member_selected(member_id)

    def _result_uids(self):
        return self.actions.result_uids()

    def _apply_item_visibility(self, wanted):
        return self.actions.apply_item_visibility(wanted)

    def _representatives(self):
        self.actions.representatives()

    def _show_all(self):
        self.actions.show_all()

    def _hide_all(self):
        self.actions.hide_all()
