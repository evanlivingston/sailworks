"""The headless one-cell ENC fallback paints inside its clip and nowhere else."""

import os
from pathlib import Path
from types import MethodType, SimpleNamespace

import numpy as np
import pytest
import shapely
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter
from PySide6.QtQuick import QQuickItem, QQuickView
from PySide6.QtTest import QTest

from sailworks.core.mapviewport import MercatorProjection
from sailworks.qt import encfallback, enc_trace, gribviewport
from sailworks.qt.encfallback import (
    ENCFallbackOverlay,
    clear_render_cache,
    device_projection,
    fallback_accepts,
    fallback_clips,
    fallback_compose_fields,
    fallback_request,
    render_cache_key,
    render_cell_overlay,
    render_clips_overlay,
    render_clips_overlay_cached,
)
from sailworks.qt.gribviewport import _viewport_widget_class
from sailworks.rendering.vectordrawer.s57chartdrawer import S57ChartDrawer
from test.enc_scene_fixtures import _write_cell


class _Canvas(MercatorProjection):
    """A frozen Mercator camera: the projection the scene publishes."""

    def __init__(self, latitude, longitude, zoom, width, height):
        self._center_lat = latitude
        self._center_lon = longitude
        self._zoom = zoom
        self.width = width
        self.height = height

    def get_allocated_width(self):
        return self.width

    def get_allocated_height(self):
        return self.height

    def queue_draw(self):
        return None


def _projection(latitude, longitude, zoom, width, height):
    """``projection_for``'s contract: an (N, 2) lon/lat array to pixels."""
    canvas = _Canvas(latitude, longitude, zoom, width, height)

    def project(points):
        points = np.asarray(points, dtype=np.float64)
        xs, ys = canvas.convert_geographic_to_screen_batch(points[:, 1], points[:, 0])
        return np.column_stack((xs, ys))

    return project


WIDTH = 300
HEIGHT = 300
ZOOM = 5.0
CENTER = (5.0, 5.0)
CLIP = [(1, 1), (3, 1), (3, 3), (1, 3)]
# The clip's own geometry, pinned independently of the projection: a few
# pixels of slack for antialiasing where the fill meets the clip path.
BBOX_TOLERANCE = 3.0


def _pixel(projection, lon, lat):
    x, y = projection(np.array([[lon, lat]]))[0]
    return int(round(x)), int(round(y))


def _alpha(image, x, y):
    assert 0 <= x < image.width() and 0 <= y < image.height()
    return image.pixelColor(x, y).alpha()


def _transparent(image):
    return all(
        image.pixelColor(x, y).alpha() == 0
        for x in range(0, image.width(), 25)
        for y in range(0, image.height(), 25)
    )


def _opaque_bbox(image):
    """The extent of the painted pixels, read straight from the buffer."""
    data = np.frombuffer(bytes(image.constBits()), dtype=np.uint8)
    rows = data.reshape(image.height(), image.bytesPerLine())
    alpha = rows[:, 3 : image.width() * 4 : 4]
    ys, xs = np.nonzero(alpha)
    assert len(xs) > 0, "nothing was painted"
    return (
        float(xs.min()),
        float(ys.min()),
        float(xs.max()),
        float(ys.max()),
    )


def _projected_bounds(projection, clip):
    ring = projection(shapely.get_coordinates(clip))
    return (
        ring[:, 0].min(),
        ring[:, 1].min(),
        ring[:, 0].max(),
        ring[:, 1].max(),
    )


@pytest.fixture()
def cell(tmp_path):
    path = tmp_path / "cell.gpkg"
    _write_cell(path)
    return path


def test_paints_inside_the_clip_only(cell):
    projection = _projection(*CENTER, ZOOM, WIDTH, HEIGHT)
    clip = shapely.Polygon(CLIP)

    image = render_cell_overlay(
        str(cell), shapely.to_wkb(clip), projection, WIDTH, HEIGHT, palette="cm93"
    )

    assert image.width() == WIDTH
    assert image.height() == HEIGHT
    assert image.format() == QImage.Format.Format_ARGB32_Premultiplied
    # Well inside the clip: the DEPARE fill is opaque.
    assert _alpha(image, *_pixel(projection, 2.0, 2.0)) == 255
    # Inside the cell's DEPARE but outside the clip: nothing is painted.
    assert _alpha(image, *_pixel(projection, 8.0, 8.0)) == 0
    # The image corner is outside the clip too.
    assert _alpha(image, 1, 1) == 0
    assert _alpha(image, WIDTH - 2, HEIGHT - 2) == 0
    # The painted extent is the clip's own extent, not an artifact of the
    # projection: an incorrectly scaled projection moves it off this box.
    painted = _opaque_bbox(image)
    expected = _projected_bounds(projection, clip)
    for actual, wanted in zip(painted, expected):
        assert abs(actual - wanted) <= BBOX_TOLERANCE, (painted, expected)


