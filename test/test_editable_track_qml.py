"""Selected editable track in a retained Qt Quick Shapes layer."""

import math
import os
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickItem, QQuickView

ROOT = Path(__file__).resolve().parents[1]
SHADERS = ROOT / "sailworks/qt/shaders"
LAYER = SHADERS / "EditableTrackLayer.qml"
SCENE = SHADERS / "ViewportChartScene.qml"

PAD = 20.0
WIDTH = 800.0
HEIGHT = 600.0
CENTER_LAT = 50.78
CENTER_LON = -1.33
ZOOM = 12.0

POINTS = [
    [50.803, -1.430],
    [50.819, -1.380],
    [50.826, -1.315],
    [50.813, -1.260],
    [50.788, -1.235],
]


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    yield QGuiApplication.instance() or QGuiApplication([])


def _world_y(latitude, world):
    radians = max(-85.05112878, min(85.05112878, latitude)) * math.pi / 180
    return world * (1 - math.asinh(math.tan(radians)) / math.pi) / 2


def _expected(
    latitude,
    longitude,
    center_latitude=CENTER_LAT,
    center_longitude=CENTER_LON,
    zoom=ZOOM,
    padding=PAD,
    width=WIDTH,
    height=HEIGHT,
):
    """The Canvas ``screenPoint`` formula, in padded local coordinates."""
    world = 256 * (2**zoom)
    dx = (longitude - center_longitude) * world / 360
    dx -= math.floor(dx / world + 0.5) * world
    return (
        padding + width / 2 + dx,
        padding
        + height / 2
        + _world_y(latitude, world)
        - _world_y(center_latitude, world),
    )


def _track(
    points=None,
    name="practice lap",
    active=True,
    highlight=1,
    editing=True,
    roundings=None,
    hover=None,
):
    track = {
        "name": name,
        "points": [list(p) for p in (points or POINTS)],
        "active": active,
        "highlight": highlight,
        "editing": editing,
    }
    if roundings is not None:
        track["roundings"] = list(roundings)
    if hover is not None:
        track["hover"] = hover
    return track


def _load(tracks, preview=None, **camera):
    view = QQuickView()
    view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
    view.resize(int(WIDTH + 2 * PAD), int(HEIGHT + 2 * PAD))
    view.setSource(QUrl.fromLocalFile(str(LAYER)))
    assert not view.errors(), view.errors()
    root = view.rootObject()
    assert root is not None
    values = {
        "tracks": tracks,
        "centerLatitude": camera.get("center_latitude", CENTER_LAT),
        "centerLongitude": camera.get("center_longitude", CENTER_LON),
        "zoom": camera.get("zoom", ZOOM),
        "panPadding": camera.get("pan_padding", PAD),
        "viewportWidth": camera.get("viewport_width", WIDTH),
        "viewportHeight": camera.get("viewport_height", HEIGHT),
    }
    if preview is not None:
        values["movePreview"] = preview
    for key, value in values.items():
        assert root.setProperty(key, value), key
    return view, root


def _projected(layer):
    points = layer.property("projected")
    assert points is not None
    if hasattr(points, "toVariant"):
        points = points.toVariant()
    result = []
    for point in points:
        if hasattr(point, "toVariant"):
            point = point.toVariant()
        if isinstance(point, dict):
            result.append((float(point["x"]), float(point["y"])))
        else:
            result.append((float(point.x()), float(point.y())))
    return result


def _close(actual, expected, tolerance=1e-6):
    assert math.isclose(actual, expected, abs_tol=tolerance), (actual, expected)


def _named_items(root):
    found = {}
    pending = [root]
    while pending:
        item = pending.pop()
        found[item.objectName()] = item
        pending.extend(item.childItems())
    return found


def _mark_part(layer, index, name):
    mark = _named_items(layer)["editableTrackMark%d" % index]
    return next(c for c in mark.childItems() if c.objectName() == name)


def _close_point(actual, expected, tolerance=1e-6):
    _close(actual[0], expected[0], tolerance)
    _close(actual[1], expected[1], tolerance)


def test_layer_is_one_retained_shapes_polyline(app):
    source = LAYER.read_text()
    assert "import QtQuick.Shapes" in source
    assert "PathPolyline" in source and "path: trackLayer.projected" in source
    # The root id must not shadow ``QQuickItem.layer``; otherwise delegate
    # expressions like ``layer.markColor(...)`` resolve to the item's layer
    # property and evaluate to undefined at runtime.
    assert "id: layer\n" not in source
    assert "id: trackLayer" in source
    assert "MouseArea" not in source, "the layer must not take input"
    assert "property var tracks: []" in source


