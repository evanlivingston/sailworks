"""Blender-style scene collection browser for Qt."""

from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRect, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QCursor, QFont, QGuiApplication, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHeaderView,
    QLabel,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sailworks.common import resource_path
from sailworks.core.geo.rounding import (
    ROUNDING_PORT,
    ROUNDING_SHORT,
    ROUNDING_STARBOARD,
    normalize_rounding,
    rounding_label,
)
from sailworks.core.geo.routing import (
    DEFAULT_ISOCHRONE_COLOUR_MODE,
    DEFAULT_ROUTE_COLOUR_MODE,
    ISOCHRONE_COLOUR_MODE_LABELS,
    ISOCHRONE_COLOUR_MODES,
    ROUTE_COLOUR_MODE_LABELS,
    ROUTE_COLOUR_MODES,
)
from sailworks.core.scene import (
    KIND_LOG,
    KIND_POI,
    KIND_ROUTING,
    KIND_TRACK,
    SceneCollection,
    SceneEntry,
)
from sailworks.rendering.navigation import (
    routing_detail_fields,
    routing_point_detail_fields,
    routing_point_tree_cells,
    routing_summary,
    track_detail_fields,
    track_mark_count,
)
from .navigationcommands import (
    AddPointCommand,
    RemovePointCommand,
    RenamePointCommand,
)
from .pointeditdialog import PointEditDialog
from .theme import (
    BORDER,
    PANEL,
    SURFACE,
    TEXT,
    MenuBuilder,
    alternate_row_base,
    symbol_icon,
    ThemedMenu,
)

NODE_ROLE = Qt.ItemDataRole.UserRole
COORDINATE_ROLE = Qt.ItemDataRole.UserRole + 1
ROUNDING_ROLE = Qt.ItemDataRole.UserRole + 2
DETAIL_ROLE = Qt.ItemDataRole.UserRole + 3
# Dev's routing-legs grouping caches its children, so it needs its own role now
# that the point-detail fields claim +3.
LEGS_LOADED_ROLE = Qt.ItemDataRole.UserRole + 4
# Set on the Marks row while it is expanded, so the delegate knows to paint the
# column headers there.
MARK_HEADERS_ROLE = Qt.ItemDataRole.UserRole + 5
# The strings a mark row paints: a routing mark shows the clock time while the
# item data and tooltip keep the full timestamp.
MARK_DETAIL_ROLE = Qt.ItemDataRole.UserRole + 6
MUTED = QColor("#b0b0ad")
POINT_DETAIL = QColor("#8a8a87")
ROUNDING_COLOR = MUTED
# Port and starboard are distinct side constraints; sail-to-mark and gate stay
# neutral so only a real side choice draws the eye.
ROUNDING_COLORS = {
    ROUNDING_PORT: QColor("#d05a4f"),
    ROUNDING_STARBOARD: QColor("#4e9d63"),
}
ROUNDING_SIDES = {label: side for side, label in ROUNDING_SHORT.items()}
ROUNDING_GAP = 8
DETAIL_GAP = 12
DETAIL_MIN_LABEL = 48
# A point row gives up detail fields before it gives up the name.
POINT_MIN_LABEL = 56
# Routing mark rows share a fixed column grid so a column's right edge is the
# same on every row. Each width is sized for the widest realistic value, never
# for the row's own text, and the headers reuse those widths so a header drops
# with its column. The time cell is the time of day only; the row's tooltip and
# item data keep the full date. The placeholders use the widest digits of a
# proportional font: a sample like "08:01" is narrower than a real "00:00"
# because "1" advances less than "8".
MARK_COLUMN_WIDEST = ("88:88", "888°", "88.8", "88.8", "888°")
MARK_COLUMN_HEADERS = ("Time", "TWA", "TWS", "Speed", "Brg")
# Mark rows squeeze harder than point rows: a tighter gap and a smaller name
# reserve are what let the five fields share the narrow default panel.
MARK_DETAIL_GAP = 6
MARK_MIN_LABEL = 44
# The rounding slot is only as wide as "Stbd", so the full "Rounding" header
# would crowd its neighbour; the column gets the short form instead.
MARK_ROUNDING_HEADER = "Rnd"
NAME_COLUMN = 0
VISIBLE_COLUMN = 1
TYPE_ICONS = {
    KIND_TRACK: "track-symbolic",
    KIND_ROUTING: "isochrone-symbolic",
    KIND_POI: "poi-symbolic",
    KIND_LOG: "information-symbolic",
}


def item_tooltip(entry, element, detail):
    """Tooltip for a scene item row: the navigation summary or name and detail.

    A routing row shows only its navigation summary, since the row already
    carries the name and the icon carries the type. Other rows keep the name
    and detail. Empty fields are dropped so the tooltip never ends in a blank
    line.
    """
    if entry.kind == KIND_ROUTING:
        return routing_summary(element) if element is not None else ""
    return "\n".join(line for line in (entry.name, detail) if line)


def rounding_column_width(metrics):
    """Fixed width for the rounding slot, sized for the longest short label."""
    widest = max(
        (metrics.horizontalAdvance(label) for label in ROUNDING_SHORT.values()),
        default=0,
    )
    return widest + 6


def rounding_color(short_label):
    """Label colour for a rounding short label; neutral unless it names a side."""
    return ROUNDING_COLORS.get(ROUNDING_SIDES.get(short_label), ROUNDING_COLOR)


def point_columns(rect, metrics, label_left=None):
    """The rounding slot for a point row, anchored to the row's right edge.

    Coordinates are no longer painted on point rows, so the rounding label
    takes the row's right edge and every point row starts it at the same
    offset from that edge whatever its name. The slot has a fixed width sized
    for the longest short label, which also fixes the right edge of the detail
    fields beside it. A row too narrow for the slot beside a readable name
    shows the elided name alone rather than an empty name with right-hand
    fields.
    """
    left = rect.left() if label_left is None else max(rect.left(), label_left)
    available = max(0, rect.right() - left + 1)
    slot = rounding_column_width(metrics)
    minimum = min(POINT_MIN_LABEL, available)
    if slot <= 0 or available - minimum - ROUNDING_GAP < slot:
        return QRect(rect.right() + 1, rect.top(), 0, rect.height())
    return QRect(rect.right() - slot + 1, rect.top(), slot, rect.height())


def point_label_width(label_rect, rounding_rect):
    """Width a point row's name may use before the rounding slot starts.

    A row that drops the rounding slot paints no gap before it, so the name
    takes the whole label rect; the slot's rect is empty in that case.
    """
    if rounding_rect.width() == 0:
        return label_rect.width()
    return max(0, rounding_rect.left() - label_rect.left() - ROUNDING_GAP)


def row_geometry(rect, metrics, icon_width):
    """Rects a point row paints into: icon, label and rounding label."""
    icon = QRect(rect.left(), rect.top(), icon_width, rect.height())
    label = rect.adjusted(icon_width, 0, 0, 0)
    rounding = point_columns(rect, metrics, label.left())
    return icon, label, rounding


def detail_columns(rect, metrics, values, label_left, min_label=DETAIL_MIN_LABEL):
    """Rects for the detail segments a row can show, highest priority first.

    The values are painted as separate right-aligned segments ending at the
    row's right edge, in priority order from left to right. Segments are kept
    only while the block leaves ``min_label`` pixels for the label, so a
    narrow panel drops the lowest priority fields instead of painting them over
    the name. A value that does not fit stops the block, keeping the order
    intact rather than skipping to a shorter lower priority field.
    """
    available = max(0, rect.right() - label_left + 1)
    kept = []
    width = 0
    for value in values:
        text = str(value or "")
        if not text:
            continue
        text_width = metrics.horizontalAdvance(text)
        added = text_width + (DETAIL_GAP if kept else 0)
        if width + added + min_label > available:
            break
        kept.append((text, text_width))
        width += added
    columns = []
    right = rect.right()
    for text, text_width in reversed(kept):
        columns.append(
            (
                text,
                QRect(right - text_width + 1, rect.top(), text_width, rect.height()),
            )
        )
        right -= text_width + DETAIL_GAP
    columns.reverse()
    return columns