def test_clip_hole_is_excluded(cell):
    projection = _projection(*CENTER, ZOOM, WIDTH, HEIGHT)
    outer = [(0.5, 0.5), (3.5, 0.5), (3.5, 3.5), (0.5, 3.5)]
    # The hole winds the same way as its exterior: under nonzero winding it
    # would be filled, which is exactly the double-draw the clip prevents.
    inner = [(1.5, 1.5), (2.5, 1.5), (2.5, 2.5), (1.5, 2.5)]
    clip = shapely.Polygon(outer, [inner])
    assert clip.exterior.is_ccw == clip.interiors[0].is_ccw

    image = render_cell_overlay(
        str(cell), shapely.to_wkb(clip), projection, WIDTH, HEIGHT
    )

    assert _alpha(image, *_pixel(projection, 2.0, 2.0)) == 0
    assert _alpha(image, *_pixel(projection, 1.0, 1.0)) == 255
    assert _alpha(image, *_pixel(projection, 3.0, 3.0)) == 255


def test_degenerate_clip_is_transparent(cell):
    projection = _projection(*CENTER, ZOOM, WIDTH, HEIGHT)

    for clip in (
        shapely.to_wkb(shapely.Polygon()),
        shapely.to_wkb(shapely.Polygon([(2, 2), (2, 2), (2, 2)])),
        b"",
    ):
        image = render_cell_overlay(str(cell), clip, projection, WIDTH, HEIGHT)
        assert image.width() == WIDTH
        assert image.height() == HEIGHT
        assert _transparent(image)


def test_missing_cell_raises(cell):
    projection = _projection(*CENTER, ZOOM, WIDTH, HEIGHT)

    with pytest.raises(FileNotFoundError):
        render_cell_overlay(
            str(cell) + ".missing",
            shapely.to_wkb(shapely.Polygon(CLIP)),
            projection,
            WIDTH,
            HEIGHT,
        )


def test_drawer_failure_paints_nothing(monkeypatch, cell):
    projection = _projection(*CENTER, ZOOM, WIDTH, HEIGHT)

    def boom(self, gpsmap, cr, vector_file, bounding):
        raise RuntimeError("the drawer failed")

    monkeypatch.setattr(S57ChartDrawer, "draw", boom)

    image = render_cell_overlay(
        str(cell), shapely.to_wkb(shapely.Polygon(CLIP)), projection, WIDTH, HEIGHT
    )

    assert image.width() == WIDTH
    assert image.height() == HEIGHT
    assert _transparent(image)


def _square(min_lon, min_lat, max_lon, max_lat):
    return shapely.to_wkb(
        shapely.Polygon(
            [
                (min_lon, min_lat),
                (max_lon, min_lat),
                (max_lon, max_lat),
                (min_lon, max_lat),
            ]
        )
    )


def test_composite_paints_every_clip(cell):
    projection = _projection(*CENTER, ZOOM, WIDTH, HEIGHT)
    clips = [
        {"path": str(cell), "clip_wkb": _square(1, 1, 2, 2)},
        {"path": str(cell), "clip_wkb": _square(7, 7, 8, 8)},
    ]

    image = render_clips_overlay(clips, projection, WIDTH, HEIGHT)

    assert image.width() == WIDTH
    assert image.height() == HEIGHT
    assert image.format() == QImage.Format.Format_ARGB32_Premultiplied
    assert _alpha(image, *_pixel(projection, 1.5, 1.5)) == 255
    assert _alpha(image, *_pixel(projection, 7.5, 7.5)) == 255
    # Between the two clips the cell's own DEPARE is still not painted: the
    # composite is the clips' ground, not the cell's extent.
    assert _alpha(image, *_pixel(projection, 3.5, 3.5)) == 0
    assert _alpha(image, *_pixel(projection, 9.0, 9.0)) == 0


def test_composite_skips_a_cell_that_cannot_be_painted(cell):
    projection = _projection(*CENTER, ZOOM, WIDTH, HEIGHT)
    clips = [
        {"path": str(cell) + ".missing", "clip_wkb": _square(1, 1, 2, 2)},
        {"path": str(cell), "clip_wkb": _square(7, 7, 8, 8)},
    ]

    image = render_clips_overlay(clips, projection, WIDTH, HEIGHT)

    assert _alpha(image, *_pixel(projection, 1.5, 1.5)) == 0
    assert _alpha(image, *_pixel(projection, 7.5, 7.5)) == 255


def test_fallback_clips_keeps_only_drawable_entries():
    refused = {
        "path": "/cells/a.gpkg",
        "name": "a",
        "reason": "oversized",
        "clip_wkb": b"clip",
    }

    assert fallback_clips({"unavailable_clips": (refused,)}) == (refused,)
    assert fallback_clips(None) == ()
    assert fallback_clips({}) == ()
    # A plan entry with no path or no clip is the scene's report, not the
    # overlay's request.
    assert (
        fallback_clips(
            {
                "unavailable_clips": (
                    {"path": "", "clip_wkb": b"clip"},
                    {"path": "/cells/b.gpkg", "clip_wkb": None},
                    None,
                )
            }
        )
        == ()
    )


