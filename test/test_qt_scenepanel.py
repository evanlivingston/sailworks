import os
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog

from sailworks.core.geo.elementmultipoint import NamedPoint
from sailworks.core.geo.rounding import ROUNDING_GATE, ROUNDING_PORT, ROUNDING_STARBOARD
from sailworks.core.scene import KIND_LOG, KIND_POI, KIND_ROUTING, KIND_TRACK
from sailworks.qt.scenepanel import (
    COORDINATE_ROLE,
    NAME_COLUMN,
    ROUNDING_ROLE,
    VISIBLE_COLUMN,
    ScenePanel,
)


class FakeTrack:
    def __init__(self, uid, name):
        self.uid = uid
        self.name = name
        self.visible = True
        self.points = [(51.1234567, -1.2345678), (52.0, -2.0)]
        self._names = {}
        self._roundings = {}

    def __len__(self):
        return 2

    def point_name(self, index):
        return self._names.get(index, "") or getattr(self.points[index], "name", "")

    def rename_point(self, index, name):
        name = str(name).strip()
        self._names[index] = name
        self.points[index] = NamedPoint(tuple(self.points[index]), name)
        return True

    def point_rounding(self, index):
        return self._roundings.get(index)

    def set_point_rounding(self, index, value):
        self._roundings[index] = int(value)
        return True


class FakeRoute:
    def __init__(self, uid, name):
        self.uid = uid
        self.name = name
        self.visible = True
        self.points = [(48.1, -123.1), (48.2, -123.2)]
        self._names = {}
        self._roundings = {}

    def point_name(self, index):
        return self._names.get(index, "")

    def rename_point(self, index, name):
        self._names[index] = str(name).strip()
        return True

    def point_rounding(self, index):
        return self._roundings.get(index)

    def set_point_rounding(self, index, value):
        self._roundings[index] = int(value)
        return True


class FakePOI:
    def __init__(self, uid, name):
        self.uid = uid
        self.name = name
        self.visible = True
        self.position = (48.5, -123.5)


class FakeCollection(list):
    def save(self):
        pass

    def get_unique_name(self, name):
        existing = {item.name for item in self}
        if name not in existing:
            return name
        index = 1
        while f"{name}.{index:03d}" in existing:
            index += 1
        return f"{name}.{index:03d}"


class FakeLibrary:
    def __init__(self, entries=()):
        self._entries = list(entries)

    def entries(self):
        return tuple(self._entries)

    def update(self, identifier, **values):
        item = next(entry for entry in self._entries if entry["id"] == identifier)
        item.update(values)


class FakeCore:
    def __init__(self):
        self.trackManager = FakeCollection()
        self.routingManager = FakeCollection()
        self.poiManager = FakeCollection()


class FakeSession:
    def __init__(self, log=None):
        self.log = log


class FakeController:
    def __init__(self, core, library, session=None):
        self.core = core
        self.log_library = library
        self.session = session or FakeSession()

    @property
    def log_library_changed(self):
        return self

    def emit(self):
        pass


def application():
    return QApplication.instance() or QApplication([])


def log_entry(tmp_path, identifier="log-1", name="Saturday.log"):
    return {
        "id": identifier,
        "name": name,
        "status": "Replayable",
        "file_info": {"path": str(tmp_path / f"{identifier}.log")},
    }


def panel_for(tmp_path, tracks=(), routes=(), pois=(), logs=(), loaded_log=None):
    core = FakeCore()
    core.trackManager.extend(tracks)
    core.routingManager.extend(routes)
    core.poiManager.extend(pois)
    return ScenePanel(
        FakeController(core, FakeLibrary(logs), FakeSession(loaded_log)),
        scene_path=tmp_path / "scene.json",
    )


def test_panel_builds_default_tree_and_items(tmp_path):
    application()
    log = log_entry(tmp_path)
    panel = panel_for(
        tmp_path,
        tracks=[FakeTrack("t1", "Morning")],
        routes=[FakeRoute("r1", "Route")],
        pois=[FakePOI("p1", "Harbor")],
        logs=[log],
        loaded_log=SimpleNamespace(path=log["file_info"]["path"]),
    )

    root = panel.tree.topLevelItem(0)
    assert root.text(NAME_COLUMN) == "Scene Collection"
    names = [root.child(index).text(NAME_COLUMN) for index in range(root.childCount())]
    assert names == ["Tracks", "Routings", "POIs", "Logs"]
    assert panel.item_row(KIND_TRACK, "t1") is not None
    assert panel.item_row(KIND_POI, "p1") is not None
    assert panel.item_row(KIND_LOG, "log-1") is not None


