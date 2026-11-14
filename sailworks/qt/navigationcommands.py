"""Undoable navigation edits shared by the scene tree and the File menu."""

from PySide6.QtGui import QUndoCommand

from sailworks.core.geo.mapshapes import ShapeValidationError, validate_vertices


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


class AddPointCommand(QUndoCommand):
    """Undoable append of a track point, including implicit track creation.

    Adding a point when no track exists creates the track as a side effect.
    Undo therefore also removes that track, and redo re-creates it before
    appending the point again.
    """

    def __init__(self, refresh, element, coordinates, collection=None, created=False):
        super().__init__("Add point")
        self.refresh = refresh
        self.element = element
        self.coordinates = (coordinates[0], coordinates[1])
        self.collection = collection
        self.created = bool(created)
        self.index = len(element)
        self.track_index = None
        if self.created:
            elements = self._sequence(self._collection())
            if elements is not None:
                self.track_index = (
                    elements.index(element) if element in elements else len(elements)
                )

    def _collection(self):
        if self.collection is not None:
            return self.collection
        return getattr(self.element, "collection", None)

    def _sequence(self, collection):
        if collection is None:
            return None
        elements = getattr(collection, "elements", None)
        if elements is not None:
            return elements
        if hasattr(collection, "insert") and hasattr(collection, "__contains__"):
            return collection
        return None

    def _persist(self, collection):
        for name in ("touch", "save"):
            method = getattr(collection, name, None)
            if callable(method):
                method()

    def _insert_track(self):
        if not self.created:
            return
        collection = self._collection()
        elements = self._sequence(collection)
        if elements is None or self.element in elements:
            return
        index = len(elements) if self.track_index is None else self.track_index
        elements.insert(min(index, len(elements)), self.element)
        self._persist(collection)
        set_active = getattr(collection, "set_active", None)
        if callable(set_active):
            set_active(self.element)

    def _remove_track(self):
        if not self.created:
            return
        collection = self._collection()
        elements = self._sequence(collection)
        if elements is None or self.element not in elements:
            return
        self.track_index = elements.index(self.element)
        elements.remove(self.element)
        self._persist(collection)
        set_active = getattr(collection, "set_active", None)
        if callable(set_active):
            set_active(elements[-1] if len(elements) else None)

    def redo(self):
        self._insert_track()
        if self.index >= len(self.element):
            self.element.add(self.coordinates[0], self.coordinates[1])
        self.refresh()

    def undo(self):
        if 0 <= self.index < len(self.element):
            self.element.remove(self.index)
        self._remove_track()
        self.refresh()


class SetShapeVerticesCommand(QUndoCommand):
    """Undoable replacement of one shape's complete vertex ring.

    A corner move and an edge insertion both end as a new ring, so each
    records full before/after copies of the vertices. Undo therefore restores
    the exact vertex order of the earlier state rather than an inferred edit.
    Each redo/undo state change is persisted once, then the refresh callback
    republishes the overlay and announces the change.
    """

    def __init__(self, refresh, manager, element, before, after, label="Edit shape"):
        super().__init__(label)
        self.refresh = refresh
        self.manager = manager
        self.element = element
        self.before = tuple(tuple(point) for point in before)
        self.after = tuple(tuple(point) for point in after)
        self.error = ""
        self._applied = False

    def _restore(self, vertices):
        try:
            self.element.vertices = vertices
            return True
        except ShapeValidationError:
            return False

    def _apply(self, vertices):
        """Validate, store, persist and republish one geometry state."""
        previous = tuple(tuple(point) for point in self.element.vertices)
        try:
            # The whole ring is revalidated: a stored state that no longer
            # fits the shape is refused instead of reaching memory or disk.
            validate_vertices(self.element.kind, vertices)
        except ShapeValidationError as error:
            self.error = str(error)
            return False
        self.element.vertices = vertices
        save = getattr(self.manager, "save", None)
        if callable(save):
            try:
                save()
            except OSError as error:
                self._restore(previous)
                self.error = f"could not save the shape: {error}"
                return False
        self.error = ""
        self._applied = True
        self.refresh()
        return True

    def redo(self):
        self._apply(self.after)

    def undo(self):
        if not self._applied:
            # A redo that failed already restored the previous geometry, so
            # there is nothing to reverse and nothing to write.
            return
        self._apply(self.before)


class RenamePointCommand(QUndoCommand):
    """Undoable rename of one track point."""

    def __init__(self, refresh, element, index, before, after):
        super().__init__("Rename point")
        self.refresh = refresh
        self.element = element
        self.index = int(index)
        self.before = before
        self.after = after

    def _rename(self, name):
        if 0 <= self.index < len(self.element):
            self.element.rename_point(self.index, name)
            self.refresh()

    def redo(self):
        self._rename(self.after)

    def undo(self):
        self._rename(self.before)


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
