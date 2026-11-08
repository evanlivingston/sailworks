"""Transport-independent NMEA 2000 messages and instrument decoding."""

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from .datasource import AISDataPacket, NMEADataPacket


@dataclass(frozen=True)
class N2KMessage:
    """A complete NMEA 2000 PGN, independent of its gateway wire format."""

    timestamp: datetime
    priority: int
    pgn: int
    destination: int
    source: int
    source_timestamp_ms: int
    data: bytes
    direction: str = "rx"
    fragmented: bool = False

    def to_canboat(self) -> str:
        """Serialize as a timestamped CANboat-compatible PGN record."""
        stamp = self.timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        fields = [
            stamp,
            str(self.priority),
            str(self.pgn),
            str(self.source),
            str(self.destination),
            str(len(self.data)),
        ]
        return ",".join(fields + [f"{b:02x}" for b in self.data])


class N2KAsciiDecoder:
    """Decode Actisense N2K ASCII records into complete PGN messages."""

    _record = re.compile(
        r"^A(\d{6}(?:\.\d{1,3})?)\s+([0-9a-fA-F]{5})\s+"
        r"([0-9a-fA-F]{5,6})\s+([0-9a-fA-F]+)(?:\s+.*)?$"
    )

    def __init__(self):
        self.buffer = bytearray()
        self.invalid_frames = 0

    def feed(
        self, chunk: bytes, timestamp: Optional[datetime] = None
    ) -> List[N2KMessage]:
        messages: List[N2KMessage] = []
        self.buffer.extend(chunk)
        if len(self.buffer) > 65536 and b"\n" not in self.buffer:
            self.buffer.clear()
            self.invalid_frames += 1
            return messages

        while b"\n" in self.buffer:
            raw, _, remainder = self.buffer.partition(b"\n")
            self.buffer = bytearray(remainder)
            if len(raw) > 4096:
                self.invalid_frames += 1
                continue
            msg = self._decode_line(raw.decode("ascii", errors="replace"), timestamp)
            if msg is not None:
                messages.append(msg)
        return messages

    def _decode_line(self, line, received_at=None):
        line = line.strip()
        if line and not line.startswith("A"):
            # Actisense reserves other record prefixes for gateway metadata.
            return None
        match = self._record.fullmatch(line)
        if match is None:
            if line.strip():
                self.invalid_frames += 1
            return None

        try:
            return self._decode_match(match, received_at)
        except (ValueError, OverflowError):
            self.invalid_frames += 1
            return None

    @staticmethod
    def _decode_match(match, received_at):
        clock, source_header, pgn_text, data_text = match.groups()
        if len(data_text) % 2 or len(data_text) > 3572:
            raise ValueError("invalid payload length")
        source = int(source_header[:2], 16)
        destination = int(source_header[2:4], 16)
        priority = int(source_header[4], 16) & 7
        pgn = int(pgn_text, 16)
        data = bytes.fromhex(data_text)
        if pgn > 0x3FFFF or not data:
            raise ValueError("invalid PGN or empty payload")

        received_at = received_at or datetime.now(timezone.utc)
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)
        hour, minute = int(clock[:2]), int(clock[2:4])
        second = float(clock[4:])
        if hour > 23 or minute > 59 or second >= 60:
            raise ValueError("invalid timestamp")
        whole_second = int(second)
        micros = int(round((second - whole_second) * 1_000_000))
        timestamp = received_at.astimezone(timezone.utc).replace(
            hour=hour,
            minute=minute,
            second=whole_second,
            microsecond=micros,
        )
        delta = timestamp - received_at.astimezone(timezone.utc)
        if delta > timedelta(hours=12):
            timestamp -= timedelta(days=1)
        elif delta < -timedelta(hours=12):
            timestamp += timedelta(days=1)

        milliseconds = ((hour * 60 + minute) * 60 + whole_second) * 1000 + round(
            micros / 1000
        )
        return N2KMessage(
            timestamp,
            priority,
            pgn,
            destination,
            source,
            milliseconds,
            data,
        )


def number(data, offset, size, scale=1.0, signed=False):
    """Decode a field, rejecting NMEA unavailable/error/reserved sentinels."""
    if len(data) < offset + size:
        return None
    raw = int.from_bytes(data[offset : offset + size], "little", signed=signed)
    maximum = (1 << (size * 8 - int(signed))) - 1
    if raw >= maximum - 2:
        return None
    return raw * scale


