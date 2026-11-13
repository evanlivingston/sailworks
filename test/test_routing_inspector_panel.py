"""Tests for the Qt routing inspector panel."""

from __future__ import annotations

from dataclasses import replace

import pytest
from PySide6.QtWidgets import QApplication

from sailworks.core.marine.routingrecord import (
    Capabilities,
    Contour,
    EngineIdentity,
    Layer,
    Leg,
    Outcome,
    Point,
    Provenance,
    Validation,
)
from sailworks.core.marine.routingresult import RoutingResult, canonical_utc
from sailworks.qt.routinginspector import RoutingInspectorPanel

CALCULATION = "6f1c1a1e-0000-4000-8000-000000000009"


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    return instance


def _result() -> RoutingResult:
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
        calculation_id=CALCULATION,
        generation=1,
        sequence=1,
        status="complete",
        outcome=Outcome(True, True, Validation("passed", True)),
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
            "absent",
            "sampled",
            12,
            1800,
            {
                "inspection_geometry": "sampled",
                "waves": "absent",
                "shoreline": "unsupported",
                "depth": "unsupported",
            },
        ),
    )


def test_panel_lists_layers_with_exact_time_and_direction(app):
    panel = RoutingInspectorPanel()
    panel.set_result(_result())

    assert panel.layer_combo.count() == 2
    assert panel.layer_combo.currentText() == "layer-0"
    text = panel.time_label.text()
    assert "2026-01-01T00:15:00Z" in text
    assert "+0 s" in text
    assert "forward" in text

    panel.layer_combo.setCurrentIndex(1)
    text = panel.time_label.text()
    assert "2026-01-01T00:45:00Z" in text
    assert "+1800 s" in text
    assert "reverse" in text


def test_panel_diagnostics_lists_mark_roundings_by_name(app):
    rounding = {
        "mark": {"latitude": 48.255, "longitude": -123.6},
        "mark_name": "Duntze Rock",
        "side": "starboard",
        "radius_nm": 0.3,
        "achieved_closest_distance_nm": 0.12,
        "achieved_terminal_course_deg": 167.3,
        "satisfied": True,
        "failure_code": "none",
    }
    result = replace(
        _result(),
        provenance=Provenance("2026-01-01T00:00:00Z", "recorded", (rounding,)),
    )
    panel = RoutingInspectorPanel()
    panel.set_result(result)

    text = panel.diagnostics.text()
    assert "Mark roundings:" in text
    assert "Duntze Rock: starboard" in text
    assert "closest 0.12 nm" in text


def test_panel_readout_shows_recorded_values_and_unknown_absent_fields(app):
    panel = RoutingInspectorPanel()
    panel.set_result(_result())

    text = panel.readout.toPlainText()
    assert "heading_degrees: 43.5" in text
    assert "speed_over_ground_knots: unknown" in text
    assert "missing:" in text


def test_panel_nearest_layer_selection_links_to_selection_signal(app):
    panel = RoutingInspectorPanel()
    panel.set_result(_result())
    selected = []
    panel.layer_selected.connect(selected.append)

    assert panel.select_nearest_layer(1767228300) is True
    assert panel.layer_combo.currentText() == "layer-1"
    assert selected[-1] == "layer-1"
    assert panel.select_nearest_layer(1767226500) is True
    assert panel.layer_combo.currentText() == "layer-0"


def test_panel_nearest_layer_skips_hidden_overlay_layers(app):
    from sailworks.rendering.routingoverlay import RoutingOverlayModel

    result = _result()
    model = RoutingOverlayModel(result)
    panel = RoutingInspectorPanel()
    panel.set_result(result, layer_visibility=model.is_layer_visible)

    model.set_layer_visible("layer-0", False)
    assert panel.select_nearest_layer(1767226500) is True
    assert panel.layer_combo.currentText() == "layer-1"

    model.set_layer_visible("layer-1", False)
    assert panel.select_nearest_layer(1767228300) is False


def test_panel_selection_is_shared_across_surfaces(app):
    from sailworks.core.marine.routingselection import RoutingSelection

    result = _result()
    selection = RoutingSelection(result)
    first = RoutingInspectorPanel()
    second = RoutingInspectorPanel()
    first.set_result(result, selection)
    second.set_result(result, selection)

    first.layer_combo.setCurrentIndex(1)
    assert selection.layer_id == "layer-1"
    # The second surface follows the same selection, not its own copy.
    assert second.layer_combo.currentText() == "layer-1"
    assert second.selection is selection


def test_panel_diagnostics_disclose_sampling_and_services(app):
    panel = RoutingInspectorPanel()
    panel.set_result(_result())

    text = panel.diagnostics.text()
    assert "status: complete" in text
    assert "validation: passed (independent)" in text
    assert "inspection_geometry: sampled" in text
    assert "trace times: absent" in text
    assert "bounded" in text
    assert "shoreline: unsupported" in text


def test_forecast_time_change_keeps_readouts_and_geometry(app):
    from sailworks.rendering.routingoverlay import (
        RoutingOverlayModel,
        routing_overlay_data,
    )

    result = _result()
    model = RoutingOverlayModel(result)
    panel = RoutingInspectorPanel()
    panel.set_result(result)
    readout = panel.readout.toPlainText()
    digest = model.geometry_digest

    assert panel.forecast_time_changed(1767228300) is True
    assert panel.layer_combo.currentText() == "layer-1"
    assert panel.readout.toPlainText() == readout
    assert model.geometry_digest == digest
    assert len(routing_overlay_data(model)["frontiers"]) == 2


def test_plot_follows_the_shared_selection(app):
    from sailworks.core.marine.routingselection import RoutingSelection

    result = _result()
    selection = RoutingSelection(result)
    panel = RoutingInspectorPanel()
    panel.set_result(result, selection)

    selection.select_leg("leg-0")
    assert panel.plot._selection is selection
    # Painting must not raise for a recorded leg.
    panel.plot.repaint()
    assert panel.plot._result is result
