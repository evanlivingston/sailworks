import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import timezone
from types import SimpleNamespace

from PySide6.QtWidgets import QApplication

from sailworks.core.race import parse_race
from sailworks.qt import racepanel


def _sample_race():
    return parse_race(
        {
            "race_id": "batch-test",
            "name": "Batch test",
            "start_time": "2026-05-16T03:00:00Z",
            "end_time": "2026-05-16T04:00:00Z",
            "teams": [{"id": 1, "name": "Boat A"}, {"id": 2, "name": "Boat B"}],
            "positions": [
                {
                    "id": 1,
                    "moments": [
                        {"gpsAt": "2026-05-16T03:00:00Z", "lat": 48.0, "lon": -123.0},
                    ],
                },
                {
                    "id": 2,
                    "moments": [
                        {"gpsAt": "2026-05-16T03:00:00Z", "lat": 48.1, "lon": -123.1},
                    ],
                },
            ],
        }
    )


class _TrackManager(list):
    def __init__(self):
        super().__init__()
        self.removed_names = []
        self.appended_batches = []
        self.removed_batches = []
        self.saves = 0
        self.active = None

    def get_unique_name(self, name):
        candidate = name
        suffix = 1
        while any(t.name == candidate for t in self):
            candidate = f"{name}-{suffix}"
            suffix += 1
        return candidate

    def remove_many(self, tracks, save=True):
        self.removed_batches.append((list(tracks), save))
        ids = {id(track) for track in tracks}
        self[:] = [track for track in self if id(track) not in ids]

    def remove_by_names(self, names, save=True):
        self.removed_names.append(list(names))
        wanted = set(names or ())
        self[:] = [
            track for track in self if getattr(track, "name", None) not in wanted
        ]

    def append_many(self, tracks, save=True):
        self.appended_batches.append(list(tracks))
        for track in tracks:
            track.collection = self
            self.append(track)

    def set_active(self, track, save=True):
        self.active = track

    def save_async(self):
        self.saves += 1

    def get_active(self):
        return self.active


def test_show_race_tracks_uses_batched_remove_and_append(qtbot):
    from unittest.mock import patch

    app = QApplication.instance() or QApplication([])
    manager = _TrackManager()

    # Create a fully mocked core with only our mock trackManager
    class MockCore(SimpleNamespace):
        def __init__(self):
            super().__init__()
            self.trackManager = manager
            self.routingManager = []
            self.poiManager = []

    # Mock load_saved_races to return empty to prevent loading real tracks
    with patch("sailworks.qt.racepanel.load_saved_races", return_value=[]):
        controller = SimpleNamespace(
            time_control=SimpleNamespace(time=None),
            core=MockCore(),
        )
        panel = racepanel.RacesPanel(controller)
        qtbot.addWidget(panel)

    race = _sample_race()
    panel._defer_heavy = False

    panel._show_race_tracks(race)

    assert len(manager.appended_batches) == 1
    assert len(manager.appended_batches[0]) == 2
    assert all(
        getattr(track, "race_track", False) for track in manager.appended_batches[0]
    )
    assert panel._race_track_names == [
        f"{race.name} - Boat A",
        f"{race.name} - Boat B",
    ]
    assert (
        manager.active is manager[-2] or manager.active in manager.appended_batches[0]
    )
    original = list(manager)
    panel._show_race_tracks(race)
    assert all(a is b for a, b in zip(manager, original))
    assert len(manager) == 2


def test_restart_change_and_stale_tracks(qtbot, monkeypatch):
    import json
    from sailworks.core.geo.track import Track

    monkeypatch.setattr(racepanel, "load_saved_races", lambda: [])
    manager = _TrackManager()
    panel = racepanel.RacesPanel(
        SimpleNamespace(core=SimpleNamespace(trackManager=manager))
    )
    qtbot.addWidget(panel)
    panel._defer_heavy = False
    race = _sample_race()
    panel._show_race_tracks(race)
    saved = json.loads(json.dumps([t.to_json() for t in manager]))
    assert all("race_times" not in track for track in saved)
    manager[:] = [Track.from_json(t) for t in saved]
    for track in manager:
        track.collection = manager
    original = list(manager)
    panel._race_track_names = []
    panel.race_track_times = {}
    panel._show_race_tracks(race)
    assert len(manager) == 2 and all(a is b for a, b in zip(manager, original))
    assert len(panel._race_track_names) == len(panel.race_track_times) == 2
    revision = manager[0]._revision
    race.boats[0].positions[0].latitude += 1
    race.boats.pop()
    panel._show_race_tracks(race)
    assert len(manager) == 1 and manager[0] is original[0]
    assert manager[0]._revision > revision
    assert manager[0].points[0][0] == race.boats[0].positions[0].latitude


