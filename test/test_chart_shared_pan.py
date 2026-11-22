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


@pytest.fixture(autouse=True)
def _the_layer_owns_its_pan(monkeypatch):
    """Measure the layer's own pan placement, not the container experiment.

    ``CONTAINER_CARRIES_PAN`` is covered in ``test/test_gribviewport.py``;
    these checks model the two placements it replaces.
    """
    from sailworks.qt import gribviewport as _gv

    monkeypatch.setattr(_gv, "CONTAINER_CARRIES_PAN", False)


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
        # Wave layers stay unbound in this headless double; the camera pass
        # reads their visibility and the retained items on a rebase.
        _wave_visible=False,
        _wave_arrows_visible=False,
        _wave_fill=None,
        _wave_fill_back=None,
        _wave_barbs=None,
        _wave_barbs_back=None,
        _wave_token=None,
        _wave_barbs_token=None,
        _wind_style="streamlines",
        cameraChanged=SimpleNamespace(emit=mock.Mock()),
        _show_state=mock.Mock(),
        _publish_streamlines=mock.Mock(),
        _disposed=False,
        devicePixelRatio=lambda: dpr,
        _map_content=None,
        _map_pan_reference=None,
        _map_pan_window=None,
        # No ENC scene item is attached to this headless double, so the
        # camera-absolute scene placement is a no-op here.
        _enc_scene_item=None,
        _enc_scene_camera=None,
        _camera_dpr=dpr,
        _marine_layer=SimpleNamespace(set_camera=lambda camera: None),
        _icoads_layer=SimpleNamespace(set_camera=lambda camera: None),
        _publication_manager=SimpleNamespace(
            reference_camera=None, set_reference_camera=lambda camera: None
        ),
        _place_route_overlay=lambda camera, anchor_changed=False: None,
        _place_world=lambda camera: None,
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
    widget._place_enc_scene = MethodType(widget_class._place_enc_scene, widget)
    widget._enc_trace_begin_demand = MethodType(
        widget_class._enc_trace_begin_demand, widget
    )
    widget._native_publication_dpr = MethodType(
        widget_class._native_publication_dpr, widget
    )
    widget._retire_barb_anchor_frame = MethodType(
        widget_class._retire_barb_anchor_frame, widget
    )
    widget._barb_zoom_preview_holds = MethodType(
        widget_class._barb_zoom_preview_holds, widget
    )
    widget._maybe_arm_barb_zoom_preview = MethodType(
        widget_class._maybe_arm_barb_zoom_preview, widget
    )
    widget._clear_barb_zoom_preview = MethodType(
        widget_class._clear_barb_zoom_preview, widget
    )
    widget._barb_anchor_frame_places = widget_class._barb_anchor_frame_places
    widget._set_barb_anchor_frame_valid = widget_class._set_barb_anchor_frame_valid
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


class _PlacedItem:
    """A headless placement item whose offset can be read like a QML one."""

    def __init__(self, **props):
        self.props = {name: float(value) for name, value in props.items()}

    def setProperty(self, name, value):
        self.props[name] = float(value)

    def property(self, name):
        return self.props.get(name, 0.0)


class _SceneItem:
    """A headless ENC scene item: the offset it owns is ``x``/``y``."""

    def __init__(self, x=0.0, y=0.0):
        self.x = float(x)
        self.y = float(y)

    def setX(self, value):
        self.x = float(value)

    def setY(self, value):
        self.y = float(value)

    def set_projection(self, value):
        """Accept the re-solved projection a rescale writes."""
        self.projection = value

    def property(self, name):
        return getattr(self, name, 0.0)


def _carrying_widget(anchor, camera, viewport, reference, dpr=1.0):
    """A widget whose shared container carries the pan from ``anchor``."""
    widget, calls = _make_widget(camera, camera, viewport, reference, dpr)
    widget._map_content = SimpleNamespace(setProperty=lambda name, value: None)
    widget._map_pan_reference = reference
    widget._map_anchor_camera = anchor
    widget._chart_composition_camera = camera
    widget._chart_composition_dpr = dpr
    return widget, calls


