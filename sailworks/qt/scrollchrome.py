"""Scrollbar and wheel helpers shared by the panel stacks."""

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QScrollArea,
    QSlider,
)

# How long the thin scrollbar stays visible after the last scroll tick.
SCROLLBAR_FLASH_MS = 900


class _ScrollActivity(QObject):
    """Show a scrollarea's bars while scrolling, hide them when idle.

    QScrollArea with ScrollBarAsNeeded shows a rail whenever content
    overflows, even when the user is not touching anything. This watcher
    hides both bars when idle and flashes them (only if actually needed)
    on value/range/wheel activity, then hides them again after a delay.
    Wheel/trackpad/keyboard scrolling keeps working while hidden.
    """

    def __init__(self, area: QScrollArea, delay_ms: int = SCROLLBAR_FLASH_MS):
        super().__init__(area)
        self._area = area
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self.hide_bars)
        vbar = area.verticalScrollBar()
        hbar = area.horizontalScrollBar()
        vbar.valueChanged.connect(lambda _value: self.activity())
        hbar.valueChanged.connect(lambda _value: self.activity())
        vbar.rangeChanged.connect(lambda *_args: self.ranges_changed())
        hbar.rangeChanged.connect(lambda *_args: self.ranges_changed())
        viewport = area.viewport()
        viewport.installEventFilter(self)
        area.installEventFilter(self)
        QTimer.singleShot(0, self.hide_bars)

    def eventFilter(self, watched, event):  # noqa: N802 - Qt virtual method name
        if event.type() == QEvent.Type.Wheel:
            self.activity()
        return False

    @staticmethod
    def _needed(bar) -> bool:
        return bar.maximum() > bar.minimum()

    def activity(self):
        bars = (
            self._area.verticalScrollBar(),
            self._area.horizontalScrollBar(),
        )
        if not any(self._needed(bar) for bar in bars):
            return
        for bar in bars:
            if self._needed(bar):
                bar.show()
        self._timer.start()

    def ranges_changed(self):
        # Layout/content updates re-show AsNeeded bars; re-hide when idle
        # so a static overflowing panel does not keep a rail on screen.
        if not self._timer.isActive():
            self.hide_bars()

    def hide_bars(self):
        self._area.verticalScrollBar().hide()
        self._area.horizontalScrollBar().hide()


class _WheelGuard(QObject):
    """Stop mouse-wheel hover from changing dropdown/spinner values.

    Scrolling a panel with the wheel over a QComboBox, spin box, or slider would
    otherwise change that control instead of scrolling. The guard eats
    hover-wheel ticks and redirects them to the nearest enclosing scroll
    area; wheel events over controls are always redirected or consumed, even
    when the control has focus or a combo-box popup is open.
    """

    def eventFilter(self, watched, event):  # noqa: N802 - Qt virtual method name
        if event.type() != QEvent.Type.Wheel:
            return False
        control = watched
        while control is not None and not isinstance(
            control, (QComboBox, QAbstractSpinBox, QSlider)
        ):
            control = control.parent()
        if control is None:
            return False
        area = control.parent()
        while area is not None and not isinstance(area, QAbstractScrollArea):
            area = area.parent()
        if area is None:
            return True
        self._scroll(area, event)
        return True

    @staticmethod
    def _scroll(area, event):
        pixel = event.pixelDelta()
        if not pixel.isNull():
            dx, dy = pixel.x(), pixel.y()
        else:
            angle = event.angleDelta()
            dx, dy = angle.x() * 20 // 120, angle.y() * 20 // 120
        vbar, hbar = area.verticalScrollBar(), area.horizontalScrollBar()
        if dy and vbar.maximum() > vbar.minimum():
            vbar.setValue(vbar.value() - dy)
        elif dx and hbar.maximum() > hbar.minimum():
            hbar.setValue(hbar.value() - dx)


def install_wheel_guard(app: QApplication) -> _WheelGuard | None:
    """Install the dropdown wheel guard once for the whole application."""
    existing = app.findChild(_WheelGuard)
    if existing is not None:
        return existing
    guard = _WheelGuard(app)
    app.installEventFilter(guard)
    return guard


def style_scrollarea(
    area: QScrollArea, autohide: bool = True, delay_ms: int = SCROLLBAR_FLASH_MS
) -> QScrollArea:
    """Thin, flash-only scrollbars for panel content."""
    area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.setViewportMargins(0, 0, 0, 0)
    for bar in (area.verticalScrollBar(), area.horizontalScrollBar()):
        bar.setSingleStep(12)
    if autohide and area.findChild(_ScrollActivity) is None:
        _ScrollActivity(area, delay_ms)
    return area
