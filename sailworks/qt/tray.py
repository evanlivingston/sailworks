"""System tray icon for the Qt application."""

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from sailworks.common import resource_path


def install_tray_icon(window):
    """Add a tray icon whose menu can show, hide or quit the window.

    Returns ``None`` on platforms without a system tray, where the window's own
    icon remains the only affordance.
    """
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None

    tray = QSystemTrayIcon(QIcon(resource_path("sailworks", "data/icon.png")), window)
    tray.setToolTip("Sailworks")

    menu = QMenu(window)
    toggle = menu.addAction("Hide Sailworks")
    menu.addSeparator()
    quit_action = menu.addAction("Quit Sailworks")
    tray.setContextMenu(menu)

    def sync_toggle_text():
        toggle.setText("Hide Sailworks" if window.isVisible() else "Show Sailworks")

    def toggle_window():
        if window.isVisible():
            window.hide()
        else:
            window.show()
            window.raise_()
            window.activateWindow()

    def quit_application():
        window.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def restore_window(reason):
        if reason not in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            return
        if not window.isVisible():
            toggle_window()

    menu.aboutToShow.connect(sync_toggle_text)
    toggle.triggered.connect(toggle_window)
    quit_action.triggered.connect(quit_application)
    tray.activated.connect(restore_window)
    tray.show()
    window.tray_icon = tray
    return tray
