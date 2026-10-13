"""Shared content spacing and settings controls for dockable tool panels."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GdkPixbuf, Gtk

from gweatherrouting.common import resource_path


def tool_content():
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=10)


def settings_row(content, control, callback):
    row = Gtk.Box(spacing=6)
    row.pack_start(control, True, True, 0)
    icon = GdkPixbuf.Pixbuf.new_from_file_at_scale(
        resource_path("gweatherrouting", "data/icons/gear-symbolic.svg"), 20, 20, True
    )
    button = Gtk.Button(image=Gtk.Image.new_from_pixbuf(icon))
    button.set_relief(Gtk.ReliefStyle.NONE)
    button.set_tooltip_text("Settings")
    button.get_accessible().set_name("Settings")
    button.connect("clicked", callback)
    row.pack_end(button, False, False, 0)
    content.pack_start(row, False, False, 0)
    return button
