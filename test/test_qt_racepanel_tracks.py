import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from sailworks.core.geo import TrackCollection
from sailworks.core.geo.track import Track
from sailworks.core.race import Race, RaceBoat, RaceCourse, RacePosition
from sailworks.core.timecontrol import utc_to_wall
from sailworks.qt.racepanel import RacesPanel
from sailworks.qt.window import PreviewWindow


def make_race(race_id):
    position = RacePosition(datetime(2026, 1, 1), 48, -123)
    return Race(
        race_id,
        race_id,
        boats=[
            RaceBoat(1, name="Boat", positions=[position]),
            RaceBoat(2, name="Boat", positions=[position]),
            RaceBoat(3, name="Empty"),
        ],
    )


@pytest.fixture
def panel(qtbot, monkeypatch):
    monkeypatch.setattr(
        "sailworks.core.geo.collection.CollectionStorage",
        lambda name: SimpleNamespace(
            data={},
            save_async_data=lambda payload: None,
            flush_async=lambda: None,
        ),
    )
    monkeypatch.setattr("sailworks.qt.racepanel.load_saved_races", lambda: [])
    manager = TrackCollection()
    controller = SimpleNamespace(core=SimpleNamespace(trackManager=manager))
    widget = RacesPanel(controller)
    widget._defer_heavy = False
    qtbot.addWidget(widget)
    return widget


def test_load_tracks_uses_boat_ids_and_timestamps(panel):
    race = make_race("first")
    assert panel.load_race_tracks(race) == 2
    tracks = panel.race_tracks(race)
    assert {track.boat_id for track in tracks} == {1, 2}
    assert all(track.race_id == race.race_id for track in tracks)
    assert all(
        track.timestamps == [race.boats[0].positions[0].epoch] for track in tracks
    )
    assert panel.active_race_track is tracks[0]
    assert panel.load_race_tracks(race) == 0
    assert len(panel.race_tracks(race)) == 2


def test_unload_only_owned_tracks(panel):
    first, second = make_race("first"), make_race("second")
    panel.load_race_tracks(first)
    panel.load_race_tracks(second)
    assert panel.unload_race_tracks(first) == 2
    assert panel.unload_race_tracks(first) == 0
    assert not panel.race_tracks(first)
    assert len(panel.race_tracks(second)) == 2
    assert all(track.timestamps for track in panel.race_tracks(second))


def test_selection_replaces_displayed_but_preserves_other_race(panel):
    first, second, third = (make_race(name) for name in ("first", "second", "third"))
    panel._add_race(first)
    panel.load_race_tracks(third)
    panel._add_race(second)
    assert not panel.race_tracks(first)
    assert len(panel.race_tracks(second)) == 2
    assert len(panel.race_tracks(third)) == 2


def test_explicit_load_survives_selection_change(panel):
    first, second, third = (make_race(name) for name in ("first", "second", "third"))
    panel._add_race(first)
    panel.load_race_tracks(second)
    panel._add_race(third)
    assert not panel.race_tracks(first)
    assert len(panel.race_tracks(second)) == 2
    assert len(panel.race_tracks(third)) == 2
    assert panel.unload_race_tracks(second) == 2
    assert not panel.race_tracks(second)


def test_context_menu_targets_row_without_changing_selection(panel):
    first, second = make_race("first"), make_race("second")
    panel._add_race(first)
    panel._add_race(second)
    panel.race_list.setCurrentRow(0)
    item = panel.race_list.item(1)
    position = panel.race_list.visualItemRect(item).center()
    with patch("sailworks.qt.racepanel.QMenu") as menu:
        panel._race_context_menu(position)
        menu.return_value.exec.assert_called_once()
        load = menu.return_value.addAction.return_value
        load.triggered.connect.call_args_list[0].args[0]()
    assert panel.race_list.currentRow() == 0
    assert panel.race is first
    assert len(panel.race_tracks(second)) == 2


