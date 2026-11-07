import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import timezone
from types import SimpleNamespace
from unittest.mock import Mock

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QSlider

from sailworks.core.race import RaceTrack, parse_race
from sailworks.core.timecontrol import TimeControl, utc_to_wall
from sailworks.qt.mapoverlays import RetainedOverlayMixin
from sailworks.qt.window import PreviewWindow


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
    from sailworks.qt import racepanel

    class TrackManager(list):
        active = None

        def get_unique_name(self, name):
            return name

        def exists(self, name):
            return any(track.name == name for track in self)

        def save(self):
            pass

        def set_active(self, track, save=True):
            self.active = track

        def remove_by_names(self, names, save=True):
            wanted = set(names or ())
            self[:] = [
                track for track in self if getattr(track, "name", None) not in wanted
            ]

        def append_many(self, tracks, save=True):
            for track in tracks:
                track.collection = self
                self.append(track)

    manager = TrackManager()
    controller = SimpleNamespace(
        time_control=TimeControl(), core=SimpleNamespace(trackManager=manager)
    )
    panel = racepanel.RacesPanel(controller)
    qtbot.addWidget(panel)
    race = _sample_race()
    panel._add_race(race)

    controller.time_control.set_time(utc_to_wall(race.start_time))
    positions = panel.boats_at_current_time()
    assert 1 in positions
    assert positions[1].latitude == 48.0

    controller.time_control.set_time(utc_to_wall(race.end_time))
    positions = panel.boats_at_current_time()
    assert positions[1].latitude == 48.1


def test_window_race_time_changed_publishes_all_boat_positions(qtbot):
    race = _sample_race()
    positions = race.boat(1).positions
    track = RaceTrack(
        boat_id=1,
        name="Boat",
        race_id=race.race_id,
        points=[(p.latitude, p.longitude) for p in positions],
        timestamps=[
            p.timestamp.replace(tzinfo=timezone.utc).timestamp() for p in positions
        ],
    )
    race_panel = SimpleNamespace(race=race, active_race_track=track)
    race_panel.race_tracks = lambda selected: [track]
    controller = SimpleNamespace(
        time_control=TimeControl(), core=SimpleNamespace(trackManager=[])
    )
    canvas_map = Mock()
    canvas_log = Mock()
    window = SimpleNamespace(
        controller=controller,
        race_panel=race_panel,
        map=canvas_map,
        log_map=canvas_log,
        _race_boat_positions=[],
    )
    window._race_time_changed = lambda when: PreviewWindow._race_time_changed(
        window, when
    )

    when = utc_to_wall(race.start_time)
    controller.time_control.set_time(when)
    window._race_time_changed(when)

    assert window._race_boat_positions == [[48.0, -123.0]]
    for canvas in (canvas_map, canvas_log):
        assert canvas.race_boat_positions == [[48.0, -123.0]]
        assert canvas.race_positions == [[48.0, -123.0]]
        canvas.invalidate_navigation.assert_called()
        canvas.update.assert_called()

    later = utc_to_wall(race.end_time)
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
        race_panel=SimpleNamespace(race=None, active_race_track=None),
        map=canvas_map,
        log_map=canvas_log,
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
    positions = race.boat(1).positions
    track = RaceTrack(
        boat_id=1,
        name="Boat",
        race_id=race.race_id,
        points=[(p.latitude, p.longitude) for p in positions],
        timestamps=[
            p.timestamp.replace(tzinfo=timezone.utc).timestamp() for p in positions
        ],
    )

    class Host(SimpleNamespace):
        pass

    host = Host()
    host.controller = SimpleNamespace(
        core=SimpleNamespace(
            routingManager=[],
            trackManager=[],
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
    race_panel = SimpleNamespace(race=race, active_race_track=track)
    race_panel.race_tracks = lambda selected: [track]
    window = SimpleNamespace(
        controller=controller,
        race_panel=race_panel,
        map=host,
        log_map=host,
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
            utc_to_wall(race.start_time if value == 0 else race.end_time)
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

    window.race_panel.active_race_track = None
    window.race_panel.race = None
    controller.time_control.set_time(utc_to_wall(race.end_time))
    qtbot.waitUntil(lambda: window._race_boat_positions == [], timeout=1000)
    cleared_centroid = _marker_centroid(RetainedOverlayMixin._navigation_image(host))
    assert cleared_centroid is None
