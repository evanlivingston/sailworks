from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QScrollArea,
    QSlider,
    QSpinBox,
    QWidget,
)

from gweatherrouting.qt.dockchrome import _WheelGuard


def _wheel_event():
    return QWheelEvent(
        QPointF(1, 1),
        QPointF(1, 1),
        QPoint(0, 0),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )


def test_focused_spinbox_wheel_does_not_change_value(qtbot):
    area = QScrollArea()
    content = QWidget()
    spin = QSpinBox(content)
    area.setWidget(content)
    qtbot.addWidget(area)
    area.show()
    spin.setFocus()
    QApplication.processEvents()
    spin.setValue(4)
    guard = _WheelGuard()

    assert guard.eventFilter(spin, _wheel_event()) is True
    assert spin.value() == 4


def test_open_combo_popup_wheel_does_not_change_value(qtbot):
    area = QScrollArea()
    content = QWidget()
    combo = QComboBox(content)
    combo.addItems(["first", "second"])
    area.setWidget(content)
    qtbot.addWidget(area)
    area.show()
    combo.showPopup()
    QApplication.processEvents()
    combo.setCurrentIndex(0)
    guard = _WheelGuard()

    assert combo.view().isVisible()
    assert guard.eventFilter(combo, _wheel_event()) is True
    assert combo.currentIndex() == 0


def test_slider_wheel_does_not_change_value(qtbot):
    area = QScrollArea()
    content = QWidget()
    slider = QSlider(Qt.Orientation.Horizontal, content)
    slider.setRange(0, 100)
    slider.setValue(40)
    area.setWidget(content)
    qtbot.addWidget(area)
    area.show()
    slider.setFocus()
    QApplication.processEvents()
    guard = _WheelGuard()

    assert guard.eventFilter(slider, _wheel_event()) is True
    assert slider.value() == 40
