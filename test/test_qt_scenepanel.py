import os
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QRect, QSignalBlocker, Qt
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QGuiApplication,
    QHelpEvent,
    QImage,
    QMouseEvent,
    QPalette,
    QUndoStack,
    QWheelEvent,
)
from PySide6.QtWidgets import QApplication, QDialog, QFrame, QPushButton, QToolTip

from sailworks.core.geo.elementmultipoint import NamedPoint
from sailworks.core.geo.rounding import (
    ROUNDING_GATE,
    ROUNDING_PORT,
    ROUNDING_SAIL_TO,
    ROUNDING_SHORT,
    ROUNDING_STARBOARD,
)
from sailworks.core import utils
from sailworks.core.scene import KIND_LOG, KIND_POI, KIND_ROUTING, KIND_TRACK
from sailworks.qt.scenepanel import (
    COORDINATE_ROLE,
    DETAIL_GAP,
    DETAIL_MIN_LABEL,
    DETAIL_ROLE,
    MUTED,
    MARK_COLUMN_HEADERS,
    MARK_HEADERS_ROLE,
    MARK_DETAIL_GAP,
    MARK_DETAIL_ROLE,
    MARK_MIN_LABEL,
    MARK_ROUNDING_HEADER,
    NAME_COLUMN,
    NODE_ROLE,
    POINT_MIN_LABEL,
    ROUNDING_GAP,
    ROUNDING_ROLE,
    VISIBLE_COLUMN,
    SceneItemDelegate,
    ScenePanel,
    SceneTooltip,
    TOOLTIP_OFFSET_X,
    TOOLTIP_OFFSET_Y,
    detail_columns,
    mark_column_widths,
    mark_columns,
    mark_header_columns,
    point_label_width,
    row_geometry,
    rounding_color,
    rounding_column_width,
)


