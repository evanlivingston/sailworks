"""Replay regression tests using the published Actisense EBL/BST-95 layout."""

from datetime import datetime, timezone
from collections import Counter

import pytest

from gweatherrouting.core import ebllog
from gweatherrouting.core.ebl import EBLDecoder
from gweatherrouting.core.nmeasession import NMEASession, read_nmea_log
from gweatherrouting.core.utils import EventDispatcher


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
    from gweatherrouting.core.n2kdevices import source_label
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
    from gweatherrouting.core.ebllog import read_ebl_log

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
