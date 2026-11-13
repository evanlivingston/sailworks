"""Undoable navigation edits shared by the scene tree and the File menu."""

from PySide6.QtGui import QUndoCommand


class RemoveNavigationCommand(QUndoCommand):
    """Undoable removal of one or more tracks or routes."""

    def __init__(self, refresh, manager, removed, current, kind):
        label = "track" if kind == "track" else "route"
        super().__init__(
            f"Remove {len(removed)} {label}{'s' if len(removed) != 1 else ''}"
        )
        self.refresh, self.manager = refresh, manager
        self.removed, self.current, self.kind = removed, current, kind

    def redo(self):
        # Remove every selected element in one batch so the collection is
        # serialized/persisted once instead of once per element.  Removing
        # tracks one at a time made deleting many YB race tracks O(n^2) in
        # both list scans and storage writes, which could hang the UI.
        current_ids = {id(element) for element in self.manager.elements}
        elements = [
            element
            for _, element in reversed(self.removed)
            if id(element) in current_ids
        ]
        if elements:
            self.manager.remove_many(elements)
        if any(element is self.current for _, element in self.removed):
            self.manager.set_active(self.manager[-1] if len(self.manager) else None)
        self.refresh()

    def undo(self):
        for index, element in self.removed:
            if element not in self.manager.elements:
                self.manager.elements.insert(min(index, len(self.manager)), element)
        self.manager.touch()
        self.manager.save()
        self.manager.set_active(self.current)
        self.refresh()


class MovePointCommand(QUndoCommand):
    """Undoable relocation of one track point."""

    def __init__(self, refresh, element, index, before, after):
        super().__init__(f"Move {element.name} point {index + 1}")
        self.refresh = refresh
        self.element = element
        self.index = index
        self.before = (before[0], before[1])
        self.after = (after[0], after[1])

    def _move(self, position):
        if 0 <= self.index < len(self.element):
            self.element.move(self.index, position[0], position[1])
            self.refresh()

    def redo(self):
        self._move(self.after)

    def undo(self):
        self._move(self.before)


class RemovePointCommand(QUndoCommand):
    """Undoable removal of track points, independent of the navigation panel."""

    def __init__(self, refresh, element, indices):
        indices = tuple(sorted({int(index) for index in indices}, reverse=True))
        super().__init__(
            f"Remove {len(indices)} point{'s' if len(indices) != 1 else ''}"
        )
        self.refresh = refresh
        self.element = element
        self.indices = indices
        self.removed = []

    def redo(self):
        self.removed = []
        for index in self.indices:
            if 0 <= index < len(self.element):
                self.removed.append((index, self.element.points[index]))
                self.element.remove(index)
        self.refresh()

    def undo(self):
        for index, point in sorted(self.removed):
            self.element.points.insert(min(index, len(self.element)), point)
        touch = getattr(self.element, "touch", None)
        if callable(touch):
            touch()
        collection = getattr(self.element, "collection", None)
        if collection is not None:
            collection.save()
        self.refresh()
