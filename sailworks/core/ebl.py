"""Streaming reader for the public Actisense EBL container format.

The container yields decoded BST payloads and plain NMEA 0183 data between
metatags. EBL TimeUTC is authoritative: BST-95's wrapping device clock has no
defined UTC epoch.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from .bst import BdtpStreamDecoder, BstDecoder
from .datasource import AISDataPacket, NMEADataPacket

ESC = 0x1B
FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
MAX_TAG_SIZE = 1800  # On-disk payload size, including ESC stuffing.
EBL_VERSION = 1002
MAX_SENTENCE_LENGTH = 1024


@dataclass(frozen=True)
class EBLDecodeResult:
    messages: tuple
    diagnostics: tuple
    records: int


class EBLDecoder:
    """Incrementally decode ESC-stuffed metatags and raw stream data."""

    def __init__(self, materialize=None, fast_pgns=(), inventory_pgns=()):
        self.buffer = bytearray()
        self.timestamp: Optional[datetime] = None
        self.version: Optional[int] = None
        self.description: Optional[str] = None
        self.direction = "unknown"
        self.diagnostics: List[str] = []
        self.records = 0
        self.discarded = 0
        self._bdtp = BdtpStreamDecoder()
        self._bst = BstDecoder(
            materialize=materialize,
            fast_pgns=fast_pgns,
            inventory_pgns=inventory_pgns,
        )
        self._sentence = bytearray()
        self._bst_seen = 0
        self._bdtp_seen = 0

    # -- input -----------------------------------------------------------

    def feed(self, chunk: bytes) -> List[object]:
        self.buffer.extend(chunk)
        messages: List[object] = []
        position = 0
        size = len(self.buffer)
        while position < size:
            escape = self.buffer.find(ESC, position)
            if escape < 0:
                self._stream(bytes(self.buffer[position:size]), messages)
                position = size
                break
            if escape > position:
                self._stream(bytes(self.buffer[position:escape]), messages)
                position = escape
                continue
            if position + 1 >= size:
                break
            following = self.buffer[position + 1]
            if following == 0x01:
                consumed = self._tag(position, size, messages)
                if consumed is None:
                    break
                position = consumed
            elif following == ESC:
                self._stream(b"\x1b", messages)
                position += 2
            else:
                self.diagnostics.append("malformed escape in EBL stream")
                position += 2
        if position:
            del self.buffer[:position]
        self._collect()
        return messages

    def _tag(self, position, size, messages):
        """Consume one metatag. Return the next position, or None for more."""
        index = position + 2
        while True:
            escape = self.buffer.find(ESC, index)
            if escape < 0:
                if size - position > MAX_TAG_SIZE + 3:
                    self.diagnostics.append("discarded oversized EBL metatag")
                    return position + MAX_TAG_SIZE + 3
                return None
            if escape + 1 >= size:
                return None
            following = self.buffer[escape + 1]
            if following == ESC:
                index = escape + 2
                continue
            if following == 0x0A:
                payload_start = position + 2
                if self.buffer.find(ESC, payload_start, escape) < 0:
                    raw = memoryview(self.buffer)[payload_start:escape]
                else:
                    raw = bytes(self.buffer[payload_start:escape]).replace(
                        b"\x1b\x1b", b"\x1b"
                    )
                self.records += 1
                self._dispatch(raw, messages)
                return escape + 2
            self.diagnostics.append("malformed escape in EBL metatag")
            # Preserve a new ESC SOH so a truncated record cannot swallow the
            # next valid record. Other malformed pairs are discarded.
            return escape if following == 0x01 else escape + 2

    def _dispatch(self, payload, messages):
        if not payload:
            self.diagnostics.append("empty EBL metatag")
            return
        tag, data = payload[0], payload[1:]
        if tag == 0x01:
            self.version = None
            if len(data) != 4:
                self.diagnostics.append("invalid EBL Version length")
            else:
                self.version = int.from_bytes(data, "little")
                if self.version != EBL_VERSION:
                    self.diagnostics.append(f"unsupported EBL version {self.version}")
        elif tag == 0x02:
            self.description = bytes(data).decode("utf-8", errors="replace")
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
        elif tag == 0x04:
            if len(data) != 1:
                self.diagnostics.append("invalid EBL tag 0x04 length")
        elif tag == 0x05:
            if len(data) != 1:
                self.diagnostics.append("invalid EBL tag 0x05 length")
            elif data[0] == 0x00:
                self.direction = "rx"
            elif data[0] == 0x01:
                self.direction = "tx"
            else:
                self.diagnostics.append("invalid EBL DirectionMarker")
        elif tag == 0x07:
            if self.version != EBL_VERSION:
                self.diagnostics.append("BSTRawFrame requires EBL Version 1002")
            else:
                message = self._bst.decode(data, self.timestamp, self.direction)
                if message is not None:
                    messages.append(message)
        else:
            # Unknown tag IDs are skip-and-continue per the format spec.
            self.diagnostics.append(f"unsupported EBL record tag 0x{tag:02x}")

    # -- raw stream ------------------------------------------------------

    def _collect(self):
        while self._bst_seen < len(self._bst.diagnostics):
            self.diagnostics.append(self._bst.diagnostics[self._bst_seen])
            self._bst_seen += 1
        while self._bdtp_seen < len(self._bdtp.diagnostics):
            self.diagnostics.append(self._bdtp.diagnostics[self._bdtp_seen])
            self._bdtp_seen += 1

    def _stream(self, data, messages):
        frames, stray = self._bdtp.feed(data)
        for frame in frames:
            if self.version != EBL_VERSION:
                self.diagnostics.append("EBL stream payload requires EBL Version 1002")
                break
            message = self._bst.decode(frame, self.timestamp, self.direction)
            if message is not None:
                messages.append(message)
        if stray:
            self._sentences(stray, messages)

    def _sentences(self, data, messages):
        self._sentence.extend(data)
        while True:
            end = self._sentence.find(b"\n")
            if end < 0:
                break
            line = bytes(self._sentence[:end]).strip(b"\r\x00 ")
            del self._sentence[: end + 1]
            self._sentence_line(line, messages)
        if len(self._sentence) > MAX_SENTENCE_LENGTH:
            self.discarded += len(self._sentence)
            self._sentence.clear()
            self.diagnostics.append("discarded oversized NMEA 0183 stream data")

    def _sentence_line(self, line, messages):
        if not line:
            return
        if not line.startswith((b"$", b"!")):
            self.discarded += len(line)
            return
        try:
            text = line.decode("ascii")
            packet = (
                AISDataPacket(text)
                if text.startswith(("!AIVDM,", "!AIVDO,"))
                else NMEADataPacket.parse(text)
            )
        except (ValueError, UnicodeDecodeError):
            self.discarded += len(line)
            return
        packet.direction = self.direction
        if self.timestamp is not None:
            packet.recorded_at = self.timestamp
        messages.append(packet)

    # -- completion ------------------------------------------------------

    def finish(self) -> List[object]:
        messages: List[object] = []
        if self._sentence:
            line = bytes(self._sentence).strip(b"\r\x00 ")
            self._sentence.clear()
            self._sentence_line(line, messages)
        self._collect()
        if self.discarded:
            self.diagnostics.append(
                f"discarded {self.discarded:,} bytes outside EBL records"
            )
        return messages

    def result(self, messages=()):
        return EBLDecodeResult(tuple(messages), tuple(self.diagnostics), self.records)
