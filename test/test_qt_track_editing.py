"""Focused regression coverage for selected-track editing behavior."""

from types import SimpleNamespace

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QTableWidget, QTableWidgetItem

from sailworks.core.geo.track import Track
from sailworks.qt.navigationpanel import NavigationPanel


class _Tracks(list):
    def __init__(self, *tracks):
        super().__init__(tracks)
        self.active = tracks[0] if tracks else None

    def get_active(self):
        return self.active

    def set_active(self, track):
        self.active = track


class _Panel:
    """Minimal panel state for direct behavioral tests of NavigationPanel."""

    def __init__(self, track):
        self.core = SimpleNamespace(trackManager=_Tracks(track))
        self.canvas = SimpleNamespace(
            navigation=SimpleNamespace(edit_track=None, hl_track_item=None),
            invalidate_race_geometry=lambda: None,
            update=lambda: None,
        )
        self.track_buttons = [QPushButton(), QPushButton()]
        self.point_index = 0
        self.track_points = QTableWidget()
        self.tracks = QTableWidget()
        self.changed = SimpleNamespace(emit=lambda: None)

    def refresh(self):
        pass

    def refresh_points(self):
        pass


def test_track_select_edit_escape_and_race_track_restriction(qtbot):
    app = QApplication.instance()
    assert app is not None
    ordinary = Track("Editable", [(1, 2, None)])
    panel = _Panel(ordinary)

    NavigationPanel.edit_track(panel)
    assert panel.canvas.navigation.edit_track is ordinary
    assert panel.track_buttons[1].isEnabled()

    event = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier
    )
    assert NavigationPanel.eventFilter(panel, panel.tracks, event) is True
    assert event.isAccepted()
    assert panel.canvas.navigation.edit_track is None

    NavigationPanel.select_track(panel)
    assert panel.canvas.navigation.edit_track is None
    assert not panel.track_buttons[1].isEnabled()

    race = Track("Race", [(1, 2, None)])
    panel.core.trackManager = _Tracks(race)
    NavigationPanel.edit_track(panel)
    assert panel.canvas.navigation.edit_track is None
    assert not panel.track_buttons[1].isEnabled()


def test_point_modifications_require_selected_editable_track(qtbot):
    track = Track("Track", [(1, 2, None), (3, 4, None)])
    panel = _Panel(track)
    panel.move_requested = SimpleNamespace(emit=lambda callback: None)
    before = list(track.points)

    NavigationPanel.edit_point(panel, "remove")
    assert track.points == before

    panel.canvas.navigation.edit_track = track
    NavigationPanel.edit_point(panel, "remove")
    assert len(track.points) == 1

    race = Track("Race", [(1, 2, None)])
    panel.core.trackManager = _Tracks(race)
    panel.canvas.navigation.edit_track = race
    NavigationPanel.edit_point(panel, "remove")
    assert len(race.points) == 1


def test_add_point_requires_edit_mode_for_existing_tracks(qtbot):
    track = Track("Track", [(1, 2, None)])
    panel = _Panel(track)
    panel.status = SimpleNamespace(setText=lambda text: None)
    panel.tabs = SimpleNamespace(setCurrentIndex=lambda index: None)
    panel.canvas._weather_presenter = SimpleNamespace(
        invalidations=0,
        invalidate_race_geometry=lambda: setattr(
            panel.canvas._weather_presenter,
            "invalidations",
            panel.canvas._weather_presenter.invalidations + 1,
        ),
    )
    panel.canvas.invalidations = 0
    panel.canvas.invalidate_race_geometry = lambda: setattr(
        panel.canvas, "invalidations", panel.canvas.invalidations + 1
    )

    NavigationPanel.add_point(panel, (5, 6))
    assert len(track.points) == 1

    panel.canvas.navigation.edit_track = track
    NavigationPanel.add_point(panel, (5, 6))
    assert len(track.points) == 2
    assert panel.canvas.invalidations == 1
    assert panel.canvas._weather_presenter.invalidations == 1

    race = Track("Race", [(1, 2, None)])
    panel.core.trackManager = _Tracks(race)
    panel.canvas.navigation.edit_track = race
    NavigationPanel.add_point(panel, (5, 6))
    assert len(race.points) == 1


def _new_track_panel():
    panel = _Panel(None)
    manager = panel.core.trackManager
    manager.new_element = lambda points: Track("New", points)
    panel.status = SimpleNamespace(setText=lambda text: None)
    panel.tabs = SimpleNamespace(setCurrentIndex=lambda index: None)
    panel.canvas._weather_presenter = SimpleNamespace(
        invalidate_race_geometry=lambda: None
    )
    panel.canvas.invalidate_race_geometry = lambda: None
    panel._set_track_editing = lambda enabled: setattr(
        panel.canvas.navigation, "edit_track", manager.get_active() if enabled else None
    )
    panel._invalidate_race_geometry = lambda: None
    return panel