def test_fallback_request_needs_clips_and_a_projection():
    camera = object()
    clip = {"path": "/cells/a.gpkg", "clip_wkb": b"clip"}

    def projection(points):
        return points

    metadata = {
        "unavailable_clips": (clip,),
        "projection": projection,
        "camera": camera,
    }

    assert fallback_request(metadata) == ((clip,), projection, camera)
    # A scene with no refused cell, and a scene with no projection to place
    # one, both paint nothing.
    assert fallback_request({**metadata, "unavailable_clips": ()}) is None
    assert fallback_request({**metadata, "projection": None}) is None


def test_fallback_accepts_only_the_current_request():
    camera = object()
    other_camera = object()
    image = QImage(2, 2, QImage.Format.Format_ARGB32_Premultiplied)
    token = (3, camera)

    assert fallback_accepts((token, image), token)
    # An older generation, or another camera, is a view the overlay left.
    assert not fallback_accepts(((4, camera), image), token)
    assert not fallback_accepts(((3, other_camera), image), token)
    assert not fallback_accepts(((4, camera), image), (4, other_camera))
    assert not fallback_accepts(None, token)
    assert not fallback_accepts((token, QImage()), token)
    assert not fallback_accepts((token, image), None)


def test_device_projection_scales_the_published_projection():
    projection = _projection(*CENTER, ZOOM, WIDTH, HEIGHT)
    points = np.array([[2.0, 2.0], [6.0, 4.0]])

    scaled = device_projection(projection, 2.0)

    assert np.allclose(scaled(points), projection(points) * 2.0)
    assert device_projection(projection, 1.0) is projection
    assert scaled(np.empty((0, 2))).shape == (0, 2)


def test_compose_fields_name_the_render():
    fields = fallback_compose_fields(
        7,
        3,
        (
            {"path": "/cells/US5CA1AB.gpkg", "name": "US5CA1AB"},
            {"path": "/cells/unnamed.gpkg", "clip_wkb": b"clip"},
        ),
        600,
        400,
        12.5,
        False,
    )

    assert fields == {
        "generation": 7,
        "serial": 3,
        "clips": 2,
        "cells": ["US5CA1AB", "unnamed.gpkg"],
        "width": 600,
        "height": 400,
        "elapsed_ms": 12.5,
        "failed": False,
    }
    failed = fallback_compose_fields(None, 4, (), 0, 0, None, True)
    assert failed["failed"] is True
    assert failed["clips"] == 0
    assert failed["cells"] == []


class _Placed:
    """Records the placement a real item would apply to itself."""

    def __init__(self):
        self.x = None
        self.y = None

    def setX(self, value):
        self.x = float(value)

    def setY(self, value):
        self.y = float(value)

    def setWidth(self, value):
        self.width = value

    def setHeight(self, value):
        self.height = value


class _FallbackRecorder:
    def __init__(self):
        self.placed = None
        self.camera_calls = []

    def place(self, x, y):
        self.placed = (x, y)

    def camera_changed(self, camera, dpr=None):
        self.camera_calls.append((camera, dpr))


def test_place_enc_scene_moves_the_fallback_with_the_mesh(monkeypatch):
    reference = object()
    calls = []

    def placement(pan_reference, frame, dpr):
        calls.append((pan_reference, frame, dpr))
        return 12.0, -7.5

    monkeypatch.setattr(gribviewport, "enc_scene_placement", placement)

    mesh = _Placed()
    fallback = _FallbackRecorder()
    widget = SimpleNamespace(
        _enc_scene_item=mesh,
        _enc_fallback=fallback,
        _map_pan_reference=reference,
        _camera=None,
        _enc_scene_frame_camera=None,
        _enc_scene_camera=None,
        _camera_dpr=2.0,
    )
    widget._place_enc_scene = MethodType(
        _viewport_widget_class()._place_enc_scene, widget
    )

    widget._place_enc_scene()

    # The mesh and the fallback take the same helper's values: one placement,
    # not a second computation.
    assert calls == [(reference, None, 2.0)]
    assert (mesh.x, mesh.y) == (12.0, -7.5)
    assert fallback.placed == (12.0, -7.5)


def test_request_enc_scene_hands_the_mesh_ratio_to_the_fallback():
    camera = SimpleNamespace(width=300, height=200)

    class _Layer:
        def request(self, camera, replace=False):
            return 9

    fallback = _FallbackRecorder()
    widget = SimpleNamespace(
        _camera=camera,
        _enc_scene_item=_Placed(),
        _enc_scene_model=_Layer(),
        _enc_scene_pending_generation=None,
        _enc_scene_retry_attempt=0,
        _enc_scene_retry_camera=camera,
        # ``set_camera`` records the ratio the mesh is placed with here.
        _camera_dpr=2.0,
        _enc_fallback=fallback,
    )
    widget._request_enc_scene = MethodType(
        _viewport_widget_class()._request_enc_scene, widget
    )

    assert widget._request_enc_scene() == 9

    assert fallback.camera_calls == [(camera, 2.0)]


