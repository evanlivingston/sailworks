import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QRect, Qt
from PySide6.QtGui import QColor, QImage, QKeyEvent, QPainter, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QLineEdit,
    QStyle,
    QStyleOptionButton,
    QVBoxLayout,
    QWidget,
)

from sailworks.qt.dropdown import ToggleFieldRow
from sailworks.qt.theme import (
    ACCENT,
    DISABLED_SURFACE,
    DISABLED_TEXT,
    HOVER_SURFACE,
    INDICATOR_SIZE,
    SURFACE,
    TEXT,
    TRACK,
    UNCHECKED_HOVER,
    UNCHECKED_SURFACE,
    MenuBuilder,
    ThemedMenu,
    apply_theme,
)


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


def test_disabled_line_edit_outer_edge_is_not_brighter_than_its_fill():
    # A disabled field keeps the same 1 px box purely as a metrics placeholder,
    # but that border is painted in the disabled fill: visibly borderless, and
    # nothing reflows when the field is toggled on and off.
    app = application()
    apply_theme(app)
    window = QWidget()
    layout = QVBoxLayout(window)
    field = QLineEdit("0")
    field.setEnabled(False)
    layout.addWidget(field)
    window.show()
    app.processEvents()
    fill = QColor(DISABLED_SURFACE).lightness()
    edge = max(color.lightness() for color in _edge_luminances(field))
    assert window.isVisible()
    assert not field.isEnabled()
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


def _image_edge_colors(image, inset=0):
    width, height = image.width(), image.height()
    points = [(x, inset) for x in range(inset, width - inset)]
    points += [(x, height - 1 - inset) for x in range(inset, width - inset)]
    points += [(inset, y) for y in range(inset, height - inset)]
    points += [(width - 1 - inset, y) for y in range(inset, height - inset)]
    return [image.pixelColor(x, y) for x, y in points]


def _inner_colors(image, inset=2):
    # The tick lives inside the box; sampling away from the edge means a light
    # border cannot satisfy a check-mark assertion.
    return [
        image.pixelColor(x, y)
        for y in range(inset, image.height() - inset)
        for x in range(inset, image.width() - inset)
    ]


def _checkbox_image(box):
    box.setFixedSize(INDICATOR_SIZE, INDICATOR_SIZE)
    return box.grab().toImage()


def _painted_indicator(state):
    app = application()
    apply_theme(app)
    image = QImage(INDICATOR_SIZE, INDICATOR_SIZE, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    option = QStyleOptionButton()
    option.rect = QRect(0, 0, INDICATOR_SIZE, INDICATOR_SIZE)
    option.state = state
    painter = QPainter(image)
    app.style().drawPrimitive(
        QStyle.PrimitiveElement.PE_IndicatorCheckBox, option, painter
    )
    painter.end()
    return image


def test_unchecked_checkbox_indicator_has_no_light_border():
    app = application()
    apply_theme(app)
    box = QCheckBox()
    image = _checkbox_image(box)
    fill = QColor(UNCHECKED_SURFACE).lightness()
    # The box is a flat fill, so its straight edges are no brighter than the
    # fill. The corners are rounded away from the fill, so they are skipped.
    edge = _image_edge_colors(image, inset=3)
    assert max(color.lightness() for color in edge) <= fill + 3
    assert max(_rendered_luminances(box)) < 120
    # Markedly grey against the row background, not near-black.
    assert fill >= QColor(SURFACE).lightness() * 2 - 6


def test_checked_checkbox_keeps_a_visible_check_mark():
    app = application()
    apply_theme(app)
    box = QCheckBox()
    box.setChecked(True)
    image = _checkbox_image(box)
    accent = QColor(ACCENT).lightness()
    # No lighter frame around the accent box...
    assert max(color.lightness() for color in _image_edge_colors(image)) <= accent + 3
    # ...and the mark itself is inside the box.
    tick = QColor(TEXT).lightness()
    assert max(color.lightness() for color in _inner_colors(image)) >= tick - 20


def test_disabled_checkbox_indicator_is_flat_with_a_dimmed_mark():
    app = application()
    apply_theme(app)
    box = QCheckBox()
    box.setChecked(True)
    box.setEnabled(False)
    image = _checkbox_image(box)
    fill = QColor(DISABLED_SURFACE).lightness()
    assert max(color.lightness() for color in _image_edge_colors(image)) <= fill + 3
    inner = max(color.lightness() for color in _inner_colors(image))
    assert inner >= QColor(DISABLED_TEXT).lightness() - 10
    assert inner < QColor(TEXT).lightness() - 20


def test_checkbox_fills_come_from_the_theme_surfaces():
    hovered = _painted_indicator(
        QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_MouseOver
    )
    assert hovered.pixelColor(7, 7).name() == UNCHECKED_HOVER
    unchecked = _painted_indicator(QStyle.StateFlag.State_Enabled)
    assert unchecked.pixelColor(7, 7).name() == UNCHECKED_SURFACE
    # Nothing else moved: checked stays the accent, disabled the dim surface.
    # Sample beside the tick, which is painted over the centre.
    checked = QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_On
    assert _painted_indicator(checked).pixelColor(12, 7).name() == ACCENT
    assert (
        _painted_indicator(QStyle.StateFlag.State_On).pixelColor(12, 7).name()
        == DISABLED_SURFACE
    )
    assert UNCHECKED_HOVER != HOVER_SURFACE


def test_checkbox_indicator_keeps_its_fifteen_pixel_slot():
    app = application()
    apply_theme(app)
    assert (
        app.style().pixelMetric(QStyle.PixelMetric.PM_IndicatorWidth) == INDICATOR_SIZE
    )
    assert (
        app.style().pixelMetric(QStyle.PixelMetric.PM_IndicatorHeight) == INDICATOR_SIZE
    )
    box = QCheckBox()
    option = QStyleOptionButton()
    option.initFrom(box)
    rect = app.style().subElementRect(
        QStyle.SubElement.SE_CheckBoxIndicator, option, box
    )
    assert (rect.width(), rect.height()) == (INDICATOR_SIZE, INDICATOR_SIZE)
    assert box.sizeHint().width() == INDICATOR_SIZE

    # The Compare rows are as tall as their field; the checkbox fills the
    # 15 px toggle column and must not add a row.
    with_box = ToggleFieldRow("Axis", QLineEdit("0"))
    without_box = ToggleFieldRow("Axis", QLineEdit("0"), checkbox=False)
    assert with_box._checkbox.sizeHint().width() == INDICATOR_SIZE
    assert with_box.sizeHint().height() == without_box.sizeHint().height()


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
