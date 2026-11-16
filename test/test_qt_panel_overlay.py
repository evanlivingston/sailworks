"""Permanent overlay sidebars that float above the central views."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (  # noqa: E402
    QLabel,
    QVBoxLayout,
    QWidget,
)

from sailworks.qt.collapsiblepanel import CollapsiblePanel  # noqa: E402
from sailworks.qt.paneloverlay import (  # noqa: E402
    PANEL_MAX_WIDTH,
    PANEL_MIN_WIDTH,
    RESIZE_HANDLE_WIDTH,
    PanelOverlay,
)
from sailworks.qt.panelstack import PanelStack  # noqa: E402
from sailworks.qt.window import PreviewWindow  # noqa: E402


def _build_overlay(qtbot, width=1200, height=600):
    overlay = PanelOverlay(QWidget(), QWidget(), QWidget())
    overlay.resize(width, height)
    qtbot.addWidget(overlay)
    overlay.show()
    qtbot.wait(10)
    return overlay


def test_views_keep_full_geometry_when_widths_change(qtbot):
    overlay = _build_overlay(qtbot)
    before = overlay.views.geometry()

    overlay.set_side_width("left", 420)
    overlay.set_side_width("right", 500)

    assert overlay.views.geometry() == before
    assert before.width() == overlay.width()
    assert before.height() == overlay.height()


def test_side_stacks_sit_at_the_overlay_edges(qtbot):
    overlay = _build_overlay(qtbot)
    overlay.set_side_width("left", 420)
    overlay.set_side_width("right", 500)

    left = overlay.stack("left")
    right = overlay.stack("right")
    assert left.x() == 0
    assert left.width() == 420
    assert right.x() + right.width() == overlay.width()
    assert right.width() == 500


def test_closed_side_is_hidden_at_zero_width(qtbot):
    overlay = _build_overlay(qtbot)
    overlay.set_side_open("left", False)

    stack = overlay.stack("left")
    assert stack.width() == 0
    assert stack.isHidden()
    assert not overlay.handles["left"].isVisible()
    assert overlay.side_width("left") > 0
    # The other side is untouched and still draggable.
    assert overlay.stack("right").width() >= PANEL_MIN_WIDTH
    assert overlay.handles["right"].isVisible()


def test_width_is_clamped_to_the_panel_range(qtbot):
    overlay = _build_overlay(qtbot)

    assert overlay.set_side_width("left", 10) == PANEL_MIN_WIDTH
    assert overlay.set_side_width("left", 5000) == PANEL_MAX_WIDTH
    assert overlay.side_width("left") == PANEL_MAX_WIDTH
    assert overlay.stack("left").width() == PANEL_MAX_WIDTH


def test_width_leaves_room_for_the_map(qtbot):
    overlay = _build_overlay(qtbot, width=800)

    applied = overlay.set_side_width("right", PANEL_MAX_WIDTH)

    assert applied == 800 - 300
    assert overlay.stack("right").width() == applied
    assert overlay.views.width() == 800


def test_handles_sit_on_the_inner_edges(qtbot):
    overlay = _build_overlay(qtbot)
    overlay.set_side_width("left", 420)
    overlay.set_side_width("right", 500)

    left = overlay.handles["left"]
    right = overlay.handles["right"]
    assert left.x() == 420 - RESIZE_HANDLE_WIDTH
    assert left.width() == RESIZE_HANDLE_WIDTH
    assert right.x() == overlay.width() - 500
    assert right.height() == overlay.height()


def test_resize_handle_drag_updates_width_and_emits(qtbot):
    overlay = _build_overlay(qtbot)
    emitted = []
    overlay.widths_changed.connect(lambda: emitted.append(True))

    handle = overlay.handles["left"]
    handle.start_drag(0)
    handle.drag_to(120)
    handle.end_drag()

    assert overlay.side_width("left") == PANEL_MIN_WIDTH + 120
    assert emitted == [True]


def test_right_handle_grows_when_dragged_inward(qtbot):
    overlay = _build_overlay(qtbot)

    handle = overlay.handles["right"]
    handle.start_drag(1000)
    handle.drag_to(880)
    handle.end_drag()

    assert overlay.side_width("right") == PANEL_MIN_WIDTH + 120


def test_handle_drag_is_clamped(qtbot):
    overlay = _build_overlay(qtbot)

    handle = overlay.handles["left"]
    handle.start_drag(0)
    handle.drag_to(-400)
    handle.end_drag()
    assert overlay.side_width("left") == PANEL_MIN_WIDTH

    handle.start_drag(0)
    handle.drag_to(1000)
    handle.end_drag()
    assert overlay.side_width("left") == PANEL_MAX_WIDTH


class _StateHost(QWidget):
    """Minimal host that borrows the window's panel persistence."""

    _panel_side = PreviewWindow._panel_side
    _panel_visible = PreviewWindow._panel_visible
    panel_state = PreviewWindow.panel_state
    restore_panel_state = PreviewWindow.restore_panel_state

    def _position_drawer_flags(self, *_):
        pass


