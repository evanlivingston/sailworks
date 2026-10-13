# -*- coding: utf-8 -*-
# Copyright (C) 2017-2026 Davide Gessa
"""
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

For detail about GNU see <http://www.gnu.org/licenses/>.
"""

import os
from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from gweatherrouting.core import TimeControl
from gweatherrouting.gtk.timepickerdialog import TimePickerDialog
from .timescrubber import TimeScrubber

TIME_UNITS = {
    "30s": 30,
    "1m": 1 * 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "3h": 180 * 60,
    "6h": 360 * 60,
    "12h": 720 * 60,
    "24h": 1440 * 60,
    "1d": 1440 * 60,
}


class TimeTravelWidget(Gtk.Box):
    def __init__(
        self,
        parent: Gtk.Window,
        time_control: TimeControl,
        map,
        smaller_unit=False,
        grib_manager=None,
        marine_manager=None,
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        self.play = False
        self._play_timer = 0
        self.parent = parent
        self.time_control = time_control
        self.map = map

        self.builder = Gtk.Builder()
        self.builder.add_from_file(
            os.path.abspath(os.path.dirname(__file__)) + "/timetravel.glade"
        )
        self.builder.connect_signals(self)

        self.pack_start(self.builder.get_object("timetravel"), True, True, 0)

        self.scrubber = None
        if grib_manager is not None or marine_manager is not None:
            self.scrubber = TimeScrubber(
                self.time_control,
                self._scrub_to,
                grib_manager,
                marine_manager,
                on_interact=lambda: self.on_stop_click(None),
            )
            self.pack_start(self.scrubber, False, False, 0)
        self.timeLabel = self.builder.get_object("time-label")
        self.timeUnitCombo = self.builder.get_object("time-unit-combo")
        self.time_control.connect("time_change", self.on_time_change)
        self.on_time_change(self.time_control.time)

        self.seconds = 300 if smaller_unit else 900
        if smaller_unit:
            self.timeUnitCombo.set_active(2)
        else:
            self.timeUnitCombo.set_active(3)
        self.connect("destroy", self._destroy)
        self.show_all()

    def get_change_unit(self):
        return self.seconds

    def on_time_unit_combo_change(self, widget):
        u = self.timeUnitCombo.get_active_text()
        self.seconds = TIME_UNITS[u]

    def on_time_change(self, t):
        date_str = t.astimezone().strftime("%a %d %b %Z")
        time_str = t.strftime("%H:%M")
        self.timeLabel.set_markup(
            f'<span font_weight="bold" font_size="large">{time_str}</span>'
            f'  <span alpha="60%">{date_str}</span>'
        )
        self.map.queue_draw()

    def on_time_now(self, event):
        self.on_stop_click(None)
        self.time_control.now()

    def on_play_click(self, event):
        if self.play:
            return
        self.play = True
        self._play_timer = GLib.timeout_add(1000, self.on_play_step)

    def on_play_step(self):
        if not self.play:
            return False
        self.on_foward_click(None)
        if self.scrubber and self.scrubber.bounds:
            if self.time_control.time.timestamp() >= self.scrubber.bounds[1]:
                self.on_stop_click(None)
        return self.play

    def on_stop_click(self, event):
        self.play = False
        if self._play_timer:
            GLib.source_remove(self._play_timer)
            self._play_timer = 0

    def _scrub_to(self, instant):
        self.on_stop_click(None)
        self.time_control.set_time(instant)

    def _step(self, seconds):
        if self.scrubber and self.scrubber.bounds:
            start, end = self.scrubber.bounds
            stamp = max(start, min(end, self.time_control.time.timestamp() + seconds))
            self.time_control.set_time(datetime.fromtimestamp(stamp))
        else:
            self.time_control.increase(seconds=seconds)

    def on_foward_click(self, event):
        self._step(self.seconds)
        self.map.queue_draw()

    def on_backward_click(self, event):
        self._step(-self.seconds)

    def on_time_select(self, event):
        self.on_stop_click(None)
        tp = TimePickerDialog(self.parent)
        tp.set_from_date_time(self.time_control.time)
        response = tp.run()

        if response == Gtk.ResponseType.OK:
            self.time_control.set_time(tp.get_date_time())

        tp.destroy()

    def _destroy(self, *args):
        self.on_stop_click(None)
        self.time_control.disconnect("time_change", self.on_time_change)
