"""Streaming EBL replay with full Actisense container coverage.

One decode engine feeds both the materialising :func:`read_ebl_log` and the
streaming :func:`iter_ebl_sessions`. Fast packets are reassembled for every
PGN that canboat marks ``Type: Fast``; only PGNs with a known instrument,
battery, navigation or AIS conversion become replay entries.
"""

import json
import os
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType

import nmeatoolkit as nt

from .ebl import EBLDecoder
from .fastpacket import FastPacketAssembler
from .n2kdevices import DeviceCollector
from .nmea2000 import N2KMessage, instruments
from .nmeasession import (
    NMEALog,
    NMEATrack,
    NumericFieldCollector,
    _track_sentence,
    slice_log,
)
from .polarperformance import PerformanceHistory

LIMITATION = (
    "Partial EBL support: Actisense BST-93/94/95/9D/D0 and BDTP raw streams "
    "are decoded, with strict fast-packet reassembly for published fast PGNs. "
    "Only PGNs with a known instrument, battery, navigation or AIS conversion "
    "are replayed; transmitted frames are counted and excluded. Product "
    "information is assembled only for captured device metadata. UTC times "
    "come from EBL TimeUTC tags; device counters do not refine them. "
    "The original file is preserved unchanged."
)

# Raw frames are streamed; only supported replay entries are retained. Keep
# their work and memory limits separate (most captured PGNs may be unsupported).
MAX_RAW_FRAMES = 10_000_000
MAX_REPLAY_ENTRIES = 1_000_000

# A continuous recording burst is bounded independently of the folder so the
# library importer can stream a large set of segments as separate sessions.
MAX_SESSION_FRAMES = 50_000_000
MAX_SESSION_ENTRIES = 1_000_000

EBL_SESSION_GAP_SECONDS = 3600.0

SINGLE_FRAME_PGNS = frozenset(
    {
        127245,
        127250,
        127488,
        128259,
        129026,
        129025,
        129283,
        127508,
        130306,
        128267,
        130310,
        130312,
        130316,
    }
)

FAST_REPLAY_PGNS = frozenset(
    {
        127489,
        127506,
        127507,
        127509,
        129029,
        129038,
        129039,
        129040,
        129041,
        129284,
        129285,
        129793,
        129794,
        129798,
        129809,
        129810,
    }
)

REPLAY_PGNS = SINGLE_FRAME_PGNS | FAST_REPLAY_PGNS

INVENTORY_PGNS = frozenset({60928, 126996})


def _load_fast_packets():
    path = (
        Path(__file__).resolve().parent.parent
        / "data"
        / "canboat-fast-packet-pgns.json"
    )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return frozenset(int(pgn) for pgn in data["pgns"])
    except (OSError, ValueError, KeyError):
        # The decoder still works without the table; fast packets simply stay
        # archive-only rather than being guessed from a hand-maintained list.
        return frozenset()


FAST_PACKET_PGNS = _load_fast_packets()


def _materialize(pgn):
    return pgn in REPLAY_PGNS or pgn in FAST_PACKET_PGNS or pgn in INVENTORY_PGNS


class _EntryBudgetExceeded(Exception):
    def __init__(self, retained):
        super().__init__(retained)
        self.retained = retained


def _paths(path):
    return (
        tuple(Path(part) for part in path)
        if isinstance(path, Sequence) and not isinstance(path, (str, bytes, Path))
        else (Path(path),)
    )


class _DecodeStream:
    """Decode an EBL group once, yielding materialised payloads in order."""

    def __init__(self, paths, progress=None):
        self.paths = paths
        self.progress = progress
        self.total = sum(part.stat().st_size for part in paths)
        self.consumed = 0
        self.decoder = EBLDecoder(materialize=_materialize)
        self.devices = DeviceCollector()
        self.assembler = FastPacketAssembler(FAST_PACKET_PGNS)

    def __iter__(self):
        if self.progress is not None:
            self.progress(0, self.total)
        for part in self.paths:
            with open(part, "rb") as stream:
                while chunk := stream.read(65536):
                    for item in self.decoder.feed(chunk):
                        yield item
                    if len(self.decoder.diagnostics) > 100:
                        self.decoder.diagnostics[:] = self.decoder.diagnostics[:100]
                    self.consumed += len(chunk)
                    if self.progress is not None and self.consumed < self.total:
                        self.progress(self.consumed, self.total)
        for item in self.decoder.finish():
            yield item
        self.assembler.finish()
        if self.progress is not None:
            self.progress(self.total, self.total)


