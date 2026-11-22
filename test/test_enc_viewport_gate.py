"""Small, headless regression checks for the native acceptance helpers."""

import importlib.util
import json
import math
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from types import SimpleNamespace

from sailworks.qt.gribviewport import _viewport_widget_class
from sailworks.qt import enc_trace


@pytest.mark.parametrize("partial", (False, True))
def test_scene_publish_trace_reports_mesh_cache_delta_and_snapshot(
    monkeypatch, tmp_path, partial
):
    target = tmp_path / "scene.jsonl"
    monkeypatch.setenv(enc_trace.ENV_PATH, str(target))
    monkeypatch.setattr(enc_trace, "_config", (str(target), enc_trace.DEFAULT_SLOW_MS))
    scene = SimpleNamespace(mesh_hits=1, mesh_misses=0, mesh_built=0)
    metadata = {
        "partial": partial,
        "cells_order": (),
        "cell_states": (),
        "timings": {
            "assembly_ms": 2.0,
            "cells_cache": {
                "cells": [{"index": 0, "cell": "US5CA13M", "lookup": "hit"}],
                "dropped": 0,
                "cache": {
                    "evictions": 2,
                    "refused": 0,
                    "pinned_evictions_avoided": 1,
                },
            },
        },
        "cache_counters": {"mesh_hits": 8, "mesh_misses": 3},
        "granule": (-127, 43, -122, 46),
    }

    class Captured(Exception):
        pass

    original = enc_trace.event

    def capture(name, **fields):
        line = original(name, **fields)
        if name == "scene_publish":
            assert line is not None
            raise Captured
        return line

    monkeypatch.setattr(enc_trace, "event", capture)
    widget = SimpleNamespace(
        _disposed=False,
        _enc_scene_item=SimpleNamespace(),
        _enc_scene_adopt_pending=None,
        _enc_trace_phases={},
        _enc_trace_demand_at=None,
    )
    with pytest.raises(Captured):
        _viewport_widget_class()._enc_scene_published(widget, (1, scene, metadata))
    record = json.loads(target.read_text().splitlines()[-1])
    assert record["event"] == "scene_publish"
    assert record["state"] == ("partial" if partial else "complete")
    assert record["mesh_cache_hits"] == 1
    assert record["mesh_cache_misses"] == 0
    assert record["mesh_cache_built"] == 0
    assert record["cache_counters"]["mesh_hits"] == 8
    assert record["granule"] == [-127, 43, -122, 46]
    assert record["cells_cache"] == metadata["timings"]["cells_cache"]
    # Separate from the duration list: this publication traced no cell
    # durations, so ``phase_cells`` stays empty while the cache rows survive.
    assert record["phase_cells"] == []
    assert "cells_cache" not in record["stage_ms"]


