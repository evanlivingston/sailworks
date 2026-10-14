"""A shared chart timeline for enabled wind, current and station forecasts."""

from datetime import datetime

from gweatherrouting.core.forecasttimeline import forecast_intervals, forecast_bounds

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk


class TimeScrubber(Gtk.Box):
    def __init__(
        self,
        time_control,
        on_scrub,
        grib_manager=None,
        marine_manager=None,
        on_interact=None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin=6)
        self.clock = time_control
        self.on_scrub = on_scrub
        self.on_interact = on_interact
        self.gribs = grib_manager
        self.marine = marine_manager
        self.bounds = None
        self._syncing = False
        self._pending = None
        self._commit_timer = 0
        self._signature = None
        self.caption = Gtk.Label(xalign=0)
        self.caption.set_no_show_all(True)
        self.pack_start(self.caption, False, False, 0)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.scale.set_draw_value(False)
        self.scale.set_hexpand(True)
        self.scale.set_tooltip_text("Scrub forecast time; dragging pauses playback")
        self.scale.connect("value-changed", self._slide)
        self.scale.connect("button-release-event", self._release)
        self.pack_start(self.scale, False, False, 0)
        endpoints = Gtk.Box()
        self.start_label = Gtk.Label(xalign=0)
        self.end_label = Gtk.Label(xalign=1)
        endpoints.pack_start(self.start_label, True, True, 0)
        endpoints.pack_start(self.end_label, True, True, 0)
        self.pack_start(endpoints, False, False, 0)
        self.clock.connect("time_change", self._time_changed)
        if self.marine:
            self.marine.connect("changed", self.refresh)
            self.marine.fields.connect("changed", self.refresh)
        # GRIB manager has no change signal. Inspect only its small enabled list;
        # marine changes refresh immediately, without re-reading forecast arrays.
        self._poll_timer = GLib.timeout_add(500, self.refresh)
        self.connect("destroy", self._destroy)
        self.refresh()

    def _intervals(self):
        return forecast_intervals(self.gribs, self.marine)

    def refresh(self, *args):
        intervals = self._intervals()
        if intervals == self._signature:
            return True
        self._signature = intervals
        self.bounds = forecast_bounds(intervals)
        self._cancel_commit()
        self._syncing = True
        if self.bounds:
            start, end = self.bounds
            self.scale.set_range(start, max(start + 1, end))
            self.scale.set_increments(60, 3600)
            self.scale.set_sensitive(end > start)
            for widget, stamp in ((self.start_label, start), (self.end_label, end)):
                instant = datetime.fromtimestamp(stamp).astimezone()
                widget.set_text(instant.strftime("%d %b %H:%M %Z"))
                widget.set_tooltip_text(instant.isoformat())
        else:
            self.scale.set_sensitive(False)
            self.start_label.set_text("")
            self.end_label.set_text("")
        self._syncing = False
        self._time_changed(self.clock.time)
        return True

    def _time_changed(self, instant):
        self._cancel_commit()
        self._syncing = True
        if self.bounds:
            self.scale.set_value(instant.timestamp())
            outside = not self.bounds[0] <= instant.timestamp() <= self.bounds[1]
            self.caption.set_text(
                "Selected time is outside the loaded forecast range" if outside else ""
            )
        else:
            self.caption.set_text(
                "Load wind or download tides / currents to enable scrubbing"
            )
        self.caption.set_visible(bool(self.caption.get_text()))
        self._syncing = False

    def _slide(self, scale):
        if self._syncing or not self.bounds or not scale.get_sensitive():
            return
        if self.on_interact:
            self.on_interact()
        self._pending = scale.get_value()
        if not self._commit_timer:
            # Coalesce pointer motion so background layers can finish frames.
            self._commit_timer = GLib.timeout_add(75, self._commit)

    def _commit(self):
        stamp = self._pending
        self._pending = None
        self._commit_timer = 0
        if stamp is not None:
            self.on_scrub(datetime.fromtimestamp(stamp))
        return False

    def _release(self, *args):
        if self._commit_timer:
            GLib.source_remove(self._commit_timer)
            self._commit_timer = 0
            self._commit()
        return False

    def _cancel_commit(self):
        if self._commit_timer:
            GLib.source_remove(self._commit_timer)
            self._commit_timer = 0
        self._pending = None

    def _destroy(self, *args):
        self._cancel_commit()
        GLib.source_remove(self._poll_timer)
        self.clock.disconnect("time_change", self._time_changed)
        if self.marine:
            self.marine.disconnect("changed", self.refresh)
            self.marine.fields.disconnect("changed", self.refresh)
