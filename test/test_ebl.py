"""Regression vectors use the published Actisense EBL and BST-95 layouts."""

from datetime import datetime, timedelta, timezone

import pytest

from gweatherrouting.core.ebl import EBLDecoder, FILETIME_EPOCH


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
