"""Tests for chart shared-pan reference placement (#118)."""

import math
from datetime import datetime, timezone
from types import MethodType, SimpleNamespace
from unittest import mock

import pytest

from sailworks.core.mapviewport import MapViewport
from sailworks.qt.gribviewport import ViewportCamera, _viewport_widget_class
from sailworks.qt.gribviewportchart import ChartCamera, MARGIN, chart_placement
from sailworks.qt.viewporttransform import PanReference, CameraChange


def _cameras():
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    composed = ViewportCamera(
        48.30887922260005, -123.09473088970692, 9.837590787619122, 900, 700, stamp
    )
    current = ViewportCamera(
        48.30887922260005, -123.09573088970692, 9.837590787619122, 900, 700, stamp
    )
    viewport = MapViewport(ChartCamera(composed, 1.0), MARGIN, lambda: None)
    return composed, current, viewport


def _prop_recorder():
    calls = []
    chart = SimpleNamespace(
        setProperty=lambda name, value: calls.append((name, float(value)))
    )
    return chart, calls


def _make_widget(composed, current, viewport, reference, dpr=1.0):
    chart, calls = _prop_recorder()
    widget_class = _viewport_widget_class()
    widget = SimpleNamespace(
        _chart=chart,
        _chart_viewport=viewport,
        _chart_camera=composed,
        _chart_pan_reference=reference,
        _chart_composition_camera=composed,
        _chart_composition_dpr=dpr,
        _camera=current,
        _model=mock.Mock(),
        _chart_model=None,
        _published=1,
        _weather_display_camera=None,
        _native_weather_fields=False,
        _wind_style="streamlines",
        cameraChanged=SimpleNamespace(emit=mock.Mock()),
        _show_state=mock.Mock(),
        _publish_streamlines=mock.Mock(),
        _disposed=False,
        devicePixelRatio=lambda: dpr,
        _map_content=None,
        _map_pan_reference=None,
        _map_pan_window=None,
        _marine_layer=SimpleNamespace(set_camera=lambda camera: None),
        _icoads_layer=SimpleNamespace(set_camera=lambda camera: None),
        _publication_manager=SimpleNamespace(
            reference_camera=None, set_reference_camera=lambda camera: None
        ),
        _place_route_overlay=lambda camera, anchor_changed=False: None,
        _state="ready",
        _presentation_spans=[],
        _set_weather_visible=lambda visible: None,
    )
    widget._place_chart = MethodType(widget_class._place_chart, widget)
    widget._update_shared_map_pan = MethodType(
        widget_class._update_shared_map_pan, widget
    )
    widget._rebase_retained_imagery = MethodType(
        widget_class._rebase_retained_imagery, widget
    )
    return widget, calls


def test_pan_reference_invalidation_gives_zero_translation():
    ref = PanReference(center_lat=0, center_lon=0, zoom=5, width=640, height=480, dpr=1)
    assert ref.classify(ref) == CameraChange.PAN
    assert ref.translation_to(ref) == (0.0, 0.0)


def test_pan_reference_continuous_longitude():
    ref = PanReference(center_lat=0, center_lon=179.9, zoom=4, width=640, height=480)
    east = PanReference(center_lat=0, center_lon=-179.9, zoom=4, width=640, height=480)
    dx, dy = ref.translation_to(east)
    assert abs(dx) < 10.0


def test_place_chart_pan_uses_reference_camera_and_one_translation():
    composed, current, viewport = _cameras()
    reference = PanReference.from_camera(composed)
    widget, calls = _make_widget(composed, current, viewport, reference)
    widget._place_chart()
    base_x, base_y, width, height = chart_placement(viewport, composed)
    dx, dy = reference.translation_to(current)
    actual = dict(calls)
    for name, expected in {
        "imageX": base_x + dx,
        "imageY": base_y + dy,
        "imageWidth": width,
        "imageHeight": height,
    }.items():
        assert math.isclose(actual[name], expected, rel_tol=1e-10)
    assert [name for name, _ in calls] == [
        "imageX",
        "imageY",
        "imageWidth",
        "imageHeight",
    ]


def test_place_chart_zero_reference_matches_current():
    composed, current, viewport = _cameras()
    viewport = MapViewport(ChartCamera(current, 1.0), MARGIN, lambda: None)
    widget, calls = _make_widget(
        current, current, viewport, PanReference.from_camera(current)
    )
    widget._place_chart()
    base = chart_placement(viewport, current)
    for (name, value), expected in zip(calls, (base[0], base[1], base[2], base[3])):
        assert math.isclose(value, expected, rel_tol=1e-10)


def test_set_camera_pan_keeps_reference_and_original_camera():
    composed, current, viewport = _cameras()
    reference = PanReference.from_camera(composed)
    widget, calls = _make_widget(composed, current, viewport, reference)
    _viewport_widget_class().set_camera(widget, current)
    assert widget._chart_pan_reference is reference
    assert widget._chart_composition_camera is composed
    base = chart_placement(viewport, composed)
    dx, dy = reference.translation_to(current)
    actual = dict(calls)
    for name, expected in {
        "imageX": base[0] + dx,
        "imageY": base[1] + dy,
        "imageWidth": base[2],
        "imageHeight": base[3],
    }.items():
        assert math.isclose(actual[name], expected, rel_tol=1e-10)


def test_set_camera_invalidation_rebases_reference_on_zoom_and_dpr():
    composed, current, viewport = _cameras()
    reference = PanReference.from_camera(composed, 1.0)
    widget, calls = _make_widget(composed, current, viewport, reference)
    parent_calls = []
    widget._map_content = SimpleNamespace(
        setProperty=lambda name, value: parent_calls.append((name, value))
    )
    widget._map_pan_reference = reference
    ratio = [1.0]
    widget.devicePixelRatio = lambda: ratio[0]
    widget._camera = composed
    zoomed = ViewportCamera(
        composed.center_lat,
        composed.center_lon,
        composed.zoom + 1,
        composed.width,
        composed.height,
        composed.time,
    )
    _viewport_widget_class().set_camera(widget, zoomed)
    assert widget._map_pan_reference == PanReference.from_camera(zoomed, 1.0)
    assert widget._map_anchor_camera is zoomed
    assert widget._chart_composition_camera is composed
    base = chart_placement(viewport, zoomed)
    actual = dict(calls)
    assert math.isclose(actual["imageWidth"], base[2], rel_tol=1e-10)
    assert math.isclose(actual["imageHeight"], base[3], rel_tol=1e-10)
    ratio[0] = 2.0
    _viewport_widget_class().set_camera(widget, zoomed)
    assert widget._map_pan_reference == PanReference.from_camera(zoomed, 2.0)
    assert parent_calls[-2:] == [("x", 0.0), ("y", 0.0)]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
