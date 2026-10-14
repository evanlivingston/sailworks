"""Affinity-style dock chrome: native bars when docked, frameless panels floating.

Docked panels keep the native title bar (restyled: close on the left, no
float button) so main-window drag/drop keeps working. Floating panels go
frameless like Affinity: our own header with a left close button, drag to
move, double-click to redock, edge resize, and a rounded mask so no native
frame or white outline is drawn by the OS.
"""

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QCursor, QRegion
from PySide6.QtWidgets import (
    QApplication,
    QAbstractScrollArea,
    QAbstractSpinBox,
    QComboBox,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QToolButton,
    QWidget,
)

import logging
from shiboken6 import isValid

logger = logging.getLogger("gweatherrouting")

# Single minimum width shared by every component, docked or floating.
# QDockWidget honors this both inside QMainWindow layout and as a
# floating top-level window, so one constant keeps them identical.
DOCK_MIN_WIDTH = 300

# How long the thin scrollbar stays visible after the last scroll tick.
SCROLLBAR_FLASH_MS = 900

# Frameless floating panels: corner radius and resize-grip thickness.
FLOATING_RADIUS = 12
RESIZE_EDGE = 7


class _SnapPreview(QWidget):
    """Translucent tint showing where a dragged panel will snap on release."""

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # Plain QWidgets need this or stylesheet backgrounds never paint.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("snap-preview")
        self.hide()

    def show_at(self, rect):
        if self.geometry() != rect:
            self.setGeometry(rect)
        if not self.isVisible():
            self.show()

    def dismiss(self):
        if self.isVisible():
            self.hide()


