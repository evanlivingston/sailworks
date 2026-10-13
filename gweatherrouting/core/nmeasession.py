"""One selected instrument stream for charts, logs and polar performance."""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import List, Optional, Tuple

import pynmea2
import nmeatoolkit as nt

from .datasource import AISDataPacket, DataPacket, NMEADataPacket
from .instrumentrecording import InstrumentRecording
from .polarperformance import PerformanceHistory
from .storage import DATA_DIR
from .utils import EventDispatcher


@dataclass(frozen=True)
class NMEALog:
    path: str
    entries: Tuple[Tuple[float, DataPacket], ...]  # elapsed seconds, packet
    skipped: int
    timestamped: bool
    samples: tuple = ()
    points: tuple = ()
    point_offsets: tuple = ()

    @property
    def duration(self):
        return self.entries[-1][0] if self.entries else 0.0


class _LogTiming:
    def __init__(self):
        self.first_stamp = self.last_stamp = None
        self.day = 0
        self.elapsed = 0.0

    def offset(self, packet, index):
        stamp = getattr(packet.data, "timestamp", None)
        if stamp is not None:
            seconds = (
                stamp.hour * 3600
                + stamp.minute * 60
                + stamp.second
                + stamp.microsecond / 1000000
            )
            if self.last_stamp is not None and seconds < self.last_stamp - 43200:
                self.day += 86400
            if self.first_stamp is None:
                self.first_stamp = seconds + self.day - self.elapsed
            self.elapsed = max(self.elapsed, seconds + self.day - self.first_stamp)
            self.last_stamp = seconds
        elif self.first_stamp is None:
            self.elapsed = index / 10.0
        return self.elapsed


def _track_sentence(translator, wind_pipe, packet):
    if packet.data is None or getattr(packet.data, "sentence_type", "") in (
        "ALK",
        "VDM",
    ):
        return
    try:
        for sentence in wind_pipe.transform(packet.data):
            translator.feed(sentence)
    except (ValueError, TypeError, AttributeError):
        pass


def read_nmea_log(path):
    """Use NMEA UTC timestamps; untimed logs play at ten sentences/second.

    Interpreted NMEA 2000 companion logs are supported. Raw gateway archives
    are not NMEA sentences and are deliberately rejected rather than guessed.
    """
    entries: List[Tuple[float, DataPacket]] = []
    skipped = 0
    point_offsets = []
    timing = _LogTiming()
    history = PerformanceHistory(max_age=50, interval=0)
    translator = nt.TrackPointTranslator()
    wind_pipe = nt.TrueWindPipe()
    with open(path, encoding="utf-8", errors="replace") as stream:
        for line_index, line in enumerate(stream):
            line = line.strip()
            try:
                packet = (
                    AISDataPacket(line)
                    if line.startswith(("!AIVDM,", "!AIVDO,"))
                    else NMEADataPacket.parse(line)
                )
                elapsed = timing.offset(packet, len(entries))
                setattr(packet, "log_index", line_index)
                history.ingest(packet.data, now=line_index)
                previous_points = len(translator.track)
                _track_sentence(translator, wind_pipe, packet)
                point_offsets.extend(
                    [elapsed] * (len(translator.track) - previous_points)
                )
                entries.append((elapsed, packet))
            except (pynmea2.ParseError, ValueError, AttributeError):
                skipped += 1
            if len(entries) > 1000000:
                raise OSError("Log exceeds the one-million-sentence playback limit")
    if not entries:
        raise ValueError(
            "No interpreted NMEA sentences found. "
            "Use the .log companion of a raw .n2k.log archive."
        )
    return NMEALog(
        str(path),
        tuple(entries),
        skipped,
        timing.first_stamp is not None,
        history.snapshot()[1],
        tuple(translator.track),
        tuple(point_offsets),
    )


