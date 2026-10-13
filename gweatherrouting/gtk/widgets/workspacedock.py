"""A single component dock that follows the active workspace tab."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from .tooldock import ToolDock


class WorkspaceDock(ToolDock):
    def __init__(self, parent=None, width=340):
        self.views = Gtk.Stack()
        self.views.set_homogeneous(False)
        self.mounts = {}
        self._tab_stack = None
        self._tab_handler = None
        super().__init__(self.views, parent, width)
        self.views.show()

    def add_view(self, name, content):
        """Return a slot below the view's toolbar and above its status bar."""
        self.views.add_named(content, name)
        slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.mounts[name] = slot
        return slot

    def bind(self, tabs):
        self._tab_stack = tabs
        self._tab_handler = tabs.connect(
            "notify::visible-child-name", self._tab_changed
        )
        self._tab_changed(tabs, None)

    def _tab_changed(self, tabs, _):
        self.activate(tabs.get_visible_child_name())

    def activate(self, name):
        if name not in self.mounts:
            return
        slot = self.mounts[name]
        parent = self.get_parent()
        if parent is not slot:
            if parent is not None:
                self._preserve_split_positions()
                parent.remove(self)
            slot.pack_start(self, True, True, 0)
        self.views.set_visible_child_name(name)
        # Avoid show_all(): it would reveal intentionally hidden view controls.
        slot.show()
        self.show()

    def _preserve_split_positions(self):
        # Tab toolbars can have different heights. GTK otherwise distributes
        # the height difference between both sides of each vertical divider.
        children = self.sidebar.get_children()
        split = children[0] if children else None
        while isinstance(split, Gtk.Paned):
            self._retain_position(split)
            split = split.get_child2()

    @staticmethod
    def _retain_position(split):
        position = split.get_position()

        def allocated(widget, allocation):
            if allocation.height > 1:
                widget.disconnect(handler)
                widget.set_position(position)

        handler = split.connect("size-allocate", allocated)

    def _destroy(self, *args):
        if self._tab_handler is not None and self._tab_stack is not None:
            self._tab_stack.disconnect(self._tab_handler)
            self._tab_handler = None
        super()._destroy(*args)