# --- The retained-ground policy -------------------------------------------


class _Camera:
    """A camera stub: the overlay reads its size and its identity only."""

    def __init__(self, width=WIDTH, height=HEIGHT):
        self.width = width
        self.height = height


def _projection_passthrough(points):
    return points


def _refused(name="US5CA1AB"):
    return {
        "path": "/cells/%s.gpkg" % (name,),
        "name": name,
        "reason": "oversized",
        "clip_wkb": b"clip",
    }


def _publication(camera, clips=(), partial=False):
    return {
        "unavailable_clips": tuple(clips),
        "projection": _projection_passthrough,
        "camera": camera,
        "partial": partial,
    }


def _overlay():
    container = QQuickItem()
    container.setWidth(WIDTH)
    container.setHeight(HEIGHT)
    return ENCFallbackOverlay(container, dpr=1.0)


def _painted_image(colour):
    image = QImage(8, 8, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(colour)
    return image


def test_partial_publication_keeps_the_retained_ground():
    overlay = _overlay()
    camera = _Camera()
    overlay.publish(_publication(camera, [_refused()]), generation=1, dpr=1.0)

    token = overlay._token
    canvas = overlay._canvas
    clips = overlay._clips

    # A partial that has not resolved any refusal yet proves nothing: it must
    # not drop the retained state nor invalidate the live request.
    overlay.publish(_publication(camera, (), partial=True), generation=2, dpr=1.0)

    assert overlay._token == token
    assert overlay._canvas == canvas
    assert overlay._clips == clips

    # The completion for the same camera still renders.
    overlay.publish(_publication(camera, [_refused()]), generation=3, dpr=1.0)
    assert overlay._token != token
    assert overlay._canvas[1] is camera
    overlay.stop()


def test_complete_publication_without_clips_clears():
    overlay = _overlay()
    camera = _Camera()
    overlay.publish(_publication(camera, [_refused()]), generation=1, dpr=1.0)
    assert overlay._canvas is not None

    overlay.publish(_publication(camera, ()), generation=2, dpr=1.0)

    assert overlay._token is None
    assert overlay._clips == ()
    assert overlay._canvas is None


def test_camera_change_retains_the_image_and_rejects_the_old_serial():
    overlay = _overlay()
    first = _Camera()
    second = _Camera(width=420, height=260)
    overlay.publish(_publication(first, [_refused()]), generation=1, dpr=1.0)

    painted = _painted_image(0xFF0000FF)
    overlay.item.set_image(painted)
    stale_token = overlay._token
    stale_serial = overlay._serial

    overlay.camera_changed(second, dpr=1.0)

    # The mesh keeps its previous frame; so does the overlay.
    assert overlay.item.image() is painted
    assert overlay._serial > stale_serial
    assert overlay._token != stale_token
    assert overlay._clips
    # A render from the old serial can never be presented...
    assert not fallback_accepts((stale_token, painted), overlay._token)
    overlay._painted((stale_token, _painted_image(0xFFFF00FF), {}))
    assert overlay.item.image() is painted

    # ...and the new camera's publication replaces the image.
    overlay.publish(_publication(second, [_refused()]), generation=2, dpr=1.0)
    fresh = _painted_image(0xFF00FF00)
    overlay._painted((overlay._token, fresh, {}))
    assert overlay.item.image() is fresh
    overlay.stop()


def test_camera_change_before_any_paint_traces_nothing(monkeypatch):
    events = []

    def record(kind, **fields):
        events.append((kind, fields))

    monkeypatch.setattr(enc_trace, "event", record)
    overlay = _overlay()

    overlay.camera_changed(_Camera(), dpr=1.0)

    assert events == []
    assert overlay.item.image().isNull()
    assert overlay._token is None


def test_a_held_publication_still_notifies_the_overlay():
    class _Mesh:
        def set_projection(self, projection):
            self.projection = projection

        def set_compose_margin(self, margin):
            self.margin = margin

    class _Fallback:
        def __init__(self):
            self.calls = []

        def publish(self, metadata, generation=None, dpr=None):
            self.calls.append((metadata, generation, dpr))

    camera = _Camera()
    metadata = _publication(camera, [_refused("refused")])
    fallback = _Fallback()
    widget = SimpleNamespace(
        _disposed=False,
        _enc_scene_item=_Mesh(),
        _enc_fallback=fallback,
        _camera=camera,
        _camera_dpr=2.0,
        # The first-raster hold returns True: this scene is never adopted.
        _hold_scene_for_first_raster=lambda generation, payload: True,
    )
    widget._enc_scene_published = MethodType(
        _viewport_widget_class()._enc_scene_published, widget
    )

    widget._enc_scene_published((6, "scene", metadata))

    # The overlay's content does not depend on adoption: the held complete
    # scene still paints the cells it refused.
    assert fallback.calls == [(metadata, 6, 2.0)]


# --- Bounded rendering, memoization and stale composes ---------------------


@pytest.fixture(autouse=True)
def _empty_render_cache():
    clear_render_cache()
    yield
    clear_render_cache()


class _KeyedCamera:
    """A camera stub with the fields the render cache key reads."""

    def __init__(
        self, center_lat=5.0, center_lon=5.0, zoom=ZOOM, width=WIDTH, height=HEIGHT
    ):
        self.center_lat = center_lat
        self.center_lon = center_lon
        self.zoom = zoom
        self.width = width
        self.height = height


def _pixel_bytes(image):
    """The image's ARGB bytes as ``(height, width, 4)``."""
    data = np.frombuffer(bytes(image.constBits()), dtype=np.uint8)
    rows = data.reshape(image.height(), image.bytesPerLine())
    return rows[:, : image.width() * 4].reshape(image.height(), image.width(), 4)


def _compose_clips(cell, clip=CLIP):
    return [{"path": str(cell), "clip_wkb": shapely.to_wkb(shapely.Polygon(clip))}]


def test_clip_bounds_cover_only_the_clip(cell):
    projection = _projection(*CENTER, ZOOM, WIDTH, HEIGHT)
    clip = shapely.Polygon(CLIP)
    canvas = encfallback._ProjectedCanvas(projection, WIDTH, HEIGHT, 2.0)

    bounds = encfallback._clip_pixel_bounds(canvas, clip, WIDTH, HEIGHT)

    left, top, right, bottom = bounds
    assert 0 <= left < right <= WIDTH
    assert 0 <= top < bottom <= HEIGHT
    assert (right - left) * (bottom - top) < WIDTH * HEIGHT * 0.1

    # The clamp keeps a clip that leaves the frame from addressing pixels
    # outside it; a clip entirely off the frame bounds to no pixels.
    far = shapely.Polygon([(100, 100), (101, 100), (101, 101), (100, 101)])
    empty = encfallback._clip_pixel_bounds(canvas, far, WIDTH, HEIGHT)
    assert empty[2] <= empty[0] or empty[3] <= empty[1]


def test_bounded_render_matches_the_full_frame_inside_the_clip(cell):
    projection = _projection(*CENTER, ZOOM, WIDTH, HEIGHT)
    clip = shapely.Polygon(CLIP)
    wkb = shapely.to_wkb(clip)

    # The full-frame paint, exactly as the overlay painted before the clip
    # was bounded: one frame-sized surface, the same canvas, no offset.
    reference = QImage(WIDTH, HEIGHT, QImage.Format.Format_ARGB32_Premultiplied)
    reference.fill(Qt.GlobalColor.transparent)
    canvas = encfallback._ProjectedCanvas(projection, WIDTH, HEIGHT, 2.0)
    encfallback._paint(reference, canvas, clip, wkb, str(cell), "cm93")

    bounded = render_cell_overlay(
        str(cell), wkb, projection, WIDTH, HEIGHT, palette="cm93"
    )

    # Compare the whole padded box the bounded paint covers, not only the
    # pixels strictly inside the clip: a wrong or doubled offset, a missing
    # pad or a mis-clamped bound leaves a difference at the edges.
    bounded_pixels = _pixel_bytes(bounded)
    reference_pixels = _pixel_bytes(reference)
    left, top, right, bottom = encfallback._clip_pixel_bounds(
        canvas, clip, WIDTH, HEIGHT
    )
    window = (slice(top, bottom), slice(left, right))
    assert window[0].stop > window[0].start and window[1].stop > window[1].start
    assert np.array_equal(bounded_pixels[window], reference_pixels[window])

    # Compare pixel for pixel inside the projected clip, where the ground is.
    ring = projection(shapely.get_coordinates(clip))
    xs, ys = np.meshgrid(np.arange(WIDTH) + 0.5, np.arange(HEIGHT) + 0.5)
    inside = shapely.contains_xy(shapely.Polygon(ring), xs, ys)
    assert inside.sum() > 0

    assert np.array_equal(bounded_pixels[inside], reference_pixels[inside])


def test_repeat_compose_avoids_the_drawer(monkeypatch, cell):
    calls = []
    original = S57ChartDrawer.draw

    def counting(drawer, *args, **kwargs):
        calls.append(1)
        return original(drawer, *args, **kwargs)

    monkeypatch.setattr(S57ChartDrawer, "draw", counting)
    projection = _projection(*CENTER, ZOOM, WIDTH, HEIGHT)
    clips = _compose_clips(cell)
    camera = _KeyedCamera()

    first, hit = render_clips_overlay_cached(
        clips, projection, WIDTH, HEIGHT, camera=camera, dpr=1.0
    )
    second, repeat = render_clips_overlay_cached(
        clips, projection, WIDTH, HEIGHT, camera=camera, dpr=1.0
    )

    assert hit is False
    assert repeat is True
    assert len(calls) == 1
    assert second is first
    # The cached frame is the painted one, not an empty stand-in.
    assert _alpha(second, *_pixel(projection, 2.0, 2.0)) == 255


def test_changed_camera_or_dpr_misses_the_cache(monkeypatch, cell):
    calls = []
    original = S57ChartDrawer.draw

    def counting(drawer, *args, **kwargs):
        calls.append(1)
        return original(drawer, *args, **kwargs)

    monkeypatch.setattr(S57ChartDrawer, "draw", counting)
    projection = _projection(*CENTER, ZOOM, WIDTH, HEIGHT)
    clips = _compose_clips(cell)

    hits = []
    for camera, dpr in (
        (_KeyedCamera(), 1.0),
        (_KeyedCamera(center_lon=5.25), 1.0),
        (_KeyedCamera(), 2.0),
    ):
        _, hit = render_clips_overlay_cached(
            clips, projection, WIDTH, HEIGHT, camera=camera, dpr=dpr
        )
        hits.append(hit)

    assert hits == [False, False, False]
    assert len(calls) == 3


def test_render_cache_evicts_to_its_byte_budget(monkeypatch, cell):
    """The budget, not the number of frames, bounds the cached pixels."""
    monkeypatch.setattr(encfallback, "RENDER_CACHE_BUDGET", 2_000)
    projection = _projection(*CENTER, ZOOM, WIDTH, HEIGHT)
    clips = _compose_clips(cell)
    frames = []

    def fake_render(clips, projection, width, height, palette=None):
        image = QImage(20, 10, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        frames.append(image)
        return image

    monkeypatch.setattr(encfallback, "render_clips_overlay", fake_render)
    cameras = [_KeyedCamera(center_lon=5.0 + step * 0.25) for step in range(4)]

    for camera in cameras:
        image, hit = render_clips_overlay_cached(
            clips, projection, WIDTH, HEIGHT, camera=camera, dpr=1.0
        )
        assert hit is False
        assert image is frames[-1]

    # Each 20x10 frame costs 800 bytes, so the fourth insertion pushes the
    # resident pixels past 2000 and the least recently used frame goes.
    assert len(frames) == 4
    assert len(encfallback._render_cache) == 2
    assert encfallback._render_cache_bytes == 1_600
    assert encfallback._render_cache_bytes <= encfallback.RENDER_CACHE_BUDGET

    # The frame just painted is not the one evicted.
    newest, hit = render_clips_overlay_cached(
        clips, projection, WIDTH, HEIGHT, camera=cameras[-1], dpr=1.0
    )
    assert hit is True
    assert newest is frames[-1]

    clear_render_cache()
    assert not encfallback._render_cache
    assert encfallback._render_cache_bytes == 0


def test_cache_key_captures_the_render_inputs(cell):
    clips = _compose_clips(cell)
    base = render_cache_key(clips, _KeyedCamera(), WIDTH, HEIGHT, 1.0)

    assert render_cache_key(clips, _KeyedCamera(), WIDTH, HEIGHT, 1.0) == base
    assert (
        render_cache_key(clips, _KeyedCamera(center_lat=5.5), WIDTH, HEIGHT, 1.0)
        != base
    )
    assert (
        render_cache_key(clips, _KeyedCamera(center_lon=5.5), WIDTH, HEIGHT, 1.0)
        != base
    )
    assert (
        render_cache_key(clips, _KeyedCamera(zoom=ZOOM + 1), WIDTH, HEIGHT, 1.0) != base
    )
    assert render_cache_key(clips, _KeyedCamera(), WIDTH * 2, HEIGHT, 1.0) != base
    assert render_cache_key(clips, _KeyedCamera(), WIDTH, HEIGHT, 2.0) != base
    assert render_cache_key(clips, _KeyedCamera(), WIDTH, HEIGHT, 1.0, "other") != base

    # A rewritten cell changes the key through its size and mtime alone.
    info = os.stat(cell)
    os.utime(cell, ns=(info.st_atime_ns, info.st_mtime_ns + 10**9))
    assert render_cache_key(clips, _KeyedCamera(), WIDTH, HEIGHT, 1.0) != base


def test_superseded_serial_is_not_rendered(monkeypatch):
    events = []
    renders = []

    def record(kind, **fields):
        events.append((kind, fields))

    def render(*args, **kwargs):
        renders.append(args)
        return QImage(), False

    monkeypatch.setattr(enc_trace, "event", record)
    monkeypatch.setattr(encfallback, "render_clips_overlay_cached", render)
    overlay = _overlay()
    camera = _Camera()
    clips = ({"path": "/cells/a.gpkg", "clip_wkb": b"clip"},)
    # The view has moved on: this job's serial is no longer the current one.
    overlay._token = (2, camera)

    payload = overlay._compose(
        ((1, camera), clips, _projection_passthrough, WIDTH, HEIGHT, 4, 1.0)
    )

    assert payload is None
    assert renders == []
    assert events == [
        (
            "fallback_skip",
            {"reason": "stale", "generation": 4, "serial": 1, "clips": 1},
        )
    ]
    assert overlay.item.image().isNull()


def test_current_serial_is_composed_and_traced(monkeypatch):
    events = []

    def record(kind, **fields):
        events.append((kind, fields))

    monkeypatch.setattr(enc_trace, "event", record)
    overlay = _overlay()
    camera = _Camera()
    clips = ({"path": "/cells/a.gpkg", "clip_wkb": b"clip"},)
    token = (3, camera)
    overlay._token = token

    payload = overlay._compose(
        (token, clips, _projection_passthrough, WIDTH, HEIGHT, 5, 1.0)
    )

    assert payload is not None
    assert payload[0] == token
    assert [kind for kind, _ in events] == ["fallback_cache"]
    # A hit or miss must be attributable to the generation that asked for it.
    assert events[0][1] == {"hit": False, "generation": 5, "serial": 3}


# --- The pixel grid the ground is presented on ----------------------------
#
# The defect these pin was invisible to every test above because each one
# painted and compared in the same grid.  On screen the overlay is a
# device-pixel image presented inside a QQuickPaintedItem, and the item used
# to stretch that image onto its own logical rect: one ratio too many, and the
# wrong ground at the wrong scale whenever the container, the camera frame and
# the adopted device ratio disagreed.

BACKGROUND = (12, 34, 56)


def _colour(image, x, y):
    """``(r, g, b, a)`` of one pixel, endian-independent."""
    pixel = image.pixelColor(int(x), int(y))
    return (pixel.red(), pixel.green(), pixel.blue(), pixel.alpha())


def _filled(width, height, colour=BACKGROUND):
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(*colour))
    return image


def _composited(overlay, background):
    painter = QPainter(background)
    try:
        painter.drawImage(0, 0, overlay)
    finally:
        painter.end()
    return background


def test_the_overlay_is_premultiplied_and_transparent_outside_the_clip(cell):
    projection = _projection(*CENTER, ZOOM, WIDTH, HEIGHT)
    wkb = shapely.to_wkb(shapely.Polygon(CLIP))

    overlay = render_cell_overlay(str(cell), wkb, projection, WIDTH, HEIGHT, "cm93")

    # Unpremultiplied ARGB32 composites every alpha-0 pixel as opaque black.
    assert overlay.format() == QImage.Format.Format_ARGB32_Premultiplied
    # A pixel the clip cannot reach is premultiplied transparent black, not
    # merely "alpha 0 with stale colour".
    outside = _pixel(projection, 8.0, 8.0)
    assert _colour(overlay, *outside) == (0, 0, 0, 0)

    # Compositing cannot tint the background there: the overlay contributes
    # nothing at all outside the clip.
    composited = _composited(overlay, _filled(WIDTH, HEIGHT))
    assert _colour(composited, *outside) == BACKGROUND + (255,)


def test_the_clip_rect_follows_the_scene_projection_at_each_ratio():
    clip = shapely.Polygon(CLIP)
    wkb = shapely.to_wkb(clip)
    logical = _projection(*CENTER, ZOOM, WIDTH, HEIGHT)
    ring = shapely.get_coordinates(clip)

    rects = {}
    for dpr in (1.0, 2.0):
        width = int(round(WIDTH * dpr))
        height = int(round(HEIGHT * dpr))
        rect = encfallback.overlay_clip_rect(
            wkb, device_projection(logical, dpr), width, height
        )
        projected = logical(ring) * dpr
        assert rect == pytest.approx(
            (
                projected[:, 0].min(),
                projected[:, 1].min(),
                projected[:, 0].max(),
                projected[:, 1].max(),
            )
        )
        rects[dpr] = rect

    # Linear in the ratio: a rect that is scaled twice (or not at all) breaks
    # this, and it is the whole "2x on one side only" failure.
    assert [value * 2.0 for value in rects[1.0]] == pytest.approx(list(rects[2.0]))


def test_compositing_at_ratio_two_leaves_every_outside_pixel_the_background(cell):
    dpr = 2.0
    width = int(round(WIDTH * dpr))
    height = int(round(HEIGHT * dpr))
    logical = _projection(*CENTER, ZOOM, WIDTH, HEIGHT)
    projection = device_projection(logical, dpr)
    wkb = shapely.to_wkb(shapely.Polygon(CLIP))

    overlay = render_cell_overlay(str(cell), wkb, projection, width, height, "cm93")
    composited = _composited(overlay, _filled(width, height))
    pixels = _pixel_bytes(composited).reshape(height, width, 4)

    # The projected clip's own rect is the only place ground may appear; the
    # box is widened by the antialiased edge only.
    rect = encfallback.overlay_clip_rect(wkb, projection, width, height)
    left = max(0, int(np.floor(rect[0])))
    top = max(0, int(np.floor(rect[1])))
    right = min(width, int(np.ceil(rect[2])) + 1)
    bottom = min(height, int(np.ceil(rect[3])) + 1)
    assert (right - left) * (bottom - top) < width * height * 0.2

    outside = np.ones((height, width), dtype=bool)
    outside[top:bottom, left:right] = False
    expected = np.array(
        (BACKGROUND[2], BACKGROUND[1], BACKGROUND[0], 255), dtype=np.uint8
    )
    assert np.array_equal(
        pixels[outside], np.broadcast_to(expected, pixels[outside].shape)
    )
    assert not np.array_equal(
        pixels[~outside], np.broadcast_to(expected, pixels[~outside].shape)
    )


def test_the_item_keeps_the_image_on_the_device_grid():
    item = encfallback.ENCFallbackItem()
    item.setWidth(300.0)
    item.setHeight(200.0)

    image = QImage(200, 200, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    marker = QColor(255, 0, 0, 255)
    for x in range(100, 104):
        for y in range(100, 104):
            image.setPixelColor(x, y, marker)
    item.set_image(image)

    # The item's own texture: 300x200 logical is 600x400 device at two device
    # pixels per item pixel.  It starts saturated so any pixel the paint leaves
    # alone is visible as stale ground.
    texture = _filled(600, 400, (0, 0, 255))
    painter = QPainter(texture)
    try:
        painter.scale(2.0, 2.0)
        item.paint(painter)
    finally:
        painter.end()

    # One image pixel, one device pixel: the 200x200 image covers the first
    # 200x200 device pixels, so the marker stays at its own coordinates.
    assert _colour(texture, 101, 101) == (255, 0, 0, 255)
    # Placing the image at its size in item coordinates would put the marker
    # at device (201, 201); stretching it onto the item's logical rect would
    # put it at device (300, 200).  Neither may show the marker.
    assert _colour(texture, 201, 201) == (0, 0, 0, 0)
    assert _colour(texture, 301, 201) == (0, 0, 0, 0)
    # Paint owns the whole item rect, so no previous ground survives outside
    # the image.
    assert _colour(texture, 350, 250) == (0, 0, 0, 0)

    # Composited, that untouched ground is the window's own, not black paint.
    composited = _composited(texture, _filled(600, 400))
    assert _colour(composited, 350, 250) == BACKGROUND + (255,)


def test_a_presented_image_is_converted_to_premultiplied():
    item = encfallback.ENCFallbackItem()
    unpremultiplied = QImage(4, 4, QImage.Format.Format_ARGB32)
    unpremultiplied.fill(QColor(200, 10, 10, 0))

    item.set_image(unpremultiplied)

    assert item.image().format() == QImage.Format.Format_ARGB32_Premultiplied
    # The transparent pixel is exact premultiplied black, which is what the
    # compositor requires for "nothing here".
    assert _colour(item.image(), 1, 1) == (0, 0, 0, 0)


# --- The layers that host the overlay --------------------------------------
#
# Both layers are plain ``Item``s whose only content is the item the viewport
# parents into them from Python.  Their visibility is a QML binding over the
# child list, so it only holds if a child added (and removed) imperatively is
# observed by that binding: a layer stuck at ``children.length == 0`` would
# hide the overlay no matter what the worker painted into it.

SHADERS = Path(__file__).resolve().parents[1] / "sailworks/qt/shaders"


@pytest.fixture(scope="module")
def scene_root():
    """The real chart scene, shown offscreen so its bindings evaluate."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QGuiApplication.instance() or QGuiApplication([])
    view = QQuickView()
    view.setSource(QUrl.fromLocalFile(str(SHADERS / "ViewportChartScene.qml")))
    assert not view.errors(), view.errors()
    root = view.rootObject()
    assert root is not None
    root.setProperty("width", 96)
    root.setProperty("height", 96)
    view.resize(96, 96)
    view.show()
    QTest.qWait(20)
    try:
        yield root
    finally:
        view.close()


def test_the_host_layer_follows_the_item_parented_into_it(scene_root):
    for name in ("encFallbackLayer", "encSceneLayer"):
        layer = scene_root.findChild(QQuickItem, name)
        assert layer is not None, name
        assert layer.property("visible") is False, f"{name} starts hosting an item"

        # What the viewport does from Python: attach one item and size it.
        item = QQuickItem(layer)
        item.setWidth(96.0)
        item.setHeight(96.0)
        QTest.qWait(1)
        assert layer.property("visible") is True, f"{name} ignored its item"

        item.setParentItem(None)
        QTest.qWait(1)
        assert layer.property("visible") is False, f"{name} stayed visible"
