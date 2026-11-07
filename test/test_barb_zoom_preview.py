"""Tests for the temporary zoom-only front barb preview.

The preview maps the retained previous frame's anchor atlas into the new map
reference.  Only anchor positions move; the +/-32 px symbol quad is added after
the transform, so every preview symbol keeps its screen size.  The preview is
strictly a loading state: it never becomes the current selection, and a real
new selection commits exactly as before.
"""

from datetime import datetime, timezone
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest import mock

import numpy as np
import pytest

from sailworks.qt.gribviewport import ViewportCamera, _viewport_widget_class
from sailworks.qt.viewporttransform import (
    PanReference,
    continuous_longitude,
    zoom_preview_transform,
)
from sailworks.rendering.windpainter import mercator_project_flat

STAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)
OTHER_STAMP = datetime(2026, 1, 1, 6, tzinfo=timezone.utc)


def _project(lat, lon, reference, longitude=None):
    longitude = lon if longitude is None else longitude
    x, y = mercator_project_flat(
        np.asarray([lat], dtype=float),
        np.asarray([longitude], dtype=float),
        reference.center_lat,
        reference.center_lon,
        reference.zoom,
        reference.width,
        reference.height,
    )
    return float(x[0]), float(y[0])


def _camera(lat, lon, zoom, width=900, height=700, stamp=STAMP):
    return ViewportCamera(lat, lon, zoom, width, height, stamp)


class _Recorder:
    def __init__(self):
        self.props = {}
        self.visible = None
        self.opacity = None

    def setProperty(self, name, value):
        self.props[name] = value

    def setVisible(self, visible):
        self.visible = visible

    def setOpacity(self, opacity):
        self.opacity = opacity


class _Identity:
    def __init__(self, covers=True, source=None):
        self._covers = covers
        self.source = source

    def contains(self, stamp):
        return self._covers


def _preview_widget(active_camera, camera, anchor_reference, target, identity=None):
    """A minimal viewport exposing the preview placement methods."""
    widget_class = _viewport_widget_class()
    front, back = _Recorder(), _Recorder()
    widget = SimpleNamespace(
        _native_weather_fields=True,
        _native_active_camera=active_camera,
        _native_active_identity=identity or _Identity(),
        _native_active_selection=SimpleNamespace(anchor_reference=anchor_reference),
        _barb_zoom_preview=None,
        _barbs=front,
        _barbs_back=back,
        _map_pan_reference=target,
        _camera=camera,
        _native_publication_dpr=lambda: 1.0,
        devicePixelRatio=lambda: 1.0,
    )
    for name in (
        "_maybe_arm_barb_zoom_preview",
        "_barb_zoom_preview_holds",
        "_apply_barb_zoom_preview",
        "_clear_barb_zoom_preview",
        "_retire_barb_anchor_frame",
    ):
        setattr(widget, name, MethodType(getattr(widget_class, name), widget))
    widget._barb_zoom_dpr_matches = widget_class._barb_zoom_dpr_matches
    widget._barb_anchor_frame_places = widget_class._barb_anchor_frame_places
    widget._reset_barb_zoom_preview_item = widget_class._reset_barb_zoom_preview_item
    widget._set_barb_anchor_frame_valid = widget_class._set_barb_anchor_frame_valid
    return widget


def test_zoom_preview_transform_matches_target_projection():
    """z +/- fractional: scaled atlas pixels equal the target projection."""
    anchor = PanReference(48.3, -123.1, 9.5, 900, 700)
    for delta in (0.35, -0.35, 0.7):
        target = PanReference(48.3, -123.1, 9.5 + delta, 900, 700)
        scale, offset_x, offset_y = zoom_preview_transform(anchor, target)
        assert scale == pytest.approx(2.0**delta)
        for lat, lon in ((48.4, -123.2), (48.1, -122.9)):
            ax, ay = _project(lat, lon, anchor)
            bx, by = _project(lat, lon, target)
            assert scale * ax + offset_x == pytest.approx(bx, abs=1e-6)
            assert scale * ay + offset_y == pytest.approx(by, abs=1e-6)


