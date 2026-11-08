"""Regression vectors use the published Actisense EBL and BST-95 layouts."""

from datetime import datetime, timedelta, timezone

import pytest

from sailworks.core.ebl import EBLDecoder, FILETIME_EPOCH


def tag(identifier, data):
    payload = bytes([identifier]) + data
    return b"\x1b\x01" + payload.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\x0a"


def header(stamp):
    delta = stamp - FILETIME_EPOCH
    ticks = ((delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds) * 10
    return tag(3, ticks.to_bytes(8, "little")) + tag(1, (1002).to_bytes(4, "little"))


def test_file_header_and_published_bst95_vector_at_every_chunk_boundary():
    stamp = datetime(2024, 1, 2, 12, 34, 56, 789012, tzinfo=timezone.utc)
    # SDK BST-95 example: 0x0e means 14 bytes AFTER its two-byte header.
    frame = bytes.fromhex("95 0e 01 20 30 02 f8 09 ff fc 37 0a 00 10 ff ff")
    raw = header(stamp) + tag(7, frame)
    for split in range(len(raw) + 1):
        decoder = EBLDecoder()
        messages = decoder.feed(raw[:split]) + decoder.feed(raw[split:])
        assert decoder.version == 1002
        assert decoder.records == 3
        assert decoder.diagnostics == []
        assert not decoder.buffer
        assert len(messages) == 1
        message = messages[0]
        assert (message.pgn, message.priority, message.source) == (129026, 2, 48)
        assert message.destination == 255
        assert message.data == frame[8:]
        assert message.timestamp == stamp
        assert message.source_timestamp_ms == 8193


def test_escaped_filetime_and_payload_decode_one_byte_at_a_time():
    # Force ESC within the 64-bit FILETIME, retaining its exact UTC date.
    ticks = (133485408000000000 & ~(0xFF << 8)) | (0x1B << 8)
    stamp = FILETIME_EPOCH + timedelta(microseconds=ticks // 10)
    frame = bytes.fromhex("95 0e 1b 01 1b 01 f8 0d 1b 01 1b 0a 10 10 00 ff")
    raw = tag(3, ticks.to_bytes(8, "little"))
    raw += tag(1, (1002).to_bytes(4, "little")) + tag(7, frame)
    decoder = EBLDecoder()
    messages = [message for byte in raw for message in decoder.feed(bytes([byte]))]
    assert len(messages) == 1
    assert messages[0].timestamp == stamp
    assert messages[0].data == frame[8:]
    assert messages[0].source == 0x1B
    assert decoder.diagnostics == []


@pytest.mark.parametrize("control,resolution_us", enumerate((1000, 100, 10, 1)))
def test_dppc_pdu1_data_pages_direction_and_device_clock(control, resolution_us):
    stamp = datetime(2026, 1, 2, tzinfo=timezone.utc)
    dppc = 0x80 | (control << 5) | (5 << 2) | 3
    frame = bytes([0x95, 6, 0xFF, 0xFF, 42, 0x23, 0xEE, dppc])
    decoder = EBLDecoder()
    message = decoder.feed(header(stamp) + tag(7, frame))[0]
    assert (message.pgn, message.destination, message.priority) == (0x3EE00, 0x23, 5)
    assert message.source_timestamp_ms == 65535 * resolution_us // 1000
    assert message.timestamp == stamp  # Device counter must not shift UTC.
    assert message.data == b""


def test_unknown_tags_and_corruption_resynchronise_to_following_frame():
    stamp = datetime(2024, 1, 2, tzinfo=timezone.utc)
    frame = bytes.fromhex("95 06 00 00 2a 01 f8 0d")
    raw = header(stamp) + tag(0x99, b"unknown\x1bdata")
    raw += tag(7, frame[:-1]) + tag(7, b"\x93\x00")
    raw += b"\x1b\x01\x07broken"  # Missing end; next start must resynchronise.
    raw += tag(7, frame)
    decoder = EBLDecoder()
    assert len(decoder.feed(raw)) == 1
    assert any("unsupported EBL record tag 0x99" in d for d in decoder.diagnostics)
    assert any("length does not match" in d for d in decoder.diagnostics)
    assert any("unsupported BST message" in d for d in decoder.diagnostics)
    assert any("malformed escape" in d for d in decoder.diagnostics)


@pytest.mark.parametrize("version", [None, 1001, 1003])
def test_missing_or_unsupported_version_cannot_emit_messages(version):
    decoder = EBLDecoder()
    raw = b"" if version is None else tag(1, version.to_bytes(4, "little"))
    raw += tag(7, bytes.fromhex("95 06 00 00 2a 01 f8 0d"))
    assert decoder.feed(raw) == []
    assert decoder.diagnostics


def test_invalid_timestamp_clears_previous_time_and_oversized_tag_is_bounded():
    decoder = EBLDecoder()
    decoder.feed(header(datetime(2024, 1, 2, tzinfo=timezone.utc)))
    decoder.feed(tag(3, b"\xff" * 8))
    assert decoder.timestamp is None
    decoder.feed(b"\x1b\x01\x02" + b"x" * 2000)
    assert len(decoder.buffer) < 1803
    assert any("invalid EBL timestamp" in d for d in decoder.diagnostics)
    assert any("oversized" in d for d in decoder.diagnostics)


def test_oversized_tag_limit_is_checked_before_consuming_boundary_byte():
    for size, oversized in ((1800, False), (1801, False), (1802, True)):
        decoder = EBLDecoder()
        decoder.feed(b"\x1b\x01" + b"x" * size)
        assert ("discarded oversized EBL metatag" in decoder.diagnostics) is oversized


def test_bulk_payload_scanning_preserves_escaped_and_malformed_recovery():
    frame = bytes.fromhex("95 06 00 00 2a 01 f8 0d")
    raw = tag(1, (1002).to_bytes(4, "little"))
    raw += b"\x1b\x01" + b"x" * 1700 + b"\x1b\x1b" + b"y" * 20 + b"\x1b\x00"
    raw += tag(7, frame)
    for split in range(len(raw) + 1):
        decoder = EBLDecoder()
        first = decoder.feed(raw[:split])
        second = decoder.feed(raw[split:])
        assert len(first + second) == 1
        assert decoder.records == 2
        assert decoder.diagnostics == ["malformed escape in EBL metatag"]


def test_escaped_raw_stream_cannot_fabricate_a_metatag():
    raw = b"raw\x1b\x1b\x01\x99ignored\x1b\x0a"
    for split in range(len(raw) + 1):
        decoder = EBLDecoder()
        assert decoder.feed(raw[:split]) + decoder.feed(raw[split:]) == []
        assert decoder.records == 0
        assert all("unsupported EBL record tag" not in d for d in decoder.diagnostics)


def test_serial_checksum_variant_is_checked_when_present():
    frame = bytes.fromhex("95 06 00 00 2a 01 f8 0d")
    checksum = (-sum(frame)) & 0xFF
    decoder = EBLDecoder()
    raw = header(datetime(2024, 1, 2, tzinfo=timezone.utc))
    raw += tag(7, frame + bytes([checksum]))
    raw += tag(7, frame + bytes([(checksum + 1) & 0xFF]))
    assert len(decoder.feed(raw)) == 1
    assert decoder.diagnostics == ["invalid BST-95 checksum"]


# -- full BST coverage ----------------------------------------------------


def _checksum(body):
    return bytes(body) + bytes([(-sum(body)) & 0xFF])


def bst93(pgn, payload, source=42, destination=255, priority=2, millis=1234):
    body = bytes(
        [
            0x93,
            len(payload) + 11,
            priority,
            pgn & 0xFF,
            (pgn >> 8) & 0xFF,
            (pgn >> 16) & 3,
            destination,
            source,
        ]
    )
    body += millis.to_bytes(4, "little") + bytes([len(payload)]) + payload
    return _checksum(body)


def bst94(pgn, payload, destination=42, priority=2):
    body = bytes(
        [
            0x94,
            len(payload) + 6,
            priority,
            pgn & 0xFF,
            (pgn >> 8) & 0xFF,
            (pgn >> 16) & 3,
            destination,
            len(payload),
        ]
    )
    return _checksum(body + payload)


def bst95(pgn, payload, source=42, direction=0):
    dppc = (direction << 7) | (2 << 2) | ((pgn >> 16) & 3)
    body = bytes(
        [0x95, 6 + len(payload), 0, 0, source, pgn & 0xFF, (pgn >> 8) & 0xFF, dppc]
    )
    return _checksum(body + payload)


def bst9d(sentence):
    text = sentence.encode("ascii")
    return _checksum(bytes([0x9D, len(text)]) + text)


def bst_d0(pgn, payload, source=42, destination=255, direction=0, priority=2):
    dpp = (priority << 2) | ((pgn >> 16) & 3)
    control = (direction << 3) | 0  # single packet, external source
    body = bytearray(
        [
            0xD0,
            0,
            0,
            destination,
            source,
            pgn & 0xFF,
            (pgn >> 8) & 0xFF,
            dpp,
            control,
            0,
            0,
            0,
            0,
        ]
    )
    body += payload
    total = len(body)
    body[1] = total & 0xFF
    body[2] = (total >> 8) & 0xFF
    return _checksum(bytes(body))


def bdtp(message):
    block = _checksum(message)
    escaped = block.replace(b"\x10", b"\x10\x10")
    return b"\x10\x02" + escaped + b"\x10\x03"


def raw_stream(data):
    return data.replace(b"\x1b", b"\x1b\x1b")


def decode_all(raw):
    decoder = EBLDecoder()
    return decoder.feed(raw), decoder


def test_bst93_reassembled_gateway_message_and_checksum():
    stamp = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
    message = bst93(129025, b"\x01\x02\x03\x04\x05\x06\x07\x08")
    messages, decoder = decode_all(header(stamp) + tag(7, message))
    assert decoder.diagnostics == []
    assert len(messages) == 1
    assert (messages[0].pgn, messages[0].source, messages[0].destination) == (
        129025,
        42,
        255,
    )
    assert messages[0].timestamp == stamp
    assert messages[0].data == b"\x01\x02\x03\x04\x05\x06\x07\x08"


def test_bst94_is_host_direction_and_bst9d_yields_nmea():
    stamp = datetime(2026, 2, 3, tzinfo=timezone.utc)
    raw = header(stamp) + tag(7, bst94(128259, b"\x00\x01\x02\x03"))
    messages, decoder = decode_all(raw)
    assert decoder.diagnostics == []
    assert messages[0].direction == "tx"
    sentence = "$GPRMC,120000,A,4807.038,N,01131.000,E,7,0,010126,,"
    raw = header(stamp) + tag(7, bst9d(sentence))
    messages, decoder = decode_all(raw)
    assert decoder.diagnostics == []
    assert messages[0].serialize().startswith("$GPRMC")


def test_bst_d0_single_and_fast_packet_flags():
    stamp = datetime(2026, 2, 3, tzinfo=timezone.utc)
    message = bst_d0(130312, b"\x00\x01\x02\x03\x04\x05\x06\x07", direction=1)
    messages, decoder = decode_all(header(stamp) + tag(7, message))
    assert decoder.diagnostics == []
    assert (messages[0].pgn, messages[0].source, messages[0].destination) == (
        130312,
        42,
        255,
    )
    assert messages[0].direction == "tx"
    assert messages[0].data == b"\x00\x01\x02\x03\x04\x05\x06\x07"


def test_classic_bdtp_capture_decodes_instead_of_archiving():
    stamp = datetime(2026, 2, 3, tzinfo=timezone.utc)
    wire = bdtp(bst93(128259, b"\x00" + (400).to_bytes(2, "little") + b"\xff" * 5))
    messages, decoder = decode_all(header(stamp) + raw_stream(wire))
    assert decoder.diagnostics == []
    assert [message.pgn for message in messages] == [128259]


def test_wire_trace_honours_direction_markers_and_raw_frames():
    stamp = datetime(2026, 2, 3, tzinfo=timezone.utc)
    raw = header(stamp)
    raw += tag(5, b"\x01") + raw_stream(bdtp(bst94(128259, b"\x00\x01")))
    raw += tag(5, b"\x00") + tag(7, bst93(129025, b"\x00" * 8))
    messages, decoder = decode_all(raw)
    assert decoder.diagnostics == []
    assert [message.direction for message in messages] == ["tx", "rx"]


def test_bst95_direction_bit_and_checksum_are_reported():
    stamp = datetime(2026, 2, 3, tzinfo=timezone.utc)
    message = bst95(128259, b"\x00" * 6, direction=1)
    messages, decoder = decode_all(header(stamp) + tag(7, message))
    assert decoder.diagnostics == []
    assert messages[0].direction == "tx"


def test_description_and_direction_metatags_are_captured():
    stamp = datetime(2026, 2, 3, tzinfo=timezone.utc)
    raw = header(stamp) + tag(2, "W2K-1 segment".encode("utf-8"))
    raw += tag(5, b"\x02")
    decoder = EBLDecoder()
    assert decoder.feed(raw) == []
    assert decoder.description == "W2K-1 segment"
    assert decoder.direction == "unknown"
    assert any("invalid EBL DirectionMarker" in d for d in decoder.diagnostics)


def test_vendored_canboat_fast_packet_table_matches_published_version():
    import json
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent
        / "sailworks"
        / "data"
        / "canboat-fast-packet-pgns.json"
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == "9.1.0"
    assert data["records"] == 418
    assert len(data["pgns"]) == len(set(data["pgns"])) == 181
    assert 127489 in data["pgns"] and 129029 in data["pgns"]