def test_new_track_enters_edit_mode_and_accepts_repeated_points(qtbot):
    panel = _new_track_panel()

    track = NavigationPanel.new_track(panel)
    assert panel.canvas.navigation.edit_track is track
    NavigationPanel.add_point(panel, (1, 2))
    NavigationPanel.add_point(panel, (3, 4))
    assert track.points == [(1, 2, None), (3, 4, None)]

    race = Track("Race", [(5, 6, None)])
    panel.core.trackManager = _Tracks(race)
    panel.canvas.navigation.edit_track = race
    NavigationPanel.add_point(panel, (7, 8))
    assert race.points == [(5, 6, None)]


def test_add_point_implicitly_creates_editable_track(qtbot):
    panel = _new_track_panel()

    NavigationPanel.add_point(panel, (1, 2))
    track = panel.core.trackManager.get_active()
    assert track is not None
    assert panel.canvas.navigation.edit_track is track
    NavigationPanel.add_point(panel, (3, 4))
    assert track.points == [(1, 2, None), (3, 4, None)]

    race = Track("Race", [(5, 6, None)])
    panel.core.trackManager = _Tracks(race)
    panel.canvas.navigation.edit_track = None
    NavigationPanel.add_point(panel, (7, 8))
    assert race.points == [(5, 6, None)]


def test_context_click_preserves_multiselection_and_selects_unselected_row(qtbot):
    from PySide6.QtWidgets import QTableWidgetItem

    table = QTableWidget(2, 1)
    qtbot.addWidget(table)
    table.setItem(0, 0, QTableWidgetItem("one"))
    table.setItem(1, 0, QTableWidgetItem("two"))
    table.selectRow(0)
    table.selectRow(1)
    NavigationPanel._select_context_item(table, table.item(0, 0))
    assert {item.row() for item in table.selectedItems()} == {0, 1}
    NavigationPanel._select_context_item(table, table.item(1, 0))
    assert {item.row() for item in table.selectedItems()} == {0, 1}
    table.clearSelection()
    NavigationPanel._select_context_item(table, table.item(1, 0))
    assert {item.row() for item in table.selectedItems()} == {1}


def test_track_list_click_and_double_click_are_wired(qtbot):
    table = QTableWidget(2, 1)
    qtbot.addWidget(table)
    ordinary = Track("Editable", [(1, 2, None)])
    race = Track("Race", [(3, 4, None)])
    manager = _Tracks(ordinary, race)
    buttons = [QPushButton(), QPushButton()]
    canvas = SimpleNamespace(
        navigation=SimpleNamespace(edit_track=None, hl_track_item=None),
        invalidate_race_geometry=lambda: None,
        update=lambda: None,
    )
    panel = SimpleNamespace(
        tracks=table,
        core=SimpleNamespace(trackManager=manager),
        canvas=canvas,
        track_buttons=buttons,
        syncing=False,
        point_index=-1,
        refresh_points=lambda: None,
        _invalidate_race_geometry=lambda: None,
        _select_track=lambda row: NavigationPanel._select_track(panel, row),
        edit_track=lambda: NavigationPanel.edit_track(panel),
    )
    for row, track in enumerate(manager):
        table.setItem(row, 0, QTableWidgetItem(track.name))
    NavigationPanel._wire_track_selection(panel)

    table.show()
    qtbot.waitExposed(table)
    ordinary_cell = table.visualItemRect(table.item(0, 0)).center()
    QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=ordinary_cell)
    QApplication.processEvents()
    assert manager.get_active() is ordinary
    assert canvas.navigation.edit_track is None
    assert not buttons[1].isEnabled()

    QTest.mouseDClick(table.viewport(), Qt.MouseButton.LeftButton, pos=ordinary_cell)
    QApplication.processEvents()
    assert canvas.navigation.edit_track is ordinary
    assert buttons[1].isEnabled()

    race_cell = table.visualItemRect(table.item(1, 0)).center()
    QTest.mouseDClick(table.viewport(), Qt.MouseButton.LeftButton, pos=race_cell)
    QApplication.processEvents()
    assert manager.get_active() is race
    assert canvas.navigation.edit_track is None
    assert not buttons[1].isEnabled()


def test_only_edited_track_gets_point_handle_circles():
    from sailworks.rendering.navigation import NavigationRenderer

    class TrackManager(list):
        def is_active(self, track):
            return True

    tracks = [
        Track("First", [(1, 2, None)]),
        Track("Second", [(3, 4, None)]),
    ]
    renderer = NavigationRenderer(
        SimpleNamespace(
            trackManager=TrackManager(tracks), poiManager=[], routingManager=[]
        )
    )
    renderer.edit_track = tracks[1]

    class Cr:
        def __init__(self):
            self.arcs = []

        def arc(self, x, y, radius, *_):
            self.arcs.append((x, y, radius))

        def __getattr__(self, _):
            return lambda *args, **kwargs: None

    cr = Cr()

    class GpsMap:
        def convert_geographic_to_screen(self, point):
            return point

    renderer.draw_navigation(GpsMap(), cr)
    assert cr.arcs == [(3.0, 4.0, 5)]
