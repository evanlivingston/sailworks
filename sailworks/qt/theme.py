"""Dark application styling for the Qt workspace."""

import logging
import string
import sys
from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPalette, QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMenu,
    QProxyStyle,
    QStyle,
    QStyleFactory,
)
from sailworks.common import resource_path
from .scrollchrome import install_wheel_guard

BACKGROUND = "#242424"
PANEL = "#303030"
SURFACE = "#1e1e1e"
# Fill of an editable field, identical to the slider track in
# ``sailworks.qt.slider`` so text inputs and sliders read as the same control.
TRACK = "#2b2b2b"
TEXT = "#e6e6e6"
MUTED = "#b0b0ad"
ACCENT = "#3584e4"
BORDER = "#454545"
DISABLED_SURFACE = "#292929"
DISABLED_TEXT = "#777774"
# Dimmed accent fill for a checked control that cannot be operated.
DISABLED_ACCENT = "#3a4656"
# Hover fill of an editable field.
HOVER_SURFACE = "#323232"
# An unchecked box is a flat grey, roughly twice the row background in
# lightness, so it reads as a box rather than a black hole. Its hover is one
# step lighter; it is distinct from the field hover, which is darker.
UNCHECKED_SURFACE = "#3a3a3a"
UNCHECKED_HOVER = "#474747"
# Checkbox indicator box. The toggle field rows reserve a slot this wide, so
# the painted indicator and its metrics must keep exactly this size.
INDICATOR_SIZE = 15

# Alternating row tint for dense lists such as the scene tree. Blender's
# outliner separates rows with a barely visible band rather than a stripe, so
# the tint is a small blend toward white of the row base, not a second colour.
ROW_TINT = 0.04

# Qt chrome type scale. These name the ad-hoc sizes the chrome already used;
# each role keeps exactly the size and weight it rendered with before.
TYPE_CAPTION = "10px"  # compact glyph and caption role
TYPE_DETAIL = "12px"  # small detail and popup role
TYPE_DISCLOSURE = "16px"  # disclosure and chevron role
TYPE_WEIGHT_SEMIBOLD = "600"


def alternate_row_base(base=SURFACE, amount=ROW_TINT):
    """Row background for alternating rows, blended from ``base``.

    Deriving the colour from the theme surface keeps the tint proportional in
    any palette; a hard-coded hex would only suit this one.
    """
    color = QColor(base)
    return QColor(
        round(color.red() + (255 - color.red()) * amount),
        round(color.green() + (255 - color.green()) * amount),
        round(color.blue() + (255 - color.blue()) * amount),
    )


# Tooltip chrome: the native frame is a white outline that clashes with the
# dark panels, so tooltips use the same surface, text and border as the rest.
TOOLTIP_STYLE = (
    f"QToolTip {{ background: {PANEL}; color: {TEXT};"
    f" border: 1px solid {BORDER}; border-radius: 4px;"
    " padding: 4px 6px; }"
)


logger = logging.getLogger("sailworks")


