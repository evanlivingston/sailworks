"""One selected instrument stream for charts, logs and polar performance."""

import logging
from array import array
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import math
from pathlib import Path
from threading import RLock
from typing import List, Mapping, Optional, Tuple

import pynmea2
import nmeatoolkit as nt

from .datasource import AISDataPacket, DataPacket, NMEADataPacket
from .instrumentrecording import InstrumentRecording
from .n2kdevices import CapturedDevice
from .polarperformance import PerformanceHistory
from .storage import DATA_DIR
from .utils import EventDispatcher

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NMEATrack:
    """Visualization fixes from one GPS, on the log's shared elapsed clock."""

    points: tuple = ()
    point_offsets: tuple = ()


@dataclass(frozen=True)
class LoggedFieldSeries:
    """One numeric field found in the decoded log, stored compactly."""

    key: str
    label: str
    offsets: array
    values: array


class NumericFieldCollector:
    """Collect numeric sentence fields for user-selectable log charts."""

    def __init__(self):
        self._series = {}

    def add(self, elapsed, packet):
        sentence = getattr(packet, "data", None)
        fields = getattr(sentence, "fields", ()) if sentence is not None else ()
        sentence_type = getattr(sentence, "sentence_type", "NMEA")
        for definition in fields:
            if len(definition) < 2:
                continue
            display, attribute = definition[:2]
            if not isinstance(attribute, str):
                continue
            value = getattr(sentence, attribute, None)
            if attribute == "lat":
                value = getattr(sentence, "latitude", value)
            elif attribute == "lon":
                value = getattr(sentence, "longitude", value)
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(numeric):
                continue
            key = f"{sentence_type}.{attribute}"
            label = f"{sentence_type} · {display}"
            if sentence_type == "MWV" and attribute == "wind_speed":
                unit = {"N": "kn", "M": "m/s", "K": "km/h"}.get(
                    getattr(sentence, "wind_speed_units", None)
                )
                if unit:
                    label += f" ({unit})"
                    key += f"[{unit}]"
            series = self._series.get(key)
            if series is None:
                series = [label, array("d"), array("d")]
                self._series[key] = series
            series[1].append(float(elapsed))
            series[2].append(numeric)

    def snapshot(self):
        return tuple(
            LoggedFieldSeries(key, values[0], values[1], values[2])
            for key, values in sorted(self._series.items())
        )


@dataclass(frozen=True)
class NMEALog:
    path: str
    entries: Tuple[Tuple[float, DataPacket], ...]  # elapsed seconds, packet
    skipped: int
    timestamped: bool
    samples: tuple = ()
    points: tuple = ()
    point_offsets: tuple = ()
    diagnostics: tuple = ()
    format: str = "NMEA 0183"
    # EBL tracks only; points/point_offsets remain the Auto selection for
    # existing consumers. Source choice never filters entries or samples.
    source_tracks: Mapping[int, NMEATrack] = field(default_factory=dict)
    default_track_source: Optional[int] = None
    devices: Tuple[CapturedDevice, ...] = ()
    device_metadata_available: bool = False
    frame_count: Optional[int] = None
    timestamp_origin: Optional[datetime] = None
    numeric_series: Tuple[LoggedFieldSeries, ...] = ()

    @property
    def duration(self):
        return self.entries[-1][0] if self.entries else 0.0


class _LogTiming:
    def __init__(self):
        self.first_stamp = self.last_stamp = None
        self.first_absolute = None
        self.day = 0
        self.elapsed = 0.0

    def offset(self, packet, index, absolute=None):
        if absolute is not None:
            if self.first_absolute is None:
                self.first_absolute = absolute
            self.elapsed = max(
                self.elapsed, (absolute - self.first_absolute).total_seconds()
            )
            return self.elapsed
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


