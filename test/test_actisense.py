"""Wire-level and recording tests independent of onboard hardware."""

import math
import struct
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from gweatherrouting.core.actisense import (
    ActisenseDecoder,
    frame,
)
from gweatherrouting.core.nmea2000 import (
    N2KMessage,
    instruments,
    number,
    N2KAsciiDecoder,
)
from gweatherrouting.core.actisensedatasource import ActisenseDataSource
from gweatherrouting.core.nmea2000datasource import NMEA2000DataSource
from gweatherrouting.core.connectionmanager import (
    ConnectionManager,
    create_data_source,
)
from gweatherrouting.core.instrumentrecording import InstrumentRecording

NOW = datetime(2026, 10, 12, 23, 59, 59, 123000, tzinfo=timezone.utc)


def message(pgn, data, source=35):
    return N2KMessage(NOW, 2, pgn, 255, source, 1234, data)


def wire(msg):
    payload = bytes((msg.priority,)) + msg.pgn.to_bytes(3, "little")
    payload += bytes((msg.destination, msg.source))
    payload += msg.source_timestamp_ms.to_bytes(4, "little")
    payload += bytes((len(msg.data),)) + msg.data
    return frame(0x93, payload)


def wind(reference=2, speed=10, degrees=45):
    return (
        struct.pack(
            "<BHHB",
            0,
            round(speed * 100),
            round(math.radians(degrees) * 10000),
            reference,
        )
        + b"\xff\xff"
    )


@pytest.mark.parametrize("split", range(28))
def test_fragmented_and_escaped_frames(split):
    msg = message(130306, b"\x10" * 8, source=16)
    encoded = wire(msg)
    decoder = ActisenseDecoder()
    assert decoder.feed(encoded[:split], NOW) + decoder.feed(encoded[split:], NOW) == [
        msg
    ]


def test_corruption_and_incomplete_frame_recover():
    msg = message(130306, wind())
    encoded = wire(msg)
    bad = bytearray(encoded)
    bad[-3] ^= 1
    decoder = ActisenseDecoder()
    assert decoder.feed(b"noise" + bad + b"\x10\x02broken" + encoded, NOW) == [msg]
    assert decoder.invalid_frames == 1
    decoder.feed(b"\x10\x02" + b"x" * 10000)
    assert len(decoder.buffer) <= 260
    assert decoder.feed(encoded, NOW) == [msg]


def test_fast_packet_and_legacy_bst_length():
    msg = message(129029, bytes(range(43)))
    encoded = wire(msg)
    assert ActisenseDecoder().feed(encoded, NOW) == [msg]
    payload = bytes((msg.priority,)) + msg.pgn.to_bytes(3, "little") + bytes((255, 35))
    payload += (1234).to_bytes(4, "little") + bytes((43,)) + msg.data
    body = bytes((0x93, 19)) + payload
    body += bytes((-sum(body) & 255,))
    encoded = b"\x10\x02" + body.replace(b"\x10", b"\x10\x10") + b"\x10\x03"
    assert ActisenseDecoder().feed(encoded, NOW) == [msg]


@pytest.mark.parametrize(
    "payload",
    [b"", b"\x00" * 10, b"\x00" * 10 + b"\x08", b"\x00" * 10 + b"\xe0" + b"\x00" * 224],
)
def test_invalid_payload_lengths(payload):
    assert ActisenseDecoder().feed(frame(0x93, payload), NOW) == []


@pytest.mark.parametrize("reference", range(8))
def test_wind_reference_and_units(reference):
    result = instruments(message(130306, wind(reference)))
    if reference in (2, 4):
        s = result[0].data
        assert s.reference == ("R" if reference == 2 else "T")
        assert float(s.wind_angle) == pytest.approx(45, abs=0.004)
        assert float(s.wind_speed) == pytest.approx(19.4384, abs=0.0001)
    else:
        assert result == []


@pytest.mark.parametrize(
    "signed,size", [(False, 2), (True, 2), (False, 4), (True, 4), (True, 8)]
)
def test_unavailable_values(signed, size):
    maximum = (1 << (size * 8 - int(signed))) - 1
    for raw in range(maximum - 2, maximum + 1):
        assert (
            number(raw.to_bytes(size, "little", signed=signed), 0, size, signed=signed)
            is None
        )
    assert number(b"", 0, size) is None


def test_unavailable_wind_and_short_messages():
    assert instruments(message(130306, b"\x00" + b"\xff" * 7)) == []
    for pgn in [
        127250,
        128259,
        129026,
        129025,
        129029,
        130306,
        128267,
        130310,
        130312,
        130316,
    ]:
        assert instruments(message(pgn, b"")) == []


