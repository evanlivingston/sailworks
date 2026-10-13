"""UTC download start that follows chart time until explicitly edited."""

from datetime import datetime, timezone

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GdkPixbuf, Gtk

from gweatherrouting.common import resource_path


class DownloadStartEntry(Gtk.Entry):
    def __init__(self, clock=None):
        super().__init__(width_chars=16)
        self.clock = clock
        self._follows_chart = True
        self._edited_handler = self.connect("changed", self._edited)
        icon = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            resource_path("gweatherrouting", "data/icons/go-bottom-symbolic.svg"),
            16,
            16,
            True,
        )
        self.set_icon_from_pixbuf(Gtk.EntryIconPosition.SECONDARY, icon)
        self.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, "Select now")
        self.set_icon_activatable(Gtk.EntryIconPosition.SECONDARY, True)
        self.connect("icon-release", self._select_now)
        self.connect("destroy", self._cleanup)
        if clock:
            clock.connect("time_change", self._chart_changed)
        self._chart_changed()

    def _edited(self, *args):
        self._follows_chart = False

    def _chart_changed(self, *args):
        if not self._follows_chart:
            return
        instant = self.clock.time if self.clock else datetime.now(timezone.utc)
        self.handler_block(self._edited_handler)
        try:
            self.set_text(instant.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M"))
        finally:
            self.handler_unblock(self._edited_handler)

    def _select_now(self, entry, position, event):
        if position == Gtk.EntryIconPosition.SECONDARY:
            self._follows_chart = False
            self.set_text(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))

    def _cleanup(self, *args):
        if self.clock:
            self.clock.disconnect("time_change", self._chart_changed)
