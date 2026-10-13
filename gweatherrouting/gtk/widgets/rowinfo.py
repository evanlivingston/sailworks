"""An information affordance shown only on the hovered explorer row."""

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GdkPixbuf, Gtk

from gweatherrouting.common import resource_path


class HoveredRowInfo:
    def __init__(self, view, description):
        self.view, self.description = view, description
        self.hover_path = None
        self.icon = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            resource_path("gweatherrouting", "data/icons/information-symbolic.svg"),
            20,
            20,
            True,
        )
        self.renderer = Gtk.CellRendererPixbuf(xalign=0.5, yalign=0.5)
        self.column = Gtk.TreeViewColumn("", self.renderer)
        self.column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        self.column.set_fixed_width(30)
        self.column.set_cell_data_func(self.renderer, self._render)
        view.append_column(self.column)
        view.add_events(
            Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        view.connect("motion-notify-event", self._motion)
        view.connect("leave-notify-event", self._leave)
        view.set_has_tooltip(True)
        view.connect("query-tooltip", self._query_tooltip)

    def _render(self, column, cell, model, iterator, data=None):
        path = model.get_path(iterator).to_string()
        cell.set_property("pixbuf", self.icon if path == self.hover_path else None)

    def _motion(self, view, event):
        x, y = int(event.x), int(event.y)
        if event.window != view.get_bin_window():
            x, y = view.convert_widget_to_bin_window_coords(x, y)
        hit = view.get_path_at_pos(x, y)
        path = hit[0].to_string() if hit else None
        if path != self.hover_path:
            self.hover_path = path
            view.queue_draw()
        return False

    def _leave(self, *args):
        self.clear()
        return False

    def clear(self):
        self.hover_path = None
        self.view.queue_draw()

    def _query_tooltip(self, view, x, y, keyboard, tooltip):
        if keyboard:
            model, iterator = view.get_selection().get_selected()
            if iterator is None:
                return False
            path = model.get_path(iterator)
        else:
            hit = view.get_path_at_pos(*view.convert_widget_to_bin_window_coords(x, y))
            if not hit or hit[1] != self.column:
                return False
            path = hit[0]
        widget = self.description(view.get_model()[path])
        widget.show_all()
        tooltip.set_custom(widget)
        view.set_tooltip_cell(tooltip, path, self.column, self.renderer)
        return True
