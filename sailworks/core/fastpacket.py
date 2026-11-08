"""Bounded NMEA 2000 fast-packet reassembly for raw CAN captures.

Only PGNs published as fast-packet are assembled. Assembly is strict and
in-order per ``(source, pgn, sequence)``; reordered, restarted, duplicate or
expired sequences are dropped with a bounded diagnostic instead of guessed.
"""

from collections import OrderedDict
from dataclasses import dataclass

from .nmea2000 import N2KMessage

MAX_PENDING = 64
MAX_LENGTH = 223
TIMEOUT_SECONDS = 2.0


@dataclass
class _Pending:
    sequence: int
    expected: int
    length: int
    started: float
    data: bytearray
    priority: int
    destination: int
    source_timestamp_ms: int
    started_at: object
    frames: int = 1


class FastPacketAssembler:
    """Reassemble NMEA 2000 fast packets from 8-byte CAN frames."""

    def __init__(self, fast_pgns, max_pending=MAX_PENDING, timeout=TIMEOUT_SECONDS):
        self.fast_pgns = fast_pgns
        self.max_pending = max_pending
        self.timeout = timeout
        self.pending = OrderedDict()
        self.diagnostics = []
        self.assembled = 0
        self.fragments = 0
        self.dropped_frames = 0

    def _issue(self, text):
        if len(self.diagnostics) < 100:
            self.diagnostics.append(text)

    def add(self, message):
        """Return a completed :class:`N2KMessage`, or ``None``."""
        if message.pgn not in self.fast_pgns:
            return message
        data = message.data
        if len(data) < 2:
            self._issue("truncated fast-packet frame")
            self.dropped_frames += 1
            return None
        self.fragments += 1
        now = self._seconds(message.timestamp)
        if self.fragments % 64 == 0:
            self._expire(message.timestamp)
        sequence = data[0] & 0x07
        index = data[0] >> 5
        key = (message.source, message.pgn, sequence)
        if index == 0:
            if key in self.pending:
                self.dropped_frames += self.pending.pop(key).frames
                self._issue("restarted fast-packet sequence")
            length = data[1]
            if not 1 <= length <= MAX_LENGTH:
                self._issue(f"invalid fast-packet length {length}")
                self.dropped_frames += 1
                return None
            if len(self.pending) >= self.max_pending:
                self.dropped_frames += self.pending.popitem(last=False)[1].frames
                self._issue("fast-packet pending limit reached")
            pending = _Pending(
                sequence,
                1,
                length,
                now,
                bytearray(data[2 : 1 + length]),
                message.priority,
                message.destination,
                message.source_timestamp_ms,
                message.timestamp,
            )
            self.pending[key] = pending
            if len(pending.data) >= length:
                return self._complete(key, message)
            return None
        pending = self.pending.get(key)
        if pending is None:
            self._issue("orphaned fast-packet fragment")
            self.dropped_frames += 1
            return None
        if now - pending.started > self.timeout or now < pending.started:
            self.dropped_frames += pending.frames + 1
            del self.pending[key]
            self._issue("timed out incomplete fast packet")
            return None
        if index != pending.expected:
            self.dropped_frames += pending.frames + 1
            del self.pending[key]
            self._issue(
                "duplicate fast-packet fragment"
                if index < pending.expected
                else "out-of-order fast-packet fragment"
            )
            return None
        pending.expected += 1
        pending.frames += 1
        remaining = pending.length - len(pending.data)
        pending.data.extend(data[1 : 1 + remaining])
        if len(pending.data) >= pending.length:
            return self._complete(key, message)
        return None

    def _complete(self, key, message):
        pending = self.pending.pop(key, None)
        if pending is None:
            return None
        self.assembled += 1
        return N2KMessage(
            pending.started_at,
            pending.priority,
            message.pgn,
            pending.destination,
            message.source,
            pending.source_timestamp_ms,
            bytes(pending.data[: pending.length]),
            message.direction,
        )

    def _seconds(self, timestamp):
        try:
            return timestamp.timestamp()
        except (AttributeError, OSError, OverflowError, ValueError):
            return 0.0

    def _expire(self, timestamp):
        now = self._seconds(timestamp)
        expired = [
            key
            for key, pending in self.pending.items()
            if now - pending.started > self.timeout or now < pending.started
        ]
        for key in expired:
            self.dropped_frames += self.pending.pop(key).frames
            self._issue("timed out incomplete fast packet")

    def drain_unsupported(self):
        """Return rejected/incomplete frame count for the finished session."""
        count = self.dropped_frames + sum(
            pending.frames for pending in self.pending.values()
        )
        self.dropped_frames = 0
        self.pending.clear()
        return count

    def finish(self):
        if self.pending:
            self._issue("incomplete fast packet at end of input")
            self.pending.clear()
