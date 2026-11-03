"""Regression coverage for retained route/navigation pan placement (#123)."""

from pathlib import Path
from datetime import datetime, timezone
from types import MethodType, SimpleNamespace

from sailworks.qt.gribviewport import ViewportCamera, _viewport_widget_class
from sailworks.core.mapviewport import MapViewport
from sailworks.qt.gribviewportchart import ChartCamera, MARGIN, chart_placement
from sailworks.qt.viewporttransform import PanReference


def _camera(center_lon=-123.1, zoom=9.0, width=900, height=700):
    return SimpleNamespace(
        center_lat=48.3,
        center_lon=center_lon,
        zoom=zoom,
        width=width,
        height=height,
    )


def _overlay(route_plan=None):
    return {
        "route_plan": route_plan or [[48.3, -123.1], [48.4, -123.0]],
        "route_preview": [],
        "selected_point": [],
        "track_points": [],
        "track_position": [],
        "track_heading": None,
        "track_awa": None,
        "caption": "",
        "dashboard_visible": True,
    }


def test_route_camera_pan_moves_retained_canvas_without_republishing_content():
    calls = []
    item = SimpleNamespace(setProperty=lambda name, value: calls.append((name, value)))
    widget = SimpleNamespace(
        _route_overlay=item,
        _camera=_camera(),
        _route_pan_window=None,
        _route_content_key=None,
        devicePixelRatio=lambda: 1.0,
    )
    widget_class = _viewport_widget_class()
    widget._place_route_overlay = MethodType(widget_class._place_route_overlay, widget)
    set_route_overlay = widget_class.set_route_overlay

    set_route_overlay(widget, _overlay())
    calls.clear()
    widget._camera = _camera(center_lon=-123.11)
    set_route_overlay(widget, _overlay())

    names = [name for name, _ in calls]
    assert names == ["panX", "panY"]
    assert calls[0][1] != 0.0


def test_set_camera_moves_route_canvas_in_camera_pass_without_content_callback():
    calls = []
    route_positions = {}
    item = SimpleNamespace(
        setProperty=lambda name, value: (
            calls.append((name, value)),
            route_positions.__setitem__(name, value),
        )
    )
    widget_class = _viewport_widget_class()
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    initial_camera = ViewportCamera(48.3, -123.1, 9.0, 900, 700, stamp)
    chart_positions = {}
    chart_item = SimpleNamespace(
        setProperty=lambda name, value: chart_positions.__setitem__(name, value)
    )
    chart_viewport = MapViewport(ChartCamera(initial_camera, 1.0), MARGIN, lambda: None)
    widget = SimpleNamespace(
        _route_overlay=item,
        _camera=initial_camera,
        _route_pan_window=None,
        _route_content_key=None,
        _chart=chart_item,
        _chart_viewport=chart_viewport,
        _chart_composition_camera=initial_camera,
        _chart_composition_dpr=1.0,
        _chart_model=None,
        _disposed=False,
        _marine_layer=SimpleNamespace(set_camera=lambda camera: None),
        _icoads_layer=SimpleNamespace(set_camera=lambda camera: None),
        _publication_manager=SimpleNamespace(
            reference_camera=None,
            set_reference_camera=lambda camera: None,
        ),
        _native_publication_dpr=lambda: 1.0,
        _native_weather_fields=False,
        _native_active_camera=None,
        _native_pending_camera=None,
        _native_active_dpr=None,
        _retire_barb_anchor_frame=lambda *args, **kwargs: None,
        _maybe_arm_barb_zoom_preview=lambda *args, **kwargs: None,
        _barb_zoom_preview=None,
        _camera_dpr=1.0,
        devicePixelRatio=lambda: 1.0,
        _place_chart=None,
        _update_shared_map_pan=lambda camera: False,
        _place_route_overlay=lambda camera: widget_class._place_route_overlay(
            widget, camera
        ),
        _publish_streamlines=lambda camera: None,
        _wind_style="streamlines",
        _set_weather_visible=lambda visible: None,
        cameraChanged=SimpleNamespace(emit=lambda camera: None),
        _state="ready",
        _presentation_spans=[],
    )
    widget._place_chart = MethodType(widget_class._place_chart, widget)
    widget._update_shared_map_pan = MethodType(
        widget_class._update_shared_map_pan, widget
    )
    widget._map_content = None
    widget._place_route_overlay = MethodType(widget_class._place_route_overlay, widget)
    widget._place_chart()
    widget_class.set_route_overlay(widget, _overlay())
    calls.clear()

    initial_x = chart_positions["imageX"]
    initial_y = chart_positions["imageY"]
    for lon in (-123.1003, -123.1008, -123.1014):
        camera = ViewportCamera(48.3, lon, 9.0, 900, 700, stamp)
        calls.clear()
        widget_class.set_camera(widget, camera)
        chart_dx = chart_positions["imageX"] - initial_x
        chart_dy = chart_positions["imageY"] - initial_y
        assert chart_dx != 0.0
        assert chart_dx != round(chart_dx)
        assert route_positions["panX"] != 0.0
        assert route_positions["panX"] != round(route_positions["panX"])
        assert abs(chart_dx - route_positions["panX"]) < 0.001
        assert abs(chart_dy - route_positions["panY"]) < 0.001

    # A large camera change exhausts route padding; placement must rebase the
    # retained route surface even when there is no presenter/content callback.
    calls.clear()
    widget_class.set_camera(widget, ViewportCamera(48.3, -119.0, 9.0, 900, 700, stamp))
    names = [name for name, _ in calls]
    assert "centerLongitude" in names
    assert ("panX", 0.0) in calls
    assert ("panY", 0.0) in calls