def test_toggling_item_updates_element_visibility(tmp_path):
    application()
    track = FakeTrack("t1", "Morning")
    panel = panel_for(tmp_path, tracks=[track])
    row = panel.item_row(KIND_TRACK, "t1")

    row.setCheckState(VISIBLE_COLUMN, Qt.CheckState.Unchecked)

    assert track.visible is False
    assert panel.scene.item(KIND_TRACK, "t1").visible is False


def test_poi_appears_in_default_group_and_toggles_visibility(tmp_path):
    application()
    poi = FakePOI("p1", "Harbor")
    panel = panel_for(tmp_path, pois=[poi])
    row = panel.item_row(KIND_POI, "p1")

    assert row is not None
    assert row.parent().text(NAME_COLUMN) == "POIs"
    row.setCheckState(VISIBLE_COLUMN, Qt.CheckState.Unchecked)

    assert poi.visible is False
    assert panel.scene.item(KIND_POI, "p1").visible is False


def test_double_click_poi_name_renames_poi(tmp_path):
    application()
    poi = FakePOI("p1", "Harbor")
    panel = panel_for(tmp_path, pois=[poi])
    row = panel.item_row(KIND_POI, "p1")

    panel._double_clicked(row, NAME_COLUMN)
    row.setText(NAME_COLUMN, "Marina")

    assert poi.name == "Marina"
    assert panel.item_row(KIND_POI, "p1").text(NAME_COLUMN) == "Marina"


def test_group_selection_creates_collection_under_common_parent(tmp_path):
    application()
    track = FakeTrack("t1", "Morning")
    panel = panel_for(tmp_path, tracks=[track])
    row = panel.item_row(KIND_TRACK, "t1")
    row.setSelected(True)
    panel.tree.setCurrentItem(row)

    panel._group_selection()

    item = panel.scene.item(KIND_TRACK, "t1")
    assert item.group is not None
    assert panel.scene.group(item.group).name == "Collection"
    assert panel.scene.group(item.group).parent == "default-tracks"


def test_log_rows_are_not_checkable(tmp_path):
    application()
    log = log_entry(tmp_path)
    panel = panel_for(
        tmp_path,
        logs=[log],
        loaded_log=SimpleNamespace(path=log["file_info"]["path"]),
    )
    row = panel.item_row(KIND_LOG, "log-1")

    assert not row.flags() & Qt.ItemFlag.ItemIsUserCheckable


def test_expanding_track_shows_every_point(tmp_path):
    application()
    panel = panel_for(tmp_path, tracks=[FakeTrack("t1", "Morning")])
    row = panel.item_row(KIND_TRACK, "t1")

    row.setExpanded(True)

    assert row.childCount() == 2
    assert row.child(0).text(NAME_COLUMN) == "Point 1"
    assert row.child(0).data(NAME_COLUMN, COORDINATE_ROLE) == "51.123457, -1.234568"
    assert row.child(0).data(NAME_COLUMN, ROUNDING_ROLE) == "Sail"
    assert row.child(1).text(NAME_COLUMN) == "Point 2"
    assert row.child(1).data(NAME_COLUMN, COORDINATE_ROLE) == "52.000000, -2.000000"


def test_rename_track_point_updates_element_and_tree(tmp_path):
    application()
    track = FakeTrack("t1", "Morning")
    panel = panel_for(tmp_path, tracks=[track])
    row = panel.item_row(KIND_TRACK, "t1")
    row.setExpanded(True)
    point_row = row.child(0)

    assert point_row.flags() & Qt.ItemFlag.ItemIsEditable
    panel._double_clicked(point_row, NAME_COLUMN)
    point_row.setText(NAME_COLUMN, "Start line")

    assert track.point_name(0) == "Start line"
    row = panel.item_row(KIND_TRACK, "t1")
    assert row.child(0).text(NAME_COLUMN) == "Start line"
    assert row.child(0).data(NAME_COLUMN, COORDINATE_ROLE) == ("51.123457, -1.234568")
    assert row.child(1).text(NAME_COLUMN) == "Point 2"


def test_move_track_point_requests_relocation(tmp_path):
    application()
    track = FakeTrack("t1", "Morning")
    panel = panel_for(tmp_path, tracks=[track])
    received = []
    panel.move_point_requested.connect(
        lambda element, index: received.append((element, index))
    )

    panel._move_point(("point", KIND_TRACK, "t1", 0))

    assert received == [(track, 0)]


