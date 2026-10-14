"""Offline currents and station tides at the chart's selected instant."""

import json
import math
from dataclasses import dataclass
from datetime import timezone

import cairo

from gweatherrouting.core.marine.fields import FieldSelection
from gweatherrouting.core.marine.manager import Selection
from gweatherrouting.core.marine.models import Capability
from gweatherrouting.core.mapviewport import MapPoint
from gweatherrouting.rendering.overlayrenderer import OverlayRenderer
from .marinelandmask import MarineLandMask
from gweatherrouting.core.scheduling import schedule_idle


def chart_instant(value):
    """The existing chart clock is local wall time; marine data uses aware UTC."""
    return value.astimezone(timezone.utc)


def flow(east, north):
    """Speed in knots and bearing TOWARD true north, clockwise."""
    return (
        math.hypot(east, north) * 3600 / 1852,
        math.degrees(math.atan2(east, north)) % 360,
    )


def current_style(speed):
    """Fixed knots scale: green, yellow (1), orange (2), red (3+)."""
    stops = (
        (0, (0.12, 0.70, 0.25)),
        (1, (0.98, 0.85, 0.08)),
        (2, (1.0, 0.45, 0.08)),
        (3, (0.94, 0.12, 0.10)),
    )
    color = stops[-1][1]
    for (low, a), (high, b) in zip(stops, stops[1:]):
        if speed <= high:
            weight = max(0, (speed - low) / (high - low))
            color = tuple(x + (y - x) * weight for x, y in zip(a, b))
            break
    length = 10 + min(max(speed, 0), 5) * 5
    return length, color


def arrow(cr, x, y, east, north, *, station=False):
    speed, bearing = flow(east, north)
    length, color = current_style(speed)
    cr.save()
    cr.translate(x, y)
    if station:
        cr.set_source_rgb(1.0, 0.70, 0.18)
        cr.set_line_width(1.5)
        cr.arc(0, 0, 8, 0, 2 * math.pi)
        cr.stroke()
    cr.set_source_rgb(*color)
    if speed < 0.05:
        cr.arc(0, 0, 2.5, 0, 2 * math.pi)
        cr.fill()
    else:
        cr.rotate(math.radians(bearing))
        head = length * 0.25
        shaft = length * 0.11
        # A filled, broad-headed arrow, pointing toward the water's motion.
        cr.move_to(0, -length / 2)
        cr.line_to(head, -length * 0.05)
        cr.line_to(shaft, -length * 0.05)
        cr.line_to(shaft, length / 2)
        cr.line_to(-shaft, length / 2)
        cr.line_to(-shaft, -length * 0.05)
        cr.line_to(-head, -length * 0.05)
        cr.line_to(0, -length / 2)
        cr.close_path()
        cr.fill_preserve()
        cr.set_source_rgba(0, 0, 0, 0.3)
        cr.set_line_width(0.7)
        cr.stroke()
    cr.restore()


def label(cr, x, y, value):
    cr.save()
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    cr.set_font_size(11)
    cr.move_to(x, y)
    cr.text_path(value)
    cr.set_source_rgba(0, 0, 0, 0.9)
    cr.set_line_width(3)
    cr.stroke_preserve()
    cr.set_source_rgb(1, 1, 1)
    cr.fill()
    cr.restore()


@dataclass(frozen=True, eq=False)
class MarineSources:
    fields: tuple
    levels: Selection
    currents: Selection


