"""Replay regression tests using the published Actisense EBL/BST-95 layout."""

from datetime import datetime, timezone
from collections import Counter

import pytest

from sailworks.core import ebllog
from sailworks.core.ebl import EBLDecoder
from sailworks.core.nmeasession import NMEASession, read_nmea_log
from sailworks.core.utils import EventDispatcher


def timestamp(seconds):
    ticks = (seconds + 11644473600) * 10000000
    return metatag(3, ticks.to_bytes(8, "little"))


def metatag(identifier, data):
    payload = bytes([identifier]) + data
    return b"\x1b\x01" + payload.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\x0a"


VERSION = b"\x1b\x01\x01\xea\x03\x00\x00\x1b\x0a"


def frame(pgn, payload, source=42):
    return metatag(
        7,
        bytes(
            [
                0x95,
                6 + len(payload),
                0,
                0,
                source,
                pgn & 0xFF,
                (pgn >> 8) & 0xFF,
                (3 << 2) | ((pgn >> 16) & 3),
            ]
        )
        + payload,
    )


def test_raw_frame_budget_accepts_stitched_log_size():
    # Counts measured from the user's stitched archive; raw CAN work is not
    # the same budget as retained, supported sentences.
    assert ebllog.MAX_RAW_FRAMES == 10_000_000
    assert ebllog.MAX_RAW_FRAMES > 4_878_604
    assert ebllog.MAX_REPLAY_ENTRIES == 1_000_000
    assert 732_919 < ebllog.MAX_REPLAY_ENTRIES


def test_iter_ebl_sessions_streams_one_log_per_real_gap(tmp_path):
    speed = b"\x00" + (400).to_bytes(2, "little") + b"\xff" * 5
    raw = (
        timestamp(1704153600)
        + VERSION
        + frame(128259, speed) * 3
        + timestamp(1704153610)
        + frame(128259, speed) * 2
        + timestamp(1704157300)
        + frame(128259, speed) * 3
        + timestamp(1704157310)
        + frame(128259, speed)
    )
    path = tmp_path / "gapped.ebl"
    path.write_bytes(raw)

    sessions = list(ebllog.iter_ebl_sessions(path))
    assert len(sessions) == 2
    assert [len(session.entries) for session in sessions] == [5, 4]
    assert sessions[0].duration == 10.0
    assert sessions[1].duration == 10.0
    assert [session.frame_count for session in sessions] == [5, 4]

    expected = ebllog.split_ebl_log(read_nmea_log(path))
    assert [len(session.entries) for session in expected] == [5, 4]


def test_iter_ebl_sessions_splits_at_the_entry_budget(tmp_path):
    speed = b"\x00" + (400).to_bytes(2, "little") + b"\xff" * 5
    raw = timestamp(1704153600) + VERSION + frame(128259, speed) * 5
    path = tmp_path / "budget.ebl"
    path.write_bytes(raw)

    sessions = list(ebllog.iter_ebl_sessions(path, max_entries=2))
    assert [len(session.entries) for session in sessions] == [2, 2, 1]


def test_unsupported_frames_do_not_consume_replay_entry_budget(tmp_path, monkeypatch):
    # Scale down both budgets rather than decoding a million frames in a unit
    # test: exceed the old shared ceiling, reaching the new raw ceiling exactly.
    monkeypatch.setattr(ebllog, "MAX_RAW_FRAMES", 10)
    monkeypatch.setattr(ebllog, "MAX_REPLAY_ENTRIES", 1)
    path = tmp_path / "mostly-unsupported.ebl"
    speed = b"\x00" + (400).to_bytes(2, "little") + b"\xff" * 5
    raw = VERSION + frame(130000, b"\x00" * 8) * 9 + frame(128259, speed)
    path.write_bytes(raw)
    progress = []
    log = read_nmea_log(path, progress=lambda *args: progress.append(args))
    assert log.frame_count == 10
    assert log.skipped == 9
    assert len(log.entries) == 1
    assert log.entries[0][1].log_index == 10
    assert progress[-1] == (len(raw), len(raw))
    assert path.read_bytes() == raw