def test_route_overlay_publishes_race_tracks_to_the_qml_layer():
    calls = []
    item = SimpleNamespace(setProperty=lambda name, value: calls.append((name, value)))
    widget = SimpleNamespace(
        _route_overlay=item,
        _camera=_camera(),
        _route_pan_window=None,
        _route_content_key=None,
        devicePixelRatio=lambda: 1.0,
    )
    widget_class = _viewport_widget_class()
    widget._place_route_overlay = MethodType(widget_class._place_route_overlay, widget)
    overlay = _overlay()
    overlay["race_tracks"] = [
        {
            "name": "Race A",
            "points": [[48.3, -123.1], [48.4, -123.0]],
            "color": [1.0, 0.0, 0.0],
            "active": True,
        }
    ]
    widget_class.set_route_overlay(widget, overlay)
    assert ("raceTracks", overlay["race_tracks"]) in calls


def test_route_overlay_publishes_navigation_tracks_to_the_qml_layer():
    calls = []
    item = SimpleNamespace(setProperty=lambda name, value: calls.append((name, value)))
    widget = SimpleNamespace(
        _route_overlay=item,
        _camera=_camera(),
        _route_pan_window=None,
        _route_content_key=None,
        devicePixelRatio=lambda: 1.0,
    )
    widget_class = _viewport_widget_class()
    widget._place_route_overlay = MethodType(widget_class._place_route_overlay, widget)
    overlay = _overlay()
    overlay["tracks"] = [
        {
            "name": "Track A",
            "points": [[48.3, -123.1], [48.4, -123.0]],
            "active": True,
            "highlight": -1,
            "editing": False,
        }
    ]
    widget_class.set_route_overlay(widget, overlay)
    assert ("tracks", overlay["tracks"]) in calls


