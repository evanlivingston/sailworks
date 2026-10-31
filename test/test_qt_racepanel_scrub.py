import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import timezone
from types import SimpleNamespace
from unittest.mock import Mock

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QSlider, QWidget

from gweatherrouting.core.race import parse_race
from gweatherrouting.core.timecontrol import TimeControl
from gweatherrouting.qt.mapoverlays import RetainedOverlayMixin
from gweatherrouting.qt.window import PreviewWindow


def _sample_race():
    return parse_race(
        {
            "race_id": "scrub-test",
            "name": "Scrub test",
            "start_time": "2026-05-16T03:00:00Z",
            "end_time": "2026-05-16T04:00:00Z",
            "teams": [{"id": 1, "name": "Boat"}],
            "positions": [
                {
                    "id": 1,
                    "moments": [
                        {"gpsAt": "2026-05-16T03:00:00Z", "lat": 48.0, "lon": -123.0},
                        {"gpsAt": "2026-05-16T04:00:00Z", "lat": 48.1, "lon": -123.1},
                    ],
                }
            ],
        }
    )


def test_boats_at_current_time_follows_scrubbed_time(qtbot):
    from gweatherrouting.qt import racepanel

    class TrackManager(list):
        active = None

        def get_unique_name(self, name):
            return name

        def set_active(self, track):
            self.active = track

    manager = TrackManager()
    controller = SimpleNamespace(
        time_control=TimeControl(), core=SimpleNamespace(trackManager=manager)
    )
    panel = racepanel.RacesPanel(controller)
    qtbot.addWidget(panel)
    race = _sample_race()
    panel._add_race(race)

    controller.time_control.set_time(race.start_time)
    positions = panel.boats_at_current_time()
    assert 1 in positions
    assert positions[1].latitude == 48.0

    controller.time_control.set_time(race.end_time)
    positions = panel.boats_at_current_time()
    assert positions[1].latitude == 48.1


def test_window_race_time_changed_publishes_all_boat_positions(qtbot):
    race = _sample_race()
    track_name = f"{race.name} - Boat"
    race_panel = SimpleNamespace(race=race, race_track_times={})
    race_panel.race_track_times[track_name] = [
        p.timestamp.replace(tzinfo=timezone.utc).timestamp()
        for p in race.boat(1).positions
    ]

    controller = SimpleNamespace(
        time_control=TimeControl(),
        core=SimpleNamespace(
            trackManager=SimpleNamespace(
                get_active=lambda: SimpleNamespace(name=track_name)
            )
        ),
    )
    canvas_map = Mock()
    canvas_log = Mock()
    window = SimpleNamespace(
        controller=controller,
        race_panel=race_panel,
        map=canvas_map,
        log_map=canvas_log,
        _race_track_times=dict(race_panel.race_track_times),
        _race_boat_positions=[],
    )
    window._race_time_changed = lambda when: PreviewWindow._race_time_changed(
        window, when
    )

    when = race.start_time
    controller.time_control.set_time(when)
    window._race_time_changed(when)

    assert window._race_boat_positions == [[48.0, -123.0]]
    for canvas in (canvas_map, canvas_log):
        assert canvas.race_boat_positions == [[48.0, -123.0]]
        assert canvas.race_positions == [[48.0, -123.0]]
        canvas.invalidate_navigation.assert_called()
        canvas.update.assert_called()

    later = race.end_time
    controller.time_control.set_time(later)
    window._race_time_changed(later)
    assert window._race_boat_positions == [[48.1, -123.1]]
    for canvas in (canvas_map, canvas_log):
        assert canvas.race_boat_positions == [[48.1, -123.1]]


def test_window_race_time_changed_clears_stale_markers(qtbot):
    race = _sample_race()
    canvas_map = Mock()
    canvas_log = Mock()
    controller = SimpleNamespace(
        time_control=TimeControl(),
        core=SimpleNamespace(trackManager=SimpleNamespace(get_active=lambda: None)),
    )
    window = SimpleNamespace(
        controller=controller,
        race_panel=SimpleNamespace(race=None, race_track_times={}),
        map=canvas_map,
        log_map=canvas_log,
        _race_track_times={},
        _race_boat_positions=[[48.0, -123.0]],
    )
    window._race_time_changed = lambda when: PreviewWindow._race_time_changed(
        window, when
    )

    window._race_time_changed(race.start_time)
    assert window._race_boat_positions == []
    for canvas in (canvas_map, canvas_log):
        assert canvas.race_boat_positions == []
        assert canvas.race_positions == []