def test_a_container_pan_publication_keeps_the_chart_screen_position(monkeypatch):
    """The container already applies the pan, so a publication adds none of it."""
    from sailworks.qt import gribviewport as _gv

    monkeypatch.setattr(_gv, "CONTAINER_CARRIES_PAN", True)
    anchor, camera, _ = _cameras()
    viewport = MapViewport(ChartCamera(camera, 1.0), MARGIN, lambda: None)
    reference = PanReference.from_camera(anchor)
    widget, _calls = _carrying_widget(anchor, camera, viewport, reference)

    # The chart last placed in the container's own reference frame.
    before = chart_placement(viewport, anchor)
    item = _PlacedItem(imageX=before[0], imageY=before[1])
    widget._chart = item
    widget._place_chart(cause="publication")

    dx, dy = reference.translation_to(camera)
    assert math.isclose(item.props["imageX"], before[0], rel_tol=1e-10)
    assert math.isclose(item.props["imageY"], before[1], rel_tol=1e-10)
    screen_before = (before[0] + dx, before[1] + dy)
    screen_after = (item.props["imageX"] + dx, item.props["imageY"] + dy)
    assert screen_after == pytest.approx(screen_before, rel=1e-10)
    # The screen position is still the geographically correct one.
    assert screen_after == pytest.approx(
        chart_placement(viewport, camera)[:2], rel=1e-10
    )


def test_a_zoom_publication_places_the_chart_in_full(monkeypatch):
    """A size change cannot be carried, so it is written on the item."""
    from sailworks.qt import gribviewport as _gv

    monkeypatch.setattr(_gv, "CONTAINER_CARRIES_PAN", True)
    anchor, _camera, viewport = _cameras()
    zoomed = ViewportCamera(
        anchor.center_lat,
        anchor.center_lon,
        anchor.zoom + 1,
        anchor.width,
        anchor.height,
        anchor.time,
    )
    reference = PanReference.from_camera(anchor)
    widget, _calls = _carrying_widget(anchor, zoomed, viewport, reference)
    item = _PlacedItem(imageX=-999.0, imageY=-999.0)
    widget._chart = item
    widget._place_chart(cause="publication")

    expected = chart_placement(viewport, anchor)
    assert math.isclose(item.props["imageX"], expected[0], rel_tol=1e-10)
    assert math.isclose(item.props["imageY"], expected[1], rel_tol=1e-10)
    assert math.isclose(item.props["imageWidth"], expected[2], rel_tol=1e-10)
    assert math.isclose(item.props["imageHeight"], expected[3], rel_tol=1e-10)


def test_a_container_pan_publication_keeps_the_enc_scene_screen_position():
    """The adopted scene cancels exactly the pan from the container reference."""
    from sailworks.qt.viewporttransform import enc_scene_placement

    anchor, camera, viewport = _cameras()
    reference = PanReference.from_camera(anchor)
    dx, dy = reference.translation_to(camera)

    # Before the publication: a scene composed at the anchor camera, offset
    # cancelled to zero, drawn under the container's translation.
    before = chart_placement(viewport, anchor)
    screen_before = (before[0] + 0.0 + dx, before[1] + 0.0 + dy)

    # The adopted scene is composed at the current camera, so it cancels the
    # whole reference-to-frame translation and the container draws the pan.
    offset = enc_scene_placement(reference, camera)
    assert offset == pytest.approx((-dx, -dy), rel=1e-10)
    item = _SceneItem()
    _gribviewport_place(item, offset, camera)
    after = chart_placement(viewport, camera)
    screen_after = (after[0] + item.x + dx, after[1] + item.y + dy)
    assert screen_after == pytest.approx(screen_before, rel=1e-10)
    assert screen_after == pytest.approx(after[:2], rel=1e-10)


def _gribviewport_place(item, offset, camera):
    from sailworks.qt import gribviewport as _gv

    _gv.place_enc_scene_item(
        item,
        offset[0],
        offset[1],
        cause="publication",
        camera=camera,
    )