def test_route_overlay_publishes_pois_to_the_qml_layer():
    calls = []
    item = SimpleNamespace(setProperty=lambda name, value: calls.append((name, value)))
    widget = SimpleNamespace(
        _route_overlay=item,
        _camera=_camera(),
        _route_pan_window=None,
        _route_content_key=None,
        devicePixelRatio=lambda: 1.0,
    )
    widget_class = _viewport_widget_class()
    widget._place_route_overlay = MethodType(widget_class._place_route_overlay, widget)
    overlay = _overlay()
    overlay["pois"] = [[48.3, -123.1, "Harbor", False]]
    widget_class.set_route_overlay(widget, overlay)
    assert ("pois", [[48.3, -123.1, "Harbor", False]]) in calls


def test_route_content_mutation_republishes_and_rebases_canvas():
    calls = []
    item = SimpleNamespace(setProperty=lambda name, value: calls.append((name, value)))
    widget = SimpleNamespace(
        _route_overlay=item,
        _camera=_camera(),
        _route_pan_window=None,
        _route_content_key=None,
        devicePixelRatio=lambda: 1.0,
    )
    widget_class = _viewport_widget_class()
    widget._place_route_overlay = MethodType(widget_class._place_route_overlay, widget)
    set_route_overlay = widget_class.set_route_overlay
    set_route_overlay(widget, _overlay())
    calls.clear()

    set_route_overlay(widget, _overlay([[48.3, -123.1], [48.5, -122.9]]))

    names = [name for name, _ in calls]
    assert "routePlan" in names
    assert "centerLatitude" in names
    assert ("panX", 0.0) in calls
    assert ("panY", 0.0) in calls


def test_route_canvas_rebases_after_declared_padding_is_exhausted():
    calls = []
    item = SimpleNamespace(setProperty=lambda name, value: calls.append((name, value)))
    widget = SimpleNamespace(
        _route_overlay=item,
        _camera=_camera(),
        _route_pan_window=None,
        _route_content_key=None,
        devicePixelRatio=lambda: 1.0,
    )
    widget_class = _viewport_widget_class()
    widget._place_route_overlay = MethodType(widget_class._place_route_overlay, widget)
    set_route_overlay = widget_class.set_route_overlay
    set_route_overlay(widget, _overlay())
    calls.clear()
    widget._camera = _camera(center_lon=-119.0)

    set_route_overlay(widget, _overlay())

    names = [name for name, _ in calls]
    assert "centerLongitude" in names
    assert ("panX", 0.0) in calls
    assert ("panY", 0.0) in calls


def test_padded_basemap_targets_keep_current_camera_placement_without_shared_parent():
    viewport_class = _viewport_widget_class()
    target = SimpleNamespace(
        x=lambda: -24.0,
        y=lambda: 18.0,
        width=lambda: 960.0,
        height=lambda: 760.0,
    )
    image = SimpleNamespace(cacheKey=lambda: 7)

    for method_name, item_name, provider_name, key_name in (
        ("set_base", "_base", "_base_provider", "_base_key"),
        (
            "set_environment",
            "_environment",
            "_environment_provider",
            "_environment_key",
        ),
    ):
        calls = []
        item = SimpleNamespace(
            setProperty=lambda name, value: calls.append((name, value)),
            setVisible=lambda visible: None,
        )
        widget = SimpleNamespace(
            **{
                item_name: item,
                provider_name: SimpleNamespace(
                    publish=lambda key, image: "image://retained/1"
                ),
                key_name: None,
            }
        )
        widget._place_retained_image = MethodType(
            viewport_class._place_retained_image, widget
        )

        getattr(viewport_class, method_name)(widget, image, target)

        assert [entry for entry in calls if entry[0].startswith("image")][-4:] == [
            ("imageX", -24.0),
            ("imageY", 18.0),
            ("imageWidth", 960.0),
            ("imageHeight", 760.0),
        ]


