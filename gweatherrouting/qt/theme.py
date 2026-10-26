"""Dark application styling aligned with the GTK workspace."""

import logging
import sys
from functools import lru_cache

from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QColor, QPalette, QIcon, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication, QLabel
from gweatherrouting.common import resource_path
from .dockchrome import install_wheel_guard

BACKGROUND = "#242424"
PANEL = "#303030"
SURFACE = "#1e1e1e"
TEXT = "#eeeeec"
MUTED = "#b0b0ad"
ACCENT = "#3584e4"
BORDER = "#454545"


logger = logging.getLogger("gweatherrouting")


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


def apply_theme(app: QApplication):
    # Fusion consistently honors the dark palette on all supported platforms.
    # Native window decorations and native filesystem dialogs remain OS-owned.
    app.setStyle("Fusion")
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
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#777774"))
    app.setPalette(palette)
    install_wheel_guard(app)
    app.setStyleSheet("""
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
            color: #c8c8c5; font-size: 16px; font-weight: 700;
            padding: 0px; }
        QToolButton#drawer-flag-left {
            border-left: none; border-top-right-radius: 6px;
            border-bottom-right-radius: 6px; }
        QToolButton#drawer-flag-right {
            border-right: none; border-top-left-radius: 6px;
            border-bottom-left-radius: 6px; }
        QToolButton#drawer-flag-left:hover, QToolButton#drawer-flag-right:hover {
            background: #3c3c3c; color: #ffffff; }
        QDockWidget { border: 1px solid #454545; background: #242424; }
        QDockWidget::title { background: #303030; padding: 7px 10px 7px 30px;
                             text-align: left; }
        QDockWidget::close-button, QDockWidget::float-button {
            border: none; padding: 0px; margin: 0px; }
        QDockWidget::close-button {
            subcontrol-origin: margin; subcontrol-position: top left;
            position: absolute; top: 0px; left: 4px; bottom: 0px;
            width: 22px; image: none; icon-size: 12px;
            color: #b0b0ad; font-weight: 700; }
        QDockWidget::close-button:hover { background: #454545; color: #ffffff; }
        QDockWidget::float-button {
            subcontrol-origin: margin; subcontrol-position: top right;
            position: absolute; top: 0px; right: 0px; bottom: 0px;
            width: 0px; height: 0px; image: none; icon-size: 0px;
            border: none; padding: 0px; margin: 0px; }
        QWidget#dock-titlebar { background: #2b2b2b;
                                border-top-left-radius: 12px;
                                border-top-right-radius: 12px;
                                border-bottom: 1px solid #454545; }
        QLabel#dock-title { color: #eeeeec; font-weight: 600;
                            background: transparent; }
        QToolButton#dock-close { background: transparent; border: none;
                                 border-radius: 8px; padding: 2px 6px;
                                 color: #b0b0ad; font-weight: 700; }
        QToolButton#dock-close:hover { background: #454545; color: #ffffff; }
        QWidget#snap-preview { background: rgba(53, 132, 228, 40);
                               border: 1px solid rgba(53, 132, 228, 160);
                               border-radius: 12px; }
        QTabBar::tab { padding: 7px 14px; background: #242424;
                       border-bottom: 2px solid transparent; }
        QTabBar::tab:selected { background: #303030; border-bottom-color: #3584e4; }
        QPushButton { min-height: 24px; padding: 4px 10px; border-radius: 6px;
                      background: #303030; border: 1px solid #454545; }
        QPushButton:hover { background: #3c3c3c; }
        QPushButton:disabled { color: #777774; background: #292929; }
        QPushButton#instruments-toggle:checked { background: #344966;
                                                  border-color: #3584e4; }
        QPushButton#instruments-toggle:checked:hover { background: #3c547a; }
        QLabel#connection-status { background: #242424; border: 1px solid #3a3a3a;
                                   border-radius: 6px; padding: 6px 8px; }
        QPushButton[primary="true"] { background: #3584e4; color: white;
                                       border: 1px solid #3584e4; }
        QPushButton[primary="true"]:hover { background: #4b93eb; }
        QPushButton[primary="true"]:disabled { background: #3a4656; color: #9b9b98; }
        QComboBox, QDateTimeEdit, QDoubleSpinBox { min-height: 24px; padding: 3px;
            border: 1px solid #454545; border-radius: 6px; background: #1e1e1e; }
        QComboBox:focus, QDateTimeEdit:focus, QDoubleSpinBox:focus { border-color: #3584e4; }
        QLabel[section="true"] { color: #eeeeec; font-weight: 600; padding-top: 6px; }
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
        QMainWindow::separator { background: #454545; width: 4px; height: 4px;
                                 border: none; }
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
    """)


def section_label(text):
    label = QLabel(text)
    label.setProperty("section", True)
    return label


@lru_cache(maxsize=32)
def symbol_icon(name):
    source = QIcon(resource_path("gweatherrouting", "data/icons/" + name + ".svg"))

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


@lru_cache(maxsize=4)
def transport_icon(action):
    pixmap = QPixmap(48, 48)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(TEXT))
    if action == "stop":
        painter.drawRect(QRectF(6, 6, 12, 12))
    elif action == "play":
        painter.drawPolygon(QPolygonF([QPointF(7, 5), QPointF(19, 12), QPointF(7, 19)]))
    elif action == "pause":
        painter.drawRect(QRectF(6, 5, 4, 14))
        painter.drawRect(QRectF(14, 5, 4, 14))
    elif action == "restart":
        painter.drawRect(QRectF(4, 5, 3, 14))
        painter.drawPolygon(
            QPolygonF([QPointF(18, 5), QPointF(9, 12), QPointF(18, 19)])
        )
    painter.end()
    return QIcon(pixmap)