def test_zoom_preview_transform_antimeridian_is_continuous():
    """A preview across the seam stays one screen away, not one world away."""
    anchor = PanReference(0.0, 179.6, 8.25, 800, 600)
    target = PanReference(0.0, -179.7, 8.75, 800, 600)
    scale, offset_x, offset_y = zoom_preview_transform(anchor, target)
    lat, lon = 0.0, 179.8
    ax, ay = _project(lat, lon, anchor, continuous_longitude(lon, anchor.center_lon))
    bx, by = _project(lat, lon, target, continuous_longitude(lon, target.center_lon))
    assert scale * ax + offset_x == pytest.approx(bx, abs=1e-6)
    assert scale * ay + offset_y == pytest.approx(by, abs=1e-6)
    assert abs(bx - target.width / 2.0) < target.width
    assert abs(scale * ax + offset_x - target.width / 2.0) < target.width


def test_zoom_preview_keeps_the_32px_symbol_quad():
    """The transform scales the anchor only; localPoint is added unscaled."""
    anchor = PanReference(48.3, -123.1, 9.0, 900, 700)
    target = PanReference(48.3, -123.1, 10.0, 900, 700)
    scale, offset_x, offset_y = zoom_preview_transform(anchor, target)
    anchor_x, anchor_y = 420.0, 360.0
    corners = []
    for dx, dy in ((-32.0, -32.0), (32.0, 32.0)):
        x = scale * anchor_x + offset_x + dx
        y = scale * anchor_y + offset_y + dy
        corners.append((x, y))
    assert corners[1][0] - corners[0][0] == pytest.approx(64.0)
    assert corners[1][1] - corners[0][1] == pytest.approx(64.0)

    source = (Path(__file__).parents[1] / "sailworks/qt/shaders/barbs.vert").read_text()
    assert "previewScale * anchor" in source
    assert "previewScale * localPoint" not in source
    assert "position + offset + localPoint" in source

    qml = (
        Path(__file__).parents[1] / "sailworks/qt/shaders/WeatherBarbs.qml"
    ).read_text()
    assert "property real previewScale: 1" in qml
    assert "property real previewOffsetX: 0" in qml
    assert "property real previewOffsetY: 0" in qml


def test_zoom_preview_follows_a_pan_while_pending():
    """A pan at the target zoom re-places the old anchors in the new ref."""
    active = _camera(48.3, -123.1, 9.5)
    target = _camera(48.3, -123.1, 9.8)
    anchor_reference = (48.3, -123.1, 9.5, 900, 700)
    target_reference = PanReference.from_camera(target, 1.0)
    widget = _preview_widget(active, target, anchor_reference, target_reference)

    assert widget._maybe_arm_barb_zoom_preview(active, target, 1.0, 1.0) is True
    anchor = PanReference(*anchor_reference, dpr=1.0)
    first = zoom_preview_transform(anchor, target_reference)
    assert widget._barbs.props["previewScale"] == pytest.approx(first[0])
    assert widget._barbs.props["previewOffsetX"] == pytest.approx(first[1])
    assert widget._barbs.props["previewOffsetY"] == pytest.approx(first[2])
    assert widget._barbs.props["anchorFrameValid"] is True
    assert widget._barbs.props["anchorOffsetX"] == 0.0
    assert widget._barbs.props["anchorOffsetY"] == 0.0

    panned = _camera(48.3, -123.05, 9.8)
    panned_reference = PanReference.from_camera(panned, 1.0)
    widget._map_pan_reference = panned_reference
    widget._camera = panned
    assert widget._maybe_arm_barb_zoom_preview(target, panned, 1.0, 1.0) is True
    # On a pending pan at the same zoom, the preview is re-anchored to the
    # current camera, so the scale resets to 1.0 (pure translation) and the
    # offset lands in the new map reference.
    assert widget._barbs.props["previewScale"] == pytest.approx(1.0)
    assert widget._barbs.props["anchorFrameValid"] is True
    # The pending back layer is never given preview uniforms.
    assert "previewScale" not in widget._barbs_back.props


