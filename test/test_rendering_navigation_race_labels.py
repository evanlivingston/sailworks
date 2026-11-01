import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

from gweatherrouting.rendering.navigation import NavigationRenderer


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