def _timestamp(item):
    if isinstance(item, tuple):
        return item[0]
    stamp = getattr(item, "timestamp", None)
    if stamp is None:
        stamp = getattr(item, "recorded_at", None)
    return stamp


def _direction(item):
    if isinstance(item, tuple):
        return item[3]
    return getattr(item, "direction", "rx")


class _SessionState:
    """Replay state for one continuous recording burst."""

    def __init__(self, max_entries):
        self.max_entries = max_entries
        self.reset()

    def reset(self):
        self.entries = []
        self.source_points = {}
        self.source_offsets = {}
        self.plain_points = []
        self.plain_offsets = []
        self.seen_fixes = set()
        self.history = PerformanceHistory(max_age=50, interval=0)
        self.numeric_fields = NumericFieldCollector()
        self.translator = nt.TrackPointTranslator()
        self.wind = nt.TrueWindPipe()
        self.origin = None
        self.elapsed = 0.0
        self.unsupported = 0
        self.timed = False
        self.frames = 0
        self.tx_frames = 0

    def _timing(self, stamp):
        if stamp is not None and stamp.year != 1970:
            self.timed = True
            if self.origin is None:
                self.origin = stamp
            self.elapsed = max(self.elapsed, (stamp - self.origin).total_seconds())
            return True
        self.elapsed = max(self.elapsed, (self.frames - 1) / 10.0)
        return False

    def add(self, item, index, assembler, devices, overflow=False):
        self.frames += 1
        stamp = _timestamp(item)
        has_time = self._timing(stamp)
        if _direction(item) == "tx":
            self.tx_frames += 1
            return
        if isinstance(item, tuple):
            devices.observe_header(item[0], item[1], item[2])
            self.unsupported += 1
            return
        if isinstance(item, N2KMessage):
            devices.observe(item)
            message = (
                assembler.add(item) if getattr(item, "fragmented", False) else item
            )
            if message is None:
                return
            packets = instruments(message) if message.pgn in REPLAY_PGNS else []
            if not packets:
                self.unsupported += 1
                return
            for packet in packets:
                self._append(packet, index, message.source, message, overflow)
        else:
            self._append(item, index, getattr(item, "source", None), item, overflow)

    def _append(self, packet, index, source, item, overflow=False):
        if not overflow and len(self.entries) >= self.max_entries:
            raise _EntryBudgetExceeded(len(self.entries))
        packet.log_index = index
        if source is not None:
            packet.source = source
        self.history.ingest(packet.data, now=index)
        previous = len(self.translator.track)
        _track_sentence(self.translator, self.wind, packet)
        points = self.translator.track[previous:]
        if source is None:
            self.plain_points.extend(points)
            self.plain_offsets.extend([self.elapsed] * len(points))
        else:
            for point in points:
                fix = (source, _timestamp(item), self._fix(item))
                if fix not in self.seen_fixes:
                    self.seen_fixes.add(fix)
                    self.source_points.setdefault(source, []).append(point)
                    self.source_offsets.setdefault(source, []).append(self.elapsed)
        self.translator.track.clear()
        self.numeric_fields.add(self.elapsed, packet)
        self.entries.append((self.elapsed, packet))

    @staticmethod
    def _fix(item):
        data = getattr(item, "data", None)
        if isinstance(data, bytes):
            return data[:8]
        return getattr(item, "serialize", lambda: b"")()[:64]

    def finish(self, path, decoder, devices, assembler, final):
        self.unsupported += assembler.drain_unsupported()
        if not self.entries:
            return None
        warnings = [LIMITATION]
        if decoder.version is None:
            warnings.append("Missing or invalid EBL Version metatag.")
        if not self.timed:
            warnings.append("No usable EBL timestamps; replay uses ten frames/second.")
        if self.unsupported:
            warnings.append(
                f"{self.unsupported:,} CAN frames had no supported instrument data."
            )
        if self.tx_frames:
            warnings.append(
                f"{self.tx_frames:,} transmitted frames excluded from replay."
            )
        if final and decoder.buffer:
            warnings.append("Incomplete EBL record at end of file.")
        warnings.extend(dict.fromkeys(decoder.diagnostics))
        warnings.extend(assembler.diagnostics)
        assembler.diagnostics.clear()
        warnings.extend(devices.diagnostics())
        tracks = {
            source: NMEATrack(tuple(points), tuple(self.source_offsets[source]))
            for source, points in sorted(self.source_points.items())
        }
        default_source = max(
            tracks, key=lambda source: len(tracks[source].points), default=None
        )
        if default_source is not None:
            default_track = tracks[default_source]
            points, offsets = default_track.points, default_track.point_offsets
        else:
            points, offsets = tuple(self.plain_points), tuple(self.plain_offsets)
        return NMEALog(
            str(path),
            tuple(self.entries),
            self.unsupported,
            self.timed,
            self.history.snapshot()[1],
            points,
            offsets,
            tuple(warnings),
            "EBL (partial)",
            MappingProxyType(tracks),
            default_source,
            devices.devices(),
            True,
            self.frames,
            timestamp_origin=self.origin,
            numeric_series=self.numeric_fields.snapshot(),
            ebl_description=decoder.description,
        )