def test_raw_frame_limit_is_separate_and_inclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(ebllog, "MAX_RAW_FRAMES", 2)
    monkeypatch.setattr(ebllog, "MAX_REPLAY_ENTRIES", 1)
    path = tmp_path / "raw-limit.ebl"
    unsupported = frame(130000, b"\x00" * 8)
    path.write_bytes(VERSION + unsupported * 2)
    assert read_nmea_log(path).frame_count == 2
    path.write_bytes(VERSION + unsupported * 3)
    with pytest.raises(OSError, match="raw CAN frame limit exceeded \\(2\\)") as error:
        read_nmea_log(path)
    assert "3 raw frames" in str(error.value)
    assert "0 supported replay entries" in str(error.value)


def test_supported_entry_limit_still_guards_retained_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(ebllog, "MAX_RAW_FRAMES", 10)
    monkeypatch.setattr(ebllog, "MAX_REPLAY_ENTRIES", 2)
    path = tmp_path / "entry-limit.ebl"
    speed = frame(128259, b"\x00" + (400).to_bytes(2, "little") + b"\xff" * 5)
    path.write_bytes(VERSION + speed * 2)
    assert len(read_nmea_log(path).entries) == 2
    path.write_bytes(VERSION + speed * 3)
    with pytest.raises(
        OSError, match="supported replay entry limit exceeded \\(2\\)"
    ) as error:
        read_nmea_log(path)
    assert "retained 2 entries from 3 raw CAN frames" in str(error.value)


def test_entry_limit_is_checked_per_sentence_not_per_frame(tmp_path, monkeypatch):
    monkeypatch.setattr(ebllog, "MAX_RAW_FRAMES", 10)
    monkeypatch.setattr(ebllog, "MAX_REPLAY_ENTRIES", 1)
    path = tmp_path / "position-limit.ebl"
    fix = (481173000).to_bytes(4, "little", signed=True)
    fix += (115166667).to_bytes(4, "little", signed=True)
    path.write_bytes(VERSION + timestamp(1704153600) + frame(129025, fix))
    with pytest.raises(OSError, match="supported replay entry limit exceeded") as error:
        read_nmea_log(path)
    assert "retained 1 entries from 1 raw CAN frames" in str(error.value)


def test_supported_single_frame_instruments_use_ebl_timing_and_replay(tmp_path):
    path = tmp_path / "race.EBL"
    stamp = int(datetime(2024, 1, 2, tzinfo=timezone.utc).timestamp())
    # Speed through water, an eight-byte single-frame PGN.
    payload = b"\x00" + (400).to_bytes(2, "little") + b"\xff" * 5
    raw = timestamp(stamp) + VERSION + frame(128259, payload)
    raw += timestamp(stamp + 3) + frame(128259, payload)
    path.write_bytes(raw)
    log = read_nmea_log(path)
    assert log.timestamped
    assert log.duration == 3
    assert [packet.data.sentence_type for _, packet in log.entries] == ["VHW", "VHW"]
    assert any("Partial EBL" in message for message in log.diagnostics)
    assert path.read_bytes() == raw
    session = NMEASession(EventDispatcher(), tmp_path / "recordings")
    received = []
    session.connect("data", received.extend)
    try:
        session.finish_load(session.begin_load(), log)
        session.set_playing(True)
        session.advance(0)
        assert len(received) == 1
        session.advance(3)
        assert len(received) == 2
        assert not session.playing
    finally:
        session.close()


def test_fast_packet_and_unknown_data_are_archive_only_not_fabricated(tmp_path):
    path = tmp_path / "raw.ebl"
    path.write_bytes(VERSION + frame(129029, b"\x00" * 8) + frame(126996, b"\x00" * 8))
    log = read_nmea_log(path)
    assert log.entries == log.points == ()
    assert log.skipped == 2
    assert any("playback unavailable" in message for message in log.diagnostics)
    session = NMEASession(EventDispatcher(), tmp_path / "recordings")
    try:
        session.finish_load(session.begin_load(), log)
        session.set_playing(True)
        assert not session.playing
    finally:
        session.close()