def _make_state_host(qtbot):
    host = _StateHost()
    host.resize(1200, 600)
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    left = PanelStack()
    right = PanelStack()
    views = QWidget()
    overlay = PanelOverlay(views, left, right)
    overlay.set_side_width("left", 480)
    overlay.set_side_width("right", 380)
    layout.addWidget(overlay)
    host.panels = {
        "scene": CollapsiblePanel("Scene", QLabel("scene")),
        "nmea": CollapsiblePanel("NMEA", QLabel("nmea")),
    }
    left.add_panel(host.panels["scene"])
    right.add_panel(host.panels["nmea"])
    host._panel_home = {"scene": "left", "nmea": "right"}
    host._panel_stacks = {"left": left, "right": right}
    host._panel_overlay = overlay
    host.views = views
    host._panel_stack_width = {"left": 480, "right": 380}
    host._panel_stack_open = {"left": True, "right": True}
    host.panel_actions = {}
    host._default_panel_state = host.panel_state()
    qtbot.addWidget(host)
    host.show()
    qtbot.wait(10)
    return host


def test_panel_state_has_no_splitter(qtbot):
    host = _make_state_host(qtbot)

    state = host.panel_state()

    assert "splitter" not in state
    assert state["width"] == {"left": 480, "right": 380}
    assert state["open"] == {"left": True, "right": True}
    assert {entry["name"] for entry in state["panels"]} == {"scene", "nmea"}


def test_restore_applies_width_and_open(qtbot):
    host = _make_state_host(qtbot)

    host.restore_panel_state(
        {
            "open": {"left": False, "right": True},
            "width": {"left": 420, "right": 520},
            "panels": [
                {"name": "scene", "side": "left", "visible": True},
                {"name": "nmea", "side": "right", "visible": True},
            ],
        }
    )

    assert host._panel_stack_width == {"left": 420, "right": 520}
    assert host._panel_overlay.side_width("left") == 420
    assert host._panel_overlay.side_width("right") == 520
    assert not host._panel_overlay.is_open("left")
    assert host._panel_overlay.stack("left").isHidden()
    assert host._panel_overlay.stack("right").width() == 520


def test_restore_falls_back_to_legacy_splitter_sizes(qtbot):
    host = _make_state_host(qtbot)

    host.restore_panel_state(
        {
            "splitter": [450, 400, 350],
            "open": {"left": True, "right": True},
            "panels": [
                {"name": "scene", "side": "left", "visible": True},
                {"name": "nmea", "side": "right", "visible": True},
            ],
        }
    )

    assert host._panel_stack_width["left"] == 450
    assert host._panel_stack_width["right"] == 350
    assert host._panel_overlay.side_width("left") == 450
    assert host._panel_overlay.side_width("right") == 350


def test_restore_prefers_width_over_legacy_splitter(qtbot):
    host = _make_state_host(qtbot)

    host.restore_panel_state(
        {
            "splitter": [450, 400, 350],
            "width": {"left": 500, "right": 400},
            "open": {"left": True, "right": True},
        }
    )

    assert host._panel_overlay.side_width("left") == 500
    assert host._panel_overlay.side_width("right") == 400