def angle(value):
    return math.degrees(value) % 360


def coordinate(value, latitude):
    value_abs = abs(value)
    degrees = int(value_abs)
    width = 2 if latitude else 3
    text = f"{degrees:0{width}d}{(value_abs - degrees) * 60:09.6f}"
    hemisphere = (
        ("N" if value >= 0 else "S") if latitude else ("E" if value >= 0 else "W")
    )
    return text, hemisphere


def sentence(kind, fields):
    body = "II" + kind + "," + ",".join(str(f) for f in fields)
    checksum = 0
    for c in body:
        checksum ^= ord(c)
    return NMEADataPacket.parse(f"${body}*{checksum:02X}")


class _N2KSentence:
    """Minimal pynmea2-like sentence for raw N2K PGN instrument fields."""

    def __init__(self, sentence_type, fields):
        self.sentence_type = sentence_type
        self.fields = fields
        for display, attr, value in fields:
            setattr(self, attr, value)

    def serialize(self):
        body = self.sentence_type + ",".join(
            "," if getattr(self, f[1]) is None else str(getattr(self, f[1]))
            for f in self.fields
        ).lstrip(",")
        checksum = 0
        for c in body:
            checksum ^= ord(c)
        return f"${body}*{checksum:02X}"


def _n2k_packet(kind, fields):
    return NMEADataPacket(_N2KSentence(kind, fields))


# -- AIS ------------------------------------------------------------------


def _ais_char(value):
    return chr(value + 48) if value < 40 else chr(value + 56)


def _ais_text(value, length):
    """Encode AIS six-bit text, padding with '@' (0)."""
    out = 0
    for index in range(length):
        char = value[index] if index < len(value) else "@"
        code = ord(char)
        if 64 <= code <= 95:
            six = code - 64
        elif 32 <= code <= 63:
            six = code
        else:
            six = 0
        out = (out << 6) | six
    return out, length * 6


class _AisBits:
    def __init__(self):
        self.value = 0
        self.length = 0

    def add(self, value, length, signed=False):
        if value is None:
            value = 0
        if signed and value < 0:
            value += 1 << length
        value &= (1 << length) - 1
        self.value = (self.value << length) | value
        self.length += length
        return self

    def text(self, value, length):
        packed, bits = _ais_text(value or "", length)
        self.value = (self.value << bits) | packed
        self.length += bits
        return self

    def sentence(self, channel="A"):
        fill = (-self.length) % 6
        total = self.length + fill
        payload = []
        value = self.value << fill
        for shift in range(total - 6, -1, -6):
            payload.append(_ais_char((value >> shift) & 0x3F))
        body = f"AIVDM,1,1,,{channel},{''.join(payload)},{fill}"
        checksum = 0
        for char in body:
            checksum ^= ord(char)
        return f"!{body}*{checksum:02X}"


def _ais_signed(data, offset, size, scale=1.0):
    value = number(data, offset, size, scale, True)
    return None if value is None else value


def _ais_position(data):
    lon = _ais_signed(data, 5, 4, 1e-7)
    lat = _ais_signed(data, 9, 4, 1e-7)
    return (
        None if lon is None else round(lon * 600000),
        None if lat is None else round(lat * 600000),
    )


def _ais_cog(value):
    return None if value is None else round(math.degrees(value) * 10) % 3600


def _ais_sog(value):
    return None if value is None else round(value * 19.438444)


def _ais_heading(value):
    if value is None:
        return 511
    return round(math.degrees(value)) % 360


def _ais_rot(value):
    if value is None:
        return -128
    degrees_per_minute = value * 60
    magnitude = min(126, round(4.733 * math.sqrt(abs(degrees_per_minute))))
    return -magnitude if degrees_per_minute < 0 else magnitude


def _ais_common(bits, message_type, repeat, mmsi):
    bits.add(message_type, 6).add(repeat, 2).add(mmsi, 30)