def test_expanding_route_shows_marks_and_rounding(tmp_path):
    application()
    route = FakeRoute("r1", "Route")
    panel = panel_for(tmp_path, routes=[route])
    row = panel.item_row(KIND_ROUTING, "r1")

    row.setExpanded(True)

    assert row.child(0).text(NAME_COLUMN) == "Mark 1"
    assert row.child(0).data(NAME_COLUMN, ROUNDING_ROLE) == "Sail"
    assert row.child(0).data(NAME_COLUMN, COORDINATE_ROLE) == ("48.100000, -123.100000")


def test_cycle_rounding_updates_route_mark(tmp_path):
    application()
    route = FakeRoute("r1", "Route")
    panel = panel_for(tmp_path, routes=[route])
    node = ("point", KIND_ROUTING, "r1", 0)

    panel._cycle_rounding(node)
    assert route.point_rounding(0) == ROUNDING_GATE
    panel._cycle_rounding(node)
    assert route.point_rounding(0) == ROUNDING_PORT
    panel._cycle_rounding(node)
    assert route.point_rounding(0) == ROUNDING_STARBOARD

    row = panel.item_row(KIND_ROUTING, "r1")
    row.setExpanded(True)
    assert row.child(0).data(NAME_COLUMN, ROUNDING_ROLE) == "Stbd"


def test_edit_point_dialog_updates_name_and_rounding(tmp_path):
    application()
    route = FakeRoute("r1", "Route")
    panel = panel_for(tmp_path, routes=[route])

    class Dialog:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def point_name(self):
            return "Windward mark"

        def point_rounding(self):
            return ROUNDING_PORT

    with patch("sailworks.qt.scenepanel.PointEditDialog", Dialog):
        panel._edit_point(("point", KIND_ROUTING, "r1", 0))

    assert route.point_name(0) == "Windward mark"
    assert route.point_rounding(0) == ROUNDING_PORT


def test_edit_track_point_dialog_sets_rounding(tmp_path):
    application()
    track = FakeTrack("t1", "Morning")
    panel = panel_for(tmp_path, tracks=[track])

    class Dialog:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def point_name(self):
            return ""

        def point_rounding(self):
            return ROUNDING_STARBOARD

    with patch("sailworks.qt.scenepanel.PointEditDialog", Dialog):
        panel._edit_point(("point", KIND_TRACK, "t1", 0))

    assert track.point_rounding(0) == ROUNDING_STARBOARD
    row = panel.item_row(KIND_TRACK, "t1")
    row.setExpanded(True)
    assert row.child(0).data(NAME_COLUMN, ROUNDING_ROLE) == "Stbd"


def test_double_click_item_name_renames_track(tmp_path):
    application()
    track = FakeTrack("t1", "Morning")
    panel = panel_for(tmp_path, tracks=[track])
    row = panel.item_row(KIND_TRACK, "t1")

    panel._double_clicked(row, NAME_COLUMN)

    assert row.flags() & Qt.ItemFlag.ItemIsEditable
    row.setText(NAME_COLUMN, "Evening")

    assert track.name == "Evening"
    assert panel.item_row(KIND_TRACK, "t1").text(NAME_COLUMN) == "Evening"


def test_double_click_log_name_renames_library_entry(tmp_path):
    application()
    log = log_entry(tmp_path)
    panel = panel_for(
        tmp_path,
        logs=[log],
        loaded_log=SimpleNamespace(path=log["file_info"]["path"]),
    )
    row = panel.item_row(KIND_LOG, "log-1")

    panel._double_clicked(row, NAME_COLUMN)
    row.setText(NAME_COLUMN, "Sunday.log")

    assert log["name"] == "Sunday.log"
    assert panel.item_row(KIND_LOG, "log-1").text(NAME_COLUMN) == "Sunday.log"


def test_loaded_race_tracks_appear_as_scene_track_items(tmp_path):
    application()

    class RaceTrack:
        boat_id = "boat-9"
        name = "Blue Boat"
        visible = True
        points = [(51.0, -1.0), (52.0, -2.0)]

    class Race:
        race_id = "race-1"
        name = "Spring Race"

    panel = panel_for(tmp_path)
    track = RaceTrack()
    panel.race_panel = type(
        "Races",
        (),
        {"_races": [Race()], "loaded_race_tracks": lambda self: {"race-1": [track]}},
    )()

    panel.refresh()

    row = panel.item_row(KIND_TRACK, "race:race-1:boat-9")
    assert row is not None
    assert row.text(NAME_COLUMN) == "Blue Boat"
    race_group = panel.scene.item(KIND_TRACK, "race:race-1:boat-9").group
    assert panel.scene.group(race_group).name == "Spring Race"
    assert panel.scene.group(race_group).parent == "default-tracks"
    row.setCheckState(VISIBLE_COLUMN, Qt.CheckState.Unchecked)
    assert track.visible is False


