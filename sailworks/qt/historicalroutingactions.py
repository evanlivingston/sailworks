"""Job execution, retry, publishing and visibility for the historical panel.

``HistoricalRoutingPanel`` owns the widgets and the durable state; this helper
does the work behind its buttons so the panel file stays a composition root.
The owner is the panel, and the mutable run state (thread, cancel event,
record, outcomes) stays on it so callers and tests see one object.
"""

from __future__ import annotations

import threading

from sailworks.core.scene import KIND_ROUTING

__all__ = ["HistoricalRoutingActions"]


class HistoricalRoutingActions:
    """Run the coordinator and route visibility on behalf of the panel."""

    def __init__(self, owner):
        self.owner = owner

    # -- job lifecycle ---------------------------------------------------
    def start_job(self):
        owner = self.owner
        if owner._thread is not None and owner._thread.is_alive():
            owner.options.set_status("A historical run is already in progress.")
            return
        try:
            spec = owner.spec()
            base = owner._base_request()
            message = owner.options.validation_message()
            if message:
                raise ValueError(message)
            owner._tranche_store()
            preflight = getattr(owner.coordinator, "preflight", None)
            if callable(preflight):
                prepared = preflight(spec, base)
                west, south, east, north = spec.region
                owner.options.set_estimate(
                    f"{prepared.effective_years} years · "
                    f"{prepared.estimated_bytes / 1e9:.1f} GB wind data · "
                    f"region {south:.0f}°–{north:.0f} lat, "
                    f"{west:.0f}°–{east:.0f} lon"
                )
        except Exception as error:
            owner.options.set_status(str(error))
            return
        owner._cancel_event.clear()
        owner._outcomes = {}
        owner.options.set_running(True)
        owner.options.set_status("Starting historical routing…")
        owner._thread = threading.Thread(
            target=owner._run_job,
            args=(spec, base),
            name="qt-historical-routing",
            daemon=True,
        )
        owner._thread.start()

    def run_job(self, spec, base):
        owner = self.owner
        try:
            job_id = owner.coordinator.start(
                spec,
                base,
                ensure_wind=owner.ensure_wind,
                acquire_currents=owner.acquire_currents,
                current_selection_factory=owner.current_selection_factory,
                run_bound=owner.run_bound,
                on_progress=lambda event: owner.progress_reported.emit(
                    event.stage, event.completed, event.total, event.message
                ),
                on_outcome=lambda outcome: owner.outcome_reported.emit(outcome),
            )
        except Exception as error:  # the UI thread shows the message
            owner.job_finished.emit("", f"{type(error).__name__}: {error}")
            return
        owner.job_finished.emit(str(job_id), "")

    def cancel_job(self):
        owner = self.owner
        owner._cancel_event.set()
        cancel = getattr(owner.routing_controller, "cancel", None)
        if callable(cancel):
            cancel()
        owner.options.set_status("Cancelling; the job stays resumable.")

    def retry_failed(self):
        """Re-run the indeterminate members of the current job."""
        owner = self.owner
        if owner._thread is not None and owner._thread.is_alive():
            return
        record = owner._record
        if record is None:
            owner.options.set_status("Select a historical job before retrying.")
            return
        try:
            base = owner._base_request()
        except (ValueError, OverflowError) as error:
            owner.options.set_status(str(error))
            return
        owner._cancel_event.clear()
        owner._outcomes = {}
        owner._retrying = True
        owner.options.set_running(True)
        owner.options.set_status("Retrying indeterminate members…")
        owner.results.set_actions_enabled(False, False, False)
        owner._thread = threading.Thread(
            target=owner._run_retry,
            args=(record.job_id, base),
            name="qt-historical-routing-retry",
            daemon=True,
        )
        owner._thread.start()

    def run_retry(self, job_id, base):
        """Run the coordinator's retry off the UI thread, as ``start_job`` does."""
        owner = self.owner
        try:
            owner.coordinator.retry_failed(
                job_id,
                base,
                ensure_wind=owner.ensure_wind,
                run_bound=owner.run_bound,
                current_selection_factory=owner.current_selection_factory,
                on_progress=lambda event: owner.progress_reported.emit(
                    event.stage, event.completed, event.total, event.message
                ),
                on_outcome=lambda outcome: owner.outcome_reported.emit(outcome),
            )
        except Exception as error:  # the UI thread shows the message
            owner.job_finished.emit(str(job_id), f"{type(error).__name__}: {error}")
            return
        owner.job_finished.emit(str(job_id), "")

    def recheck_excluded(self):
        """Re-examine the years the job's acquisition excluded."""
        owner = self.owner
        if owner._thread is not None and owner._thread.is_alive():
            return
        record = owner._record
        if record is None:
            owner.options.set_status("Select a historical job before rechecking.")
            return
        if not getattr(record, "excluded_years", ()):
            owner.options.set_status("No excluded years to recheck.")
            return
        try:
            base = owner._base_request()
        except (ValueError, OverflowError) as error:
            owner.options.set_status(str(error))
            return
        owner._cancel_event.clear()
        owner._outcomes = {}
        # A successful recheck may create routes, so reuse the retry
        # completion path that publishes them into the job's scene group.
        owner._retrying = True
        owner.options.set_running(True)
        owner.options.set_status("Rechecking excluded years…")
        owner.results.set_actions_enabled(False, False, False)
        owner._thread = threading.Thread(
            target=owner._run_recheck,
            args=(record.job_id, base),
            name="qt-historical-routing-recheck",
            daemon=True,
        )
        owner._thread.start()

    def run_recheck(self, job_id, base):
        """Run the coordinator's excluded-year recheck off the UI thread."""
        owner = self.owner
        try:
            owner.coordinator.recheck_excluded(
                job_id,
                base,
                ensure_wind=owner.ensure_wind,
                run_bound=owner.run_bound,
                current_selection_factory=owner.current_selection_factory,
                concurrency=None,
                on_progress=lambda event: owner.progress_reported.emit(
                    event.stage, event.completed, event.total, event.message
                ),
                on_outcome=lambda outcome: owner.outcome_reported.emit(outcome),
            )
        except Exception as error:  # the UI thread shows the message
            owner.job_finished.emit(str(job_id), f"{type(error).__name__}: {error}")
            return
        owner.job_finished.emit(str(job_id), "")

    # -- UI thread slots -------------------------------------------------
    def progress(self, stage, completed, total, message):
        owner = self.owner
        if str(stage).startswith("acquir"):
            # The progress bars carry the counts and the current hour, so the
            # status line stays a short phase label instead of duplicating it.
            owner.options.set_weather_progress(completed, total, message)
            owner.options.set_status("Acquiring weather")
            return
        suffix = f" {completed}/{total}" if total else ""
        owner.options.set_status(
            f"{stage.replace('_', ' ').title()}{suffix} {message}".strip()
        )

    def outcome(self, outcome):
        member_id = str(getattr(outcome, "member_id", ""))
        if member_id:
            self.owner._outcomes[member_id] = outcome

    def finished(self, job_id, error):
        owner = self.owner
        owner.options.set_running(False)
        owner.options.progress_widget.set_finished(not error, error or "")
        retried = owner._retrying
        owner._retrying = False
        if error:
            owner.options.set_status(error)
            if owner._record is not None:
                owner._show_record(owner._record)
            return
        owner._cancel_event.clear()
        try:
            owner._record = owner.store.load(job_id)
        except Exception as error:  # keep the run visible even if loading fails
            owner.options.set_status(f"Could not load job {job_id}: {error}")
            return
        owner._show_record(owner._record)
        if retried:
            self.publish_routes(owner._record)
        owner.options.set_status(f"Historical job {str(job_id)[:8]} complete.")
        owner._refresh_jobs()

    # -- route actions ---------------------------------------------------
    def publish_routes(self, record):
        """Attach a retry's new routes to the job's scene group."""
        owner = self.owner
        routes = self.routes_for_record(record)
        if not routes:
            return
        try:
            owner.coordinator.publish_routes(record.job_id, routes)
        except Exception as error:  # routes stay usable even if grouping fails
            owner.options.set_status(
                f"Routes are not attached to the scene yet: {error}"
            )

    def collection(self):
        owner = self.owner
        return getattr(
            getattr(owner.routing_controller, "core", None), "routingManager", None
        )

    def routes_for_record(self, record):
        collection = self.collection()
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
        owner = self.owner
        record = owner._record
        if record is None:
            return
        collection = self.collection()
        routes = self.routes_for_record(record)
        owner.coordinator.delete_job(record.job_id, routes, collection=collection)
        owner._record = None
        owner.results.clear()
        owner.results.set_actions_enabled(False, False, False)
        owner.options.set_status("Job archived and its routes removed.")
        owner._refresh_jobs()

    def member_selected(self, member_id):
        owner = self.owner
        record = owner._record
        if record is None:
            return
        member = (getattr(record, "members", {}) or {}).get(str(member_id))
        uid = str(getattr(member, "route_uid", "") or "") if member is not None else ""
        collection = self.collection()
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

    def result_uids(self):
        """Route uids named by the current result rows."""
        record = self.owner._record
        if record is None:
            return []
        uids = []
        for member in (getattr(record, "members", {}) or {}).values():
            uid = str(getattr(member, "route_uid", "") or "")
            if uid and uid not in uids:
                uids.append(uid)
        return uids

    def apply_item_visibility(self, wanted):
        """Set ``{uid: visible}`` on routes and the attached scene, then notify.

        Only item visibility moves; group visibility is left untouched so the
        job's scene group keeps its own checkbox state.
        """
        owner = self.owner
        if not wanted:
            return False
        changed = False
        collection = self.collection()
        if collection is not None:
            try:
                routes = list(collection)
            except TypeError:
                routes = []
            for route in routes:
                uid = str(getattr(route, "uid", "") or "")
                if uid not in wanted:
                    continue
                desired = bool(wanted[uid])
                if bool(getattr(route, "visible", True)) != desired:
                    route.visible = desired
                    changed = True
            if changed:
                save = getattr(collection, "save", None)
                if callable(save):
                    save()
        scene = getattr(owner.coordinator, "scene", None)
        if scene is not None:
            for uid, visible in wanted.items():
                setter = getattr(scene, "set_item_visible", None)
                if callable(setter):
                    setter(KIND_ROUTING, uid, bool(visible))
            scene.save()
        if changed or scene is not None:
            owner.visibility_changed.emit()
        return changed

    def representatives(self):
        """Show only the median-duration finished route the results picked."""
        owner = self.owner
        member_id = str(getattr(owner.results, "_median_member_id", "") or "")
        record = owner._record
        member = None
        if member_id and record is not None:
            member = (getattr(record, "members", {}) or {}).get(member_id)
        keep = str(getattr(member, "route_uid", "") or "")
        if not keep:
            owner.options.set_status("No finished median route to show.")
            return
        wanted = {uid: uid == keep for uid in self.result_uids()}
        self.apply_item_visibility(wanted)

    def show_all(self):
        self.apply_item_visibility({uid: True for uid in self.result_uids()})

    def hide_all(self):
        self.apply_item_visibility({uid: False for uid in self.result_uids()})
