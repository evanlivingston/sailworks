"""Qt lifecycle adapter for the production marine renderer."""

from dataclasses import replace

from gweatherrouting.core.marine.manager import Selection
from gweatherrouting.core.marine.models import Capability
from gweatherrouting.rendering.marinemaplayer import MarineMapLayer, chart_instant


class MarineOverlay(MarineMapLayer):
    def __init__(self, manager, clock, schedule):
        super().__init__(manager, clock, schedule)
        self.closed = False

    def do_draw(self, canvas, cr):
        if self.closed or not self.visible or not canvas.isVisible():
            return
        if not (
            self.sources.fields
            or self.sources.levels.datasets
            or self.sources.currents.datasets
        ):
            self.forget(canvas)
            return
        self._maps.add(canvas)
        sources = self.sources
        presenter = getattr(canvas, "_weather_presenter", None)
        if presenter is not None and presenter.marine_active:
            # Quick owns both kinds of current arrow. Retain only tide labels
            # in the compatibility image; never duplicate arrows in that image.
            sources = replace(
                sources, fields=(), currents=Selection((), Capability.STATION_CURRENT)
            )
        self._renderer.draw(
            canvas, cr, ((sources,),), chart_instant(self.time_control.time)
        )

    def forget(self, canvas):
        self._renderer.forget(canvas)
        self._maps.discard(canvas)

    def close(self):
        if self.closed:
            return
        self.closed = True
        for canvas in tuple(self._maps):
            self.forget(canvas)
        self.manager.disconnect("changed", self._changed)
        self.manager.fields.disconnect("changed", self._changed)
        self.time_control.disconnect("time_change", self._time_changed)
