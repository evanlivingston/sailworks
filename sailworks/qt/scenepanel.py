"""Blender-style scene collection browser for Qt."""

from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QMenu,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sailworks.core.scene import (
    KIND_LOG,
    KIND_ROUTING,
    KIND_TRACK,
    TYPE_TITLES,
    SceneCollection,
    SceneEntry,
)

NODE_ROLE = Qt.ItemDataRole.UserRole
MUTED = QColor("#b0b0ad")


class SceneTree(QTreeWidget):
    """Tree widget that hands drops back to the scene panel."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel

    def dropEvent(self, event):  # noqa: N802 - Qt virtual method name
        self.panel.drop_scene_items(event)


class ScenePanel(QWidget):
    """Compact tree of collections over tracks, routings and logs."""

    changed = Signal()
    selected = Signal(str, str)
    open_requested = Signal(str, str)

    def __init__(self, controller, parent=None, scene_path=None):
        super().__init__(parent)
        self.controller = controller
        self.core = controller.core
        self.scene = SceneCollection(scene_path)
        self.syncing = False
        self._rows = {}
        self._expanded_items = set()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        layout.setSpacing(6)

        self.tree = SceneTree(self)
        self.tree.setColumnCount(2)
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)
        self.tree.setDragEnabled(True)
        self.tree.setAcceptDrops(True)
        self.tree.setDropIndicatorShown(True)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
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

    def _entries(self):
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
        entry = self._loaded_log_entry()
        if entry is not None:
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

    def _loaded_log_entry(self):
        """Return the library entry for the log currently loaded in the session."""
        session = getattr(self.controller, "session", None)
        log = getattr(session, "log", None) if session is not None else None
        path = getattr(log, "path", None)
        if not path:
            return None
        target = Path(path).resolve()
        library = self.controller.log_library
        for entry in library.entries():
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
            self.scene.reconcile(self._entries())
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

    # ------------------------------------------------------------------
    # Tree construction

    def _rebuild(self):
        selected = self._selected_nodes()
        with QSignalBlocker(self.tree):
            self.tree.clear()
            self._rows = {}
            root = QTreeWidgetItem(["Scene Collection", ""])
            root.setData(0, NODE_ROLE, ("root",))
            font = QFont(root.font(0))
            font.setBold(True)
            root.setFont(0, font)
            root.setForeground(1, QBrush(MUTED))
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
            row = QTreeWidgetItem([group.name, "Collection"])
            row.setData(0, NODE_ROLE, ("group", group.id))
            row.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEditable
                | Qt.ItemFlag.ItemIsDragEnabled
                | Qt.ItemFlag.ItemIsDropEnabled
            )
            row.setCheckState(
                0,
                Qt.CheckState.Checked if group.visible else Qt.CheckState.Unchecked,
            )
            row.setForeground(1, QBrush(MUTED))
            row.setToolTip(
                0,
                "Collection. Uncheck to hide every item inside."
                + (" Built-in." if group.builtin else ""),
            )
            if not self.scene.group_visible(group.id):
                row.setForeground(0, QBrush(MUTED))
            else:
                font = QFont(row.font(0))
                font.setBold(True)
                row.setFont(0, font)
            parent_item.addChild(row)
            self._rows[("group", group.id)] = row
            self._add_groups(row, group.id)
            self._add_items(row, group.id)

    def _add_items(self, parent_item, group_id):
        for entry in self.scene.items_in(group_id):
            row = QTreeWidgetItem([entry.name, TYPE_TITLES.get(entry.kind, "")])
            row.setData(0, NODE_ROLE, ("item", entry.kind, entry.key))
            flags = (
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsDragEnabled
            )
            if entry.kind in (KIND_TRACK, KIND_ROUTING):
                flags |= Qt.ItemFlag.ItemIsUserCheckable
            if entry.kind in (KIND_TRACK, KIND_ROUTING, KIND_LOG):
                flags |= Qt.ItemFlag.ItemIsEditable
                row.setCheckState(
                    0,
                    Qt.CheckState.Checked if entry.visible else Qt.CheckState.Unchecked,
                )
            row.setFlags(flags)
            live = self.scene.live_entry(entry.kind, entry.key)
            detail = live.detail if live is not None else ""
            row.setToolTip(
                0, f"{entry.name}\n{TYPE_TITLES.get(entry.kind, '')}\n{detail}"
            )
            row.setForeground(1, QBrush(MUTED))
            if not self.scene.effective_visible(entry.kind, entry.key):
                row.setForeground(0, QBrush(MUTED))
                row.setForeground(1, QBrush(MUTED))
            parent_item.addChild(row)
            self._rows[("item", entry.kind, entry.key)] = row
            live = self.scene.live_entry(entry.kind, entry.key)
            if (
                entry.kind == KIND_TRACK
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
        """Show a track's recorded positions beneath its expanded tree row."""
        if row.childCount():
            return
        live = self.scene.live_entry(entry.kind, entry.key)
        if live is None or live.element is None:
            return
        for index, point in enumerate(live.element.points):
            child = QTreeWidgetItem(
                [f"Point {index + 1}", f"{point[0]:.6f}, {point[1]:.6f}"]
            )
            child.setData(0, NODE_ROLE, ("point", entry.kind, entry.key, index))
            child.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            child.setForeground(0, QBrush(MUTED))
            child.setForeground(1, QBrush(MUTED))
            child.setToolTip(0, f"{point[0]:.6f}, {point[1]:.6f}")
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
            name = row.text(0).strip()
            if name and name != group.name:
                changed = self.scene.rename_group(group.id, name) or changed
                structural = True
            visible = row.checkState(0) == Qt.CheckState.Checked
            if visible != group.visible:
                changed = self.scene.set_group_visible(group.id, visible) or changed
        elif node[0] == "item":
            item = self.scene.item(node[1], node[2])
            if item is None:
                return
            name = row.text(0).strip()
            if name and name != item.name:
                self._rename_item(item, name)
                return
            if item.kind == KIND_LOG:
                return
            visible = row.checkState(0) == Qt.CheckState.Checked
            if visible != item.visible:
                changed = (
                    self.scene.set_item_visible(node[1], node[2], visible) or changed
                )
        if changed:
            self._after_change(structural=structural)

    def _expanded_changed(self, row):
        if self.syncing:
            return
        node = row.data(0, NODE_ROLE)
        if node is None:
            return
        if node[0] == "group":
            self.scene.set_group_collapsed(node[1], False)
        elif node[0] == "item" and node[1] == KIND_TRACK:
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
        elif node[0] == "item" and node[1] == KIND_TRACK:
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
            manager = (
                self.core.trackManager
                if item.kind == KIND_TRACK
                else self.core.routingManager
            )
            entry.element.name = manager.get_unique_name(name)
            manager.save()
        self.refresh()
        self.changed.emit()

    def _selection_changed(self):
        if self.syncing:
            return
        nodes = self._selected_nodes()
        if len(nodes) == 1 and nodes[0][0] == "item":
            self.selected.emit(nodes[0][1], nodes[0][2])

    def _double_clicked(self, row, _column):
        if _column != 0:
            return
        node = row.data(0, NODE_ROLE)
        if node is None:
            return
        if node[0] in ("group", "item"):
            self.tree.editItem(row, 0)

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
            self.tree.editItem(row, 0)
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
            self.tree.editItem(row, 0)
        self.changed.emit()

    def _context_menu(self, position):
        row = self.tree.itemAt(position)
        if row is not None and row not in self.tree.selectedItems():
            self.tree.clearSelection()
            row.setSelected(True)
        node = row.data(0, NODE_ROLE) if row is not None else None
        menu = QMenu(self)

        menu.addAction("New Collection", self._new_collection)
        keys = self._selected_item_keys()
        if keys:
            menu.addAction("Group Selection", self._group_selection)
        groups = self._selected_groups()
        if len(groups) == 1 and node is not None and node[0] == "group":
            menu.addAction("Rename", lambda: self.tree.editItem(row, 0))
        deletable = [
            identifier
            for identifier in groups
            if (
                self.scene.group(identifier) is not None
                and not self.scene.group(identifier).builtin
            )
        ]
        if deletable:
            menu.addAction(
                "Delete Collection",
                lambda ids=tuple(deletable): self._delete_groups(ids),
            )
        if node is not None and node[0] == "item" and node[1] == KIND_LOG:
            menu.addAction(
                "Open log",
                lambda kind=node[1], key=node[2]: self.open_requested.emit(kind, key),
            )
        if keys or groups:
            self._add_move_menu(menu, keys, groups)
        if menu.actions():
            menu.exec(self.tree.viewport().mapToGlobal(position))

    def _delete_groups(self, identifiers):
        changed = False
        for identifier in identifiers:
            changed = self.scene.delete_group(identifier) or changed
        if changed:
            self.refresh()
            self.changed.emit()

    def _add_move_menu(self, menu, keys, groups):
        targets = [(None, "Scene Collection")]
        targets.extend((group.id, group.name) for group in self.scene.groups.values())
        submenu = menu.addMenu("Move to")
        for identifier, name in targets:
            action = submenu.addAction(name)
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