def test_grouping_race_tracks_keeps_them_visible_when_type_group_collapsed(tmp_path):
    application()

    class RaceTrack:
        boat_id = "boat-9"
        name = "Blue Boat"
        visible = True
        points = [(51.0, -1.0), (52.0, -2.0)]

    class Race:
        race_id = "race-1"
        name = "Spring Race"

    panel = panel_for(tmp_path)
    panel.race_panel = type(
        "Races",
        (),
        {
            "_races": [Race()],
            "loaded_race_tracks": lambda self: {"race-1": [RaceTrack()]},
        },
    )()
    panel.refresh()
    row = panel.item_row(KIND_TRACK, "race:race-1:boat-9")
    row.setSelected(True)
    panel.tree.setCurrentItem(row)
    panel.group_row("default-tracks").setExpanded(False)

    panel._group_selection()

    grouped = panel.scene.item(KIND_TRACK, "race:race-1:boat-9")
    assert grouped.group != "default-tracks"
    assert panel.scene.group(grouped.group).parent == "race-group:race-1"
    assert panel.group_row("default-tracks").isExpanded()
    assert panel.item_row(KIND_TRACK, "race:race-1:boat-9") is not None

    panel._move_to([], [grouped.group], "default-tracks")
    assert panel.scene.group(grouped.group).parent == "default-tracks"
    assert panel.group_row(grouped.group) is not None, (
        grouped.group,
        [(group.id, group.parent) for group in panel.scene.groups.values()],
    )
    assert panel.group_row(grouped.group).isExpanded()
    assert panel.item_row(KIND_TRACK, "race:race-1:boat-9") is not None


def test_grouping_multiple_types_moves_all_selected_rows(tmp_path):
    application()
    track = FakeTrack("t1", "Morning")
    route = FakeRoute("r1", "Route")
    panel = panel_for(tmp_path, tracks=[track], routes=[route])
    track_row = panel.item_row("track", "t1")
    route_row = panel.item_row("routing", "r1")
    panel.tree.setCurrentItem(route_row)
    track_row.setSelected(True)
    route_row.setSelected(True)

    assert len(panel.tree.selectedItems()) == 2
    panel._group_selection()

    track_group = panel.scene.item("track", "t1").group
    route_group = panel.scene.item("routing", "r1").group
    assert track_group == route_group
    assert panel.scene.group(track_group).parent is None
    assert panel.item_row("track", "t1") is not None
    assert panel.item_row("routing", "r1") is not None


def test_scene_actions_fit_single_track_or_point(tmp_path):
    application()
    panel = panel_for(tmp_path, tracks=[FakeTrack("t1", "Morning")])
    track_node = ("item", KIND_TRACK, "t1")
    point_node = ("point", KIND_TRACK, "t1", 0)

    assert panel._nodes_for_fit([track_node]) == [track_node]
    assert panel._nodes_for_fit([point_node]) == [point_node]
    assert panel._nodes_for_fit([track_node, point_node]) == ()
    assert panel._nodes_for_fit([("item", KIND_LOG, "log-1")]) == ()


def test_scene_actions_delete_skips_logs_and_race_tracks(tmp_path):
    application()
    panel = panel_for(tmp_path)
    track_node = ("item", KIND_TRACK, "t1")
    point_node = ("point", KIND_TRACK, "t1", 0)

    assert panel._nodes_for_delete([track_node, point_node]) == [track_node, point_node]
    assert panel._nodes_for_delete([("item", KIND_LOG, "log-1")]) == []
    assert panel._nodes_for_delete([("item", KIND_TRACK, "race:r:1")]) == []
    assert panel._nodes_for_delete([("point", KIND_TRACK, "race:r:1", 0)]) == []


def test_scene_accumulates_opened_library_logs(tmp_path):
    application()
    first = log_entry(tmp_path, "log-1", "Saturday.log")
    second = log_entry(tmp_path, "log-2", "Sunday.log")
    panel = panel_for(
        tmp_path,
        logs=[first, second],
        loaded_log=SimpleNamespace(path=first["file_info"]["path"]),
    )

    assert panel.item_row(KIND_LOG, "log-1") is not None
    assert panel.item_row(KIND_LOG, "log-2") is None

    panel.controller.session.log = SimpleNamespace(path=second["file_info"]["path"])
    panel.refresh()

    assert panel.item_row(KIND_LOG, "log-1") is not None
    assert panel.item_row(KIND_LOG, "log-2") is not None


def test_scene_hides_library_logs_when_none_is_loaded(tmp_path):
    application()
    panel = panel_for(tmp_path, logs=[log_entry(tmp_path)])

    assert panel.item_row(KIND_LOG, "log-1") is None
