"""Panel chrome and application-owned tab dragging and docking previews."""

from dataclasses import dataclass
from typing import cast

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QObject,
    QPoint,
    QRect,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import QContextMenuEvent, QCursor, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QAbstractScrollArea,
    QAbstractSpinBox,
    QComboBox,
    QSlider,
    QDockWidget,
    QMainWindow,
    QHBoxLayout,
    QLabel,
    QMenu,
    QScrollArea,
    QTabBar,
    QToolButton,
    QWidget,
)

import logging
from shiboken6 import getCppPointer, isValid

from .collapsiblepanel import CollapsiblePanel

logger = logging.getLogger("sailworks")

# Single minimum width shared by every component, docked or floating.
# QDockWidget honors this both inside QMainWindow layout and as a
# floating top-level window, so one constant keeps them identical.
DOCK_MIN_WIDTH = 300

# How long the thin scrollbar stays visible after the last scroll tick.
SCROLLBAR_FLASH_MS = 900

# Frameless floating panel resize-grip thickness.
RESIZE_EDGE = 7


def _application() -> QCoreApplication:
    """The live application that owns the global event filters.

    The filters are only installed from a live QMainWindow, so an application
    instance always exists here; the cast records that instead of testing it.
    """
    return cast(QCoreApplication, QApplication.instance())


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
        # Draw directly in paintEvent: stylesheet backgrounds do not paint
        # reliably on macOS top-level translucent windows.
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setObjectName("snap-preview")
        self.hide()

    def paintEvent(self, event):  # noqa: N802
        from PySide6.QtGui import QColor, QPainter, QPen

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(53, 132, 228, 65))
        painter.setPen(QPen(QColor(53, 132, 228, 160), 2))
        painter.drawRoundedRect(self.rect(), 4, 4)

    def show_at(self, rect):
        if self.geometry() != rect:
            self.setGeometry(rect)
        if not self.isVisible():
            self.show()
        self.raise_()

    def dismiss(self):
        if self.isVisible():
            self.hide()


@dataclass
class _DropTarget:
    kind: str
    rect: QRect
    dock: QDockWidget | None = None
    area: Qt.DockWidgetArea = Qt.DockWidgetArea.NoDockWidgetArea


class _EmptyTitleBar(QWidget):
    def sizeHint(self):  # noqa: N802
        return QSize(0, 0)

    def minimumSizeHint(self):  # noqa: N802
        return QSize(0, 0)


def _dock_context_menu(dock: QDockWidget) -> QMenu:
    menu = QMenu(dock)
    close_action = menu.addAction("Close panel")
    close_action.triggered.connect(dock.close)
    return menu


def _show_dock_context_menu(dock: QDockWidget, position):
    menu = _dock_context_menu(dock)
    menu.exec(position)