def read_nmea_log(
    path,
    progress=None,
    *,
    frame_count: Optional[int] = None,
    devices: Tuple[CapturedDevice, ...] = (),
    device_metadata_available: bool = False,
    timestamp_origin: Optional[datetime] = None,
):
    """Use NMEA UTC timestamps; untimed logs play at ten sentences/second.

    Interpreted NMEA 2000 companion logs are supported. EBL archives use an
    explicitly partial CAN decoder with diagnostics; other raw gateway
    archives are not guessed to be NMEA sentences.

    Optional EBL metadata (frame_count, devices, etc.) can be supplied by
    callers that have access to the library's file_info (e.g., decoded .log
    files that originated from EBL).
    """
    if Path(path).suffix.lower() == ".ebl":
        from .ebllog import read_ebl_log

        return read_ebl_log(path, progress=progress)
    total = Path(path).stat().st_size
    if progress is not None:
        progress(0, total)
    reported = 0
    entries: List[Tuple[float, DataPacket]] = []
    skipped = 0
    point_offsets = []
    timing = _LogTiming()
    history = PerformanceHistory(max_age=50, interval=0)
    numeric_fields = NumericFieldCollector()
    translator = nt.TrackPointTranslator()
    wind_pipe = nt.TrueWindPipe()
    source_translators = {}
    source_winds = {}
    source_points = {}
    source_point_offsets = {}
    seen_source_fixes = set()
    with open(path, encoding="utf-8", errors="replace") as stream:
        for line_index, line in enumerate(stream):
            line = line.strip()
            try:
                explicit_timestamp = None
                explicit_offset = None
                explicit_source = None
                if line.startswith("@"):
                    prefix, source_text, line = line[1:].split("\t", 2)
                    if prefix.startswith("+"):
                        explicit_offset = float(prefix[1:])
                        if not math.isfinite(explicit_offset) or explicit_offset < 0:
                            raise ValueError(
                                "Replay offsets must be finite and nonnegative"
                            )
                    else:
                        explicit_timestamp = datetime.fromisoformat(
                            prefix.replace("Z", "+00:00")
                        )
                        if explicit_timestamp.tzinfo is None:
                            raise ValueError(
                                "Decoded log timestamps must include a timezone"
                            )
                    if source_text not in ("", "-"):
                        explicit_source = int(source_text)
                packet = (
                    AISDataPacket(line)
                    if line.startswith(("!AIVDM,", "!AIVDO,"))
                    else NMEADataPacket.parse(line)
                )
                if explicit_source is not None:
                    packet.source = explicit_source
                natural_elapsed = timing.offset(
                    packet, len(entries), absolute=explicit_timestamp
                )
                elapsed = (
                    explicit_offset if explicit_offset is not None else natural_elapsed
                )
                numeric_fields.add(elapsed, packet)
                setattr(packet, "log_index", line_index)
                if explicit_timestamp is not None:
                    packet.recorded_at = explicit_timestamp
                history.ingest(packet.data, now=line_index)
                source = getattr(packet, "source", None)
                track_translator = translator
                track_wind = wind_pipe
                if source is not None:
                    track_translator = source_translators.setdefault(
                        source, nt.TrackPointTranslator()
                    )
                    track_wind = source_winds.setdefault(source, nt.TrueWindPipe())
                previous_points = len(track_translator.track)
                _track_sentence(track_translator, track_wind, packet)
                new_points = track_translator.track[previous_points:]
                if source is None:
                    point_offsets.extend([elapsed] * len(new_points))
                else:
                    for point in new_points:
                        key = (source, explicit_timestamp, point.lat, point.lon)
                        if key not in seen_source_fixes:
                            seen_source_fixes.add(key)
                            source_points.setdefault(source, []).append(point)
                            source_point_offsets.setdefault(source, []).append(elapsed)
                entries.append((elapsed, packet))
            except (pynmea2.ParseError, ValueError, AttributeError):
                skipped += 1
            if len(entries) > 1000000:
                raise OSError("Log exceeds the one-million-sentence playback limit")
            if progress is not None and line_index % 256 == 255:
                # The binary position is bytes read for decoding (with bounded
                # text-buffer read-ahead), not TextIOWrapper's opaque newline
                # cookie. Reserve 100% for the fully constructed result.
                consumed = min(stream.buffer.tell(), max(0, total - 1))
                if consumed - reported >= 64 * 1024:
                    progress(consumed, total)
                    reported = consumed
    if not entries:
        raise ValueError(
            "No interpreted NMEA sentences found. "
            "Use the .log companion of a raw .n2k.log archive."
        )
    source_tracks = {
        source: NMEATrack(tuple(points), tuple(source_point_offsets[source]))
        for source, points in sorted(source_points.items())
    }
    default_source = max(
        source_tracks,
        key=lambda source: len(source_tracks[source].points),
        default=None,
    )
    selected_track = (
        source_tracks[default_source] if default_source is not None else None
    )
    log = NMEALog(
        str(path),
        tuple(entries),
        skipped,
        timing.first_stamp is not None or timing.first_absolute is not None,
        history.snapshot()[1],
        (
            selected_track.points
            if selected_track is not None
            else tuple(translator.track)
        ),
        (
            selected_track.point_offsets
            if selected_track is not None
            else tuple(point_offsets)
        ),
        source_tracks=source_tracks,
        default_track_source=default_source,
        devices=devices,
        device_metadata_available=device_metadata_available,
        frame_count=frame_count,
        timestamp_origin=timestamp_origin,
        numeric_series=numeric_fields.snapshot(),
    )
    if progress is not None:
        progress(total, total)
    return log


