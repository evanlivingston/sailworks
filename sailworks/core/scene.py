"""User-authored scene tree over navigation data.

The scene collection only stores grouping and visibility. Tracks, routings, POIs,
map shapes and log-library entries remain owned by their existing collections; scene
items refer to them by ``Element.uid`` (or the log-library identifier).
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional
from uuid import uuid4

from .storage import DATA_DIR
from .workspacestate import WorkspaceStateStore

KIND_TRACK = "track"
KIND_ROUTING = "routing"
KIND_POI = "poi"
KIND_LOG = "log"
KIND_SHAPE = "shape"
KINDS = (KIND_TRACK, KIND_ROUTING, KIND_POI, KIND_LOG, KIND_SHAPE)

TYPE_TITLES = {
    KIND_TRACK: "Track",
    KIND_ROUTING: "Routing",
    KIND_POI: "Point of interest",
    KIND_LOG: "Log",
    KIND_SHAPE: "Shape",
}

DEFAULT_GROUPS = (
    (KIND_TRACK, "default-tracks", "Tracks"),
    (KIND_ROUTING, "default-routings", "Routings"),
    (KIND_POI, "default-pois", "POIs"),
    (KIND_LOG, "default-logs", "Logs"),
    (KIND_SHAPE, "default-shapes", "Shapes"),
)


@dataclass(frozen=True)
class SceneEntry:
    """A live item that can appear in the scene tree."""

    kind: str
    key: str
    name: str
    visible: bool = True
    visible_editable: bool = True
    detail: str = ""
    group: Optional[str] = None
    element: object = field(default=None, compare=False, repr=False)


@dataclass
class SceneGroup:
    id: str
    name: str
    parent: Optional[str]
    visible: bool = True
    collapsed: bool = False
    builtin: bool = False


@dataclass
class SceneItem:
    kind: str
    key: str
    name: str
    group: Optional[str]
    visible: bool = True
    element: object = field(default=None, compare=False, repr=False)

    @property
    def identity(self):
        return (self.kind, self.key)


class SceneCollection:
    """Persisted grouping tree with per-item and per-group visibility."""

    def __init__(self, path=None):
        self.store = WorkspaceStateStore(
            Path(path) if path is not None else Path(DATA_DIR) / "scene-qt.json"
        )
        self.groups: dict[str, SceneGroup] = {}
        self.items: dict[tuple[str, str], SceneItem] = {}
        self._live: dict[tuple[str, str], SceneEntry] = {}
        self.load()

    # ------------------------------------------------------------------
    # Persistence

    def load(self):
        saved = self.store.load()
        for value in saved.get("groups", ()):
            if not isinstance(value, dict):
                continue
            identifier = value.get("id")
            if not isinstance(identifier, str) or not identifier:
                continue
            parent = value.get("parent")
            self.groups[identifier] = SceneGroup(
                id=identifier,
                name=str(value.get("name") or "Collection"),
                parent=parent if isinstance(parent, str) else None,
                visible=bool(value.get("visible", True)),
                collapsed=bool(value.get("collapsed", False)),
                builtin=bool(value.get("builtin", False)),
            )
        for value in saved.get("items", ()):
            if not isinstance(value, dict):
                continue
            kind = value.get("kind")
            key = value.get("key")
            if kind not in KINDS or not isinstance(key, str) or not key:
                continue
            group = value.get("group")
            self.items[(kind, key)] = SceneItem(
                kind=kind,
                key=key,
                name=str(value.get("name") or ""),
                group=group if isinstance(group, str) else None,
                visible=bool(value.get("visible", True)),
            )
        self._normalise()
        self.ensure_default_groups()

    def save(self):
        self.store.save(
            {
                "groups": [
                    {
                        "id": group.id,
                        "name": group.name,
                        "parent": group.parent,
                        "visible": group.visible,
                        "collapsed": group.collapsed,
                        "builtin": group.builtin,
                    }
                    for group in self.groups.values()
                ],
                "items": [
                    {
                        "kind": item.kind,
                        "key": item.key,
                        "name": item.name,
                        "group": item.group,
                        "visible": item.visible,
                    }
                    for item in self.items.values()
                ],
            }
        )

    def _normalise(self):
        # Drop dangling parents and keep the graph acyclic.
        for group in self.groups.values():
            if group.parent is not None and group.parent not in self.groups:
                group.parent = None
        for group in self.groups.values():
            seen = set()
            node = group
            while node is not None:
                if node.id in seen:
                    node.parent = None
                    break
                seen.add(node.id)
                node = self.groups.get(node.parent) if node.parent else None
        for item in self.items.values():
            if item.group is not None and item.group not in self.groups:
                item.group = None

    # ------------------------------------------------------------------
    # Defaults and lookup

    def ensure_default_groups(self):
        for _kind, identifier, title in DEFAULT_GROUPS:
            group = self.groups.get(identifier)
            if group is None:
                self.groups[identifier] = SceneGroup(
                    id=identifier,
                    name=title,
                    parent=None,
                    builtin=True,
                )
            else:
                group.builtin = True

    def default_group(self, kind) -> Optional[str]:
        for candidate_kind, identifier, _title in DEFAULT_GROUPS:
            if candidate_kind == kind:
                if identifier not in self.groups:
                    self.ensure_default_groups()
                return identifier
        return None

    def ensure_group(self, identifier, name, parent=None):
        """Create or update a stable, data-backed collection group."""
        parent = parent if parent in self.groups else None
        group = self.groups.get(identifier)
        if group is None:
            group = SceneGroup(
                id=identifier,
                name=self._unique_group_name(name, parent),
                parent=parent,
            )
            self.groups[identifier] = group
            self.save()
        return group

    def group(self, identifier) -> Optional[SceneGroup]:
        return self.groups.get(identifier)

    def item(self, kind, key) -> Optional[SceneItem]:
        return self.items.get((kind, key))

    def live_entry(self, kind, key) -> Optional[SceneEntry]:
        return self._live.get((kind, key))

    def groups_in(self, parent) -> list[SceneGroup]:
        return [group for group in self.groups.values() if group.parent == parent]

    def items_in(self, group) -> list[SceneItem]:
        return [item for item in self.items.values() if item.group == group]

    def group_visible(self, identifier) -> bool:
        group = self.groups.get(identifier)
        while group is not None:
            if not group.visible:
                return False
            group = self.groups.get(group.parent) if group.parent else None
        return True

    def effective_visible(self, kind, key) -> bool:
        item = self.items.get((kind, key))
        if item is None:
            return True
        return item.visible and self.group_visible(item.group)

    # ------------------------------------------------------------------
    # Live data reconciliation

    def reconcile(self, entries: Iterable[SceneEntry]):
        """Match live data against stored assignments and prune stale items."""
        self.ensure_default_groups()
        live = {(entry.kind, entry.key): entry for entry in entries}
        self._live = live
        dirty = False
        for identity in tuple(self.items):
            if identity not in live:
                del self.items[identity]
                dirty = True
        for identity, entry in live.items():
            item = self.items.get(identity)
            if item is None:
                item = SceneItem(
                    kind=entry.kind,
                    key=entry.key,
                    name=entry.name,
                    group=(
                        entry.group
                        if entry.group in self.groups
                        else self.default_group(entry.kind)
                    ),
                    visible=entry.visible if entry.visible_editable else True,
                    element=entry.element,
                )
                self.items[identity] = item
                dirty = True
            else:
                item.name = entry.name
                item.element = entry.element
                if entry.group in self.groups and item.group in (
                    None,
                    self.default_group(entry.kind),
                ):
                    if item.group != entry.group:
                        item.group = entry.group
                        dirty = True
                # Adopt edits made outside the scene only while the item's
                # collection chain is visible. Items hidden by a collection
                # keep their own remembered state.
                if entry.visible_editable and self.group_visible(item.group):
                    if item.visible != entry.visible:
                        item.visible = entry.visible
                        dirty = True
        if dirty:
            self.save()

    def apply_visibility(self):
        """Push effective visibility to live elements; return changed kinds."""
        changed = set()
        for item in self.items.values():
            entry = self._live.get(item.identity)
            element = entry.element if entry is not None else item.element
            if entry is None or element is None:
                continue
            desired = self.effective_visible(item.kind, item.key)
            if getattr(element, "visible", None) != desired:
                element.visible = desired
                changed.add(item.kind)
        return changed

    # ------------------------------------------------------------------
    # Editing

    def _unique_group_name(self, name, parent, ignore=None):
        name = (name or "Collection").strip() or "Collection"
        siblings = {
            group.name for group in self.groups_in(parent) if group.id != ignore
        }
        if name not in siblings:
            return name
        index = 1
        while f"{name}.{index:03d}" in siblings:
            index += 1
        return f"{name}.{index:03d}"

    def create_group(self, name="Collection", parent=None, item_keys=()):
        identifier = uuid4().hex
        group = SceneGroup(
            id=identifier,
            name=self._unique_group_name(name, parent),
            parent=parent if parent in self.groups else None,
        )
        self.groups[identifier] = group
        self.assign(item_keys, identifier, save=False)
        self.save()
        return group

    def rename_group(self, identifier, name):
        group = self.groups.get(identifier)
        if group is None:
            return False
        value = (name or "").strip()
        if not value or value == group.name:
            return False
        group.name = self._unique_group_name(value, group.parent, ignore=identifier)
        self.save()
        return True

    def set_group_visible(self, identifier, visible):
        group = self.groups.get(identifier)
        if group is None or group.visible == bool(visible):
            return False
        group.visible = bool(visible)
        self.save()
        return True

    def set_group_collapsed(self, identifier, collapsed):
        group = self.groups.get(identifier)
        if group is None or group.collapsed == bool(collapsed):
            return False
        group.collapsed = bool(collapsed)
        self.save()
        return True

    def set_item_visible(self, kind, key, visible):
        item = self.items.get((kind, key))
        if item is None or item.visible == bool(visible):
            return False
        item.visible = bool(visible)
        self.save()
        return True

    def assign(self, item_keys, group, save=True):
        valid = group if group is None or group in self.groups else None
        changed = False
        for kind, key in item_keys or ():
            item = self.items.get((kind, key))
            if item is not None and item.group != valid:
                item.group = valid
                changed = True
        if changed and save:
            self.save()
        return changed

    def move_group(self, identifier, parent):
        group = self.groups.get(identifier)
        if group is None:
            return False
        if parent is not None and parent not in self.groups:
            return False
        if parent == identifier or self._is_descendant(parent, identifier):
            return False
        if group.parent == parent:
            return False
        group.parent = parent
        self.save()
        return True

    def _is_descendant(self, candidate, ancestor):
        node = self.groups.get(candidate) if candidate else None
        while node is not None:
            if node.parent == ancestor:
                return True
            node = self.groups.get(node.parent) if node.parent else None
        return False

    def delete_group(self, identifier):
        group = self.groups.get(identifier)
        if group is None or group.builtin:
            return False
        parent = group.parent
        for child in self.groups_in(identifier):
            child.parent = parent
        for item in self.items.values():
            if item.group == identifier:
                item.group = parent
        del self.groups[identifier]
        self.save()
        return True