class MarineRenderer(OverlayRenderer):
    """Sample a bounded screen grid on a worker, preserving native model masks."""

    def __init__(self, schedule=schedule_idle):
        super().__init__(schedule)
        self._land_mask = MarineLandMask()

    def _can_preview(self, job, key):
        if job.key == key:
            return True
        if job.key[1:] != key[1:]:
            return False
        # Keep a completed frame during time-only updates, but clear it promptly
        # when the requested time leaves all enabled coverage.
        sources = job.charts[0][0]
        instant = key[0]
        return any(
            f.times[0] <= instant.timestamp() <= f.times[-1] for f in sources.fields
        ) or any(
            d.requested_start <= instant <= d.requested_end
            for selection in (sources.levels, sources.currents)
            for d in selection.datasets
        )

    def _render(self, job):
        sources = job.charts[0][0]
        instant = job.palette
        view = job.viewport
        surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32, view.width * job.factor, view.height * job.factor
        )
        surface.set_device_scale(job.factor, job.factor)
        cr = cairo.Context(surface)
        land = self._land_mask.surface(view)
        selection = FieldSelection(sources.fields)
        # Screen spacing keeps work and clutter bounded at every map scale.
        for y in range(28, view.height, 64):
            if job.cancel.is_set():
                return None
            for x in range(28, view.width, 64):
                if self._land_mask.contains(land, x, y):
                    continue
                lat, lon = view.convert_screen_to_geographic(x, y).get_degrees()
                result = selection.sample(lat, lon, instant)
                if result.status != "available":
                    continue
                arrow(cr, x, y, *result.values)
        # Clip the entire glyph, including tips that cross a coastal boundary.
        self._land_mask.clear(cr, land)
        self._stations(cr, sources, view, instant, job.cancel)
        surface.flush()
        return None if job.cancel.is_set() else surface

    @staticmethod
    def _stations(cr, sources, view, instant, cancel):
        for station_selection, is_level in (
            (sources.levels, True),
            (sources.currents, False),
        ):
            seen = set()
            for dataset in station_selection.datasets:
                if cancel.is_set():
                    return
                station = dataset.station
                if station.id in seen:
                    continue
                seen.add(station.id)
                # Wrap to the viewport's world copy, including date-line views.
                lon = (
                    view._center_lon
                    + (station.longitude - view._center_lon + 180) % 360
                    - 180
                )
                x, y = view.convert_geographic_to_screen(
                    MapPoint(station.latitude, lon)
                )
                if not (0 <= x <= view.width and 0 <= y <= view.height):
                    continue
                result = station_selection.sample(station.id, instant)
                if result.status != "available":
                    continue
                if is_level:
                    cr.set_source_rgb(1.0, 0.70, 0.18)
                    cr.arc(x, y, 4, 0, math.pi * 2)
                    cr.fill()
                    label(
                        cr,
                        x + 8,
                        y - 8,
                        "%s %.2f m (source datum)" % (station.name, result.values[0]),
                    )
                else:
                    arrow(cr, x, y, *result.values, station=True)
                    speed, _ = flow(*result.values)
                    label(
                        cr,
                        x + 10,
                        y + 30,
                        "%s %.1f kn (station)" % (station.name, speed),
                    )


class MarineMapLayer:
    def __init__(self, manager, time_control, schedule=schedule_idle):
        self.manager = manager
        self.time_control = time_control
        self.visible = True
        self.currents_visible = True
        self.tides_visible = True
        self._maps = set()
        self._renderer = MarineRenderer(schedule)
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
        )

    def _changed(self, *args):
        self._refresh_sources()
        self._time_changed()

    def _time_changed(self, *args):
        for canvas in self._maps:
            canvas.queue_draw()

    def set_visible(self, visible):
        self.visible = visible
        if not visible:
            for canvas in self._maps:
                self._renderer.forget(canvas)
        self._time_changed()

    def set_display(self, *, currents=None, tides=None):
        if currents is not None:
            self.currents_visible = bool(currents)
        if tides is not None:
            self.tides_visible = bool(tides)
        self._changed()

    def _forget(self, canvas):
        self._renderer.forget(canvas)
        self._maps.discard(canvas)
        if not self._maps:
            self.manager.disconnect("changed", self._changed)
            self.manager.fields.disconnect("changed", self._changed)
            self.time_control.disconnect("time_change", self._time_changed)

    def do_draw(self, gpsmap, cr):
        if not self.visible:
            return
        if gpsmap not in self._maps:
            self._maps.add(gpsmap)
            gpsmap.connect("destroy", self._forget)
            gpsmap.connect("unmap", lambda canvas: self._renderer.forget(canvas))
        instant = chart_instant(self.time_control.time)
        self._renderer.draw(gpsmap, cr, ((self.sources,),), instant)

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

    def do_button_press(self, gpsmap, event):
        return False
