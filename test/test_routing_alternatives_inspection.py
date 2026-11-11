"""Alternatives, diagnostics and offline inspection context for results."""

from __future__ import annotations

from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication

from sailworks.core import storage
from sailworks.core.geo.routing import Routing
from sailworks.core.geo.routingcollection import RoutingCollection
from sailworks.core.marine.routinginspection import (
    InspectionContext,
    diagnostics,
)
from sailworks.core.marine.routingrecord import (
    Capabilities,
    Contour,
    EngineIdentity,
    Layer,
    Leg,
    Outcome,
    Point,
    Provenance,
    Trace,
    Validation,
)
from sailworks.core.marine.routingresult import RoutingResult, canonical_utc
from sailworks.core.marine.routingresultstore import RoutingResultStore
from sailworks.core.marine.routingselection import RoutingSelection
from sailworks.qt.routinginspector import RoutingInspectorPanel
from sailworks.qt.window import PreviewWindow

CALCULATION_A = "6f1c1a1e-0000-4000-8000-0000000000aa"
CALCULATION_B = "6f1c1a1e-0000-4000-8000-0000000000bb"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _result(
    calculation_id=CALCULATION_A,
    *,
    status="complete",
    validation="passed",
    independent=True,
    diagnostics_payload=None,
    geometry="sampled",
) -> RoutingResult:
    layers = tuple(
        Layer(
            f"layer-{index}",
            1767226500 + 1800 * index,
            canonical_utc(1767226500 + 1800 * index),
            index == 1,
            (Contour(f"c-{index}", True, (Point(48.0, -124.0), Point(48.1, -123.9))),),
        )
        for index in range(2)
    )
    leg = Leg(
        "leg-0",
        Point(48.2, -123.7),
        Point(48.4, -123.5),
        1767225600,
        "2026-01-01T00:00:00Z",
        1767226500,
        "2026-01-01T00:15:00Z",
        None,
        {"heading_degrees": 43.5, "warning_count": 0},
    )
    return RoutingResult(
        calculation_id=calculation_id,
        generation=1,
        sequence=1,
        status=status,
        outcome=Outcome(
            status == "complete",
            status == "complete",
            Validation(validation, independent if validation == "passed" else False),
            diagnostics_payload,
        ),
        engine_identity=EngineIdentity(
            "rgleason/weather_routing_pi",
            "4df1b519e75e664dd72eed62dbb62b4a3e02b8d1",
            "1.2.1",
            "1.1.0",
            "a" * 64,
            None,
        ),
        provenance=Provenance("2026-01-01T00:00:00Z", "recorded"),
        legs=(leg,),
        layers=layers,
        capabilities=Capabilities(
            "main",
            "per_position",
            "sampled" if geometry == "sampled" else "none",
            12 if geometry == "sampled" else None,
            1800 if geometry == "sampled" else None,
            {"inspection_geometry": geometry, "shoreline": "unsupported"},
        ),
        traces=(Trace("trace-0", Point(48.3, -123.6), (Point(48.2, -123.7),)),),
    )


def _states(result):
    return {state.code: state.state for state in diagnostics(result)}


def test_each_status_has_a_distinct_visible_state():
    complete = _states(_result())
    incomplete = _states(
        _result(status="incomplete", validation="unknown", independent=False)
    )
    failed = _states(_result(status="failed", validation="failed", independent=False))
    cancelled = _states(
        _result(status="cancelled", validation="unknown", independent=False)
    )
    preview = _states(
        _result(status="preview", validation="unknown", independent=False)
    )

    assert complete["status"].startswith("complete")
    assert incomplete["status"].startswith("incomplete")
    assert failed["status"].startswith("failed")
    assert cancelled["status"].startswith("cancelled")
    assert preview["status"].startswith("preview")
    assert (
        len(
            {
                complete["status"],
                incomplete["status"],
                failed["status"],
                cancelled["status"],
                preview["status"],
            }
        )
        == 5
    )
    assert incomplete["search"].startswith("incomplete")
    assert failed["validation"].startswith("failed")


def test_resource_exhaustion_uses_recorded_native_marker():
    result = _result(
        status="failed",
        validation="unknown",
        independent=False,
        diagnostics_payload={"diagnostics": {"reason": "resource_limit_reached"}},
    )
    states = _states(result)
    assert states["resource"].startswith("exhausted")
    assert "resource_limit_reached" in states["resource"]

    plain = _states(_result(status="failed", validation="failed", independent=False))
    assert "resource" not in plain


def test_geometry_only_claims_bounded_when_the_result_recorded_it():
    sampled = _states(_result())["inspection_geometry"]
    absent = _states(_result(geometry="absent"))["inspection_geometry"]

    assert "bounded" in sampled
    assert "sampled" in sampled
    assert "12 samples" in sampled
    assert "absent" in absent
    assert "bounded" not in absent