from sailworks.qt.theme import BORDER, PANEL, SURFACE, TEXT, TOOLTIP_STYLE, ThemedMenu
from sailworks.rendering.navigation import (
    routing_detail_fields,
    routing_point_cells,
    routing_point_detail_fields,
    routing_point_tree_cells,
    routing_summary,
    track_detail_fields,
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
        return len(self.points)

    def add(self, lat, lon, t=None):
        self.points.append(NamedPoint((lat, lon), ""))

    def remove(self, index):
        del self.points[index]

    def move(self, index, lat, lon):
        self.points[index] = NamedPoint((lat, lon), self._names.get(index, ""))

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
        self.isochrones_visible = True
        self.traces_visible = False
        self.result_ref = None
        self.loaded_result = None
        self.result_error = None
        self.contour_error = None
        self.points = [(48.1, -123.1), (48.2, -123.2)]
        self._names = {}
        self._roundings = {}
        self.provenance = {}

    def point_name(self, index):
        return self._names.get(index, "")

    def __iter__(self):
        return iter(self.points)

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
    def __init__(self, *args):
        super().__init__(*args)
        self.load_calls = []
        self._active = None

    def save(self):
        pass

    def get_active(self):
        return self._active

    def set_active(self, element):
        self._active = element

    def new_element(self, points=None):
        element = FakeTrack(f"track-{len(self) + 1}", "New track")
        element.points = list(points or [])
        self.append(element)
        return element

    def create(self, coordinates):
        poi = FakePOI(f"poi-{len(self) + 1}", "New POI")
        poi.position = tuple(coordinates)
        self.append(poi)
        return poi

    def get_unique_name(self, name):
        existing = {item.name for item in self}
        if name not in existing:
            return name
        index = 1
        while f"{name}.{index:03d}" in existing:
            index += 1
        return f"{name}.{index:03d}"

    def load_result(self, route):
        self.load_calls.append(route)
        error = getattr(route, "result_error", None)
        if error is not None:
            raise error
        return getattr(route, "loaded_result", None)


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


def panel_for(
    tmp_path,
    tracks=(),
    routes=(),
    pois=(),
    logs=(),
    loaded_log=None,
    undo_stack=None,
):
    core = FakeCore()
    core.trackManager.extend(tracks)
    core.routingManager.extend(routes)
    core.poiManager.extend(pois)
    return ScenePanel(
        FakeController(core, FakeLibrary(logs), FakeSession(loaded_log)),
        scene_path=tmp_path / "scene.json",
        undo_stack=undo_stack,
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


def test_panel_has_no_collection_or_group_buttons(tmp_path):
    application()
    panel = panel_for(tmp_path, tracks=[FakeTrack("t1", "Morning")])

    assert not hasattr(panel, "new_button")
    assert not hasattr(panel, "group_button")
    labels = [button.text() for button in panel.findChildren(QPushButton)]
    assert "+ Collection" not in labels
    assert "Group Selection" not in labels


def test_new_collection_action_remains_available(tmp_path):
    application()
    panel = panel_for(tmp_path, tracks=[FakeTrack("t1", "Morning")])

    with patch.object(
        panel.scene, "create_group", wraps=panel.scene.create_group
    ) as create_group:
        panel._new_collection()

    create_group.assert_called_once()


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

    legs = panel.tree_item(("routing-legs", KIND_ROUTING, "r1"))
    mark = legs.child(0)
    assert mark.text(NAME_COLUMN) == "Mark 1"
    assert mark.data(NAME_COLUMN, ROUNDING_ROLE) == "Sail"
    assert mark.data(NAME_COLUMN, COORDINATE_ROLE) == "48.100000, -123.100000"


def test_route_isochrone_toggle_is_independent_of_route_visibility(tmp_path):
    application()
    route = FakeRoute("r1", "Route")
    panel = panel_for(tmp_path, routes=[route])
    emitted = []
    panel.changed.connect(lambda: emitted.append(True))
    routing_events = []
    panel.routing_visibility_changed.connect(lambda *_: routing_events.append(True))
    row = panel.item_row(KIND_ROUTING, "r1")
    row.setExpanded(True)

    isochrones = panel.tree_item(("routing-isochrones", KIND_ROUTING, "r1"))
    assert isochrones is not None
    assert isochrones.checkState(VISIBLE_COLUMN) == Qt.CheckState.Checked
    assert isochrones.flags() & Qt.ItemFlag.ItemIsUserCheckable

    isochrones.setCheckState(VISIBLE_COLUMN, Qt.CheckState.Unchecked)

    assert route.isochrones_visible is False
    assert route.visible is True
    assert panel.scene.item(KIND_ROUTING, "r1").visible is True
    assert emitted == [True]
    assert routing_events == [True]

    row.setCheckState(VISIBLE_COLUMN, Qt.CheckState.Unchecked)

    assert route.visible is False
    assert len(routing_events) == 2


def test_isochrone_row_reports_unavailable_artifact(tmp_path):
    application()
    route = FakeRoute("r1", "Route")
    route.contour_error = "OSError: artifact unavailable"
    panel = panel_for(tmp_path, routes=[route])
    row = panel.item_row(KIND_ROUTING, "r1")
    row.setExpanded(True)

    isochrones = panel.tree_item(("routing-isochrones", KIND_ROUTING, "r1"))
    assert isochrones.text(NAME_COLUMN) == "Isochrones (unavailable)"
    # The box never claims contours are present when the artifact is unreadable.
    assert isochrones.checkState(VISIBLE_COLUMN) == Qt.CheckState.Unchecked
    assert not (isochrones.flags() & Qt.ItemFlag.ItemIsUserCheckable)
    tooltip = isochrones.toolTip(NAME_COLUMN)
    assert "Isochrones unavailable" in tooltip
    assert "artifact unavailable" in tooltip
    # The persisted flag survives the unavailable presentation, so a later
    # successful read restores the user's choice.
    assert route.isochrones_visible is True
    isochrones.setCheckState(VISIBLE_COLUMN, Qt.CheckState.Checked)
    assert route.isochrones_visible is True


def test_route_traces_toggle_is_independent_of_isochrones_and_route(
    tmp_path,
):
    application()
    route = FakeRoute("r1", "Route")
    panel = panel_for(tmp_path, routes=[route])
    emitted = []
    panel.changed.connect(lambda: emitted.append(True))
    routing_events = []
    panel.routing_visibility_changed.connect(lambda *_: routing_events.append(True))
    row = panel.item_row(KIND_ROUTING, "r1")
    row.setExpanded(True)

    isochrones = panel.tree_item(("routing-isochrones", KIND_ROUTING, "r1"))
    traces = panel.tree_item(("routing-traces", KIND_ROUTING, "r1"))
    assert traces is not None
    assert traces.parent() is row
    assert traces.text(NAME_COLUMN) == "Traces"
    # Default off: the trace web hides until asked for, in every saved route.
    assert traces.checkState(VISIBLE_COLUMN) == Qt.CheckState.Unchecked
    assert traces.flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert isochrones.checkState(VISIBLE_COLUMN) == Qt.CheckState.Checked

    traces.setCheckState(VISIBLE_COLUMN, Qt.CheckState.Checked)

    assert route.traces_visible is True
    assert route.isochrones_visible is True
    assert route.visible is True
    assert panel.scene.item(KIND_ROUTING, "r1").visible is True
    assert emitted == [True]
    assert routing_events == [True]

    isochrones.setCheckState(VISIBLE_COLUMN, Qt.CheckState.Unchecked)

    assert route.isochrones_visible is False
    assert route.traces_visible is True
    assert (
        panel.tree_item(("routing-traces", KIND_ROUTING, "r1")).checkState(
            VISIBLE_COLUMN
        )
        == Qt.CheckState.Checked
    )


def test_traces_row_is_a_sibling_overlay_not_a_scene_item(tmp_path):
    application()
    panel = panel_for(tmp_path, routes=[FakeRoute("r1", "Route")])
    row = panel.item_row(KIND_ROUTING, "r1")
    row.setExpanded(True)
    traces = panel.tree_item(("routing-traces", KIND_ROUTING, "r1"))

    # The traces row groups one route's overlay; it never becomes a scene item.
    assert traces is not None
    assert traces.parent() is row
    assert traces is not panel.item_row(KIND_ROUTING, "r1")


def test_routing_marks_wrapper_is_labelled_marks(tmp_path):
    application()
    panel = panel_for(tmp_path, routes=[FakeRoute("r1", "Route")])
    panel.item_row(KIND_ROUTING, "r1").setExpanded(True)

    # The wrapper's immediate children are marks, so the label names them even
    # though calculated legs are appended below once it is expanded.
    marks = panel.tree_item(("routing-legs", KIND_ROUTING, "r1"))
    assert marks is not None
    assert marks.text(NAME_COLUMN) == "Marks"
    tooltip = marks.toolTip(NAME_COLUMN)
    assert tooltip.startswith("Route marks.")
    assert "calculated legs" in tooltip
    assert "Legs" not in tooltip


def test_legacy_route_marks_live_under_legs_wrapper_without_artifact(tmp_path):
    application()
    route = FakeRoute("r1", "Route")
    panel = panel_for(tmp_path, routes=[route])
    row = panel.item_row(KIND_ROUTING, "r1")
    row.setExpanded(True)

    assert panel.tree_item(("routing-isochrones", KIND_ROUTING, "r1")) is not None
    legs = panel.tree_item(("routing-legs", KIND_ROUTING, "r1"))
    assert legs is not None
    assert legs.childCount() == 2
    assert legs.child(0).text(NAME_COLUMN) == "Mark 1"
    assert legs.child(0).flags() & Qt.ItemFlag.ItemIsEditable
    assert panel.core.routingManager.load_calls == []

    row.setExpanded(False)
    row.setExpanded(True)
    legs = panel.tree_item(("routing-legs", KIND_ROUTING, "r1"))
    assert legs.childCount() == 2


def test_drop_on_legs_does_not_move_a_scene_item_to_root(tmp_path):
    application()
    panel = panel_for(tmp_path, routes=[FakeRoute("r1", "Route")])
    row = panel.item_row(KIND_ROUTING, "r1")
    row.setExpanded(True)
    legs = panel.tree_item(("routing-legs", KIND_ROUTING, "r1"))
    calls = []
    panel._move_to = lambda *args: calls.append(args)
    panel.tree.itemAt = lambda _position: legs

    class Event:
        def source(self):
            return panel.tree

        def position(self):
            return SimpleNamespace(toPoint=lambda: None)

        def ignore(self):
            calls.append("ignored")

        def acceptProposedAction(self):
            calls.append("accepted")

    panel.drop_scene_items(Event())

    assert calls == ["ignored"]


def test_expanding_legs_reads_calculated_result_legs(tmp_path):
    application()
    route = FakeRoute("r1", "Route")
    route.result_ref = "artifact"
    route.loaded_result = SimpleNamespace(
        legs=(
            SimpleNamespace(
                start=SimpleNamespace(lat=48.1, lon=-123.1),
                end=SimpleNamespace(lat=48.2, lon=-123.2),
            ),
            SimpleNamespace(
                start=SimpleNamespace(lat=48.2, lon=-123.2),
                end=SimpleNamespace(lat=48.3, lon=-123.3),
            ),
        )
    )
    panel = panel_for(tmp_path, routes=[route])
    row = panel.item_row(KIND_ROUTING, "r1")
    row.setExpanded(True)

    legs = panel.tree_item(("routing-legs", KIND_ROUTING, "r1"))
    assert legs is not None
    assert legs.childCount() == 2
    assert panel.core.routingManager.load_calls == []

    legs.setExpanded(True)

    assert legs.childCount() == 4
    assert legs.child(2).text(NAME_COLUMN).startswith("Leg 1:")
    assert legs.child(2).data(NAME_COLUMN, COORDINATE_ROLE) == (
        "48.10000, -123.10000 → 48.20000, -123.20000"
    )
    assert legs.child(3).text(NAME_COLUMN).startswith("Leg 2:")
    assert panel.core.routingManager.load_calls == [route]

    legs.setExpanded(False)
    legs.setExpanded(True)

    assert legs.childCount() == 4
    assert panel.core.routingManager.load_calls == [route]


def test_legs_wrapper_reports_unavailable_result(tmp_path):
    application()
    route = FakeRoute("r1", "Route")
    route.result_ref = "artifact"
    route.result_error = ValueError("artifact missing")
    panel = panel_for(tmp_path, routes=[route])
    row = panel.item_row(KIND_ROUTING, "r1")
    row.setExpanded(True)

    legs = panel.tree_item(("routing-legs", KIND_ROUTING, "r1"))
    legs.setExpanded(True)

    assert legs.childCount() == 3
    assert legs.child(2).text(NAME_COLUMN) == "Result unavailable: artifact missing"


def test_refresh_prunes_legs_expansion_for_removed_route(tmp_path):
    application()
    route = FakeRoute("r1", "Route")
    route.result_ref = "artifact"
    route.loaded_result = SimpleNamespace(legs=())
    panel = panel_for(tmp_path, routes=[route])
    row = panel.item_row(KIND_ROUTING, "r1")
    row.setExpanded(True)
    panel.tree_item(("routing-legs", KIND_ROUTING, "r1")).setExpanded(True)

    panel.core.routingManager.clear()
    panel.refresh()

    assert (KIND_ROUTING, "r1") not in panel._expanded_legs
    assert panel.item_row(KIND_ROUTING, "r1") is None


def test_point_row_rects_never_overlap_icon_or_each_other(tmp_path):
    application()
    panel = panel_for(tmp_path)
    metrics = QFontMetrics(panel.tree.font())
    values = ["01/01 00:00", "45°", "12.5", "6.4", "270°"]

    for width in (200, 600):
        rect = QRect(0, 0, width - 8, 20)
        icon, label, rounding = row_geometry(rect, metrics, 20)
        segments = mark_columns(rect, metrics, values, label.left(), rounding)

        assert icon.right() <= label.left()
        assert rounding.left() >= label.left()
        assert rounding.right() == rect.right()
        for _text, segment in segments:
            assert segment.left() >= label.left()
            assert segment.right() < rounding.left()
        for earlier, later in zip(segments, segments[1:]):
            assert later[1].left() - earlier[1].right() - 1 == MARK_DETAIL_GAP
        if segments:
            assert segments[0][1].left() - label.left() >= MARK_MIN_LABEL


def test_narrow_point_row_drops_details_instead_of_overlapping(tmp_path):
    application()
    panel = panel_for(tmp_path)
    metrics = QFontMetrics(panel.tree.font())
    values = ["01/01 00:00", "45°", "12.5", "6.4", "270°"]

    narrow = QRect(0, 0, 96, 20)
    _, label, rounding = row_geometry(narrow, metrics, 20)
    assert mark_columns(narrow, metrics, values, label.left(), rounding) == []

    wide = QRect(0, 0, 760, 20)
    _, wide_label, wide_rounding = row_geometry(wide, metrics, 20)
    segments = mark_columns(wide, metrics, values, wide_label.left(), wide_rounding)
    assert [text for text, _ in segments] == values


def test_rounding_slot_is_fixed_and_anchored_to_the_row_edge(tmp_path):
    application()
    panel = panel_for(tmp_path)
    metrics = QFontMetrics(panel.tree.font())
    rect = QRect(0, 0, 400, 20)

    sail = row_geometry(rect, metrics, 20)
    stbd = row_geometry(rect, metrics, 20)

    # The rounding slot is sized for the widest short label and pinned to the
    # row's right edge, so every point row starts it at the same x.
    assert sail[2].left() == stbd[2].left()
    assert sail[2].width() == stbd[2].width() == rounding_column_width(metrics)
    assert sail[2].right() == stbd[2].right() == rect.right()
    assert rounding_column_width(metrics) >= max(
        metrics.horizontalAdvance(label) for label in ROUNDING_SHORT.values()
    )


def test_rounding_colours_distinguish_sides(tmp_path):
    application()

    port = rounding_color(ROUNDING_SHORT[ROUNDING_PORT])
    starboard = rounding_color(ROUNDING_SHORT[ROUNDING_STARBOARD])
    sail = rounding_color(ROUNDING_SHORT[ROUNDING_SAIL_TO])
    gate = rounding_color(ROUNDING_SHORT[ROUNDING_GATE])

    assert port.name() != starboard.name()
    assert sail.name() == gate.name()
    assert sail.name() != port.name()
    assert sail.name() != starboard.name()
    # Sail to mark and Gate name no side, so they stay neutral rather than
    # borrowing the accent colour.
    assert sail.name() == MUTED.name()
    assert gate.name() == MUTED.name()


def test_rounding_slot_position_ignores_detail_fields(tmp_path):
    application()
    panel = panel_for(tmp_path)
    metrics = QFontMetrics(panel.tree.font())
    rect = QRect(0, 0, 400, 20)

    _, label, rounding = row_geometry(rect, metrics, 20)
    segments = mark_columns(
        rect, metrics, ["01/01 00:00", "45°"], label.left(), rounding
    )

    # The rounding slot comes from the row alone: details grow leftward into
    # the space the coordinate slot used to hold and never move it.
    assert segments
    assert all(segment.right() < rounding.left() for _, segment in segments)
    assert row_geometry(rect, metrics, 20)[2] == rounding


class PointRectRecorder(SceneItemDelegate):
    """Records the real option rect the delegate receives for point rows."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rects = {}

    def paint(self, painter, option, index):
        node = index.data(NODE_ROLE)
        if isinstance(node, tuple) and node[:1] == ("point",):
            self.rects[node] = QRect(option.rect)
        super().paint(painter, option, index)


def rendered_point_rects(panel, track, width):
    """Point-row rects from rendering the panel at a given width."""
    tree = panel.tree
    recorder = PointRectRecorder(tree)
    tree.setItemDelegateForColumn(NAME_COLUMN, recorder)
    panel.resize(width, 320)
    panel.show()
    QApplication.processEvents()
    panel.item_row(KIND_TRACK, track.uid).setExpanded(True)
    QApplication.processEvents()
    tree.grab()
    return recorder.rects


def rendered_routing_mark_rects(panel, route, width):
    """Routing mark-row rects from rendering the panel at a given width."""
    tree = panel.tree
    recorder = PointRectRecorder(tree)
    tree.setItemDelegateForColumn(NAME_COLUMN, recorder)
    panel.resize(width, 320)
    panel.show()
    QApplication.processEvents()
    panel.item_row(KIND_ROUTING, route.uid).setExpanded(True)
    panel.tree_item(("routing-legs", KIND_ROUTING, route.uid)).setExpanded(True)
    QApplication.processEvents()
    tree.grab()
    return recorder.rects


def test_narrow_point_row_keeps_a_readable_name(tmp_path):
    application()
    track = FakeTrack("t1", "Morning")
    track.set_point_rounding(0, ROUNDING_PORT)
    panel = panel_for(tmp_path, tracks=[track])
    metrics = QFontMetrics(panel.tree.font())

    rect = rendered_point_rects(panel, track, 200)[("point", KIND_TRACK, "t1", 0)]
    # The real rect has already lost the tree indentation and the visibility
    # column, so it is the narrow row a synthetic full-width rect hid.
    assert rect.width() < 100
    paint_rect = rect.adjusted(4, 0, -4, 0)
    _, label_rect, rounding_rect = row_geometry(paint_rect, metrics, 0)
    label_width = point_label_width(label_rect, rounding_rect)
    label = metrics.elidedText("Morning", Qt.TextElideMode.ElideRight, label_width)

    assert label_width >= POINT_MIN_LABEL
    assert label and label != "…"
    assert metrics.horizontalAdvance(label) <= label_width
    assert label_rect.left() + label_width <= rounding_rect.left()
    # The rounding label is dropped before the name shrinks.
    assert rounding_rect.width() == 0


def test_point_row_shows_name_then_rounding_label(tmp_path):
    application()
    track = FakeTrack("t1", "Morning")
    track.set_point_rounding(0, ROUNDING_PORT)
    track.set_point_rounding(1, ROUNDING_STARBOARD)
    panel = panel_for(tmp_path, tracks=[track])
    metrics = QFontMetrics(panel.tree.font())
    slot_width = rounding_column_width(metrics)

    measured = {}
    for width in (200, 224, 304, 400, 600):
        rects = rendered_point_rects(panel, track, width)
        first = rects[("point", KIND_TRACK, "t1", 0)]
        second = rects[("point", KIND_TRACK, "t1", 1)]
        paint_rect = first.adjusted(4, 0, -4, 0)
        _, label_rect, rounding_rect = row_geometry(paint_rect, metrics, 0)
        label_width = point_label_width(label_rect, rounding_rect)
        label = metrics.elidedText("Morning", Qt.TextElideMode.ElideRight, label_width)
        measured[width] = (paint_rect.width(), label_width, rounding_rect)

        # The name survives at every width and never reaches the rounding slot.
        assert label and label != "…"
        assert label_width >= POINT_MIN_LABEL
        assert metrics.horizontalAdvance(label) <= label_width
        assert label_rect.left() + label_width <= rounding_rect.left()

        # Rows at one width share the rounding slot's x, whatever their name.
        other = second.adjusted(4, 0, -4, 0)
        assert row_geometry(other, metrics, 0)[2].left() == rounding_rect.left()

        if rounding_rect.width():
            assert rounding_rect.width() == slot_width
            assert rounding_rect.right() == paint_rect.right()

    assert measured[200][2].width() == 0
    assert measured[600][2].width() == slot_width
    for width, (paint_width, _, rounding_rect) in measured.items():
        # The rounding slot is drawn exactly when it still fits beside the
        # name's floor, and never at the name's expense.
        fits = (
            paint_width - min(POINT_MIN_LABEL, paint_width) - ROUNDING_GAP >= slot_width
        )
        assert bool(rounding_rect.width()) == fits, width


def test_track_marks_carry_side_colours(tmp_path):
    application()
    track = FakeTrack("t1", "Morning")
    track.set_point_rounding(0, ROUNDING_PORT)
    track.set_point_rounding(1, ROUNDING_STARBOARD)
    panel = panel_for(tmp_path, tracks=[track])
    row = panel.item_row(KIND_TRACK, "t1")

    row.setExpanded(True)

    port = rounding_color(row.child(0).data(NAME_COLUMN, ROUNDING_ROLE))
    starboard = rounding_color(row.child(1).data(NAME_COLUMN, ROUNDING_ROLE))
    assert port.name() != starboard.name()


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


def test_edit_point_can_clear_full_rounding_spec_to_sail_to_mark(tmp_path):
    application()
    track = FakeTrack("t1", "Morning")
    track._roundings[0] = ROUNDING_GATE
    track.point_rounding_spec = lambda index: (
        {
            "side": "port",
            "radius_nm": 2.0,
        }
        if index == 0
        else None
    )
    track.set_point_rounding_spec = lambda index, spec: False
    panel = panel_for(tmp_path, tracks=[track])

    class Dialog:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def point_name(self):
            return ""

        def point_rounding(self):
            return ROUNDING_SAIL_TO

        def point_rounding_spec(self):
            return None

    with patch("sailworks.qt.scenepanel.PointEditDialog", Dialog):
        panel._edit_point(("point", KIND_TRACK, "t1", 0))

    assert track.point_rounding(0) == ROUNDING_SAIL_TO


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


def route_with_points(name="routing-J120-hugo-1"):
    """Fake route carrying positions, so its navigation summary is non-empty."""
    route = FakeRoute("r1", name)
    route.points = [
        (48.0, -123.0, "2026-01-01T00:00:00", None, None, 6.0),
        (48.5, -123.5, "2026-01-01T01:00:00", None, None, 6.0),
        (49.0, -124.0, "2026-01-01T02:00:00", None, None, 6.0),
    ]
    return route


def route_with_detail_points():
    """Route whose points carry the serialized figures the table shows."""
    route = FakeRoute("r1", "Detailed")
    route.points = [
        (48.0, -123.0, "2026-01-01T00:00:00", 45.0, 12.5, 6.4, 270.0),
        (48.5, -123.5, "2026-01-01T01:00:00", 50.0, 14.0, 7.1, 280.0),
        (49.0, -124.0, "2026-01-01T02:00:00", 55.0, 15.5, 8.2, 290.0),
    ]
    return route


def test_routing_row_tooltip_shows_navigation_summary(tmp_path):
    application()
    route = route_with_points()
    panel = panel_for(tmp_path, routes=[route])
    row = panel.item_row(KIND_ROUTING, "r1")

    tooltip = row.toolTip(NAME_COLUMN)
    summary = routing_summary(route)

    # The summary alone: the row already shows the name and the icon the type.
    assert summary
    assert tooltip == summary
    assert "routing-J120-hugo-1" not in tooltip
    assert "Routing" not in tooltip
    assert "waypoints" not in tooltip
    assert not tooltip.endswith("\n")


def test_point_row_tooltip_shows_side_and_coordinate(tmp_path):
    application()
    route = FakeRoute("r1", "Route")
    route.set_point_rounding(0, ROUNDING_PORT)
    panel = panel_for(tmp_path, routes=[route])
    row = panel.item_row(KIND_ROUTING, "r1")

    row.setExpanded(True)
    marks = panel.tree_item(("routing-legs", KIND_ROUTING, "r1"))
    tooltip = marks.child(0).toolTip(NAME_COLUMN)

    assert tooltip == "Leave to port\n48.100000, -123.100000"
    assert "Mark 1" not in tooltip
    assert not tooltip.endswith("\n")


def row_image(panel):
    """Rendered pixels of the scene tree, for glyph-level assertions."""
    image = panel.tree.grab().toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    return bytes(image.constBits())


def test_point_row_paints_name_and_rounding_without_coordinates(tmp_path):
    application()
    track = FakeTrack("t1", "Morning")
    track.set_point_rounding(0, ROUNDING_PORT)
    panel = panel_for(tmp_path, tracks=[track])

    rect = rendered_point_rects(panel, track, 700)[("point", KIND_TRACK, "t1", 0)]
    assert rect.width() > 100
    child = panel.item_row(KIND_TRACK, track.uid).child(0)
    coordinates = child.data(NAME_COLUMN, COORDINATE_ROLE)
    assert coordinates == "51.123457, -1.234568"
    # The coordinates moved from the row into its tooltip.
    assert coordinates in child.toolTip(NAME_COLUMN)

    painted = row_image(panel)
    with QSignalBlocker(panel.tree):
        child.setData(NAME_COLUMN, COORDINATE_ROLE, "10.000000, 10.000000")
    # Changing the coordinate data paints nothing: the row draws no glyphs for
    # it, so the two renderings are pixel-identical.
    assert row_image(panel) == painted

    with QSignalBlocker(panel.tree):
        child.setData(NAME_COLUMN, COORDINATE_ROLE, coordinates)
        child.setText(NAME_COLUMN, "")
    assert row_image(panel) != painted

    with QSignalBlocker(panel.tree):
        child.setText(NAME_COLUMN, "Morning")
        child.setData(NAME_COLUMN, ROUNDING_ROLE, None)
    assert row_image(panel) != painted


def test_routing_detail_fields_match_the_summary(tmp_path):
    application()
    route = route_with_points()

    fields = routing_detail_fields(route)

    assert [label for label, _ in fields] == ["distance", "duration", "speed"]
    # One source: the tree segments are exactly the summary's figures.
    assert [value for _, value in fields] == routing_summary(route).split(" | ")
    distance = sum(
        utils.point_distance(
            route.points[index - 1][0],
            route.points[index - 1][1],
            route.points[index][0],
            route.points[index][1],
        )
        for index in range(1, len(route.points))
    )
    assert fields[0][1] == "%.0f nm" % distance
    assert fields[1][1] == "2h 00m"
    assert fields[2][1] == "avg %.1f kts" % (distance / 2)


def test_track_detail_fields_show_mark_count_then_distance(tmp_path):
    application()
    points = [(51.0, -1.0), (51.5, -1.5), (52.0, -2.0)]

    fields = track_detail_fields(points)

    assert [label for label, _ in fields] == ["marks", "distance"]
    assert fields[0][1] == "3 marks"
    distance = utils.point_distance(51.0, -1.0, 51.5, -1.5) + utils.point_distance(
        51.5, -1.5, 52.0, -2.0
    )
    assert fields[1][1] == "%.1f nm" % distance


def test_scene_rows_carry_detail_fields_for_their_type(tmp_path):
    application()
    route = route_with_points()
    track = FakeTrack("t1", "Morning")
    track.points = [(51.0, -1.0), (51.5, -1.5), (52.0, -2.0)]
    panel = panel_for(
        tmp_path, tracks=[track], routes=[route], pois=[FakePOI("p1", "Harbor")]
    )

    route_values = [value for _, value in routing_detail_fields(route)]
    track_values = [value for _, value in track_detail_fields(track.points)]

    assert panel.item_row(KIND_ROUTING, "r1").data(NAME_COLUMN, DETAIL_ROLE) == (
        route_values
    )
    assert panel.item_row(KIND_TRACK, "t1").data(NAME_COLUMN, DETAIL_ROLE) == (
        track_values
    )
    assert panel.item_row(KIND_POI, "p1").data(NAME_COLUMN, DETAIL_ROLE) is None


def test_point_rows_keep_rounding_and_coordinate_data_without_detail_fields(tmp_path):
    application()
    panel = panel_for(tmp_path, routes=[route_with_points()])
    row = panel.item_row(KIND_ROUTING, "r1")

    row.setExpanded(True)
    child = panel.tree_item(("routing-legs", KIND_ROUTING, "r1")).child(0)

    assert child.data(NAME_COLUMN, DETAIL_ROLE) is None
    # The data stays on the item even though the row no longer paints it.
    assert child.data(NAME_COLUMN, COORDINATE_ROLE) == "48.000000, -123.000000"
    assert child.data(NAME_COLUMN, ROUNDING_ROLE) == ROUNDING_SHORT[ROUNDING_SAIL_TO]
    assert child.toolTip(NAME_COLUMN) == "Sail to mark\n48.000000, -123.000000"


def test_routing_mark_rows_carry_the_table_figures(tmp_path):
    application()
    route = route_with_detail_points()
    panel = panel_for(tmp_path, routes=[route])
    row = panel.item_row(KIND_ROUTING, "r1")

    row.setExpanded(True)
    marks = panel.tree_item(("routing-legs", KIND_ROUTING, "r1"))

    # The table's display strings, shared through routing_point_cells.
    assert list(routing_point_cells(route.points[0])) == [
        "01/01 00:00",
        "45°",
        "12.5",
        "6.4",
        "270°",
    ]
    for index, point in enumerate(route.points):
        assert marks.child(index).data(NAME_COLUMN, DETAIL_ROLE) == list(
            routing_point_cells(point)
        )
        # The row paints the clock time; the tooltip keeps the full stamp.
        assert marks.child(index).data(NAME_COLUMN, MARK_DETAIL_ROLE) == list(
            routing_point_tree_cells(point)
        )
        assert routing_point_cells(point)[0] in marks.child(index).toolTip(NAME_COLUMN)
    assert [label for label, _ in routing_point_detail_fields(route.points[0])] == [
        "time",
        "twa",
        "tws",
        "speed",
        "brg",
    ]


# The delegate allows a 16px decoration plus a 4px gap before the label.
ROUTING_MARK_ICON_WIDTH = 20
# The shipped default width of the left panel stack (window.py) that holds the
# scene tree; a mark row's columns are laid out against the tree's own width.
DEFAULT_LEFT_STACK_WIDTH = 480


def routing_mark_segments(panel, route, width, index=0):
    """Detail segments the delegate draws on a rendered routing mark row.

    ``row_geometry`` and ``mark_columns`` are the delegate's own
    layout, fed the rect the tree really renders.
    """
    metrics = QFontMetrics(panel.tree.font())
    rects = rendered_routing_mark_rects(panel, route, width)
    rect = rects[("point", KIND_ROUTING, route.uid, index)]
    paint_rect = rect.adjusted(4, 0, -4, 0)
    point = route.points[index]
    _, label_rect, rounding_rect = row_geometry(
        paint_rect, metrics, ROUTING_MARK_ICON_WIDTH
    )
    values = list(routing_point_tree_cells(point))
    segments = mark_columns(
        paint_rect, metrics, values, label_rect.left(), rounding_rect
    )
    return paint_rect, metrics, label_rect, rounding_rect, segments, values


def test_routing_mark_details_adapt_to_the_panel_width(tmp_path):
    application()
    route = route_with_detail_points()
    panel = panel_for(tmp_path, routes=[route])
    expected = list(routing_point_tree_cells(route.points[0]))
    # The painted time cell is the time of day; the item data keeps the date.
    assert expected[0] == "00:00"
    assert list(routing_point_cells(route.points[0]))[0] == "01/01 00:00"

    measured = {}
    for width in (960, 700, 480, 360):
        paint_rect, metrics, label_rect, rounding_rect, segments, values = (
            routing_mark_segments(panel, route, width)
        )
        measured[width] = (paint_rect.width(), segments)
        assert values == expected
        texts = [text for text, _ in segments]
        # Kept fields are a prefix: the lowest priority drops first.
        assert texts == expected[: len(texts)]

        label_width = point_label_width(label_rect, rounding_rect)
        assert label_width >= MARK_MIN_LABEL
        label = metrics.elidedText("Mark 1", Qt.TextElideMode.ElideRight, label_width)
        assert label and label != "…"
        assert metrics.horizontalAdvance(label) <= label_width

        if segments:
            # The details and the rounding slot never overlap, and the name
            # keeps its floor clear of the highest priority field.
            assert rounding_rect.width() > 0
            assert segments[-1][1].right() < rounding_rect.left()
            assert segments[0][1].left() - label_rect.left() >= MARK_MIN_LABEL
            widths = mark_column_widths(metrics)
            for index, (text, segment) in enumerate(segments):
                # Each segment is a fixed column sized for its widest expected
                # value, so its right edge is shared by every row.
                assert segment.width() == widths[index]
                assert segment.width() >= metrics.horizontalAdvance(text)
            for earlier, later in zip(segments, segments[1:]):
                assert later[1].left() - earlier[1].right() - 1 == MARK_DETAIL_GAP

    # The coordinate slot's removal lets the fields appear far narrower: the
    # whole block at 700 and most of it already at 480.
    assert len(measured[960][1]) == len(expected)
    assert len(measured[700][1]) == len(expected)
    assert len(measured[480][1]) > 0
    assert len(measured[360][1]) > 0
    counts = [len(measured[width][1]) for width in (960, 700, 480, 360)]
    assert counts == sorted(counts, reverse=True)

    # A row narrower than the name floor keeps the name and drops everything
    # else rather than painting over it.
    _, _, narrow_label, narrow_rounding, narrow_segments, _ = routing_mark_segments(
        panel, route, 200
    )
    assert narrow_segments == []
    assert point_label_width(narrow_label, narrow_rounding) < POINT_MIN_LABEL


def test_default_panel_paints_mark_columns_with_aligned_headers(tmp_path):
    """The shipped 300 px left stack and the width that carries the full grid."""
    application()
    route = route_with_detail_points()
    route.set_point_rounding(0, ROUNDING_PORT)
    panel = panel_for(tmp_path, routes=[route])
    values = list(routing_point_tree_cells(route.points[0]))
    indent = panel.tree.indentation()

    paint_rect, metrics, label_rect, rounding_rect, segments, painted = (
        routing_mark_segments(panel, route, DEFAULT_LEFT_STACK_WIDTH)
    )
    assert painted == values
    # At the shipped default width the name, the rounding label and all five
    # fields paint together; the time is the clock time, not the date.
    assert [text for text, _ in segments] == values
    assert values[0] == "00:00"
    assert point_label_width(label_rect, rounding_rect) >= MARK_MIN_LABEL
    assert segments[0][1].left() >= label_rect.left() + MARK_MIN_LABEL
    assert segments[-1][1].right() < rounding_rect.left()
    assert rounding_rect.width() == rounding_column_width(metrics)
    # Every painted field keeps its full column width, so the value's right
    # edge is the same on every row.
    widths = mark_column_widths(metrics)
    for index, (text, segment) in enumerate(segments):
        assert segment.width() == widths[index]
        assert segment.width() >= metrics.horizontalAdvance(text)

    # Headers on the expanded Marks row sit at the same right edges as the
    # values beneath them, and drop with a column the row cannot afford.
    for width in (300, DEFAULT_LEFT_STACK_WIDTH):
        row_rect, row_metrics, _, row_rounding, row_segments, _ = routing_mark_segments(
            panel, route, width
        )
        headers = mark_header_columns(
            row_rect.adjusted(-indent, 0, 0, 0),
            row_metrics,
            ROUTING_MARK_ICON_WIDTH,
            indent,
        )
        assert [header for header, _ in headers] == list(
            MARK_COLUMN_HEADERS[: len(row_segments)]
        ) + [MARK_ROUNDING_HEADER]
        for (_, header), (_, column) in zip(headers[:-1], row_segments):
            assert header == column
        assert headers[-1][1] == row_rounding

    # The tree really places the Marks row one indent left of its mark rows.
    marks = panel.tree_item(("routing-legs", KIND_ROUTING, route.uid))
    marks_rect = panel.tree.visualItemRect(marks)
    assert marks_rect.left() + 4 == paint_rect.left() - indent

    # Narrower than the default the grid drops whole columns from the right,
    # and the widest stack setting keeps them all. 465 px is the scene panel
    # left when the stack's vertical scrollbar is showing.
    layout = {}
    for width in (300, 465, DEFAULT_LEFT_STACK_WIDTH, 560):
        _, _, _, _, columns, _ = routing_mark_segments(panel, route, width)
        layout[width] = [text for text, _ in columns]
    assert layout[465] == values
    assert layout[DEFAULT_LEFT_STACK_WIDTH] == values
    assert layout[560] == values
    assert layout[300] == values[: len(layout[300])]
    assert 0 < len(layout[300]) < len(values)


def test_routing_mark_row_keeps_name_and_rounding_beside_details(tmp_path):

    application()
    route = route_with_detail_points()
    route.set_point_rounding(0, ROUNDING_PORT)
    panel = panel_for(tmp_path, routes=[route])
    paint_rect, metrics, label_rect, rounding_rect, segments, values = (
        routing_mark_segments(panel, route, 480)
    )

    assert values
    assert segments
    # Details take the room the coordinate slot used to hold; the rounding
    # label keeps the row's right edge and the name keeps its floor.
    assert rounding_rect.width() == rounding_column_width(metrics)
    assert rounding_rect.right() == paint_rect.right()
    assert segments[-1][1].right() < rounding_rect.left()
    assert point_label_width(label_rect, rounding_rect) >= POINT_MIN_LABEL


def detail_width(metrics, values, label_left, count):
    """Row width that fits exactly ``count`` detail values plus the label."""
    widths = [metrics.horizontalAdvance(value) for value in values[:count]]
    return label_left + DETAIL_MIN_LABEL + sum(widths) + DETAIL_GAP * (count - 1)


def test_narrow_row_keeps_only_the_highest_priority_detail(tmp_path):
    application()
    panel = panel_for(tmp_path)
    metrics = QFontMetrics(panel.tree.font())
    label_left = 24
    cases = (
        (["108 nm", "2h 00m", "avg 54.0 kts"], "108 nm"),
        (["3 marks", "21.4 nm"], "3 marks"),
    )

    for values, first in cases:
        narrow = QRect(0, 0, detail_width(metrics, values, label_left, 1) + 2, 20)
        wide = QRect(
            0, 0, detail_width(metrics, values, label_left, len(values)) + 2, 20
        )

        assert [
            text for text, _ in detail_columns(narrow, metrics, values, label_left)
        ] == [first]
        assert [
            text for text, _ in detail_columns(wide, metrics, values, label_left)
        ] == values


def test_detail_segments_never_overlap_the_label_or_each_other(tmp_path):
    application()
    panel = panel_for(tmp_path)
    metrics = QFontMetrics(panel.tree.font())
    label_left = 24
    values = ["108 nm", "2h 00m", "avg 54.0 kts"]

    for count in (1, 2, 3):
        rect = QRect(0, 0, detail_width(metrics, values, label_left, count) + 2, 20)
        segments = detail_columns(rect, metrics, values, label_left)

        assert len(segments) == count
        assert segments[-1][1].right() == rect.right()
        assert segments[0][1].left() - label_left >= DETAIL_MIN_LABEL
        for earlier, later in zip(segments, segments[1:]):
            assert later[1].left() - earlier[1].right() - 1 == DETAIL_GAP


def test_application_stylesheet_styles_tooltips(tmp_path):
    # The installed application sheet starts with the tooltip rule, which
    # replaces the native white frame with the panel surface and border.
    rule = TOOLTIP_STYLE

    assert rule.startswith("QToolTip")
    assert "background:" in rule
    assert "border: 1px solid #454545" in rule
    assert "white" not in rule
    assert "#ffffff" not in rule.lower()


def shown_panel(tmp_path, **kwargs):
    panel = panel_for(tmp_path, **kwargs)
    panel.resize(360, 420)
    panel.show()
    QApplication.processEvents()
    return panel


def row_tooltip_event(panel, item):
    """Tooltip event over ``item``, positioned inside the visible viewport."""
    panel.tree.scrollToItem(item)
    QApplication.processEvents()
    point = panel.tree.visualItemRect(item).center()
    assert panel.tree.itemAt(point) is item
    return QHelpEvent(
        QEvent.Type.ToolTip,
        point,
        panel.tree.viewport().mapToGlobal(point),
    )


def deliver_tooltip(panel, item):
    """Send a tooltip event to the tree viewport; True means it was consumed."""
    return QApplication.sendEvent(panel.tree.viewport(), row_tooltip_event(panel, item))


def test_scene_tooltip_is_a_frameless_tooltip_window():
    application()
    tooltip = SceneTooltip()

    assert isinstance(tooltip, QFrame)
    assert tooltip.windowType() == Qt.WindowType.ToolTip
    assert tooltip.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert tooltip.frameShape() == QFrame.Shape.NoFrame
    # Never takes focus, so showing it cannot disturb the tree selection.
    assert tooltip.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert tooltip.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)


def test_scene_tooltip_uses_the_panel_theme():
    application()
    style = SceneTooltip().styleSheet()

    assert PANEL in style
    assert TEXT in style
    assert BORDER in style
    assert "border-radius" in style
    assert "padding" in style


def test_scene_tooltip_shows_on_routing_row_and_suppresses_native(tmp_path):
    application()
    route = route_with_points()
    panel = shown_panel(tmp_path, routes=[route])
    row = panel.item_row(KIND_ROUTING, "r1")

    assert deliver_tooltip(panel, row) is True

    assert panel.tree.tooltip.isVisible()
    assert panel.tree.tooltip.label.text() == row.toolTip(NAME_COLUMN)
    assert panel.tree.tooltip.label.text() == routing_summary(route)
    assert "waypoints" not in panel.tree.tooltip.label.text()
    assert not QToolTip.isVisible()


def test_scene_tooltip_suppresses_the_native_tooltip(tmp_path):
    application()
    panel = shown_panel(tmp_path, routes=[route_with_points()])
    row = panel.item_row(KIND_ROUTING, "r1")

    assert panel.tree.viewportEvent(row_tooltip_event(panel, row)) is True
    assert panel.tree.tooltip.isVisible()
    assert not QToolTip.isVisible()


def test_scene_tooltip_shows_on_point_row(tmp_path):
    application()
    route = FakeRoute("r1", "Route")
    route.set_point_rounding(0, ROUNDING_PORT)
    panel = shown_panel(tmp_path, routes=[route])
    row = panel.item_row(KIND_ROUTING, "r1")
    row.setExpanded(True)
    point = panel.tree_item(("routing-legs", KIND_ROUTING, "r1")).child(0)

    assert deliver_tooltip(panel, point) is True

    shown = panel.tree.tooltip.label.text()
    assert panel.tree.tooltip.isVisible()
    assert shown == point.toolTip(NAME_COLUMN)
    assert shown == "Leave to port\n48.100000, -123.100000"


def test_scene_tooltip_hides_when_the_pointer_leaves_or_moves(tmp_path):
    application()
    panel = shown_panel(tmp_path, routes=[route_with_points()])
    row = panel.item_row(KIND_ROUTING, "r1")
    viewport = panel.tree.viewport()

    assert deliver_tooltip(panel, row) is True
    QApplication.sendEvent(viewport, QEvent(QEvent.Type.Leave))
    assert not panel.tree.tooltip.isVisible()

    assert deliver_tooltip(panel, row) is True
    point = viewport.rect().center()
    # Synthetic mouse events are routed by QApplication, so hand this one to
    # the viewport directly, as the scroll area does for real moves.
    move = QMouseEvent(
        QEvent.Type.MouseMove,
        point,
        viewport.mapToGlobal(point),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    panel.tree.viewportEvent(move)
    assert not panel.tree.tooltip.isVisible()


def test_scene_tooltip_hides_on_scroll_and_when_the_tree_hides(tmp_path):
    application()
    panel = shown_panel(tmp_path, routes=[route_with_points()])
    row = panel.item_row(KIND_ROUTING, "r1")
    viewport = panel.tree.viewport()

    assert deliver_tooltip(panel, row) is True
    point = viewport.rect().center()
    QApplication.sendEvent(
        viewport,
        QWheelEvent(
            point,
            viewport.mapToGlobal(point),
            QPoint(0, 0),
            QPoint(0, -120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        ),
    )
    assert not panel.tree.tooltip.isVisible()

    assert deliver_tooltip(panel, row) is True
    panel.tree.hide()
    assert not panel.tree.tooltip.isVisible()


def test_scene_tooltip_is_kept_on_screen():
    application()
    tooltip = SceneTooltip()
    tooltip.label.setText("Route\n3 waypoints\nWind 6 kn")
    tooltip.adjustSize()
    screen = QGuiApplication.primaryScreen().availableGeometry()
    outside = QPoint(screen.right() + 400, screen.bottom() + 400)

    position = tooltip._clamped_position(outside)

    assert screen.contains(QRect(position, tooltip.sizeHint()))


def test_scene_tooltip_is_offset_from_the_pointer():
    application()
    tooltip = SceneTooltip()
    screen = QGuiApplication.primaryScreen().availableGeometry()
    anchor = screen.center()

    tooltip.show_text("Route\n3 waypoints\nWind 6 kn", anchor)
    try:
        shown = tooltip.pos()
        assert shown.x() >= anchor.x() + TOOLTIP_OFFSET_X
        assert shown.y() >= anchor.y() + TOOLTIP_OFFSET_Y
        assert screen.contains(QRect(shown, tooltip.size()))
    finally:
        tooltip.hide()


def test_scene_tooltip_is_clamped_near_an_edge():
    application()
    tooltip = SceneTooltip()
    screen = QGuiApplication.primaryScreen().availableGeometry()
    anchor = QPoint(screen.right() - 2, screen.bottom() - 2)

    tooltip.show_text("Route\n3 waypoints\nWind 6 kn", anchor)
    try:
        assert screen.contains(QRect(tooltip.pos(), tooltip.size()))
    finally:
        tooltip.hide()


class MarkHeaderRecorder(SceneItemDelegate):
    """Records each row's real rect and the headers the delegate paints."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rects = {}
        self.headers = {}

    def paint(self, painter, option, index):
        node = index.data(NODE_ROLE)
        if isinstance(node, tuple):
            self.rects[node] = QRect(option.rect)
        super().paint(painter, option, index)

    def _paint_mark_headers(self, painter, option, index):
        self.headers[index.data(NODE_ROLE)] = [
            (text, QRect(segment))
            for text, segment in self._header_segments(option, index)
        ]
        super()._paint_mark_headers(painter, option, index)


def rendered_mark_headers(panel, route, width, expanded=True):
    """Render the panel at ``width`` and record rows plus painted headers."""
    tree = panel.tree
    recorder = MarkHeaderRecorder(tree)
    tree.setItemDelegateForColumn(NAME_COLUMN, recorder)
    panel.resize(width, 320)
    panel.show()
    QApplication.processEvents()
    panel.item_row(KIND_ROUTING, route.uid).setExpanded(True)
    panel.tree_item(("routing-legs", KIND_ROUTING, route.uid)).setExpanded(expanded)
    QApplication.processEvents()
    tree.grab()
    return recorder


def mark_value_columns(recorder, route, index, metrics):
    """The fixed columns and rounding slot for one rendered mark row."""
    rect = recorder.rects[("point", KIND_ROUTING, route.uid, index)]
    paint_rect = rect.adjusted(4, 0, -4, 0)
    _, label_rect, rounding_rect = row_geometry(
        paint_rect, metrics, ROUTING_MARK_ICON_WIDTH
    )
    values = [value for _, value in routing_point_detail_fields(route.points[index])]
    columns = mark_columns(
        paint_rect, metrics, values, label_rect.left(), rounding_rect
    )
    return columns, rounding_rect


def test_expanded_marks_row_paints_headers_aligned_with_values(tmp_path):
    application()
    route = route_with_detail_points()
    panel = panel_for(tmp_path, routes=[route])
    metrics = QFontMetrics(panel.tree.font())
    node = ("routing-legs", KIND_ROUTING, route.uid)

    recorder = rendered_mark_headers(panel, route, 700)
    headers = recorder.headers[node]
    assert [text for text, _ in headers] == list(MARK_COLUMN_HEADERS) + [
        MARK_ROUNDING_HEADER
    ]

    columns, rounding_rect = mark_value_columns(recorder, route, 0, metrics)
    assert len(headers) == len(columns) + 1
    for (_, header_rect), (_, column) in zip(headers, columns):
        assert header_rect.right() == column.right()
    assert headers[-1][1].right() == rounding_rect.right()


def test_collapsed_marks_row_paints_no_headers(tmp_path):
    application()
    route = route_with_detail_points()
    panel = panel_for(tmp_path, routes=[route])
    node = ("routing-legs", KIND_ROUTING, route.uid)

    recorder = rendered_mark_headers(panel, route, 700, expanded=False)
    assert node in recorder.rects
    assert node not in recorder.headers
    assert not panel.tree_item(node).data(NAME_COLUMN, MARK_HEADERS_ROLE)

    expanded = rendered_mark_headers(panel, route, 700)
    assert expanded.headers[node]
    panel.tree_item(node).setExpanded(False)
    QApplication.processEvents()
    assert not panel.tree_item(node).data(NAME_COLUMN, MARK_HEADERS_ROLE)


def test_mark_columns_share_one_right_edge_across_rows(tmp_path):
    application()
    route = FakeRoute("r1", "Mixed")
    route.points = [
        (48.0, -123.0, "2026-01-01T00:00:00", 91.0, 12.5, 6.4, 91.0),
        (48.5, -123.5, "2026-01-01T01:00:00", 157.0, 14.0, 7.1, 157.0),
    ]
    panel = panel_for(tmp_path, routes=[route])
    metrics = QFontMetrics(panel.tree.font())

    recorder = rendered_mark_headers(panel, route, 700)
    first, _ = mark_value_columns(recorder, route, 0, metrics)
    second, _ = mark_value_columns(recorder, route, 1, metrics)

    assert first[1][0] == "91°"
    assert second[1][0] == "157°"
    assert metrics.horizontalAdvance(first[1][0]) != metrics.horizontalAdvance(
        second[1][0]
    )
    # Same column, same slot: the right edge comes from the grid, not the text.
    assert [rect.right() for _, rect in first] == [rect.right() for _, rect in second]
    assert [rect.width() for _, rect in first] == [rect.width() for _, rect in second]


def test_narrow_marks_row_drops_a_column_and_its_header(tmp_path):
    application()
    route = route_with_detail_points()
    panel = panel_for(tmp_path, routes=[route])
    metrics = QFontMetrics(panel.tree.font())
    node = ("routing-legs", KIND_ROUTING, route.uid)

    shown_counts = []
    for width in (700, 480, 360):
        recorder = rendered_mark_headers(panel, route, width)
        columns, rounding = mark_value_columns(recorder, route, 0, metrics)
        expected = [
            (MARK_COLUMN_HEADERS[index], column_rect)
            for index, (_, column_rect) in enumerate(columns)
        ]
        expected.append((MARK_ROUNDING_HEADER, rounding))
        shown = recorder.headers[node]
        # Every header sits on a shown column, at exactly that column's right
        # edge, and a dropped column has no header left behind.
        assert [text for text, _ in shown] == [text for text, _ in expected]
        for (_, header_rect), (_, column_rect) in zip(shown, expected):
            assert header_rect.right() == column_rect.right()
        shown_counts.append(len(columns))

    # Narrowing drops columns from the right, each with its header.
    assert shown_counts[0] == len(MARK_COLUMN_HEADERS)
    assert shown_counts == sorted(shown_counts, reverse=True)
    assert shown_counts[-1] < shown_counts[0]


def test_mark_headers_never_overlap_at_any_width(tmp_path):
    application()
    route = route_with_detail_points()
    panel = panel_for(tmp_path, routes=[route])
    metrics = QFontMetrics(panel.tree.font())
    node = ("routing-legs", KIND_ROUTING, route.uid)

    measured = {}
    for width in (200, 304, 480, 700, 960):
        recorder = rendered_mark_headers(panel, route, width)
        headers = recorder.headers.get(node, [])
        columns, _ = mark_value_columns(recorder, route, 0, metrics)
        measured[width] = [text for text, _ in headers]
        # A header exists only for a column whose values are shown; the
        # rounding header stands alone when no numeric column fits.
        assert len(headers) in (len(columns), len(columns) + 1)
        for text, rect in headers:
            assert metrics.horizontalAdvance(text) <= rect.width()
        for earlier, later in zip(headers, headers[1:]):
            assert later[1].left() > earlier[1].right()

    # Columns drop from the right as the panel narrows.
    assert measured[960] == list(MARK_COLUMN_HEADERS) + [MARK_ROUNDING_HEADER]
    assert measured[200] == []
    assert len(measured[200]) < len(measured[304]) < len(measured[960])


def tree_rgb(tree, point):
    """Rendered pixel at ``point`` in the scene tree."""
    image = tree.grab().toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    color = image.pixelColor(point)
    return (color.red(), color.green(), color.blue())


def colour_rgb(color):
    return (color.red(), color.green(), color.blue())


def row_background_point(rect):
    """A point inside a row that no glyph, icon or branch arrow covers."""
    return QPoint(rect.left() + 4, rect.bottom() - 2)


def test_scene_tree_alternating_row_tint_is_subtle_and_theme_derived(tmp_path):
    application()
    panel = panel_for(tmp_path, tracks=[FakeTrack("t1", "Morning")])
    tree = panel.tree
    palette = tree.palette()
    base = palette.color(QPalette.ColorRole.Base)
    alternate = palette.color(QPalette.ColorRole.AlternateBase)

    assert tree.alternatingRowColors()
    assert base.name() == SURFACE
    assert alternate != base
    # A few percent lighter, not a visible band.
    delta = max(
        abs(first - second)
        for first, second in zip(colour_rgb(base), colour_rgb(alternate))
    )
    assert 0 < delta <= 12


def test_scene_tree_alternating_rows_paint_and_selection_wins(tmp_path):
    application()
    panel = panel_for(tmp_path, tracks=[FakeTrack("t1", "Morning")])
    tree = panel.tree
    panel.resize(480, 320)
    panel.show()
    QApplication.processEvents()
    tree.clearSelection()
    QApplication.processEvents()

    palette = tree.palette()
    base = colour_rgb(palette.color(QPalette.ColorRole.Base))
    alternate = colour_rgb(palette.color(QPalette.ColorRole.AlternateBase))

    root = tree.topLevelItem(0)
    assert root.childCount() >= 2
    first = tree.visualItemRect(root)
    second = tree.visualItemRect(root.child(0))
    third = tree.visualItemRect(root.child(1))
    assert not first.isEmpty() and not second.isEmpty() and not third.isEmpty()
    assert second.top() == first.bottom() + 1
    assert third.top() > second.bottom()

    top = tree_rgb(tree, row_background_point(first))
    below = tree_rgb(tree, row_background_point(second))
    assert top != below
    assert {top, below} == {base, alternate}

    # Selection keeps the normal highlight instead of a blend of stripe and
    # highlight.
    tree.setCurrentItem(root.child(1))
    QApplication.processEvents()
    highlight = colour_rgb(palette.color(QPalette.ColorRole.Highlight))
    selected = tree_rgb(tree, row_background_point(third))
    assert selected == highlight
    assert selected not in (base, alternate)


def context_menu_labels(panel, row):
    labels = []

    def capture(menu, *_args):
        labels.extend(action.text() for action in menu.actions())
        return None

    with (
        patch.object(panel.tree, "itemAt", lambda position: row),
        patch.object(ThemedMenu, "exec", capture),
    ):
        panel._context_menu(QPoint(0, 0))
    return labels


def assert_action(labels, label):
    texts = [text.replace("&", "") for text in labels]
    assert any(label in text for text in texts), (label, labels)


def test_track_row_context_menu_offers_add_point(tmp_path):
    application()
    panel = panel_for(tmp_path, tracks=[FakeTrack("t1", "Morning")])
    row = panel.item_row(KIND_TRACK, "t1")

    labels = context_menu_labels(panel, row)

    assert_action(labels, "Add Point")
    assert not any("Delete Point" in text for text in labels)


def test_point_row_context_menu_offers_point_editing(tmp_path):
    application()
    panel = panel_for(tmp_path, tracks=[FakeTrack("t1", "Morning")])
    row = panel.item_row(KIND_TRACK, "t1")
    row.setExpanded(True)

    labels = context_menu_labels(panel, row.child(0))

    for label in (
        "Add Point",
        "Rename Point",
        "Move Point",
        "Delete Point",
        "Edit",
    ):
        assert_action(labels, label)


def test_add_point_action_appends_to_track(tmp_path):
    application()
    track = FakeTrack("t1", "Morning")
    panel = panel_for(tmp_path, tracks=[track])
    seen = {}

    def dialog(title, default, callback):
        seen["default"] = default
        callback((53.5, -3.25))

    panel._coordinates_dialog = dialog
    panel._add_point(("item", KIND_TRACK, "t1"))

    assert len(track) == 3
    assert tuple(track.points[-1][:2]) == (53.5, -3.25)
    assert seen["default"] == (52.0, -2.0)


def test_add_point_action_creates_track_when_none_exists(tmp_path):
    application()
    panel = panel_for(tmp_path)
    panel._coordinates_dialog = lambda title, default, callback: callback((10.0, 20.0))

    panel._add_point(None)

    tracks = panel.core.trackManager
    assert len(tracks) == 1
    assert tuple(tracks[0].points[0][:2]) == (10.0, 20.0)
    assert tracks.get_active() is tracks[0]


def test_delete_point_action_is_undoable(tmp_path):
    application()
    track = FakeTrack("t1", "Morning")
    stack = QUndoStack()
    panel = panel_for(tmp_path, tracks=[track], undo_stack=stack)

    panel._delete_point(("point", KIND_TRACK, "t1", 0))

    assert len(track) == 1
    assert stack.count() == 1
    stack.undo()
    assert len(track) == 2
    assert tuple(track.points[0][:2]) == (51.1234567, -1.2345678)


def test_add_point_action_is_undoable(tmp_path):
    application()
    track = FakeTrack("t1", "Morning")
    stack = QUndoStack()
    panel = panel_for(tmp_path, tracks=[track], undo_stack=stack)
    panel._coordinates_dialog = lambda title, default, callback: callback((53.5, -3.25))

    panel._add_point(("item", KIND_TRACK, "t1"))

    assert len(track) == 3
    assert stack.count() == 1
    stack.undo()
    assert len(track) == 2
    stack.redo()
    assert len(track) == 3
    assert tuple(track.points[-1][:2]) == (53.5, -3.25)


def test_add_point_action_undo_removes_implicit_track(tmp_path):
    application()
    stack = QUndoStack()
    panel = panel_for(tmp_path, undo_stack=stack)
    panel._coordinates_dialog = lambda title, default, callback: callback((10.0, 20.0))

    panel._add_point(None)

    tracks = panel.core.trackManager
    assert len(tracks) == 1
    stack.undo()
    assert len(tracks) == 0
    stack.redo()
    assert len(tracks) == 1
    assert tuple(tracks[0].points[0][:2]) == (10.0, 20.0)


def test_rename_point_action_is_undoable(tmp_path):
    application()
    track = FakeTrack("t1", "Morning")
    stack = QUndoStack()
    panel = panel_for(tmp_path, tracks=[track], undo_stack=stack)
    before = track.point_name(0)

    panel._rename_point(("point", KIND_TRACK, "t1", 0), "Start line")

    assert track.point_name(0) == "Start line"
    assert stack.count() == 1
    stack.undo()
    assert track.point_name(0) == before
    stack.redo()
    assert track.point_name(0) == "Start line"


def test_map_request_adds_point_from_the_scene_tree(tmp_path):
    application()
    track = FakeTrack("t1", "Morning")
    panel = panel_for(tmp_path, tracks=[track])
    panel.core.trackManager.set_active(track)

    panel.add_navigation_request("point", (54.0, -4.0))

    assert tuple(track.points[-1][:2]) == (54.0, -4.0)


def test_map_request_adds_poi_from_the_scene_tree(tmp_path):
    application()
    panel = panel_for(tmp_path)

    poi = panel.add_navigation_request("poi", (54.0, -4.0))

    assert tuple(poi.position) == (54.0, -4.0)
    assert len(panel.core.poiManager) == 1


def test_window_navigation_request_uses_scene_panel(tmp_path):
    from sailworks.qt.window import PreviewWindow

    application()
    track = FakeTrack("t1", "Morning")
    panel = panel_for(tmp_path, tracks=[track])
    panel.core.trackManager.set_active(track)
    shown = []

    window = SimpleNamespace(scene_panel=panel, _show_panel=shown.append)

    PreviewWindow._navigation_request(window, "point", (55.0, -5.0))

    assert tuple(track.points[-1][:2]) == (55.0, -5.0)
    assert shown == ["scene"]