def test_navigation_target_is_parent_local_across_shared_pan_and_rebase():
    from PySide6.QtCore import QRectF

    widget_class = _viewport_widget_class()
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    camera = ViewportCamera(48.3, -123.1, 9.0, 900, 700, stamp)
    writes = []
    published = []
    item = SimpleNamespace(setProperty=lambda name, value: writes.append((name, value)))
    widget = SimpleNamespace(
        _navigation_overlay=item,
        _navigation_provider=SimpleNamespace(
            publish=lambda key, image: published.append(key) or f"image://nav/{key}"
        ),
        _navigation_key=None,
        _navigation_target=None,
        _camera=camera,
        _map_content=object(),
        _map_pan_reference=PanReference.from_camera(camera),
    )
    image = SimpleNamespace(cacheKey=lambda: 1)
    target = QRectF(-40.0, -30.0, 980.0, 760.0)
    widget_class.set_navigation_overlay(widget, image, target)
    writes.clear()

    moved = ViewportCamera(48.3, -123.101, 9.0, 900, 700, stamp)
    dx, dy = widget._map_pan_reference.translation_to(moved)
    widget._camera = moved
    widget_class.set_navigation_overlay(
        widget,
        image,
        QRectF(target.x() + dx, target.y() + dy, target.width(), target.height()),
    )
    assert widget._navigation_target == (-40.0, -30.0, 980.0, 760.0)
    assert not [
        entry
        for entry in writes
        if entry[0] in ("imageX", "imageY", "imageWidth", "imageHeight")
    ]
    assert published == [1]

    # Rebase the parent at the current camera; the same screen-space target
    # now arrives in the new parent-local coordinate system.
    screen_x, screen_y = target.x() + dx, target.y() + dy
    widget._map_pan_reference = PanReference.from_camera(moved)
    widget_class.set_navigation_overlay(
        widget, image, QRectF(screen_x, screen_y, 980.0, 760.0)
    )
    assert widget._navigation_target == (screen_x, screen_y, 980.0, 760.0)

    image.cacheKey = lambda: 2
    widget_class.set_navigation_overlay(
        widget, image, QRectF(screen_x, screen_y, 980.0, 760.0)
    )
    assert published == [1, 2]


def test_navigation_rebase_compensates_retained_image_without_republication():
    widget_class = _viewport_widget_class()
    properties = {}
    widget = SimpleNamespace(
        _navigation_overlay=SimpleNamespace(
            setProperty=lambda name, value: properties.__setitem__(name, value)
        ),
        _navigation_target=(-40.0, -30.0, 980.0, 760.0),
        _navigation_key=7,
    )
    widget_class._rebase_retained_imagery(widget, 12.25, -7.5)
    assert widget._navigation_target == (-27.75, -37.5, 980.0, 760.0)
    assert properties == {"imageX": -27.75, "imageY": -37.5}
    assert widget._navigation_key == 7


def test_navigation_target_legacy_fallback_and_camera_adoption_invalidation():
    from PySide6.QtCore import QRectF
    from sailworks.qt.viewportmap import ViewportMap

    widget_class = _viewport_widget_class()
    calls = []
    item = SimpleNamespace(setProperty=lambda name, value: calls.append((name, value)))
    widget = SimpleNamespace(
        _navigation_overlay=item,
        _navigation_provider=SimpleNamespace(
            publish=lambda key, image: "image://nav/1"
        ),
        _navigation_key=None,
        _navigation_target=None,
        _camera=_camera(),
        _map_content=None,
        _map_pan_reference=None,
    )
    widget_class.set_navigation_overlay(
        widget, SimpleNamespace(cacheKey=lambda: 1), QRectF(3, 4, 50, 60)
    )
    assert calls[-4:] == [
        ("imageX", 3.0),
        ("imageY", 4.0),
        ("imageWidth", 50.0),
        ("imageHeight", 60.0),
    ]

    host = SimpleNamespace(
        camera_state=SimpleNamespace(center_lat=48.3, center_lon=-123.1, zoom=9.0),
        invalidate_navigation=lambda: calls.append(("invalidate", None)),
        _cancel_cursor=lambda: None,
        view_changed=SimpleNamespace(emit=lambda: None),
        _weather_presenter=None,
        _cursor_screen_position=None,
        _cursor_refresh_timer=None,
    )
    ViewportMap.adopt_viewport_camera(
        host, SimpleNamespace(center_lat=48.3, center_lon=-123.11, zoom=9.0)
    )
    assert ("invalidate", None) not in calls
    ViewportMap.adopt_viewport_camera(
        host, SimpleNamespace(center_lat=48.3, center_lon=-123.11, zoom=10.0)
    )
    assert calls[-1] == ("invalidate", None)