class ThemedMenu(QMenu):
    """Menu without the native light popup frame on macOS."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)

    def _mnemonic_action(self, letter):
        for action in self.actions():
            if action.isSeparator() or not action.isEnabled():
                continue
            text = action.text()
            index = text.find("&")
            if index >= 0 and index + 1 < len(text):
                if text[index + 1].lower() == letter:
                    return action
        return None

    def keyPressEvent(self, event):  # noqa: N802 - Qt virtual method name
        modifiers = event.modifiers()
        if not modifiers & (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        ):
            text = event.text()
            if len(text) == 1 and text.isalnum():
                action = self._mnemonic_action(text.lower())
                if action is not None:
                    if action.menu() is not None:
                        self.setActiveAction(action)
                        geometry = self.actionGeometry(action)
                        action.menu().popup(self.mapToGlobal(geometry.topRight()))
                    else:
                        self.close()
                        action.trigger()
                    return
        super().keyPressEvent(event)


class MenuBuilder:
    """Add menu entries with unique underlined mnemonic keys."""

    def __init__(self, menu):
        self.menu = menu
        self._keys = set()

    def _mark(self, text, key=None):
        if "&" in text:
            return text
        letters = [char for char in text if char.isalnum()]
        if key is None:
            key = next(
                (char for char in letters if char.lower() not in self._keys),
                letters[0] if letters else None,
            )
        if key is None:
            return text
        self._keys.add(key.lower())
        index = text.lower().find(key.lower())
        if index < 0:
            return text
        return f"{text[:index]}&{text[index:]}"

    def add(self, text, callback=None, key=None):
        return self.menu.addAction(self._mark(text, key), callback)

    def add_menu(self, text, key=None):
        submenu = ThemedMenu(self.menu)
        self.menu.addMenu(submenu)
        submenu.setTitle(self._mark(text, key))
        return submenu

    def section(self):
        actions = self.menu.actions()
        if actions and not actions[-1].isSeparator():
            self.menu.addSeparator()


def _force_macos_dark_appearance() -> bool:
    """Force dark native window frames to match the dark Qt theme.

    Our palette is hard-coded dark, but on macOS the floating panels and
    window frames are drawn by the OS. When the system is in light mode
    the native frame stays white even though Qt paints a dark title bar,
    leaving a white outline around every panel. There is no Info.plist
    key to force dark (only one to force light), so request DarkAqua on
    NSApp directly. Pure ctypes/objection-runtime, no new dependency.
    Returns True when the dark appearance was applied.
    """
    if sys.platform != "darwin":
        return False
    try:
        import ctypes
        import ctypes.util

        library = ctypes.util.find_library("objc")
        if library is None:
            return False
        objc = ctypes.cdll.LoadLibrary(library)
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]

        def sel(name):
            return objc.sel_registerName(name.encode("ascii"))

        def _send(target, selector, *args, argtypes=()):
            objc.objc_msgSend.restype = ctypes.c_void_p
            objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, *argtypes]
            return objc.objc_msgSend(target, selector, *args)

        c_void = ctypes.c_void_p
        ns_string = objc.objc_getClass(b"NSString")
        name = _send(
            ns_string,
            sel("stringWithUTF8String:"),
            b"NSAppearanceNameDarkAqua",
            argtypes=[ctypes.c_char_p],
        )
        if not name:
            return False
        appearance = _send(
            objc.objc_getClass(b"NSAppearance"),
            sel("appearanceNamed:"),
            name,
            argtypes=[c_void],
        )
        if not appearance:
            return False
        nsapp = _send(objc.objc_getClass(b"NSApplication"), sel("sharedApplication"))
        if not nsapp:
            return False
        _send(nsapp, sel("setAppearance:"), appearance, argtypes=[c_void])
        return True
    except Exception as exc:  # Never break startup for a cosmetic tweak.
        logger.debug("Could not force macOS dark appearance: %s", exc)
        return False


class ThemeStyle(QProxyStyle):
    """Fusion with flat, borderless checkbox indicators.

    QSS cannot express this: any drawable rule on ``QCheckBox::indicator``
    replaces Fusion's rendering, and ``:checked`` loses the tick glyph as soon
    as the rule paints a background. So the box and the tick are painted here,
    while everything else stays with Fusion. The indicator keeps the theme's
    fixed size so the toggle field rows keep their geometry.
    """

    def __init__(self):
        super().__init__(QStyleFactory.create("Fusion"))

    def pixelMetric(self, metric, option=None, widget=None):  # noqa: N802
        if metric in (
            QStyle.PixelMetric.PM_IndicatorWidth,
            QStyle.PixelMetric.PM_IndicatorHeight,
        ):
            return INDICATOR_SIZE
        return super().pixelMetric(metric, option, widget)

    def drawPrimitive(self, element, option, painter, widget=None):  # noqa: N802
        if element == QStyle.PrimitiveElement.PE_IndicatorCheckBox:
            self._paint_checkbox(option, painter)
            return
        super().drawPrimitive(element, option, painter, widget)

    @staticmethod
    def _paint_checkbox(option, painter):
        state = option.state
        enabled = bool(state & QStyle.StateFlag.State_Enabled)
        checked = bool(state & QStyle.StateFlag.State_On)
        hovered = bool(state & QStyle.StateFlag.State_MouseOver)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # No pen at any point: the box is a flat fill like the slider track.
        painter.setPen(Qt.PenStyle.NoPen)
        if not enabled:
            fill, mark = QColor(DISABLED_SURFACE), QColor(DISABLED_TEXT)
        elif checked:
            fill, mark = QColor(ACCENT), QColor(TEXT)
        else:
            fill = QColor(UNCHECKED_HOVER if hovered else UNCHECKED_SURFACE)
            mark = None
        rect = QRectF(option.rect)
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 3, 3)

        if mark is not None:
            tick = QPen(mark, 1.7)
            tick.setCapStyle(Qt.PenCapStyle.RoundCap)
            tick.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(tick)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            left, top = rect.left(), rect.top()
            painter.drawPolyline(
                [
                    QPointF(left + 3.5, top + 7.6),
                    QPointF(left + 6.3, top + 10.4),
                    QPointF(left + 11.4, top + 4.6),
                ]
            )
        painter.restore()


# Application keeps ownership of the style; the reference only stops Python
# from collecting it while the QApplication still points at it.
_style = None

# Stored as a Qt dynamic property on the application itself rather than in a
# module-level set of ids: the property dies with the C++ QApplication, so a
# later QApplication in the same process (a new test session, an embedded
# re-exec) is correctly treated as unthemed, and no id can be reused by a
# different object.
_THEMED = "_sailworks_theme_applied"


def apply_theme(app: QApplication):
    # Fusion consistently honors the dark palette on all supported platforms.
    # Native window decorations and native filesystem dialogs remain OS-owned.
    # The proxy paints the controls QSS cannot style cleanly; the palette and
    # stylesheet are applied on top of it exactly as before.
    #
    # Re-applying is not idempotent-safe: once the stylesheet below is
    # installed Qt wraps the base style in a QStyleSheetStyle, and replacing
    # the base style makes Qt polish through the old wrapper's baseStyle()
    # pointer while the previous ThemeStyle is being released.
    if app.property(_THEMED):
        return
    global _style
    style = ThemeStyle()
    app.setStyle(style)
    # Publish the Python reference only after Qt has finished swapping styles.
    # Assigning the global first dropped the last reference to the previous
    # style before setStyle walked the widget tree through the freed base.
    _style = style
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    _force_macos_dark_appearance()
    palette = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, BACKGROUND),
        (QPalette.ColorRole.WindowText, TEXT),
        (QPalette.ColorRole.Base, SURFACE),
        (QPalette.ColorRole.AlternateBase, PANEL),
        (QPalette.ColorRole.Text, TEXT),
        (QPalette.ColorRole.Button, PANEL),
        (QPalette.ColorRole.ButtonText, TEXT),
        (QPalette.ColorRole.Highlight, ACCENT),
        (QPalette.ColorRole.HighlightedText, "#ffffff"),
        (QPalette.ColorRole.ToolTipBase, PANEL),
        (QPalette.ColorRole.ToolTipText, TEXT),
        (QPalette.ColorRole.Link, "#78aeed"),
        (QPalette.ColorRole.PlaceholderText, MUTED),
    ):
        palette.setColor(role, QColor(color))
    for role in (
        QPalette.ColorRole.Text,
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.PlaceholderText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(DISABLED_TEXT))
    for role in (
        QPalette.ColorRole.Base,
        QPalette.ColorRole.Button,
        QPalette.ColorRole.AlternateBase,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(DISABLED_SURFACE))
    app.setPalette(palette)
    install_wheel_guard(app)
    app.setStyleSheet(
        TOOLTIP_STYLE
        + string.Template("""
        QToolBar { spacing: 8px; padding: 5px 8px; border: none;
                   border-bottom: 1px solid #454545; }
        QToolBar QToolButton { padding: 5px 9px; border-radius: 6px; }
        QToolBar QToolButton:hover { background: #3c3c3c; }
        QToolBar QToolButton:pressed { background: #454545; }
        QToolBar QToolButton:checked { background: #344966;
                                       border: 1px solid #3584e4; }
        QToolBar QToolButton:disabled { color: #777774; background: #292929; }
        QWidget#forecast-timeline-overlay QToolButton {
            padding: 0px; border: none; border-radius: 6px;
            background: transparent; }
        QWidget#forecast-timeline-overlay QToolButton:hover {
            background: #3c3c3c; }
        QWidget#forecast-timeline-overlay QToolButton:pressed {
            background: #454545; }
        QWidget#forecast-timeline-overlay QToolButton:checked {
            background: #344966; border: 1px solid #3584e4; }
        QToolButton#drawer-flag-left, QToolButton#drawer-flag-right {
            background: #303030; border: 1px solid #454545;
            color: #c8c8c5; font-size: $disclosure; font-weight: $semibold;
            padding: 0px; }
        QToolButton#drawer-flag-left {
            border-left: none; border-top-right-radius: 6px;
            border-bottom-right-radius: 6px; }
        QToolButton#drawer-flag-right {
            border-right: none; border-top-left-radius: 6px;
            border-bottom-left-radius: 6px; }
        QToolButton#drawer-flag-left:hover, QToolButton#drawer-flag-right:hover {
            background: #3c3c3c; color: #ffffff; }
        QWidget#collapsible-panel, QWidget#panel-stack,
        QWidget#panel-stack-container { background: #242424; border: none; }
        QWidget#panel-header { background: #2b2b2b; border: none; }
        QLabel#panel-title { color: #e6e6e6; font-weight: $semibold;
                             background: transparent; }
        QToolButton#panel-collapse { background: transparent; border: none;
                                     border-radius: 6px; padding: 1px 3px; }
        QToolButton#panel-collapse:hover { background: #454545; }
        QToolButton#panel-close { background: transparent; border: none;
                                  border-radius: 6px; padding: 1px 4px;
                                  color: #b0b0ad; font-weight: $semibold; }
        QToolButton#panel-close:hover { background: #454545; color: #ffffff; }
        QWidget#panel-header[dragging="true"] { background: #344966; }
        QFrame#panel-drop-gap { background: rgba(53, 132, 228, 24);
                                border: 1px dashed #3584e4; }
        QWidget#snap-preview { background: rgba(53, 132, 228, 40);
                               border: 1px solid rgba(53, 132, 228, 160);
                               border-radius: 12px; }
        QTabBar::tab { padding: 7px 14px; background: #242424;
                       border-bottom: 2px solid transparent; }
        QTabBar::tab:selected { background: #303030; border-bottom-color: #3584e4; }
        QPushButton { min-height: 24px; padding: 4px 10px; border-radius: 6px;
                      background: #303030; border: 1px solid #454545; }
        QPushButton:hover { background: #3c3c3c; }
        QPushButton:pressed { background: #242424; border-color: #3584e4; }
        QPushButton:disabled { color: #777774; background: #292929; }
        QPushButton#instruments-toggle:checked { background: #344966;
                                                  border-color: #3584e4; }
        QPushButton#instruments-toggle:checked:hover { background: #3c547a; }
        QLabel#connection-status { background: #242424; border: 1px solid #3a3a3a;
                                   border-radius: 6px; padding: 6px 8px; }
        QPushButton[primary="true"] { background: #3584e4; color: white;
                                       border: 1px solid #3584e4; }
        QPushButton[primary="true"]:hover { background: #4b93eb; }
        QPushButton[primary="true"]:pressed { background: #2d6fc0; }
        QPushButton[primary="true"]:disabled { background: $disabled_accent;
                                               color: #9b9b98; }
        QPushButton[quiet="true"] { background: transparent; border-color: #3a3a3a;
                                    color: #b0b0ad; min-height: 20px; padding: 3px 10px; }
        QPushButton[quiet="true"]:hover { background: #303030; color: #e6e6e6; }
        QPushButton[quiet="true"]:pressed { background: #242424;
                                            border-color: #3584e4; }
        QPushButton[quiet="true"]:disabled { background: transparent;
                                             border-color: #303030; color: #666663; }
        QComboBox, QDateTimeEdit, QDoubleSpinBox, QSpinBox { min-height: 24px;
            padding: 3px; border: 1px solid $track; border-radius: 3px;
            background: $track; outline: none; }
        QLineEdit { padding: 2px 4px; border: 1px solid $track;
            border-radius: 3px; background: $track; color: $text; }
        QComboBox:hover { background: $hover_surface;
            border-color: $hover_surface; }
        QLineEdit:focus, QComboBox:focus, QDateTimeEdit:focus,
        QDoubleSpinBox:focus, QSpinBox:focus {
            border-color: $accent; outline: none; }
        QComboBox QAbstractItemView { background: #1e1e1e; color: #e6e6e6;
            border: none; outline: 0; padding: 0;
            selection-background-color: #3584e4; selection-color: #ffffff; }
        QComboBoxPrivateContainer { border: none; background: #1e1e1e; }
        QMenu { background: #303030; border: none; padding: 4px; }
        QMenu::item { padding: 4px 18px 4px 10px; border-radius: 4px; }
        QMenu::item:selected { background: #3584e4; color: #ffffff; }
        QMenu::item:disabled { color: #777774; }
        QMenu::separator { height: 1px; background: #454545; margin: 4px 8px; }
        QComboBox QAbstractItemView::item { padding: 4px 6px; border: none;
            background: transparent; }
        QComboBox QAbstractItemView::item:hover { background: #3c3c3c;
            color: #ffffff; }
        QComboBox QAbstractItemView::item:selected { background: #3584e4;
            color: #ffffff; }
        QLineEdit#placeSearchEdit { min-height: 24px; padding: 3px 8px;
            border: 1px solid $track; border-radius: 3px;
            background: $track; color: $text; }
        QLineEdit#placeSearchEdit:focus { border-color: $accent; }
        QListView#placeSearchResults { background: #242424; color: #e6e6e6;
            border: 1px solid #454545; border-radius: 6px;
            outline: 0; padding: 2px; }
        QListView#placeSearchResults::item { padding: 4px 6px; }
        QListView#placeSearchResults::item:selected {
            background: #3584e4; color: #ffffff; }
        QLabel[section="true"] { color: #e6e6e6; font-weight: $semibold; padding-top: 6px; }
        QLabel[secondary="true"] { color: #b0b0ad; }
        QProgressBar { border: 1px solid #454545; border-radius: 4px;
                       background: #1e1e1e; min-height: 8px; text-align: center; }
        QProgressBar::chunk { background: #3584e4; border-radius: 3px; }
        QToolButton[transport="true"] { background: #303030; border: 1px solid #454545;
                                        border-radius: 0; padding: 0; }
        QToolButton[transport="true"]:hover { background: #454545; }
        QToolButton[transport="true"]:checked { background: #344966; }
        QToolButton#record:checked { background: #623535; border-color: #e34b4b; }
        QToolButton[transport="true"]:disabled { background: #292929; }
        QHeaderView::section { background: #303030; padding: 2px 4px;
                              border: none; }
        QTableWidget { border: none; gridline-color: #303030; }
        QTableWidget::item { padding: 0px; }
        QSplitter::handle { background: #454545; height: 5px; width: 5px; }
        QSplitter#panel-stack-splitter::handle { background: #242424;
                                                 width: 3px; }
        QTabWidget::pane { border: none; background: #242424;
                           margin: 0px; padding: 0px; }
        QTabBar { background: #242424; border: none; }
        QScrollArea { background: transparent; border: none; padding: 0px; }
        QScrollBar:vertical { background: transparent; width: 6px; margin: 0px;
                              border: none; }
        QScrollBar::handle:vertical { background: #5a5a5a; min-height: 24px;
                                      border-radius: 3px; }
        QScrollBar::handle:vertical:hover { background: #777774; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
            height: 0px; border: none; background: none; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
            background: none; }
        QScrollBar:horizontal { background: transparent; height: 6px; margin: 0px;
                                border: none; }
        QScrollBar::handle:horizontal { background: #5a5a5a; min-width: 24px;
                                        border-radius: 3px; }
        QScrollBar::handle:horizontal:hover { background: #777774; }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
            width: 0px; border: none; background: none; }
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
            background: none; }
        QStatusBar { border-top: 1px solid #454545; }
        QLineEdit:disabled, QLineEdit#placeSearchEdit:disabled,
        QComboBox:disabled, QDateTimeEdit:disabled,
        QDoubleSpinBox:disabled, QSpinBox:disabled,
        QAbstractSpinBox:disabled {
            background: $disabled_surface; color: $disabled_text;
            border-color: $disabled_surface; }
    """).substitute(
            caption=TYPE_CAPTION,
            detail=TYPE_DETAIL,
            disclosure=TYPE_DISCLOSURE,
            semibold=TYPE_WEIGHT_SEMIBOLD,
            disabled_surface=DISABLED_SURFACE,
            disabled_text=DISABLED_TEXT,
            disabled_accent=DISABLED_ACCENT,
            surface=SURFACE,
            border=BORDER,
            accent=ACCENT,
            track=TRACK,
            hover_surface=HOVER_SURFACE,
            text=TEXT,
        )
    )
    app.setProperty(_THEMED, True)


def section_label(text):
    label = QLabel(text)
    label.setProperty("section", True)
    return label


@lru_cache(maxsize=32)
def symbol_icon(name):
    source = QIcon(resource_path("sailworks", "data/icons/" + name + ".svg"))

    def tinted(color):
        pixmap = source.pixmap(48, 48)
        pixmap.setDevicePixelRatio(2)
        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), color)
        painter.end()
        return pixmap

    palette = QApplication.palette()
    icon = QIcon(tinted(palette.color(QPalette.ColorRole.Text)))
    disabled = tinted(QColor("#777774"))
    icon.addPixmap(disabled, QIcon.Mode.Disabled)
    return icon