def test_polyline_length_follows_the_track(app):
    view, layer = _load([_track()])
    try:
        assert layer.property("selectedIndex") == 0
        assert layer.property("trackVisible") is True
        path = layer.findChild(QObject, "editableTrackPath")
        assert path is not None
        assert path.property("strokeColor").name() == "#91321c"
        _close(path.property("strokeWidth"), 1.0)
        assert [float(v) for v in layer.property("dashPattern").toVariant()] == [
            8.0,
            4.0,
        ]

        longer = POINTS + [
            [50.770, -1.300],
            [50.755, -1.275],
            [50.742, -1.250],
            [50.730, -1.235],
            [50.760, -1.400],
            [50.780, -1.420],
            [50.795, -1.440],
        ]
        assert layer.setProperty("tracks", [_track(longer)])
        assert len(_projected(layer)) == len(longer)

        assert layer.setProperty("tracks", [_track(POINTS[:2])])
        assert len(_projected(layer)) == 2
    finally:
        view.close()
        view.releaseResources()


def test_projection_refreshes_on_camera_and_padding(app):
    view, layer = _load([_track()])
    try:
        first = _projected(layer)[0]
        assert layer.setProperty("centerLatitude", CENTER_LAT + 0.05)
        moved = _projected(layer)[0]
        assert moved[1] != first[1]
        _close(
            moved[1],
            _expected(POINTS[0][0], POINTS[0][1], center_latitude=CENTER_LAT + 0.05)[1],
        )

        assert layer.setProperty("centerLatitude", CENTER_LAT)
        assert layer.setProperty("centerLongitude", CENTER_LON + 0.05)
        moved = _projected(layer)[0]
        _close(
            moved[0],
            _expected(POINTS[0][0], POINTS[0][1], center_longitude=CENTER_LON + 0.05)[
                0
            ],
        )

        assert layer.setProperty("centerLongitude", CENTER_LON)
        assert layer.setProperty("zoom", ZOOM + 1)
        assert _projected(layer)[0] != first
        _close(
            _projected(layer)[0][0],
            _expected(POINTS[0][0], POINTS[0][1], zoom=ZOOM + 1)[0],
        )

        before = _projected(layer)[0]
        assert layer.setProperty("panPadding", PAD + 16)
        after = _projected(layer)[0]
        _close(after[0] - before[0], 16)
        _close(after[1] - before[1], 16)
        assert layer.setProperty("viewportWidth", WIDTH + 40)
        _close(_projected(layer)[0][0] - after[0], 20)
    finally:
        view.close()
        view.releaseResources()


def test_preview_replaces_first_and_middle_vertices(app):
    view, layer = _load([_track()])
    try:
        base = _projected(layer)

        assert layer.setProperty("movePreview", [0, 0, 50.90, -1.20])
        moved = _projected(layer)
        _close_point(moved[0], _expected(50.90, -1.20))
        assert moved[1:] == base[1:]

        assert layer.setProperty("movePreview", [0, 2, 50.70, -1.10])
        moved = _projected(layer)
        _close_point(moved[2], _expected(50.70, -1.10))
        assert moved[0] == base[0] and moved[4] == base[4]

        assert layer.setProperty("movePreview", [3, 1, 50.70, -1.10])
        assert _projected(layer) == base
        assert layer.setProperty("movePreview", [0, 9, 50.70, -1.10])
        assert _projected(layer) == base
    finally:
        view.close()
        view.releaseResources()


def test_inactive_style_follows_the_canvas(app):
    view, layer = _load([_track(active=False, highlight=False)])
    try:
        path = layer.findChild(QObject, "editableTrackPath")
        color = path.property("strokeColor")
        _close(color.red() / 255, 145 / 255, 1e-3)
        _close(color.green() / 255, 74 / 255, 1e-3)
        _close(color.blue() / 255, 124 / 255, 1e-3)
        _close(color.alpha() / 255, 0.6, 1e-3)
        _close(path.property("strokeWidth"), 0.8)
        assert [float(v) for v in layer.property("dashPattern").toVariant()] == [
            10.0,
            5.0,
        ]
        assert layer.property("highlightIndex") == -1
    finally:
        view.close()
        view.releaseResources()


