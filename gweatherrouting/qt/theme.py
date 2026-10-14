"""Dark application styling aligned with the GTK workspace."""

from functools import lru_cache

from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QColor, QPalette, QIcon, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication, QLabel
from gweatherrouting.common import resource_path

BACKGROUND = "#242424"
PANEL = "#303030"
SURFACE = "#1e1e1e"
TEXT = "#eeeeec"
MUTED = "#b0b0ad"
ACCENT = "#3584e4"
BORDER = "#454545"


def apply_theme(app: QApplication):
    # Fusion consistently honors the dark palette on all supported platforms.
    # Native window decorations and native filesystem dialogs remain OS-owned.
    app.setStyle("Fusion")
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
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
    app.setStyleSheet("""
        QToolBar { spacing: 8px; padding: 5px 8px; border: none;
                   border-bottom: 1px solid #454545; }
        QToolBar QToolButton { padding: 5px 9px; border-radius: 6px; }
        QDockWidget { border: 1px solid #454545; }
        QDockWidget::title { background: #303030; padding: 7px 10px; }
        QTabBar::tab { padding: 7px 14px; background: #242424;
                       border-bottom: 2px solid transparent; }
        QTabBar::tab:selected { background: #303030; border-bottom-color: #3584e4; }
        QPushButton { min-height: 24px; padding: 4px 10px; border-radius: 6px;
                      background: #303030; border: 1px solid #454545; }
        QPushButton:hover { background: #3c3c3c; }
        QPushButton:disabled { color: #777774; background: #292929; }
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
        QHeaderView::section { background: #303030; padding: 5px; border: none;
                              border-right: 1px solid #454545; }
        QTableWidget { border: 1px solid #454545; gridline-color: #3b3b3b; }
        QSplitter::handle { background: #454545; height: 5px; width: 5px; }
        QStatusBar { border-top: 1px solid #454545; }
    """)


def section_label(text):
    label = QLabel(text)
    label.setProperty("section", True)
    return label


@lru_cache(maxsize=32)
def symbol_icon(name):
    pixmap = QIcon(
        resource_path("gweatherrouting", "data/icons/" + name + ".svg")
    ).pixmap(48, 48)
    pixmap.setDevicePixelRatio(2)
    painter = QPainter(pixmap)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(
        pixmap.rect(), QApplication.palette().color(QPalette.ColorRole.Text)
    )
    painter.end()
    return QIcon(pixmap)


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
