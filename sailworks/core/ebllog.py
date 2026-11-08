"""Limited EBL instrument replay, never a claim of full gateway conversion."""

import os
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from collections.abc import Sequence

import nmeatoolkit as nt

from .ebl import EBLDecoder
from .nmea2000 import instruments
from .n2kdevices import DeviceCollector
from .nmeasession import (
    NMEALog,
    NMEATrack,
    NumericFieldCollector,
    _track_sentence,
    slice_log,
)
from .polarperformance import PerformanceHistory

LIMITATION = (
    "Partial EBL support: raw BST-95 CAN frames, not NMEA 0183 sentences. "
    "Only supported single-frame instrument PGNs are replayed; fast-packet "
    "instrument reassembly and other EBL record variants are unsupported. "
    "Product information is assembled only for captured device metadata. "
    "UTC times come from EBL TimeUTC tags; device counters do not refine them. "
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
        127250,
        128259,
        129026,
        129025,
        130306,
        128267,
        130310,
        130312,
        130316,
    }
)


class _SessionState:
    """Replay state for one continuous recording burst."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.entries = []
        self.source_points = {}
        self.source_offsets = {}
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

    def add(self, message, index, has_time):
        self.frames += 1
        self.timed |= has_time
        if has_time:
            if self.origin is None:
                self.origin = message.timestamp
            self.elapsed = max(
                self.elapsed, (message.timestamp - self.origin).total_seconds()
            )
        else:
            self.elapsed = max(self.elapsed, (self.frames - 1) / 10.0)
        packets = instruments(message) if message.pgn in SINGLE_FRAME_PGNS else []
        if not packets:
            self.unsupported += 1
        for packet in packets:
            packet.log_index = index
            packet.source = message.source
            self.history.ingest(packet.data, now=index)
            _track_sentence(self.translator, self.wind, packet)
            self.numeric_fields.add(self.elapsed, packet)
            for point in self.translator.track:
                fix = (message.source, message.timestamp, message.data[:8])
                if fix not in self.seen_fixes:
                    self.seen_fixes.add(fix)
                    self.source_points.setdefault(message.source, []).append(point)
                    self.source_offsets.setdefault(message.source, []).append(
                        self.elapsed
                    )
            self.translator.track.clear()
            self.entries.append((self.elapsed, packet))

    def finish(self, path, decoder, devices, final):
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
        if final and decoder.buffer:
            warnings.append("Incomplete EBL record at end of file.")
        warnings.extend(dict.fromkeys(decoder.diagnostics))
        warnings.extend(devices.diagnostics())
        tracks = {
            source: NMEATrack(tuple(points), tuple(self.source_offsets[source]))
            for source, points in sorted(self.source_points.items())
        }
        default_source = max(
            tracks, key=lambda source: len(tracks[source].points), default=None
        )
        default_track = (
            tracks[default_source] if default_source is not None else NMEATrack()
        )
        return NMEALog(
            str(path),
            tuple(self.entries),
            self.unsupported,
            self.timed,
            self.history.snapshot()[1],
            default_track.points,
            default_track.point_offsets,
            tuple(warnings),
            "EBL (partial)",
            MappingProxyType(tracks),
            default_source,
            devices.devices(),
            True,
            self.frames,
            timestamp_origin=self.origin,
            numeric_series=self.numeric_fields.snapshot(),
        )


def iter_ebl_sessions(
    path,
    progress=None,
    gap_seconds=None,
    max_frames=None,
    max_entries=None,
):
    """Yield one :class:`NMEALog` per recording burst, streaming the input.

    Unlike :func:`read_ebl_log`, replay state is released at every session
    boundary, so memory follows the largest continuous burst rather than the
    whole folder. A burst that reaches the frame or entry budget is finalized
    and continued as the next session, keeping memory bounded.
    """
    if gap_seconds is None:
        gap_seconds = EBL_SESSION_GAP_SECONDS
    if max_frames is None:
        max_frames = MAX_SESSION_FRAMES
    if max_entries is None:
        max_entries = MAX_SESSION_ENTRIES
    paths = (
        tuple(Path(part) for part in path)
        if isinstance(path, Sequence) and not isinstance(path, (str, bytes, Path))
        else (Path(path),)
    )
    total = sum(part.stat().st_size for part in paths)
    consumed = 0
    decoder = EBLDecoder()
    devices = DeviceCollector()
    state = _SessionState()
    index = 0
    last_stamp = None
    if progress is not None:
        progress(0, total)
    for part in paths:
        with open(part, "rb") as stream:
            while chunk := stream.read(65536):
                for message in decoder.feed(chunk):
                    index += 1
                    has_time = message.timestamp.year != 1970
                    if (
                        has_time
                        and last_stamp is not None
                        and (message.timestamp - last_stamp).total_seconds()
                        > float(gap_seconds)
                    ):
                        log = state.finish(paths[0], decoder, devices, final=False)
                        if log is not None:
                            yield log
                        state.reset()
                        decoder.diagnostics.clear()
                        last_stamp = None
                    devices.observe(message)
                    state.add(message, index, has_time)
                    if has_time:
                        last_stamp = message.timestamp
                    if state.frames >= max_frames or len(state.entries) >= max_entries:
                        log = state.finish(paths[0], decoder, devices, final=False)
                        if log is not None:
                            yield log
                        state.reset()
                        decoder.diagnostics.clear()
                        last_stamp = None
                if len(decoder.diagnostics) > 100:
                    decoder.diagnostics[:] = decoder.diagnostics[:100]
                consumed += len(chunk)
                if progress is not None and consumed < total:
                    progress(consumed, total)
    log = state.finish(paths[0], decoder, devices, final=True)
    if log is not None:
        yield log
    if progress is not None:
        progress(total, total)


def read_ebl_log(path, progress=None):
    """Decode in chunks; optional progress receives decoded bytes and size."""
    paths = (
        tuple(Path(part) for part in path)
        if isinstance(path, Sequence) and not isinstance(path, (str, bytes, Path))
        else (Path(path),)
    )
    total = sum(part.stat().st_size for part in paths)
    consumed = 0
    if progress is not None:
        progress(0, total)
    decoder = EBLDecoder()
    devices = DeviceCollector()
    entries = []
    source_points, source_offsets, seen_fixes = {}, {}, set()
    history = PerformanceHistory(max_age=50, interval=0)
    numeric_fields = NumericFieldCollector()
    translator, wind = nt.TrackPointTranslator(), nt.TrueWindPipe()
    first = None
    elapsed = 0.0
    unsupported = 0
    timed = False
    frames = 0
    # Explicit allowlist excludes fast-packet PGNs even if an accidental
    # fragment could otherwise resemble a supported instrument field.
    single_frame = SINGLE_FRAME_PGNS
    for part in paths:
        with open(part, "rb") as stream:
            while chunk := stream.read(65536):
                for message in decoder.feed(chunk):
                    frames += 1
                    if frames > MAX_RAW_FRAMES:
                        raise OSError(
                            f"EBL raw CAN frame limit exceeded ({MAX_RAW_FRAMES:,}); "
                            f"read {frames:,} raw frames and retained "
                            f"{len(entries):,} supported replay entries"
                        )
                    devices.observe(message)
                    has_time = message.timestamp.year != 1970
                    timed |= has_time
                    if has_time:
                        if first is None:
                            first = message.timestamp
                        elapsed = max(
                            elapsed, (message.timestamp - first).total_seconds()
                        )
                    else:
                        elapsed = max(elapsed, (frames - 1) / 10.0)
                    packets = (
                        instruments(message) if message.pgn in single_frame else []
                    )
                    if not packets:
                        unsupported += 1
                    for packet in packets:
                        if len(entries) >= MAX_REPLAY_ENTRIES:
                            raise OSError(
                                "EBL supported replay entry limit exceeded "
                                f"({MAX_REPLAY_ENTRIES:,}); retained {len(entries):,} "
                                f"entries from {frames:,} raw CAN frames"
                            )
                        packet.log_index = frames
                        packet.source = message.source
                        history.ingest(packet.data, now=frames)
                        _track_sentence(translator, wind, packet)
                        numeric_fields.add(elapsed, packet)
                        # Keep the shared instrument state, but never interleave
                        # position sources into a single visualization track.
                        for point in translator.track:
                            fix = (message.source, message.timestamp, message.data[:8])
                            if fix not in seen_fixes:
                                seen_fixes.add(fix)
                                source_points.setdefault(message.source, []).append(
                                    point
                                )
                                source_offsets.setdefault(message.source, []).append(
                                    elapsed
                                )
                        translator.track.clear()
                        entries.append((elapsed, packet))
                # Keep diagnostics bounded on large malformed archives.
                if len(decoder.diagnostics) > 100:
                    decoder.diagnostics[:] = decoder.diagnostics[:100]
                consumed += len(chunk)
                if progress is not None and consumed < total:
                    progress(consumed, total)
    warnings = [LIMITATION]
    if decoder.version is None:
        warnings.append("Missing or invalid EBL Version metatag.")
    if not timed:
        warnings.append("No usable EBL timestamps; replay uses ten frames/second.")
    if unsupported:
        warnings.append(f"{unsupported:,} CAN frames had no supported instrument data.")
    if decoder.buffer:
        warnings.append("Incomplete EBL record at end of file.")
    warnings.extend(dict.fromkeys(decoder.diagnostics))
    warnings.extend(devices.diagnostics())
    if not entries:
        warnings.append(
            "No replayable instrument data; archive only, playback unavailable."
        )
    tracks = {
        source: NMEATrack(tuple(points), tuple(source_offsets[source]))
        for source, points in sorted(source_points.items())
    }
    # Deterministic tie-break: the lowest source address wins equal counts.
    default_source = max(
        tracks, key=lambda source: len(tracks[source].points), default=None
    )
    default_track = (
        tracks[default_source] if default_source is not None else NMEATrack()
    )
    log = NMEALog(
        str(paths[0]) if len(paths) == 1 else str(paths[0]),
        tuple(entries),
        unsupported,
        timed,
        history.snapshot()[1],
        default_track.points,
        default_track.point_offsets,
        tuple(warnings),
        "EBL (partial)",
        MappingProxyType(tracks),
        default_source,
        devices.devices(),
        True,
        frames,
        timestamp_origin=first,
        numeric_series=numeric_fields.snapshot(),
    )
    if progress is not None:
        progress(total, total)
    return log


def write_decoded_log(log, path):
    """Write decoded NMEA sentences to a text log file."""
    with open(path, "w", encoding="utf-8") as stream:
        for elapsed, packet in log.entries:
            stream.write(packet.serialize() + "\n")


def write_edited_log(log, path):
    """Write edited replay offsets separately from the original NMEA sentences."""
    with open(path, "w", encoding="utf-8") as stream:
        for elapsed, packet in log.entries:
            source = getattr(packet, "source", None)
            source_text = "-" if source is None else str(source)
            stream.write(f"@+{elapsed:.9f}\t{source_text}\t{packet.serialize()}\n")
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