def test_labels_marks_and_handles_follow_projected_points(app):
    view, layer = _load([_track(highlight=2)])
    try:
        view.show()
        app.processEvents()
        assert layer.property("trackEditing") is True
        projected = _projected(layer)
        named = {}
        pending = [layer]
        while pending:
            item = pending.pop()
            named[item.objectName()] = item
            pending.extend(item.childItems())
        _close(named["editableTrackName"].x(), projected[0][0] - 10)
        _close(named["editableTrackName"].y(), projected[0][1] - 20)

        for index, point in enumerate(projected):
            mark = named["editableTrackMark%d" % index]
            _close_point((mark.x(), mark.y()), point)
            children = mark.childItems()
            number = next(
                c for c in children if c.objectName() == "editableTrackNumber"
            )
            handle = next(
                c for c in children if c.objectName() == "editableTrackHandle"
            )
            _close(number.x() + mark.x(), point[0] - 4)
            _close(number.y() + mark.y(), point[1] + 8)
            assert number.property("text") == str(index + 1)
            diameter = 16 if index == 2 else 12
            assert handle.width() == diameter, {
                "index": index,
                "delegate_index": mark.property("index"),
                "highlight": layer.property("highlightIndex"),
                "track": layer.property("track"),
                "width": handle.width(),
            }
            _close(handle.property("ringWidth"), 1.7)
            _close(handle.x() + mark.x(), point[0] - diameter / 2)
            _close(handle.y() + mark.y(), point[1] - diameter / 2)
            halo = next(c for c in children if c.objectName() == "editableTrackHalo")
            assert halo.width() == 38
            assert halo.property("ringWidth") == 2
            _close(halo.x() + mark.x(), point[0] - 19)
            _close(halo.y() + mark.y(), point[1] - 19)
            assert handle.property("visible") is True, {
                "layer": layer.property("visible"),
                "mark": mark.property("visible"),
                "editing": layer.property("trackEditing"),
                "handle": handle.property("visible"),
            }
    finally:
        view.close()
        view.releaseResources()


def test_empty_and_edit_off_are_safe(app):
    view, layer = _load([])
    try:
        assert layer.property("selectedIndex") == -1
        assert layer.property("trackVisible") is False
        assert _projected(layer) == []

        assert layer.setProperty("tracks", [_track(editing=False)])
        assert layer.property("selectedIndex") == -1
        assert layer.property("trackVisible") is False

        # A single-point editable track has no polyline to draw, but its one
        # mark stays visible and pickable.
        assert layer.setProperty("tracks", [_track(POINTS[:1])])
        assert layer.property("trackVisible") is True
        assert layer.property("lineVisible") is False
        assert len(_projected(layer)) == 1

        assert layer.setProperty("tracks", [_track(POINTS[:2])])
        assert layer.property("selectedIndex") == 0
        assert layer.property("trackVisible") is True

        tracks = [
            _track(name="first", editing=False),
            _track(name="second"),
            _track(name="third"),
        ]
        assert layer.setProperty("tracks", tracks)
        assert layer.property("selectedIndex") == 1
        assert layer.property("track").get("name") == "second"
        assert layer.setProperty("movePreview", [1, 0, 50.95, -1.5])
        _close_point(_projected(layer)[0], _expected(50.95, -1.5))
    finally:
        view.close()
        view.releaseResources()


def test_scene_layer_and_canvas_share_geometry_and_payload(app):
    view = QQuickView()
    view.setSource(QUrl.fromLocalFile(str(SCENE)))
    try:
        assert not view.errors(), view.errors()
        root = view.rootObject()
        overlay = root.findChild(QQuickItem, "routeOverlay")
        layer = root.findChild(QQuickItem, "editableTrackLayer")
        assert overlay is not None and layer is not None

        assert overlay.setProperty("centerLatitude", CENTER_LAT)
        assert overlay.setProperty("centerLongitude", CENTER_LON)
        assert overlay.setProperty("zoom", ZOOM)
        assert overlay.setProperty("tracks", [_track(active=False)])
        app.processEvents()

        for accessor in ("x", "y", "width", "height"):
            _close(getattr(layer, accessor)(), getattr(overlay, accessor)(), 1e-3)
        assert len(layer.property("tracks")) == 1
        assert layer.property("selectedIndex") == 0
        assert layer.property("trackVisible") is True

        padding = overlay.property("panPadding")
        width = overlay.property("viewportWidth")
        height = overlay.property("viewportHeight")
        for index, point in enumerate(_projected(layer)):
            _close_point(
                point,
                _expected(*POINTS[index], padding=padding, width=width, height=height),
                1e-3,
            )

        assert root.findChild(QObject, "navigationOverlay") is not None
        assert root.findChild(QObject, "chartLayer") is not None
    finally:
        view.close()
        view.releaseResources()


