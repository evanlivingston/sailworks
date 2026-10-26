"""Strict, bounded capture metadata, independent of replay translation."""

from datetime import datetime, timedelta, timezone
from dataclasses import asdict

import pytest

from gweatherrouting.core.n2kdevices import (
    DeviceCollector,
    captured_device_from_dict,
    source_label,
)
from gweatherrouting.core.nmea2000 import N2KMessage


STAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)


def message(pgn, data, source=6, destination=255, seconds=0):
    return N2KMessage(
        STAMP + timedelta(seconds=seconds), 6, pgn, destination, source, 0, data
    )


def claim(name, source=6):
    return message(60928, name.to_bytes(8, "little"), source)


def product_data(model="Captured GPS", code=1234):
    def string(value):
        return value.encode("ascii").ljust(32, b"\x00")

    return (
        (2100).to_bytes(2, "little")
        + code.to_bytes(2, "little")
        + b"".join(string(value) for value in (model, "2.4", "Rev B", "SERIAL-7"))
        + b"\x02\x03"
    )


def fragments(data=None, source=6, sequence=0, destination=255):
    data = product_data() if data is None else data
    header = sequence << 5
    payloads = [bytes([header, len(data)]) + data[:6]]
    payloads.extend(
        bytes([header | index]) + data[start : start + 7]
        for index, start in enumerate(range(6, len(data), 7), 1)
    )
    return [message(126996, payload, source, destination) for payload in payloads]


def observe(collector, messages):
    for item in messages:
        collector.observe(item)


def test_product_layout_name_and_numeric_manufacturer_are_captured():
    collector = DeviceCollector()
    name = (185 << 21) | 42 | (1 << 63)
    observe(collector, [claim(name), *fragments(sequence=7)])
    (device,) = collector.devices()
    assert device.name == name
    assert device.name_hex == f"0x{name:016X}"
    assert device.manufacturer == 185
    assert device.product.protocol_version == 2100
    assert device.product.product_code == 1234
    assert device.product.model == "Captured GPS"
    assert device.product.software == "2.4"
    assert device.product.model_version == "Rev B"
    assert device.product.serial == "SERIAL-7"
    assert device.product.certification == 2
    assert device.product.load_equivalency == 3
    assert device.frames == 21
    assert dict(device.pgn_counts) == {60928: 1, 126996: 20}
    assert collector.diagnostics() == ()
    assert source_label(collector.devices(), 6) == "Captured GPS (Source 6)"


def test_device_index_roundtrip_restores_nested_product_information():
    collector = DeviceCollector()
    observe(collector, [claim((185 << 21) | 1), *fragments()])
    (original,) = collector.devices()

    restored = captured_device_from_dict(asdict(original))

    assert restored == original
    assert source_label((restored,), restored.source) == "Captured GPS (Source 6)"


@pytest.mark.parametrize("missing", [0, 1, 10, 19])
def test_missing_fragment_never_fabricates_product(missing):
    collector = DeviceCollector()
    observe(
        collector, [part for index, part in enumerate(fragments()) if index != missing]
    )
    assert collector.devices()[0].product is None
    assert collector.diagnostics()


@pytest.mark.parametrize(
    "failure",
    [
        "out-of-order",
        "duplicate",
        "short-first",
        "short-middle",
        "short-last",
        "wrong-size",
    ],
)
def test_invalid_product_is_rejected(failure):
    collector = DeviceCollector()
    parts = fragments()
    if failure == "out-of-order":
        parts[1], parts[2] = parts[2], parts[1]
    elif failure == "duplicate":
        parts.insert(2, parts[1])
    elif failure == "wrong-size":
        parts[0] = message(126996, b"\x00\x85" + parts[0].data[2:])
    else:
        index = {"short-first": 0, "short-middle": 7, "short-last": 19}[failure]
        parts[index] = message(126996, parts[index].data[:-1])
    observe(collector, parts)
    assert collector.devices()[0].product is None
    assert source_label(collector.devices(), 6) == "Source 6"
    assert collector.diagnostics()


def test_interleaved_sources_sequences_and_destinations_do_not_mix():
    collector = DeviceCollector()
    streams = [
        fragments(source=6, sequence=1),
        fragments(source=43, sequence=1, data=product_data("Other GPS")),
        fragments(source=6, sequence=2),
        fragments(source=6, sequence=1, destination=42),
    ]
    for group in zip(*streams):
        observe(collector, group)
    assert [device.product.model for device in collector.devices()] == [
        "Captured GPS",
        "Other GPS",
    ]
    assert not collector.pending
    assert not collector.diagnostics()


def test_address_reuse_never_inherits_old_product_and_track_stays_ambiguous():
    collector = DeviceCollector()
    observe(collector, [claim(1), *fragments(), claim(1), claim(2)])
    old, new = collector.devices()
    assert old.name == 1 and old.product.model == "Captured GPS"
    assert new.name == 2 and new.product is None
    assert source_label(collector.devices(), 6) == "Source 6"
    observe(collector, fragments(product_data("New GPS")))
    assert collector.devices()[1].product.model == "New GPS"
    assert source_label(collector.devices(), 6) == "Source 6"


def test_identity_change_discards_inflight_product():
    collector = DeviceCollector()
    parts = fragments()
    observe(collector, [claim(1), *parts[:10], claim(2), *parts[10:]])
    assert all(device.product is None for device in collector.devices())
    assert collector.diagnostics()


def test_anonymous_product_cannot_be_assigned_to_later_name():
    collector = DeviceCollector()
    observe(collector, fragments())
    collector.observe(claim(2))
    old, new = collector.devices()
    assert old.name is None and old.product.model == "Captured GPS"
    assert new.name == 2 and new.product is None
    assert source_label(collector.devices(), 6) == "Source 6"