def iter_ebl_sessions(
    path,
    progress=None,
    gap_seconds=None,
    max_frames=None,
    max_entries=None,
):
    """Yield one :class:`NMEALog` per recording burst, streaming the input."""
    if gap_seconds is None:
        gap_seconds = EBL_SESSION_GAP_SECONDS
    if max_frames is None:
        max_frames = MAX_SESSION_FRAMES
    if max_entries is None:
        max_entries = MAX_SESSION_ENTRIES
    paths = _paths(path)
    stream = _DecodeStream(paths, progress)
    devices, assembler = stream.devices, stream.assembler
    state = _SessionState(max_entries)
    index = 0
    last_stamp = None
    for item in stream:
        index += 1
        stamp = _timestamp(item)
        has_time = stamp is not None and stamp.year != 1970
        if (
            has_time
            and last_stamp is not None
            and (stamp - last_stamp).total_seconds() > float(gap_seconds)
        ):
            log = state.finish(paths[0], stream.decoder, devices, assembler, False)
            if log is not None:
                yield log
            state.reset()
            stream.decoder.diagnostics.clear()
            assembler.diagnostics.clear()
            last_stamp = None
        while True:
            try:
                state.add(item, index, assembler, devices)
                break
            except _EntryBudgetExceeded:
                log = state.finish(paths[0], stream.decoder, devices, assembler, False)
                if log is not None:
                    yield log
                state.reset()
                stream.decoder.diagnostics.clear()
                assembler.diagnostics.clear()
                last_stamp = None
                # One item may produce more sentences than the budget; allow
                # that item to complete rather than looping forever.
                state.add(item, index, assembler, devices, overflow=True)
                break
        if has_time:
            last_stamp = stamp
        if state.frames >= max_frames:
            log = state.finish(paths[0], stream.decoder, devices, assembler, False)
            if log is not None:
                yield log
            state.reset()
            stream.decoder.diagnostics.clear()
            assembler.diagnostics.clear()
            last_stamp = None
    log = state.finish(paths[0], stream.decoder, devices, assembler, True)
    if log is not None:
        yield log


