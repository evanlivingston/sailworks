"""Streaming reader for EBL metatags containing raw BST-95 CAN frames.

Serial/BDTP streams and fast-packet reassembly are not supported. EBL TimeUTC
is authoritative: BST-95's wrapping device clock has no defined UTC epoch.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from .nmea2000 import N2KMessage


ESC = 0x1B
FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
MAX_TAG_SIZE = 1800  # On-disk payload size, including ESC stuffing.


@dataclass(frozen=True)
class EBLDecodeResult:
    messages: tuple
    diagnostics: tuple
    records: int


class EBLDecoder:
    """Incrementally decode ESC-stuffed metatags, diagnosing bad records."""

    def __init__(self):
        self.buffer = bytearray()
        self.timestamp: Optional[datetime] = None
        self.version: Optional[int] = None
        self.diagnostics: List[str] = []
        self.records = 0

    def feed(self, chunk: bytes) -> List[N2KMessage]:
        self.buffer.extend(chunk)
        messages = []
        while self.buffer:
            start, partial_escape = self._find_start()
            if start != 0:
                # Retain a split start delimiter, but discard unsupported raw
                # stream data (including escaped ESCs) as a single region.
                discard = start if start >= 0 else len(self.buffer)
                if start < 0 and partial_escape:
                    discard -= 1
                if discard:
                    del self.buffer[:discard]
                    self.diagnostics.append("discarded bytes outside EBL records")
                if start < 0:
                    break

            payload = bytearray()
            index = 2
            while index < len(self.buffer):
                if index - 2 > MAX_TAG_SIZE:
                    self.diagnostics.append("discarded oversized EBL metatag")
                    del self.buffer[:index]
                    break
                escape_index = self.buffer.find(ESC, index)
                if escape_index < 0:
                    # Keep the original pre-consumption size check cadence:
                    # reject at the first logical byte past the limit.
                    limit = 2 + MAX_TAG_SIZE + 1
                    end = min(len(self.buffer), limit)
                    payload.extend(self.buffer[index:end])
                    index = end
                    continue
                if escape_index > index:
                    limit = 2 + MAX_TAG_SIZE + 1
                    end = min(escape_index, limit)
                    payload.extend(self.buffer[index:end])
                    index = end
                    continue
                if index + 1 == len(self.buffer):
                    return messages
                following = self.buffer[index + 1]
                if following == ESC:
                    payload.append(ESC)
                    index += 2
                    continue
                if following == 0x0A:
                    del self.buffer[: index + 2]
                    self.records += 1
                    message = self._decode_tag(bytes(payload))
                    if message is not None:
                        messages.append(message)
                    break
                self.diagnostics.append("malformed escape in EBL metatag")
                # Preserve a new ESC SOH so a truncated record cannot swallow
                # the next valid record. Other malformed pairs are discarded.
                del self.buffer[: index if following == 0x01 else index + 2]
                break
            else:
                return messages  # Incomplete tag: wait for its end delimiter.
        return messages

    def _find_start(self):
        """Do not mistake ESC ESC SOH in unsupported raw data for a tag."""
        index = 0
        while index < len(self.buffer):
            index = self.buffer.find(ESC, index)
            if index < 0:
                return -1, False
            if index + 1 == len(self.buffer):
                return -1, True
            following = self.buffer[index + 1]
            if following == 0x01:
                return index, False
            index += 2  # Includes escaped ESC and malformed escape pairs.
        return -1, False

    def _decode_tag(self, payload):
        if not payload:
            self.diagnostics.append("empty EBL metatag")
            return None
        tag, data = payload[0], payload[1:]
        if tag == 0x01:
            self.version = None
            if len(data) != 4:
                self.diagnostics.append("invalid EBL Version length")
            else:
                self.version = int.from_bytes(data, "little")
                if self.version != 1002:
                    self.diagnostics.append(f"unsupported EBL version {self.version}")
        elif tag == 0x03:
            self.timestamp = None
            if len(data) != 8:
                self.diagnostics.append("invalid EBL TimeUTC length")
            else:
                try:
                    # datetime has microsecond precision; truncate the final
                    # 100ns digit without introducing float/Unix epoch rounding.
                    self.timestamp = FILETIME_EPOCH + timedelta(
                        microseconds=int.from_bytes(data, "little") // 10
                    )
                except OverflowError:
                    self.diagnostics.append("invalid EBL timestamp")
        elif tag == 0x07:
            if self.version != 1002:
                self.diagnostics.append("BSTRawFrame requires EBL Version 1002")
            else:
                return self._decode_can(data)
        elif tag in (0x02, 0x04, 0x05):
            # Description and indexing/direction hints do not affect replay.
            if tag != 0x02 and len(data) != 1:
                self.diagnostics.append(f"invalid EBL tag 0x{tag:02x} length")
        else:
            self.diagnostics.append(f"unsupported EBL record tag 0x{tag:02x}")
        return None

    def _decode_can(self, frame):
        if len(frame) < 2:
            self.diagnostics.append("truncated BST header")
            return None
        if frame[0] != 0x95:
            self.diagnostics.append(f"unsupported BST message ID 0x{frame[0]:02x}")
            return None
        length = frame[1]
        if not 6 <= length <= 14:
            self.diagnostics.append(f"unsupported BST-95 length {length}")
            return None
        expected = length + 2
        # Direct-device W2K records have no serial checksum. Also accept a
        # trailing BST checksum from writers retaining serial message bytes.
        if len(frame) == expected + 1:
            if sum(frame) & 0xFF:
                self.diagnostics.append("invalid BST-95 checksum")
                return None
            frame = frame[:-1]
        elif len(frame) != expected:
            self.diagnostics.append("BST-95 length does not match record")
            return None
        source, pdus, pduf, dppc = frame[4:8]
        priority = (dppc >> 2) & 7
        pgn = ((dppc & 3) << 16) | (pduf << 8)
        destination = pdus if pduf < 240 else 255
        if pduf >= 240:
            pgn |= pdus
        # This is a wrapping device counter, NOT milliseconds since midnight
        # and NOT an offset to add to TimeUTC. Preserve it at the container's
        # millisecond precision, decoding DPPC's resolution control bits.
        resolution_us = (1000, 100, 10, 1)[(dppc >> 5) & 3]
        millis = int.from_bytes(frame[2:4], "little") * resolution_us // 1000
        timestamp = self.timestamp or datetime(1970, 1, 1, tzinfo=timezone.utc)
        return N2KMessage(
            timestamp, priority, pgn, destination, source, millis, frame[8:]
        )

    def result(self, messages=()):
        return EBLDecodeResult(tuple(messages), tuple(self.diagnostics), self.records)
