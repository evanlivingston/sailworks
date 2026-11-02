"""Limited EBL instrument replay, never a claim of full gateway conversion."""

import os
from pathlib import Path
from types import MappingProxyType
from collections.abc import Sequence

import nmeatoolkit as nt

from .ebl import EBLDecoder
from .nmea2000 import instruments
from .n2kdevices import DeviceCollector
from .nmeasession import NMEALog, NMEATrack, NumericFieldCollector, _track_sentence
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
    single_frame = {
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
