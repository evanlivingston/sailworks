"""Shared symbolic icons and text labels for UI navigation."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

TIDES_CURRENTS_ICON = "tides-currents-symbolic"


def icon_label(title, icon_name):
    """Keep navigation titles visible alongside a theme-colored icon."""
    box = Gtk.Box(spacing=6)
    box.pack_start(
        Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU), False, False, 0
    )
    box.pack_start(Gtk.Label(label=title), False, False, 0)
    box.show_all()
    return box
