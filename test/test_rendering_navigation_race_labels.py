import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

from sailworks.rendering.navigation import NavigationRenderer


class _FakeCr:
    def __init__(self):
        self.texts = []

    def __getattr__(self, name):
        return lambda *args, **kwargs: None

    def show_text(self, text):
        self.texts.append(str(text))


class _Track:
    def __init__(self, name, points):
        self.name = name
        self.visible = True
        self.race_track = True
        self._points = points

    def __iter__(self):
        return iter(self._points)


class _TrackManager(list):
    def is_active(self, track):
        return True


class _Poi:
    name = "Harbor"
    visible = True
    position = (48.0, -123.0)


class _Route:
    name = "Route A"
    visible = True
    provenance = {}
    _points = [
        (48.0, -123.0, "2026-01-01T00:00:00", 0.0, 5.0, 0.0, 0.0),
        (48.1, -123.1, "2026-01-01T01:00:00", 0.0, 5.0, 0.0, 0.0),
    ]

    def __iter__(self):
        return iter(self._points)


def test_draw_routes_can_skip_for_qml_overlay():
    core = SimpleNamespace(
        trackManager=_TrackManager([]),
        poiManager=[],
        routingManager=[_Route()],
    )
    nav = NavigationRenderer(core)

    class _GpsMap:
        def convert_geographic_to_screen(self, point):
            return (10.0, 20.0)

    skipped = _FakeCr()
    nav.draw_routes(_GpsMap(), skipped, 1767225600.0, draw_routes=False)
    assert "Route A" not in skipped.texts

    drawn = _FakeCr()
    nav.draw_routes(_GpsMap(), drawn, 1767225600.0, draw_routes=True)
    assert "Route A" in drawn.texts


def test_draw_navigation_can_skip_tracks_for_qml_overlay():
    track = _Track("Track A", [(48.0, -123.0), (48.1, -123.1)])
    track.race_track = False
    core = SimpleNamespace(
        trackManager=_TrackManager([track]),
        poiManager=[],
        routingManager=[],
    )
    nav = NavigationRenderer(core)

    class _GpsMap:
        def convert_geographic_to_screen(self, point):
            return (10.0, 20.0)

    skipped = _FakeCr()
    nav.draw_navigation(_GpsMap(), skipped, draw_tracks=False)
    assert "Track A" not in skipped.texts

    drawn = _FakeCr()
    nav.draw_navigation(_GpsMap(), drawn, draw_tracks=True)
    assert "Track A" in drawn.texts


def test_draw_race_tracks_can_skip_pois_for_qml_overlay():
    core = SimpleNamespace(
        trackManager=_TrackManager([]),
        poiManager=[_Poi()],
        routingManager=[],
    )
    nav = NavigationRenderer(core)

    class _GpsMap:
        def convert_geographic_to_screen(self, point):
            return (10.0, 20.0)

    skipped = _FakeCr()
    nav.draw_race_tracks(_GpsMap(), skipped, draw_pois=False)
    assert "Harbor" not in skipped.texts

    drawn = _FakeCr()
    nav.draw_race_tracks(_GpsMap(), drawn, draw_pois=True)
    assert "Harbor" in drawn.texts


def test_draw_race_tracks_omits_per_fix_point_numbers():
    track = _Track("Race A", [(48.0, -123.0), (48.1, -123.1)])
    core = SimpleNamespace(
        trackManager=_TrackManager([track]),
        poiManager=[],
        routingManager=[],
    )
    nav = NavigationRenderer(core)

    class _GpsMap:
        def convert_geographic_to_screen(self, point):
            return (10.0, 20.0)

    cr = _FakeCr()
    nav.draw_race_tracks(_GpsMap(), cr)
    # Track name may be drawn, but no per-fix numeric labels.
    assert "1" not in cr.texts
    assert "2" not in cr.texts