def test_legacy_duplicate_migration(qtbot, monkeypatch):
    from sailworks.core.geo.track import Track

    monkeypatch.setattr(racepanel, "load_saved_races", lambda: [])
    manager = _TrackManager()
    panel = racepanel.RacesPanel(
        SimpleNamespace(core=SimpleNamespace(trackManager=manager))
    )
    qtbot.addWidget(panel)
    panel._defer_heavy = False
    race = _sample_race()
    a = Track(f"{race.name} - Boat A", race_track=True)
    b = Track(f"{race.name} - Boat B", race_track=True)
    duplicate1 = Track(f"{a.name}-1", race_track=True)
    duplicate2 = Track(f"{a.name}-2", race_track=True)
    unrelated = Track("Another race - Boat A-1", race_track=True)
    manager.extend([duplicate2, duplicate1, a, b, unrelated])
    panel._show_race_tracks(race)
    assert a.race_key and a.race_boat_key
    assert b.race_key and b.race_boat_key
    assert manager == [a, b, unrelated]
    assert manager.removed_batches == [([duplicate2, duplicate1], False)]
    assert manager.saves == 1
    panel._show_race_tracks(race)
    assert manager == [a, b, unrelated]
    assert len(manager.removed_batches) == 1
    assert manager.saves == 1


def test_legacy_suffix_order_and_ambiguous_names(qtbot, monkeypatch):
    from sailworks.core.geo.track import Track

    monkeypatch.setattr(racepanel, "load_saved_races", lambda: [])
    manager = _TrackManager()
    panel = racepanel.RacesPanel(
        SimpleNamespace(core=SimpleNamespace(trackManager=manager))
    )
    qtbot.addWidget(panel)
    panel._defer_heavy = False
    race = _sample_race()
    base = f"{race.name} - Boat A"
    later = Track(f"{base}-10", race_track=True)
    earlier = Track(f"{base}-2", race_track=True)
    manager.extend([later, earlier])
    panel._show_race_tracks(race)
    assert earlier.race_key and later not in manager

    manager.clear()
    manager.removed_batches.clear()
    race.boats[1].name = race.boats[0].name
    legacy = Track(base, race_track=True)
    duplicate = Track(f"{base}-1", race_track=True)
    manager.extend([legacy, duplicate])
    panel._show_race_tracks(race)
    assert legacy.race_key is None and duplicate.race_key is None
    assert legacy in manager and duplicate in manager
    assert len(manager) == 4
    assert not manager.removed_batches


def test_tracks_changed_precedes_persistence(qtbot, monkeypatch):
    monkeypatch.setattr(racepanel, "load_saved_races", lambda: [])
    manager = _TrackManager()
    order = []
    manager.save_async = lambda: order.append("save")
    panel = racepanel.RacesPanel(
        SimpleNamespace(core=SimpleNamespace(trackManager=manager))
    )
    qtbot.addWidget(panel)
    panel._defer_heavy = False
    panel.tracks_changed.connect(lambda: order.append("signal"))
    panel._show_race_tracks(_sample_race())
    assert order == ["signal", "save"]


def test_panel_close_flushes(qtbot, monkeypatch):
    monkeypatch.setattr(racepanel, "load_saved_races", lambda: [])
    manager = _TrackManager()
    flushed = []
    manager.flush_async = lambda: flushed.append(True)
    panel = racepanel.RacesPanel(
        SimpleNamespace(core=SimpleNamespace(trackManager=manager))
    )
    qtbot.addWidget(panel)
    panel.close()
    assert flushed == [True]
