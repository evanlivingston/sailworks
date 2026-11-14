"""Historical-years routing panel: options, results and job orchestration."""

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
from .historicalroutingoptions import HistoricalRoutingOptions
from .historicalroutingpresenter import apply as apply_results
from .historicalroutingresults import HistoricalRoutingResults
from .historicalroutingseams import HistoricalRoutingSeams


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

        root = Path(DATA_DIR) / "historical-routing"
        factory = store_factory or HistoricalRoutingStore
        self.store = factory(root)
        self.coordinator = HistoricalRoutingCoordinator(
            store=self.store,
            scene=scene_provider() if scene_provider is not None else None,
        )

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
        """Let the window attach the live scene collection after construction."""
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
        if self._thread is not None and self._thread.is_alive():
            return
        try:
            spec = self.spec()
            base = self._base_request()
            message = self.options.validation_message()
            if message:
                raise ValueError(message)
            preflight = getattr(self.coordinator, "preflight", None)
            if callable(preflight):
                prepared = preflight(spec, base)
                self.options.set_estimate(
                    f"{prepared.effective_years} years · "
                    f"{prepared.estimated_bytes / 1e9:.1f} GB wind data"
                )
        except (ValueError, OverflowError) as error:
            self.options.set_status(str(error))
            return
        self._cancel_event.clear()
        self._outcomes = {}
        self.options.set_running(True)
        self.options.set_status("Starting historical routing…")
        self._thread = threading.Thread(
            target=self._run_job,
            args=(spec, base),
            name="qt-historical-routing",
            daemon=True,
        )
        self._thread.start()

    def _run_job(self, spec, base):
        try:
            job_id = self.coordinator.start(
                spec,
                base,
                ensure_wind=self.ensure_wind,
                acquire_currents=self.acquire_currents,
                current_selection_factory=self.current_selection_factory,
                run_bound=self.run_bound,
                on_progress=lambda event: self.progress_reported.emit(
                    event.stage, event.completed, event.total, event.message
                ),
                on_outcome=lambda outcome: self.outcome_reported.emit(outcome),
            )
        except Exception as error:  # the UI thread shows the message
            self.job_finished.emit("", f"{type(error).__name__}: {error}")
            return
        self.job_finished.emit(str(job_id), "")

    def cancel_job(self):
        self._cancel_event.set()
        cancel = getattr(self.routing_controller, "cancel", None)
        if callable(cancel):
            cancel()
        self.options.set_status("Cancelling; the job stays resumable.")

    def retry_failed(self):
        raise NotImplementedError("Retry failed members is not wired yet.")

    def recheck_excluded(self):
        raise NotImplementedError("Rechecking excluded years is not wired yet.")

    # -- UI thread slots -------------------------------------------------
    def _progress(self, stage, completed, total, message):
        suffix = f" {completed}/{total}" if total else ""
        self.options.set_status(
            f"{stage.replace('_', ' ').title()}{suffix} {message}".strip()
        )

    def _outcome(self, outcome):
        member_id = str(getattr(outcome, "member_id", ""))
        if member_id:
            self._outcomes[member_id] = outcome

    def _finished(self, job_id, error):
        self.options.set_running(False)
        if error:
            self.options.set_status(error)
            return
        self._cancel_event.clear()
        try:
            self._record = self.store.load(job_id)
        except Exception as error:  # keep the run visible even if loading fails
            self.options.set_status(f"Could not load job {job_id}: {error}")
            return
        self._show_record(self._record)
        self.options.set_status(f"Historical job {str(job_id)[:8]} complete.")
        self._refresh_jobs()

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

    # -- route actions ---------------------------------------------------
    def _collection(self):
        return getattr(
            getattr(self.routing_controller, "core", None), "routingManager", None
        )

    def _routes_for_record(self, record):
        collection = self._collection()
        if collection is None:
            return []
        wanted = {
            str(getattr(member, "route_uid", ""))
            for member in (getattr(record, "members", {}) or {}).values()
            if getattr(member, "route_uid", "")
        }
        routes = []
        try:
            candidates = list(collection)
        except TypeError:
            candidates = []
        for route in candidates:
            if str(getattr(route, "uid", "")) in wanted:
                routes.append(route)
        return routes

    def delete_job(self):
        record = self._record
        if record is None:
            return
        collection = self._collection()
        routes = self._routes_for_record(record)
        self.coordinator.delete_job(record.job_id, routes, collection=collection)
        self._record = None
        self.results.clear()
        self.results.set_actions_enabled(False, False, False)
        self.options.set_status("Job archived and its routes removed.")
        self._refresh_jobs()

    def _member_selected(self, member_id):
        record = self._record
        if record is None:
            return
        member = (getattr(record, "members", {}) or {}).get(str(member_id))
        uid = str(getattr(member, "route_uid", "") or "") if member is not None else ""
        collection = self._collection()
        if not uid or collection is None:
            return
        try:
            candidates = list(collection)
        except TypeError:
            return
        for route in candidates:
            if str(getattr(route, "uid", "")) != uid:
                continue
            for name in ("select_route", "select", "activate"):
                select = getattr(collection, name, None)
                if callable(select):
                    try:
                        select(route)
                    except TypeError:
                        continue
                    return

    def _representatives(self):
        return None

    def _show_all(self):
        return None

    def _hide_all(self):
        return None