def test_late_identity_can_name_anonymous_fixes_without_changing_source():
    collector = DeviceCollector()
    collector.observe(message(129025, b"\x00" * 8))
    observe(collector, [claim(1), *fragments()])
    assert len(collector.devices()) == 1
    assert source_label(collector.devices(), 6) == "Captured GPS (Source 6)"
    assert source_label(collector.devices(), 43) == "Source 43"


def test_missing_model_and_reserved_numeric_fields_stay_unknown():
    collector = DeviceCollector()
    observe(collector, [claim(2047 << 21), *fragments(b"\xff" * 134)])
    (device,) = collector.devices()
    assert device.manufacturer is None
    assert device.product.product_code is None
    assert device.product.protocol_version is None
    assert device.product.model == ""
    assert source_label(collector.devices(), 6) == "Source 6"


def test_manufacturer_fallback_is_offline_and_model_remains_preferred():
    from gweatherrouting.core.n2kmanufacturers import manufacturer_name

    assert [manufacturer_name(code) for code in (229, 135, 358, 419)] == [
        "Garmin",
        "Airmar",
        "Victron Energy",
        "Fusion Electronics",
    ]
    assert manufacturer_name(2046) == "Unknown manufacturer (2046)"
    assert manufacturer_name(None) is None
    collector = DeviceCollector()
    observe(collector, [claim(229 << 21)])
    (device,) = collector.devices()
    assert device.product is None
    assert source_label(collector.devices(), 6) == "Garmin (Source 6)"
    observe(collector, fragments())
    assert source_label(collector.devices(), 6) == "Captured GPS (Source 6)"


def test_manufacturer_fallback_does_not_name_ambiguous_address():
    collector = DeviceCollector()
    observe(collector, [claim(229 << 21), claim((135 << 21) | 1)])
    assert source_label(collector.devices(), 6) == "Source 6"


@pytest.mark.parametrize(
    "code, expected",
    [
        (229, "Garmin (Source 6)"),
        (2000, "Unknown manufacturer (2000) (Source 6)"),
        (None, "Source 6"),
    ],
)
def test_manufacturer_fallback_with_empty_model_unknown_or_missing_code(code, expected):
    collector = DeviceCollector()
    if code is not None:
        collector.observe(claim(code << 21))
    observe(collector, fragments(product_data(model="")))
    assert collector.devices()[0].product.model == ""
    assert source_label(collector.devices(), 6) == expected


def test_identity_limited_device_never_uses_manufacturer_or_model():
    from dataclasses import replace

    collector = DeviceCollector()
    observe(collector, [claim(229 << 21), *fragments()])
    device = replace(collector.devices()[0], identity_limited=True)
    assert source_label((device,), 6) == "Source 6"


def test_conflicting_products_do_not_retain_previous_name():
    collector = DeviceCollector()
    observe(collector, [claim(1), *fragments(), *fragments(product_data("Different"))])
    first, second = collector.devices()
    assert first.name == 1
    assert second.name is None and second.product.model == "Different"
    assert second.first_frame == second.last_frame
    assert sum(device.frames for device in collector.devices()) == collector.frames
    assert source_label(collector.devices(), 6) == "Source 6"


def test_metadata_bounds_and_eof_diagnostics():
    collector = DeviceCollector()
    for source in range(100):
        collector.observe(fragments(source=source)[0])
    assert len(collector.pending) == collector.MAX_PENDING
    assert all(len(packet.data) <= 134 for packet in collector.pending.values())
    assert any("EOF" in diagnostic for diagnostic in collector.diagnostics())
    for name in range(100):
        collector.observe(claim(name))
    assert len(collector.epochs[6]) == collector.MAX_EPOCHS
    assert (
        len([device for device in collector.devices() if device.source == 6])
        == collector.MAX_EPOCHS
    )
    current = collector.epochs[6][-1]
    assert current.identity_limited and current.product is None and current.name is None
    observe(collector, fragments())
    assert source_label(collector.devices(), 6) == "Source 6"
    for pgn in range(1000):
        collector.observe(message(pgn, b""))
    assert len(current.pgn_counts) <= collector.MAX_PGNS
    assert current.other_pgn_frames > 0


@pytest.mark.parametrize("timeout", ["utc", "frames"])
def test_incomplete_assembly_expires(timeout):
    collector = DeviceCollector()
    parts = fragments()
    collector.observe(parts[0])
    if timeout == "utc":
        collector.observe(message(1, b"", seconds=6))
    else:
        observe(collector, [message(1, b"")] * (collector.MAX_FRAME_AGE + 1))
    observe(collector, parts[1:])
    assert collector.devices()[0].product is None
    assert any("expired" in diagnostic for diagnostic in collector.diagnostics())


def test_eof_and_restart_are_diagnosed_and_padding_is_not_product_data():
    collector = DeviceCollector()
    parts = fragments()
    collector.observe(parts[0])
    assert any("EOF" in diagnostic for diagnostic in collector.diagnostics())
    collector.observe(parts[0])
    observe(collector, parts[1:-1])
    collector.observe(message(126996, parts[-1].data.ljust(8, b"\xff")))
    assert collector.devices()[0].product.model == "Captured GPS"
    assert any("restarted" in diagnostic for diagnostic in collector.diagnostics())


@pytest.mark.parametrize("data", [b"", b"\x00" * 7, b"\xff" * 8])
def test_invalid_address_claim_is_not_an_invented_identity(data):
    collector = DeviceCollector()
    collector.observe(message(60928, data))
    (device,) = collector.devices()
    assert device.name is None and device.manufacturer is None
    assert device.frames == 1
    assert source_label(collector.devices(), 6) == "Source 6"
    assert any(
        "invalid address claim" in diagnostic for diagnostic in collector.diagnostics()
    )