def test_zoom_preview_persists_through_pending_staging():
    """Arming a preview keeps it visible; a pending pan re-places, not clears.

    The preview survives until the commit swaps the staged back into the
    front, at which point _commit_native_weather clears it.
    """
    active = _camera(48.3, -123.1, 9.5)
    target = _camera(48.3, -123.1, 9.8)
    anchor_reference = (48.3, -123.1, 9.5, 900, 700)
    target_reference = PanReference.from_camera(target, 1.0)
    widget = _preview_widget(active, target, anchor_reference, target_reference)

    assert widget._maybe_arm_barb_zoom_preview(active, target, 1.0, 1.0) is True
    assert widget._barb_zoom_preview is not None
    first_scale = widget._barbs.props["previewScale"]

    # A pan while the replacement is pending re-places the old anchors in the
    # new map reference instead of hiding the preview.
    panned = _camera(48.3, -123.05, 9.8)
    widget._map_pan_reference = PanReference.from_camera(panned, 1.0)
    widget._camera = panned
    assert widget._maybe_arm_barb_zoom_preview(target, panned, 1.0, 1.0) is True
    assert widget._barb_zoom_preview is not None
    assert widget._barbs.props["previewScale"] != first_scale
    assert widget._barbs.props["anchorFrameValid"] is True

    # Only the commit that replaces the front clears the preview.
    widget._clear_barb_zoom_preview()
    assert widget._barb_zoom_preview is None
    assert widget._barbs.props["previewScale"] == 1.0
    assert widget._barbs.props["anchorOffsetX"] == 0.0
    assert widget._barbs.props["anchorOffsetY"] == 0.0


def test_continued_zoom_ticks_keep_the_preview_armed():
    """Every wheel tick changes the zoom, so the preview must be renewed.

    Dropping it between ticks leaves the front anchor-invalid until the
    replacement commits, which is the visible zoom flash.
    """
    active = _camera(48.3, -123.1, 9.5)
    first_target = _camera(48.3, -123.1, 9.8)
    anchor_reference = (48.3, -123.1, 9.5, 900, 700)
    widget = _preview_widget(
        active, first_target, anchor_reference, PanReference.from_camera(first_target)
    )
    assert widget._maybe_arm_barb_zoom_preview(active, first_target, 1.0, 1.0) is True

    second_target = _camera(48.3, -123.1, 9.9)
    widget._map_pan_reference = PanReference.from_camera(second_target, 1.0)
    widget._camera = second_target
    # The committed frame is still the pre-zoom camera, not the previous tick.
    assert (
        widget._maybe_arm_barb_zoom_preview(first_target, second_target, 1.0, 1.0)
        is True
    )
    assert widget._barb_zoom_preview is not None
    expected = zoom_preview_transform(
        PanReference(*anchor_reference, dpr=1.0),
        widget._map_pan_reference,
    )
    assert widget._barbs.props["previewScale"] == pytest.approx(expected[0])
    assert widget._barbs.props["previewOffsetX"] == pytest.approx(expected[1])
    assert widget._barbs.props["previewOffsetY"] == pytest.approx(expected[2])
    assert widget._barbs.props["anchorFrameValid"] is True


def test_retire_keeps_the_preview_but_retires_other_frames():
    active = _camera(48.3, -123.1, 9.5)
    target = _camera(48.3, -123.1, 9.8)
    anchor_reference = (48.3, -123.1, 9.5, 900, 700)
    widget = _preview_widget(
        active, target, anchor_reference, PanReference.from_camera(target, 1.0)
    )
    widget._maybe_arm_barb_zoom_preview(active, target, 1.0, 1.0)

    widget._barbs.props["anchorFrameValid"] = False
    widget._retire_barb_anchor_frame(active, target, 1.0)
    assert widget._barbs.props["anchorFrameValid"] is True

    later = _camera(48.3, -123.1, 9.8, stamp=OTHER_STAMP)
    widget._retire_barb_anchor_frame(active, later, 1.0)
    assert widget._barbs.props["anchorFrameValid"] is False