def test_deferred_selection_does_not_restore_stale_race(panel, qtbot):
    panel._defer_heavy = True
    first, second = make_race("first"), make_race("second")
    panel._add_race(first)
    panel._add_race(second)
    qtbot.wait(1)
    assert not panel.race_tracks(first)
    assert len(panel.race_tracks(second)) == 2


def test_review_from_start_reapplies_race_bounds(panel):
    race = make_race("first")
    race.start_time = datetime(2026, 1, 1, 0, 0)
    race.end_time = datetime(2026, 1, 1, 6, 0)
    times = []
    panel.controller.time_control = SimpleNamespace(set_time=times.append)
    emitted = []
    panel.race_selected.connect(emitted.append)
    panel._add_race(race)
    emitted.clear()

    panel.review_from_start()

    assert emitted == [race]
    assert times == [utc_to_wall(race.start_time)]


def test_race_track_uses_boat_colour(panel):
    race = Race(
        "colour",
        "colour",
        boats=[
            RaceBoat(
                1,
                name="Red",
                colour="FF0000",
                positions=[RacePosition(datetime(2026, 1, 1), 48, -123)],
            )
        ],
    )

    assert panel.load_race_tracks(race) == 1
    assert panel.race_tracks(race)[0].color == (1.0, 0.0, 0.0)


def test_race_timeline_bounds_fall_back_to_positions():
    race = Race(
        "first",
        "first",
        boats=[
            RaceBoat(
                1,
                name="Boat",
                positions=[
                    RacePosition(datetime(2026, 1, 1, 1), 48, -123),
                    RacePosition(datetime(2026, 1, 1, 3), 48, -123),
                ],
            )
        ],
    )
    timeline = Mock()
    window = SimpleNamespace(
        _race_timeline_bounds=None, map=SimpleNamespace(timeline=timeline)
    )
    window._apply_race_timeline_bounds = lambda *args, **kwargs: (
        PreviewWindow._apply_race_timeline_bounds(window, *args, **kwargs)
    )
    window._apply_race_course = Mock()

    PreviewWindow._race_selected(window, race)

    start, end = timeline.set_region.call_args.args
    assert (start, end) == (
        race.boats[0].positions[0].epoch,
        race.boats[0].positions[1].epoch,
    )


def test_delete_race_removes_entry_tracks_and_file(panel, tmp_path, monkeypatch):
    path = tmp_path / "first.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("sailworks.qt.racepanel.race_file_path", lambda race_id: path)
    race = make_race("first")
    panel._add_race(race)
    assert len(panel.race_tracks(race)) == 2

    assert panel.delete_race(race) is True

    assert race not in panel._races
    assert panel.race_list.count() == 0
    assert panel.race is None
    assert not panel.race_tracks(race)
    assert not path.exists()
    assert panel.delete_race(race) is False


def test_create_track_from_race_uses_course(panel):
    race = make_race("first")
    race.course = [RaceCourse(name="Course", points=[(48.0, -123.0), (48.2, -123.2)])]
    panel._races.append(race)

    track = panel.create_track_from_race(race)

    assert track is not None
    assert track in panel.controller.core.trackManager
    assert track.points == [(48.0, -123.0), (48.2, -123.2)]
    assert panel.controller.core.trackManager.get_active() is track


def test_weather_request_fills_and_starts_history(panel):
    race = make_race("first")
    panel._add_race(race)
    calls = []
    history = SimpleNamespace(
        apply_race_request=lambda region, start, end: calls.append((region, start, end))
    )
    dialog = SimpleNamespace(
        show=Mock(),
        raise_=Mock(),
        activateWindow=Mock(),
        history=history,
        tabs=SimpleNamespace(setCurrentWidget=Mock()),
    )
    window = SimpleNamespace(_ensure_library=lambda: None, library_dialog=dialog)
    panel.window = lambda: window

    panel.show_weather_request()

    assert len(calls) == 1
    region, start, end = calls[0]
    assert region == race.weather_request()["region"]
    assert start == race.start_time
    assert end == race.end_time
    dialog.tabs.setCurrentWidget.assert_called_once_with(history)