def test_single_frame_position_preserves_utc_date_and_elapsed_time(tmp_path):
    path = tmp_path / "position.ebl"
    stamp = int(datetime(2025, 12, 31, 23, 59, 58, tzinfo=timezone.utc).timestamp())
    payload = (481173000).to_bytes(4, "little", signed=True)
    payload += (115166667).to_bytes(4, "little", signed=True)
    raw = timestamp(stamp) + VERSION + frame(129025, payload)
    raw += timestamp(stamp + 3) + frame(129025, payload)
    path.write_bytes(raw)
    log = read_nmea_log(path)
    assert log.timestamped
    assert log.duration == 3
    assert [offset for offset, _ in log.entries] == [0, 0, 3, 3]
    packets = [packet.data for _, packet in log.entries]
    assert [packet.sentence_type for packet in packets] == ["ZDA", "GLL"] * 2
    assert "ZDA,235958.000,31,12,2025" in str(packets[0])
    assert "ZDA,000001.000,1,1,2026" in str(packets[2])
    assert abs(float(packets[1].latitude) - 48.1173) < 1e-6
    assert abs(float(packets[1].longitude) - 11.5166667) < 1e-6
    assert not any("No usable EBL timestamps" in d for d in log.diagnostics)


def test_gps_sources_use_distinct_fixes_without_filtering_replay(tmp_path):
    path = tmp_path / "two-gps.ebl"
    stamp = 1704153600

    def position(source, lat):
        payload = lat.to_bytes(4, "little", signed=True)
        payload += (115166667).to_bytes(4, "little", signed=True)
        return frame(129025, payload, source=source)

    speed = b"\x00" + (400).to_bytes(2, "little") + b"\xff" * 5
    raw = VERSION + timestamp(stamp) + frame(128259, speed, source=12)
    raw += position(6, 481173000) * 2  # exact duplicate: visualization only
    raw += position(6, 481174000)  # same timestamp, different coordinates
    raw += position(43, 481183000) * 8  # more messages, fewer distinct fixes
    raw += timestamp(stamp + 1)
    raw += position(6, 481174000)  # same coordinates, different timestamp
    raw += position(43, 481184000)
    path.write_bytes(raw)
    log = read_nmea_log(path)

    assert set(log.source_tracks) == {6, 43}
    assert log.default_track_source == 6
    assert log.points == log.source_tracks[6].points
    assert log.point_offsets == log.source_tracks[6].point_offsets == (0, 0, 1)
    assert len(log.source_tracks[43].points) == 2
    assert log.source_tracks[43].point_offsets == (0, 1)
    assert [round(p.lat, 4) for p in log.points] == [48.1173, 48.1174, 48.1174]
    assert [round(p.lat, 4) for p in log.source_tracks[43].points] == [48.1183, 48.1184]
    # Position fixes are retained and deduplicated per source, while replay
    # continues to include every original message (including duplicate fixes).
    assert Counter(p.source for _, p in log.entries) == {6: 8, 43: 18, 12: 1}
    assert len(log.entries) == 27
    assert path.read_bytes() == raw

    session = NMEASession(EventDispatcher(), tmp_path / "recordings")
    received = []
    session.connect("data", received.extend)
    try:
        session.finish_load(session.begin_load(), log)
        session.set_playing(True)
        session.advance(log.duration)
        assert received == [packet for _, packet in log.entries]
    finally:
        session.close()


def test_gps_auto_tie_uses_lowest_source_address(tmp_path):
    path = tmp_path / "tie.ebl"
    payload = (481173000).to_bytes(4, "little", signed=True)
    payload += (115166667).to_bytes(4, "little", signed=True)
    path.write_bytes(
        VERSION
        + timestamp(1704153600)
        + frame(129025, payload, source=43)
        + frame(129025, payload, source=6)
    )
    log = read_nmea_log(path)
    assert log.default_track_source == 6
    assert len(log.source_tracks[6].points) == len(log.source_tracks[43].points) == 1


