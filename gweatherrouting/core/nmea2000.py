"""Transport-independent NMEA 2000 messages and instrument decoding."""

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from .datasource import NMEADataPacket


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
    return []