def test_preview_fast_path_rebuilds_when_marks_disagree(app):
    """A single-row preview rewrite needs an intact, same-track mark model.

    When the model still describes another track or a different row count, the
    fast path has to fall back to a full sync instead of rewriting one row and
    leaving the rest stale.
    """
    source = LAYER.read_text()
    start = source.index("function syncPreviewMarks()")
    fast_path = source[start : source.index("function syncMarks()", start)]

    assert "syncedTrack !== track" in fast_path
    assert "markModel.count !== positions.length" in fast_path
    assert fast_path.index("syncMarks()") < fast_path.index("writeMark(")
    assert "syncedTrack = track" in source


def test_hover_fades_the_wide_indicator_in_and_out(app):
    """The edit ring eases from hidden to visible on hover and back on leave."""
    from PySide6.QtTest import QTest

    view, layer = _load([_track(highlight=-1)])
    try:
        view.show()
        app.processEvents()
        halo = _mark_part(layer, 0, "editableTrackHalo")
        _close(halo.opacity(), 0.0)

        assert layer.setProperty("tracks", [_track(highlight=-1, hover=0)])
        QTest.qWait(220)
        assert halo.opacity() > 0.8, halo.opacity()

        assert layer.setProperty("tracks", [_track(highlight=-1)])
        QTest.qWait(220)
        assert halo.opacity() < 0.05, halo.opacity()
        # The mark itself stays drawn; only the ring fades.
        assert _mark_part(layer, 0, "editableTrackHandle").isVisible()
    finally:
        view.close()
        view.releaseResources()


def test_drag_preview_lights_the_moved_handle(app):
    """Dragging keeps the previewed row's indicator lit like a hover."""
    from PySide6.QtTest import QTest

    view, layer = _load([_track(highlight=-1)], preview=[0, 2, 50.79, -1.24])
    try:
        view.show()
        app.processEvents()
        assert layer.property("previewIndex") == 2
        QTest.qWait(220)
        assert _mark_part(layer, 2, "editableTrackHalo").opacity() > 0.8
        _close(_mark_part(layer, 3, "editableTrackHalo").opacity(), 0.0)
    finally:
        view.close()
        view.releaseResources()


def test_marks_colour_port_starboard_and_neutral_roundings(app):
    """Port is red, starboard green, sail-to/gate/unset are blue."""
    from PySide6.QtGui import QColor

    roundings = [0, 1, 2, 3, None]
    view, layer = _load([_track(roundings=roundings, highlight=-1)])
    try:
        view.show()
        app.processEvents()
        expected = [
            QColor("#d43a1e"),
            QColor("#258054"),
            QColor("#2f6fbd"),
            QColor("#2f6fbd"),
            QColor("#2f6fbd"),
        ]
        for index, wanted in enumerate(expected):
            handle = _mark_part(layer, index, "editableTrackHandle")
            halo = _mark_part(layer, index, "editableTrackHalo")
            assert handle.property("ringColor").getRgb() == wanted.getRgb(), (
                index,
                handle.property("ringColor").getRgb(),
            )
            assert halo.property("ringColor").getRgb() == wanted.getRgb()
    finally:
        view.close()
        view.releaseResources()


def test_rounding_change_recolours_retained_marks(app):
    """Rounding can change without moving points; the fast path must not skip it."""
    from PySide6.QtGui import QColor

    port = QColor("#d43a1e")
    view, layer = _load([_track(roundings=[2] * len(POINTS), highlight=-1)])
    try:
        view.show()
        app.processEvents()
        before = _named_items(layer)
        assert (
            _mark_part(layer, 0, "editableTrackHandle").property("ringColor").getRgb()
            == QColor("#2f6fbd").getRgb()
        )

        assert layer.setProperty("tracks", [_track(roundings=[0] * len(POINTS))])
        app.processEvents()
        after = _named_items(layer)
        for index in range(len(POINTS)):
            name = "editableTrackMark%d" % index
            assert before[name] is after[name]
        assert (
            _mark_part(layer, 0, "editableTrackHandle").property("ringColor").getRgb()
            == port.getRgb()
        )
    finally:
        view.close()
        view.releaseResources()