def read_ebl_log(path, progress=None):
    """Decode in chunks; optional progress receives decoded bytes and size."""
    paths = _paths(path)
    stream = _DecodeStream(paths, progress)
    state = _SessionState(MAX_REPLAY_ENTRIES)
    index = 0
    for item in stream:
        index += 1
        if state.frames + 1 > MAX_RAW_FRAMES:
            raise OSError(
                f"EBL raw CAN frame limit exceeded ({MAX_RAW_FRAMES:,}); "
                f"read {state.frames + 1:,} raw frames and retained "
                f"{len(state.entries):,} supported replay entries"
            )
        try:
            state.add(item, index, stream.assembler, stream.devices)
        except _EntryBudgetExceeded as error:
            raise OSError(
                "EBL supported replay entry limit exceeded "
                f"({MAX_REPLAY_ENTRIES:,}); retained {error.retained:,} "
                f"entries from {state.frames:,} raw CAN frames"
            ) from None
    log = state.finish(paths[0], stream.decoder, stream.devices, stream.assembler, True)
    if log is None:
        warnings = [LIMITATION]
        if stream.decoder.version is None:
            warnings.append("Missing or invalid EBL Version metatag.")
        if stream.decoder.timestamp is None:
            warnings.append("No usable EBL timestamps; replay uses ten frames/second.")
        if stream.decoder.buffer:
            warnings.append("Incomplete EBL record at end of file.")
        warnings.extend(dict.fromkeys(stream.decoder.diagnostics))
        warnings.extend(stream.assembler.diagnostics)
        warnings.extend(stream.devices.diagnostics())
        warnings.append(
            "No replayable instrument data; archive only, playback unavailable."
        )
        if state.unsupported:
            warnings.insert(
                1,
                f"{state.unsupported:,} CAN frames had no supported instrument data.",
            )
        return NMEALog(
            str(paths[0]),
            (),
            state.unsupported,
            False,
            (),
            (),
            (),
            tuple(warnings),
            "EBL (partial)",
            MappingProxyType({}),
            None,
            stream.devices.devices(),
            True,
            state.frames,
            ebl_description=stream.decoder.description,
        )
    return log


def write_decoded_log(log, path):
    """Write decoded replay with explicit elapsed and source prefixes."""
    with open(path, "w", encoding="utf-8") as stream:
        batch = []
        for elapsed, packet in log.entries:
            source = getattr(packet, "source", None)
            source_text = "-" if source is None else str(source)
            batch.append(f"@+{elapsed:.9f}\t{source_text}\t{packet.serialize()}\n")
            if len(batch) >= 4096:
                stream.write("".join(batch))
                batch.clear()
        if batch:
            stream.write("".join(batch))
        stream.flush()
        os.fsync(stream.fileno())


def write_edited_log(log, path):
    """Write edited replay offsets separately from the original NMEA sentences."""
    with open(path, "w", encoding="utf-8") as stream:
        batch = []
        for elapsed, packet in log.entries:
            source = getattr(packet, "source", None)
            source_text = "-" if source is None else str(source)
            batch.append(f"@+{elapsed:.9f}\t{source_text}\t{packet.serialize()}\n")
            if len(batch) >= 4096:
                stream.write("".join(batch))
                batch.clear()
        if batch:
            stream.write("".join(batch))
        stream.flush()
        os.fsync(stream.fileno())


def split_ebl_log(log, gap_seconds=EBL_SESSION_GAP_SECONDS):
    """Split a timed EBL log into one log per gap larger than ``gap_seconds``."""
    if not getattr(log, "timestamped", False) or not getattr(log, "entries", None):
        return [log]
    origin = getattr(log, "timestamp_origin", None)
    if origin is None or len(log.entries) < 2:
        return [log]
    sessions = []
    start = 0
    for index in range(1, len(log.entries)):
        previous = origin + timedelta(seconds=log.entries[index - 1][0])
        current = origin + timedelta(seconds=log.entries[index][0])
        if (current - previous).total_seconds() > float(gap_seconds):
            sessions.append((start, index))
            start = index
    sessions.append((start, len(log.entries)))
    if len(sessions) <= 1:
        return [log]
    result = []
    for start_index, end_index in sessions:
        start_elapsed = log.entries[start_index][0]
        end_elapsed = log.entries[end_index - 1][0]
        sliced = slice_log(log, start_elapsed, end_elapsed)
        if sliced is not None:
            result.append(sliced)
    return result or [log]