def mark_column_widths(metrics):
    """Fixed width per routing mark column, sized for its widest value or header."""
    return tuple(
        max(metrics.horizontalAdvance(widest), metrics.horizontalAdvance(header))
        for widest, header in zip(MARK_COLUMN_WIDEST, MARK_COLUMN_HEADERS)
    )


def mark_columns(rect, metrics, values, label_left, rounding_rect):
    """Rects for a routing mark row's fixed columns, highest priority first.

    Every column keeps the same width on every row, so a value's right edge
    does not depend on the text beside it. Columns are laid out right to left
    from the rounding slot and drop whole from the right (bearing first, then
    speed, TWS, TWA and time) while ``MARK_MIN_LABEL`` remains for the name.
    A column with no value keeps its slot so the rows below stay aligned.
    """
    widths = mark_column_widths(metrics)
    right = (
        rounding_rect.left() if rounding_rect.width() > 0 else rect.right() + 1
    ) - MARK_DETAIL_GAP
    limit = label_left + MARK_MIN_LABEL
    kept = 0
    used = 0
    for width in widths:
        added = width + (MARK_DETAIL_GAP if kept else 0)
        if right - used - added + 1 < limit:
            break
        kept += 1
        used += added
    columns = []
    edge = right
    for index in range(kept - 1, -1, -1):
        width = widths[index]
        text = str(values[index] or "") if index < len(values) else ""
        columns.append(
            (text, QRect(edge - width + 1, rect.top(), width, rect.height()))
        )
        edge -= width + MARK_DETAIL_GAP
    columns.reverse()
    return columns


def mark_header_columns(rect, metrics, icon_width, indent):
    """Header text and rects for an expanded Marks row.

    ``rect`` is the Marks row's own paint rect. Its mark rows sit one
    indentation level below and share its right edge, so shifting by
    ``indent`` reproduces their column geometry; the headers then end at
    exactly the same right edges as the values beneath them and drop with a
    column the row is too narrow to show.
    """
    child = rect.adjusted(indent, 0, 0, 0)
    _, label, rounding = row_geometry(child, metrics, icon_width)
    columns = mark_columns(child, metrics, (), label.left(), rounding)
    pairs = [
        (MARK_COLUMN_HEADERS[index], column)
        for index, (_, column) in enumerate(columns)
    ]
    if rounding.width() > 0 and (
        metrics.horizontalAdvance(MARK_ROUNDING_HEADER) <= rounding.width()
    ):
        pairs.append((MARK_ROUNDING_HEADER, rounding))
    return pairs


def _visibility_style():
    open_eye = Path(
        resource_path("sailworks", "data/icons/eye-open-negative-filled-symbolic.svg")
    ).as_posix()
    closed_eye = Path(
        resource_path("sailworks", "data/icons/eye-closed-symbolic.svg")
    ).as_posix()
    return (
        "QTreeWidget::indicator { width: 14px; height: 14px; }"
        f'QTreeWidget::indicator:checked {{ image: url("{open_eye}"); }}'
        f'QTreeWidget::indicator:unchecked {{ image: url("{closed_eye}"); }}'
    )


SCENE_TOOLTIP_STYLE = (
    f"QFrame#scene-tooltip {{ background: {PANEL}; color: {TEXT};"
    f" border: 1px solid {BORDER}; border-radius: 4px; padding: 4px 6px; }}"
    f"QFrame#scene-tooltip QLabel {{ background: transparent; color: {TEXT}; }}"
)


TOOLTIP_OFFSET_X = 16
TOOLTIP_OFFSET_Y = 20