def test_chart_image_uses_its_composition_camera_for_pan_placement():
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    composition_camera = ViewportCamera(48.3, -123.1, 9.0, 900, 700, stamp)
    current_camera = ViewportCamera(48.3, -123.11, 9.0, 900, 700, stamp)
    viewport = MapViewport(ChartCamera(composition_camera, 1.0), MARGIN, lambda: None)
    calls = []
    chart_item = SimpleNamespace(
        setProperty=lambda name, value: calls.append((name, value))
    )
    widget = SimpleNamespace(
        _chart=chart_item,
        _chart_viewport=viewport,
        _camera=current_camera,
        _chart_composition_camera=composition_camera,
        _chart_composition_dpr=1.0,
        devicePixelRatio=lambda: 1.0,
    )
    base = chart_placement(viewport, composition_camera)
    _viewport_widget_class()._place_chart(widget)
    dx, dy = PanReference.from_camera(composition_camera).translation_to(current_camera)
    assert calls == [
        ("imageX", base[0] + dx),
        ("imageY", base[1] + dy),
        ("imageWidth", base[2]),
        ("imageHeight", base[3]),
    ]


def test_route_canvas_declares_padded_surface_and_does_not_paint_on_pan():
    qml = (
        Path(__file__).parents[1]
        / "sailworks"
        / "qt"
        / "shaders"
        / "ViewportChartScene.qml"
    ).read_text()
    route = qml.split('objectName: "routeOverlay"', 1)[1].split(
        'objectName: "dashboardOverlay"', 1
    )[0]
    assert "width: viewportWidth + 2 * panPadding" in route
    assert "height: viewportHeight + 2 * panPadding" in route
    assert "onPanXChanged" not in route
    assert "onPanYChanged" not in route
    assert "onCenterLatitudeChanged: requestPaint()" in route


