import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QKeyEvent, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from sailworks.qt.theme import ACCENT, TRACK, MenuBuilder, ThemedMenu, apply_theme


def application():
    return QApplication.instance() or QApplication([])


def press(menu, key, text):
    menu.keyPressEvent(
        QKeyEvent(
            QEvent.Type.KeyPress,
            key,
            Qt.KeyboardModifier.NoModifier,
            text,
        )
    )


def test_disabled_inputs_resolve_to_a_dimmed_palette():
    app = application()
    apply_theme(app)
    palette = QLineEdit().palette()
    roles = (
        QPalette.ColorRole.Base,
        QPalette.ColorRole.Text,
    )
    for role in roles:
        enabled = palette.color(QPalette.ColorGroup.Active, role)
        disabled = palette.color(QPalette.ColorGroup.Disabled, role)
        assert disabled != enabled


def _rendered_luminances(box):
    image = box.grab().toImage()
    values = []
    for y in range(image.height()):
        for x in range(image.width()):
            values.append(image.pixelColor(x, y).lightness())
    return values


def _edge_luminances(box):
    image = box.grab().toImage()
    width, height = image.width(), image.height()
    points = [(x, 0) for x in range(width)]
    points += [(x, height - 1) for x in range(width)]
    points += [(0, y) for y in range(height)]
    points += [(width - 1, y) for y in range(height)]
    return [image.pixelColor(x, y) for x, y in points]


def _line_edit_pair():
    app = application()
    apply_theme(app)
    window = QWidget()
    layout = QVBoxLayout(window)
    focused = QLineEdit("0")
    unfocused = QLineEdit("0")
    layout.addWidget(focused)
    layout.addWidget(unfocused)
    window.show()
    window.activateWindow()
    focused.setFocus()
    app.processEvents()
    assert focused.hasFocus()
    return window, focused, unfocused


def test_line_edit_outer_edge_is_not_brighter_than_its_fill():
    # A styled border changes how Qt sizes the box, so the inputs match the
    # slider track with a border painted in the track colour: no visible
    # outline and no geometry change. This test fails if either half regresses.
    # Holding the window keeps the widget tree alive for the grab.
    window, _focused, unfocused = _line_edit_pair()
    fill = QColor(TRACK).lightness()
    # Tolerant of rounding and antialiasing at the corners, which may only
    # darken; anything lighter reintroduces an outline.
    edge = max(color.lightness() for color in _edge_luminances(unfocused))
    assert window.isVisible()
    assert edge <= fill + 3


def test_focused_line_edit_keeps_a_distinguishable_accent_edge():
    # Holding the window keeps the widget tree alive for the grab.
    window, focused, _unfocused = _line_edit_pair()
    accent = QColor(ACCENT)
    # Accent is much bluer than the neutral track, so the edge is compared by
    # its blue-over-red split instead of an exact colour that settings or
    # platform rounding could shift.
    edge = max(color.blue() - color.red() for color in _edge_luminances(focused))
    assert window.isVisible()
    assert edge >= accent.blue() - accent.red() - 40


def test_unchecked_checkbox_indicator_has_no_light_border():
    app = application()
    apply_theme(app)
    box = QCheckBox()
    box.setFixedSize(15, 15)
    assert max(_rendered_luminances(box)) < 120


def test_checked_checkbox_keeps_a_visible_check_mark():
    app = application()
    apply_theme(app)
    box = QCheckBox()
    box.setFixedSize(15, 15)
    box.setChecked(True)
    # Styling the checked indicator with a background or border makes Qt drop
    # the style's check glyph, leaving an accent square with no mark.
    assert max(_rendered_luminances(box)) > 150


def test_menu_builder_assigns_unique_mnemonics_and_sections():
    application()
    menu = ThemedMenu()
    builder = MenuBuilder(menu)

    first = builder.add("Select")
    second = builder.add("Select hierarchy")
    builder.section()
    third = builder.add("Delete")

    assert first.text() == "&Select"
    assert second.text() == "S&elect hierarchy"
    assert third.text() == "&Delete"
    assert menu.actions()[2].isSeparator()


def test_themed_menu_mnemonic_triggers_action_and_opens_submenu():
    application()
    menu = ThemedMenu()
    builder = MenuBuilder(menu)
    fired = []

    builder.add("Delete", lambda: fired.append("delete"), key="D")
    submenu = builder.add_menu("Move to", key="M")
    MenuBuilder(submenu).add("Scene Collection", lambda: fired.append("move"), key="S")

    press(menu, Qt.Key_D, "d")
    assert fired == ["delete"]

    press(menu, Qt.Key_M, "m")
    assert isinstance(submenu, ThemedMenu)

    press(submenu, Qt.Key_S, "s")
    assert fired == ["delete", "move"]
    submenu.close()
