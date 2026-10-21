"""Small, headless regression checks for the native acceptance helpers."""

import importlib.util
from pathlib import Path

import numpy as np
from types import SimpleNamespace

from gweatherrouting.qt.gribviewport import _viewport_widget_class


def tool():
    path = Path(__file__).resolve().parents[1] / "tools/check_enc_viewport.py"
    spec = importlib.util.spec_from_file_location("enc_gate_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nested_geometry_identity():
    assert tool()._rounded([[(1.12345678, 2.0)]]) == (((1.123457, 2.0),),)


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
        _enc_chart_layer=True, _weather=item(), _barbs=item(), _fill=item()
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
    widget = SimpleNamespace(_enc_chart_layer=False, _weather=scene)
    _viewport_widget_class()._set_weather_visible(widget, False)
    assert not scene.visible


def test_initial_weather_miss_does_not_block_enc_request():
    requests = []
    widget = SimpleNamespace(
        _chart_model=SimpleNamespace(request=requests.append),
        _camera="camera",
        _weather_ready_at=None,
        _enc_chart_layer=True,
        _model=SimpleNamespace(state="unavailable", busy=lambda: False),
        _chart_camera_changed=0,
        counters={"chart_requests": 0},
        _chart_state_now=lambda: {},
    )
    method = _viewport_widget_class()._request_chart
    method(widget)
    assert requests == ["camera"]
    widget._model.busy = lambda: True
    method(widget)
    assert requests == ["camera"]


def test_text_size_observer_forwards_the_actual_draw(monkeypatch):
    from gweatherrouting.rendering.vectordrawer.s57chartdrawer import S57ChartDrawer

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
    from gweatherrouting.qt.gribviewportchart import ViewportChartLayer

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