def test_scrub_markers_include_track_colour_and_name(panel):
    race = make_race("first")
    panel._add_race(race)
    canvas = SimpleNamespace(
        race_boat_positions=None,
        race_positions=None,
        race_boat_markers=None,
        offsets=(),
        position=None,
        refresh_route_overlay=Mock(),
        invalidate_navigation=Mock(),
        update=Mock(),
    )
    window = SimpleNamespace(
        race_panel=panel,
        controller=SimpleNamespace(time_control=SimpleNamespace(time=None)),
        map=canvas,
        log_map=canvas,
        _race_boat_positions=[],
    )

    PreviewWindow._race_time_changed(window, utc_to_wall(race.start_time))

    assert len(canvas.race_boat_markers) == 2
    marker = canvas.race_boat_markers[0]
    assert marker["name"] == "Boat"
    assert marker["color"] == list(panel.race_tracks(race)[0].color)
    assert marker["lat"] == 48.0
    assert marker["lon"] == -123.0


def test_apply_race_course_publishes_lines():
    race = Race(
        "first",
        "first",
        course=[
            RaceCourse(name="START", points=[(48.0, -123.0), (48.01, -123.0)]),
            RaceCourse(name="Course", points=[(48.0, -123.0), (48.2, -123.2)]),
            RaceCourse(name="Empty", points=[]),
        ],
    )
    canvas = SimpleNamespace(
        race_courses=None, invalidate_navigation=Mock(), update=Mock()
    )
    window = SimpleNamespace(map=canvas, log_map=canvas)

    PreviewWindow._apply_race_course(window, race)

    assert [course["name"] for course in canvas.race_courses] == ["START", "Course"]
    assert canvas.race_courses[0]["points"] == [[48.0, -123.0], [48.01, -123.0]]
    assert canvas.race_courses[1]["closed"] is False
    canvas.invalidate_navigation.assert_called()
    canvas.update.assert_called()

    PreviewWindow._apply_race_course(window, None)
    assert canvas.race_courses == []


def test_boat_tab_lists_metadata_and_toggles_tracks(panel):
    race = Race(
        "boats",
        "boats",
        boats=[
            RaceBoat(
                1,
                name="Absolutely",
                sail="3909",
                model="Farr 39 ML",
                boat_type="MONO_M",
                country="United States",
                positions=[RacePosition(datetime(2026, 1, 1), 48, -123)],
            ),
            RaceBoat(
                2,
                name="Afterglow",
                sail="62",
                positions=[RacePosition(datetime(2026, 1, 1), 48.1, -123.1)],
            ),
        ],
    )
    panel._add_race(race)

    assert panel.boat_table.rowCount() == 2

    def row_for(boat_id):
        for row in range(panel.boat_table.rowCount()):
            item = panel.boat_table.item(row, 0)
            if item.data(Qt.ItemDataRole.UserRole) == boat_id:
                return row
        raise AssertionError(f"boat {boat_id} not in table")

    first_row = row_for(1)
    first = panel.boat_table.item(first_row, 0)
    assert first.text() == "Absolutely"
    assert panel.boat_table.item(first_row, 1).text() == "3909"
    assert panel.boat_table.item(first_row, 2).text() == "Farr 39 ML"
    assert panel.boat_table.item(first_row, 3).text() == "MONO_M"
    assert panel.boat_table.item(first_row, 4).text() == "United States"
    assert first.checkState() == Qt.CheckState.Checked

    first.setCheckState(Qt.CheckState.Unchecked)
    assert panel.track_for_boat(race, 1).visible is False
    first.setCheckState(Qt.CheckState.Checked)
    assert panel.track_for_boat(race, 1).visible is True

    panel.boat_table.setCurrentCell(row_for(2), 0)
    assert panel.active_race_track.boat_id == 2


def test_legacy_race_tracks_are_purged_from_track_collection(panel):
    manager = panel.controller.core.trackManager
    legacy = Track("first - Boat", [(48.0, -123.0)])
    legacy._legacy_race_track = True
    manager.append(legacy)

    panel._purge_legacy_race_tracks()

    assert legacy not in manager
