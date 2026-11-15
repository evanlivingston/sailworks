"""Blocking coordinator seams the historical routing panel supplies.

The mixin expects the owning panel to provide ``routing_controller``,
``options``, ``_service_factory`` and ``_cancel_event``. The panel's default
factory downloads ERA5 wind through the ARCO transport; the manual ERA5
download tab keeps using the CDS service directly.
"""

from __future__ import annotations

import threading

from sailworks.core.historicalcurrents import CurrentRequest, acquire_target_currents
from sailworks.core.historicalrouting import resolve_local_departure
from sailworks.core.marine.fields import FieldSelection
from .historicalera5 import Era5Coverage


class HistoricalRoutingSeams:
    """ERA5 coverage, current acquisition and bound-run adapters."""

    def _tranche_store(self):
        controller = self.routing_controller
        analysis = getattr(
            getattr(controller, "session_controller", None), "analysis", None
        )
        if analysis is None:
            analysis = getattr(controller, "analysis", None)
        if analysis is None:
            analysis = getattr(getattr(controller, "core", None), "analysis", None)
        store = getattr(getattr(analysis, "state", None), "store", None)
        if store is None:
            raise RuntimeError(
                "Historical routing needs the forecast analysis store. "
                "Open or refresh a forecast first."
            )
        return store

    def ensure_wind(self, units, progress=None):
        """Block until every acquisition unit is covered by the ERA5 catalog."""
        coverage = Era5Coverage(
            self._tranche_store(),
            service_factory=self._service_factory,
            cancel_event=self._cancel_event,
        )
        coverage.ensure(units, progress)

    def _marine_manager(self):
        manager = getattr(
            getattr(self.routing_controller, "core", None), "marine_manager", None
        )
        if manager is None:
            raise RuntimeError("Current acquisition requires a marine manager.")
        return manager

    def acquire_currents(self, prepared):
        spec = prepared.spec
        selected = spec.selected_departure_local
        target, _adjustments = resolve_local_departure(
            spec.reference_year,
            selected.month,
            selected.day,
            selected.hour,
            selected.minute,
            spec.timezone_id,
        )
        base = self._base_request()
        request = CurrentRequest(
            target_departure_utc=target,
            horizon_hours=int(spec.max_duration_hours),
            area=tuple(spec.region),
            priority=getattr(base, "current_priority", None) or "finest",
        )
        return acquire_target_currents(
            self._marine_manager(),
            request,
            cancel=self._cancel_event.is_set,
        )

    def current_selection_factory(self, binding):
        manager = self._marine_manager()
        fields = getattr(manager.fields, "fields", manager.fields)
        available = (
            fields
            if isinstance(fields, dict)
            else {field.id: field for field in fields}
        )
        selected = []
        for field_id in binding.field_ids:
            field = available.get(field_id)
            if field is None:
                raise RuntimeError(f"current field {field_id} is no longer stored")
            selected.append(field)
        return FieldSelection(selected, priority=binding.priority)

    def run_bound(self, executions, *, on_outcome=None, concurrency=None):
        done = threading.Event()
        outcomes = []
        errors = []

        def finished(result, error):
            outcomes.extend(result or ())
            errors.append(error)
            done.set()

        started = self.routing_controller.start_bound_runs(
            executions,
            concurrency=concurrency,
            on_finished=finished,
        )
        if not started:
            message = getattr(self.routing_controller, "error", "") or "could not start"
            raise RuntimeError(f"Bound executions refused: {message}")
        while not done.wait(0.1):
            if self._cancel_event.is_set():
                cancel = getattr(self.routing_controller, "cancel", None)
                if callable(cancel):
                    cancel()
        if errors and errors[0]:
            raise RuntimeError(str(errors[0]))
        for outcome in outcomes:
            if on_outcome is not None:
                on_outcome(outcome)
        return outcomes
