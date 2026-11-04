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

    def exists(self, name):
        return any(track.name == name for track in self)

    def save(self):
        pass

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


def test_show_race_tracks_builds_transient_race_tracks(qtbot):
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

    old_race = _sample_race()
    old_race.race_id = "old"
    panel._races.append(old_race)
    panel._displayed_race_id = "old"
    panel._defer_heavy = False
    panel._load_race_tracks(old_race)

    panel._show_race_tracks(race)

    assert not panel.race_tracks(old_race)
    assert [track.name for track in panel.race_tracks(race)] == [
        "Boat A",
        "Boat B",
    ]
    assert panel.active_race_track in panel.race_tracks(race)
    # Race tracks are transient: they never enter the user's track collection.
    assert len(manager) == 0
    assert not manager.appended_batches