@pytest.mark.parametrize(
    "case",
    ("time", "resize", "dpr", "source", "forecast", "missing_front"),
)
def test_preview_is_suppressed_on_incompatible_frames(case):
    active = _camera(48.3, -123.1, 9.5)
    anchor_reference = (48.3, -123.1, 9.5, 900, 700)
    identity = _Identity()
    target = _camera(48.3, -123.1, 9.8)
    previous = target
    previous_dpr, current_dpr = 1.0, 1.0
    if case == "time":
        target = _camera(48.3, -123.1, 9.8, stamp=OTHER_STAMP)
        previous = _camera(48.3, -123.1, 9.8)
        anchor_reference = (48.3, -123.1, 9.8, 900, 700)
    elif case == "resize":
        target = _camera(48.3, -123.1, 9.8, width=901)
        previous = _camera(48.3, -123.1, 9.8, width=900)
    elif case == "dpr":
        target = _camera(48.3, -123.1, 9.8)
        previous = target
        previous_dpr, current_dpr = 1.0, 2.0
    elif case == "source":
        # Same frame and covering identity, but a different source: the
        # requested camera's source does not match the displayed front's.
        identity = _Identity(covers=True, source="grib")
        target = ViewportCamera(48.3, -123.1, 9.8, 900, 700, STAMP, source="noaa")
    elif case == "forecast":
        anchor_reference = (48.3, -123.1, 9.8, 900, 700)
        previous = _camera(48.3, -123.1, 9.8, stamp=OTHER_STAMP)
    elif case == "missing_front":
        active = None
    widget = _preview_widget(
        active, target, anchor_reference, PanReference.from_camera(target, 1.0)
    )
    widget._native_active_identity = identity if case != "missing_front" else None
    if case == "missing_front":
        widget._native_active_camera = None

    assert (
        widget._maybe_arm_barb_zoom_preview(previous, target, previous_dpr, current_dpr)
        is False
    )
    assert widget._barb_zoom_preview is None
    assert "previewScale" not in widget._barbs.props


def test_commit_resets_preview_on_the_new_front():
    """A real staged selection commits and the swapped front is identity."""
    old_front, staged = _Recorder(), _Recorder()
    for item in (old_front, staged):
        item.props.update(
            {"previewScale": 2.0, "previewOffsetX": 3.0, "previewOffsetY": 4.0}
        )

    pending_camera = _camera(48.3, -123.1, 9.8)
    provider = SimpleNamespace(pin_selection=mock.Mock())
    manager = SimpleNamespace(commit_pending=mock.Mock())
    widget = SimpleNamespace(
        _barbs=old_front,
        _barbs_back=staged,
        _fill=None,
        _fill_back=None,
        _native_front_index=0,
        _wind_opacity=1.0,
        _wind_gradient_enabled=False,
        _wind_gradient_opacity=0.0,
        _native_pending_camera=pending_camera,
        _native_pending_identity=object(),
        _native_pending_selection=object(),
        _native_pending_dpr=1.0,
        _native_pending_selection_urls=(),
        _barb_zoom_preview={
            "anchor": PanReference(48.3, -123.1, 9.5, 900, 700),
            "zoom": 9.8,
            "stamp": STAMP.timestamp(),
            "width": 900.0,
            "height": 700.0,
            "dpr": 1.0,
        },
        _adopt_shared_anchor_front=lambda: None,
        _provider=provider,
        _publication_manager=manager,
        _native_publication_tokens={("barbs", 1): object(), ("fill", 1): object()},
        _native_contexts={},
    )
    for name in (
        "_commit_native_weather",
        "_clear_barb_zoom_preview",
    ):
        setattr(
            widget,
            name,
            MethodType(getattr(_viewport_widget_class(), name), widget),
        )
    widget._reset_barb_zoom_preview_item = (
        _viewport_widget_class()._reset_barb_zoom_preview_item
    )

    widget._commit_native_weather()

    assert widget._barb_zoom_preview is None
    assert widget._barbs is staged
    for item in (old_front, staged):
        assert item.props["previewScale"] == 1.0
        assert item.props["previewOffsetX"] == 0.0
        assert item.props["previewOffsetY"] == 0.0
    provider.pin_selection.assert_called()
    manager.commit_pending.assert_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
