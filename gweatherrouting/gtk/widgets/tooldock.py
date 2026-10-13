"""Reusable tabbed palettes: drag out to float, drag to the right edge to dock."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GObject, Gtk

from ..icons import icon_label


class ToolDock(Gtk.Paned):
    __gsignals__ = {
        "panel-visibility-changed": (GObject.SignalFlags.RUN_FIRST, None, (str, bool)),
    }

    def __init__(self, main_content, parent=None, width=340):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.parent_window = parent
        self.panel_width = width
        self.panels = {}
        self.floating = []
        self._drag = None
        self._group = "tools-" + str(id(self))
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            .tool-palette { border: 1px solid alpha(@theme_fg_color, 0.22); }
            .tool-palette > header { padding: 3px; }
            .tool-palette > header button { padding: 2px; min-height: 18px; min-width: 18px; }
            .tool-palette > header tabs tab { padding: 4px 10px; }
            .tool-dock-drop { border-right: 3px solid #559ddd; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.pack1(main_content, True, False)
        self.notebook = self._notebook()
        self.notebook.set_size_request(width, -1)
        self.notebook.set_no_show_all(True)
        self.pack2(self.notebook, False, False)
        self.connect("destroy", self._destroy)

    def _notebook(self):
        notebook = Gtk.Notebook(scrollable=True, show_border=False)
        notebook.get_style_context().add_class("tool-palette")
        notebook.set_group_name(self._group)
        notebook.connect("create-window", self._detach)
        notebook.connect("page-added", self._page_added)
        notebook.connect("page-removed", self._page_removed)
        close = Gtk.Button.new_from_icon_name(
            "window-close-symbolic", Gtk.IconSize.MENU
        )
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_tooltip_text("Hide this panel group")
        close.connect("clicked", lambda button: self._hide_notebook(notebook))
        notebook.set_action_widget(close, Gtk.PackType.START)
        grip = Gtk.EventBox()
        grip.add(Gtk.Label(label="⠿", margin_start=6, margin_end=6))
        grip.set_tooltip_text("Drag to float; drag to the right chart edge to dock")
        grip.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        grip.connect("button-press-event", self._drag_start, notebook)
        grip.connect("motion-notify-event", self._drag_move)
        grip.connect("button-release-event", self._drag_end)
        notebook.set_action_widget(grip, Gtk.PackType.END)
        close.show_all()
        grip.show_all()
        return notebook

    def register(self, key, title, content, icon_name=None):
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        panel.tool_key, panel.tool_title = key, title
        panel.tool_icon_name = icon_name
        panel.tool_visible = False
        panel.tool_floating = False
        panel.tool_home = self.notebook
        panel.pack_start(content, True, True, 0)
        self.panels[key] = panel
        return panel

    def is_open(self, key):
        return self.panels[key].tool_visible

    def show_panel(self, key):
        panel = self.panels[key]
        if panel.get_parent() is None:
            self._add(panel.tool_home, panel)
        target = panel.get_parent()
        target.set_current_page(target.page_num(panel))
        panel.show_all()
        target.show()
        if target is not self.notebook:
            target.get_toplevel().present()
        if not panel.tool_visible:
            panel.tool_visible = True
            self.emit("panel-visibility-changed", key, True)

    def hide_panel(self, key):
        panel = self.panels[key]
        target = panel.get_parent()
        if target is not None:
            target.remove_page(target.page_num(panel))
        panel.hide()
        if panel.tool_visible:
            panel.tool_visible = False
            self.emit("panel-visibility-changed", key, False)

    def show_group(self, keys):
        for key in reversed(keys):
            self.show_panel(key)
        self.show_panel(keys[0])

    def hide_group(self, keys):
        for key in keys:
            self.hide_panel(key)

    def toggle_group(self, keys):
        if any(self.is_open(key) for key in keys):
            self.hide_group(keys)
        else:
            self.show_group(keys)

    def toggle_panel(self, key):
        if self.is_open(key):
            self.hide_panel(key)
        else:
            self.show_panel(key)

    def toggle_dock(self, key):
        """Programmatic placement; the user moves palettes by dragging."""
        panel = self.panels[key]
        target = self.notebook if panel.tool_floating else self._floating_notebook()
        self._move_panel(panel, target)
        self.show_panel(key)

    def _move_panel(self, panel, target):
        current = panel.get_parent()
        if current is target:
            return
        if current is not None:
            current.remove_page(current.page_num(panel))
        self._add(target, panel)
        panel.show_all()
        target.show()

    def _add(self, notebook, panel):
        label = (
            icon_label(panel.tool_title, panel.tool_icon_name)
            if panel.tool_icon_name
            else Gtk.Label(label=panel.tool_title)
        )
        label.show()
        notebook.append_page(panel, label)

    def _floating_notebook(self, x=None, y=None):
        for window in self.floating:
            if window.get_child().get_n_pages() == 0:
                if x is not None:
                    window.move(x, y)
                window.show_all()
                return window.get_child()
        window = Gtk.Window(title="Tools", decorated=False)
        window.set_transient_for(self.parent_window)
        window.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        window.set_default_size(self.panel_width, 660)
        notebook = self._notebook()
        window.add(notebook)
        window.connect("delete-event", self._close_float, notebook)
        window.connect("button-press-event", self._resize_float)
        self.floating.append(window)
        if x is not None:
            window.move(x, y)
        window.show_all()
        return notebook

    def _at_dock_edge(self, x, y):
        toplevel = self.get_toplevel()
        if not isinstance(toplevel, Gtk.Window) or not toplevel.get_window():
            return False
        origin = toplevel.get_window().get_origin()[-2:]
        offset = self.translate_coordinates(toplevel, 0, 0)
        if offset is None:
            return False
        left, top = origin[0] + offset[0], origin[1] + offset[1]
        right = left + self.get_allocated_width()
        return (
            right - 48 <= x <= right + 24
            and top <= y <= top + self.get_allocated_height()
        )

    def _detach(self, notebook, panel, x, y):
        if self._at_dock_edge(x, y) and notebook is not self.notebook:
            self.notebook.show()
            return self.notebook
        return self._floating_notebook(x, y)

    def _drag_start(self, grip, event, notebook):
        if event.button != 1:
            return False
        self._drag = dict(
            notebook=notebook, x=event.x_root, y=event.y_root, window=None
        )
        return True

    def _drag_move(self, grip, event):
        if self._drag is None:
            return False
        drag = self._drag
        dx, dy = event.x_root - drag["x"], event.y_root - drag["y"]
        if drag["window"] is None:
            if max(abs(dx), abs(dy)) < 6:
                return True
            notebook = drag["notebook"]
            if notebook is self.notebook:
                target = self._floating_notebook(
                    int(drag["x"] - 100), int(drag["y"] - 16)
                )
                for panel in list(notebook.get_children()):
                    self._move_panel(panel, target)
                notebook = target
            window = notebook.get_toplevel()
            drag["window"] = window
            drag["origin"] = window.get_position()
        drag["window"].move(int(drag["origin"][0] + dx), int(drag["origin"][1] + dy))
        context = self.get_style_context()
        if self._at_dock_edge(event.x_root, event.y_root):
            context.add_class("tool-dock-drop")
        else:
            context.remove_class("tool-dock-drop")
        return True

    def _drag_end(self, grip, event):
        drag, self._drag = self._drag, None
        self.get_style_context().remove_class("tool-dock-drop")
        if drag and drag["window"] and self._at_dock_edge(event.x_root, event.y_root):
            self._dock_notebook(drag["window"].get_child())
        if self.notebook.get_n_pages() == 0:
            self.notebook.hide()
        return drag is not None

    def _dock_notebook(self, notebook):
        selected = notebook.get_nth_page(notebook.get_current_page())
        for panel in list(notebook.get_children()):
            self._move_panel(panel, self.notebook)
        if selected is not None:
            self.notebook.set_current_page(self.notebook.page_num(selected))

    @staticmethod
    def _resize_float(window, event):
        if (
            event.button == 1
            and event.x >= window.get_allocated_width() - 12
            and event.y >= window.get_allocated_height() - 12
        ):
            window.begin_resize_drag(
                Gdk.WindowEdge.SOUTH_EAST,
                1,
                int(event.x_root),
                int(event.y_root),
                event.time,
            )
            return True
        return False

    def _page_added(self, notebook, panel, index):
        notebook.set_tab_reorderable(panel, True)
        notebook.set_tab_detachable(panel, True)
        panel.tool_home = notebook
        panel.tool_floating = notebook is not self.notebook
        notebook.show()
        if panel.tool_floating:
            notebook.get_toplevel().set_title(panel.tool_title)
        elif notebook.get_n_pages() == 1:
            self.set_position(max(200, self.get_allocated_width() - self.panel_width))

    def _page_removed(self, notebook, panel, index):
        if notebook.get_n_pages() == 0:
            if notebook is self.notebook:
                if self._drag is None:
                    notebook.hide()
            else:
                notebook.get_toplevel().hide()

    def _hide_notebook(self, notebook):
        for panel in list(notebook.get_children()):
            self.hide_panel(panel.tool_key)

    def _close_float(self, window, event, notebook):
        self._hide_notebook(notebook)
        return True

    def _destroy(self, *args):
        for window in list(self.floating):
            window.destroy()
        self.floating.clear()
        for panel in self.panels.values():
            if panel.get_parent() is None:
                panel.destroy()