class SceneTooltip(QFrame):
    """Themed tooltip window for the scene tree.

    The native tooltip keeps its white frame on macOS whatever the application
    stylesheet says, so the scene tree draws its own frame instead, matching the
    application's panels.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("scene-tooltip")
        self.setWindowFlags(
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFrameShadow(QFrame.Shadow.Plain)
        self.setLineWidth(0)
        self.setStyleSheet(SCENE_TOOLTIP_STYLE)

        self.label = QLabel(self)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.label.setStyleSheet(f"background: transparent; color: {TEXT};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label)
        self.hide()

    def show_text(self, text, position=None):
        """Show ``text`` at the global ``position``, kept inside the screen."""
        if not text:
            self.hide()
            return
        self.label.setText(text)
        self.adjustSize()
        anchor = QCursor.pos() if position is None else position
        self.move(self._clamped_position(anchor))
        self.show()

    def _clamped_position(self, anchor):
        """Anchor position moved clear of the pointer and onto the screen.

        The tooltip sits below and to the right of the anchor so the pointer
        does not cover its text, then moves back inside the available
        geometry of the screen it is on.
        """
        screen = QGuiApplication.screenAt(anchor) or QGuiApplication.primaryScreen()
        if screen is None:
            return QPoint(anchor.x() + TOOLTIP_OFFSET_X, anchor.y() + TOOLTIP_OFFSET_Y)
        bounds = screen.availableGeometry()
        size = self.sizeHint()
        x = min(
            max(anchor.x() + TOOLTIP_OFFSET_X, bounds.left()),
            bounds.right() - size.width() + 1,
        )
        y = min(
            max(anchor.y() + TOOLTIP_OFFSET_Y, bounds.top()),
            bounds.bottom() - size.height() + 1,
        )
        return QPoint(x, y)


def _isochrone_modes_for(element):
    """Isochrone modes this element can honour.

    A legacy route has no referenced result artifact, so a Time choice would
    persist and render nothing; only Uniform is offered.
    """
    if getattr(element, "result_ref", None) is None:
        return ("uniform",)
    return ISOCHRONE_COLOUR_MODES


class SceneTree(QTreeWidget):
    """Tree widget that hands drops back to the scene panel.

    It also shows a themed tooltip for its rows and suppresses the native one,
    whose white frame cannot be styled away on macOS.
    """

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        self.tooltip = SceneTooltip(self)
        # Blender-style alternating rows: every row at every depth gets a barely
        # visible tint over the panel surface. The row palette is set from the
        # theme rather than inherited so the tint stays proportional when the
        # application palette is not the dark one. Selection is painted on top
        # by the style, so a selected row keeps the normal highlight.
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Base, QColor(SURFACE))
        palette.setColor(QPalette.ColorRole.AlternateBase, alternate_row_base())
        self.setPalette(palette)
        self.setAlternatingRowColors(True)
        self.verticalScrollBar().valueChanged.connect(self.tooltip.hide)
        self.horizontalScrollBar().valueChanged.connect(self.tooltip.hide)

    def dropEvent(self, event):  # noqa: N802 - Qt virtual method name
        self.panel.drop_scene_items(event)

    def _hide_tooltip(self):
        # Qt can deliver viewport events while the base class is still being
        # constructed, before the tooltip exists.
        tooltip = getattr(self, "tooltip", None)
        if tooltip is not None:
            tooltip.hide()

    def viewportEvent(self, event):  # noqa: N802 - Qt virtual method name
        kind = event.type()
        if kind == QEvent.Type.ToolTip:
            return self._show_row_tooltip(event)
        if kind in (
            QEvent.Type.Leave,
            QEvent.Type.Hide,
            QEvent.Type.Wheel,
            QEvent.Type.MouseButtonPress,
        ):
            self._hide_tooltip()
        elif kind == QEvent.Type.MouseMove:
            # Hiding on every move also covers moving to a row without a
            # tooltip; Qt re-sends the tooltip event once the pointer settles.
            self._hide_tooltip()
        return super().viewportEvent(event)

    def hideEvent(self, event):  # noqa: N802 - Qt virtual method name
        self._hide_tooltip()
        super().hideEvent(event)

    def _show_row_tooltip(self, event):
        """Show the themed tooltip for the row under the cursor.

        Returns True so Qt does not show the native tooltip as well.
        """
        item = self.itemAt(event.pos())
        text = "" if item is None else item.toolTip(NAME_COLUMN)
        if not text:
            self.tooltip.hide()
            return False
        self.tooltip.show_text(text, self.viewport().mapToGlobal(event.pos()))
        return True


class SceneItemDelegate(QStyledItemDelegate):
    """Paint a row's detail fields and mark rounding.

    Point rows paint the name and the right-aligned rounding label; their
    coordinates live in the tooltip only. Track and routing rows carry detail
    fields instead, painted as right-aligned segments before the label so they
    never cover the name.
    """

    def paint(self, painter, option, index):
        coordinates = index.data(COORDINATE_ROLE)
        details = index.data(MARK_DETAIL_ROLE) or index.data(DETAIL_ROLE) or ()
        if coordinates is None and not details:
            super().paint(painter, option, index)
            if index.data(MARK_HEADERS_ROLE):
                self._paint_mark_headers(painter, option, index)
            return

        adjusted = QStyleOptionViewItem(option)
        self.initStyleOption(adjusted, index)
        label = adjusted.text
        adjusted.text = ""
        widget = adjusted.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem, adjusted, painter, widget
        )

        selected = bool(adjusted.state & QStyle.StateFlag.State_Selected)
        if selected:
            label_color = adjusted.palette.color(QPalette.ColorRole.HighlightedText)
            detail_color = label_color
        else:
            foreground = index.data(Qt.ItemDataRole.ForegroundRole)
            label_color = (
                foreground.color()
                if isinstance(foreground, QBrush)
                else adjusted.palette.color(QPalette.ColorRole.Text)
            )
            detail_color = POINT_DETAIL

        rect = adjusted.rect.adjusted(4, 0, -4, 0)
        metrics = adjusted.fontMetrics
        icon_width = 0
        if index.data(Qt.ItemDataRole.DecorationRole) is not None:
            size = adjusted.decorationSize
            if size is None or size.width() <= 0:
                size = widget.iconSize() if widget is not None else None
            icon_width = (size.width() if size is not None else 16) + 4

        if coordinates is None:
            self._paint_details(
                painter,
                adjusted,
                rect,
                metrics,
                icon_width,
                label,
                details,
                label_color,
                detail_color,
            )
            return

        rounding_text = str(index.data(ROUNDING_ROLE) or "")
        _, label_rect, rounding_rect = row_geometry(rect, metrics, icon_width)
        segments = (
            mark_columns(rect, metrics, details, label_rect.left(), rounding_rect)
            if details
            else []
        )
        if segments:
            label_width = max(
                0, segments[0][1].left() - label_rect.left() - MARK_DETAIL_GAP
            )
        elif rounding_rect.width() > 0:
            label_width = point_label_width(label_rect, rounding_rect)
        else:
            label_width = max(0, rect.right() - label_rect.left() + 1)
        label_text = metrics.elidedText(
            str(label), Qt.TextElideMode.ElideRight, label_width
        )

        painter.save()
        painter.setFont(adjusted.font)
        painter.setPen(label_color)
        painter.drawText(
            label_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            label_text,
        )
        if rounding_text and rounding_rect.width() > 0:
            painter.setPen(label_color if selected else rounding_color(rounding_text))
            painter.drawText(
                rounding_rect,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                rounding_text,
            )
        if segments:
            painter.setPen(detail_color)
            for text, segment in segments:
                if not text:
                    continue
                painter.drawText(
                    segment,
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    text,
                )
        painter.restore()

    def _header_segments(self, option, index):
        """Header text and rects for an expanded Marks row."""
        adjusted = QStyleOptionViewItem(option)
        self.initStyleOption(adjusted, index)
        widget = adjusted.widget
        rect = adjusted.rect.adjusted(4, 0, -4, 0)
        metrics = adjusted.fontMetrics
        icon_width = 0
        if index.data(Qt.ItemDataRole.DecorationRole) is not None:
            size = adjusted.decorationSize
            if size is None or size.width() <= 0:
                size = widget.iconSize() if widget is not None else None
            icon_width = (size.width() if size is not None else 16) + 4
        indent = widget.indentation() if widget is not None else 0
        return mark_header_columns(rect, metrics, icon_width, indent)

    def _paint_mark_headers(self, painter, option, index):
        """Paint the mark column headers on the Marks row that heads them."""
        segments = self._header_segments(option, index)
        if not segments:
            return
        adjusted = QStyleOptionViewItem(option)
        self.initStyleOption(adjusted, index)
        selected = bool(adjusted.state & QStyle.StateFlag.State_Selected)
        color = (
            adjusted.palette.color(QPalette.ColorRole.HighlightedText)
            if selected
            else POINT_DETAIL
        )
        painter.save()
        painter.setFont(adjusted.font)
        painter.setPen(color)
        for text, segment in segments:
            painter.drawText(
                segment,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                text,
            )
        painter.restore()

    @staticmethod
    def _paint_details(
        painter,
        option,
        rect,
        metrics,
        icon_width,
        label,
        details,
        label_color,
        detail_color,
    ):
        """Paint the label followed by the detail segments that fit."""
        label_rect = rect.adjusted(icon_width, 0, 0, 0)
        segments = detail_columns(rect, metrics, details, label_rect.left())
        detail_left = segments[0][1].left() if segments else rect.right() + 1
        label_text = metrics.elidedText(
            str(label),
            Qt.TextElideMode.ElideRight,
            max(0, detail_left - label_rect.left() - DETAIL_GAP),
        )
        painter.save()
        painter.setFont(option.font)
        painter.setPen(label_color)
        painter.drawText(
            label_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            label_text,
        )
        painter.setPen(detail_color)
        for text, segment in segments:
            painter.drawText(
                segment,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                text,
            )
        painter.restore()


class ScenePanel(QWidget):
    """Compact tree of collections over tracks, routings and logs."""

    changed = Signal()
    selected = Signal(str, str)
    open_requested = Signal(str, str)
    fit_requested = Signal(object)
    delete_requested = Signal(object)
    move_point_requested = Signal(object, int)
    edit_track_requested = Signal(str)
    routing_visibility_changed = Signal()

    def __init__(self, controller, parent=None, scene_path=None, undo_stack=None):
        super().__init__(parent)
        self.controller = controller
        self.core = controller.core
        self.scene = SceneCollection(scene_path)
        self.undo_stack = undo_stack
        self.syncing = False
        self._rows = {}
        self._expanded_items = set()
        self._expanded_legs = set()
        self._opened_logs = set()
        # Rebuilding clears the tree, so never do it from inside itemChanged:
        # deleting the emitting item there can crash Qt. Defer to the event loop.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._deferred_change)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        layout.setSpacing(6)

        self.tree = SceneTree(self)
        self.tree.setFrameShape(QFrame.Shape.NoFrame)
        self.tree.setColumnCount(2)
        self.tree.setHeaderHidden(True)
        self.tree.setItemDelegateForColumn(NAME_COLUMN, SceneItemDelegate(self.tree))
        self.tree.setUniformRowHeights(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)
        self.tree.setDragEnabled(True)
        self.tree.setAcceptDrops(True)
        self.tree.setDropIndicatorShown(True)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.setStyleSheet(_visibility_style())
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(
            NAME_COLUMN, QHeaderView.ResizeMode.Stretch
        )
        self.tree.header().setSectionResizeMode(
            VISIBLE_COLUMN, QHeaderView.ResizeMode.Fixed
        )
        self.tree.header().resizeSection(VISIBLE_COLUMN, 24)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        self.tree.itemExpanded.connect(self._expanded_changed)
        self.tree.itemCollapsed.connect(self._collapsed_changed)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.tree, 1)

        self.refresh()

    # ------------------------------------------------------------------
    # Live data

    def _entries(self, library_entries):
        entries = []
        for track in self.core.trackManager:
            details = (
                "Race track"
                if getattr(track, "race_track", False)
                else track_mark_count(len(track))
            )
            entries.append(
                SceneEntry(
                    kind=KIND_TRACK,
                    key=track.uid,
                    name=track.name,
                    visible=bool(track.visible),
                    detail=details,
                    element=track,
                )
            )
        for route in self.core.routingManager:
            entries.append(
                SceneEntry(
                    kind=KIND_ROUTING,
                    key=route.uid,
                    name=route.name,
                    visible=bool(route.visible),
                    detail=f"{len(route.points)} waypoints",
                    element=route,
                )
            )
        for poi in getattr(self.core, "poiManager", ()):
            position = getattr(poi, "position", None)
            entries.append(
                SceneEntry(
                    kind=KIND_POI,
                    key=poi.uid,
                    name=poi.name,
                    visible=bool(poi.visible),
                    detail=(
                        f"{position[0]:.5f}, {position[1]:.5f}"
                        if position is not None
                        else ""
                    ),
                    element=poi,
                )
            )
        race_panel = getattr(self.window(), "race_panel", None)
        if race_panel is not None:
            races = {race.race_id: race for race in getattr(race_panel, "_races", ())}
            for race_id, tracks in race_panel.loaded_race_tracks().items():
                race = races.get(race_id)
                race_name = race.name if race is not None else "Race"
                race_group = self.scene.ensure_group(
                    f"race-group:{race_id}",
                    race_name,
                    self.scene.default_group(KIND_TRACK),
                )
                for track in tracks:
                    entries.append(
                        SceneEntry(
                            kind=KIND_TRACK,
                            key=f"race:{race_id}:{track.boat_id}",
                            name=track.name,
                            visible=bool(track.visible),
                            detail=(
                                f"{race_name} · {track_mark_count(len(track.points))}"
                            ),
                            group=race_group.id,
                            element=track,
                        )
                    )
        for entry in library_entries.values():
            if entry["id"] not in self._opened_logs:
                continue
            details = entry.get("status") or ""
            vessel = entry.get("vessel")
            if vessel:
                details = f"{vessel} · {details}" if details else vessel
            entries.append(
                SceneEntry(
                    kind=KIND_LOG,
                    key=entry["id"],
                    name=entry.get("name") or "Log",
                    visible=True,
                    visible_editable=False,
                    detail=details,
                )
            )
        return entries

    def _loaded_log_entry(self, library_entries):
        """Return the library entry for the log currently loaded in the session."""
        session = getattr(self.controller, "session", None)
        log = getattr(session, "log", None) if session is not None else None
        path = getattr(log, "path", None)
        if not path:
            return None
        target = Path(path).resolve()
        library = self.controller.log_library
        for entry in library_entries.values():
            candidate = (entry.get("file_info") or {}).get("path")
            if not candidate:
                resolver = getattr(library, "path", None)
                if resolver is not None:
                    try:
                        candidate = str(resolver(entry["id"]))
                    except (KeyError, ValueError):
                        candidate = None
            if candidate and Path(candidate).resolve() == target:
                return entry
        return None

    def refresh(self):
        if self.syncing:
            return
        self.syncing = True
        try:
            library_entries = {
                entry["id"]: entry for entry in self.controller.log_library.entries()
            }
            active = self._loaded_log_entry(library_entries)
            if active is not None:
                self._opened_logs.add(active["id"])
            self._opened_logs.intersection_update(library_entries)
            self.scene.reconcile(self._entries(library_entries))
            changed = self.scene.apply_visibility()
            if changed:
                self._save_visible_elements(changed)
            # Route identities are collection uids and stay stable, but drop
            # expansion memory for routes that are no longer present.
            self._expanded_items.intersection_update(self.scene.items)
            self._expanded_legs.intersection_update(self.scene.items)
            self._rebuild()
        finally:
            self.syncing = False
        if changed:
            self.changed.emit()

    def _save_visible_elements(self, kinds):
        if KIND_TRACK in kinds:
            self.core.trackManager.save()
        if KIND_ROUTING in kinds:
            self.core.routingManager.save()
        if KIND_POI in kinds:
            manager = getattr(self.core, "poiManager", None)
            if manager is not None:
                manager.save()

    # ------------------------------------------------------------------
    # Tree construction

    def _rebuild(self):
        selected = self._selected_nodes()
        with QSignalBlocker(self.tree):
            self.tree.clear()
            self._rows = {}
            root = QTreeWidgetItem(["Scene Collection", ""])
            root.setData(NAME_COLUMN, NODE_ROLE, ("root",))
            root.setIcon(NAME_COLUMN, symbol_icon("collection-symbolic"))
            font = QFont(root.font(NAME_COLUMN))
            font.setBold(True)
            root.setFont(NAME_COLUMN, font)
            root.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tree.addTopLevelItem(root)
            root.setExpanded(True)
            self._add_groups(root, None)
            self._add_items(root, None)
        for node in selected:
            item = self.tree_item(node)
            if item is not None:
                item.setSelected(True)
        # Collapsed state is owned by the model, so honour it after insertion.
        for node in self._all_nodes():
            if node[0] != "group":
                continue
            group = self.scene.group(node[1])
            item = self.tree_item(node)
            if group is not None and item is not None:
                item.setExpanded(not group.collapsed)
        if not self.tree.selectedItems():
            first = self._first_selectable()
            if first is not None:
                first.setSelected(True)

    def _all_nodes(self):
        nodes = [("root",)]
        for group in self.scene.groups.values():
            nodes.append(("group", group.id))
        for item in self.scene.items.values():
            nodes.append(("item", item.kind, item.key))
        return nodes

    def _add_groups(self, parent_item, parent_id):
        for group in self.scene.groups_in(parent_id):
            row = QTreeWidgetItem([group.name, ""])
            row.setData(NAME_COLUMN, NODE_ROLE, ("group", group.id))
            row.setIcon(NAME_COLUMN, symbol_icon("collection-symbolic"))
            row.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEditable
                | Qt.ItemFlag.ItemIsDragEnabled
                | Qt.ItemFlag.ItemIsDropEnabled
            )
            row.setCheckState(
                VISIBLE_COLUMN,
                Qt.CheckState.Checked if group.visible else Qt.CheckState.Unchecked,
            )
            row.setToolTip(
                NAME_COLUMN,
                "Collection. Click the eye to hide every item inside."
                + (" Built-in." if group.builtin else ""),
            )
            if not self.scene.group_visible(group.id):
                row.setForeground(NAME_COLUMN, QBrush(MUTED))
            else:
                font = QFont(row.font(NAME_COLUMN))
                font.setBold(True)
                row.setFont(NAME_COLUMN, font)
            parent_item.addChild(row)
            self._rows[("group", group.id)] = row
            self._add_groups(row, group.id)
            self._add_items(row, group.id)

    @staticmethod
    def _detail_fields(kind, element):
        """Ordered (label, value) detail fields for a row, highest priority first.

        Only routings and tracks carry them; collections, groups, POIs and logs
        have no extra detail worth a segment.
        """
        if element is None:
            return []
        if kind == KIND_ROUTING:
            return routing_detail_fields(element)
        if kind == KIND_TRACK:
            return track_detail_fields(getattr(element, "points", ()))
        return []

    def _add_items(self, parent_item, group_id):
        for entry in self.scene.items_in(group_id):
            row = QTreeWidgetItem([entry.name, ""])
            row.setData(NAME_COLUMN, NODE_ROLE, ("item", entry.kind, entry.key))
            row.setIcon(
                NAME_COLUMN,
                symbol_icon(TYPE_ICONS.get(entry.kind, "information-symbolic")),
            )
            flags = (
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsDragEnabled
            )
            if entry.kind in (KIND_TRACK, KIND_ROUTING, KIND_POI):
                flags |= Qt.ItemFlag.ItemIsUserCheckable
                row.setCheckState(
                    VISIBLE_COLUMN,
                    Qt.CheckState.Checked if entry.visible else Qt.CheckState.Unchecked,
                )
            if entry.kind in (KIND_TRACK, KIND_ROUTING, KIND_POI, KIND_LOG):
                flags |= Qt.ItemFlag.ItemIsEditable
            if entry.kind == KIND_LOG:
                row.setIcon(
                    VISIBLE_COLUMN,
                    symbol_icon("eye-open-negative-filled-symbolic"),
                )
            row.setFlags(flags)
            live = self.scene.live_entry(entry.kind, entry.key)
            detail = live.detail if live is not None else ""
            element = live.element if live is not None else None
            fields = self._detail_fields(entry.kind, element)
            if fields:
                row.setData(NAME_COLUMN, DETAIL_ROLE, [value for _, value in fields])
            row.setToolTip(NAME_COLUMN, item_tooltip(entry, element, detail))
            if not self.scene.effective_visible(entry.kind, entry.key):
                row.setForeground(NAME_COLUMN, QBrush(MUTED))
            parent_item.addChild(row)
            self._rows[("item", entry.kind, entry.key)] = row
            live = self.scene.live_entry(entry.kind, entry.key)
            if (
                entry.kind in (KIND_TRACK, KIND_ROUTING)
                and live is not None
                and live.element is not None
            ):
                row.setChildIndicatorPolicy(
                    QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator
                )
                if entry.identity in self._expanded_items:
                    row.setExpanded(True)
                    self._populate_track_points(row, entry)

    def _populate_track_points(self, row, entry):
        """Show a track or routing element's positions beneath its tree row."""
        if row.childCount():
            return
        live = self.scene.live_entry(entry.kind, entry.key)
        if live is None or live.element is None:
            return
        element = live.element
        if entry.kind == KIND_ROUTING:
            self._populate_routing_children(row, entry, element)
            return
        self._add_point_rows(row, entry, element, "Point")

    def _add_point_rows(self, row, entry, element, default_label):
        """Add editable mark rows for a track or routing element's positions."""
        rename_point = getattr(element, "rename_point", None)
        point_name = getattr(element, "point_name", None)
        point_rounding = getattr(element, "point_rounding", None)
        for index, point in enumerate(element.points):
            name = point_name(index) if callable(point_name) else ""
            label = name or f"{default_label} {index + 1}"
            coordinates = f"{point[0]:.6f}, {point[1]:.6f}"
            child = QTreeWidgetItem([label, ""])
            child.setData(
                NAME_COLUMN, NODE_ROLE, ("point", entry.kind, entry.key, index)
            )
            child.setData(NAME_COLUMN, COORDINATE_ROLE, coordinates)
            rounding_value = (
                normalize_rounding(point_rounding(index))
                if callable(point_rounding)
                else None
            )
            if callable(point_rounding):
                child.setData(
                    NAME_COLUMN,
                    ROUNDING_ROLE,
                    ROUNDING_SHORT[rounding_value],
                )
            details = []
            if entry.kind == KIND_ROUTING:
                details = [value for _, value in routing_point_detail_fields(point)]
                if details:
                    child.setData(NAME_COLUMN, DETAIL_ROLE, details)
                    child.setData(
                        NAME_COLUMN,
                        MARK_DETAIL_ROLE,
                        list(routing_point_tree_cells(point)),
                    )
            child.setToolTip(
                NAME_COLUMN,
                "\n".join(
                    text
                    for text in (
                        rounding_label(rounding_value),
                        coordinates,
                        details[0] if entry.kind == KIND_ROUTING and details else "",
                    )
                    if text
                ),
            )
            child.setIcon(NAME_COLUMN, symbol_icon("poi-symbolic"))
            flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            if callable(rename_point):
                flags |= Qt.ItemFlag.ItemIsEditable
            child.setFlags(flags)
            child.setForeground(NAME_COLUMN, QBrush(MUTED))
            row.addChild(child)

    def _populate_routing_children(self, row, entry, element):
        """Add calculated-result wrappers below a routing item.

        These are display wrappers over the saved result, not editable
        waypoints. Neither wrapper reads the artifact until the Marks node is
        expanded, so a collapsed tree never hydrates contours.
        """
        identity = (entry.kind, entry.key)
        visible = bool(getattr(element, "isochrones_visible", True))
        # A recorded contour-read failure means the artifact is missing or
        # corrupt. Show that instead of a checked box that would claim contours
        # are present; the persisted visibility flag is left untouched so a
        # later successful read restores the user's choice.
        contour_error = getattr(element, "contour_error", None)
        isochrones = QTreeWidgetItem(
            ["Isochrones (unavailable)" if contour_error else "Isochrones", ""]
        )
        isochrones.setData(
            NAME_COLUMN, NODE_ROLE, ("routing-isochrones", entry.kind, entry.key)
        )
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if not contour_error:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        isochrones.setFlags(flags)
        isochrones.setCheckState(
            VISIBLE_COLUMN,
            (
                Qt.CheckState.Unchecked
                if contour_error or not visible
                else Qt.CheckState.Checked
            ),
        )
        isochrones.setIcon(NAME_COLUMN, symbol_icon("isochrone-symbolic"))
        if contour_error:
            isochrones.setForeground(NAME_COLUMN, QBrush(MUTED))
            isochrones.setToolTip(
                NAME_COLUMN,
                f"Isochrones unavailable: {contour_error}",
            )
        else:
            isochrones.setToolTip(
                NAME_COLUMN,
                "Calculated isochrones. Their visibility is independent of the "
                "route line.",
            )
        row.addChild(isochrones)
        self._rows[("routing-isochrones", entry.kind, entry.key)] = isochrones

        # Traces are their own layer: they stay available when the isochrones
        # are hidden. Off by default because the dense trace web makes the map
        # unreadable. Legacy routes keep the toggle but have no trace geometry.
        traces = QTreeWidgetItem(["Traces", ""])
        traces.setData(
            NAME_COLUMN, NODE_ROLE, ("routing-traces", entry.kind, entry.key)
        )
        traces.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
        )
        traces.setCheckState(
            VISIBLE_COLUMN,
            (
                Qt.CheckState.Checked
                if bool(getattr(element, "traces_visible", False))
                else Qt.CheckState.Unchecked
            ),
        )
        traces.setIcon(NAME_COLUMN, symbol_icon("track-symbolic"))
        traces.setToolTip(
            NAME_COLUMN,
            "Routing traces. Independent of the route line and the isochrones. "
            "Hidden by default because the trace web makes the map unreadable.",
        )
        row.addChild(traces)
        self._rows[("routing-traces", entry.kind, entry.key)] = traces

        # Route marks are editable waypoints, like track points, but they stay
        # grouped with the calculated legs. Legacy routes without a result still
        # get the wrapper so their marks keep one consistent location.
        # The node groups the route's marks; calculated legs appear below them
        # when it is expanded. Its "routing-legs" id and LEGS_LOADED_ROLE keep
        # their original names because expansion state is keyed on them.
        legs = QTreeWidgetItem(["Marks", ""])
        legs.setData(NAME_COLUMN, NODE_ROLE, ("routing-legs", entry.kind, entry.key))
        legs.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        legs.setIcon(NAME_COLUMN, symbol_icon("track-symbolic"))
        legs.setToolTip(
            NAME_COLUMN,
            "Route marks. Expand to read calculated legs from the saved result.",
        )
        legs.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        row.addChild(legs)
        self._rows[("routing-legs", entry.kind, entry.key)] = legs
        self._add_point_rows(legs, entry, element, "Mark")
        if identity in self._expanded_legs:
            legs.setData(NAME_COLUMN, MARK_HEADERS_ROLE, True)
            legs.setExpanded(True)
            self._populate_legs(legs, entry)

    def _populate_legs(self, row, entry):
        """Read calculated legs from the saved result, reporting load failures."""
        if row.data(NAME_COLUMN, LEGS_LOADED_ROLE):
            return
        live = self.scene.live_entry(entry.kind, entry.key)
        element = live.element if live is not None else None
        if element is None:
            row.setData(NAME_COLUMN, LEGS_LOADED_ROLE, True)
            self._legs_failure(row, "route is no longer available")
            return
        if getattr(element, "result_ref", None) is None:
            # Legacy routes have no calculated artifact; their inline contours
            # stay available through the Isochrones toggle above.
            return
        row.setData(NAME_COLUMN, LEGS_LOADED_ROLE, True)
        manager = getattr(self.core, "routingManager", None)
        loader = getattr(manager, "load_result", None)
        if not callable(loader):
            self._legs_failure(row, "routing collection cannot load results")
            return
        try:
            result = loader(element)
        except (OSError, KeyError, ValueError, LookupError) as exc:
            self._legs_failure(row, self._error_text(exc))
            return
        legs = tuple(getattr(result, "legs", ()))
        if not legs:
            child = QTreeWidgetItem(["No calculated legs", ""])
            child.setFlags(Qt.ItemFlag.ItemIsEnabled)
            child.setForeground(NAME_COLUMN, QBrush(MUTED))
            row.addChild(child)
            return
        for index, leg in enumerate(legs, 1):
            start = getattr(leg, "start", None)
            end = getattr(leg, "end", None)
            label = f"Leg {index}"
            coordinates = ""
            if start is not None and end is not None:
                coordinates = (
                    f"{start.lat:.5f}, {start.lon:.5f} → "
                    f"{end.lat:.5f}, {end.lon:.5f}"
                )
                label = f"{label}: {coordinates}"
            child = QTreeWidgetItem([label, ""])
            child.setData(
                NAME_COLUMN,
                NODE_ROLE,
                ("routing-leg", entry.kind, entry.key, index - 1),
            )
            if coordinates:
                child.setData(NAME_COLUMN, COORDINATE_ROLE, coordinates)
            child.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            child.setForeground(NAME_COLUMN, QBrush(MUTED))
            row.addChild(child)

    @staticmethod
    def _error_text(exc):
        """User-readable failure text without a redundant class prefix."""
        detail = str(exc).strip()
        return detail or type(exc).__name__

    def _legs_failure(self, row, detail):
        child = QTreeWidgetItem([f"Result unavailable: {detail}", ""])
        child.setFlags(Qt.ItemFlag.ItemIsEnabled)
        child.setForeground(NAME_COLUMN, QBrush(MUTED))
        row.addChild(child)
        row.setToolTip(NAME_COLUMN, f"Calculated legs unavailable: {detail}")

    def _first_selectable(self):
        for index in range(self.tree.topLevelItemCount()):
            root = self.tree.topLevelItem(index)
            for row in self._descendants(root):
                node = row.data(0, NODE_ROLE)
                if node and node[0] == "item":
                    return row
        return None

    @staticmethod
    def _descendants(item):
        for index in range(item.childCount()):
            child = item.child(index)
            yield child
            yield from ScenePanel._descendants(child)

    def tree_item(self, node):
        return self._rows.get(tuple(node))

    def item_row(self, kind, key):
        return self._rows.get(("item", kind, key))

    def group_row(self, identifier):
        return self._rows.get(("group", identifier))

    def _expand_group_path(self, identifier):
        group = self.scene.group(identifier) if identifier else None
        path = []
        while group is not None:
            path.append(group.id)
            group = self.scene.group(group.parent) if group.parent else None
        for group_id in reversed(path):
            row = self.group_row(group_id)
            if row is not None:
                row.setExpanded(True)

    def _selected_nodes(self):
        return [
            tuple(item.data(0, NODE_ROLE))
            for item in self.tree.selectedItems()
            if item.data(0, NODE_ROLE) is not None
        ]

    def _selected_item_keys(self):
        return [
            (node[1], node[2]) for node in self._selected_nodes() if node[0] == "item"
        ]

    def _selected_groups(self):
        return [node[1] for node in self._selected_nodes() if node[0] == "group"]

    def _current_node(self):
        item = self.tree.currentItem()
        return tuple(item.data(0, NODE_ROLE)) if item is not None else None

    def _current_group(self):
        node = self._current_node()
        if node is not None and node[0] == "group":
            return node[1]
        if node is not None and node[0] == "item":
            entry = self.scene.item(node[1], node[2])
            return entry.group if entry is not None else None
        return None

    # ------------------------------------------------------------------
    # Interaction

    def _item_changed(self, row, _column):
        if self.syncing:
            return
        node = row.data(0, NODE_ROLE)
        if node is None:
            return
        changed = False
        structural = False
        if node[0] == "group":
            group = self.scene.group(node[1])
            if group is None:
                return
            name = row.text(NAME_COLUMN).strip()
            if name and name != group.name:
                changed = self.scene.rename_group(group.id, name) or changed
                structural = True
            visible = row.checkState(VISIBLE_COLUMN) == Qt.CheckState.Checked
            if visible != group.visible:
                changed = self.scene.set_group_visible(group.id, visible) or changed
        elif node[0] == "item":
            item = self.scene.item(node[1], node[2])
            if item is None:
                return
            name = row.text(NAME_COLUMN).strip()
            if name and name != item.name:
                self._rename_item(item, name)
                return
            if item.kind == KIND_LOG:
                return
            visible = row.checkState(VISIBLE_COLUMN) == Qt.CheckState.Checked
            if visible != item.visible:
                changed = (
                    self.scene.set_item_visible(node[1], node[2], visible) or changed
                )
        elif node[0] == "routing-isochrones":
            entry = self.scene.live_entry(node[1], node[2])
            element = entry.element if entry is not None else None
            if element is None:
                return
            if getattr(element, "contour_error", None):
                # The row reports an unreadable artifact and is not checkable;
                # never let a displayed unchecked state overwrite the persisted
                # visibility flag.
                return
            visible = row.checkState(VISIBLE_COLUMN) == Qt.CheckState.Checked
            if bool(getattr(element, "isochrones_visible", True)) != visible:
                element.isochrones_visible = visible
                self._save_visible_elements({KIND_ROUTING})
                self.changed.emit()
                self.routing_visibility_changed.emit()
            return
        elif node[0] == "routing-traces":
            entry = self.scene.live_entry(node[1], node[2])
            element = entry.element if entry is not None else None
            if element is None:
                return
            visible = row.checkState(VISIBLE_COLUMN) == Qt.CheckState.Checked
            if bool(getattr(element, "traces_visible", False)) != visible:
                element.traces_visible = visible
                self._save_visible_elements({KIND_ROUTING})
                self.changed.emit()
                self.routing_visibility_changed.emit()
            return
        elif node[0] == "point":
            self._rename_point(node, row.text(NAME_COLUMN))
            return
        if changed:
            if structural:
                self._after_change(structural=True)
            else:
                applied = self.scene.apply_visibility()
                if applied:
                    self._save_visible_elements(applied)
                self._refresh_timer.start(0)
                if KIND_ROUTING in applied:
                    self.routing_visibility_changed.emit()

    def _deferred_change(self):
        self.syncing = True
        try:
            self._rebuild()
        finally:
            self.syncing = False
        self.changed.emit()

    def _expanded_changed(self, row):
        if self.syncing:
            return
        node = row.data(0, NODE_ROLE)
        if node is None:
            return
        if node[0] == "group":
            self.scene.set_group_collapsed(node[1], False)
        elif node[0] == "item" and node[1] in (KIND_TRACK, KIND_ROUTING):
            identity = (node[1], node[2])
            self._expanded_items.add(identity)
            item = self.scene.item(*identity)
            if item is not None:
                self._populate_track_points(row, item)
        elif node[0] == "routing-legs":
            identity = (node[1], node[2])
            self._expanded_legs.add(identity)
            row.setData(NAME_COLUMN, MARK_HEADERS_ROLE, True)
            item = self.scene.item(*identity)
            if item is not None:
                self._populate_legs(row, item)

    def _collapsed_changed(self, row):
        if self.syncing:
            return
        node = row.data(0, NODE_ROLE)
        if node is None:
            return
        if node[0] == "group":
            self.scene.set_group_collapsed(node[1], True)
        elif node[0] == "item" and node[1] in (KIND_TRACK, KIND_ROUTING):
            self._expanded_items.discard((node[1], node[2]))
        elif node[0] == "routing-legs":
            self._expanded_legs.discard((node[1], node[2]))
            row.setData(NAME_COLUMN, MARK_HEADERS_ROLE, False)

    def _rename_item(self, item, name):
        if item.kind == KIND_LOG:
            try:
                self.controller.log_library.update(item.key, name=name)
            except (KeyError, ValueError):
                self.refresh()
                return
            self.controller.log_library_changed.emit()
        elif item.kind == KIND_TRACK and item.key.startswith("race:"):
            entry = self.scene.live_entry(item.kind, item.key)
            if entry is None or entry.element is None:
                self.refresh()
                return
            entry.element.name = name
        else:
            entry = self.scene.live_entry(item.kind, item.key)
            if entry is None or entry.element is None:
                self.refresh()
                return
            manager = {
                KIND_TRACK: self.core.trackManager,
                KIND_ROUTING: self.core.routingManager,
                KIND_POI: self.core.poiManager,
            }.get(item.kind)
            if manager is None:
                self.refresh()
                return
            entry.element.name = manager.get_unique_name(name)
            manager.save()
        self.refresh()
        self.changed.emit()

    def _point_element(self, node):
        if node is None or node[0] != "point":
            return None
        entry = self.scene.live_entry(node[1], node[2])
        element = entry.element if entry is not None else None
        if element is None or not callable(getattr(element, "rename_point", None)):
            return None
        return element

    def _rename_point(self, node, name):
        element = self._point_element(node)
        if element is None:
            return
        index = int(node[3])
        normalized = "" if name is None else str(name).strip()
        get_name = getattr(element, "point_name", None)
        before = get_name(index) if callable(get_name) else ""
        if before == normalized:
            self.refresh()
            return
        if self.undo_stack is None:
            if element.rename_point(index, normalized):
                self.changed.emit()
            self.refresh()
            return
        self.undo_stack.push(
            RenamePointCommand(self._points_changed, element, index, before, normalized)
        )

    def _move_point(self, node):
        element = self._point_element(node)
        if element is not None:
            self.move_point_requested.emit(element, int(node[3]))

    def _track_element(self, node):
        if node is None or node[1] != KIND_TRACK:
            return None
        entry = self.scene.live_entry(node[1], node[2])
        element = entry.element if entry is not None else None
        if element is None or not callable(getattr(element, "add", None)):
            return None
        return element

    def _point_target(self):
        """The track that receives a point added outside the tree."""
        return self._ensure_point_target()[0]

    def _ensure_point_target(self):
        """Return the target track and whether this call created it."""
        manager = self.core.trackManager
        get_active = getattr(manager, "get_active", None)
        active = get_active() if callable(get_active) else None
        if active is not None:
            return active, False
        elements = list(manager)
        if len(elements) > 1:
            return None, False
        created = False
        if elements:
            active = elements[0]
        else:
            new_element = getattr(manager, "new_element", None)
            if not callable(new_element):
                return None, False
            active = new_element(points=[])
            created = True
        set_active = getattr(manager, "set_active", None)
        if callable(set_active):
            set_active(active)
        return active, created

    def add_point(self, coordinates):
        """Append a point to the track being edited, creating one if needed."""
        element, created = self._ensure_point_target()
        if element is None:
            return None
        return self._append_point(element, coordinates, created=created)

    def add_poi(self, coordinates):
        """Create a POI at the requested position."""
        create = getattr(self.core.poiManager, "create", None)
        if not callable(create):
            return None
        poi = create(tuple(coordinates))
        self._points_changed()
        return poi

    def add_navigation_request(self, kind, coordinates):
        """Route a map request to a POI mark or a track point."""
        if kind == "poi":
            return self.add_poi(coordinates)
        return self.add_point(coordinates)

    def _append_point(self, element, coordinates, created=False):
        add = getattr(element, "add", None)
        if not callable(add):
            return None
        if self.undo_stack is None:
            add(coordinates[0], coordinates[1])
            self._points_changed()
            return element
        self.undo_stack.push(
            AddPointCommand(
                self._points_changed,
                element,
                coordinates,
                collection=self.core.trackManager,
                created=created,
            )
        )
        return element

    def _points_changed(self):
        self.changed.emit()
        self.refresh()

    def _add_point(self, node):
        element = self._track_element(node)
        created = False
        if element is None:
            element, created = self._ensure_point_target()
        if element is None:
            return
        default = tuple(element.points[-1][:2]) if len(element) else (0.0, 0.0)

        def add(coordinates):
            self._append_point(element, coordinates, created=created)

        self._coordinates_dialog("Add track point", default, add)

    def _delete_point(self, node):
        element = self._point_element(node)
        if element is None:
            return
        index = int(node[3])
        if not 0 <= index < len(element):
            return
        if self.undo_stack is None:
            element.remove(index)
            self._points_changed()
            return
        self.undo_stack.push(RemovePointCommand(self._points_changed, element, [index]))

    def _coordinates_dialog(self, title, default, callback):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        form = QFormLayout(dialog)
        latitude, longitude = QDoubleSpinBox(), QDoubleSpinBox()
        for control, limit, value in (
            (latitude, 90, default[0]),
            (longitude, 180, default[1]),
        ):
            control.setRange(-limit, limit)
            control.setDecimals(6)
            control.setValue(value)
        form.addRow("Latitude", latitude)
        form.addRow("Longitude", longitude)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            callback((latitude.value(), longitude.value()))
        dialog.deleteLater()

    def _edit_point(self, node):
        element = self._point_element(node)
        if element is None:
            return
        index = int(node[3])
        if not 0 <= index < len(element.points):
            return
        point_rounding = getattr(element, "point_rounding", None)
        point_spec = getattr(element, "point_rounding_spec", None)
        current_rounding = normalize_rounding(
            point_rounding(index) if callable(point_rounding) else None
        )
        current_spec = point_spec(index) if callable(point_spec) else None
        spec = current_spec or {}
        dialog = PointEditDialog(
            element.point_name(index),
            rounding=current_rounding,
            coordinates=tuple(element.points[index][:2]),
            radius_nm=spec.get("radius_nm"),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        changed = False
        name = dialog.point_name()
        if name != element.point_name(index):
            changed = element.rename_point(index, name) or changed
        spec_getter = getattr(dialog, "point_rounding_spec", None)
        set_spec = getattr(element, "set_point_rounding_spec", None)
        if callable(spec_getter) and callable(set_spec):
            new_spec = spec_getter()
            if new_spec is not None:
                if new_spec != current_spec:
                    changed = set_spec(index, new_spec) or changed
            else:
                rounding = dialog.point_rounding()
                set_rounding = getattr(element, "set_point_rounding", None)
                if (
                    rounding is not None
                    and callable(set_rounding)
                    and normalize_rounding(current_rounding) != rounding
                ):
                    changed = set_rounding(index, rounding) or changed
        else:
            rounding = dialog.point_rounding()
            set_rounding = getattr(element, "set_point_rounding", None)
            if (
                rounding is not None
                and callable(set_rounding)
                and normalize_rounding(current_rounding) != rounding
            ):
                changed = set_rounding(index, rounding) or changed
        if changed:
            self.changed.emit()
        self.refresh()

    def _selection_changed(self):
        if self.syncing:
            return
        nodes = self._selected_nodes()
        if len(nodes) == 1 and nodes[0][0] == "item":
            self.selected.emit(nodes[0][1], nodes[0][2])

    def _double_clicked(self, row, _column):
        if _column != NAME_COLUMN:
            return
        node = row.data(NAME_COLUMN, NODE_ROLE)
        if node is None:
            return
        if node[0] in ("group", "item", "point"):
            self.tree.editItem(row, NAME_COLUMN)

    def _after_change(self, structural=False):
        changed = self.scene.apply_visibility()
        if changed:
            self._save_visible_elements(changed)
        self.syncing = True
        try:
            self._rebuild()
        finally:
            self.syncing = False
        if changed or structural:
            self.changed.emit()
        if KIND_ROUTING in changed:
            self.routing_visibility_changed.emit()

    def _new_collection(self):
        parent = self._current_group()
        group = self.scene.create_group("Collection", parent)
        self.refresh()
        row = self.tree_item(("group", group.id))
        if row is not None:
            self._expand_group_path(group.id)
            self.tree.setCurrentItem(row)
            self.tree.editItem(row, NAME_COLUMN)
        self.changed.emit()

    def _group_selection(self):
        keys = self._selected_item_keys()
        if not keys:
            return
        parent_chains = []
        for kind, key in keys:
            item = self.scene.item(kind, key)
            parent = item.group if item is not None else None
            chain = []
            while parent is not None:
                chain.append(parent)
                group = self.scene.group(parent)
                parent = group.parent if group is not None else None
            chain.append(None)
            parent_chains.append(chain)
        parent = next(
            (
                candidate
                for candidate in parent_chains[0]
                if all(candidate in chain for chain in parent_chains[1:])
            ),
            None,
        )
        group = self.scene.create_group("Collection", parent, keys)
        self.refresh()
        row = self.tree_item(("group", group.id))
        if row is not None:
            self._expand_group_path(group.id)
            self.tree.setCurrentItem(row)
            self.tree.editItem(row, NAME_COLUMN)
        self.changed.emit()

    def _nodes_for_fit(self, nodes):
        if len(nodes) != 1:
            return ()
        node = nodes[0]
        if node[0] == "point":
            return nodes
        if node[0] == "item" and node[1] in (KIND_TRACK, KIND_ROUTING, KIND_POI):
            return nodes
        return ()

    def _nodes_for_delete(self, nodes):
        deletable = []
        for node in nodes:
            if node[0] == "point":
                if not node[2].startswith("race:"):
                    deletable.append(node)
            elif node[0] == "item" and node[1] in (KIND_TRACK, KIND_ROUTING):
                if node[1] == KIND_TRACK and node[2].startswith("race:"):
                    continue
                deletable.append(node)
        return deletable

    def _context_menu(self, position):
        row = self.tree.itemAt(position)
        if row is not None and row not in self.tree.selectedItems():
            self.tree.clearSelection()
            row.setSelected(True)
        node = row.data(0, NODE_ROLE) if row is not None else None
        menu = ThemedMenu(self)
        builder = MenuBuilder(menu)

        keys = self._selected_item_keys()
        nodes = self._selected_nodes()
        groups = self._selected_groups()
        fit_nodes = self._nodes_for_fit(nodes)
        delete_nodes = self._nodes_for_delete(nodes)
        point_element = self._point_element(node)
        track_element = self._track_element(node)
        point_delete = point_element is not None and not node[2].startswith("race:")
        if point_delete:
            delete_nodes = [
                candidate for candidate in delete_nodes if candidate != node
            ]
        rename = (
            len(groups) == 1 and node is not None and node[0] == "group"
        ) or point_element is not None
        deletable = [
            identifier
            for identifier in groups
            if (
                self.scene.group(identifier) is not None
                and not self.scene.group(identifier).builtin
            )
        ]
        open_log = node is not None and node[0] == "item" and node[1] == KIND_LOG
        edit_track = (
            node is not None
            and node[0] == "item"
            and node[1] == KIND_TRACK
            and not node[2].startswith("race:")
        )

        builder.add("New Collection", self._new_collection, key="N")
        if keys:
            builder.add("Group Selection", self._group_selection, key="G")
        if keys or groups:
            self._add_move_menu(builder, keys, groups, key="M")
        colour_target = self._colour_by_target(node)
        if colour_target is not None:
            builder.section()
            submenu = builder.add_menu("Colour by", key="U")
            sub_builder = MenuBuilder(submenu)
            current = getattr(colour_target, "colour_by", DEFAULT_ROUTE_COLOUR_MODE)
            for mode in ROUTE_COLOUR_MODES:
                action = sub_builder.add(
                    ROUTE_COLOUR_MODE_LABELS[mode],
                    lambda _checked=False, m=mode: self._set_colour_by(node, m),
                )
                action.setCheckable(True)
                action.setChecked(mode == current)
            # Isochrone portrayal is independent of the route-line mode and
            # rides the same routing context menu.
            submenu = builder.add_menu("Isochrones", key="I")
            sub_builder = MenuBuilder(submenu)
            current = getattr(
                colour_target,
                "isochrone_colour_by",
                DEFAULT_ISOCHRONE_COLOUR_MODE,
            )
            modes = _isochrone_modes_for(colour_target)
            if current not in modes:
                current = DEFAULT_ISOCHRONE_COLOUR_MODE
            for mode in modes:
                action = sub_builder.add(
                    ISOCHRONE_COLOUR_MODE_LABELS[mode],
                    lambda _checked=False, m=mode: self._set_isochrone_colour_by(
                        node, m
                    ),
                )
                action.setCheckable(True)
                action.setChecked(mode == current)
        if (
            fit_nodes
            or point_element is not None
            or track_element is not None
            or edit_track
        ):
            builder.section()
            if fit_nodes:
                builder.add("Fit", lambda: self.fit_requested.emit(fit_nodes), key="F")
            if track_element is not None:
                builder.add(
                    "Add Point…",
                    lambda: self._add_point(node),
                    key="P",
                )
            if edit_track:
                builder.add(
                    "Edit Track",
                    lambda: self.edit_track_requested.emit(node[2]),
                    key="E",
                )
            if point_element is not None:
                builder.add(
                    "Edit…",
                    lambda: self._edit_point(node),
                    key="E",
                )
                builder.add(
                    "Move Point",
                    lambda: self._move_point(node),
                    key="V",
                )
        if rename or open_log:
            builder.section()
            if rename:
                builder.add(
                    "Rename Point" if point_element is not None else "Rename",
                    lambda: self.tree.editItem(row, NAME_COLUMN),
                    key="R",
                )
            if open_log:
                builder.add(
                    "Open log",
                    lambda kind=node[1], key=node[2]: self.open_requested.emit(
                        kind, key
                    ),
                    key="O",
                )
        destructive = []
        if point_delete:
            destructive.append(("Delete Point", lambda: self._delete_point(node), "D"))
        if delete_nodes:
            destructive.append(
                (
                    "Delete",
                    lambda: self.delete_requested.emit(delete_nodes),
                    "D",
                )
            )
        if deletable:
            destructive.append(
                (
                    "Delete Collection",
                    lambda ids=tuple(deletable): self._delete_groups(ids),
                    "C",
                )
            )
        if destructive:
            builder.section()
            for text, callback, key in destructive:
                builder.add(text, callback, key=key)
        if menu.actions():
            menu.exec(self.tree.viewport().mapToGlobal(position))

    def _colour_by_target(self, node):
        """The routing element a colour-by menu action applies to, if any."""
        if node is None or node[0] != "item" or node[1] != KIND_ROUTING:
            return None
        live = self.scene.live_entry(node[1], node[2])
        return None if live is None else live.element

    def _set_colour_by(self, node, mode):
        element = self._colour_by_target(node)
        if element is None or getattr(element, "colour_by", None) == mode:
            return
        # The property setter bumps the route revision, which invalidates the
        # viewport overlay cache; emitting changed saves and repaints.
        element.colour_by = mode
        self.changed.emit()

    def _set_isochrone_colour_by(self, node, mode):
        element = self._colour_by_target(node)
        if element is None or getattr(element, "isochrone_colour_by", None) == mode:
            return
        if mode == "time" and mode not in _isochrone_modes_for(element):
            return
        # Separate from route colour and from the isochrone eye toggle. The
        # setter bumps the revision, invalidating cached overlay payloads.
        element.isochrone_colour_by = mode
        self.changed.emit()

    def _delete_groups(self, identifiers):
        changed = False
        for identifier in identifiers:
            changed = self.scene.delete_group(identifier) or changed
        if changed:
            self.refresh()
            self.changed.emit()

    def _add_move_menu(self, builder, keys, groups, key=None):
        targets = [(None, "Scene Collection")]
        targets.extend((group.id, group.name) for group in self.scene.groups.values())
        submenu = builder.add_menu("Move to", key=key)
        sub_builder = MenuBuilder(submenu)
        for identifier, name in targets:
            action = sub_builder.add(name)
            action.triggered.connect(
                lambda _checked=False, target=identifier: self._move_to(
                    keys, groups, target
                )
            )

    def _move_to(self, keys, groups, target):
        changed = False
        for identifier in groups:
            changed = self.scene.move_group(identifier, target) or changed
        changed = self.scene.assign(keys, target) or changed
        if changed:
            self.refresh()
            self._expand_group_path(target)
            for identifier in groups:
                row = self.group_row(identifier)
                if row is not None:
                    self._expand_group_path(identifier)
                    row.setExpanded(True)
            self.changed.emit()

    @staticmethod
    def _inside_legs(row):
        while row is not None:
            node = row.data(NAME_COLUMN, NODE_ROLE)
            if node is not None and node[0] == "routing-legs":
                return True
            row = row.parent()
        return False

    def drop_scene_items(self, event):
        if event.source() is not self.tree:
            event.ignore()
            return
        target_row = self.tree.itemAt(event.position().toPoint())
        node = target_row.data(0, NODE_ROLE) if target_row is not None else None
        if node is None:
            target = None
        elif node[0] == "group":
            target = node[1]
        elif node[0] == "item":
            item = self.scene.item(node[1], node[2])
            target = item.group if item is not None else None
        elif node[0] in ("routing-isochrones", "routing-traces") or self._inside_legs(
            target_row
        ):
            # These rows group one route's contents, not a collection. Dropping
            # there must not move a scene item out to the root.
            event.ignore()
            return
        else:
            target = None
        self._move_to(self._selected_item_keys(), self._selected_groups(), target)
        event.acceptProposedAction()
