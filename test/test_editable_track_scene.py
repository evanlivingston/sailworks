"""Scene wiring: only the selected track moves from Canvas to Shapes."""

from pathlib import Path

SCENE = (
    Path(__file__).resolve().parents[1] / "sailworks/qt/shaders/ViewportChartScene.qml"
)


def _block(source, marker):
    start = source.rindex("{", 0, source.index(marker))
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index]
    raise AssertionError("unbalanced block for %s" % marker)


def test_scene_instantiates_the_layer_beside_the_canvas():
    content = _block(SCENE.read_text(), 'objectName: "mapContent"')
    assert content.index('objectName: "routeOverlay"') < content.index(
        'objectName: "editableTrackLayer"'
    )
    props = (
        "x y width height tracks movePreview centerLatitude "
        "centerLongitude zoom panPadding viewportWidth viewportHeight"
    )
    for prop in props.split():
        assert "%s: routeOverlayCanvas.%s" % (prop, prop) in content, prop


def test_canvas_skips_only_the_retained_track():
    canvas = _block(SCENE.read_text(), "id: routeOverlayCanvas")
    assert "function editableTrackIndex()" in canvas
    assert "const retainedTrack = editableTrackIndex()" in canvas
    loop = canvas[canvas.index("const retainedTrack") :]
    assert loop.index("if (t === retainedTrack)") < loop.index(
        "context.fillText(track.name"
    )
    assert "context.setLineDash([8, 4])" in canvas
    assert "track.editing" in canvas
    assert "onMovePreviewChanged:" in canvas
    assert "movePreview[0] !== editableTrackIndex()" in canvas


def test_track_payload_publishes_roundings_parallel_to_points():
    """The QML layer colours marks from a per-point rounding list."""
    from types import SimpleNamespace

    from sailworks.core.geo.elementmultipoint import ElementMultiPoint
    from sailworks.core.geo.rounding import ROUNDING_PORT, ROUNDING_STARBOARD
    from sailworks.qt.viewportmap import _track_payload

    points = [[50.803, -1.430], [50.819, -1.380], [50.826, -1.315]]
    track = ElementMultiPoint("rounding track", points)
    assert track.set_point_rounding(0, ROUNDING_PORT)
    assert track.set_point_rounding(1, ROUNDING_STARBOARD)

    host = SimpleNamespace(
        controller=SimpleNamespace(
            core=SimpleNamespace(
                trackManager=SimpleNamespace(is_active=lambda candidate: True)
            )
        ),
        navigation=SimpleNamespace(edit_track=track, hl_track_item=0, hl_track_hover=1),
    )
    payload = _track_payload(track, host)
    assert payload["points"] == [list(point) for point in points]
    assert payload["roundings"] == [ROUNDING_PORT, ROUNDING_STARBOARD, None]
    assert len(payload["roundings"]) == len(payload["points"])
    assert payload["editing"] is True
    assert payload["hover"] == 1

    # A rounding edit must invalidate the cached payload the scene retains.
    revision = track.revision
    assert track.set_point_rounding(2, ROUNDING_PORT)
    assert track.revision > revision