def _marker_centroid(image):
    xs = []
    ys = []
    for y in range(image.height()):
        for x in range(image.width()):
            color = QColor(image.pixel(x, y))
            if color.alpha() > 0 and color.red() > 200 and color.green() < 120:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def test_navigation_overlay_drives_race_scrub_markers(qtbot):
    race = parse_race(
        {
            "race_id": "overlay-scrub",
            "name": "Overlay scrub",
            "start_time": "2026-05-16T03:00:00Z",
            "end_time": "2026-05-16T04:00:00Z",
            "teams": [{"id": 1, "name": "Boat"}],
            "positions": [
                {
                    "id": 1,
                    "moments": [
                        {"gpsAt": "2026-05-16T03:00:00Z", "lat": 48.0, "lon": -123.0},
                        {"gpsAt": "2026-05-16T04:00:00Z", "lat": 48.05, "lon": -123.05},
                    ],
                }
            ],
        }
    )
    track_name = f"{race.name} - Boat"
    times = [
        p.timestamp.replace(tzinfo=timezone.utc).timestamp()
        for p in race.boat(1).positions
    ]

    class Host(SimpleNamespace):
        pass

    host = Host()
    host.controller = SimpleNamespace(
        core=SimpleNamespace(
            routingManager=[],
            trackManager=SimpleNamespace(
                get_active=lambda: SimpleNamespace(name=track_name)
            ),
            poiManager=[],
        )
    )
    host.time_control = SimpleNamespace(time=SimpleNamespace(timestamp=lambda: 0.0))
    host.isochrone_frame = SimpleNamespace(isochrones=False)
    host.navigation = SimpleNamespace(
        draw_routes=lambda *a, **k: None, draw_navigation=lambda *a, **k: None
    )
    host.ais_visible = False
    host.ais_targets = []
    host.ais_renderer = SimpleNamespace(draw_targets=lambda *a, **k: None)
    host.isochrones = SimpleNamespace(draw=lambda *a, **k: None)
    host.get_scale_factor = lambda: 1.0
    host.get_allocated_width = lambda: 64
    host.get_allocated_height = lambda: 64
    host._view_pixel_size = lambda: (64, 64, 1.0)
    host.invalidate_navigation = lambda: None
    host.update = lambda: None

    def convert(point):
        lat = getattr(point, "_lat", None)
        lon = getattr(point, "_lon", None)
        if lat is None and isinstance(point, (tuple, list)):
            lat, lon = point[0], point[1]
        return (
            (float(lat) - 48.0) * 100.0 + 16.0,
            (float(lon) + 123.0) * 100.0 + 16.0,
        )

    host.convert_geographic_to_screen = convert
    host.points = ()
    host.position = None
    host.offsets = ()
    host.route_plan = ()
    host.route_preview = ()
    host.route = ()
    host.selected_route_point = -1

    controller = SimpleNamespace(time_control=TimeControl(), core=host.controller.core)
    race_panel = SimpleNamespace(race=race, race_track_times={track_name: times})
    window = SimpleNamespace(
        controller=controller,
        race_panel=race_panel,
        map=host,
        log_map=host,
        _race_track_times={track_name: times},
        _race_boat_positions=[],
    )

    def race_time_changed(when):
        PreviewWindow._race_time_changed(window, when)

    controller.time_control.connect("time_change", race_time_changed)

    slider = QSlider(Qt.Orientation.Horizontal)
    qtbot.addWidget(slider)
    slider.setRange(0, 1)

    def scrub(value):
        controller.time_control.set_time(
            race.start_time if value == 0 else race.end_time
        )

    slider.valueChanged.connect(scrub)

    slider.setValue(1)
    qtbot.waitUntil(
        lambda: window._race_boat_positions == [[48.05, -123.05]], timeout=1000
    )
    first_centroid = _marker_centroid(RetainedOverlayMixin._navigation_image(host))
    assert first_centroid is not None

    slider.setValue(0)
    qtbot.waitUntil(
        lambda: window._race_boat_positions == [[48.0, -123.0]], timeout=1000
    )
    second_centroid = _marker_centroid(RetainedOverlayMixin._navigation_image(host))
    assert second_centroid is not None
    assert first_centroid != second_centroid

    window._race_track_times = {}
    window.race_panel.race = None
    controller.time_control.set_time(race.end_time)
    qtbot.waitUntil(lambda: window._race_boat_positions == [], timeout=1000)
    cleared_centroid = _marker_centroid(RetainedOverlayMixin._navigation_image(host))
    assert cleared_centroid is None
