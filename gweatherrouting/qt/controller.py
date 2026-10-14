"""Qt scheduling and worker notifications around the existing NMEA session."""

import time
from threading import Thread

import nmeatoolkit as nt
from PySide6.QtCore import QObject, QTimer, Signal, Slot

from gweatherrouting.core.connectionmanager import ConnectionManager
from gweatherrouting.core.nmeasession import NMEASession, read_nmea_log
from gweatherrouting.core.utils import EventDispatcher


class SessionController(QObject):
    changed = Signal()
    model_changed = Signal()
    load_finished = Signal(int, object, object)
    packets = Signal(object, int)

    def __init__(self, offline=False, recording_directory=None, parent=None):
        super().__init__(parent)
        self.offline = offline
        self.connections = EventDispatcher() if offline else ConnectionManager()
        self.session = NMEASession(self.connections, recording_directory)
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

    def start_connections(self):
        if isinstance(self.connections, ConnectionManager):
            self.connections.start_polling()

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
        self.timer.stop()
        if isinstance(self.connections, ConnectionManager):
            self.connections.stop_polling()
        self.session.close()
        for event, callback in (
            ("changed", self._changed),
            ("reset", self._reset),
            ("loaded", self._loaded),
            ("data", self._queue_packets),
        ):
            self.session.disconnect(event, callback)