def _ais_position_report(message, message_type):
    d = message.data
    mmsi = number(d, 1, 4)
    if mmsi is None:
        return None
    lon, lat = _ais_position(d)
    bits = _AisBits()
    _ais_common(bits, message_type, (d[0] >> 6) & 3, int(mmsi))
    if message_type in (1, 2, 3):
        nav_status = d[25] & 0x0F if len(d) > 25 else None
        rot = number(d, 23, 2, 3.125e-05, True)
        cog = _ais_cog(number(d, 14, 2, 0.0001))
        sog = _ais_sog(number(d, 16, 2, 0.01))
        heading = _ais_heading(number(d, 21, 2, 0.0001))
        # Field offsets follow canboat: accuracy bit 104, RAIM bit 105,
        # timestamp bits 106..111.
        accuracy = (d[13] >> 0) & 1 if len(d) > 13 else 0
        raim = (d[13] >> 1) & 1 if len(d) > 13 else 0
        timestamp = (int.from_bytes(d[13:14], "little") >> 2) & 0x3F
        bits.add(0 if nav_status is None else int(nav_status), 4)
        bits.add(_ais_rot(rot), 8, True)
        bits.add(0 if sog is None else sog, 10)
        bits.add(accuracy, 1)
        bits.add(0 if lon is None else lon, 28, True)
        bits.add(0 if lat is None else lat, 27, True)
        bits.add(0 if cog is None else cog, 12)
        bits.add(heading, 9)
        bits.add(0 if timestamp is None else int(timestamp), 6)
        bits.add(0, 2)  # maneuver
        bits.add(0, 3)  # spare
        bits.add(raim, 1)
        bits.add(0, 19)  # radio status
    else:
        sog = _ais_sog(number(d, 16, 2, 0.01))
        cog = _ais_cog(number(d, 14, 2, 0.0001))
        heading = _ais_heading(number(d, 21, 2, 0.0001))
        accuracy = (d[13] >> 0) & 1 if len(d) > 13 else 0
        raim = (d[13] >> 1) & 1 if len(d) > 13 else 0
        timestamp = (int.from_bytes(d[13:14], "little") >> 2) & 0x3F
        bits.add(0, 8)  # reserved
        bits.add(0 if sog is None else sog, 10)
        bits.add(accuracy, 1)
        bits.add(0 if lon is None else lon, 28, True)
        bits.add(0 if lat is None else lat, 27, True)
        bits.add(0 if cog is None else cog, 12)
        bits.add(heading, 9)
        bits.add(0 if timestamp is None else int(timestamp), 6)
        bits.add(0, 2)
        bits.add(0, 1)
        bits.add(0, 1)
        bits.add(0, 1)
        bits.add(0, 1)
        bits.add(0, 1)
        bits.add(0, 1)
        bits.add(raim, 1)
        bits.add(0, 20)
    return bits.sentence()


def _ais_extended_position(message):
    d = message.data
    mmsi = number(d, 1, 4)
    if mmsi is None:
        return None
    lon, lat = _ais_position(d)
    bits = _AisBits()
    _ais_common(bits, 19, (d[0] >> 6) & 3, int(mmsi))
    sog = _ais_sog(number(d, 16, 2, 0.01))
    cog = _ais_cog(number(d, 14, 2, 0.0001))
    heading = _ais_heading(number(d, 21, 2, 0.0001))
    accuracy = (d[13] >> 0) & 1 if len(d) > 13 else 0
    timestamp = (int.from_bytes(d[13:14], "little") >> 2) & 0x3F
    name = (
        d[32:52]
        .split(b"\x00", 1)[0]
        .split(b"\xff", 1)[0]
        .decode("ascii", errors="replace")
    )
    ship_type = number(d, 20, 1) or 0
    to_bow = number(d, 24, 2, 0.1) or 0
    to_stern = number(d, 26, 2, 0.1) or 0
    to_port = number(d, 28, 2, 0.1) or 0
    to_starboard = number(d, 30, 2, 0.1) or 0
    bits.add(0, 8)
    bits.add(0 if sog is None else sog, 10)
    bits.add(accuracy, 1)
    bits.add(0 if lon is None else lon, 28, True)
    bits.add(0 if lat is None else lat, 27, True)
    bits.add(0 if cog is None else cog, 12)
    bits.add(heading, 9)
    bits.add(0 if timestamp is None else int(timestamp), 6)
    bits.add(0, 4)
    bits.text(name, 20)
    bits.add(ship_type, 8)
    bits.add(min(511, round(to_bow)), 9)
    bits.add(min(511, round(to_stern)), 9)
    bits.add(min(63, round(to_port)), 6)
    bits.add(min(63, round(to_starboard)), 6)
    bits.add(0, 4)
    bits.add(0, 1)
    bits.add(0, 1)
    bits.add(0, 1)
    bits.add(0, 4)
    return bits.sentence()


