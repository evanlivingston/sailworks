"""Receive-only Actisense BST framing for NMEA 2000 PGN messages.

Wire layout and PGN units follow the public CANboat protocol definitions:
https://github.com/canboat/canboat (actisense-serial and docs/canboat.json).
No CANboat executable is required. NGT-1 firmware reassembles fast packets.
"""

from datetime import datetime, timezone
from typing import List, Optional

from .nmea2000 import N2KMessage

DLE = 0x10


def frame(command: int, payload: bytes) -> bytes:
    """Encode an Actisense BST frame, including checksum and DLE stuffing."""
    body = bytes((command, len(payload))) + payload
    body += bytes((-sum(body) & 0xFF,))
    return b"\x10\x02" + body.replace(b"\x10", b"\x10\x10") + b"\x10\x03"


class ActisenseDecoder:
    """Incremental, bounded parser that recovers at the next frame boundary."""

    def __init__(self):
        self.buffer = bytearray()
        self.active = False
        self.escaped = False
        self.invalid_frames = 0

    def feed(
        self, chunk: bytes, timestamp: Optional[datetime] = None
    ) -> List[N2KMessage]:
        messages = []
        for b in chunk:
            if self.escaped:
                self.escaped = False
                if b == 2:
                    self.active = True
                    self.buffer.clear()
                elif self.active and b == DLE:
                    self.buffer.append(b)
                elif self.active and b == 3:
                    msg = self._decode(timestamp or datetime.now(timezone.utc))
                    if msg is not None:
                        messages.append(msg)
                    self.active = False
                    self.buffer.clear()
                else:
                    self.active = False
                    self.buffer.clear()
            elif b == DLE:
                self.escaped = True
            elif self.active:
                self.buffer.append(b)
            if len(self.buffer) > 260:
                self.invalid_frames += 1
                self.active = False
                self.buffer.clear()
        return messages

    def _decode(self, timestamp):
        b = self.buffer
        if len(b) < 3 or sum(b) & 0xFF:
            self.invalid_frames += 1
            return None
        if b[0] != 0x93:  # N2K_MSG_RECEIVED; ignore gateway status/replies
            return None
        payload = b[2:-1]
        if len(payload) < 11:
            self.invalid_frames += 1
            return None
        length = payload[10]
        # Some NGT-1 firmware leaves BST length at 19 for fast packets.
        if (
            length > 223
            or len(payload) != 11 + length
            or (b[1] != len(payload) and not (b[1] == 19 and length > 8))
        ):
            self.invalid_frames += 1
            return None
        return N2KMessage(
            timestamp,
            payload[0],
            int.from_bytes(payload[1:4], "little"),
            payload[4],
            payload[5],
            int.from_bytes(payload[6:10], "little"),
            bytes(payload[11:]),
        )