def test_the_publication_trace_reports_zero_deltas_for_a_stationary_publication(
    monkeypatch,
):
    from sailworks.qt import gribviewport as _gv

    monkeypatch.setattr(_gv, "CONTAINER_CARRIES_PAN", True)
    events = []
    monkeypatch.setattr(_gv.enc_trace, "enabled", lambda: True)
    monkeypatch.setattr(
        _gv.enc_trace,
        "camera_fields",
        lambda camera: {"center_lon": camera.center_lon},
    )
    monkeypatch.setattr(
        _gv.enc_trace,
        "event",
        lambda kind, **fields: events.append((kind, fields)),
    )
    anchor, _camera, viewport = _cameras()
    reference = PanReference.from_camera(anchor)
    widget, _calls = _carrying_widget(anchor, anchor, viewport, reference)
    placement = chart_placement(viewport, anchor)
    widget._chart = _PlacedItem(imageX=placement[0], imageY=placement[1])
    widget._place_chart(cause="publication")

    assert events, "a publication must record its anchor"
    kind, fields = events[-1]
    assert kind == "chart_publish_anchor"
    assert fields["layer"] == "chart"
    assert (fields["deltaX"], fields["deltaY"]) == (0.0, 0.0)
    assert fields["screenX"] == pytest.approx(placement[0])
    assert fields["previousScreenX"] == pytest.approx(placement[0])


def test_a_rebase_keeps_the_chart_and_enc_screen_positions(monkeypatch):
    """Resetting the bounded parent must not move either container-frame layer.

    The chart and the ENC scene are positioned in ``mapContent``'s frame and
    are not re-placed by a translation-only pan, so the rebase has to absorb
    the withdrawn translation for them exactly as it does for the retained
    images.
    """
    from sailworks.qt import gribviewport as _gv
    from sailworks.qt.viewporttransform import (
        PaddedPanWindow,
        enc_scene_placement,
    )

    monkeypatch.setattr(_gv, "CONTAINER_CARRIES_PAN", True)
    anchor, _camera, viewport = _cameras()
    far = ViewportCamera(
        anchor.center_lat - 3.0,
        anchor.center_lon - 6.0,
        anchor.zoom,
        anchor.width,
        anchor.height,
        anchor.time,
    )
    reference = PanReference.from_camera(anchor)
    widget, _calls = _carrying_widget(anchor, far, viewport, reference)
    parent = {}
    widget._map_content = SimpleNamespace(
        setProperty=lambda name, value: parent.__setitem__(name, value)
    )
    widget._map_pan_window = PaddedPanWindow(reference, 0.5 * anchor.width)
    # The camera pass re-places the scene from its own projection; this check
    # is about the offset the rebase leaves, so that placement is stubbed.
    widget._place_enc_scene = mock.Mock()

    pan_x, pan_y = reference.translation_to(far)
    placement = chart_placement(viewport, anchor)
    widget._chart = _PlacedItem(imageX=placement[0], imageY=placement[1])
    offset = enc_scene_placement(reference, anchor)
    widget._enc_scene_item = _SceneItem(x=offset[0], y=offset[1])
    widget._enc_scene_camera = anchor
    widget._enc_scene_frame_camera = anchor

    # Before the rebase both offsets are drawn under the pan the container
    # carries, so these are the screen positions a user sees.
    chart_screen = (placement[0] + pan_x, placement[1] + pan_y)
    enc_screen = (offset[0] + pan_x, offset[1] + pan_y)

    widget._update_shared_map_pan(far)

    assert parent["x"] == pytest.approx(0.0)
    assert parent["y"] == pytest.approx(0.0)
    assert (
        widget._chart.props["imageX"] + parent["x"],
        widget._chart.props["imageY"] + parent["y"],
    ) == pytest.approx(chart_screen, rel=1e-10)
    assert (
        widget._enc_scene_item.x + parent["x"],
        widget._enc_scene_item.y + parent["y"],
    ) == pytest.approx(enc_screen, rel=1e-10)