def test_late_product_metadata_names_gps_without_changing_tracks_or_replay(tmp_path):
    from sailworks.core.n2kdevices import source_label
    from test.test_n2kdevices import fragments

    path = tmp_path / "named.ebl"
    fix = (481173000).to_bytes(4, "little", signed=True)
    fix += (115166667).to_bytes(4, "little", signed=True)
    raw = VERSION + timestamp(1704153600) + frame(129025, fix, source=6)
    path.write_bytes(raw)
    baseline = read_nmea_log(path)
    raw += frame(60928, ((185 << 21) | 1).to_bytes(8, "little"), source=6)
    raw += b"".join(
        frame(part.pgn, part.data, source=part.source) for part in fragments()
    )
    raw += frame(130000, b"\x00" * 8, source=99)
    path.write_bytes(raw)
    log = read_nmea_log(path)
    assert log.device_metadata_available and log.frame_count == 23
    assert source_label(log.devices, 6) == "Captured GPS (Source 6)"
    assert source_label(log.devices, 99) == "Source 99"
    assert [device.source for device in log.devices] == [6, 99]
    assert log.default_track_source == baseline.default_track_source == 6
    assert log.source_tracks.keys() == baseline.source_tracks.keys()
    for source, track in log.source_tracks.items():
        previous = baseline.source_tracks[source]
        # Separate reads create distinct TrackPoints with identity equality.
        assert [vars(point) for point in track.points] == [
            vars(point) for point in previous.points
        ]
        assert track.point_offsets == previous.point_offsets
    assert [vars(point) for point in log.points] == [
        vars(point) for point in baseline.points
    ]
    assert log.point_offsets == baseline.point_offsets
    assert [(offset, str(packet.data)) for offset, packet in log.entries] == [
        (offset, str(packet.data)) for offset, packet in baseline.entries
    ]
    assert path.read_bytes() == raw


def test_truncated_and_unrecognised_ebl_produce_explicit_diagnostics(tmp_path):
    path = tmp_path / "broken.ebl"
    path.write_bytes(b"not an EBL record\x1b\x01\x07\x08\x00")
    log = read_nmea_log(path)
    assert not log.entries
    assert any("Incomplete" in message for message in log.diagnostics)
    assert any("outside" in message for message in log.diagnostics)


def test_frames_with_embedded_escape_and_pdu1_destination_decode_at_every_split():
    raw = VERSION + frame(0xEE23, b"\x1b\x01\x07\x08\x01\x02\x03\x04")
    for split in range(len(raw) + 1):
        decoder = EBLDecoder()
        messages = decoder.feed(raw[:split]) + decoder.feed(raw[split:])
        assert len(messages) == 1
        assert messages[0].pgn == 0xEE00
        assert messages[0].destination == 0x23
        assert messages[0].data.startswith(b"\x1b\x01\x07\x08")


def test_read_ebl_log_segment_boundary_matches_concatenated_reference(tmp_path):
    from sailworks.core.ebllog import read_ebl_log

    payload = b"\x00" + (400).to_bytes(2, "little") + b"\xff" * 5
    raw = timestamp(1704153600) + VERSION + frame(128259, payload, source=0x1B) * 3
    # Split inside a record (and escaped payload), not at a frame boundary.
    split = raw.index(b"\x1b\x1b") + 1
    first, second = tmp_path / "000031_001.ebl", tmp_path / "000031_002.ebl"
    first.write_bytes(raw[:split])
    second.write_bytes(raw[split:])
    segmented = read_ebl_log([first, second])
    reference = tmp_path / "reference.ebl"
    reference.write_bytes(raw)
    expected = read_ebl_log(reference)
    assert [(t, str(p.data)) for t, p in segmented.entries] == [
        (t, str(p.data)) for t, p in expected.entries
    ]
    assert segmented.diagnostics == expected.diagnostics


def test_ebl_decode_progress_is_chunked_without_changing_decoded_data(tmp_path):
    path = tmp_path / "large.ebl"
    payload = b"\x00" + (400).to_bytes(2, "little") + b"\xff" * 5
    path.write_bytes(timestamp(1704153600) + VERSION + frame(128259, payload) * 5000)
    updates = []
    baseline = read_nmea_log(path)
    log = read_nmea_log(path, progress=lambda *args: updates.append(args))
    total = path.stat().st_size
    assert updates == [(0, total), (65536, total), (total, total)]
    assert [(t, str(p.data)) for t, p in log.entries] == [
        (t, str(p.data)) for t, p in baseline.entries
    ]
    assert log.diagnostics == baseline.diagnostics
    assert log.point_offsets == baseline.point_offsets
    assert log.skipped == baseline.skipped


