from datetime import datetime
from types import SimpleNamespace

import pytest

from sailworks.core.geo.collection import CollectionWithActiveElement
from sailworks.core.geo.track import Track
from sailworks.qt.gribviewport import _viewport_widget_class
from sailworks.qt.viewportmap import ViewportMap


def _host():
    manager = CollectionWithActiveElement.__new__(CollectionWithActiveElement)
    manager.elements = []
    manager.activeElement = None
    manager.revision = 0
    manager.save = lambda: None
    host = SimpleNamespace(
        _view_state=SimpleNamespace(
            selected_route_point=-1, points=[], route_plan=[], route_preview=[]
        ),
        controller=SimpleNamespace(
            core=SimpleNamespace(
                trackManager=manager,
                routingManager=SimpleNamespace(get_by_name=lambda name: None),
                poiManager=[],
            ),
            chart_errors=[],
        ),
        navigation=SimpleNamespace(
            hl_routing=None,
            hl_poi=None,
            hl_track_item=None,
            edit_track=None,
            route_overlay_data=lambda timestamp: [],
        ),
        time_control=SimpleNamespace(time=datetime(2024, 1, 1)),
        ais_visible=False,
        isochrone_frame=SimpleNamespace(isochrones=[], path=[], exclusions=[]),
        dashboard_visible=False,
        move_callback=None,
    )
    return host, manager


def _build(host):
    return ViewportMap.viewport_route_overlay(host)


def test_race_payload_reused_and_published_only_on_change():
    host, manager = _host()
    track = Track("boat", [(48, -123), (49, -122)], race_track=True, color=(1, 0, 0))
    manager.append(track)
    calls = []
    camera = SimpleNamespace(
        center_lat=48, center_lon=-123, zoom=9, width=900, height=700
    )
    widget = SimpleNamespace(
        _route_overlay=SimpleNamespace(
            setProperty=lambda name, value: calls.append((name, value))
        ),
        _camera=camera,
        _route_pan_window=None,
        _route_content_key=None,
        devicePixelRatio=lambda: 1.0,
        _place_route_overlay=lambda *args, **kwargs: None,
    )
    publish = _viewport_widget_class().set_route_overlay
    first = _build(host)
    assert first["race_tracks"] == [
        {
            "name": "boat",
            "points": [[48.0, -123.0], [49.0, -122.0]],
            "color": [1.0, 0.0, 0.0],
            "active": False,
        }
    ]
    publish(widget, first)
    assert ("raceTracks", first["race_tracks"]) in calls
    calls.clear()
    camera.center_lon -= 0.01
    second = _build(host)
    assert second["race_tracks"] is first["race_tracks"]
    publish(widget, second)
    assert calls == []
    manager.set_active(track, save=False)
    third = _build(host)
    assert third["race_tracks"][0]["active"] is True
    publish(widget, third)
    assert ("raceTracks", third["race_tracks"]) in calls
    calls.clear()
    third["track_position"] = [48.1, -123.1]
    publish(widget, third)
    assert ("trackPosition", [48.1, -123.1]) in calls
    assert not any(name == "raceTracks" for name, _ in calls)


def test_external_overlays_still_detect_in_place_race_geometry_edits():
    host, manager = _host()
    manager.append(Track("boat", [(1, 2)], race_track=True))
    overlay = _build(host)
    overlay.pop("_race_tracks_key", None)
    calls = []
    widget = SimpleNamespace(
        _route_overlay=SimpleNamespace(
            setProperty=lambda name, value: calls.append((name, value))
        ),
        _camera=SimpleNamespace(
            center_lat=1, center_lon=2, zoom=9, width=900, height=700
        ),
        _route_pan_window=None,
        _route_content_key=None,
        devicePixelRatio=lambda: 1.0,
        _place_route_overlay=lambda *args, **kwargs: None,
    )
    publish = _viewport_widget_class().set_route_overlay
    publish(widget, overlay)
    calls.clear()
    overlay["race_tracks"][0]["points"][0][0] = 5
    publish(widget, overlay)
    assert ("raceTracks", overlay["race_tracks"]) in calls


@pytest.mark.parametrize(
    "change",
    [
        "add",
        "remove",
        "reorder",
        "append",
        "remove_by_names",
        "point",
        "visible",
        "active",
        "name",
        "color",
    ],
)
def test_race_payload_invalidated(change):
    host, manager = _host()
    first = Track("first", [(1, 2)], race_track=True, color=[1, 0, 0])
    second = Track("second", [(3, 4)], race_track=True, color=[0, 1, 0])
    manager.append_many([first, second], save=False)
    old = _build(host)["race_tracks"]
    if change == "add":
        manager.append(Track("third", [(5, 6)], race_track=True))
    elif change == "remove":
        manager.remove(first)
    elif change == "reorder":
        manager[0], manager[1] = manager[1], manager[0]
    elif change == "append":
        first.points.append((7, 8))
    elif change == "remove_by_names":
        manager.remove_by_names(["first"], save=False)
    elif change == "point":
        first.points[0] = (7, 8)
    elif change == "visible":
        first.visible = False
    elif change == "active":
        manager.set_active(first, save=False)
    elif change == "name":
        first.name = "renamed"
    elif change == "color":
        first.color[0] = 0.5
    new = _build(host)["race_tracks"]
    assert new is not old
    assert new != old


def test_explicit_touch_invalidates_in_place_coordinate_edit():
    host, manager = _host()
    track = Track("boat", [[1, 2]], race_track=True)
    manager.append(track)
    old = _build(host)["race_tracks"]
    track.points[0][0] = 5
    track.touch()
    new = _build(host)["race_tracks"]
    assert new is not old
    assert new[0]["points"] == [[5.0, 2.0]]
