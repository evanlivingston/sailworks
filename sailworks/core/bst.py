"""Actisense BST and BDTP decoding shared by EBL and gateway readers.

The BST layer is transport independent: a BSTRawFrame metatag and a BDTP
datagram carry the same unframed message bytes. ``BstDecoder`` accepts BST-93,
BST-94, BST-95, BST-9D and BST-D0. ``BdtpStreamDecoder`` strips DLE framing
from serial-style captures before handing complete messages to it.

References (Actisense SDK, Apache-2.0):
docs/DataFormats/Binary/BST.md and docs/DataFormats/Binary/bst-detail/.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import ClassVar, List

from .datasource import AISDataPacket, NMEADataPacket
from .nmea2000 import N2KMessage

DLE = 0x10
STX = 0x02
ETX = 0x03

BST_N2K_GATEWAY = 0x93
BST_N2K_HOST = 0x94
BST_CAN_FRAME = 0x95
BST_NMEA0183 = 0x9D
BST_N2K_D0 = 0xD0

MAX_SENTENCE_LENGTH = 1024
UNKNOWN_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class CanHeader:
    """A decoded CAN frame whose payload was not materialised.

    The replay builder only needs the PGN, source and direction to count
    unsupported traffic; keeping the eight data bytes out of the common path
    avoids one allocation per captured frame.
    """

    timestamp: datetime
    pgn: int
    source: int
    direction: str
    data_length: int
    data: ClassVar[bytes] = b""


def _pgn(data_page, pdu_format, pdu_specific):
    pgn = (data_page << 16) | (pdu_format << 8)
    if pdu_format >= 240:
        pgn |= pdu_specific
    return pgn


def _direction(value, default):
    if value is None:
        return default
    return "tx" if value else "rx"


def _checksummed(frame, expected):
    """Return a message without its optional zero-sum trailing byte."""
    if len(frame) == expected:
        return frame
    if len(frame) == expected + 1:
        if sum(frame) & 0xFF:
            return None
        return frame[:-1]
    return None


class BstDecoder:
    """Decode one unframed BST message at a time."""

    def __init__(self, materialize=None, fast_pgns=(), inventory_pgns=()):
        self.materialize = materialize
        self.fast_pgns = fast_pgns
        self.inventory_pgns = inventory_pgns
        self.diagnostics: List[str] = []
        self.direction_counts = {"rx": 0, "tx": 0}
        self.invalid = 0

    def _issue(self, text):
        if len(self.diagnostics) < 100:
            self.diagnostics.append(text)

    def decode(self, frame, timestamp, direction="unknown"):
        if len(frame) < 2:
            self._issue("truncated BST header")
            self.invalid += 1
            return None
        identifier = frame[0]
        if identifier == BST_CAN_FRAME:
            return self._can_frame(frame, timestamp, direction)
        if identifier == BST_N2K_GATEWAY:
            return self._n2k_93(frame, timestamp, direction)
        if identifier == BST_N2K_HOST:
            return self._n2k_94(frame, timestamp, direction)
        if identifier == BST_NMEA0183:
            return self._nmea_9d(frame, timestamp, direction)
        if BST_N2K_D0 <= identifier <= 0xDF:
            return self._n2k_d0(frame, timestamp, direction)
        self._issue(f"unsupported BST message 0x{identifier:02x}")
        self.invalid += 1
        return None

    # -- BST-95 ----------------------------------------------------------

    def _can_frame(self, frame, timestamp, direction):
        length = frame[1]
        if not 6 <= length <= 14:
            self._issue(f"unsupported BST-95 length {length}")
            self.invalid += 1
            return None
        expected = length + 2
        size = len(frame)
        if size == expected:
            body = frame
        elif size == expected + 1:
            if sum(frame) & 0xFF:
                self._issue("invalid BST-95 checksum")
                self.invalid += 1
                return None
            body = frame[:-1]
        else:
            self._issue("BST-95 length does not match record")
            self.invalid += 1
            return None
        source = body[4]
        pdus = body[5]
        pduf = body[6]
        dppc = body[7]
        pgn = ((dppc & 3) << 16) | (pduf << 8)
        if pduf >= 240:
            pgn |= pdus
        marker = direction
        if marker == "unknown":
            marker = "tx" if dppc & 0x80 else "rx"
        self.direction_counts[marker] += 1
        stamp = timestamp or UNKNOWN_EPOCH
        if self.materialize is not None and not self.materialize(pgn):
            return (stamp, pgn, source, marker, length - 6)
        priority = (dppc >> 2) & 7
        destination = pdus if pduf < 240 else 255
        # A wrapping device counter, not milliseconds since midnight. Preserve
        # the container's millisecond precision via DPPC's resolution bits.
        resolution_us = (1000, 100, 10, 1)[(dppc >> 5) & 3]
        millis = (body[2] | (body[3] << 8)) * resolution_us // 1000
        if pgn in self.fast_pgns and pgn not in self.inventory_pgns:
            # Hand raw fast-packet fragments to the assembler without creating
            # one full N2KMessage per fragment.
            return (
                stamp,
                pgn,
                source,
                priority,
                destination,
                millis,
                bytes(body[8:]),
                marker,
            )
        return N2KMessage(
            stamp,
            priority,
            pgn,
            destination,
            source,
            millis,
            bytes(body[8:]),
            marker,
            True,
        )

    # -- BST-93 ----------------------------------------------------------

    def _n2k_93(self, frame, timestamp, direction):
        if len(frame) < 13:
            self._issue("unsupported BST message 0x93 length")
            self.invalid += 1
            return None
        length = frame[1]
        data_length = frame[12]
        expected = length + 2
        body = _checksummed(frame, expected)
        if body is None or length != data_length + 11:
            self._issue("BST-93 length does not match record")
            self.invalid += 1
            return None
        pgn = _pgn(body[5] & 3, body[4], body[3])
        source, destination = body[7], body[6]
        priority = body[2] & 7
        millis = int.from_bytes(body[8:12], "little")
        stamp = timestamp or datetime(1970, 1, 1, tzinfo=timezone.utc)
        marker = _direction(None, "rx" if direction == "unknown" else direction)
        self.direction_counts[marker] += 1
        if self.materialize is not None and not self.materialize(pgn):
            return (stamp, pgn, source, marker, data_length)
        return N2KMessage(
            stamp,
            priority,
            pgn,
            destination,
            source,
            millis,
            bytes(body[13 : 13 + data_length]),
            marker,
        )

    # -- BST-94 ----------------------------------------------------------

    def _n2k_94(self, frame, timestamp, direction):
        if len(frame) < 8:
            self._issue("unsupported BST message 0x94 length")
            self.invalid += 1
            return None
        length = frame[1]
        data_length = frame[7]
        expected = length + 2
        body = _checksummed(frame, expected)
        if body is None or length != data_length + 6:
            self._issue("BST-94 length does not match record")
            self.invalid += 1
            return None
        pgn = _pgn(body[5] & 3, body[4], body[3])
        destination = body[6]
        priority = body[2] & 7
        stamp = timestamp or datetime(1970, 1, 1, tzinfo=timezone.utc)
        # BST-94 is the host-to-gateway direction by definition.
        marker = "tx" if direction == "unknown" else direction
        self.direction_counts[marker] += 1
        if self.materialize is not None and not self.materialize(pgn):
            return (stamp, pgn, 255, marker, data_length)
        return N2KMessage(
            stamp,
            priority,
            pgn,
            destination,
            255,
            0,
            bytes(body[8 : 8 + data_length]),
            marker,
        )

    # -- BST-9D ----------------------------------------------------------

    def _nmea_9d(self, frame, timestamp, direction):
        if len(frame) < 3:
            self._issue("unsupported BST message 0x9d length")
            self.invalid += 1
            return None
        length = frame[1]
        expected = length + 2
        body = _checksummed(frame, expected)
        if body is None:
            self._issue("BST-9D length does not match record")
            self.invalid += 1
            return None
        text = bytes(body[2:]).strip(b"\r\n\x00")
        if not text.startswith((b"$", b"!")):
            self._issue("invalid BST-9D sentence")
            self.invalid += 1
            return None
        try:
            line = text.decode("ascii")
            packet = (
                AISDataPacket(line)
                if line.startswith(("!AIVDM,", "!AIVDO,"))
                else NMEADataPacket.parse(line)
            )
        except (ValueError, UnicodeDecodeError):
            self._issue("invalid BST-9D sentence")
            self.invalid += 1
            return None
        packet.source = None
        if timestamp is not None:
            packet.recorded_at = timestamp
        return packet

    # -- BST-D0 ----------------------------------------------------------

    def _n2k_d0(self, frame, timestamp, direction):
        if len(frame) < 13:
            self._issue(f"unsupported BST message 0x{frame[0]:02x} length")
            self.invalid += 1
            return None
        total = frame[1] | (frame[2] << 8)
        expected = total
        body = _checksummed(frame, expected)
        if body is None or total < 13:
            self._issue("BST-D0 length does not match record")
            self.invalid += 1
            return None
        destination, source = body[3], body[4]
        pgn = _pgn(body[7] & 3, body[6], body[5])
        priority = (body[7] >> 2) & 7
        control = body[8]
        marker = (
            direction
            if direction != "unknown"
            else _direction((control >> 3) & 1, direction)
        )
        millis = int.from_bytes(body[9:13], "little")
        stamp = timestamp or datetime(1970, 1, 1, tzinfo=timezone.utc)
        self.direction_counts[marker] += 1
        data = body[13:]
        if self.materialize is not None and not self.materialize(pgn):
            return (stamp, pgn, source, marker, len(data))
        return N2KMessage(
            stamp, priority, pgn, destination, source, millis, bytes(data), marker
        )


class BdtpStreamDecoder:
    """Extract BST datagrams from DLE-framed serial stream bytes.

    ``feed`` returns ``(messages, stray)``. Messages are complete BST blocks
    with the checksum already verified and removed. Stray bytes are the raw
    stream data outside BDTP framing, which the EBL layer offers to its
    plain-NMEA 0183 parser.
    """

    def __init__(self):
        self.buffer = bytearray()
        self.in_frame = False
        self.pending_dle = False
        self.frame = bytearray()
        self.diagnostics: List[str] = []
        self.invalid = 0

    def _issue(self, text):
        if len(self.diagnostics) < 100:
            self.diagnostics.append(text)

    def feed(self, chunk):
        messages = []
        stray = bytearray()
        for byte in chunk:
            if self.pending_dle:
                self.pending_dle = False
                if byte == DLE:
                    if self.in_frame:
                        self.frame.append(DLE)
                    else:
                        stray.append(DLE)
                    continue
                if byte == STX:
                    if self.in_frame:
                        self._issue("BDTP frame restarted before ETX")
                        self.invalid += 1
                    self.in_frame = True
                    self.frame.clear()
                    continue
                if byte == ETX:
                    if not self.in_frame:
                        self._issue("BDTP ETX without STX")
                        self.invalid += 1
                        continue
                    self.in_frame = False
                    block = bytes(self.frame)
                    self.frame.clear()
                    if len(block) < 2:
                        self._issue("truncated BDTP datagram")
                        self.invalid += 1
                        continue
                    if sum(block) & 0xFF:
                        self._issue("invalid BDTP checksum")
                        self.invalid += 1
                        continue
                    messages.append(block[:-1])
                    continue
                self._issue("unrecognised DLE escape")
                self.invalid += 1
                if self.in_frame:
                    self.in_frame = False
                    self.frame.clear()
                continue
            if byte == DLE:
                self.pending_dle = True
                continue
            if self.in_frame:
                self.frame.append(byte)
            else:
                stray.append(byte)
        return messages, bytes(stray)
