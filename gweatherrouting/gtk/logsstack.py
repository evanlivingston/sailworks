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

import io
import logging
import os
from datetime import timedelta
from typing import Any, List, Optional

import cairo
import gi
import nmeatoolkit as nt
import numpy
import PIL

gi.require_version("Gtk", "3.0")

from gi.repository import GLib, Gtk

from gweatherrouting.core import Core
from gweatherrouting.core.timecontrol import TimeControl, naive_wall_time
from gweatherrouting.gtk.charts import ChartManager
from gweatherrouting.gtk.settings import SettingsManager

from .maplayers.geomaplayer import GeoMapLayer
from .maplayers.toolsmaplayer import ToolsMapLayer
from .widgets.mapwidget import MapWidget
from .widgets.tooldock import ToolDock
from .nmeacontrols import NMEAControls, get_nmea_session

logger = logging.getLogger("gweatherrouting")


class LogsStack(Gtk.Box):
    def __init__(
        self,
        parent,
        chart_manager: ChartManager,
        core: Core,
        settings_manager: SettingsManager,
        tool_dock=None,
    ):
        Gtk.Widget.__init__(self)

        self.parent = parent
        self.core = core
        self.data: List[nt.TrackPoint] = []
        self.session = get_nmea_session(core)
        self._stream_generation = 0
        self._alive = True
        self._redraw_timer = None
        self._plot_cache: Optional[tuple] = None
        self._plot_log = None
        self._translator = nt.TrackPointTranslator()
        self._wind_pipe = nt.TrueWindPipe()

        self.depth_chart = False
        self.speed_chart = True
        self.apparent_wind_chart = False
        self.true_wind_chart = True
        self.hdg_chart = True
        self.rw_chart = False

        self.session.connect("data", self.data_handler)
        self.session.connect("reset", self._reset_stream)
        self.session.connect("loaded", self._log_loaded)
        self.session.connect("changed", self._session_changed)
        self.connect("destroy", self._destroy)

        self.builder = Gtk.Builder()
        self.builder.add_from_file(
            os.path.abspath(os.path.dirname(__file__)) + "/logsstack.glade"
        )
        self.builder.connect_signals(self)

        content = self.builder.get_object("logscontent")
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for name in ("map-container", "grapharea"):
            child = self.builder.get_object(name)
            content.remove(child)
            body.pack_start(child, True, True, 0)
        self.tool_dock = tool_dock if tool_dock is not None else ToolDock(body, parent)
        mount = (
            tool_dock.add_view("Logs", body)
            if tool_dock is not None
            else self.tool_dock
        )
        content.pack_start(mount, True, True, 0)
        content.reorder_child(mount, 1)
        self.nmea = NMEAControls.attach(parent, core, self.tool_dock)
        self.tool_dock.connect("panel-visibility-changed", self._panel_visibility)
        self.pack_start(content, True, True, 0)
        self.graph_area = self.builder.get_object("grapharea")

        self.status_bar = self.builder.get_object("statusbar")

        self.show_all()

        self.map = MapWidget()
        map_container = self.builder.get_object("map-container")
        map_container.pack_start(self.map, True, True, 0)
        self.map.set_center_and_zoom(39.0, 9.0, 6)
        self.map.layer_add(chart_manager)
        chart_manager.add_map(self.map)

        self.tools_map_layer = ToolsMapLayer(self.core)
        self.map.layer_add(self.tools_map_layer)

        self.time_control = TimeControl()
        self.geo_map_layer = GeoMapLayer(self.core, self.time_control)
        self.map.layer_add(self.geo_map_layer)

        body.show_all()
        self.show_all()
        if self.session.source == "log":
            self._reset_stream(None)
            self._session_changed(None)

    def on_toggle_nmea(self, widget):
        if widget.get_active():
            self.tool_dock.show_panel("nmea")
        else:
            self.tool_dock.hide_panel("nmea")

    def _panel_visibility(self, dock, key, visible):
        if key == "nmea":
            self.builder.get_object("nmea-toggle").set_active(visible)

    def _reset_stream(self, _):
        self._stream_generation += 1
        self._translator = nt.TrackPointTranslator()
        self._wind_pipe = nt.TrueWindPipe()
        log = self.session.log if self.session.source == "log" else None
        if log is None or log is not self._plot_log:
            self.data = list(log.points) if log is not None else []
            self._plot_cache = None
        self._plot_log = log
        self.tools_map_layer.gps_clear()
        self.rebuild_track()
        self.graph_area.queue_draw()

    def _log_loaded(self, log):
        self.rebuild_track()
        if self.data:
            point = self.data[0]
            self.time_control.set_time(point.time)
            self.map.set_center_and_zoom(point.lat, point.lon, 12)
        self.graph_area.queue_draw()

    def _playback_time(self):
        log = self.session.log
        if self.session.source != "log" or log is None or not log.points:
            return None
        offset = log.point_offsets[0] if log.point_offsets else 0
        return naive_wall_time(log.points[0].time) + timedelta(
            seconds=self.session.position - offset
        )

    def _session_changed(self, _):
        cursor_time = self._playback_time()
        if cursor_time is not None:
            self.time_control.set_time(cursor_time)
            self.graph_area.queue_draw()
            self.map.queue_draw()

    def _destroy(self, _):
        self._alive = False
        if self._redraw_timer is not None:
            GLib.source_remove(self._redraw_timer)
        self.session.disconnect("data", self.data_handler)
        self.session.disconnect("reset", self._reset_stream)
        self.session.disconnect("loaded", self._log_loaded)
        self.session.disconnect("changed", self._session_changed)

    def toggle_speed_chart(self, widget):
        self.speed_chart = not self.speed_chart
        self.graph_area.queue_draw()

    def toggle_apparent_wind_chart(self, widget):
        self.apparent_wind_chart = not self.apparent_wind_chart
        self.graph_area.queue_draw()

    def toggle_true_wind_chart(self, widget):
        self.true_wind_chart = not self.true_wind_chart
        self.graph_area.queue_draw()

    def toggle_depth_chart(self, widget):
        self.depth_chart = not self.depth_chart
        self.graph_area.queue_draw()

    def toggle_hdg_chart(self, widget):
        self.hdg_chart = not self.hdg_chart
        self.graph_area.queue_draw()

    def toggle_rw_chart(self, widget):
        self.rw_chart = not self.rw_chart
        self.graph_area.queue_draw()

    def data_handler(self, packets):
        if self.session.source == "log":
            return
        GLib.idle_add(self._consume, tuple(packets), self._stream_generation)

    def _consume(self, packets, generation):
        if not self._alive or generation != self._stream_generation:
            return False
        for packet in packets:
            self._consume_sentence(packet.data)
        if self.data:
            self.time_control.set_time(self.data[-1].time)
            self.status_bar.push(0, f"{len(self.data):,} track points")
        if self._redraw_timer is None:
            self._redraw_timer = GLib.timeout_add(500, self._redraw)
        return False

    def _consume_sentence(self, sentence):
        if sentence is None or getattr(sentence, "sentence_type", "") in ("ALK", "VDM"):
            return
        try:
            for interpreted in self._wind_pipe.transform(sentence):
                point = self._translator.feed(interpreted)
                if point is not None:
                    self.data.append(point)
                    # Translator keeps a second history; this view owns it.
                    self._translator.track.clear()
                    if len(self.data) == 1 or len(self.data) % 150 == 0:
                        self.core.logManager.log_history.add(point.lat, point.lon)
        except (ValueError, TypeError, AttributeError):
            logger.debug("Skipping malformed log measurement", exc_info=True)

    def _redraw(self):
        self._redraw_timer = None
        if self._alive:
            self.graph_area.queue_draw()
            self.map.queue_draw()
        return False

    def load_from_file(self, filepath):
        self.nmea.load_path(filepath)

    def rebuild_track(self):
        self.core.logManager.log_history.clear()
        for x in self.data[0::150]:
            self.core.logManager.log_history.add(x.lat, x.lon)
        self.map.queue_draw()

    def on_graph_draw(self, widget, ctx):
        if not self.data:
            return
        allocation = widget.get_allocation()
        key = (
            id(self.data),
            len(self.data),
            allocation.width,
            allocation.height,
            self.speed_chart,
            self.depth_chart,
            self.hdg_chart,
            self.apparent_wind_chart,
            self.true_wind_chart,
            self.rw_chart,
        )
        if self._plot_cache is None or self._plot_cache[0] != key:
            self._plot_cache = (key, *self._render_graph(widget))
        _, surface, _pixels, axes = self._plot_cache
        ctx.save()
        ctx.set_source_surface(surface, 0, 0)
        ctx.paint()
        ctx.restore()
        cursor_time = self._playback_time()
        if cursor_time is not None:
            from matplotlib.dates import date2num

            position = date2num(cursor_time)
            for left, top, width, height, start, end in axes:
                if not start <= position <= end:
                    continue
                cursor_x = left + (position - start) / (end - start) * width
                ctx.save()
                ctx.rectangle(left, top, width, height)
                ctx.clip()
                ctx.set_source_rgb(1, 0.75, 0.25)
                ctx.set_line_width(1.5)
                ctx.move_to(cursor_x, top)
                ctx.line_to(cursor_x, top + height)
                ctx.stroke()
                ctx.restore()

    def _render_graph(self, widget):  # noqa: C901
        s = max(1, len(self.data) // 2000)
        a = widget.get_allocation()
        width = a.width

        if len(self.data) == 0:
            return

        import matplotlib.pyplot as plt

        plt.style.use("dark_background")
        plt.rcParams.update({"font.size": 8})
        y = self.data[::s]
        if y[-1] is not self.data[-1]:
            y.append(self.data[-1])
        x: Any = numpy.array([naive_wall_time(point.time) for point in y])

        nplots = 1

        if self.hdg_chart or self.apparent_wind_chart or self.true_wind_chart:
            nplots += 1

        if self.speed_chart:
            nplots += 1

        if self.depth_chart:
            nplots += 1

        fig, ax1 = plt.subplots(nplots, dpi=100)
        try:
            ax1[0]
        except:
            ax1 = [ax1]
        fig.set_size_inches((width / 100), (a.height / 100.0))

        i = 0
        if self.speed_chart:
            if i < nplots - 1:
                plt.setp(ax1[i].get_xticklabels(), visible=False)

            data = list(map(lambda x: x.speed if x.speed else 0, y))
            ax1[i].plot(x, data, color="#8dd3c7", linewidth=0.6, label="Speed")

            ax1[i].legend()
            i += 1

        if self.apparent_wind_chart or self.true_wind_chart:
            if i < nplots - 1:
                plt.setp(ax1[i].get_xticklabels(), visible=False)

            if self.apparent_wind_chart:
                data = list(map(lambda x: x.aws if x.aws else 0, y))
                ax1[i].plot(x, data, color="#feffb3", linewidth=0.6, label="AWS")
                ax1[i].legend()

            if self.true_wind_chart:
                data = list(map(lambda x: x.tws if x.tws else 0, y))
                ax1[i].plot(x, data, color="#bfbbd9", linewidth=0.6, label="TWS")
                ax1[i].legend()

            i += 1

        if self.depth_chart:
            if i < nplots - 1:
                plt.setp(ax1[i].get_xticklabels(), visible=False)

            data = list(map(lambda x: x.depth if x.depth else 0, y))
            ax1[i].plot(x, data, color="#fa8174", linewidth=0.6, label="Depth")
            ax1[i].legend()

            i += 1

        if self.hdg_chart or self.apparent_wind_chart or self.true_wind_chart:
            if i < nplots - 1:
                plt.setp(ax1[i].get_xticklabels(), visible=False)

            ax2 = ax1[i]

            if self.apparent_wind_chart:
                if self.rw_chart:
                    data = list(map(lambda x: x.awa if x.awa else 0, y))
                    ax2.plot(x, data, color="#fe11b3", linewidth=0.3, label="AWA")

                data = [
                    (
                        (p.awa + p.hdg) % 360
                        if p.awa is not None and p.hdg is not None
                        else numpy.nan
                    )
                    for p in y
                ]
                ax2.plot(x, data, color="#feffb3", linewidth=0.6, label="AWD")

            if self.true_wind_chart:
                if self.rw_chart:
                    data = list(map(lambda x: x.twa if x.twa else 0, y))
                    ax2.plot(x, data, color="#bf11d9", linewidth=0.3, label="TWA")

                data = [
                    (
                        (p.twa + p.hdg) % 360
                        if p.twa is not None and p.hdg is not None
                        else numpy.nan
                    )
                    for p in y
                ]
                ax2.plot(x, data, color="#bfbbd9", linewidth=0.6, label="TWD")

            if self.hdg_chart:
                data = list(map(lambda x: x.hdg if x.hdg else 0, y))
                ax2.plot(x, data, color="#81b1d2", linewidth=0.6, label="HDG")

            ax2.legend()

        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, dpi=100)
        buf.seek(0)
        buf2 = PIL.Image.open(buf)

        arr = numpy.array(buf2)
        height, width, _ = arr.shape
        surface = cairo.ImageSurface.create_for_data(
            arr.data, cairo.FORMAT_RGB24, width, height
        )

        # Native backends may use a Retina canvas even when savefig uses 100 DPI.
        # Convert canvas coordinates into the saved bitmap's pixels.
        scale_x, scale_y = width / fig.bbox.width, height / fig.bbox.height
        axes = [
            (
                ax.bbox.x0 * scale_x,
                height - ax.bbox.y1 * scale_y,
                ax.bbox.width * scale_x,
                ax.bbox.height * scale_y,
                *ax.get_xlim(),
            )
            for ax in ax1
        ]

        fig.clf()
        plt.close(fig)
        return surface, arr, axes