class DockDragController(QObject):
    """Keep Qt's tab reordering, but own tear-off and panel drop operations.

    Floating panels are moved with QWidget.move(), never a native system move,
    so desktop window tiling does not participate in panel drags.
    """

    def __init__(self, window: QMainWindow):
        super().__init__(window)
        self.window = window
        self._pressed: tuple[QTabBar, QDockWidget, QPoint] | None = None
        self._drag_bar: "DockTitleBar | None" = None
        self._sync_pending = False
        _application().installEventFilter(self)
        self.schedule_sync()

    def docks(self):
        if not isValid(self.window):
            return []
        return self.window.findChildren(
            QDockWidget, options=Qt.FindChildOption.FindDirectChildrenOnly
        )

    def schedule_sync(self, *_):
        if not self._sync_pending:
            self._sync_pending = True
            QTimer.singleShot(0, self.sync_chrome)

    def sync_chrome(self):
        self._sync_pending = False
        if not isValid(self.window):
            return
        for dock in self.docks():
            frame = dock.findChild(_FloatingFrame)
            if frame is not None and not dock.isFloating():
                # Only an actually visible peer supplies a tab heading. A lone
                # panel retains its header as a drag/close affordance.
                tabbed = any(
                    not peer.isHidden()
                    for peer in self.window.tabifiedDockWidgets(dock)
                )
                frame.set_tabbed(tabbed)

    def _tab_dock(self, tabs, index):
        if index < 0:
            return None
        # QMainWindow stores the dock's C++ address as its internal tab data.
        address = tabs.tabData(index)
        return next(
            (dock for dock in self.docks() if getCppPointer(dock)[0] == address), None
        )

    def eventFilter(self, watched, event):  # noqa: N802
        if not isValid(self.window):
            return False
        kind = event.type()
        if watched is self.window and kind == QEvent.Type.LayoutRequest:
            self.schedule_sync()
        if kind == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            if self._cancel_drag():
                return True
        if self._drag_bar is not None and self._drag_bar_event(
            self._drag_bar, kind, event
        ):
            return True
        if isinstance(watched, QTabBar) and watched.parentWidget() is self.window:
            if kind == QEvent.Type.ContextMenu:
                index = watched.tabAt(event.pos())
                dock = self._tab_dock(watched, index)
                if dock is not None:
                    _show_dock_context_menu(dock, event.globalPos())
                    event.accept()
                    return True
            if self._tab_press(watched, kind, event):
                return True
        if self._pressed is not None and self._pressed_event(
            self._pressed, kind, event
        ):
            return True
        return False

    def _cancel_drag(self):
        """Cancel a running floating drag; True when one was cancelled."""
        if self._drag_bar is None:
            self._pressed = None
            return False
        bar = self._drag_bar
        bar._clear_preview()
        bar._press_offset = bar._press_global = None
        bar.releaseMouse()
        self._drag_bar = None
        return True

    def _drag_bar_event(self, bar: "DockTitleBar", kind, event):
        """Forward a mouse event to the floating drag bar; True when handled."""
        if kind == QEvent.Type.MouseMove:
            bar.drag_move(event.globalPosition().toPoint())
            return True
        if (
            kind == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._drag_bar = None
            bar.mouseReleaseEvent(event)
            return True
        return False

    def _tab_press(self, tabs: QTabBar, kind, event):
        """Record a pressed tab or float its panel; True when handled."""
        if (
            kind == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
        ):
            dock = self._tab_dock(tabs, tabs.tabAt(event.position().toPoint()))
            if dock is not None:
                self._pressed = (tabs, dock, event.globalPosition().toPoint())
            return False
        if (
            kind == QEvent.Type.MouseButtonDblClick
            and event.button() == Qt.MouseButton.LeftButton
        ):
            dock = self._tab_dock(tabs, tabs.tabAt(event.position().toPoint()))
            if dock is not None:
                self._pressed = None
                dock.setFloating(True)
                return True
        return False

    def _pressed_event(self, pressed: tuple[QTabBar, QDockWidget, QPoint], kind, event):
        """Continue a pending tab drag; True when the drag took over."""
        tabs, dock, origin = pressed
        if kind == QEvent.Type.MouseButtonRelease:
            self._pressed = None
        elif (
            kind == QEvent.Type.MouseMove
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            cursor = event.globalPosition().toPoint()
            if (cursor - origin).manhattanLength() < QApplication.startDragDistance():
                return False
            if (
                tabs.rect()
                .adjusted(-12, -12, 12, 12)
                .contains(tabs.mapFromGlobal(cursor))
            ):
                return False  # Qt still owns reordering within the strip.
            self._pressed = None
            # End Qt's internal reorder before removing this tab.
            release = QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                tabs.mapFromGlobal(cursor),
                cursor,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
                event.modifiers(),
            )
            QApplication.sendEvent(tabs, release)
            size = dock.size()
            dock.setFloating(True)
            dock.resize(size)
            # A floating panel is framed by the drag controller's title bar.
            bar = cast("DockTitleBar", dock.titleBarWidget())
            dock.move(cursor - QPoint(min(size.width() // 2, 100), bar.height() // 2))
            bar._press_offset = cursor - dock.pos()
            bar._press_global = origin
            bar.grabMouse()
            self._drag_bar = bar
            bar._update_snap(cursor)
            return True
        return False

    @staticmethod
    def global_rect(widget):
        return QRect(widget.mapToGlobal(QPoint()), widget.size())

    def target_at(self, moving, cursor):
        docks = [d for d in self.docks() if d is not moving and not d.isHidden()]
        target = self._float_target(moving, cursor, docks)
        if target is not None:
            return target
        target = self._dock_target(moving, cursor, docks)
        if target is not None:
            return target
        return self._area_target(moving, cursor)

    def _float_target(self, moving, cursor, docks):
        # Prefer floating panels in front of the workspace. Only their top and
        # bottom edges are magnetic; the screen edges deliberately are not.
        for dock in reversed(docks):
            if not dock.isFloating():
                continue
            rect = self.global_rect(dock)
            if rect.left() - 20 <= cursor.x() <= rect.right() + 20:
                above = abs(cursor.y() - rect.top()) <= 24
                below = abs(cursor.y() - rect.bottom()) <= 24
                if above or below:
                    height = moving.height()
                    result = QRect(
                        rect.left(),
                        rect.top() - height if above else rect.bottom() + 1,
                        rect.width(),
                        height,
                    )
                    screen = QApplication.screenAt(cursor)
                    if screen is not None and not screen.availableGeometry().contains(
                        result
                    ):
                        continue
                    return _DropTarget("float", result, dock)
        return None

    def _dock_target(self, moving, cursor, docks):
        for dock in docks:
            if dock.isFloating() or dock.visibleRegion().isEmpty():
                continue
            area = self.window.dockWidgetArea(dock)
            if not moving.allowedAreas() & area:
                continue
            rect = self.global_rect(dock)
            # Include the tab strip immediately above the dock content.
            hit = rect.adjusted(0, -36, 0, 0)
            if not hit.contains(cursor):
                continue
            band = min(80, rect.height() // 4)
            if rect.top() <= cursor.y() < rect.top() + band:
                preview = QRect(rect)
                preview.setHeight(rect.height() // 2)
                return _DropTarget("top", preview, dock, area)
            if cursor.y() > rect.bottom() - band:
                preview = QRect(rect)
                preview.setTop(rect.top() + rect.height() // 2)
                return _DropTarget("bottom", preview, dock, area)
            return _DropTarget("tab", rect, dock, area)
        return None

    def _area_target(self, moving, cursor):
        bounds = self.global_rect(self.window)
        center = self.global_rect(self.window.centralWidget())
        if bounds.contains(cursor) and center.top() <= cursor.y() <= center.bottom():
            for area, edge in (
                (Qt.DockWidgetArea.LeftDockWidgetArea, bounds.left()),
                (Qt.DockWidgetArea.RightDockWidgetArea, bounds.right()),
            ):
                if abs(cursor.x() - edge) <= 32 and moving.allowedAreas() & area:
                    width = max(
                        DOCK_MIN_WIDTH, min(moving.width(), bounds.width() // 3)
                    )
                    x = (
                        bounds.left()
                        if area == Qt.DockWidgetArea.LeftDockWidgetArea
                        else bounds.right() - width + 1
                    )
                    return _DropTarget(
                        "area",
                        QRect(x, center.top(), width, center.height()),
                        area=area,
                    )
        return None

    def apply_target(self, moving, target):
        if target.kind == "float":
            moving.setGeometry(target.rect)
            return
        peer = target.dock
        if peer is not None and (
            not isValid(peer) or peer.isHidden() or peer.isFloating()
        ):
            return
        moving.setFloating(False)
        # Let Qt finish restoring the previous dock location before we move it.
        QApplication.processEvents()
        # setFloating(False) restores the old tab group. Remove that saved
        # placement before applying a new one (including splits in that group).
        self.window.removeDockWidget(moving)
        self.window.addDockWidget(target.area, moving)
        if target.kind == "tab":
            self.window.tabifyDockWidget(peer, moving)
        elif target.kind in ("top", "bottom"):
            self._split_sidebar(moving, target)
        moving.show()
        moving.raise_()
        self.schedule_sync()

    def _split_sidebar(self, moving, target):
        # Qt retains a tabbed layout item even after all but one tab have been
        # removed: splitDockWidget then silently tabifies. Rebuild just this
        # sidebar's visible vertical groups, establishing splits BEFORE tabs.
        # Other sidebar groups retain their order, active tabs, and heights.
        groups = self._sidebar_groups(moving, target)
        hidden = {
            dock: dock.isHidden() for _, members, _, _ in groups for dock in members
        }
        hidden[moving] = False
        ordered = self._ordered_groups(groups, moving, target)
        self._remove_docks(ordered)
        self._add_docks(ordered, target)
        self._restore_docks(ordered, hidden)

    def _sidebar_groups(self, moving, target):
        """Visible vertical groups of the sidebar that is being split."""
        groups = []
        seen = {moving}
        for dock in self.docks():
            if (
                dock in seen
                or dock.isFloating()
                or self.window.dockWidgetArea(dock) != target.area
            ):
                continue
            members = [dock, *self.window.tabifiedDockWidgets(dock)]
            members = [d for d in members if d is not moving and not d.isFloating()]
            seen.update(members)
            visible = [d for d in members if not d.isHidden()]
            if not visible:
                continue
            active = next(
                (d for d in visible if not d.visibleRegion().isEmpty()), visible[0]
            )
            groups.append((active.y(), members, active, active.height()))
        groups.sort(key=lambda group: group[0])
        return groups

    def _ordered_groups(self, groups, moving, target):
        """The groups in placement order, with the split pair around the target."""
        ordered: list[tuple[list[QDockWidget], QDockWidget, int]] = []
        for y, members, active, height in groups:
            if target.dock in members:
                pair = [
                    ([moving], moving, target.rect.height()),
                    (members, active, target.rect.height()),
                ]
                ordered.extend(pair if target.kind == "top" else reversed(pair))
            else:
                ordered.append((members, active, height))
        return ordered

    def _remove_docks(self, ordered):
        for members, _, _ in ordered:
            for dock in members:
                self.window.removeDockWidget(dock)

    def _add_docks(self, ordered, target):
        previous = None
        for members, _, _ in ordered:
            lead = members[0]
            self.window.addDockWidget(target.area, lead, Qt.Orientation.Vertical)
            if previous is not None:
                self.window.splitDockWidget(previous, lead, Qt.Orientation.Vertical)
            previous = lead

    def _restore_docks(self, ordered, hidden):
        for members, active, _ in ordered:
            for sibling in members[1:]:
                self.window.tabifyDockWidget(members[0], sibling)
            for dock in members:
                dock.setVisible(not hidden[dock])
            active.raise_()
        self.window.resizeDocks(
            [members[0] for members, _, _ in ordered],
            [height for _, _, height in ordered],
            Qt.Orientation.Vertical,
        )


class DockTitleBar(QWidget):
    """Frameless floating header: left close, title, drag-move, dblclick-dock."""

    def __init__(self, dock: QDockWidget):
        super().__init__(dock)
        self.setObjectName("dock-titlebar")
        # Plain QWidgets need this or stylesheet backgrounds never paint.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._dock = dock
        self._press_offset: QPoint | None = None
        self._press_global: QPoint | None = None
        self._pending_snap: tuple[_DropTarget, QRect] | None = None
        self._last_snap_log: tuple | None = None
        self._preview: _SnapPreview | None = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(6)
        self._layout = layout
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

    def add_collapse_button(self, button: QToolButton):
        """Place the panel collapse arrow between close and the title."""
        self._layout.insertWidget(1, button)
        button.show()

    def mousePressEvent(self, event):  # noqa: N802 - Qt virtual method name
        if event.button() == Qt.MouseButton.LeftButton:
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
        ):
            cursor = event.globalPosition().toPoint()
            if not self._dock.isFloating():
                if (
                    cursor - self._press_global
                ).manhattanLength() <= QApplication.startDragDistance():
                    return
                self._dock.setFloating(True)
                self._press_offset = QPoint(
                    min(100, self._dock.width() // 2), self.height() // 2
                )
                self.grabMouse()
            self.drag_move(event.globalPosition().toPoint())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def drag_move(self, cursor):
        """Continue an in-progress header drag from any event source."""
        if self._press_offset is None or not self._dock.isFloating():
            return
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
                self._update_snap(cursor)
                self._apply_snap()
            else:
                self._clear_preview()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802 - Qt virtual method name
        if event.button() == Qt.MouseButton.LeftButton and self._dock.isFloating():
            self._press_offset = None
            self._press_global = None
            self._clear_preview()
            self.releaseMouse()
            if self._dock.windowState() & Qt.WindowState.WindowMaximized:
                self._dock.showNormal()
            else:
                self._dock.setFloating(False)
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent):  # noqa: N802
        _show_dock_context_menu(self._dock, event.globalPos())
        event.accept()

    # -- edge snap -------------------------------------------------------
    def _manager(self):
        """The drag controller of the window that owns this title bar.

        A docked panel always has a parent window, so the parent widget is
        cast rather than tested for None.
        """
        parent = cast("QWidget", self._dock.parentWidget())
        return parent.findChild(DockDragController)

    def _snap_target(self, cursor):
        manager = self._manager()
        target = manager.target_at(self._dock, cursor) if manager else None
        return (target, target.rect) if target else (None, None)

    def _update_snap(self, cursor):
        kind, rect = self._snap_target(cursor)
        self._pending_snap = (kind, rect) if kind else None
        key = (
            kind.kind if kind else None,
            rect.getRect() if rect is not None else None,
        )
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
        target, rect = pending
        manager = self._manager()
        if manager:
            manager.apply_target(self._dock, target)


# Qt's QWIDGETSIZE_MAX; restored when a collapsed panel expands.
_UNCONSTRAINED = (1 << 24) - 1


class DockCollapse(QObject):
    """Collapse a whole dock body from a Blender-style panel header arrow.

    Tabbed docks show the ``CollapsiblePanel`` header row inside the tab and
    use its arrow. Lone and floating docks hide that row and use the arrow in
    ``DockTitleBar`` instead, so the title is never shown twice. Collapsing a
    lone dock shrinks it to its title bar; a tabbed dock keeps the tab strip
    and header row and hides only the body.
    """

    def __init__(self, dock: QDockWidget):
        super().__init__(dock)
        self._dock = dock
        self._collapsed = False
        self._tabbed = False
        self._floating = dock.isFloating()
        self._floating_height: int | None = None
        self._docked_layout: list[tuple[QDockWidget, int]] | None = None
        self._panel: CollapsiblePanel | None = None
        self._tabs: QTabBar | None = None
        self.title_button = self._new_button()
        dock.windowTitleChanged.connect(self._sync_labels)
        self._sync_labels()

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def _new_button(self) -> QToolButton:
        button = QToolButton()
        button.setObjectName("dock-collapse")
        button.setAutoRaise(True)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(self.toggle)
        return button

    def _collapsible(self) -> CollapsiblePanel | None:
        content = self._dock.widget()
        if not isinstance(content, CollapsiblePanel):
            return None
        if self._panel is not content:
            self._panel = content
            content.collapse_button.clicked.connect(self.toggle)
            content.set_header_visible(self._tabbed and not self._floating)
            content.set_collapsed(self._collapsed, animate=False)
        return content

    def _sync_labels(self, *_):
        title = self._dock.windowTitle()
        action = "Expand" if self._collapsed else "Collapse"
        self.title_button.setArrowType(
            Qt.ArrowType.RightArrow if self._collapsed else Qt.ArrowType.DownArrow
        )
        self.title_button.setToolTip(f"{action} {title} panel")
        self.title_button.setAccessibleName(f"{action} {title} panel")
        panel = self._collapsible()
        if panel is not None:
            panel.title.setText(title)

    def toggle(self):
        self.set_collapsed(not self._collapsed)

    def set_collapsed(self, collapsed: bool):
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        panel = self._collapsible()
        if panel is not None:
            panel.set_collapsed(collapsed, animate=False)
        else:
            content = self._dock.widget()
            if content is not None:
                content.setVisible(not collapsed)
        if self._tabbed and not self._floating:
            if self._active_tabbed():
                if collapsed:
                    self._shrink_tabbed_group()
                else:
                    self._expand_tabbed_group()
        else:
            self._apply_size()
        self._sync_labels()

    def set_tabbed(self, tabbed: bool):
        previous = self._tabs
        self._tabbed = tabbed
        if tabbed:
            tabs = self._tab_bar()
            if tabs is not previous:
                self._disconnect_tabs()
                self._tabs = tabs
                if tabs is not None:
                    tabs.currentChanged.connect(self._tab_changed)
        else:
            self._disconnect_tabs()
            self._dock.setMaximumHeight(_UNCONSTRAINED)
            if self._collapsed and previous is not None:
                state = self._tab_state(previous)
                height = state.get("expanded_height")
                if isinstance(height, int) and height > 0:
                    self._docked_layout = [
                        (self._dock, height),
                        *state.get("peers", []),
                    ]
        panel = self._collapsible()
        if panel is not None:
            panel.set_header_visible(tabbed and not self._floating)
        self._apply_size()
        if tabbed and self._collapsed and self._active_tabbed():
            self._shrink_tabbed_group()

    def set_floating(self, floating: bool):
        if floating == self._floating:
            return
        self._floating = floating
        if floating:
            self._disconnect_tabs()
            self._dock.setMaximumHeight(_UNCONSTRAINED)
        else:
            self._floating_height = None
        panel = self._collapsible()
        if panel is not None:
            panel.set_header_visible(self._tabbed and not floating)
        self._apply_size()

    def _disconnect_tabs(self):
        tabs = self._tabs
        self._tabs = None
        if tabs is None or not isValid(tabs):
            return
        try:
            tabs.currentChanged.disconnect(self._tab_changed)
        except (RuntimeError, TypeError):
            pass

    def _tab_bar(self) -> QTabBar | None:
        parent = self._dock.parentWidget()
        if parent is None:
            return None
        address = getCppPointer(self._dock)[0]
        for tabs in parent.findChildren(QTabBar):
            for index in range(tabs.count()):
                if tabs.tabData(index) == address:
                    return tabs
        return None

    def _active_tabbed(self) -> bool:
        return (
            self._tabbed
            and not self._floating
            and not self._dock.visibleRegion().isEmpty()
        )

    def _tab_group(self, window: QMainWindow) -> list[QDockWidget]:
        return [self._dock, *window.tabifiedDockWidgets(self._dock)]

    def _tab_state(self, tabs: QTabBar) -> dict:
        state = tabs.property("_dockCollapseState")
        if not isinstance(state, dict):
            state = {}
            tabs.setProperty("_dockCollapseState", state)
        return state

    def _tabbed_header_height(self) -> int:
        panel = self._collapsible()
        if panel is None:
            return 1
        return max(panel.header.sizeHint().height(), 1) + 2

    def _tabbed_peers(self, window: QMainWindow) -> list[tuple[QDockWidget, int]]:
        group = set(self._tab_group(window))
        area = window.dockWidgetArea(self._dock)
        return [
            (dock, dock.height())
            for dock in window.findChildren(
                QDockWidget, options=Qt.FindChildOption.FindDirectChildrenOnly
            )
            if dock not in group
            and not dock.isFloating()
            and not dock.visibleRegion().isEmpty()
            and window.dockWidgetArea(dock) == area
        ]

    def _shrink_tabbed_group(self):
        """Shrink a tab group to its tab strip and header, lifting peers up."""
        tabs = self._tab_bar()
        window = self._dock.parentWidget()
        if tabs is None or not isinstance(window, QMainWindow):
            return
        header = self._tabbed_header_height()
        state = self._tab_state(tabs)
        if not state.get("collapsed"):
            if self._dock.height() > header:
                state["expanded_height"] = self._dock.height()
                state["peers"] = self._tabbed_peers(window)
            state["collapsed"] = True
            tabs.setProperty("_dockCollapseState", state)
        self._dock.setMaximumHeight(header)

    def _expand_tabbed_group(self):
        tabs = self._tab_bar()
        window = self._dock.parentWidget()
        if tabs is None or not isinstance(window, QMainWindow):
            return
        for dock in self._tab_group(window):
            dock.setMaximumHeight(_UNCONSTRAINED)
        state = self._tab_state(tabs)
        state["collapsed"] = False
        tabs.setProperty("_dockCollapseState", state)
        height = state.get("expanded_height")
        if not isinstance(height, int) or height <= 0:
            return
        peers = [
            (dock, value) for dock, value in state.get("peers", []) if isValid(dock)
        ]
        window.resizeDocks(
            [self._dock, *[dock for dock, _ in peers]],
            [height, *[value for _, value in peers]],
            Qt.Orientation.Vertical,
        )
        # resizeDocks distributes by a few pixels; pin the group height so a
        # collapse immediately after a tab switch captures the full height.
        self._dock.resize(self._dock.width(), height)

    def _tab_changed(self, _index):
        # currentChanged fires before the new tab's visible region updates.
        QTimer.singleShot(
            0, lambda: self._sync_tabbed_state() if isValid(self) else None
        )

    def _sync_tabbed_state(self):
        if not isValid(self._dock) or self._tabs is None:
            return
        if not self._tabbed or self._floating:
            return
        if not self._active_tabbed():
            self._dock.setMaximumHeight(_UNCONSTRAINED)
            return
        if self._collapsed:
            self._shrink_tabbed_group()
        else:
            self._expand_tabbed_group()

    def _header_height(self) -> int:
        bar = self._dock.titleBarWidget()
        height = bar.sizeHint().height() if bar is not None else 0
        return max(height, 1) + 2

    def _apply_size(self):
        dock = self._dock
        if self._tabbed and not self._floating:
            # Tab groups size themselves from their active tab; see
            # _shrink_tabbed_group/_expand_tabbed_group.
            return
        if not self._collapsed:
            dock.setMaximumHeight(_UNCONSTRAINED)
            if self._floating and self._floating_height is not None:
                dock.resize(dock.width(), self._floating_height)
                self._floating_height = None
            elif not self._floating and self._docked_layout is not None:
                self._restore_docked_layout(self._docked_layout)
                self._docked_layout = None
            return
        if self._floating:
            if self._floating_height is None:
                self._floating_height = max(dock.height(), self._header_height())
            dock.setMaximumHeight(self._header_height())
            dock.resize(dock.width(), self._header_height())
            return
        if self._docked_layout is None:
            self._docked_layout = self._capture_docked_layout()
        dock.setMaximumHeight(self._header_height())

    def _capture_docked_layout(self) -> list[tuple[QDockWidget, int]]:
        """Remember sibling heights so expanding restores the sidebar split."""
        window = self._dock.parentWidget()
        if not isinstance(window, QMainWindow):
            return [(self._dock, self._dock.height())]
        area = window.dockWidgetArea(self._dock)
        return [
            (peer, peer.height())
            for peer in window.findChildren(
                QDockWidget, options=Qt.FindChildOption.FindDirectChildrenOnly
            )
            if not peer.isFloating()
            and peer.isVisible()
            and window.dockWidgetArea(peer) == area
        ]

    def _restore_docked_layout(self, layout: list[tuple[QDockWidget, int]]):
        peers = [(dock, height) for dock, height in layout if isValid(dock)]
        heights = dict(peers)
        height = heights.get(self._dock)
        window = self._dock.parentWidget()
        if height is None or not isinstance(window, QMainWindow):
            return

        def restore():
            if not isValid(self._dock) or not isValid(window):
                return
            # resizeDocks restores stacked siblings; resize() covers a lone
            # panel, which resizeDocks leaves at its size hint.
            window.resizeDocks(
                [dock for dock, _ in peers],
                [value for _, value in peers],
                Qt.Orientation.Vertical,
            )
            self._dock.resize(self._dock.width(), height)

        # Let the unconstrained maximum settle before restoring the split.
        QTimer.singleShot(0, restore)


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
    """Use custom chrome before floating, avoiding native window decoration."""
    dock.setFeatures(
        dock.features()
        | QDockWidget.DockWidgetFeature.DockWidgetClosable
        | QDockWidget.DockWidgetFeature.DockWidgetMovable
        | QDockWidget.DockWidgetFeature.DockWidgetFloatable
    )
    dock.setMinimumWidth(DOCK_MIN_WIDTH)
    if dock.findChild(_FloatingFrame) is None:
        _FloatingFrame(dock)
    window = dock.parentWidget()
    if isinstance(window, QMainWindow):
        manager = window.findChild(DockDragController)
        if manager is None:
            manager = DockDragController(window)
        dock.topLevelChanged.connect(manager.schedule_sync)
        dock.visibilityChanged.connect(manager.schedule_sync)
        dock.dockLocationChanged.connect(manager.schedule_sync)
    return dock


class _FloatingFrame(QObject):
    """Opaque frameless floating panels; no duplicate header under dock tabs."""

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
        self._bar = DockTitleBar(dock)
        self._empty = _EmptyTitleBar(dock)
        self._empty.setFixedHeight(0)
        dock.setTitleBarWidget(self._bar)
        self._collapse = DockCollapse(dock)
        self._bar.add_collapse_button(self._collapse.title_button)
        self._resize: tuple[tuple, QRect, QPoint] | None = None
        dock.topLevelChanged.connect(self._sync)
        self._sync(dock.isFloating())

    def _watch_descendants(self):
        # Edge presses land on child widgets, not the dock itself. Content
        # is set after style_dock() runs, so (re)install lazily on float.
        for child in self._dock.findChildren(QWidget):
            if child.property("_frameFiltered") is None:
                child.setProperty("_frameFiltered", True)
                child.setMouseTracking(True)

    # -- chrome swap -----------------------------------------------------
    def _sync(self, floating: bool):
        dock = self._dock
        self._collapse.set_floating(floating)
        if floating:
            if dock.titleBarWidget() is not self._bar:
                dock.setTitleBarWidget(self._bar)
            self._watch_descendants()
            # Qt's built-in dock resize handler filters application events.
            # Watch at the same level, before it consumes edge presses.
            _application().installEventFilter(self)
        else:
            _application().removeEventFilter(self)
            self._resize = None
            self._bar._clear_preview()
        # Qt makes docks with custom title widgets frameless tool windows.
        # Do not change flags or enable translucency during topLevelChanged:
        # recreating the native window here breaks child compositing on macOS.
        dock.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        dock.clearMask()

    def set_tabbed(self, tabbed):
        bar = self._empty if tabbed else self._bar
        if self._dock.titleBarWidget() is not bar:
            self._dock.setTitleBarWidget(bar)
        self._collapse.set_tabbed(tabbed)

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
        if not isinstance(watched, QWidget) or not (
            watched is dock or dock.isAncestorOf(watched)
        ):
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