def test_heading_variation_and_true_heading():
    d = struct.pack("<BHhhB", 0, round(math.pi * 10000), 32767, round(-0.1 * 10000), 1)
    s = instruments(message(127250, d))[0].data
    assert s.sentence_type == "HDG"
    assert s.deviation is None
    assert s.var_dir == "W"
    assert float(s.variation) == pytest.approx(math.degrees(0.1), abs=0.0001)
    assert instruments(message(127250, d[:-1] + b"\x00"))[0].data.sentence_type == "HDT"
    assert instruments(message(127250, d[:-1] + b"\x03")) == []


def test_speed_course_depth_temperature_zero_and_units():
    s = instruments(
        message(128259, struct.pack("<BHHBBB", 0, 300, 65535, 0, 255, 255))
    )[0].data
    assert float(s.water_speed_knots) == pytest.approx(5.8315, abs=0.0001)
    d = struct.pack("<BBHHH", 0, 0, 0, 0, 65535)
    s = instruments(message(129026, d))[0].data
    assert s.true_track == 0
    assert s.spd_over_grnd_kts == 0
    d = struct.pack("<BIhB", 0, 1234, -500, 255)
    assert float(instruments(message(128267, d))[0].data.depth_meters) == pytest.approx(
        12.34
    )
    d = struct.pack("<BH", 0, 29315) + b"\xff" * 5
    assert float(instruments(message(130310, d))[0].data.temperature) == 20
    assert instruments(message(130312, b"\x00\x00\x01" + d[1:])) == []


def test_position_time_and_raw_source():
    msg = message(129025, struct.pack("<ii", 491234567, -1231234567))
    packets = instruments(msg)
    assert [p.data.sentence_type for p in packets] == ["ZDA", "GLL"]
    assert packets[0].data.datestamp == NOW.date()
    s = packets[1].data
    assert s.latitude == pytest.approx(49.1234567)
    assert s.longitude == pytest.approx(-123.1234567)
    assert s.timestamp.hour == 23
    assert msg.to_canboat().startswith("2026-10-12T23:59:59.123Z,2,129025,35,255,8,")
    assert instruments(message(129025, struct.pack("<ii", 2147483647, 0))) == []


def test_n2k_ascii_format_normalizes_to_shared_message():
    decoder = N2KAsciiDecoder()
    line = b"A173321.107 23FF7 1F513 012F3070002F30709F\r\n"
    assert decoder.feed(line[:17], NOW) == []
    decoded = decoder.feed(line[17:], NOW)
    assert len(decoded) == 1
    assert decoded[0].pgn == 0x1F513
    assert decoded[0].data == bytes.fromhex("012f3070002f30709f")
    assert decoded[0].priority == 7
    assert decoded[0].destination == 0xFF
    assert decoded[0].timestamp.hour == 17
    assert decoded[0].timestamp.minute == 33
    assert decoded[0].timestamp.second == 21
    assert decoded[0].timestamp.microsecond == 107000
    assert decoder.invalid_frames == 0


def test_n2k_ascii_ignores_metadata_and_rejects_bad_payloads():
    decoder = N2KAsciiDecoder()
    assert decoder.feed(b"M metadata\r\n", NOW) == []
    assert decoder.invalid_frames == 0
    assert decoder.feed(b"A173321.107 23FF7 1F513 xyz\r\n", NOW) == []
    assert decoder.invalid_frames == 1


def test_source_connect_and_disconnect():
    msg = message(130306, wind())
    with patch("gweatherrouting.core.nmea2000datasource.serial.Serial") as factory:
        port = factory.return_value
        ds = ActisenseDataSource("actisense", "in", "fake")
        assert ds.connect()
        assert port.write.call_args.args[0] == frame(0xA1, b"\x11\x02\x00")
        port.in_waiting = len(wire(msg))
        port.read.return_value = wire(msg)
        assert ds.read()[0].data.sentence_type == "MWV"
        assert ds.raw_messages[0].source == 35
        port.in_waiting = 0
        assert ds.read() == []
        assert ds.raw_messages == []
        port.read.side_effect = OSError("unplugged")
        port.in_waiting = 1
        assert ds.read() == []
        assert not ds.connected
        port.close.assert_called_once()


def test_n2k_ascii_serial_source_uses_shared_pgn_conversion():
    line = b"A173321.107 23FF2 1F112 001027FFFFFFFF00\r\n"
    with patch("gweatherrouting.core.nmea2000datasource.serial.Serial") as factory:
        port = factory.return_value
        source = NMEA2000DataSource(
            {
                "type": "serial",
                "protocol": "n2k_ascii",
                "direction": "in",
                "data-port": "fake",
                "baudrate": 115200,
            }
        )
        assert source.connect()
        port.write.assert_not_called()
        port.in_waiting = len(line)
        port.read.return_value = line
        packets = source.read()
        assert source.raw_messages[0].pgn == 127250
        assert packets[0].data.sentence_type == "HDT"
        source.close()