def test_inspection_context_round_trips_and_applies_without_recalculation():
    result = _result()
    selection = RoutingSelection(result)
    selection.select_layer("layer-1")
    selection.select_trace("trace-0")
    selection.select_leg("leg-0")
    context = InspectionContext.from_selection(selection)

    restored = InspectionContext.from_dict(context.to_dict())
    fresh = RoutingSelection(result)
    assert restored.apply(fresh) == ()
    assert fresh.layer_id == "layer-1"
    assert fresh.trace_id == "trace-0"
    assert fresh.leg_id == "leg-0"


def test_inspection_context_reports_identifiers_missing_from_the_artifact():
    result = _result()
    context = InspectionContext.from_dict(
        {
            "calculation_id": CALCULATION_A,
            "layer_id": "gone",
            "trace_id": "trace-0",
            "leg_id": "leg-0",
        }
    )
    fresh = RoutingSelection(result)
    assert context.apply(fresh) == ("layer_id",)
    assert fresh.layer_id is None
    assert fresh.trace_id == "trace-0"


def test_context_persists_alongside_the_result_reference(tmp_path):
    collection = RoutingCollection(RoutingResultStore(tmp_path))
    result = _result()
    route = collection.save_result(
        "alternative-a",
        result,
        inspection_context={
            "calculation_id": CALCULATION_A,
            "layer_id": "layer-1",
            "trace_id": "trace-0",
            "leg_id": "leg-0",
        },
    )
    payload = route.to_json()
    reloaded = Routing.from_json(payload)

    assert list(tmp_path.iterdir()), "artifact must live in the injected store root"
    assert reloaded.result_ref == route.result_ref
    assert reloaded.inspection_context == route.inspection_context
    assert reloaded.isochrones == ()


def test_panel_lists_alternatives_and_keeps_selections_per_calculation(app):
    first = _result(CALCULATION_A)
    second = _result(CALCULATION_B)
    panel = RoutingInspectorPanel()
    panel.set_alternatives(["a", "b"], None)
    assert panel.alternative_combo.currentIndex() == -1
    panel.set_alternatives(["a", "b"], 0)
    panel.set_result(first)
    panel.layer_combo.setCurrentIndex(1)
    first_selection = panel.selection
    assert first_selection.layer_id == "layer-1"

    switched = []
    panel.alternative_selected.connect(switched.append)
    panel.set_alternatives(["a", "b"], 1)
    panel.alternative_combo.setCurrentIndex(0)

    assert switched[-1] == 0
    # Presenting another calculation installs its own independent selection.
    panel.set_result(second)
    assert panel.selection.calculation_id == CALCULATION_B
    assert panel.selection is not first_selection
    assert panel.selection.layer_id == "layer-0"

    # Returning to the first alternative restores its own selection.
    panel.set_result(first, first_selection)
    assert panel.selection is first_selection
    assert panel.selection.layer_id == "layer-1"


def test_panel_restores_layer_trace_and_leg_context(app):
    result = _result()
    context = InspectionContext.from_dict(
        {
            "calculation_id": CALCULATION_A,
            "layer_id": "layer-1",
            "trace_id": "trace-0",
            "leg_id": "leg-0",
        }
    )
    panel = RoutingInspectorPanel()
    panel.set_result(result, context=context)

    assert panel.layer_combo.currentText() == "layer-1"
    assert panel.leg_combo.currentText() == "leg-0"
    assert panel.trace_label.text() == "trace-0"
    assert panel.inspection_context.to_dict() == context.to_dict()


def test_panel_reports_context_that_the_artifact_no_longer_records(app):
    result = _result()
    panel = RoutingInspectorPanel()
    panel.set_result(
        result,
        context={
            "calculation_id": CALCULATION_A,
            "layer_id": "gone",
            "trace_id": None,
            "leg_id": "leg-0",
        },
    )

    assert "stored layer_id no longer recorded" in panel.diagnostics.text()
    assert panel.layer_combo.currentText() == "layer-0"


def test_panel_keeps_an_invalid_stored_context_visible(app):
    panel = RoutingInspectorPanel()
    panel.set_result(
        _result(),
        context_error="calculation_id must be a non-empty string",
    )

    assert "status: complete" in panel.diagnostics.text()
    assert "inspection context: calculation_id must be a non-empty string" in (
        panel.diagnostics.text()
    )


def test_panel_clears_stale_data_when_the_artifact_is_unavailable(app):
    panel = RoutingInspectorPanel()
    panel.set_result(_result())

    panel.show_error("Routing result unavailable: artifact missing")

    assert panel.selection is None
    assert panel.layer_combo.count() == 0
    assert panel.readout.toPlainText() == ""
    assert "artifact missing" in panel.diagnostics.text()


def test_panel_shows_distinct_resource_and_validation_states(app):
    panel = RoutingInspectorPanel()
    panel.set_result(
        _result(
            status="failed",
            validation="failed",
            independent=False,
            diagnostics_payload={"warnings": ["resource limit reached"]},
        )
    )
    text = panel.diagnostics.text()
    assert "status: failed" in text
    assert "resource: exhausted" in text
    assert "validation: failed" in text


