"""The headless one-cell ENC fallback paints inside its clip and nowhere else."""

from types import MethodType, SimpleNamespace

import numpy as np
import pytest
import shapely
from PySide6.QtGui import QImage

from sailworks.core.mapviewport import MercatorProjection
from sailworks.qt import gribviewport
from sailworks.qt.encfallback import (
    device_projection,
    fallback_accepts,
    fallback_clips,
    fallback_compose_fields,
    fallback_request,
    render_cell_overlay,
    render_clips_overlay,
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
