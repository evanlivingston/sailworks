import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from sailworks.qt.theme import MenuBuilder, ThemedMenu


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
