"""Dockable palettes with tab groups, vertical splits, and floating windows."""

from typing import Tuple, List, Any

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GObject, Gtk

from ..icons import icon_label
from gweatherrouting.core.workspacestate import mapping, number, window_geometry


class ToolDock(Gtk.Paned):
    __gsignals__ = {
        "layout-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "panel-visibility-changed": (GObject.SignalFlags.RUN_FIRST, None, (str, bool)),
    }

    def __init__(self, main_content, parent=None, width=340):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.parent_window = parent
        self.panel_width = width
        self.panels = {}
        self.floating = []
        self._drag = None
        self._drop_preview = None
        self._position_pending = False
        self._divider_width = 0
        self._destroying = False
        self._dock_layout: Tuple[Gtk.Notebook, ...] = ()
        self._group = "tools-" + str(id(self))
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            .tool-palette { border: none; box-shadow: none; outline: none; }
            window.tool-floating { border-radius: 8px; }
            window.tool-floating decoration {
                border: none;
                box-shadow: 0 3px 9px 1px rgba(0, 0, 0, 0.5);
                outline: none;
                border-radius: 8px;
            }
            window.tool-floating > headerbar {
                min-height: 8px;
                padding: 0;
                border: none;
                margin: 0;
                border-radius: 8px 8px 0 0;
            }
            window.tool-floating .tool-palette { margin-bottom: 8px; }
            .tool-palette > header { padding: 3px; }
            .tool-palette > header button { padding: 2px; min-height: 18px; min-width: 18px; }
            .tool-palette > header tabs tab { padding: 0; }
            .tool-dock-drop { border-right: 3px solid #559ddd; }
            .tool-stack-above { border-top: 3px solid #559ddd; }
            .tool-stack-below { border-bottom: 3px solid #559ddd; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.pack1(main_content, True, False)
        self.notebook = self._notebook()
        self.docked_notebooks = [self.notebook]
        self.notebook.set_no_show_all(True)
        self.sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.sidebar.set_size_request(width, -1)
        self.sidebar.set_no_show_all(True)
        self.pack2(self.sidebar, False, False)
        self.connect("destroy", self._destroy)
        self.connect("size-allocate", self._size_allocated)
        self.connect("notify::position", lambda *_: self.emit("layout-changed"))

    def _size_allocated(self, widget, allocation):
        visible = self.sidebar.get_visible() and self.sidebar.get_allocated_width() > 1
        if visible:
            self._divider_width = max(
                0,
                allocation.width
                - self.get_child1().get_allocated_width()
                - self.sidebar.get_allocated_width(),
            )
        if self._position_pending and allocation.width > self.panel_width:
            self._position_pending = False
            self.set_position(
                max(200, allocation.width - self.panel_width - self._divider_width)
            )
        elif visible:
            self.panel_width = self.sidebar.get_allocated_width()

    def _notebook(self):
        notebook = Gtk.Notebook(scrollable=True, show_border=False)
        notebook.get_style_context().add_class("tool-palette")
        notebook.set_group_name(self._group)
        notebook.connect("page-added", self._page_added)
        notebook.connect("page-removed", self._page_removed)
        notebook.connect("switch-page", lambda *_: self.emit("layout-changed"))
        close = Gtk.Button.new_from_icon_name(
            "window-close-symbolic", Gtk.IconSize.MENU
        )
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_tooltip_text("Hide this panel group")
        close.connect("clicked", lambda button: self._hide_notebook(notebook))
        notebook.set_action_widget(close, Gtk.PackType.START)
        grip = Gtk.EventBox()
        grip.add(Gtk.Label(label="⠿", margin_start=6, margin_end=6))
        grip.set_tooltip_text(
            "Drag to float or dock. Drop above/below a component to stack; "
            "drop in its middle to group as tabs."
        )
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
        self.emit("layout-changed")
        return panel

    def is_open(self, key):
        return self.panels[key].tool_visible

    def show_panel(self, key):
        panel = self.panels[key]
        if panel.get_parent() is None:
            self._add(panel.tool_home, panel)
        target = panel.get_parent()
        panel.show_all()
        target.set_current_page(target.page_num(panel))
        target.show()
        if target not in self.docked_notebooks:
            target.get_toplevel().present()
        if not panel.tool_visible:
            panel.tool_visible = True
            self.emit("panel-visibility-changed", key, True)
        self.emit("layout-changed")

    def hide_panel(self, key):
        panel = self.panels[key]
        target = panel.get_parent()
        if target is not None:
            target.remove_page(target.page_num(panel))
        panel.hide()
        if panel.tool_visible:
            panel.tool_visible = False
            self.emit("panel-visibility-changed", key, False)
        self.emit("layout-changed")

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
        target.set_current_page(target.page_num(panel))
        self.emit("layout-changed")

    def _split_group(self, target, placement):
        for old in tuple(self.docked_notebooks):
            if old is self.notebook or old is target or old.get_n_pages():
                continue
            if not any(panel.tool_home is old for panel in self.panels.values()):
                self.docked_notebooks.remove(old)
                old.destroy()
        notebook = self._notebook()
        notebook.set_no_show_all(True)
        index = self.docked_notebooks.index(target) + (placement == "below")
        self.docked_notebooks.insert(index, notebook)
        return notebook

    def _rebuild_dock(self):
        if self._destroying:
            return
        groups = tuple(n for n in self.docked_notebooks if n.get_n_pages())
        if groups == self._dock_layout:
            return
        weights = {n: max(n.get_allocated_height(), 100) for n in self._dock_layout}
        self._dock_layout = groups
        for notebook in self.docked_notebooks:
            parent = notebook.get_parent()
            if parent is not None:
                parent.remove(notebook)
        for child in self.sidebar.get_children():
            self.sidebar.remove(child)
            child.destroy()
        if not groups:
            self.sidebar.hide()
            return
        # New groups initially share the existing height evenly. Later rebuilds
        # retain the proportions chosen with the draggable vertical dividers.
        if any(n not in weights for n in groups):
            weights = dict.fromkeys(groups, 1)
        root = groups[-1]
        for index in range(len(groups) - 2, -1, -1):
            split = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
            split.connect("notify::position", lambda *_: self.emit("layout-changed"))
            split.pack1(groups[index], True, False)
            split.pack2(root, True, False)
            fraction = weights[groups[index]] / sum(weights[n] for n in groups[index:])
            self._position_split(split, fraction)
            split.show()
            root = split
        self.sidebar.pack_start(root, True, True, 0)
        for notebook in groups:
            notebook.show()
        self.sidebar.show()

    @staticmethod
    def _position_split(split, fraction):
        def allocated(widget, allocation):
            if allocation.height > 1:
                widget.disconnect(handler)
                widget.set_position(round(allocation.height * fraction))

        handler = split.connect("size-allocate", allocated)

    @staticmethod
    def _split_zone(notebook, y):
        height = notebook.get_allocated_height()
        # Keep the tab strip available for ordinary tab reordering/merging.
        if y < 40:
            return "tab"
        if y < max(64, height * 0.25):
            return "above"
        if y > height * 0.75:
            return "below"
        return "tab"

    def _clear_drop_preview(self):
        self._drop_preview = None
        self.get_style_context().remove_class("tool-dock-drop")
        for notebook in self.docked_notebooks:
            for style in ("tool-stack-above", "tool-stack-below"):
                notebook.get_style_context().remove_class(style)

    def _preview_drop(self, target):
        if target == self._drop_preview:
            return
        self._clear_drop_preview()
        self._drop_preview = target
        if target is None:
            return
        notebook, placement = target
        if placement == "tab":
            self.get_style_context().add_class("tool-dock-drop")
        else:
            notebook.get_style_context().add_class("tool-stack-" + placement)

    def _add(self, notebook, panel):
        label = (
            icon_label(panel.tool_title, panel.tool_icon_name)
            if panel.tool_icon_name
            else Gtk.Label(label=panel.tool_title)
        )
        tab = Gtk.EventBox()
        label.set_margin_start(10)
        label.set_margin_end(10)
        label.set_margin_top(4)
        label.set_margin_bottom(4)
        tab.add(label)
        tab.set_tooltip_text("Drag to stack, group as tabs, or float")
        tab.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        tab.connect("button-press-event", self._drag_start, notebook, panel)
        tab.connect("motion-notify-event", self._drag_move)
        tab.connect("button-release-event", self._drag_end)
        tab.show_all()
        notebook.append_page(panel, tab)

    def _floating_notebook(self, x=None, y=None, reuse=True):
        for window in self.floating if reuse else ():
            if window.get_child().get_n_pages() == 0:
                if x is not None:
                    window.move(x, y)
                window.show_all()
                return window.get_child()
        window = Gtk.Window(title="Tools")
        window.get_style_context().add_class("tool-floating")
        # A custom titlebar uses the same rounded window treatment as our
        # dialogs. Keep it empty: the palette tabs provide the visible header.
        header = Gtk.HeaderBar()
        header.set_custom_title(Gtk.Box())
        window.set_titlebar(header)
        window.set_transient_for(self.parent_window)
        window.set_default_size(self.panel_width, 660)
        notebook = self._notebook()
        window.add(notebook)
        window.connect("delete-event", self._close_float, notebook)
        window.connect("button-press-event", self._resize_float)
        window.connect("configure-event", lambda *_: self.emit("layout-changed"))
        self.floating.append(window)
        if x is not None:
            window.move(x, y)
        window.show_all()
        return notebook

    def _at_dock_edge(self, x, y):
        return self._dock_drop_target(x, y) is not None

    def _dock_drop_target(self, x, y):
        toplevel = self.get_toplevel()
        if not isinstance(toplevel, Gtk.Window) or not toplevel.get_window():
            return None
        origin = toplevel.get_window().get_origin()[-2:]
        offset = self.translate_coordinates(toplevel, 0, 0)
        if offset is None:
            return None
        left, top = origin[0] + offset[0], origin[1] + offset[1]
        right = left + self.get_allocated_width()
        if not top <= y <= top + self.get_allocated_height():
            return None
        for notebook in self._dock_layout:
            position = notebook.translate_coordinates(toplevel, 0, 0)
            if position is None:
                continue
            nx, ny = origin[0] + position[0], origin[1] + position[1]
            if (
                nx <= x <= right + 24
                and ny <= y <= ny + notebook.get_allocated_height()
            ):
                return notebook, self._split_zone(notebook, y - ny)
        if right - 48 <= x <= right + 24:
            return self.notebook, "tab"
        return None

    def _drag_start(self, grip, event, notebook, panel=None):
        if event.button != 1:
            return False
        if self._drag is not None:
            self._release_grab(self._drag)
        if panel is not None:
            notebook.set_current_page(notebook.page_num(panel))
            notebook.grab_focus()
        self._drag = dict(
            notebook=notebook,
            panel=panel,
            grip=grip,
            x=event.x_root,
            y=event.y_root,
            started=False,
            origin=notebook.get_toplevel().get_position(),
        )
        # Keep the event source mapped until release. Reparenting on the first
        # motion loses the pointer grab (and the release event) on macOS.
        if grip is not None:
            grip.grab_add()
        return True

    def _drag_move(self, grip, event):
        if self._drag is None:
            return False
        drag = self._drag
        dx, dy = event.x_root - drag["x"], event.y_root - drag["y"]
        if not drag["started"] and max(abs(dx), abs(dy)) < 6:
            return True
        drag["started"] = True
        notebook = drag["notebook"]
        if drag["panel"] is None and notebook not in self.docked_notebooks:
            notebook.get_toplevel().move(
                int(drag["origin"][0] + dx), int(drag["origin"][1] + dy)
            )
        self._preview_drop(self._dock_drop_target(event.x_root, event.y_root))
        return True

    def _drag_end(self, grip, event):
        drag, self._drag = self._drag, None
        if drag is None:
            return False
        self._release_grab(drag)
        self._clear_drop_preview()
        if not drag["started"]:
            return True
        target = self._dock_drop_target(event.x_root, event.y_root)
        if drag["panel"] is not None:
            self._drop_panel(drag["panel"], target, event.x_root, event.y_root)
        elif target is not None:
            self._dock_notebook(drag["notebook"], *target)
        elif drag["notebook"] in self.docked_notebooks:
            floating = self._floating_notebook(
                int(event.x_root - 100), int(event.y_root - 16)
            )
            self._dock_notebook(drag["notebook"], floating)
        return True

    @staticmethod
    def _release_grab(drag):
        grip = drag["grip"]
        if grip is not None and grip.has_grab():
            grip.grab_remove()

    def _drop_panel(self, panel, target, x, y):
        source = panel.get_parent()
        if target is None:
            if source not in self.docked_notebooks and source.get_n_pages() == 1:
                source.get_toplevel().move(int(x - 100), int(y - 16))
                return
            notebook = self._floating_notebook(int(x - 100), int(y - 16))
        else:
            notebook, placement = target
            if notebook is source and source.get_n_pages() == 1:
                return
            if placement != "tab":
                notebook = self._split_group(notebook, placement)
            elif notebook is source:
                self._reorder_tab(panel, x)
                return
        self._move_panel(panel, notebook)

    def _reorder_tab(self, panel, x):
        notebook = panel.get_parent()
        toplevel = notebook.get_toplevel()
        origin = toplevel.get_window().get_origin()[-2:]
        index = 0
        for other in notebook.get_children():
            if other is panel:
                continue
            label = notebook.get_tab_label(other)
            position = label.translate_coordinates(
                toplevel, label.get_allocated_width() // 2, 0
            )
            if position is not None and x < origin[0] + position[0]:
                break
            index += 1
        notebook.reorder_child(panel, index)
        self.emit("layout-changed")

    def _dock_notebook(self, notebook, target=None, placement="tab"):
        target = self.notebook if target is None else target
        if target is notebook:
            return
        if placement != "tab":
            target = self._split_group(target, placement)
        selected = notebook.get_nth_page(notebook.get_current_page())
        for panel in list(notebook.get_children()):
            self._move_panel(panel, target)
        if selected is not None:
            target.set_current_page(target.page_num(selected))

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
        notebook.set_tab_reorderable(panel, False)
        notebook.set_tab_detachable(panel, False)
        panel.tool_home = notebook
        panel.tool_floating = notebook not in self.docked_notebooks
        notebook.show()
        if panel.tool_floating:
            notebook.get_toplevel().set_title(panel.tool_title)
        else:
            first = not self.sidebar.get_visible()
            self._rebuild_dock()
            if first:
                self._set_sidebar_width()

    def _set_sidebar_width(self):
        width = self.get_allocated_width()
        self._position_pending = width <= 1
        if not self._position_pending:
            self.set_position(max(200, width - self.panel_width - self._divider_width))

    def _page_removed(self, notebook, panel, index):
        if self._destroying:
            return
        if notebook in self.docked_notebooks:
            if notebook.get_n_pages() == 0:
                notebook.hide()
            self._rebuild_dock()
            return
        if notebook.get_n_pages() == 0:
            notebook.get_toplevel().hide()

    def _hide_notebook(self, notebook):
        for panel in list(notebook.get_children()):
            self.hide_panel(panel.tool_key)

    def _close_float(self, window, event, notebook):
        self._hide_notebook(notebook)
        return True

    def save_state(self):
        groups = []
        notebooks = self.docked_notebooks + [
            window.get_child() for window in self.floating
        ]
        for notebook in notebooks:
            tabs = [panel.tool_key for panel in notebook.get_children()]
            tabs += [
                key
                for key, panel in self.panels.items()
                if panel.tool_home is notebook and key not in tabs
            ]
            if not tabs:
                continue
            selected = notebook.get_nth_page(notebook.get_current_page())
            group = {
                "tabs": tabs,
                "selected": selected.tool_key if selected is not None else None,
                "floating": notebook not in self.docked_notebooks,
            }
            if group["floating"]:
                window = notebook.get_toplevel()
                x, y = window.get_position()
                width, height = window.get_size()
                group["geometry"] = dict(x=x, y=y, width=width, height=height)
            groups.append(group)
        positions = []
        children = self.sidebar.get_children()
        split = children[0] if children else None
        while isinstance(split, Gtk.Paned):
            positions.append(split.get_position())
            split = split.get_child2()
        width = (
            self.sidebar.get_allocated_width()
            if self.sidebar.get_visible()
            else self.panel_width
        )
        return {
            "groups": groups,
            "visible": [key for key in self.panels if self.is_open(key)],
            "width": width,
            "splits": positions,
        }

    def restore_state(self, value, workareas=()):
        state = mapping(value)
        if not isinstance(state.get("groups"), list):
            return
        for key in self.panels:
            self.hide_panel(key)
        old_groups, old_windows = list(self.docked_notebooks), list(self.floating)
        self.floating = []
        self.docked_notebooks = [self.notebook]
        visible = state.get("visible", [])
        visible = visible if isinstance(visible, list) else []
        used, selections = self._restore_groups(state["groups"], visible, workareas)
        for key, panel in self.panels.items():
            if key not in used:
                panel.tool_home = self.notebook
                panel.tool_floating = False
        self._rebuild_dock()
        self._restore_selections(selections)
        for notebook in old_groups:
            if notebook not in self.docked_notebooks:
                notebook.destroy()
        for window in old_windows:
            window.destroy()
        self.panel_width = int(number(state.get("width"), self.panel_width, 220, 1200))
        self.sidebar.set_size_request(220, -1)
        self._set_sidebar_width()
        self._restore_positions(state.get("splits", []))

    def _restore_selections(self, selections):
        for notebook, selected in selections:
            if isinstance(selected, str) and selected in self.panels:
                panel = self.panels[selected]
                if panel.get_parent() is notebook:
                    notebook.set_current_page(notebook.page_num(panel))

    def _restore_positions(self, positions):
        if not isinstance(positions, list):
            return
        children = self.sidebar.get_children()
        split = children[0] if children else None
        for saved in positions:
            if not isinstance(split, Gtk.Paned):
                break
            position = int(number(saved, 300, 0, 20000))
            self._restore_split(split, position)
            split = split.get_child2()

    def _restore_groups(self, saved_groups, visible, workareas):
        used = set()
        selections: List[Tuple[Any, Any]] = []
        has_docked = False
        for saved in saved_groups[:64]:
            group = mapping(saved)
            tabs = group.get("tabs")
            if not isinstance(tabs, list):
                continue
            keys = []
            for key in tabs:
                if isinstance(key, str) and key in self.panels and key not in used:
                    keys.append(key)
                    used.add(key)
            if not keys:
                continue
            notebook = self._restore_group_target(group, has_docked, workareas)
            floating = notebook not in self.docked_notebooks
            has_docked = has_docked or not floating
            for key in keys:
                self.panels[key].tool_home = notebook
                self.panels[key].tool_floating = floating
                if key in visible:
                    self.show_panel(key)
            selections.append((notebook, group.get("selected")))
            if floating and notebook.get_n_pages() == 0:
                notebook.get_toplevel().hide()
        return used, selections

    def _restore_group_target(self, group, has_docked, workareas):
        if group.get("floating") is True:
            notebook = self._floating_notebook(reuse=False)
            geometry = window_geometry(group.get("geometry"), workareas)
            window = notebook.get_toplevel()
            window.move(geometry["x"], geometry["y"])
            window.resize(geometry["width"], geometry["height"])
        elif not has_docked:
            notebook = self.notebook
        else:
            notebook = self._notebook()
            notebook.set_no_show_all(True)
            self.docked_notebooks.append(notebook)
        return notebook

    @staticmethod
    def _restore_split(split, position):
        split.set_position(position)

        def allocated(widget, allocation):
            if allocation.height > 1:
                widget.disconnect(handler)
                widget.set_position(position)

        handler = split.connect("size-allocate", allocated)
        split.queue_resize()

    def _destroy(self, *args):
        self._destroying = True
        if self._drag is not None:
            self._release_grab(self._drag)
            self._drag = None
        for window in list(self.floating):
            window.destroy()
        self.floating.clear()
        for panel in self.panels.values():
            if panel.get_parent() is None:
                panel.destroy()
        for notebook in self.docked_notebooks:
            if notebook.get_parent() is None:
                notebook.destroy()
