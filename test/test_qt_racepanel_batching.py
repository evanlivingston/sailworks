import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import timezone
from types import SimpleNamespace

from PySide6.QtWidgets import QApplication

from gweatherrouting.core.race import parse_race
from gweatherrouting.qt import racepanel


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
        self.active = None

    def get_unique_name(self, name):
        return name

    def remove_by_names(self, names):
        self.removed_names.append(list(names))
        wanted = set(names or ())
        self[:] = [track for track in self if getattr(track, "name", None) not in wanted]

    def append_many(self, tracks):
        self.appended_batches.append(list(tracks))
        for track in tracks:
            track.collection = self
            self.append(track)

    def set_active(self, track):
        self.active = track

    def get_active(self):
        return self.active


def test_show_race_tracks_uses_batched_remove_and_append(qtbot):
    app = QApplication.instance() or QApplication([])
    manager = _TrackManager()
    controller = SimpleNamespace(
        time_control=SimpleNamespace(time=None),
        core=SimpleNamespace(trackManager=manager),
    )
    panel = racepanel.RacesPanel(controller)
    qtbot.addWidget(panel)
    race = _sample_race()

    # Pre-populate manager with an old race track name.
    panel._race_track_names = ["old track"]
    manager.append(SimpleNamespace(name="old track"))

    panel._show_race_tracks(race)

    assert len(manager.removed_names) == 1
    assert manager.removed_names[0] == ["old track"]
    assert len(manager.appended_batches) == 1
    assert len(manager.appended_batches[0]) == 2
    assert all(getattr(track, "race_track", False) for track in manager.appended_batches[0])
    assert panel._race_track_names == [
        f"{race.name} - Boat A",
        f"{race.name} - Boat B",
    ]
    assert manager.active is manager[-2] or manager.active in manager.appended_batches[0]