class NMEASession(EventDispatcher):
    """Shared state; callers schedule advance() and marshal UI notifications.

    Connection callbacks may run on a worker. The lock makes source changes
    atomic, so live data cannot be mixed into a replay batch.
    """

    def __init__(self, connections, recording_directory=None):
        super().__init__()
        self.connections = connections
        self.lock = RLock()
        self.source = "live"
        self.streaming = True
        self.playing = False
        self.speed = 1.0
        self.position = 0.0
        self.cursor = 0
        self.log: Optional[NMEALog] = None
        self.overview = False
        self.loading = False
        self.error = ""
        self.history = PerformanceHistory()
        self.replay_history = PerformanceHistory(max_age=50, interval=0)
        self.recording = False
        self._closed = False
        self.recorder: Optional[InstrumentRecording] = None
        self.recording_directory = Path(
            recording_directory or Path(DATA_DIR) / "recordings"
        )
        self._generation = 0
        connections.connect("data", self._live_data)
        connections.connect("nmea2000", self._record_raw)

    @property
    def active_history(self):
        return self.history if self.source == "live" else self.replay_history

    def _deliver(self, packets, replay=False, start=0):
        history = self.replay_history if replay else self.history
        for index, packet in enumerate(packets):
            history.ingest(
                packet.data,
                now=getattr(packet, "log_index", start + index) if replay else None,
            )
        self.dispatch("data", packets)

    def _live_data(self, packets):
        with self.lock:
            if self.source == "live" and self.streaming and not self.loading:
                if self.recording:
                    assert self.recorder is not None
                    self.recorder.write_packets(packets)
                self._deliver(packets)

    def select_source(self, source):
        with self.lock:
            if source == self.source:
                return
            self.stop_recording()
            self.playing = False
            self.source = source
            if source == "log":
                self.restart()
            else:
                self.dispatch("reset", None)
            self.dispatch("changed", None)

    def set_streaming(self, active):
        with self.lock:
            if not active:
                self.stop_recording()
            self.streaming = active
            self.dispatch("changed", None)

    def begin_load(self):
        with self.lock:
            self.stop_recording()
            self._generation += 1
            self.loading = True
            self.playing = False
            self.error = ""
            self.dispatch("changed", None)
            return self._generation

    def finish_load(self, generation, log=None, error=None):
        with self.lock:
            if generation != self._generation:
                return
            self.loading = False
            self.error = str(error) if error else ""
            if log is not None:
                self.log = log
                self.source = "log"
                self.restart()
                self.replay_history.samples.extend(log.samples)
                self.replay_history.revision += 1
                self.overview = True
                self.dispatch("loaded", log)
            self.dispatch("changed", None)

    def restart(self):
        with self.lock:
            self.playing = False
            self.position = 0.0
            self.cursor = 0
            self.overview = False
            self.replay_history.clear()
            self.dispatch("reset", None)
            self.dispatch("changed", None)

    def set_playing(self, active):
        with self.lock:
            if active and self.log and self.source == "log" and not self.loading:
                if self.overview or self.cursor == len(self.log.entries):
                    self.restart()
                self.playing = True
            else:
                self.playing = False
            self.dispatch("changed", None)

    def advance(self, elapsed):
        with self.lock:
            if (
                not self.playing
                or self.loading
                or self.source != "log"
                or self.log is None
            ):
                return
            self.position = min(self.log.duration, self.position + elapsed * self.speed)
            start = self.cursor
            # Bound work per GTK tick; subsequent ticks drain overdue entries.
            while (
                self.cursor < len(self.log.entries)
                and self.log.entries[self.cursor][0] <= self.position
                and self.cursor - start < 1000
            ):
                self.cursor += 1
            packets = [p for _, p in self.log.entries[start : self.cursor]]
            if packets:
                self._deliver(packets, replay=True, start=start)
            if self.cursor == len(self.log.entries):
                self.playing = False
            self.dispatch("changed", None)

    @property
    def recording_path(self):
        """The current or most recent capture, already saved on disk."""
        return self.recorder.path if self.recorder is not None else None

    def start_recording(self):
        with self.lock:
            if (
                self._closed
                or self.recording
                or self.loading
                or self.source != "live"
                or not self.streaming
            ):
                return
            self.recording_directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            index = 0
            while True:
                suffix = f"-{index}" if index else ""
                recorder = InstrumentRecording(
                    self.recording_directory / f"nmea-{stamp}{suffix}.log"
                )
                try:
                    recorder.start(exclusive=True)
                except FileExistsError:
                    index += 1
                    continue
                self.recorder = recorder
                break
            self.recording = True
            self.dispatch("reset", None)
            self.dispatch("changed", None)

    def stop_recording(self):
        with self.lock:
            if not self.recording:
                return
            self.recording = False
            assert self.recorder is not None
            self.recorder.close()
            self.dispatch("changed", None)

    def stop(self):
        """Stop capture or replay without resetting either data history."""
        with self.lock:
            if self.recording:
                self.stop_recording()
            elif self.playing:
                self.set_playing(False)

    def _record_raw(self, messages):
        with self.lock:
            if self.recording:
                assert self.recorder is not None
                self.recorder.write_nmea2000(messages)

    def close(self):
        with self.lock:
            if self._closed:
                return
            self._closed = True
            self.stop_recording()
            self.playing = False
            self.streaming = False
            self._generation += 1
            self.connections.disconnect("data", self._live_data)
            self.connections.disconnect("nmea2000", self._record_raw)
