"""Blender-style scene collection browser for Qt."""

from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sailworks.common import resource_path
from sailworks.core.geo.rounding import ROUNDING_SHORT, normalize_rounding
from sailworks.core.scene import (
    KIND_LOG,
    KIND_POI,
    KIND_ROUTING,
    KIND_TRACK,
    TYPE_TITLES,
    SceneCollection,
    SceneEntry,
)
from .pointeditdialog import PointEditDialog
from .theme import MenuBuilder, symbol_icon, ThemedMenu

NODE_ROLE = Qt.ItemDataRole.UserRole
COORDINATE_ROLE = Qt.ItemDataRole.UserRole + 1
ROUNDING_ROLE = Qt.ItemDataRole.UserRole + 2
MUTED = QColor("#b0b0ad")
POINT_COORDINATE = QColor("#8a8a87")
ROUNDING_COLOR = QColor("#78aeed")
NAME_COLUMN = 0
VISIBLE_COLUMN = 1
TYPE_ICONS = {
    KIND_TRACK: "track-symbolic",
    KIND_ROUTING: "isochrone-symbolic",
    KIND_POI: "poi-symbolic",
    KIND_LOG: "information-symbolic",
}


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


class SceneTree(QTreeWidget):
    """Tree widget that hands drops back to the scene panel."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel

    def dropEvent(self, event):  # noqa: N802 - Qt virtual method name
        self.panel.drop_scene_items(event)


class SceneItemDelegate(QStyledItemDelegate):
    """Paint point coordinates and mark rounding in a point's name cell."""

    def paint(self, painter, option, index):
        coordinates = index.data(COORDINATE_ROLE)
        if coordinates is None:
            super().paint(painter, option, index)
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
            coordinate_color = label_color
        else:
            foreground = index.data(Qt.ItemDataRole.ForegroundRole)
            label_color = (
                foreground.color()
                if isinstance(foreground, QBrush)
                else adjusted.palette.color(QPalette.ColorRole.Text)
            )
            coordinate_color = POINT_COORDINATE

        rect = adjusted.rect.adjusted(4, 0, -4, 0)
        metrics = adjusted.fontMetrics
        icon_width = 0
        if index.data(Qt.ItemDataRole.DecorationRole) is not None:
            size = adjusted.decorationSize
            if size is None or size.width() <= 0:
                size = widget.iconSize() if widget is not None else None
            icon_width = (size.width() if size is not None else 16) + 4
        label_rect = rect.adjusted(icon_width, 0, 0, 0)
        rounding_text = str(index.data(ROUNDING_ROLE) or "")
        rounding_width = metrics.horizontalAdvance(rounding_text)
        coordinate_text = metrics.elidedText(
            str(coordinates), Qt.TextElideMode.ElideLeft, int(rect.width() * 0.6)
        )
        coordinate_width = metrics.horizontalAdvance(coordinate_text)
        label_width = max(
            0, label_rect.width() - coordinate_width - rounding_width - 16
        )
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
        if rounding_text:
            painter.setPen(label_color if selected else ROUNDING_COLOR)
            painter.drawText(
                rect.adjusted(0, 0, -(coordinate_width + 12), 0),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                rounding_text,
            )
        painter.setPen(coordinate_color)
        painter.drawText(
            rect,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            coordinate_text,
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

    def __init__(self, controller, parent=None, scene_path=None):
        super().__init__(parent)
        self.controller = controller
        self.core = controller.core
        self.scene = SceneCollection(scene_path)
        self.syncing = False
        self._rows = {}
        self._expanded_items = set()
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

        row = QHBoxLayout()
        row.setSpacing(6)
        self.new_button = QPushButton("+ Collection")
        self.new_button.clicked.connect(self._new_collection)
        row.addWidget(self.new_button)
        self.group_button = QPushButton("Group Selection")
        self.group_button.clicked.connect(self._group_selection)
        row.addWidget(self.group_button)
        layout.addLayout(row)

        self.refresh()

    # ------------------------------------------------------------------
    # Live data

    def _entries(self, library_entries):
        entries = []
        for track in self.core.trackManager:
            details = (
                "Race track"
                if getattr(track, "race_track", False)
                else f"{len(track)} fixes"
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
                            detail=f"{race_name} · {len(track.points)} race fixes",
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
            row.setToolTip(
                NAME_COLUMN,
                f"{entry.name}\n{TYPE_TITLES.get(entry.kind, '')}\n{detail}",
            )
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
        rename_point = getattr(element, "rename_point", None)
        point_name = getattr(element, "point_name", None)
        point_rounding = getattr(element, "point_rounding", None)
        default_label = "Mark" if entry.kind == KIND_ROUTING else "Point"
        for index, point in enumerate(element.points):
            name = point_name(index) if callable(point_name) else ""
            label = name or f"{default_label} {index + 1}"
            coordinates = f"{point[0]:.6f}, {point[1]:.6f}"
            child = QTreeWidgetItem([label, ""])
            child.setData(
                NAME_COLUMN, NODE_ROLE, ("point", entry.kind, entry.key, index)
            )
            child.setData(NAME_COLUMN, COORDINATE_ROLE, coordinates)
            if callable(point_rounding):
                child.setData(
                    NAME_COLUMN,
                    ROUNDING_ROLE,
                    ROUNDING_SHORT[normalize_rounding(point_rounding(index))],
                )
            child.setIcon(NAME_COLUMN, symbol_icon("poi-symbolic"))
            flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            if callable(rename_point):
                flags |= Qt.ItemFlag.ItemIsEditable
            child.setFlags(flags)
            child.setForeground(NAME_COLUMN, QBrush(MUTED))
            row.addChild(child)

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
        if element.rename_point(node[3], name):
            self.changed.emit()
        self.refresh()

    def _move_point(self, node):
        element = self._point_element(node)
        if element is not None:
            self.move_point_requested.emit(element, int(node[3]))

    def _edit_point(self, node):
        element = self._point_element(node)
        if element is None:
            return
        index = int(node[3])
        if not 0 <= index < len(element.points):
            return
        point_rounding = getattr(element, "point_rounding", None)
        current_rounding = normalize_rounding(
            point_rounding(index) if callable(point_rounding) else None
        )
        dialog = PointEditDialog(
            element.point_name(index),
            rounding=current_rounding,
            coordinates=tuple(element.points[index][:2]),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        changed = False
        name = dialog.point_name()
        if name != element.point_name(index):
            changed = element.rename_point(index, name) or changed
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

    def _cycle_rounding(self, node):
        element = self._point_element(node)
        if element is None:
            return
        set_rounding = getattr(element, "set_point_rounding", None)
        if not callable(set_rounding):
            return
        index = int(node[3])
        point_rounding = getattr(element, "point_rounding", None)
        current = normalize_rounding(
            point_rounding(index) if callable(point_rounding) else None
        )
        if set_rounding(index, (current + 1) % 4):
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

        builder.add("New Collection", self._new_collection, key="N")
        if keys:
            builder.add("Group Selection", self._group_selection, key="G")
        if keys or groups:
            self._add_move_menu(builder, keys, groups, key="M")
        if fit_nodes or point_element is not None:
            builder.section()
            if fit_nodes:
                builder.add("Fit", lambda: self.fit_requested.emit(fit_nodes), key="F")
            if point_element is not None:
                builder.add(
                    "Edit…",
                    lambda: self._edit_point(node),
                    key="E",
                )
                builder.add(
                    "Move",
                    lambda: self._move_point(node),
                    key="V",
                )
                builder.add(
                    "Cycle Rounding",
                    lambda: self._cycle_rounding(node),
                    key="C",
                )
        if rename or open_log:
            builder.section()
            if rename:
                builder.add(
                    "Rename",
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
        else:
            target = None
        self._move_to(self._selected_item_keys(), self._selected_groups(), target)
        event.acceptProposedAction()