def _ais_static(message):
    d = message.data
    mmsi = number(d, 1, 4)
    if mmsi is None:
        return None
    bits = _AisBits()
    _ais_common(bits, 5, (d[0] >> 6) & 3, int(mmsi))
    bits.add(0, 2)  # AIS version
    bits.add(number(d, 5, 4) or 0, 30)  # IMO
    callsign = d[9:16].split(b"\x00", 1)[0].split(b"\xff", 1)[0]
    name = d[16:36].split(b"\x00", 1)[0].split(b"\xff", 1)[0]
    destination = d[53:73].split(b"\x00", 1)[0].split(b"\xff", 1)[0]
    bits.text(callsign.decode("ascii", errors="replace").strip(), 7)
    bits.text(name.decode("ascii", errors="replace").strip(), 20)
    bits.add(number(d, 36, 1) or 0, 8)
    bits.add(min(511, round(number(d, 37, 2, 0.1) or 0)), 9)
    bits.add(min(511, round(number(d, 39, 2, 0.1) or 0)), 9)
    bits.add(min(63, round(number(d, 41, 2, 0.1) or 0)), 6)
    bits.add(min(63, round(number(d, 43, 2, 0.1) or 0)), 6)
    bits.add(0, 4)
    bits.add(0, 4)
    bits.add(0, 5)
    bits.add(0, 5)
    bits.add(0, 6)
    bits.add(min(255, round((number(d, 51, 2, 0.01) or 0) / 0.1)), 8)
    bits.text(destination.decode("ascii", errors="replace").strip(), 20)
    bits.add(0, 1)
    bits.add(0, 1)
    return bits.sentence()


def _ais_static_part_a(message):
    d = message.data
    mmsi = number(d, 1, 4)
    if mmsi is None:
        return None
    name = d[5:25].split(b"\x00", 1)[0].split(b"\xff", 1)[0]
    bits = _AisBits()
    _ais_common(bits, 24, (d[0] >> 6) & 3, int(mmsi))
    bits.add(0, 2)
    bits.text(name.decode("ascii", errors="replace").strip(), 20)
    return bits.sentence()


def _ais_static_part_b(message):
    d = message.data
    mmsi = number(d, 1, 4)
    if mmsi is None:
        return None
    callsign = d[13:20].split(b"\x00", 1)[0].split(b"\xff", 1)[0]
    bits = _AisBits()
    _ais_common(bits, 24, (d[0] >> 6) & 3, int(mmsi))
    bits.add(1, 2)
    bits.add(number(d, 5, 1) or 0, 8)
    bits.add(0, 42)  # vendor id
    bits.text(callsign.decode("ascii", errors="replace").strip(), 7)
    bits.add(min(511, round(number(d, 20, 2, 0.1) or 0)), 9)
    bits.add(min(511, round(number(d, 22, 2, 0.1) or 0)), 9)
    bits.add(min(63, round(number(d, 24, 2, 0.1) or 0)), 6)
    bits.add(min(63, round(number(d, 26, 2, 0.1) or 0)), 6)
    bits.add(0, 6)
    return bits.sentence()


def _ais_utc_report(message):
    d = message.data
    mmsi = number(d, 1, 4)
    if mmsi is None:
        return None
    lon, lat = _ais_position(d)
    timestamp = number(d, 14, 4, 0.0001)
    day = number(d, 21, 2)
    bits = _AisBits()
    _ais_common(bits, 4, (d[0] >> 6) & 3, int(mmsi))
    if timestamp is None:
        year, month, day_value, hour, minute, second = 0, 0, 0, 24, 60, 60
    else:
        from datetime import datetime as _dt

        days = 0 if day is None else int(day)
        value = _dt.fromtimestamp(days * 86400 + timestamp, tz=timezone.utc)
        year, month, day_value = value.year, value.month, value.day
        hour, minute, second = value.hour, value.minute, value.second
    bits.add(year, 14)
    bits.add(month, 4)
    bits.add(day_value, 5)
    bits.add(hour, 5)
    bits.add(minute, 6)
    bits.add(second, 6)
    bits.add(0, 1)
    bits.add(0 if lon is None else lon, 28, True)
    bits.add(0 if lat is None else lat, 27, True)
    bits.add(0, 4)
    bits.add(0, 10)
    bits.add(0, 1)
    bits.add(0, 19)
    return bits.sentence()