def ripple_delete_log(log, start, end):
    """Build an edited log snapshot without mutating packets or NMEA times."""
    start = max(0.0, float(start))
    end = min(float(end), log.duration)
    if end <= start:
        return None
    delta = end - start
    entries = tuple(
        (offset if offset < start else offset - delta, packet)
        for offset, packet in log.entries
        if offset < start or offset > end
    )
    if entries == log.entries:
        return None

    def trim(points, offsets):
        retained = [
            (point, offset)
            for point, offset in zip(points, offsets)
            if offset < start or offset > end
        ]
        return (
            tuple(point for point, _ in retained),
            tuple(
                offset if offset < start else offset - delta for _, offset in retained
            ),
        )

    points, point_offsets = trim(log.points, log.point_offsets)
    tracks = {}
    for source, track in log.source_tracks.items():
        track_points, track_offsets = trim(track.points, track.point_offsets)
        tracks[source] = NMEATrack(track_points, track_offsets)

    history = PerformanceHistory(max_age=50, interval=0)
    for index, (_, packet) in enumerate(entries):
        history.ingest(packet.data, now=getattr(packet, "log_index", index))
    numeric_series = []
    for series in log.numeric_series:
        offsets = array("d")
        values = array("d")
        for offset, value in zip(series.offsets, series.values):
            if offset < start:
                offsets.append(offset)
                values.append(value)
            elif offset > end:
                offsets.append(offset - delta)
                values.append(value)
        if offsets:
            numeric_series.append(
                LoggedFieldSeries(series.key, series.label, offsets, values)
            )
    return replace(
        log,
        entries=entries,
        samples=history.snapshot()[1],
        points=points,
        point_offsets=point_offsets,
        source_tracks=tracks,
        numeric_series=tuple(numeric_series),
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
        self.preview_position = None
        self.seeking = False
        self._seek_target = None
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
        self._log_edit_undo = []
        self._saved_log = None
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
            self.cancel_seek()
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
            self.cancel_seek()
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
                self._log_edit_undo.clear()
                self._saved_log = log
                self.source = "log"
                self.restart()
                self.replay_history.samples.extend(log.samples)
                self.replay_history.revision += 1
                self.overview = True
                self.dispatch("loaded", log)
            self.dispatch("changed", None)

    @property
    def log_edits_dirty(self):
        return self.log is not None and self.log is not self._saved_log

    def mark_log_saved(self):
        with self.lock:
            if self.log is None:
                return False
            self._saved_log = self.log
            self.dispatch("changed", None)
            return True

    def delete_log_range(self, start, end):
        """Ripple-delete an elapsed-time range without changing NMEA timestamps."""
        request = self.prepare_log_range_delete(start, end)
        if request is None:
            return False
        source_log, start, end, _generation = request
        edited = ripple_delete_log(source_log, start, end)
        if edited is None:
            logger.info(
                "Ripple delete range (%.3f, %.3f) made no timeline change; duration=%.3f",
                start,
                end,
                source_log.duration,
            )
            return False
        return self.apply_log_range_delete(request, edited)

    def prepare_log_range_delete(self, start, end):
        """Capture immutable state for a trim that may be built on a worker."""
        with self.lock:
            if (
                self._closed
                or self.loading
                or self.source != "log"
                or self.log is None
                or end <= start
            ):
                logger.info(
                    "Ripple delete rejected: closed=%s loading=%s source=%s "
                    "has_log=%s range=(%r, %r)",
                    self._closed,
                    self.loading,
                    self.source,
                    self.log is not None,
                    start,
                    end,
                )
                return None
            start = max(0.0, float(start))
            end = min(float(end), self.log.duration)
            if end <= start:
                logger.info(
                    "Ripple delete rejected after clamping: range=(%.3f, %.3f)",
                    start,
                    end,
                )
                return None
            return self.log, start, end, self._generation

    def apply_log_range_delete(self, request, edited):
        """Commit a worker-built trim only if its source log is still active."""
        source_log, start, end, generation = request
        if edited is None:
            return False
        with self.lock:
            if (
                self._closed
                or self.loading
                or self.source != "log"
                or self.log is not source_log
                or self._generation != generation
            ):
                logger.info("Discarded stale ripple delete result")
                return False
            previous_count = len(source_log.entries)
            self._log_edit_undo.append(self.log)
            if len(self._log_edit_undo) > 50:
                del self._log_edit_undo[0]
            self.log = edited
            self.playing = False
            self.cancel_seek()
            self.position = 0.0
            self.cursor = 0
            self.overview = True
            self.replay_history.clear()
            self.replay_history.samples.extend(edited.samples)
            self.replay_history.revision += 1
            self.dispatch("reset", None)
            self.dispatch("changed", None)
            logger.info(
                "Ripple deleted (%.3f, %.3f): entries %d -> %d, duration %.3f",
                start,
                end,
                previous_count,
                len(edited.entries),
                edited.duration,
            )
            return True

    def undo_log_edit(self):
        """Restore the previous immutable log snapshot, if it belongs to this load."""
        with self.lock:
            if self._closed or self.loading or not self._log_edit_undo:
                return False
            self.log = self._log_edit_undo.pop()
            self.playing = False
            self.cancel_seek()
            self.position = 0.0
            self.cursor = 0
            self.overview = True
            self.replay_history.clear()
            self.replay_history.samples.extend(self.log.samples)
            self.replay_history.revision += 1
            self.dispatch("reset", None)
            self.dispatch("changed", None)
            return True

    def restart(self):
        with self.lock:
            self.cancel_seek()
            self.playing = False
            self.position = 0.0
            self.cursor = 0
            self.overview = False
            self.replay_history.clear()
            self.dispatch("reset", None)
            self.dispatch("changed", None)

    def set_playing(self, active):
        with self.lock:
            if (
                active
                and self.log
                and self.log.entries
                and self.source == "log"
                and not self.loading
            ):
                if (
                    not self.seeking
                    and self.preview_position is None
                    and (self.overview or self.cursor == len(self.log.entries))
                ):
                    self.restart()
                self.playing = True
            else:
                self.playing = False
            self.dispatch("changed", None)

    def advance(self, elapsed):
        with self.lock:
            if (
                not self.playing
                or self.seeking
                or self.preview_position is not None
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

    def preview(self, position):
        """Move the displayed log position without consuming replay packets."""
        with self.lock:
            if (
                self._closed
                or self.loading
                or self.source != "log"
                or not self.log
                or not self.log.entries
            ):
                return False
            self.preview_position = max(0.0, min(float(position), self.log.duration))
            self.dispatch("changed", None)
            return True

    @property
    def display_position(self):
        return self.position if self.preview_position is None else self.preview_position

    def cancel_seek(self):
        """Invalidate pending UI work without resuming an interrupted replay."""
        with self.lock:
            self.preview_position = None
            self._seek_target = None
            self.seeking = False

    def begin_seek(self, position):
        """Reset all replay consumers once, then let the caller schedule batches.

        Always replay the prefix, even for forward seeks: overview samples and
        stateful instrument/wind/AIS consumers must agree with ordinary playback.
        Keep the user's playing intent, but advance() is suspended until done.
        """
        with self.lock:
            if not self.preview(position):
                return False
            self._seek_target = self.preview_position
            self.seeking = True
            self.cursor = 0
            self.position = 0.0
            self.overview = False
            self.replay_history.clear()
            self.dispatch("reset", None)
            self.dispatch("changed", None)
            return True

    def seek_batch(self, limit=1000):
        """Replay a bounded seek batch; return true when the seek is complete."""
        with self.lock:
            target = self._seek_target
            if not self.seeking or target is None:
                return True
            if limit <= 0:
                raise ValueError("Seek batch limit must be positive")
            end = self.cursor
            while (
                end < len(self.log.entries)
                and self.log.entries[end][0] <= target
                and end - self.cursor < limit
            ):
                end += 1
            packets = [packet for _, packet in self.log.entries[self.cursor : end]]
            if packets:
                start = self.cursor
                self.cursor = end
                self.position = self.log.entries[end - 1][0]
                self._deliver(packets, replay=True, start=start)
            if end < len(self.log.entries) and self.log.entries[end][0] <= target:
                # Do not rebuild plots/history widgets for every prefix batch.
                return False
            self.position = target
            self.preview_position = None
            self._seek_target = None
            self.seeking = False
            if self.cursor == len(self.log.entries):
                self.playing = False
            self.dispatch("changed", None)
            return True

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
            self.cancel_seek()
            self.stop_recording()
            self.playing = False
            self.streaming = False
            self._generation += 1
            self.connections.disconnect("data", self._live_data)
            self.connections.disconnect("nmea2000", self._record_raw)
