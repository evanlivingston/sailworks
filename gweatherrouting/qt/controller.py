"""Qt scheduling and worker notifications around the existing NMEA session."""

import logging
import time
from threading import Thread
from weakref import WeakKeyDictionary

import nmeatoolkit as nt
from PySide6.QtCore import QObject, QTimer, Signal, Slot

from gweatherrouting.core import Core, TimeControl
from gweatherrouting.core.settings import SettingsManager
from gweatherrouting.rendering.chartcollection import ChartCollection
from gweatherrouting.core.nmeasession import read_nmea_log
from .scheduling import UiScheduler
from .routingcontroller import RoutingController
from .backgroundtask import BackgroundTask
from .datacontroller import DataController
from .polarcontroller import PolarController
from .mapdatasetup import MapDataController


class SessionController(QObject):
    changed = Signal()
    boat_changed = Signal(object)
    model_changed = Signal()
    load_finished = Signal(int, object, object)
    packets = Signal(object, int)
    renderer_changed = Signal()

    def __init__(
        self,
        offline=False,
        recording_directory=None,
        parent=None,
        load_charts=True,
        map_renderer=None,
    ):
        super().__init__(parent)
        if map_renderer not in (None, "cpu", "gpu"):
            raise ValueError("Map renderer must be cpu or gpu")
        self.offline = offline
        self.load_charts = load_charts
        self.scheduler = UiScheduler(self)
        self.core = Core(
            schedule=self.scheduler.submit,
            start_connections=False,
            recording_directory=recording_directory,
        )
        self.time_control = TimeControl()
        self.settings = SettingsManager()
        selected_renderer = map_renderer or self.settings.get("mapRenderer", "cpu")
        self.map_renderer = (
            selected_renderer if selected_renderer in ("cpu", "gpu") else "cpu"
        )
        self.renderer_override = map_renderer is not None
        self._renderer_states = WeakKeyDictionary()
        self.charts = ChartCollection(self.settings, self.scheduler.submit)
        if offline:
            self.charts._chs_renderer.set_enabled(False)
        self.chart_errors = []
        if load_charts:
            self.charts.load_base_chart()
            for kind in ("vector", "raster", "mbtiles"):
                loader = getattr(self.charts, "load_" + kind + "_layer")
                for entry in self.settings[kind + "Charts"]:
                    try:
                        loader(
                            entry["path"],
                            entry.get("metadata"),
                            entry.get("enabled", True),
                        )
                    except Exception as exc:
                        self.chart_errors.append(str(exc))
                        logging.getLogger("gweatherrouting").warning(
                            "Unable to load chart %s: %s", entry["path"], exc
                        )
        self.core.connect("boat_data", self.boat_changed.emit)
        self.connections = self.core.connectionManager
        self.session = self.core.nmea_session
        self.routing = RoutingController(self)
        self.connection_task = BackgroundTask(self)
        self.data = DataController(self)
        self.polars = PolarController(self)
        self.map_data = MapDataController(self)
        self.points = []
        self.point_revision = 0
        self.generation = 0
        self.closed = False
        self.translator = nt.TrackPointTranslator()
        self.wind_pipe = nt.TrueWindPipe()
        self.session.connect("changed", self._changed)
        self.session.connect("reset", self._reset)
        self.session.connect("loaded", self._loaded)
        self.session.connect("data", self._queue_packets)
        self.load_finished.connect(self._finish_load)
        self.packets.connect(self._consume)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._tick)
        self.last_tick = time.monotonic()
        self.timer.start()

    def set_renderer_status(self, canvas, actual, reason="", api=""):
        state = dict(actual=actual, reason=reason, api=api)
        if self._renderer_states.get(canvas) == state:
            return
        self._renderer_states[canvas] = state
        self.renderer_changed.emit()

    def forget_renderer(self, canvas):
        self._renderer_states.pop(canvas, None)
        self.renderer_changed.emit()

    def renderer_status(self):
        states = list(self._renderer_states.values())
        failures = [s["reason"] for s in states if s["reason"]]
        if failures:
            return "CPU fallback: " + "; ".join(dict.fromkeys(failures))
        apis = sorted({s["api"] for s in states if s["actual"] == "gpu" and s["api"]})
        if apis:
            return "GPU: " + ", ".join(apis)
        if self.map_renderer == "gpu":
            return "GPU requested; waiting for a visible map"
        return "CPU" + (" (command-line override)" if self.renderer_override else "")

    def start_connections(self):
        if (
            not self.closed
            and not self.offline
            and self.session.streaming
            and not self.connections.polling_active
        ):
            self.connection_task.start(
                "Connecting instruments…", lambda _: self.connections.start_polling()
            )

    def set_live_reading(self, active):
        """Start/stop device reads, not just delivery to the live display."""
        if self.closed or self.connection_task.busy:
            return
        self.session.set_streaming(active)
        if active:
            self.start_connections()
        elif self.connections.polling_active:
            self.connection_task.start(
                "Disconnecting instruments…", lambda _: self.connections.stop_polling()
            )

    def _changed(self, _):
        self.changed.emit()

    def _reset(self, _):
        self.generation += 1
        self.translator = nt.TrackPointTranslator()
        self.wind_pipe = nt.TrueWindPipe()
        self.points = (
            list(self.session.log.points)
            if self.session.source == "log" and self.session.log
            else []
        )
        self.point_revision += 1
        self.model_changed.emit()

    def _loaded(self, log):
        self.points = list(log.points)
        self.point_revision += 1
        self.model_changed.emit()

    def _queue_packets(self, packets):
        if self.session.source == "live":
            self.packets.emit(tuple(packets), self.generation)

    @Slot(object, int)
    def _consume(self, packets, generation):
        if (
            self.closed
            or generation != self.generation
            or self.session.source != "live"
            or not self.session.streaming
        ):
            return
        for packet in packets:
            sentence = packet.data
            if sentence is None or getattr(sentence, "sentence_type", "") in (
                "ALK",
                "VDM",
            ):
                continue
            try:
                for interpreted in self.wind_pipe.transform(sentence):
                    point = self.translator.feed(interpreted)
                    if point is not None:
                        self.points.append(point)
                        self.translator.track.clear()
                        self.point_revision += 1
            except (ValueError, TypeError, AttributeError):
                continue

    def _tick(self):
        now = time.monotonic()
        self.session.advance(now - self.last_tick)
        self.last_tick = now

    def load_path(self, path):
        token = self.session.begin_load()

        def read():
            try:
                log, error = read_nmea_log(path), None
            except (OSError, ValueError) as exc:
                log, error = None, exc
            if not self.closed:
                self.load_finished.emit(token, log, error)

        Thread(target=read, daemon=True).start()

    @Slot(int, object, object)
    def _finish_load(self, token, log, error):
        if not self.closed:
            self.session.finish_load(token, log, error)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.routing.close()
        self.map_data.close()
        self.timer.stop()
        self.polars.close()
        self.data.close()
        self.connection_task.close(wait=True)
        self.connections.stop_polling()
        self.session.close()
        self.core.nmea_session.disconnect("data", self.core.data_handler)
        for canvas in list(self.charts.maps):
            canvas.dispose()
        self.core.disconnect("boat_data", self.boat_changed.emit)
        self.core.nmea_session.disconnect("reset", self.core._reset_nmea)
        self.scheduler.close()
        for event, callback in (
            ("changed", self._changed),
            ("reset", self._reset),
            ("loaded", self._loaded),
            ("data", self._queue_packets),
        ):
            self.session.disconnect(event, callback)