def _ais_sar(message):
    d = message.data
    mmsi = number(d, 1, 4)
    if mmsi is None:
        return None
    lon, lat = _ais_position(d)
    bits = _AisBits()
    _ais_common(bits, 9, (d[0] >> 6) & 3, int(mmsi))
    altitude = number(d, 21, 4, 0.01, True)
    bits.add(4095 if altitude is None else min(4094, round(altitude)), 12)
    sog = _ais_sog(number(d, 16, 2, 0.1))
    bits.add(0 if sog is None else sog, 10)
    bits.add((d[13] >> 0) & 1 if len(d) > 13 else 0, 1)
    bits.add(0 if lon is None else lon, 28, True)
    bits.add(0 if lat is None else lat, 27, True)
    bits.add(_ais_cog(number(d, 14, 2, 0.0001)) or 0, 12)
    bits.add((int.from_bytes(d[13:14], "little") >> 2) & 0x3F, 6)
    bits.add(0, 8)
    bits.add(0, 1)
    bits.add(0, 3)
    bits.add(0, 1)
    bits.add(0, 1)
    bits.add(0, 20)
    return bits.sentence()


def _ais_aton(message):
    d = message.data
    mmsi = number(d, 1, 4)
    if mmsi is None:
        return None
    lon, lat = _ais_position(d)
    bits = _AisBits()
    _ais_common(bits, 21, (d[0] >> 6) & 3, int(mmsi))
    bits.add((number(d, 22, 1) or 0) & 0x1F, 5)
    bits.text("", 20)
    bits.add((d[13] >> 0) & 1 if len(d) > 13 else 0, 1)
    bits.add(0 if lon is None else lon, 28, True)
    bits.add(0 if lat is None else lat, 27, True)
    bits.add(min(511, round(number(d, 14, 2, 0.1) or 0)), 9)
    bits.add(min(511, round(number(d, 16, 2, 0.1) or 0)), 9)
    bits.add(min(63, round(number(d, 18, 2, 0.1) or 0)), 6)
    bits.add(min(63, round(number(d, 20, 2, 0.1) or 0)), 6)
    bits.add(0, 4)
    bits.add(60, 6)
    bits.add(0, 1)
    bits.add(0, 8)
    bits.add(0, 1)
    bits.add(0, 1)
    bits.add(0, 1)
    bits.add(0, 1)
    return bits.sentence()


_AIS_CONVERTERS = {
    129038: lambda message: _ais_position_report(message, 1),
    129039: lambda message: _ais_position_report(message, 18),
    129040: _ais_extended_position,
    129041: _ais_aton,
    129793: _ais_utc_report,
    129794: _ais_static,
    129798: _ais_sar,
    129809: _ais_static_part_a,
    129810: _ais_static_part_b,
}


def _ais_packet(message):
    converter = _AIS_CONVERTERS.get(message.pgn)
    if converter is None:
        return []
    sentence = converter(message)
    return [] if sentence is None else [AISDataPacket(sentence)]


def _battery_packets(message):
    d = message.data
    pgn = message.pgn
    if pgn == 127508 and len(d) >= 7:
        fields = [("Instance", "instance", d[0])]
        for display, attribute, value, scale in (
            ("Voltage", "voltage_v", number(d, 1, 2, 0.01, True), 1),
            ("Current", "current_a", number(d, 3, 2, 0.1, True), 1),
            ("Temperature", "temperature_c", number(d, 5, 2, 0.01), 1),
        ):
            if value is not None:
                fields.append(
                    (
                        display,
                        attribute,
                        (
                            f"{value - 273.15:.1f}"
                            if "temp" in attribute
                            else f"{value:.2f}"
                        ),
                    )
                )
        return [_n2k_packet("BAT", fields)]
    if pgn == 127506 and len(d) >= 10:
        fields = [("Instance", "instance", d[1])]
        for display, attribute, offset, size, scale in (
            ("State of Charge", "state_of_charge_pct", 3, 1, 1),
            ("State of Health", "state_of_health_pct", 4, 1, 1),
            ("Time Remaining", "time_remaining_s", 5, 2, 60),
            ("Ripple Voltage", "ripple_voltage_v", 7, 2, 0.001),
            ("Remaining Capacity", "remaining_capacity_ah", 9, 2, 1),
        ):
            value = number(d, offset, size, scale)
            if value is not None:
                fields.append((display, attribute, f"{value:.2f}"))
        return [_n2k_packet("DCS", fields)]
    if pgn == 127507 and len(d) >= 6:
        fields = [("Instance", "instance", d[0])]
        value = number(d, 4, 2, 60)
        if value is not None:
            fields.append(("Equalization Time", "equalization_time_s", f"{value:.0f}"))
        return [_n2k_packet("CHG", fields)]
    if pgn == 127509 and len(d) >= 4:
        return [_n2k_packet("INV", [("Instance", "instance", d[0])])]
    return []