def tool():
    path = Path(__file__).resolve().parents[1] / "tools/check_enc_viewport.py"
    spec = importlib.util.spec_from_file_location("enc_gate_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nested_geometry_identity():
    assert tool()._rounded([[(1.12345678, 2.0)]]) == (((1.123457, 2.0),),)


def test_accepted_snapshot_hashes_are_exact():
    module = tool()
    assert module.ACCEPTED_SNAPSHOT_SHA == (
        module.EXPECTED_SNAPSHOT_SHA,
        module.CURRENT_SNAPSHOT_SHA,
        module.RETRY_SNAPSHOT_SHA,
    )
    assert module.RETRY_SNAPSHOT_SHA == (
        "52fe24319af0c97c03bdfc44aa951a49190d614ea69529bdaf465978feac96e4"
    )
    accepted = module.ACCEPTED_SNAPSHOT_SHA
    assert len(set(accepted)) == len(accepted)
    for sha in accepted:
        assert sha == sha.lower()
        assert len(sha) == 64
        int(sha, 16)
    assert module.RETRY_SNAPSHOT_SHA[:16] not in accepted
    assert module.RETRY_SNAPSHOT_SHA + "\n" not in accepted
    assert "0" * 64 not in accepted


def test_fidelity_identical_and_blank_negative():
    module = tool()
    reference = np.zeros((10, 10, 4), dtype=np.uint8)
    reference[3:7, 3:7] = (10, 20, 30, 255)
    empty = np.zeros_like(reference)
    metrics = module._fidelity(reference, reference, empty)
    assert metrics["mae_255"] == 0
    assert metrics["coverage"] == 1
    assert metrics["alpha_coverage"] == 1
    assert metrics["nonblank"]
    assert not module._fidelity(empty, reference, empty)["nonblank"]


def test_weather_miss_keeps_enc_scene_visible():
    def item():
        state = SimpleNamespace(visible=None)
        state.setVisible = lambda visible: setattr(state, "visible", visible)
        return state

    widget = SimpleNamespace(
        _enc_chart_layer=True,
        _weather=item(),
        _barbs=item(),
        _fill=item(),
        _native_weather_fields=False,
        _cancel_image_fill=lambda: None,
        _wind_gradient_enabled=True,
        _image_fill_matches_requested=lambda: True,
    )
    method = _viewport_widget_class()._set_weather_visible
    method(widget, False)
    assert widget._weather.visible
    assert not widget._barbs.visible
    assert not widget._fill.visible
    method(widget, True)
    assert widget._barbs.visible and widget._fill.visible


def test_default_viewport_still_hides_weather_scene_on_miss():
    scene = SimpleNamespace(visible=True)
    scene.setVisible = lambda visible: setattr(scene, "visible", visible)
    widget = SimpleNamespace(
        _enc_chart_layer=False,
        _weather=scene,
        _fill=None,
        _native_weather_fields=False,
        _cancel_image_fill=lambda: None,
    )
    _viewport_widget_class()._set_weather_visible(widget, False)
    assert not scene.visible


def test_desktop_native_pinch_zooms_the_viewport_at_the_gesture_anchor():
    from datetime import datetime

    from sailworks.qt.gribviewport import ViewportCamera

    widget_class = _viewport_widget_class()
    camera = ViewportCamera(48.45, -124.29, 9.0, 710, 743, datetime(2026, 10, 22))
    zoom_tick = Mock()
    zoom_tick.isActive.return_value = False
    widget = SimpleNamespace(
        _camera=camera,
        _native_gesture_active=False,
        _wheel_zoom_tick=zoom_tick,
        _wheel_zoom_settle=Mock(),
        _wheel_zoom_target=None,
        _wheel_zoom_anchor=None,
        _wheel_zoom_last_tick=None,
    )
    widget._set_zoom_around = lambda zoom, x, y, anchor=None: (
        widget_class._set_zoom_around(widget, zoom, x, y, anchor)
    )
    widget.set_camera = lambda new_camera: setattr(widget, "_camera", new_camera)

    class Gesture:
        def gestureType(self):
            return Qt.NativeGestureType.ZoomNativeGesture

        def value(self):
            return 0.3

        def position(self):
            return QPointF(123, 234)

        def accept(self):
            self.accepted = True

    event = Gesture()
    assert widget_class._native_gesture_event(widget, event)
    assert widget._native_gesture_active
    assert event.accepted
    # The pinch accumulates into the wheel tween instead of committing the
    # camera from the gesture event.
    steps = math.log2(1.3)
    assert widget._wheel_zoom_target == pytest.approx(camera.zoom + steps)
    zoom_tick.start.assert_called_once()
    widget._wheel_zoom_settle.start.assert_called_once_with(250)

    # The gesture anchor is geographic and is captured once for the gesture.
    assert widget._wheel_zoom_anchor[:2] == (123, 234)
    view = camera.view()
    anchor_lat = view._y_to_lat(
        view._lat_to_y(camera.center_lat) + 234 - camera.height / 2
    )
    anchor_lon = view._x_to_lon(
        view._lon_to_x(camera.center_lon) + 123 - camera.width / 2
    )
    assert widget._wheel_zoom_anchor[2] == pytest.approx(anchor_lat)
    assert widget._wheel_zoom_anchor[3] == pytest.approx(anchor_lon)

    # Driving the tween zooms the viewport about that anchor.
    for _ in range(500):
        widget_class._advance_wheel_zoom(widget)
    settled = widget._camera
    assert settled.zoom == pytest.approx(camera.zoom + steps, abs=1e-6)
    settled_view = settled.view()
    assert settled_view._y_to_lat(
        settled_view._lat_to_y(settled.center_lat) + 234 - settled.height / 2
    ) == pytest.approx(anchor_lat, abs=1e-9)
    assert settled_view._x_to_lon(
        settled_view._lon_to_x(settled.center_lon) + 123 - settled.width / 2
    ) == pytest.approx(anchor_lon, abs=1e-9)


def test_desktop_native_gesture_end_clears_viewport_gesture_state():
    widget_class = _viewport_widget_class()
    method = widget_class._native_gesture_event
    zoom_tick = Mock()
    zoom_tick.isActive.return_value = True
    widget = SimpleNamespace(
        _native_gesture_active=True,
        _wheel_zoom_tick=zoom_tick,
        _wheel_zoom_settle=Mock(),
        _wheel_zoom_target=9.4,
        _wheel_zoom_anchor=(1.0, 2.0, 3.0, 4.0),
        _wheel_zoom_last_tick=5.0,
        _end_zoom_gesture=Mock(),
    )
    widget._finish_wheel_zoom = lambda: widget_class._finish_wheel_zoom(widget)

    class Gesture:
        def gestureType(self):
            return Qt.NativeGestureType.EndNativeGesture

        def accept(self):
            self.accepted = True

    event = Gesture()
    assert method(widget, event)
    assert not widget._native_gesture_active
    assert event.accepted
    # The tween is still animating, so state is kept and the settle is deferred.
    assert widget._wheel_zoom_target == 9.4
    widget._wheel_zoom_settle.start.assert_called_once_with(250)
    widget._end_zoom_gesture.assert_not_called()

    # Once the tick has settled, the gesture state is cleared.
    zoom_tick.isActive.return_value = False
    assert method(widget, Gesture())
    assert not widget._native_gesture_active
    assert widget._wheel_zoom_target is None
    assert widget._wheel_zoom_anchor is None
    assert widget._wheel_zoom_last_tick is None
    widget._end_zoom_gesture.assert_called_once()


def test_desktop_native_pinch_rejects_degenerate_scales():
    method = _viewport_widget_class()._native_gesture_event
    for value in (-1.0, -2.5, float("nan"), float("inf")):
        widget = SimpleNamespace(_native_gesture_active=False, zoom_at=Mock())

        class Gesture:
            def gestureType(self):
                return Qt.NativeGestureType.ZoomNativeGesture

            def value(self):
                return value

            def position(self):
                return QPointF(10, 20)

            def accept(self):
                self.accepted = True

        event = Gesture()
        assert method(widget, event), value
        widget.zoom_at.assert_not_called()
        assert not widget._native_gesture_active, value
        assert event.accepted


def test_desktop_cancel_motion_stops_pending_camera_motion():
    method = _viewport_widget_class().cancel_motion
    timers = [Mock(), Mock(), Mock(), Mock()]
    widget = SimpleNamespace(
        _wheel_zoom_tick=timers[0],
        _wheel_zoom_settle=timers[1],
        _wheel_scroll_settle=timers[2],
        _wheel_zoom_target=(1.0, 2.0),
        _wheel_zoom_anchor=(3.0, 4.0),
        _wheel_zoom_last_tick=5.0,
        _wheel_trackpad_panning=True,
        _native_gesture_active=True,
        _drag=(6.0, 7.0),
        _pan_inertia_tick=timers[3],
        _pan_inertia=(8.0, 9.0),
        _pan_inertia_last_tick=10.0,
        _end_zoom_gesture=Mock(),
    )
    method(widget)
    for timer in timers:
        timer.stop.assert_called_once()
    assert widget._wheel_zoom_target is None
    assert widget._wheel_zoom_anchor is None
    assert widget._wheel_zoom_last_tick is None
    assert widget._wheel_trackpad_panning is False
    assert widget._native_gesture_active is False
    assert widget._drag is None
    assert widget._pan_inertia is None
    assert widget._pan_inertia_last_tick is None


def test_desktop_precise_scroll_pans_the_viewport_camera():
    method = _viewport_widget_class()._handle_desktop_wheel
    widget = SimpleNamespace(
        _native_gesture_active=False,
        _wheel_zoom_tick=Mock(),
        _wheel_zoom_settle=Mock(),
        _wheel_scroll_settle=Mock(),
        _wheel_zoom_target=9.0,
        _wheel_zoom_anchor=(10, 10, 1, 2),
        _wheel_trackpad_panning=False,
        pan_pixels=Mock(),
    )

    class Scroll:
        def phase(self):
            return Qt.ScrollPhase.ScrollUpdate

        def pixelDelta(self):
            return QPoint(12, -7)

        def modifiers(self):
            return Qt.KeyboardModifier.NoModifier

        def accept(self):
            self.accepted = True

    event = Scroll()
    method(widget, event)

    widget.pan_pixels.assert_called_once_with(12, -7)
    assert widget._wheel_trackpad_panning
    assert widget._wheel_zoom_target is None
    assert widget._wheel_zoom_anchor is None
    assert event.accepted


def test_desktop_mouse_wheel_zoom_keeps_the_pointer_anchor():
    from datetime import datetime

    from sailworks.qt.gribviewport import ViewportCamera

    method = _viewport_widget_class()._handle_desktop_wheel
    camera = ViewportCamera(48.45, -124.29, 9.0, 710, 743, datetime(2026, 10, 22))
    zoom_tick = Mock()
    zoom_tick.isActive.return_value = False
    widget = SimpleNamespace(
        _camera=camera,
        _native_gesture_active=False,
        _wheel_zoom_tick=zoom_tick,
        _wheel_zoom_settle=Mock(),
        _wheel_scroll_settle=Mock(),
        _wheel_zoom_target=None,
        _wheel_zoom_anchor=None,
        _wheel_trackpad_panning=False,
        _smooth_zoom=False,
        set_camera=Mock(),
        _end_zoom_gesture=Mock(),
    )
    widget._set_zoom_around = lambda zoom, x, y, anchor=None: (
        _viewport_widget_class()._set_zoom_around(widget, zoom, x, y, anchor)
    )
    widget._finish_wheel_zoom = lambda: _viewport_widget_class()._finish_wheel_zoom(
        widget
    )

    class Wheel:
        def phase(self):
            return Qt.ScrollPhase.NoScrollPhase

        def pixelDelta(self):
            return QPoint()

        def angleDelta(self):
            return QPoint(0, 120)

        def modifiers(self):
            return Qt.KeyboardModifier.NoModifier

        def position(self):
            return QPointF(123, 234)

        def accept(self):
            self.accepted = True

    event = Wheel()
    view = camera.view()
    anchor = (
        view._y_to_lat(view._lat_to_y(camera.center_lat) + 234 - camera.height / 2),
        view._x_to_lon(view._lon_to_x(camera.center_lon) + 123 - camera.width / 2),
    )
    method(widget, event)

    updated = widget.set_camera.call_args.args[0]
    updated_view = updated.view()
    assert updated.zoom == camera.zoom + 0.25
    assert (
        abs(
            updated_view._lat_to_y(anchor[0])
            - updated_view._lat_to_y(updated.center_lat)
            - 234
            + camera.height / 2
        )
        < 1e-8
    )
    assert (
        abs(
            updated_view._lon_to_x(anchor[1])
            - updated_view._lon_to_x(updated.center_lon)
            - 123
            + camera.width / 2
        )
        < 1e-8
    )
    assert event.accepted


def test_viewport_mouse_drag_inertia_is_bounded_and_cancelable():
    from unittest.mock import patch

    viewport_type = _viewport_widget_class()

    class Timer:
        def __init__(self):
            self.active = False

        def start(self):
            self.active = True

        def stop(self):
            self.active = False

        def isActive(self):
            return self.active

    def host(inertia_enabled):
        widget = SimpleNamespace(
            _camera=object(),
            _drag=None,
            _drag_velocity=None,
            _pan_inertia=None,
            _pan_inertia_last_tick=None,
            _pan_inertia_enabled=inertia_enabled,
            _pan_inertia_tick=Timer(),
            _wheel_zoom_target=None,
            _wheel_zoom_anchor=None,
            _wheel_zoom_last_tick=None,
            _wheel_zoom_tick=Timer(),
            _wheel_zoom_settle=Timer(),
            _wheel_scroll_settle=Timer(),
            _wheel_trackpad_panning=False,
            _native_gesture_active=False,
            pan_pixels=Mock(),
            measurement_canvas=lambda: None,
            _end_zoom_gesture=Mock(),
        )
        widget.cancel_motion = lambda: viewport_type.cancel_motion(widget)
        widget.is_moving = lambda: viewport_type.is_moving(widget)
        return widget

    def event(x, y):
        return SimpleNamespace(
            button=lambda: Qt.MouseButton.LeftButton,
            position=lambda: QPointF(x, y),
            accept=Mock(),
        )

    clock = [10.0]
    active = host(True)
    with patch(
        "sailworks.qt.gribviewport.time.monotonic", side_effect=lambda: clock[0]
    ):
        viewport_type.mousePressEvent(active, event(0, 0))
        clock[0] += 0.05
        viewport_type.mouseMoveEvent(active, event(80, 30))
        clock[0] += 0.01
        viewport_type.mouseReleaseEvent(active, event(80, 30))
        assert active._pan_inertia is not None
        assert active._pan_inertia_tick.isActive()
        clock[0] += 0.02
        viewport_type._advance_pan_inertia(active)
    assert active.pan_pixels.call_count == 2
    assert active.is_moving()
    viewport_type.cancel_motion(active)
    assert not active.is_moving()
    assert not active._pan_inertia_tick.isActive()

    disabled = host(False)
    clock[0] = 20.0
    with patch(
        "sailworks.qt.gribviewport.time.monotonic", side_effect=lambda: clock[0]
    ):
        viewport_type.mousePressEvent(disabled, event(0, 0))
        clock[0] += 0.05
        viewport_type.mouseMoveEvent(disabled, event(80, 30))
        clock[0] += 0.01
        viewport_type.mouseReleaseEvent(disabled, event(80, 30))
    assert disabled._pan_inertia is None
    assert not disabled._pan_inertia_tick.isActive()
    assert disabled.pan_pixels.call_count == 1


def test_viewport_motion_preferences_propagate_and_cancel_active_motion():
    from sailworks.qt.viewportmap import ViewportMap

    widget = SimpleNamespace(
        _smooth_zoom=True,
        _pan_inertia_enabled=True,
        finish_motion=Mock(),
        cancel_motion=Mock(),
    )
    host = SimpleNamespace(
        disposed=False,
        _weather_presenter=SimpleNamespace(widget=widget),
    )
    host._finish_zoom_silent = lambda: ViewportMap._finish_zoom_silent(host)
    host._cancel_inertia = lambda *args, **kwargs: ViewportMap._cancel_inertia(
        host, *args, **kwargs
    )

    ViewportMap._set_smooth_zoom(host, False)
    assert not host._smooth_zoom
    assert not widget._smooth_zoom
    widget.finish_motion.assert_called_once_with()

    ViewportMap._set_pan_inertia(host, False)
    assert not host._pan_inertia_enabled
    assert not widget._pan_inertia_enabled
    widget.cancel_motion.assert_called_once_with()


def test_desktop_smooth_wheel_zoom_eases_to_target_at_pointer_anchor():
    from datetime import datetime
    from unittest.mock import patch

    from sailworks.qt.gribviewport import ViewportCamera

    viewport_type = _viewport_widget_class()

    class Timer:
        def __init__(self):
            self.active = False

        def start(self, *_args):
            self.active = True

        def stop(self):
            self.active = False

        def isActive(self):
            return self.active

    class Wheel:
        def phase(self):
            return Qt.ScrollPhase.NoScrollPhase

        def pixelDelta(self):
            return QPoint()

        def angleDelta(self):
            return QPoint(0, 120)

        def modifiers(self):
            return Qt.KeyboardModifier.NoModifier

        def position(self):
            return QPointF(123, 234)

        def accept(self):
            self.accepted = True

    initial = ViewportCamera(48.45, -124.29, 9.0, 710, 743, datetime(2026, 10, 22))
    widget = SimpleNamespace(
        _camera=initial,
        _wheel_zoom_target=None,
        _wheel_zoom_anchor=None,
        _wheel_zoom_last_tick=None,
        _wheel_trackpad_panning=False,
        _native_gesture_active=False,
        _smooth_zoom=True,
        _wheel_zoom_tick=Timer(),
        _wheel_zoom_settle=Timer(),
        _wheel_scroll_settle=Timer(),
        set_camera=lambda camera: setattr(widget, "_camera", camera),
    )
    widget._set_zoom_around = lambda zoom, x, y, anchor=None: (
        viewport_type._set_zoom_around(widget, zoom, x, y, anchor)
    )

    clock = [100.0]
    wheel = Wheel()
    original_view = initial.view()
    anchor = (
        original_view._y_to_lat(
            original_view._lat_to_y(initial.center_lat) + 234 - initial.height / 2
        ),
        original_view._x_to_lon(
            original_view._lon_to_x(initial.center_lon) + 123 - initial.width / 2
        ),
    )
    with patch(
        "sailworks.qt.gribviewport.time.perf_counter",
        side_effect=lambda: clock[0],
    ):
        viewport_type._handle_desktop_wheel(widget, wheel)
        assert widget._camera.zoom == initial.zoom
        assert widget._wheel_zoom_tick.isActive()
        for _ in range(80):
            if not widget._wheel_zoom_tick.isActive():
                break
            clock[0] += 0.05
            viewport_type._advance_wheel_zoom(widget)

    assert wheel.accepted
    assert widget._camera.zoom == 9.25
    target_view = widget._camera.view()
    assert (
        abs(
            target_view._lat_to_y(anchor[0])
            - target_view._lat_to_y(widget._camera.center_lat)
            - 234
            + initial.height / 2
        )
        < 1e-8
    )
    assert (
        abs(
            target_view._lon_to_x(anchor[1])
            - target_view._lon_to_x(widget._camera.center_lon)
            - 123
            + initial.width / 2
        )
        < 1e-8
    )


def test_world_chart_scene_stays_visible_when_weather_is_unavailable():
    scene = SimpleNamespace(visible=True)
    scene.setVisible = lambda visible: setattr(scene, "visible", visible)
    barbs = SimpleNamespace(visible=True)
    barbs.setVisible = lambda visible: setattr(barbs, "visible", visible)
    widget = SimpleNamespace(
        _chart_layer=True,
        _native_weather_fields=False,
        _cancel_image_fill=lambda: None,
        _weather=scene,
        _barbs=barbs,
        _fill=None,
    )

    _viewport_widget_class()._set_weather_visible(widget, False)

    assert scene.visible
    assert not barbs.visible


class _RecordingChartModel:
    """A chart model double that records each request's camera and keywords.

    The viewport calls ``request(camera, cause=...)``, and previews add
    ``preview=True``, so the double accepts any call shape and keeps it:
    ``calls`` holds one ``(camera, kwargs)`` pair per request.
    """

    def __init__(self):
        self.calls = []

    def request(self, *args, **kwargs):
        camera = args[0] if args else kwargs.pop("camera", None)
        self.calls.append((camera, kwargs))
        # These tests assert the request shape only; a generation would pull
        # the widget into its post-request bookkeeping, which other tests cover.
        return None

    @property
    def cameras(self):
        return [camera for camera, _ in self.calls]

    @property
    def causes(self):
        return [kwargs.get("cause") for _, kwargs in self.calls]


def test_initial_weather_miss_does_not_block_enc_request():
    requests = _RecordingChartModel()
    widget = SimpleNamespace(
        _chart_model=requests,
        _camera="camera",
        _weather_ready_at=None,
        _enc_chart_layer=True,
        _model=SimpleNamespace(state="unavailable", busy=lambda: False),
        _chart_camera_changed=0,
        _restart_chart_settle=Mock(),
        _flush_held_enc_scene_adoption=lambda: None,
        counters={"chart_requests": 0},
        _chart_state_now=lambda: {},
    )
    method = _viewport_widget_class()._request_chart
    method(widget)
    # An unavailable weather model must not veto the raster: the chart is
    # requested for this camera, as a settle (never a preview).
    assert requests.calls == [("camera", {"cause": "settle"})]
    widget._model.busy = lambda: True
    method(widget)
    assert requests.calls == [("camera", {"cause": "settle"})] * 2


def test_independent_chart_request_does_not_wait_for_weather():
    requests = _RecordingChartModel()
    widget = SimpleNamespace(
        _chart_model=requests,
        _camera="camera",
        _weather_ready_at=None,
        _chart_without_weather=False,
        _enc_chart_layer=False,
        _model=SimpleNamespace(state="loading", busy=lambda: True),
        _chart_camera_changed=0,
        _flush_held_enc_scene_adoption=lambda: None,
        counters={"chart_requests": 0},
        _chart_state_now=lambda: {},
    )

    _viewport_widget_class()._request_chart(widget)

    assert requests.calls == [("camera", {"cause": "settle"})]
    assert widget.counters["chart_requests"] == 1
    assert widget._chart_requested_after_weather is False


def test_settled_chart_gives_busy_weather_a_bounded_head_start(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("sailworks.qt.gribviewport.time.perf_counter", lambda: now[0])
    requests = _RecordingChartModel()
    timer = Mock()
    widget = SimpleNamespace(
        _chart_model=requests,
        _camera="camera",
        _weather_ready_at=1,
        _flush_held_enc_scene_adoption=lambda: None,
        _model=SimpleNamespace(state="ready", busy=lambda: True),
        _chart_camera_changed=100.0,
        _chart_timer=timer,
        counters={"chart_requests": 0},
        _chart_state_now=lambda: {},
    )
    method = _viewport_widget_class()._request_chart

    method(widget)
    assert requests.calls == []
    now[0] = 100.101
    method(widget)
    assert requests.calls == []
    assert timer.start.called
    now[0] = 100.151
    method(widget)
    assert requests.calls == [("camera", {"cause": "settle"})]


def test_weather_head_start_does_not_rearm_across_continuous_camera_moves(monkeypatch):
    """A move every settle interval must not starve the chart for ever."""
    from sailworks.qt.gribviewportchart import CHART_STARTUP_HEAD_START_MS

    now = [100.0]
    monkeypatch.setattr("sailworks.qt.gribviewport.time.perf_counter", lambda: now[0])
    requests = _RecordingChartModel()
    timer = Mock()
    widget = SimpleNamespace(
        _chart_model=requests,
        _camera="camera",
        _weather_ready_at=None,
        _chart_without_weather=False,
        _enc_chart_layer=False,
        _model=SimpleNamespace(state="ready", busy=lambda: False),
        _chart_camera_changed=100.0,
        _chart_head_start_basis=100.0,
        _chart_visible_generation=None,
        _chart_timer=timer,
        _flush_held_enc_scene_adoption=lambda: None,
        counters={"chart_requests": 0},
        _chart_state_now=lambda: {},
    )
    method = _viewport_widget_class()._request_chart

    # Each move restarts the settle window; the head start is measured from
    # the last composition, so it keeps draining while the user keeps moving.
    fired = []
    for step in range(6):
        now[0] = 100.0 + step * 0.65
        widget._chart_camera_changed = now[0]
        now[0] += 0.12
        method(widget)
        fired.append(len(requests.calls))

    # The first settle still honours the (draining) weather window, which
    # expires 150ms after the basis; every later settle proceeds even though
    # the camera has never stopped moving.  A window re-armed from each move
    # would see only 120ms of wait and defer all six.
    assert fired == [0, 1, 2, 3, 4, 5]
    assert widget.counters["chart_requests"] == 5
    assert requests.cameras == ["camera"] * 5
    assert requests.causes == ["settle"] * 5
    # The window was armed once, with the remainder of the original basis,
    # never restarted from the newest camera move.
    assert timer.start.call_count == 1
    assert 0 < timer.start.call_args.args[0] <= CHART_STARTUP_HEAD_START_MS


def test_retained_chart_bounds_the_weather_head_start(monkeypatch):
    """A retained frame needs only the short bound, not the cold start."""
    from sailworks.qt.gribviewportchart import CHART_RETAINED_HEAD_START_MS

    now = [100.0]
    monkeypatch.setattr("sailworks.qt.gribviewport.time.perf_counter", lambda: now[0])
    requests = _RecordingChartModel()
    timer = Mock()
    widget = SimpleNamespace(
        _chart_model=requests,
        _camera="camera",
        _weather_ready_at=None,
        _chart_without_weather=False,
        _enc_chart_layer=False,
        _model=SimpleNamespace(state="loading", busy=lambda: False),
        _chart_camera_changed=100.0,
        _chart_head_start_basis=100.0,
        _chart_visible_generation=1,
        _chart_timer=timer,
        _flush_held_enc_scene_adoption=lambda: None,
        counters={"chart_requests": 0},
        _chart_state_now=lambda: {},
    )
    method = _viewport_widget_class()._request_chart

    now[0] = 100.12
    method(widget)
    assert requests.calls == []
    assert timer.start.called

    now[0] = 100.0 + (CHART_RETAINED_HEAD_START_MS + 50) / 1000.0
    method(widget)
    assert requests.calls == [("camera", {"cause": "settle"})]


def test_scene_request_does_not_wait_for_the_weather_head_start(monkeypatch):
    """The resident ENC scene is asked even while the raster waits."""
    now = [100.0]
    monkeypatch.setattr("sailworks.qt.gribviewport.time.perf_counter", lambda: now[0])
    requests = _RecordingChartModel()
    scene = Mock()
    widget = SimpleNamespace(
        _chart_model=requests,
        _camera="camera",
        _weather_ready_at=None,
        _chart_without_weather=False,
        _enc_chart_layer=False,
        _model=SimpleNamespace(state="loading", busy=lambda: False),
        _chart_camera_changed=100.0,
        _chart_head_start_basis=100.0,
        _chart_visible_generation=None,
        _chart_timer=Mock(),
        _request_enc_scene=scene,
        _flush_held_enc_scene_adoption=lambda: None,
        counters={"chart_requests": 0},
        _chart_state_now=lambda: {},
    )

    now[0] = 100.12
    _viewport_widget_class()._request_chart(widget)

    assert requests.calls == []
    scene.assert_called_once_with()


def test_interactive_preview_does_not_wait_for_weather_with_a_retained_chart(
    monkeypatch,
):
    """A retained frame makes the preview an improvement, never a blank."""
    now = [1.0]
    monkeypatch.setattr("sailworks.qt.gribviewport.time.perf_counter", lambda: now[0])
    chart = SimpleNamespace(request=Mock(return_value=7))
    widget = SimpleNamespace(
        _chart_model=chart,
        _camera="camera",
        _weather_ready_at=None,
        _chart_without_weather=False,
        _enc_chart_layer=False,
        _chart_visible_generation=3,
        _model=SimpleNamespace(state="loading", busy=lambda: False),
        _chart_interactive_at=None,
        counters={"chart_requests": 0},
        _chart_state_now=lambda: {},
    )
    request = _viewport_widget_class()._request_interactive_chart

    request(widget)
    chart.request.assert_called_once_with("camera", preview=True, cause="preview")
    assert widget.counters["chart_requests"] == 1

    # With nothing on screen the preview would be a blank; the weather keeps
    # its veto there.
    blank = SimpleNamespace(
        _chart_model=SimpleNamespace(request=Mock(return_value=8)),
        _camera="camera",
        _weather_ready_at=None,
        _chart_without_weather=False,
        _enc_chart_layer=False,
        _model=SimpleNamespace(state="loading", busy=lambda: False),
        _chart_interactive_at=None,
        counters={"chart_requests": 0},
        _chart_state_now=lambda: {},
    )
    request(blank)
    blank._chart_model.request.assert_not_called()


def test_world_basemap_request_does_not_wait_for_weather():
    requests = _RecordingChartModel()
    widget = SimpleNamespace(
        _chart_model=requests,
        _camera="camera",
        _weather_ready_at=None,
        _chart_without_weather=True,
        _flush_held_enc_scene_adoption=lambda: None,
        _enc_chart_layer=False,
        _model=SimpleNamespace(state="loading", busy=lambda: False),
        _chart_camera_changed=0,
        counters={"chart_requests": 0},
        _chart_state_now=lambda: {},
    )

    _viewport_widget_class()._request_chart(widget)

    assert requests.calls == [("camera", {"cause": "settle"})]


def test_text_size_observer_forwards_the_actual_draw(monkeypatch):
    from sailworks.rendering.vectordrawer.s57chartdrawer import S57ChartDrawer

    drawn = []

    def original(drawer, gpsmap, context, feature):
        return context.show_text("12.3")

    monkeypatch.setattr(S57ChartDrawer, "_render_soundings", original)
    context = SimpleNamespace(
        text_extents=lambda text: (0, 0, 20, 11, 20, 0),
        user_to_device_distance=lambda width, height: (width * 2, height * 2),
        show_text=drawn.append,
    )
    observer = tool()._TextSizes()
    restore = observer.install()
    try:
        observer.stage = "zoom"
        S57ChartDrawer.__new__(S57ChartDrawer)._render_soundings(None, context, None)
        assert drawn == ["12.3"]
        assert observer.sizes["zoom"]["12.3"] == (40, 22)
    finally:
        restore()
    assert S57ChartDrawer._render_soundings is original


def test_close_withdraws_retained_image_and_worker_releases_base_handles(tmp_path):
    from sailworks.qt.gribviewportchart import ViewportChartLayer

    layer = ViewportChartLayer((), tmp_path)
    layer._published = (1, object(), {})
    layer._payload_bytes = 4096
    released = []
    layer._composer_instance = SimpleNamespace(
        _renderer=SimpleNamespace(_release_resources=lambda: released.append(True))
    )
    layer.close()
    assert layer._published is None
    assert layer.observations()["payload_bytes"] == 0
    layer._release()
    assert released == [True]
    assert layer._composer_instance is None


@pytest.mark.parametrize("divisor", (1, 2, 4))
def test_satellite_render_camera_keeps_the_padded_extent(divisor):
    """A rendered frame must cover the geography its placement describes."""
    from dataclasses import replace
    from datetime import datetime

    from sailworks.qt.gribviewport import (
        SATELLITE_MARGIN,
        ViewportCamera,
        satellite_render_camera,
    )

    camera = ViewportCamera(48.45, -124.29, 9.0, 728, 744, datetime(2026, 10, 22))
    padded = replace(
        camera,
        width=camera.width + 2 * SATELLITE_MARGIN,
        height=camera.height + 2 * SATELLITE_MARGIN,
    )
    rendered = satellite_render_camera(padded, divisor)
    assert (rendered.width, rendered.height) == (
        padded.width // divisor,
        padded.height // divisor,
    )

    top_lat, left_lon = padded.view().get_bbox()[0].get_degrees()
    bottom_lat, right_lon = padded.view().get_bbox()[1].get_degrees()
    rendered_top, rendered_left = rendered.view().get_bbox()[0].get_degrees()
    rendered_bottom, rendered_right = rendered.view().get_bbox()[1].get_degrees()
    # Same geography: the frame is placed into the padded rectangle, so any
    # difference shows up as the imagery sliding in x/y as frames land.
    assert (rendered_top, rendered_left) == pytest.approx((top_lat, left_lon), abs=1e-9)
    assert (rendered_bottom, rendered_right) == pytest.approx(
        (bottom_lat, right_lon), abs=1e-9
    )

    # The bug: at full resolution a hardcoded one-level drop covered twice the
    # ground area, so the frame no longer matched its placement.
    if divisor == 1:
        shifted = replace(rendered, zoom=rendered.zoom - 1.0)
        covered = shifted.view().get_scale() * shifted.height
        assert covered == pytest.approx(
            padded.view().get_scale() * padded.height * 2.0, rel=1e-9
        )


def _grab_result(image):
    """A stand-in for QQuickItemGrabResult that never signals by itself."""

    class Result:
        ready = SimpleNamespace(connect=lambda slot: None)

        def image(self):
            return image

    return Result()


def test_grab_releases_the_quick_item_result_after_reading_it(monkeypatch):
    """Each grab frees its result: retention starves the render thread."""
    from PySide6.QtGui import QImage

    module = tool()
    events = []

    class Result:
        ready = SimpleNamespace(
            connect=lambda slot: None,
            disconnect=lambda: events.append("disconnect"),
        )

        def image(self):
            image = QImage(4, 2, QImage.Format.Format_ARGB32)
            image.fill(Qt.GlobalColor.red)
            return image

        def deleteLater(self):
            events.append("deleteLater")

    class Content:
        def grabToImage(self, size=None):
            return Result()

    widget = SimpleNamespace(contentItem=lambda: Content())
    monkeypatch.setattr(module, "_await_grab", lambda result, timeout_ms: None)
    frame = module._grab(widget)
    assert events == ["disconnect", "deleteLater"]
    # The pixels survive the release: QImage is refcounted, not borrowed.
    assert frame.shape == (2, 4, 4)
    assert (frame[:, :, 2] == 255).all()


def test_grab_releases_the_result_even_when_the_image_is_null(monkeypatch):
    from PySide6.QtGui import QImage

    module = tool()
    released = []

    class Result:
        ready = SimpleNamespace(connect=lambda slot: None)

        def image(self):
            return QImage()

        def deleteLater(self):
            released.append(True)

    class Content:
        def grabToImage(self, size=None):
            return Result()

    widget = SimpleNamespace(contentItem=lambda: Content())
    monkeypatch.setattr(module, "_await_grab", lambda result, timeout_ms: None)
    with pytest.raises(RuntimeError, match="timed out"):
        module._grab(widget)
    assert released


def test_grab_sequence_compares_consecutive_grabs(monkeypatch):
    """The verdict matches "every grab equals the last", holding two frames."""
    module = tool()
    first = np.zeros((2, 2, 4), np.uint8)
    same = np.zeros((2, 2, 4), np.uint8)
    changed = np.zeros((2, 2, 4), np.uint8)
    changed[0, 0, 0] = 9

    grabs = iter([first, changed, changed])
    monkeypatch.setattr(module, "_grab", lambda widget: next(grabs))
    native, digests, stable = module._grab_sequence(object(), 3)
    assert native is changed
    assert len(digests) == 3
    assert digests[1] == digests[2] != digests[0]
    assert stable is False

    grabs = iter([first, same])
    monkeypatch.setattr(module, "_grab", lambda widget: next(grabs))
    native, digests, stable = module._grab_sequence(object(), 2)
    assert native is same
    assert digests[0] == digests[1]
    assert stable is True


def test_grab_sequence_reads_one_frame_when_asked_for_one(monkeypatch):
    module = tool()
    calls = []

    def grab(widget):
        calls.append(widget)
        return np.zeros((2, 2, 4), np.uint8)

    monkeypatch.setattr(module, "_grab", grab)
    native, digests, stable = module._grab_sequence(object(), 0)
    assert len(calls) == 1
    assert len(digests) == 1
    assert stable is True
    assert native.shape == (2, 2, 4)


def test_grab_sequence_holds_at_most_two_frames(monkeypatch):
    """A ten-frame stability check must not retain ten 34 MB DPR2 frames."""
    import weakref

    module = tool()
    live = []
    alive_before_grab = []

    def grab(widget):
        alive_before_grab.append(sum(1 for ref in live if ref() is not None))
        frame = np.zeros((2, 2, 4), np.uint8)
        live.append(weakref.ref(frame))
        return frame

    monkeypatch.setattr(module, "_grab", grab)
    module._grab_sequence(object(), 10)
    assert len(alive_before_grab) == 10
    # At each grab only the immediately preceding frame is still reachable;
    # the old implementation kept all ten until the step ended.
    assert max(alive_before_grab) == 1


def test_readback_diagnostics_accepts_precomputed_digests():
    from types import SimpleNamespace

    module = tool()
    frame = np.zeros((4, 4, 4), np.uint8)
    digests = [module._digest(frame), module._digest(frame)]
    readback = module._readback_diagnostics(digests=digests)
    assert readback["grabs"] == 2
    assert readback["digests"] == digests
    assert readback["stable"]
    assert readback["diagnostic_only"]


class _DprScaledContent:
    """A content item that scales grabs the way Qt does.

    ``grabToImage(target)`` treats ``target`` as a logical, device-independent
    size and multiplies it by the window's device pixel ratio, so the image is
    in physical pixels.  A target that was already scaled by the ratio gets
    scaled twice -- the ratio squared.
    """

    def __init__(self, ratio, logical=(8, 4), color=Qt.GlobalColor.red):
        from PySide6.QtCore import QSizeF

        self.ratio = ratio
        self.logical = QSizeF(*logical)
        self.color = color
        self.targets = []

    def size(self):
        return self.logical

    def grabToImage(self, size=None):
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QImage

        self.targets.append(size)
        logical = self.logical if size is None else size
        width = max(1, round(logical.width() * self.ratio))
        height = max(1, round(logical.height() * self.ratio))
        image = QImage(QSize(width, height), QImage.Format.Format_ARGB32)
        image.fill(self.color)
        return _grab_result(image)


def test_grab_reads_native_quick_view_through_content_item(monkeypatch):
    """A QQuickView has no grabFramebuffer; the gate must grab its content."""
    module = tool()
    content = _DprScaledContent(2.0)
    # No devicePixelRatio: the helper must let Qt scale the item, not apply
    # the ratio itself.
    widget = SimpleNamespace(contentItem=lambda: content)
    monkeypatch.setattr(module, "_await_grab", lambda result, timeout_ms: None)
    frame = module._grab(widget)
    assert not hasattr(widget, "grabFramebuffer")
    # The target is omitted, so Qt returns the item's physical-pixel image.
    # A pre-scaled target would have produced 32x16 here and a frame of the
    # wrong shape, which is what the native comparison aborts on.
    assert content.targets == [None]
    assert frame.shape == (8, 16, 4)
    assert frame.dtype == np.uint8
    assert (frame[:, :, 2] == 255).all()


def test_prescaled_grab_target_would_double_the_device_pixel_ratio():
    """Guard the regression: Qt scales a target again, so never pass one."""
    from PySide6.QtCore import QSize

    content = _DprScaledContent(2.0)
    omitted = content.grabToImage().image()
    prescaled = content.grabToImage(QSize(16, 8)).image()
    assert (omitted.width(), omitted.height()) == (16, 8)
    assert (prescaled.width(), prescaled.height()) == (32, 16)
    assert (prescaled.width(), prescaled.height()) != (
        omitted.width(),
        omitted.height(),
    )


def test_grab_fails_loudly_when_the_content_grab_yields_no_image(monkeypatch):
    from PySide6.QtCore import QSizeF
    from PySide6.QtGui import QImage

    module = tool()
    empty = QImage()

    class Content:
        def size(self):
            return QSizeF(8, 4)

        def grabToImage(self, size=None):
            return _grab_result(empty)

    widget = SimpleNamespace(contentItem=lambda: Content())
    monkeypatch.setattr(module, "_await_grab", lambda result, timeout_ms: None)
    with pytest.raises(RuntimeError, match="timed out"):
        module._grab(widget)


def test_grab_still_uses_a_widget_framebuffer_and_converts_to_argb32():
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QImage

    module = tool()
    image = QImage(QSize(3, 2), QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.blue)
    widget = SimpleNamespace(
        grabFramebuffer=lambda: image,
        contentItem=lambda: pytest.fail("content item must not be grabbed"),
    )
    frame = module._grab(widget)
    assert frame.shape == (2, 3, 4)
    assert frame.dtype == np.uint8
    assert (frame[:, :, 0] == 255).all()  # BGRA: blue lands in channel 0


def test_readiness_conjuncts_name_each_unsatisfied_condition():
    module = tool()
    chart = Mock()
    chart.property.return_value = False
    widget = SimpleNamespace(
        counters={"chart_publications": 7, "chart_publications_dropped": 0},
        _chart=chart,
    )
    layer = SimpleNamespace(
        state="loading", enc_metadata={"state": "ready", "selected": None}
    )
    conjuncts = module._readiness_conjuncts(layer, widget, 7)
    assert list(conjuncts) == [
        "layer_state_ready",
        "chart_publications_advanced",
        "chart_property_ready",
        "enc_metadata_state_allowed",
        "enc_metadata_selected",
    ]
    assert conjuncts["enc_metadata_state_allowed"]
    assert not conjuncts["layer_state_ready"]
    assert not conjuncts["chart_publications_advanced"]
    assert not conjuncts["chart_property_ready"]
    assert not conjuncts["enc_metadata_selected"]


def test_readiness_diagnostics_carry_counters_and_selected_metadata():
    module = tool()
    chart = Mock()
    chart.property.return_value = False
    widget = SimpleNamespace(
        counters={"chart_publications": 7, "weather_publications": 2},
        _chart=chart,
    )
    layer = SimpleNamespace(
        state="loading",
        enc_metadata={"state": "ready", "selected": None, "coverage": ((1, 2),)},
    )
    diagnostics = module._readiness_diagnostics(layer, widget, 7)
    assert diagnostics["unsatisfied"] == [
        "layer_state_ready",
        "chart_publications_advanced",
        "chart_property_ready",
        "enc_metadata_selected",
    ]
    assert diagnostics["chart_publications"] == 7
    assert diagnostics["publications"] == {
        "chart_publications": 7,
        "weather_publications": 2,
    }
    assert diagnostics["enc_metadata_state"] == "ready"
    assert diagnostics["enc_metadata_selected"] == []
    assert diagnostics["enc_metadata_coverage"] == [(1, 2)]
    assert diagnostics["enc_metadata_keys"] == ["coverage", "selected", "state"]


def test_records_diff_is_terse_and_points_at_the_first_mismatch():
    module = tool()
    reference = [
        ("_render_area", "DEPARE", "aaaa"),
        ("_render_line", "COALNE", "bbbb"),
    ]
    candidate = [
        ("_render_area", "DEPARE", "aaaa"),
        ("_render_line", "COALNE", "cccc"),
        ("_render_point", "BOYLAT", "dddd"),
    ]
    diff = module._records_diff(candidate, reference)
    assert diff["identical"] is False
    assert diff["candidate_count"] == 3
    assert diff["reference_count"] == 2
    assert diff["first_mismatch"] == 1
    assert diff["window_start"] == 1
    assert diff["candidate_digest"] != diff["reference_digest"]
    assert len(diff["candidate_digest"]) == 16
    assert diff["candidate_window"][0] == {
        "kind": "_render_line",
        "name": "COALNE",
        "digest": "cccc",
    }
    assert diff["reference_window"][0] == {
        "kind": "_render_line",
        "name": "COALNE",
        "digest": "bbbb",
    }
    assert len(diff["candidate_window"]) <= 3
    assert diff["candidate_window"][-1] == {
        "kind": "_render_point",
        "name": "BOYLAT",
        "digest": "dddd",
    }
    assert diff["reference_window"][-1] is None


def test_records_diff_reports_identical_and_empty_sequences():
    module = tool()
    reference = [("_render_area", "DEPARE", "aaaa")]
    identical = module._records_diff(reference, reference)
    assert identical["identical"] is True
    assert identical["first_mismatch"] is None
    assert "candidate_window" not in identical
    empty = module._records_diff([], reference)
    assert empty["first_mismatch"] == 0
    assert empty["candidate_count"] == 0
    assert empty["candidate_window"] == [None]


def test_readback_diagnostics_digest_each_grab():
    module = tool()
    layer = SimpleNamespace(
        state="ready", enc_state="ready", enc_metadata={"state": "ready"}
    )
    widget = SimpleNamespace(counters={"chart_publications": 3})
    frames = [np.zeros((4, 4, 4), dtype=np.uint8) for _ in range(2)]
    frames.append(np.ones((4, 4, 4), dtype=np.uint8))
    readback = module._readback_diagnostics(frames, layer, widget)
    assert readback["grabs"] == 3
    assert readback["stable"] is False
    assert readback["distinct"] == 2
    assert readback["first_distinct_index"] == 2
    assert len(readback["digests"]) == 3
    assert all(len(digest) == 16 for digest in readback["digests"])
    assert readback["digests"][0] == readback["digests"][1]
    assert readback["digests"][1] != readback["digests"][2]
    assert readback["diagnostic_only"] is True
    assert readback["signals"]["layer_state"] == "ready"
    assert readback["signals"]["chart_publications"] == 3
    stable = module._readback_diagnostics(frames[:2])
    assert stable["stable"] is True
    assert stable["distinct"] == 1
    assert stable["first_distinct_index"] is None


def test_unstable_readback_stays_out_of_the_gate_result():
    module = tool()
    assert "stable_readback" not in module.CHECK_NAMES
    assert set(module.ALLOWED_METADATA_STATES) == {
        "ready",
        "coverage_miss",
        "unavailable",
        "disabled",
    }
    assert "diagnostic" in module.UNSTABLE_READBACK_NOTE


class _FakeGrabImage:
    """The ``QImage`` surface ``_grab`` reads, without a Qt platform."""

    def __init__(self, width=3, height=2, null=False):
        self._width = width
        self._height = height
        self._null = null
        self._bits = bytes(range(1, 13)) * (width * height // 3)

    def isNull(self):
        return self._null

    def convertToFormat(self, _format):
        return self

    def constBits(self):
        return self._bits

    def bytesPerLine(self):
        return self._width * 4

    def width(self):
        return self._width

    def height(self):
        return self._height


class _FakeView:
    """A native view stand-in: only the grab entries the tool probes."""

    def __init__(self, image=None):
        self._image = image if image is not None else _FakeGrabImage()
        self.window_calls = 0
        self.framebuffer_calls = 0

    def grabWindow(self):
        self.window_calls += 1
        return self._image

    def grabFramebuffer(self):
        self.framebuffer_calls += 1
        return self._image


class _FakeContentItem:
    def grabToImage(self):
        raise AssertionError("content item grab must not run in this branch")


class _FakeNativeView:
    """A ``QQuickView`` stand-in with no ``grabFramebuffer``."""

    def __init__(self, image=None):
        self._image = image if image is not None else _FakeGrabImage()
        self.window_calls = 0

    def grabWindow(self):
        self.window_calls += 1
        return self._image

    def contentItem(self):
        return _FakeContentItem()


def test_grab_window_path_is_opt_in(monkeypatch):
    module = tool()
    view = _FakeNativeView()
    monkeypatch.delenv(module.GRAB_WINDOW_ENV, raising=False)
    monkeypatch.setattr(module, "_quick_view_image", lambda widget: "content-item")
    assert module._grab_image(view) == "content-item"
    assert view.window_calls == 0

    monkeypatch.setenv(module.GRAB_WINDOW_ENV, "1")
    assert module._grab_image(view) is view._image
    assert view.window_calls == 1

    monkeypatch.setenv(module.GRAB_WINDOW_ENV, "0")
    assert module._grab_image(view) == "content-item"
    assert view.window_calls == 1


def test_grab_framebuffer_still_wins_over_the_env_flag(monkeypatch):
    module = tool()
    widget = _FakeView()
    monkeypatch.setenv(module.GRAB_WINDOW_ENV, "1")
    assert module._grab_image(widget) is widget._image
    assert widget.framebuffer_calls == 1
    assert widget.window_calls == 0


def test_grab_window_null_image_fails_loudly():
    module = tool()
    with pytest.raises(RuntimeError, match="grabWindow returned no image"):
        module._grab_window_image(_FakeNativeView(_FakeGrabImage(null=True)))


def test_grab_window_reports_a_slow_call_on_stderr(capsys):
    module = tool()
    image = module._grab_window_image(_FakeNativeView(), timeout_ms=0)
    assert not image.isNull()
    assert "grabWindow blocked for" in capsys.readouterr().err


def test_image_frame_matches_the_framebuffer_layout():
    module = tool()
    frame = module._image_frame(_FakeGrabImage(width=3, height=2), object())
    assert frame.shape == (2, 3, 4)
    assert frame.dtype == np.uint8
    assert frame[0, 0].tolist() == [1, 2, 3, 4]
    assert frame[1, 2].tolist() == frame[0, 2].tolist()


def test_watchdog_dumps_stacks_and_exits_nonzero():
    """A wedged readback must fail loudly instead of hanging the process."""
    import subprocess
    import sys
    import textwrap

    if not sys.executable:
        pytest.skip("no interpreter to spawn")
    path = Path(__file__).resolve().parents[1] / "tools/check_enc_viewport.py"
    script = textwrap.dedent("""
        import importlib.util, sys, time
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "enc_gate_helpers", Path(sys.argv[1])
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module._arm_watchdog(1)
        time.sleep(60)
        """)
    proc = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert proc.returncode != 0, proc.stderr
    assert "WATCHDOG:" in proc.stderr, proc.stderr


def test_watchdog_fires_while_the_main_thread_holds_the_gil():
    """The deadline must trip from a C call that never returns to Python.

    The gate wedges inside Qt's C++ event loop with the GIL held, and a
    ``threading.Timer`` needs that GIL to reach its callback, so the timer
    watchdog could never fire.  Catastrophic regex backtracking reproduces the
    wedge headlessly: the ``re`` engine stays in C for longer than any test
    budget allows.
    """
    import subprocess
    import sys
    import textwrap

    if not sys.executable:
        pytest.skip("no interpreter to spawn")
    path = Path(__file__).resolve().parents[1] / "tools/check_enc_viewport.py"
    script = textwrap.dedent("""
        import importlib.util, re, sys
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "enc_gate_helpers", Path(sys.argv[1])
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module._arm_watchdog(1)
        re.match(r"(a+)+$", "a" * 40 + "b")
        print("WEDGE SURVIVED", flush=True)
        """)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script, str(path)],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("watchdog did not fire while the main thread held the GIL")
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "WATCHDOG:" in proc.stderr, proc.stderr
    assert "WEDGE SURVIVED" not in proc.stdout, proc.stdout


def test_watchdog_cancel_disarms_the_deadline():
    """The normal return path must leave no handler or timer behind."""
    import signal

    module = tool()
    if not hasattr(signal, "ITIMER_REAL"):
        pytest.skip("no interval timer on this platform")
    before = signal.getsignal(signal.SIGALRM)
    watchdog = module._arm_watchdog(30)
    assert watchdog.mechanism == "signal"
    assert signal.getitimer(signal.ITIMER_REAL)[0] > 0
    watchdog.cancel()
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) == before
    watchdog.cancel()  # idempotent