def test_shared_scene_pan_translates_parent_and_reanchors_retained_children():
    widget_class = _viewport_widget_class()
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    camera = ViewportCamera(48.3, -123.1, 9.0, 900, 700, stamp)
    writes = {"map": [], "chart": [], "route": []}
    positions = {"map": {}, "chart": {}, "route": {}}

    def recorder(bucket):
        def record(name, value):
            writes[bucket].append((name, value))
            positions[bucket][name] = value

        return record

    widget = SimpleNamespace(
        _map_content=SimpleNamespace(setProperty=recorder("map")),
        _map_pan_reference=None,
        _map_pan_window=None,
        _route_overlay=SimpleNamespace(setProperty=recorder("route")),
        _route_pan_window=None,
        _route_content_key=None,
        _chart=SimpleNamespace(setProperty=recorder("chart")),
        _chart_viewport=MapViewport(ChartCamera(camera, 1.0), MARGIN, lambda: None),
        _chart_composition_camera=camera,
        _chart_composition_dpr=1.0,
        _camera=camera,
        devicePixelRatio=lambda: 1.0,
    )
    widget._update_shared_map_pan = MethodType(
        widget_class._update_shared_map_pan, widget
    )
    widget._place_chart = MethodType(widget_class._place_chart, widget)
    widget._place_route_overlay = MethodType(widget_class._place_route_overlay, widget)
    widget._rebase_retained_imagery = MethodType(
        widget_class._rebase_retained_imagery, widget
    )
    widget._update_shared_map_pan(camera)
    widget._place_chart()
    widget_class.set_route_overlay(widget, _overlay())
    writes = {key: [] for key in writes}

    initial_base = chart_placement(widget._chart_viewport, camera)
    for lon in (-123.1003, -123.1007, -123.1014):
        moved = ViewportCamera(48.3, lon, 9.0, 900, 700, stamp)
        widget._camera = moved
        assert widget._update_shared_map_pan(moved) is False
        widget._place_route_overlay(moved)
        assert [name for name, _ in writes["map"][-2:]] == ["x", "y"]
        assert writes["chart"] == []
        assert writes["route"] == []

        map_x = writes["map"][-2][1]
        map_y = writes["map"][-1][1]
        expected_dx, expected_dy = PanReference.from_camera(camera).translation_to(
            moved
        )
        # The chart image retains its composition placement; mapContent supplies
        # the shared translation, yielding the same effective placement.
        assert (
            abs(positions["chart"]["imageX"] + map_x - (initial_base[0] + expected_dx))
            < 0.001
        )
        assert (
            abs(positions["chart"]["imageY"] + map_y - (initial_base[1] + expected_dy))
            < 0.001
        )
        assert abs(positions["route"]["panX"] + map_x - expected_dx) < 0.001
        assert abs(positions["route"]["panY"] + map_y - expected_dy) < 0.001
        assert expected_dx != round(expected_dx)

    # Content arriving during the translated pan is anchored locally to the
    # shared scene's current camera, not the previous composition camera.
    writes = {key: [] for key in writes}
    widget_class.set_route_overlay(widget, _overlay([[48.3, -123.1], [48.5, -122.9]]))
    parent_x = positions["map"]["x"]
    parent_y = positions["map"]["y"]
    assert writes["route"][-2:] == [("panX", -parent_x), ("panY", -parent_y)]
    assert abs(positions["route"]["panX"] + parent_x) < 0.001
    assert abs(positions["route"]["panY"] + parent_y) < 0.001

    # Exhausting padding rebases the route reference at the current parent.
    far = ViewportCamera(48.3, -119.0, 9.0, 900, 700, stamp)
    widget._camera = far
    widget._update_shared_map_pan(far)
    widget._place_route_overlay(far)
    assert any(name == "centerLongitude" for name, _ in writes["route"])
    parent_x = positions["map"]["x"]
    assert abs(positions["route"]["panX"] + parent_x) < 0.001

    # Zoom is not a pan: the chart gets its scaled preview and route is reset.
    zoomed = ViewportCamera(48.3, -119.0, 10.0, 900, 700, stamp)
    widget._camera = zoomed
    writes = {key: [] for key in writes}
    widget._update_shared_map_pan(zoomed)
    assert widget._map_anchor_camera is zoomed
    widget._place_chart()
    expected = chart_placement(widget._chart_viewport, zoomed)
    assert positions["chart"]["imageWidth"] == expected[2]
    assert positions["chart"]["imageHeight"] == expected[3]
    widget._place_route_overlay(zoomed)
    assert any(name == "centerLongitude" for name, _ in writes["route"])
    assert positions["route"]["panX"] == -positions["map"]["x"]

    qml = (
        Path(__file__).parents[1] / "sailworks/qt/shaders/ViewportChartScene.qml"
    ).read_text()
    assert 'objectName: "chartLayer"' in qml and 'objectName: "routeOverlay"' in qml
    assert 'objectName: "mapContent"' in qml
    assert "x: -mapContent.x" in qml and "y: -mapContent.y" in qml
    assert (
        qml.index('objectName: "weatherBand"')
        < qml.index('objectName: "navigationOverlay"')
        < qml.index('objectName: "routeOverlay"')
    )
    assert (
        '            }\n            ChartLayer {\n                objectName: "navigationOverlay"'
        in qml
    )
    assert 'objectName: "dashboardOverlay"' in qml