def test_text_decode_progress_preserves_newlines_replacement_and_timing(tmp_path):
    path = tmp_path / "large.log"
    sentence = b"$GPRMC,120000,A,4807.038,N,01131.000,E,7,0,010126,,"
    path.write_bytes((sentence + b"\r\n" + sentence + b"\r" + b"bad\xff\n") * 1500)
    baseline = read_nmea_log(path)
    updates = []
    log = read_nmea_log(path, progress=lambda *args: updates.append(args))
    total = path.stat().st_size
    assert updates[0] == (0, total)
    assert updates[-1] == (total, total)
    assert 2 < len(updates) <= total // 65536 + 2
    assert all(size == total for _, size in updates)
    assert all(a[0] <= b[0] for a, b in zip(updates, updates[1:]))
    assert [(t, str(p.data)) for t, p in log.entries] == [
        (t, str(p.data)) for t, p in baseline.entries
    ]
    assert log.skipped == baseline.skipped == 1500
    assert log.point_offsets == baseline.point_offsets
    assert log.timestamped == baseline.timestamped


# -- fast packets, direction, retention and read fidelity -----------------


def fast_frames(pgn, payload, source=42, sequence=3):
    frames = []
    total = len(payload)
    frames.append(
        frame(pgn, bytes([(sequence << 5), total]) + payload[:6], source=source)
    )
    index, position = 1, 6
    while position < total:
        chunk = payload[position : position + 7]
        frames.append(
            frame(
                pgn,
                bytes([(sequence << 5) | index]) + chunk,
                source=source,
            )
        )
        index += 1
        position += 7
    return frames


ENGINE = b"\x00" + (400).to_bytes(2, "little") + b"\x00" * 23


def test_fast_packet_reassembles_every_published_fast_pgn(tmp_path):
    path = tmp_path / "fast.ebl"
    frames = fast_frames(127489, ENGINE)
    path.write_bytes(timestamp(1704153600) + VERSION + b"".join(frames))
    log = read_nmea_log(path)
    assert [packet.data.sentence_type for _, packet in log.entries] == ["ENG"]
    assert log.frame_count == len(frames)
    assert log.skipped == 0
    assert not any("incomplete fast packet" in d for d in log.diagnostics)


def test_fast_packets_interleave_by_source_and_sequence(tmp_path):
    path = tmp_path / "interleaved.ebl"
    left = fast_frames(127489, ENGINE, source=6, sequence=1)
    right = fast_frames(127489, ENGINE, source=43, sequence=2)
    interleaved = [frame for pair in zip(left, right) for frame in pair]
    path.write_bytes(timestamp(1704153600) + VERSION + b"".join(interleaved))
    log = read_nmea_log(path)
    assert len(log.entries) == 2
    assert {packet.source for _, packet in log.entries} == {6, 43}


def test_fast_packet_reorder_duplicate_and_timeout_are_diagnosed(tmp_path):
    frames = fast_frames(127489, ENGINE)
    reordered = tmp_path / "reordered.ebl"
    reordered.write_bytes(
        timestamp(1704153600) + VERSION + frames[0] + b"".join(frames[2:])
    )
    log = read_nmea_log(reordered)
    assert not log.entries
    assert any("out-of-order fast-packet" in d for d in log.diagnostics)

    duplicate = tmp_path / "duplicate.ebl"
    duplicate.write_bytes(
        timestamp(1704153600) + VERSION + frames[0] + frames[1] + frames[1]
    )
    log = read_nmea_log(duplicate)
    assert not log.entries
    assert any("duplicate fast-packet" in d for d in log.diagnostics)

    timeout = tmp_path / "timeout.ebl"
    timeout.write_bytes(
        timestamp(1704153600)
        + VERSION
        + frames[0]
        + timestamp(1704153700)
        + b"".join(frames[1:])
    )
    log = read_nmea_log(timeout)
    assert not log.entries
    assert any("timed out incomplete fast packet" in d for d in log.diagnostics)


def test_fast_packet_crosses_stitched_segment_boundary(tmp_path):
    from sailworks.core.ebllog import read_ebl_log

    frames = fast_frames(127489, ENGINE)
    raw = timestamp(1704153600) + VERSION + b"".join(frames)
    first, second = tmp_path / "000031_001.ebl", tmp_path / "000031_002.ebl"
    split = raw.index(frames[1]) + 4
    first.write_bytes(raw[:split])
    second.write_bytes(raw[split:])
    log = read_ebl_log([first, second])
    assert [packet.data.sentence_type for _, packet in log.entries] == ["ENG"]