def test_resource_and_internal_statuses_are_distinct_from_incomplete():
    resource = _states(
        _result(
            status="resource_limit_reached", validation="unknown", independent=False
        )
    )
    internal = _states(
        _result(status="internal_error", validation="unknown", independent=False)
    )
    incomplete = _states(
        _result(status="incomplete", validation="unknown", independent=False)
    )
    failed = _states(_result(status="failed", validation="failed", independent=False))

    assert resource["status"].startswith("resource exhausted")
    assert internal["status"].startswith("internal error")
    assert incomplete["status"].startswith("incomplete")
    assert failed["validation"].startswith("failed")
    assert len({resource["status"], internal["status"], incomplete["status"]}) == 3
    # A native resource status is evidence on its own; internal failure is not.
    assert resource["resource"].startswith("exhausted")
    assert "resource" not in internal


def test_panel_preflights_context_from_another_calculation(app):
    first = _result(CALCULATION_A)
    second = _result(CALCULATION_B)
    panel = RoutingInspectorPanel()
    panel.set_result(first)
    previous_selection = panel.selection
    previous_selection.select_layer("layer-1")
    foreign = InspectionContext(
        CALCULATION_A, layer_id="layer-1", trace_id="trace-0", leg_id="leg-0"
    )

    panel.set_result(second, context=foreign)

    assert panel.selection.calculation_id == CALCULATION_B
    assert panel.selection.layer_id == "layer-0"
    assert "stored context belongs to calculation" in panel.diagnostics.text()
    # The previous calculation's selection is untouched, while the panel now
    # listens to the new one.
    assert previous_selection.layer_id == "layer-1"
    panel.selection.select_layer("layer-1")
    assert panel.layer_combo.currentText() == "layer-1"


def test_panel_never_mutates_a_selection_from_another_calculation(app):
    first = _result(CALCULATION_A)
    second = _result(CALCULATION_B)
    panel = RoutingInspectorPanel()
    panel.set_result(first)
    foreign = panel.selection
    foreign.select_layer("layer-1")

    panel.set_result(second, foreign)

    assert panel.selection is not foreign
    assert panel.selection.calculation_id == CALCULATION_B
    assert foreign.calculation_id == CALCULATION_A
    assert foreign.layer_id == "layer-1"
    assert "another calculation" in panel.diagnostics.text()


def _offline_window(collection, panel):
    model_result = collection.load_result(collection.elements[0])
    window = SimpleNamespace(
        controller=SimpleNamespace(
            core=SimpleNamespace(routingManager=collection),
            routing=SimpleNamespace(
                native_selection=RoutingSelection(model_result),
                show_native_result=lambda loaded: SimpleNamespace(result=loaded),
            ),
        ),
        routing_inspector=panel,
        routing_inspector_dock=Mock(),
        _select_saved_route=Mock(),
        _sync_route_isochrones=Mock(),
        _routing_selection_changed=Mock(),
    )
    # Bind the real presentation method so the fixture exercises production
    # window code rather than stubbing the inspector hand-off.
    window._present_routing_result = MethodType(
        PreviewWindow._present_routing_result, window
    )
    return window


def test_offline_reload_restores_selection_through_window_without_forecasts(
    tmp_path, app, monkeypatch
):
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    store = RoutingResultStore(tmp_path / "artifacts")
    writer = RoutingCollection(store)
    result = _result()
    selection = RoutingSelection(result)
    selection.select_layer("layer-1")
    selection.select_trace("trace-0")
    selection.select_leg("leg-0")
    writer.append(
        writer.save_result(
            "alternative-a",
            result,
            inspection_context=InspectionContext.from_selection(selection).to_dict(),
        )
    )
    writer.save()

    # Offline reload: the fresh collection reads the persisted JSON and the
    # referenced artifact, with no forecast or recalculation available.
    reader = RoutingCollection(store)
    assert [route.name for route in reader.elements] == ["alternative-a"]
    panel = RoutingInspectorPanel()
    window = _offline_window(reader, panel)

    PreviewWindow._show_saved_routing_route(window, reader.elements[0])

    assert panel.selection.calculation_id == CALCULATION_A
    assert panel.layer_combo.currentText() == "layer-1"
    assert panel.trace_label.text() == "trace-0"
    assert "inspection context" not in panel.diagnostics.text()


def test_window_preflights_context_saved_beside_another_calculation(tmp_path, app):
    store = RoutingResultStore(tmp_path)
    writer = RoutingCollection(store)
    saved = writer.save_result(
        "alternative-b",
        _result(CALCULATION_B),
        inspection_context={
            "calculation_id": CALCULATION_A,
            "layer_id": "layer-1",
            "trace_id": "trace-0",
            "leg_id": "leg-0",
        },
    )

    reader = RoutingCollection(store)
    reader.load_json({"elements": [saved.to_json()], "activeElement": None})
    panel = RoutingInspectorPanel()
    window = _offline_window(reader, panel)

    PreviewWindow._show_saved_routing_route(window, reader.elements[0])

    assert panel.selection.calculation_id == CALCULATION_B
    assert panel.layer_combo.currentText() == "layer-0"
    assert "belongs to calculation" in panel.diagnostics.text()