def _navigation_packets(message):
    d = message.data
    pgn = message.pgn
    if pgn == 129283 and len(d) >= 6:
        xte = number(d, 2, 4, 0.01, True)
        if xte is None:
            return []
        return [
            _n2k_packet(
                "XTE",
                [
                    ("XTE", "xte_nm", f"{xte / 1852:.4f}"),
                    ("Mode", "mode", d[1] & 0x0F),
                ],
            )
        ]
    if pgn == 129284 and len(d) >= 34:
        fields = []
        distance = number(d, 1, 4, 0.01)
        if distance is not None:
            fields.append(("Distance", "distance_nm", f"{distance / 1852:.4f}"))
        bearing = number(d, 12, 2, 0.0001)
        if bearing is not None:
            fields.append(("Bearing", "bearing_deg", f"{angle(bearing):.4f}"))
        return [_n2k_packet("NAV", fields)] if fields else []
    if pgn == 129285 and len(d) >= 10:
        return [_n2k_packet("RTE", [("Route ID", "route_id", number(d, 6, 2) or 0)])]
    return []


def instruments(message: N2KMessage) -> List[NMEADataPacket]:  # noqa: C901
    """Convert supported PGNs to NMEA 0183 packets used by sailing features.

    Unknown PGNs remain available as N2KMessage objects for raw recording.
    """
    d, pgn = message.data, message.pgn
    if pgn == 127250 and len(d) >= 8:
        heading = number(d, 1, 2, 0.0001)
        if heading is None:
            return []
        reference = d[7] & 3
        if reference == 0:
            return [sentence("HDT", [f"{angle(heading):.4f}", "T"])]
        if reference == 1:
            deviation = number(d, 3, 2, 0.0001, True)
            variation = number(d, 5, 2, 0.0001, True)
            fields = [f"{angle(heading):.4f}"]
            for value in (deviation, variation):
                fields += (
                    ["", ""]
                    if value is None
                    else [f"{abs(math.degrees(value)):.4f}", "E" if value >= 0 else "W"]
                )
            return [sentence("HDG", fields)]
    elif pgn == 128259:
        speed = number(d, 1, 2, 0.01)
        if speed is not None:
            return [
                sentence(
                    "VHW",
                    [
                        "",
                        "T",
                        "",
                        "M",
                        f"{speed * 3600 / 1852:.4f}",
                        "N",
                        f"{speed * 3.6:.4f}",
                        "K",
                    ],
                )
            ]
    elif pgn == 129026 and len(d) >= 6:
        course, speed = number(d, 2, 2, 0.0001), number(d, 4, 2, 0.01)
        reference = d[1] & 3
        if reference in (0, 1) and (course is not None or speed is not None):
            cog = "" if course is None else f"{angle(course):.4f}"
            return [
                sentence(
                    "VTG",
                    [
                        cog if reference == 0 else "",
                        "T",
                        cog if reference == 1 else "",
                        "M",
                        "" if speed is None else f"{speed * 3600 / 1852:.4f}",
                        "N",
                        "" if speed is None else f"{speed * 3.6:.4f}",
                        "K",
                        "A",
                    ],
                )
            ]
    elif pgn in (129025, 129029):
        if pgn == 129029:
            if len(d) < 43 or (d[31] >> 4) not in (1, 2, 3, 4, 5) or (d[32] & 3) == 2:
                return []
            lat, lon = number(d, 7, 8, 1e-16, True), number(d, 15, 8, 1e-16, True)
        else:
            lat, lon = number(d, 0, 4, 1e-7, True), number(d, 4, 4, 1e-7, True)
        if lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180:
            now = message.timestamp.astimezone(timezone.utc)
            stamp = now.strftime("%H%M%S.%f")[:10]
            la, ns = coordinate(lat, True)
            lo, ew = coordinate(lon, False)
            return [
                sentence("ZDA", [stamp, now.day, now.month, now.year, "00", "00"]),
                sentence("GLL", [la, ns, lo, ew, stamp, "A", "A"]),
            ]
    elif pgn == 130306 and len(d) >= 6:
        speed, wind_angle = number(d, 1, 2, 0.01), number(d, 3, 2, 0.0001)
        reference = d[5] & 7
        if reference in (2, 4) and speed is not None and wind_angle is not None:
            return [
                sentence(
                    "MWV",
                    [
                        f"{angle(wind_angle):.4f}",
                        "R" if reference == 2 else "T",
                        f"{speed * 3600 / 1852:.4f}",
                        "N",
                        "A",
                    ],
                )
            ]
    elif pgn == 128267:
        depth = number(d, 1, 4, 0.01)
        if depth is not None:
            return [
                sentence(
                    "DBT",
                    [
                        f"{depth / 0.3048:.4f}",
                        "f",
                        f"{depth:.4f}",
                        "M",
                        f"{depth / 1.8288:.4f}",
                        "F",
                    ],
                )
            ]
    elif pgn in (130310, 130312, 130316):
        if pgn == 130310:
            temperature = number(d, 1, 2, 0.01)
        elif len(d) >= 3 and d[2] == 0:
            temperature = number(
                d, 3, 3 if pgn == 130316 else 2, 0.001 if pgn == 130316 else 0.01
            )
        else:
            temperature = None
        if temperature is not None:
            return [sentence("MTW", [f"{temperature - 273.15:.3f}", "C"])]
    elif pgn == 127245 and len(d) >= 5:
        instance = d[0]
        order = number(d, 1, 2, 0.0001, True)
        position = number(d, 3, 2, 0.0001, True)
        fields = [("Instance", "instance", instance)]
        if order is not None:
            fields.append(("Rudder Order", "order_deg", f"{math.degrees(order):.1f}"))
        if position is not None:
            fields.append(
                ("Rudder Position", "position_deg", f"{math.degrees(position):.1f}")
            )
        return [_n2k_packet("RUD", fields)]
    elif pgn == 127488 and len(d) >= 3:
        rpm = number(d, 1, 2, 0.125)
        if rpm is not None:
            return [
                _n2k_packet(
                    "ENG",
                    [
                        ("Instance", "instance", d[0]),
                        ("Engine Speed", "rpm", f"{rpm:.0f}"),
                    ],
                )
            ]
    elif pgn == 127489 and len(d) >= 11:
        instance = d[0]
        fields = [("Instance", "instance", instance)]
        oil_pressure = number(d, 1, 2, 1.0)
        if oil_pressure is not None:
            fields.append(
                ("Engine Oil Pressure", "oil_pressure_kpa", f"{oil_pressure:.0f}")
            )
        oil_temp = number(d, 3, 2, 0.125, True)
        if oil_temp is not None:
            fields.append(("Engine Oil Temp", "oil_temp_c", f"{oil_temp - 273.15:.1f}"))
        engine_temp = number(d, 5, 2, 0.125, True)
        if engine_temp is not None:
            fields.append(
                ("Engine Temperature", "engine_temp_c", f"{engine_temp - 273.15:.1f}")
            )
        alternator = number(d, 7, 2, 0.05)
        if alternator is not None:
            fields.append(("Alternator Potential", "alternator_v", f"{alternator:.2f}"))
        fuel_rate = number(d, 9, 2, 0.0001)
        if fuel_rate is not None:
            fields.append(("Fuel Rate", "fuel_rate_lph", f"{fuel_rate:.2f}"))
        return [_n2k_packet("ENG", fields)]
    elif pgn in _AIS_CONVERTERS:
        return _ais_packet(message)
    elif pgn in (127508, 127506, 127507, 127509):
        return _battery_packets(message)
    elif pgn in (129283, 129284, 129285):
        return _navigation_packets(message)
    return []