def test_transmitted_frames_are_counted_not_replayed(tmp_path):
    path = tmp_path / "tx.ebl"
    payload = b"\x00" + (400).to_bytes(2, "little") + b"\xff" * 5
    tx = (
        bytes(
            [
                0x95,
                6 + len(payload),
                0,
                0,
                42,
                128259 & 0xFF,
                (128259 >> 8) & 0xFF,
                0x80 | (2 << 2) | ((128259 >> 16) & 3),
            ]
        )
        + payload
    )
    raw = timestamp(1704153600) + VERSION
    raw += metatag(5, b"\x01") + metatag(7, tx)
    raw += metatag(5, b"\x00") + metatag(
        7,
        bytes(
            [
                0x95,
                6 + len(payload),
                0,
                0,
                42,
                128259 & 0xFF,
                (128259 >> 8) & 0xFF,
                (2 << 2) | ((128259 >> 16) & 3),
            ]
        )
        + payload,
    )
    path.write_bytes(raw)
    log = read_nmea_log(path)
    assert len(log.entries) == 1
    assert log.frame_count == 2
    assert any("1 transmitted frames excluded" in d for d in log.diagnostics)


def test_keep_ebl_originals_retains_and_can_be_disabled(tmp_path):
    from sailworks.core.loglibrary import LogLibrary

    directory = tmp_path / "EBL000031"
    directory.mkdir()
    payload = b"\x00" + (400).to_bytes(2, "little") + b"\xff" * 5
    path = directory / "000031_001.ebl"
    raw = timestamp(1704153600) + VERSION + frame(128259, payload)
    path.write_bytes(raw)

    keep = LogLibrary(tmp_path / "keep")
    (item,) = keep.import_paths([path])
    retained = item["file_info"]["ebl_originals"]
    assert len(retained) == 1
    managed = keep.originals / retained[0]["file"]
    assert managed.read_bytes() == raw
    assert retained[0]["source"] == str(path.resolve())

    drop = LogLibrary(tmp_path / "drop")
    drop.keep_ebl_originals = False
    (item,) = drop.import_paths([path])
    assert "ebl_originals" not in item["file_info"]
    assert not list(drop.originals.glob("*.ebl"))


def test_reopened_decoded_log_preserves_sources_and_duration(tmp_path):
    from sailworks.core.loglibrary import LogLibrary

    directory = tmp_path / "EBL000031"
    directory.mkdir()
    fix = (481173000).to_bytes(4, "little", signed=True)
    fix += (115166667).to_bytes(4, "little", signed=True)
    path = directory / "000031_001.ebl"
    raw = timestamp(1704153600) + VERSION + frame(129025, fix, source=6)
    raw += timestamp(1704153700) + frame(129025, fix, source=43)
    path.write_bytes(raw)
    library = LogLibrary(tmp_path / "library")
    library.keep_ebl_originals = False
    (item,) = library.import_paths([path])
    reopened = read_nmea_log(library.path(item["id"]))
    assert reopened.duration == 100
    assert set(reopened.source_tracks) == {6, 43}


def test_w2k_present_accepts_a_library_retained_original(tmp_path):
    from sailworks.core.loglibrary import LogLibrary
    from sailworks.core.w2ksync import W2KSync
    from sailworks.core.w2kapi import W2KFile

    library = LogLibrary(tmp_path / "library")
    source = tmp_path / "staging" / "SN1" / "EBL000031" / "000031_001.ebl"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"retained")
    library.originals.mkdir(parents=True)
    target = library.originals / "retained.ebl"
    target.write_bytes(b"retained")
    library._save(
        {
            "id1": {
                "id": "id1",
                "file": target.name,
                "name": "retained.ebl",
                "vessel": "",
                "notes": "",
                "imported": "2026-01-01T00:00:00+00:00",
                "status": "ok",
                "diagnostics": [],
                "file_info": {
                    "ebl_originals": [
                        {
                            "file": target.name,
                            "source": str(source.resolve()),
                            "size": 8,
                        }
                    ]
                },
            }
        }
    )
    sync = W2KSync(None, library, staging=tmp_path / "staging")
    file = W2KFile("EBL000031", "000031_001.ebl", 8, 0)
    sync._manifest = {"version": 1, "devices": {"SN1": {"files": {file.key: 8}}}}
    sync._save_manifest()
    source.unlink()
    assert sync.present("SN1", file)