def test_a_rebase_that_re_places_a_layer_does_not_apply_the_offset_twice():
    """A re-placement from the new anchor supersedes the rebase shift."""
    from sailworks.qt.viewporttransform import (
        PaddedPanWindow,
        enc_scene_placement,
    )

    anchor, _camera, viewport = _cameras()
    far = ViewportCamera(
        anchor.center_lat - 3.0,
        anchor.center_lon - 6.0,
        anchor.zoom,
        anchor.width,
        anchor.height,
        anchor.time,
    )
    reference = PanReference.from_camera(anchor)
    widget, _calls = _make_widget(anchor, far, viewport, reference)
    widget._map_content = SimpleNamespace(setProperty=lambda name, value: None)
    widget._map_pan_reference = reference
    widget._map_pan_window = PaddedPanWindow(reference, 0.5 * anchor.width)
    widget._map_anchor_camera = far
    placement = chart_placement(viewport, anchor)
    widget._chart = _PlacedItem(imageX=placement[0], imageY=placement[1])
    widget._enc_scene_item = _SceneItem()
    widget._enc_scene_camera = anchor
    widget._enc_scene_frame_camera = anchor

    dx, dy = reference.translation_to(far)
    widget._rebase_retained_imagery(dx, dy)
    # ``_update_shared_map_pan`` rebases the reference before the layer is
    # re-placed for the new anchor.
    widget._map_pan_reference = PanReference.from_camera(far)
    widget._place_chart()
    widget._place_enc_scene()

    # One translation from the anchor: the shift plus a re-placement would be
    # a pan further and read as a snap.
    expected = chart_placement(viewport, far)
    assert widget._chart.props["imageX"] == pytest.approx(expected[0], rel=1e-10)
    assert widget._chart.props["imageY"] == pytest.approx(expected[1], rel=1e-10)

    frame = widget._enc_scene_frame_camera
    enc_expected = enc_scene_placement(PanReference.from_camera(far), frame)
    assert widget._enc_scene_item.x == pytest.approx(enc_expected[0], rel=1e-10)
    assert widget._enc_scene_item.y == pytest.approx(enc_expected[1], rel=1e-10)


def test_the_rebase_trace_reports_zero_screen_delta(monkeypatch):
    """A clean rebase records both offsets' pre/post screen positions, still."""
    from sailworks.qt import gribviewport as _gv

    monkeypatch.setattr(_gv, "CONTAINER_CARRIES_PAN", True)
    events = []
    monkeypatch.setattr(_gv.enc_trace, "enabled", lambda: True)
    monkeypatch.setattr(
        _gv.enc_trace,
        "camera_fields",
        lambda camera: {"center_lon": camera.center_lon},
    )
    monkeypatch.setattr(
        _gv.enc_trace,
        "event",
        lambda kind, **fields: events.append((kind, fields)),
    )

    anchor, _camera, viewport = _cameras()
    reference = PanReference.from_camera(anchor)
    widget, _calls = _carrying_widget(anchor, anchor, viewport, reference)
    placement = chart_placement(viewport, anchor)
    widget._chart = _PlacedItem(imageX=placement[0], imageY=placement[1])
    widget._enc_scene_item = _SceneItem(x=10.0, y=-20.0)
    widget._camera = anchor

    dx, dy = 25.0, -40.0
    widget._rebase_retained_imagery(dx, dy)

    rebases = [fields for kind, fields in events if kind == "chart_rebase"]
    assert len(rebases) == 1, "the rebase is traced once per event"
    fields = rebases[0]
    assert (fields["dx"], fields["dy"]) == (dx, dy)
    assert fields["chartPreviousScreenX"] == pytest.approx(placement[0] + dx)
    assert fields["chartPreviousScreenY"] == pytest.approx(placement[1] + dy)
    assert fields["chartScreenX"] == pytest.approx(placement[0] + dx)
    assert fields["chartScreenY"] == pytest.approx(placement[1] + dy)
    assert (fields["chartDeltaX"], fields["chartDeltaY"]) == pytest.approx((0.0, 0.0))
    assert fields["encPreviousScreenX"] == pytest.approx(10.0 + dx)
    assert fields["encPreviousScreenY"] == pytest.approx(-20.0 + dy)
    assert fields["encScreenX"] == pytest.approx(10.0 + dx)
    assert fields["encScreenY"] == pytest.approx(-20.0 + dy)
    assert (fields["encDeltaX"], fields["encDeltaY"]) == pytest.approx((0.0, 0.0))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