class DockTitleBar(QWidget):
    """Frameless floating header: left close, title, drag-move, dblclick-dock."""

    SNAP_MARGIN = 14
    PANEL_MARGIN = 10

    def __init__(self, dock: QDockWidget):
        super().__init__(dock)
        self.setObjectName("dock-titlebar")
        # Plain QWidgets need this or stylesheet backgrounds never paint.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._dock = dock
        self._press_offset = None
        self._press_global = None
        self._snapped: str | None = None
        self._presnap: QRect | None = None
        self._pending_snap: tuple[str, QRect] | None = None
        self._last_snap_log: tuple | None = None
        self._preview: _SnapPreview | None = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(6)
        self.close_button = QToolButton(self)
        self.close_button.setObjectName("dock-close")
        self.close_button.setText("✕")
        self.close_button.setAutoRaise(True)
        self.close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_button.setCursor(Qt.CursorShape.ArrowCursor)
        self.close_button.clicked.connect(dock.close)
        layout.addWidget(self.close_button)
        self.title = QLabel(dock.windowTitle(), self)
        self.title.setObjectName("dock-title")
        layout.addWidget(self.title)
        layout.addStretch(1)
        dock.windowTitleChanged.connect(self.title.setText)
        dock.windowTitleChanged.connect(self._sync_close_tip)
        self._sync_close_tip(dock.windowTitle())

    def _sync_close_tip(self, title: str):
        tip = f"Close {title} (double-click header to dock)"
        self.close_button.setAccessibleName(f"Close {title} panel")
        self.close_button.setToolTip(tip)
        self.setToolTip("Drag to move · double-click to dock")

    def mousePressEvent(self, event):  # noqa: N802 - Qt virtual method name
        if event.button() == Qt.MouseButton.LeftButton and self._dock.isFloating():
            self._press_offset = event.globalPosition().toPoint() - self._dock.pos()
            self._press_global = event.globalPosition().toPoint()
            self.grabMouse()  # keep moves coming here once cursor leaves bar
            logger.debug(
                "dock-drag press panel=%r floating=%s pos=%s",
                self._dock.windowTitle(),
                self._dock.isFloating(),
                self._dock.pos(),
            )
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt virtual method name
        if (
            self._press_offset is not None
            and event.buttons() & Qt.MouseButton.LeftButton
            and self._dock.isFloating()
        ):
            self.drag_move(event.globalPosition().toPoint())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def drag_move(self, cursor):
        """Continue an in-progress header drag from any event source."""
        if self._press_offset is None or not self._dock.isFloating():
            return
        if (
            self._snapped is not None
            and self._press_global is not None
            and (cursor - self._press_global).manhattanLength()
            > QApplication.startDragDistance()
        ):
            # Only restore free size once this is a real drag; a plain
            # click to focus must leave a snapped panel alone.
            self._unsnap_for_drag(cursor)
        self._dock.move(cursor - self._press_offset)
        self._update_snap(cursor)

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt virtual method name
        if self._press_offset is not None:
            cursor = event.globalPosition().toPoint()
            moved = (
                self._press_global is not None
                and (cursor - self._press_global).manhattanLength()
                > QApplication.startDragDistance()
            )
            self._press_offset = None
            self._press_global = None
            self.releaseMouse()
            if moved:
                self._apply_snap()
            else:
                self._clear_preview()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802 - Qt virtual method name
        if event.button() == Qt.MouseButton.LeftButton and self._dock.isFloating():
            self._press_offset = None
            self._press_global = None
            self._clear_preview()
            if self._dock.windowState() & Qt.WindowState.WindowMaximized:
                self._dock.showNormal()
                self._snapped = None
            else:
                self._dock.setFloating(False)
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def clear_snap(self):
        self._snapped = None
        self._presnap = None

    # -- edge snap -------------------------------------------------------
    def _snap_target(self, cursor):
        screen = QApplication.screenAt(cursor)
        if screen is None:
            # Cursor exactly on/past the edge: judge by the panel instead.
            screen = (
                QApplication.screenAt(self._dock.geometry().center())
                or QApplication.primaryScreen()
            )
        if screen is None:
            return None, None
        avail = screen.availableGeometry()
        geom = self._dock.geometry()
        # Cursor at the edge (native-style grab) OR the panel edge touching
        # or past the screen edge (grabbed by the middle, possibly dragged
        # fast). Either one snaps; no upper bound so overshooting still
        # matches instead of falling into a dead zone.
        m = self.SNAP_MARGIN
        if (
            cursor.y() <= avail.top() + m
            or geom.top() <= avail.top() + self.PANEL_MARGIN
        ):
            return "max", QRect(avail)
        half = avail.width() // 2
        if (
            cursor.x() <= avail.left() + m
            or geom.left() <= avail.left() + self.PANEL_MARGIN
        ):
            rect = QRect(avail)
            rect.setWidth(half)
            return "left", rect
        if (
            cursor.x() >= avail.right() - m
            or geom.right() >= avail.right() - self.PANEL_MARGIN
        ):
            rect = QRect(avail)
            rect.setLeft(avail.left() + half)
            return "right", rect
        return None, None

    def _update_snap(self, cursor):
        kind, rect = self._snap_target(cursor)
        self._pending_snap = (kind, rect) if kind else None
        key = (kind, rect.getRect() if rect is not None else None)
        if key != self._last_snap_log:
            self._last_snap_log = key
            screen = QApplication.screenAt(cursor)
            logger.debug(
                "dock-snap panel=%r cursor=(%d,%d) screen=%s panel=%s target=%s",
                self._dock.windowTitle(),
                cursor.x(),
                cursor.y(),
                screen.availableGeometry().getRect() if screen else None,
                self._dock.geometry().getRect(),
                key,
            )
        if kind:
            if self._preview is None:
                self._preview = _SnapPreview()
            self._preview.show_at(rect)
        else:
            self._clear_preview()

    def _clear_preview(self):
        self._pending_snap = None
        if self._preview is not None:
            self._preview.dismiss()

    def _apply_snap(self):
        pending = self._pending_snap
        self._clear_preview()
        if not pending:
            logger.debug(
                "dock-snap release panel=%r: no target, staying at %s",
                self._dock.windowTitle(),
                self._dock.geometry().getRect(),
            )
            return
        kind, rect = pending
        dock = self._dock
        if kind == "max":
            if not self._snapped:
                self._presnap = dock.geometry()
            dock.showMaximized()
            self._snapped = "max"
            logger.debug("dock-snap release panel=%r: maximized", dock.windowTitle())
            return
        if dock.windowState() & Qt.WindowState.WindowMaximized:
            dock.showNormal()
        if not self._snapped:
            self._presnap = dock.geometry()
        dock.setGeometry(rect)
        self._snapped = kind
        logger.debug(
            "dock-snap release panel=%r: snapped %s to %s",
            dock.windowTitle(),
            kind,
            rect.getRect(),
        )

    def _unsnap_for_drag(self, cursor):
        # Grabbing a snapped panel restores its free size, like native drag.
        if not self._snapped or self._presnap is None:
            return
        dock = self._dock
        if dock.windowState() & Qt.WindowState.WindowMaximized:
            dock.showNormal()
        restored = QRect(self._presnap)
        ratio = 0.5
        if dock.width() > 0:
            ratio = min(1.0, max(0.0, (cursor.x() - dock.x()) / dock.width()))
        restored.moveLeft(round(cursor.x() - restored.width() * ratio))
        dock.setGeometry(restored)
        # Re-anchor the drag to the restored window so it doesn't jump.
        self._press_offset = cursor - dock.pos()
        self._snapped = None
        self._presnap = None


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

    Scrolling a panel with the wheel over a QComboBox (or spin box) would
    otherwise change that control instead of scrolling. The guard eats
    hover-wheel ticks and redirects them to the nearest enclosing scroll
    area; a focused control or an open popup keeps normal wheel behavior.
    """

    def eventFilter(self, watched, event):  # noqa: N802 - Qt virtual method name
        if event.type() != QEvent.Type.Wheel:
            return False
        control = watched
        while control is not None and not isinstance(
            control, (QComboBox, QAbstractSpinBox)
        ):
            control = control.parent()
        if control is None:
            return False
        if isinstance(control, QComboBox) and control.view().isVisible():
            return False
        focused = QApplication.focusWidget()
        if focused is not None and (
            focused is control or control.isAncestorOf(focused)
        ):
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
    """Thin, flash-only scrollbars for dock content."""
    area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.setViewportMargins(0, 0, 0, 0)
    for bar in (area.verticalScrollBar(), area.horizontalScrollBar()):
        bar.setSingleStep(12)
    if autohide and area.findChild(_ScrollActivity) is None:
        _ScrollActivity(area, delay_ms)
    return area


def style_dock(dock: QDockWidget) -> QDockWidget:
    """Native bar when docked, frameless Affinity-style panel when floating.

    The float/popout *button* stays hidden by stylesheet (theme.py) and
    DockWidgetFloatable stays enabled so docked drag-to-float and
    programmatic setFloating() keep working; _FloatingFrame swaps the
    chrome on topLevelChanged.
    """
    dock.setFeatures(
        dock.features()
        | QDockWidget.DockWidgetFeature.DockWidgetClosable
        | QDockWidget.DockWidgetFeature.DockWidgetMovable
        | QDockWidget.DockWidgetFeature.DockWidgetFloatable
    )
    dock.setMinimumWidth(DOCK_MIN_WIDTH)
    if dock.titleBarWidget() is not None:
        dock.setTitleBarWidget(None)  # type: ignore[arg-type]
    if dock.findChild(_FloatingFrame) is None:
        _FloatingFrame(dock)
    return dock


class _FloatingFrame(QObject):
    """Swap a dock between native (docked) and frameless (floating) chrome."""

    _CURSORS = {
        (True, False, False, True): Qt.CursorShape.SizeBDiagCursor,
        (False, True, True, False): Qt.CursorShape.SizeBDiagCursor,
        (True, False, True, False): Qt.CursorShape.SizeFDiagCursor,
        (False, True, False, True): Qt.CursorShape.SizeFDiagCursor,
        (True, False, False, False): Qt.CursorShape.SizeHorCursor,
        (False, True, False, False): Qt.CursorShape.SizeHorCursor,
        (False, False, True, False): Qt.CursorShape.SizeVerCursor,
        (False, False, False, True): Qt.CursorShape.SizeVerCursor,
    }

    def __init__(self, dock: QDockWidget):
        super().__init__(dock)
        self._dock = dock
        self._base_flags = dock.windowFlags()
        self._bar: DockTitleBar | None = None
        self._resize: tuple[tuple, QRect, QPoint] | None = None
        dock.topLevelChanged.connect(self._sync)
        dock.installEventFilter(self)
        self._sync(dock.isFloating())

    def _watch_descendants(self):
        # Edge presses land on child widgets, not the dock itself. Content
        # is set after style_dock() runs, so (re)install lazily on float.
        for child in self._dock.findChildren(QWidget):
            if child.property("_frameFiltered") is None:
                child.setProperty("_frameFiltered", True)
                child.installEventFilter(self)
                child.setMouseTracking(True)

    # -- chrome swap -----------------------------------------------------
    def _sync(self, floating: bool):
        dock = self._dock
        # Changing window flags hides the widget; restore prior visibility
        # so redocking never unhides a closed panel (or hides an open one).
        visible = dock.isVisible()
        if floating:
            dock.setWindowFlags(
                Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
            )
            dock.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            if self._bar is None:
                self._bar = DockTitleBar(dock)
            if dock.titleBarWidget() is not self._bar:
                dock.setTitleBarWidget(self._bar)
            self._watch_descendants()
            if visible:
                dock.show()
            self._update_mask()
        else:
            self._resize = None
            dock.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
            dock.clearMask()
            dock.setWindowFlags(self._base_flags)
            if dock.titleBarWidget() is not None:
                dock.setTitleBarWidget(None)  # type: ignore[arg-type]
            if visible:
                dock.show()

    def _update_mask(self):
        from PySide6.QtGui import QPainterPath

        if self._dock.windowState() & Qt.WindowState.WindowMaximized:
            self._dock.clearMask()
            return
        rect = self._dock.rect()
        if rect.isEmpty():
            return
        path = QPainterPath()
        path.addRoundedRect(
            rect.x(),
            rect.y(),
            rect.width(),
            rect.height(),
            FLOATING_RADIUS,
            FLOATING_RADIUS,
        )
        self._dock.setMask(QRegion(path.toFillPolygon().toPolygon()))

    # -- edge resize ------------------------------------------------------
    def _edges(self, dock: QDockWidget, pos) -> tuple:
        x, y = pos.x(), pos.y()
        w, h = dock.width(), dock.height()
        left = x < RESIZE_EDGE
        right = x >= w - RESIZE_EDGE
        top = y < RESIZE_EDGE
        bottom = y >= h - RESIZE_EDGE
        return (left, right, top, bottom)

    def eventFilter(self, watched, event):  # noqa: N802 - Qt virtual method name
        dock = self._dock
        # Descendant filters can outlive the dock's native widget during teardown.
        if not isValid(dock):
            return False
        if not dock.isFloating():
            return False
        if event.type() == QEvent.Type.Resize:
            self._update_mask()
            return False
        if event.type() == QEvent.Type.WindowStateChange:
            if dock.windowState() & Qt.WindowState.WindowMaximized:
                dock.clearMask()
            else:
                self._update_mask()
            return False
        if isinstance(watched, DockTitleBar):
            return False  # title bar owns move/double-click
        return self._mouse_event(watched, event)

    def _mouse_event(self, watched, event):
        dock = self._dock
        if not isinstance(watched, QWidget):
            return False
        if event.type() == QEvent.Type.MouseButtonPress and (
            event.button() == Qt.MouseButton.LeftButton
        ):
            edges = self._edges(dock, watched.mapTo(dock, event.position().toPoint()))
            if any(edges) and not self._press_on_scrollbar(dock, watched, event):
                self._resize = (
                    edges,
                    dock.geometry(),
                    event.globalPosition().toPoint(),
                )
                if isinstance(watched, QWidget):
                    watched.grabMouse()  # track outside the edge widget
                return True
            return False
        if event.type() == QEvent.Type.MouseMove:
            return self._mouse_move(watched, event)
        if event.type() == QEvent.Type.MouseButtonRelease:
            bar = dock.titleBarWidget()
            if isinstance(bar, DockTitleBar) and bar._press_offset is not None:
                bar.mouseReleaseEvent(event)
                return True
            if self._resize is not None:
                self._resize = None
                watched.releaseMouse()
        return False

    def _mouse_move(self, watched, event):
        dock = self._dock
        bar = dock.titleBarWidget()
        if (
            isinstance(bar, DockTitleBar)
            and not isinstance(watched, DockTitleBar)
            and bar._press_offset is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            # No mouse grab (or cursor left the bar): keep the header
            # drag alive from moves landing on content widgets.
            bar.drag_move(event.globalPosition().toPoint())
            return True
        if self._resize is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            self._apply_resize(event.globalPosition().toPoint())
            return True
        cursor = self._CURSORS.get(
            self._edges(dock, watched.mapTo(dock, event.position().toPoint()))
        )
        watched.setCursor(
            QCursor(cursor)
            if cursor is not None
            else QCursor(Qt.CursorShape.ArrowCursor)
        )
        return False

    def _press_on_scrollbar(self, dock, watched, event) -> bool:
        pos = event.position().toPoint()
        for area in dock.findChildren(QScrollArea):
            for bar in (area.verticalScrollBar(), area.horizontalScrollBar()):
                if bar.isVisible() and bar.rect().contains(
                    bar.mapFrom(watched, pos) if watched is not bar else pos
                ):
                    return True
        return False

    def _apply_resize(self, global_pos):
        if self._resize is None:
            return
        edges, start, origin = self._resize
        left, right, top, bottom = edges
        dx = global_pos.x() - origin.x()
        dy = global_pos.y() - origin.y()
        geom = QRect(start)
        if left:
            geom.setLeft(min(geom.left() + dx, geom.right() - DOCK_MIN_WIDTH))
        if right:
            geom.setRight(max(geom.right() + dx, geom.left() + DOCK_MIN_WIDTH))
        if top:
            geom.setTop(min(geom.top() + dy, geom.bottom() - 120))
        if bottom:
            geom.setBottom(max(geom.bottom() + dy, geom.top() + 120))
        self._dock.setGeometry(geom)
        bar = self._dock.titleBarWidget()
        if isinstance(bar, DockTitleBar):
            bar.clear_snap()  # manual resize breaks out of a snapped state