def test_n2k_ascii_network_source_reads_tcp_stream():
    line = b"A173321.107 23FF2 1F112 001027FFFFFFFF00\r\n"
    with (
        patch("gweatherrouting.core.nmea2000datasource.socket.socket") as factory,
        patch(
            "gweatherrouting.core.nmea2000datasource.select.select",
            return_value=([factory.return_value], [], []),
        ),
    ):
        sock = factory.return_value
        sock.recv.return_value = line
        source = NMEA2000DataSource(
            {
                "type": "network",
                "protocol": "n2k_ascii",
                "direction": "in",
                "network": "tcp",
                "host": "127.0.0.1",
                "port": 10110,
            }
        )
        assert source.connect()
        assert source.read()[0].data.sentence_type == "HDT"
        assert source.raw_messages[0].pgn == 127250
        source.close()


def test_nmea2000_protocol_keeps_gateway_format_separate():
    source = create_data_source(
        {
            "type": "network",
            "protocol": "nmea2000",
            "format": "n2k_ascii",
            "direction": "in",
            "network": "tcp",
            "host": "127.0.0.1",
            "port": 10110,
        }
    )
    assert isinstance(source, NMEA2000DataSource)
    assert source.protocol == "nmea2000"
    assert source.format == "n2k_ascii"
    assert source.decoder.__class__ is N2KAsciiDecoder


def test_input_only():
    with pytest.raises(ValueError):
        ActisenseDataSource("actisense", "both", "fake")


def test_manager_emits_unknown_pgn_for_recording():
    cm = ConnectionManager()
    cm.storage = MagicMock()
    cm.storage.connections = [
        {
            "type": "serial",
            "protocol": "actisense",
            "direction": "in",
            "data-port": "fake",
            "baudrate": 230400,
        }
    ]
    with patch.object(ActisenseDataSource, "connect", return_value=False):
        cm.plug_all()
    ds = cm.sources["fake"]
    ds.connected = True
    ds.read = MagicMock(return_value=[])
    ds.raw_messages = [message(130999, b"\x10\x00")]
    received = []
    cm.connect("nmea2000", received.extend)
    assert cm.poll() == 0
    assert received == ds.raw_messages
    assert cm.get_status()[0]["packets"] == 1
    cm.stop_polling()


def test_record_save_stop_and_new_session(tmp_path):
    r = InstrumentRecording(tmp_path / "temporary")
    msg = message(130306, wind())
    r.start()
    r.write_packets(instruments(msg))
    r.write_nmea2000([msg, message(130999, b"\xff")])
    target = tmp_path / "sail with spaces.nmea"
    r.save(target)
    assert "MWV" in target.read_text()
    assert "130999" in (tmp_path / "sail with spaces.nmea.n2k.log").read_text()
    r.close()
    r.write_packets(instruments(msg))
    r.write_nmea2000([msg])
    assert len(r.path.read_text().splitlines()) == 1
    r.start()
    assert not r.raw_path.exists()
    r.close()


def test_simulator_to_serial_instruments_and_recording(tmp_path):
    import importlib.util
    import os
    import pty
    import tty
    from pathlib import Path

    from gweatherrouting.core.core import Core

    spec = importlib.util.spec_from_file_location(
        "sim", Path(__file__).parents[1] / "tools/simulate_actisense.py"
    )
    sim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sim)
    master, slave = pty.openpty()
    tty.setraw(slave)
    ds = ActisenseDataSource("actisense", "in", os.ttyname(slave))
    recording = InstrumentRecording(tmp_path / "demo.nmea")
    try:
        assert ds.connect()
        # An independently encoded mock NGT-1 stream through a real serial port.
        encoded, _, _ = sim.sample(0, 49.285, -123.24)
        os.write(master, encoded)
        import time

        deadline = time.monotonic() + 2
        packets = []
        while not packets and time.monotonic() < deadline:
            packets = ds.read()
            time.sleep(0.01)
        assert len(ds.raw_messages) == 8
        assert {p.data.sentence_type for p in packets} == {
            "HDT",
            "VHW",
            "VTG",
            "MWV",
            "DBT",
            "MTW",
            "ZDA",
            "GLL",
        }
        core = object.__new__(Core)
        from gweatherrouting.core.utils import DotDict

        core.boat_info = DotDict()
        for packet in packets:
            core._parse_nmea_extra(packet)
        assert core.boat_info.aws > 0
        assert core.boat_info.tws == pytest.approx(6.2 * 3600 / 1852, abs=0.0001)
        assert core.boat_info.sog > 0
        assert core.boat_info.speed == pytest.approx(3.2 * 3600 / 1852, abs=0.0001)
        recording.start()
        recording.write_packets(packets)
        recording.write_nmea2000(ds.raw_messages)
        recording.close()
        import nmeatoolkit as nt

        points = []
        translator = nt.TrackPointTranslator()
        for packet in packets:
            point = translator.feed(packet.data)
            if point is not None:
                points.append(point)
        assert len(points) == 1
        assert points[0].lat == pytest.approx(49.285)
        assert points[0].aws > 0
        assert len(recording.raw_path.read_text().splitlines()) == 8
    finally:
        recording.close()
        ds.close()
        os.close(master)
        os.close(slave)
