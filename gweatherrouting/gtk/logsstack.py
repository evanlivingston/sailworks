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
from datetime import timezone
from pathlib import Path
import shutil
from typing import Any, List, Optional

import cairo
import gi
import nmeatoolkit as nt
from nmeatoolkit.pipes.crop import CropPipe
import numpy
import PIL

gi.require_version("Gtk", "3.0")

from gi.repository import GLib, Gtk

from gweatherrouting.core import Core
from gweatherrouting.core.instrumentrecording import InstrumentRecording
from gweatherrouting.core.timecontrol import TimeControl, naive_wall_time
from gweatherrouting.gtk.charts import ChartManager
from gweatherrouting.gtk.settings import SettingsManager

from .maplayers.geomaplayer import GeoMapLayer
from .maplayers.toolsmaplayer import ToolsMapLayer
from .widgets.mapwidget import MapWidget
from .widgets.timetravel import TimeTravelWidget
from .widgets.tooldock import ToolDock
from .nmeacontrols import NMEAControls, get_nmea_session

logger = logging.getLogger("gweatherrouting")

LOG_TEMP_FILE = "/tmp/gwr-recording.log"


def _crop_pipe(start, end):
    # The Logs clock displays NMEA UTC wall time without timezone metadata.
    bounds = [
        (
            value.replace(tzinfo=timezone.utc)
            if value is not None and value.tzinfo is None
            else value
        )
        for value in (start, end)
    ]
    return CropPipe(*bounds)


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
        self.recording = False
        self.data: List[nt.TrackPoint] = []
        self.recorder = InstrumentRecording(LOG_TEMP_FILE)
        self.session = get_nmea_session(core)
        self._stream_generation = 0
        self._alive = True
        self._redraw_timer = None
        self._export_crop = None
        self._translator = nt.TrackPointTranslator()
        self._wind_pipe = nt.TrueWindPipe()

        self.depth_chart = False
        self.speed_chart = True
        self.apparent_wind_chart = False
        self.true_wind_chart = True
        self.hdg_chart = True
        self.rw_chart = False

        self.highlighted_value: Optional[nt.TrackPoint] = None
        self.crop_a = None
        self.crop_b = None

        self.session.connect("data", self.data_handler)
        self.session.connect("reset", self._reset_stream)
        self.session.connect("changed", self._session_changed)
        self.session.connect("loaded", self._log_loaded)
        self.connect("destroy", self._destroy)
        self.core.connectionManager.connect("nmea2000", self.recorder.write_nmea2000)

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
        self.selected_time = self.time_control.time
        self.timetravel_widget = TimeTravelWidget(
            self.parent, self.time_control, self.map, True
        )
        self.time_control.connect("time_change", self.on_time_change)
        self.builder.get_object("timetravelcontainer").pack_start(
            self.timetravel_widget, True, True, 0
        )

        self.geo_map_layer = GeoMapLayer(self.core, self.time_control)
        self.map.layer_add(self.geo_map_layer)

        body.show_all()
        self.show_all()

        self.builder.get_object("stop-button").hide()

    @property
    def loading(self):
        return self.session.loading

    def on_toggle_nmea(self, widget):
        if widget.get_active():
            self.tool_dock.show_panel("nmea")
        else:
            self.tool_dock.hide_panel("nmea")

    def _panel_visibility(self, dock, key, visible):
        if key == "nmea":
            self.builder.get_object("nmea-toggle").set_active(visible)

    def _session_changed(self, _):
        if self.recording and (
            self.session.source != "live"
            or not self.session.streaming
            or self.session.loading
        ):
            self.on_stop_recording_click(None)
        self.builder.get_object("record-button").set_sensitive(
            self.session.source == "live"
            and self.session.streaming
            and not self.session.loading
        )

    def _reset_stream(self, _):
        self._stream_generation += 1
        self._translator = nt.TrackPointTranslator()
        self._wind_pipe = nt.TrueWindPipe()
        self.data = []
        self.highlighted_value = None
        self.tools_map_layer.gps_clear()
        self.crop_a = self.crop_b = None
        self._export_crop = None
        self.core.logManager.log_history.clear()
        self.map.queue_draw()
        self.graph_area.queue_draw()

    def _log_loaded(self, log):
        self.data = list(log.points)
        self.rebuild_track()
        if self.data:
            point = self.data[0]
            self.time_control.set_time(point.time)
            self.map.set_center_and_zoom(point.lat, point.lon, 12)
        self.graph_area.queue_draw()

    def _destroy(self, _):
        self._alive = False
        if self._redraw_timer is not None:
            GLib.source_remove(self._redraw_timer)
        self.recorder.close()
        self.session.disconnect("data", self.data_handler)
        self.session.disconnect("reset", self._reset_stream)
        self.session.disconnect("changed", self._session_changed)
        self.session.disconnect("loaded", self._log_loaded)
        self.core.connectionManager.disconnect("nmea2000", self.recorder.write_nmea2000)

    def on_time_change(self, time):
        self.selected_time = time
        if (
            not self.recording
            and not self.loading
            and not self.session.playing
            and self.session.source == "log"
        ):
            self.graph_area.queue_draw()
            self.map.queue_draw()

    def set_crop_a(self, widget):
        self.crop_a = self.selected_time
        self.graph_area.queue_draw()

    def set_crop_b(self, widget):
        self.crop_b = self.selected_time
        self.graph_area.queue_draw()

    def crop_data(self, widget):
        if self.recording or self.loading or not self.data or self.session.playing:
            return
        if self.crop_a is None and self.crop_b is None:
            return

        if self.crop_a is None:
            self.crop_a = naive_wall_time(self.data[0].time)

        if self.crop_b is None:
            self.crop_b = naive_wall_time(self.data[-1].time)

        self.data = [
            d
            for d in self.data
            if self.crop_a <= naive_wall_time(d.time) <= self.crop_b
        ]

        self.rebuild_track()
        self.graph_area.queue_draw()

        if self.session.source == "log":
            self._export_crop = (self.crop_a, self.crop_b)
            self.crop_a = self.crop_b = None
            return

        self.recorder.close()

        os.system(f"mv {LOG_TEMP_FILE} {LOG_TEMP_FILE}.2")

        pip = nt.Pipeline(
            nt.FileInput(LOG_TEMP_FILE + ".2"),
            nt.FileOutput(LOG_TEMP_FILE),
            nt.ToStringTranslator(),
            [_crop_pipe(self.crop_a, self.crop_b)],
        )
        pip.run()

        os.system(f"rm {LOG_TEMP_FILE}.2")
        self.crop_a = None
        self.crop_b = None

    def on_recording_click(self, widget):
        if (
            self.recording
            or self.session.loading
            or self.session.source != "live"
            or not self.session.streaming
        ):
            return

        self.builder.get_object("record-button").hide()
        self.builder.get_object("stop-button").show()

        self.recorder.start()
        self._reset_stream(None)
        self.recording = True
        self.status_bar.push(0, "Recording from devices...")

    def on_stop_recording_click(self, widget):
        self.recording = False
        self.recorder.close()
        logger.debug("Stopping recording...")

        self.builder.get_object("record-button").show()
        self.builder.get_object("stop-button").hide()
        self.status_bar.push(0, "Recording stopped")

        self.graph_area.queue_draw()

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
        if self.recording:
            self.recorder.write_packets(packets)
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

    def on_save_click(self, widget):
        dialog = Gtk.FileChooserDialog(
            "Save log",
            self.parent,
            Gtk.FileChooserAction.SAVE,
            (
                Gtk.STOCK_CANCEL,
                Gtk.ResponseType.CANCEL,
                Gtk.STOCK_SAVE,
                Gtk.ResponseType.OK,
            ),
        )
        dialog.set_do_overwrite_confirmation(True)
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            filename = dialog.get_filename()
            dialog.destroy()
            self.save_to_file(filename)

            self.status_bar.push(0, f"Saved to {filename}")
        else:
            dialog.destroy()

    def load_from_file(self, filepath):
        self.nmea.load_path(filepath)

    def save_to_file(self, filepath):
        if self.session.source != "log" or self.session.log is None:
            self.recorder.save(filepath)
            return
        crop = _crop_pipe(*self._export_crop) if self._export_crop else None
        with open(filepath, "w", encoding="utf-8") as output:
            for _, packet in self.session.log.entries:
                if crop is None or packet.data is None or crop.transform(packet.data):
                    output.write(packet.serialize() + "\n")
        # Cropping changes interpreted sentences only, preserving the raw archive.
        raw = Path(self.session.log.path + ".n2k.log")
        destination = Path(str(filepath) + ".n2k.log")
        if raw.exists() and raw.resolve() != destination.resolve():
            shutil.copyfile(raw, destination)

    def clear_data(self, widget):
        if self.recording or self.loading:
            return
        if self.session.source == "log":
            self.session.restart()
        else:
            self._reset_stream(None)

        self.recorder.close()
        self.recorder.start()
        if not self.recording:
            self.recorder.close()
        self.core.logManager.log_history.clear()
        self.map.queue_draw()
        self.graph_area.queue_draw()
        logger.debug("Data cleared")

    def rebuild_track(self):
        self.core.logManager.log_history.clear()
        for x in self.data[0::150]:
            self.core.logManager.log_history.add(x.lat, x.lon)
        self.map.queue_draw()

    def on_graph_draw(self, widget, ctx):  # noqa: C901
        s = max(1, len(self.data) // 2000)
        a = widget.get_allocation()
        width = a.width

        if len(self.data) == 0:
            return

        import matplotlib.pyplot as plt

        plt.style.use("dark_background")
        plt.rcParams.update({"font.size": 8})
        y = self.data[::s]
        x: Any = numpy.array([x.time for x in y])

        nplots = 1

        if self.hdg_chart or self.apparent_wind_chart or self.true_wind_chart:
            nplots += 1

        if self.speed_chart:
            nplots += 1

        if self.depth_chart:
            nplots += 1

        fig, ax1 = plt.subplots(nplots)
        try:
            ax1[0]
        except:
            ax1 = [ax1]
        fig.set_size_inches((width / 100), (a.height / 100.0))

        i = 0
        ii: Any = -1
        self.highlighted_value = None
        self.status_bar.push(0, "")

        if not self.recording and not self.loading:
            x = list(map(lambda x: x.replace(tzinfo=None), x))

            ii = numpy.where(
                (x > (numpy.datetime64(self.selected_time)))
                & (
                    x
                    < (
                        numpy.datetime64(self.selected_time)
                        + numpy.timedelta64(
                            self.timetravel_widget.get_change_unit(), "s"
                        )
                    )
                )
            )
            try:
                ii = ii[0][0]  # type: ignore
                self.highlighted_value = y[ii]
                self.tools_map_layer.gps_add(
                    self.highlighted_value.lat,
                    self.highlighted_value.lon,
                    self.highlighted_value.hdg or 0,
                    self.highlighted_value.speed,
                )

                self.status_bar.push(
                    0,
                    f"Time: {self.selected_time}, "
                    + f"Position: ({self.highlighted_value.lat:.2f}, "
                    + f"{self.highlighted_value.lon:.2f}), "
                    + f"Speed: {self.highlighted_value.speed:.1f}kn, "
                    + f"Heading: {self.highlighted_value.hdg}, "
                    + f"TWS: {self.highlighted_value.tws:.1f}kn, "
                    + f"TWA: {self.highlighted_value.twa}, "
                    + f"TWD: {(self.highlighted_value.twa + self.highlighted_value.hdg) % 360}, "
                    + f"Depth: {self.highlighted_value.depth:.2f}",
                )

            except (IndexError, TypeError, ValueError):
                ii = -1

        def highlight(i, data):
            if ii != -1:
                ax1[i].plot(x[ii], data[ii], color="#f00", marker="o", markersize=4)
                # ax1[i].axvline(x=ii)

        if self.speed_chart:
            if i < nplots - 1:
                plt.setp(ax1[i].get_xticklabels(), visible=False)

            data = list(map(lambda x: x.speed if x.speed else 0, y))
            ax1[i].plot(x, data, color="#8dd3c7", linewidth=0.6, label="Speed")

            highlight(i, data)

            ax1[i].legend()
            i += 1

        if self.apparent_wind_chart or self.true_wind_chart:
            if i < nplots - 1:
                plt.setp(ax1[i].get_xticklabels(), visible=False)

            if self.apparent_wind_chart:
                data = list(map(lambda x: x.aws if x.aws else 0, y))
                ax1[i].plot(x, data, color="#feffb3", linewidth=0.6, label="AWS")
                ax1[i].legend()

                highlight(i, data)
            if self.true_wind_chart:
                data = list(map(lambda x: x.tws if x.tws else 0, y))
                ax1[i].plot(x, data, color="#bfbbd9", linewidth=0.6, label="TWS")
                ax1[i].legend()

                highlight(i, data)
            i += 1

        if self.depth_chart:
            if i < nplots - 1:
                plt.setp(ax1[i].get_xticklabels(), visible=False)

            data = list(map(lambda x: x.depth if x.depth else 0, y))
            ax1[i].plot(x, data, color="#fa8174", linewidth=0.6, label="Depth")
            ax1[i].legend()

            highlight(i, data)
            i += 1

        if self.hdg_chart or self.apparent_wind_chart or self.true_wind_chart:
            if i < nplots - 1:
                plt.setp(ax1[i].get_xticklabels(), visible=False)

            ax2 = ax1[i]

            if self.apparent_wind_chart:
                if self.rw_chart:
                    data = list(map(lambda x: x.awa if x.awa else 0, y))
                    ax2.plot(x, data, color="#fe11b3", linewidth=0.3, label="AWA")
                    highlight(i, data)

                data = [
                    (
                        (p.awa + p.hdg) % 360
                        if p.awa is not None and p.hdg is not None
                        else numpy.nan
                    )
                    for p in y
                ]
                ax2.plot(x, data, color="#feffb3", linewidth=0.6, label="AWD")
                highlight(i, data)
            if self.true_wind_chart:
                if self.rw_chart:
                    data = list(map(lambda x: x.twa if x.twa else 0, y))
                    ax2.plot(x, data, color="#bf11d9", linewidth=0.3, label="TWA")
                    highlight(i, data)

                data = [
                    (
                        (p.twa + p.hdg) % 360
                        if p.twa is not None and p.hdg is not None
                        else numpy.nan
                    )
                    for p in y
                ]
                ax2.plot(x, data, color="#bfbbd9", linewidth=0.6, label="TWD")

                highlight(i, data)
            if self.hdg_chart:
                data = list(map(lambda x: x.hdg if x.hdg else 0, y))
                ax2.plot(x, data, color="#81b1d2", linewidth=0.6, label="HDG")

                highlight(i, data)

            ax2.legend()

        if self.crop_a is not None:
            try:
                ii = numpy.where(
                    (x > (numpy.datetime64(self.crop_a)))
                    & (
                        x
                        < (
                            numpy.datetime64(self.crop_a)
                            + numpy.timedelta64(
                                self.timetravel_widget.get_change_unit(), "s"
                            )
                        )
                    )
                )
                for axx in ax1:
                    axx.axvline(x=x[ii[0][0]], color="#ffffff", linewidth=0.5)
            except:
                pass

        if self.crop_b is not None:
            try:
                ii = numpy.where(
                    (x > (numpy.datetime64(self.crop_b)))
                    & (
                        x
                        < (
                            numpy.datetime64(self.crop_b)
                            + numpy.timedelta64(
                                self.timetravel_widget.get_change_unit(), "s"
                            )
                        )
                    )
                )
                for axx in ax1:
                    axx.axvline(x=x[ii[0][0]], color="#ffffff", linewidth=0.5)
            except:
                pass

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

        ctx.save()
        ctx.set_source_surface(surface, 0, 0)
        ctx.paint()
        ctx.restore()

        fig.clf()
        plt.close(fig)
