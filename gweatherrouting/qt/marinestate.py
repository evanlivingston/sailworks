"""Marine selections and display state shared by Qt marine presentations."""

import json

from gweatherrouting.core.marine.fields import FieldSelection
from gweatherrouting.core.marine.manager import Selection
from gweatherrouting.core.marine.models import Capability
from gweatherrouting.rendering.marinepresentation import (
    MarineSources,
    chart_instant,
    flow,
)


class MarineDisplayState:
    """Own source selections and user-facing marine display options."""

    def __init__(self, manager, time_control):
        self.manager = manager
        self.time_control = time_control
        self.visible = True
        self.currents_visible = True
        self.tides_visible = True
        self.current_density = "standard"
        self._hosts = set()
        self._closed = False
        self._refresh_sources()
        manager.connect("changed", self._changed)
        manager.fields.connect("changed", self._changed)
        time_control.connect("time_change", self._time_changed)

    def _refresh_sources(self):
        self.sources = MarineSources(
            self.manager.fields.snapshot().fields if self.currents_visible else (),
            (
                self.manager.snapshot(Capability.WATER_LEVEL)
                if self.tides_visible
                else Selection((), Capability.WATER_LEVEL)
            ),
            (
                self.manager.snapshot(Capability.STATION_CURRENT)
                if self.currents_visible
                else Selection((), Capability.STATION_CURRENT)
            ),
            self.current_density,
        )

    def watch(self, host):
        self._hosts.add(host)

    def forget(self, host):
        self._hosts.discard(host)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._hosts.clear()
        self.manager.disconnect("changed", self._changed)
        self.manager.fields.disconnect("changed", self._changed)
        self.time_control.disconnect("time_change", self._time_changed)

    def set_current_density(self, density):
        from gweatherrouting.rendering.marinepresentation import CURRENT_DENSITIES

        if density not in {key for key, _label, _spacing in CURRENT_DENSITIES}:
            density = "standard"
        if density != self.current_density:
            self.current_density = density
            self._changed()

    def set_display(self, *, currents=None, tides=None):
        if currents is not None:
            self.currents_visible = bool(currents)
        if tides is not None:
            self.tides_visible = bool(tides)
        self._changed()

    def set_visible(self, visible):
        self.visible = bool(visible)
        self._time_changed()

    def _changed(self, *args):
        if self._closed:
            return
        self._refresh_sources()
        self._time_changed()

    def _time_changed(self, *args):
        if self._closed:
            return
        for host in tuple(self._hosts):
            host.queue_draw()

    def cursor_text(self, latitude, longitude):
        if not self.visible or not self.currents_visible:
            return ""
        result = FieldSelection(self.sources.fields).sample(
            latitude, longitude, chart_instant(self.time_control.time)
        )
        if result.status != "available":
            return "Current: no valid data"
        speed, bearing = flow(*result.values)
        source = next(f for f in self.sources.fields if f.id == result.dataset_id)
        return "Current %.1f kn toward %.0f°T (%s)" % (
            speed,
            bearing,
            json.loads(source.metadata_json)["model"],
        )
