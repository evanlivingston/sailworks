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
    pgn: int
    source: int
    destination: int
    source_timestamp_ms: int
    started_at: object
    direction: str
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
        """Return a completed :class:`N2KMessage`, or ``None``.

        ``message`` is either an :class:`N2KMessage` or the lightweight
        ``(timestamp, pgn, source, priority, destination, source_ms, data,
        direction)`` tuple emitted for fast-packet fragments.
        """
        if isinstance(message, tuple):
            (
                timestamp,
                pgn,
                source,
                priority,
                destination,
                source_ms,
                data,
                direction,
            ) = message
        else:
            timestamp = message.timestamp
            pgn = message.pgn
            source = message.source
            priority = message.priority
            destination = message.destination
            source_ms = message.source_timestamp_ms
            data = message.data
            direction = message.direction
        if pgn not in self.fast_pgns:
            return message
        if len(data) < 2:
            self._issue("truncated fast-packet frame")
            self.dropped_frames += 1
            return None
        self.fragments += 1
        now = self._seconds(timestamp)
        if self.fragments % 64 == 0:
            self._expire(timestamp)
        sequence = data[0] >> 5
        index = data[0] & 0x1F
        key = (source, pgn, sequence)
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
                bytearray(data[2 : 2 + length]),
                priority,
                pgn,
                source,
                destination,
                source_ms,
                timestamp,
                direction,
            )
            self.pending[key] = pending
            if len(pending.data) >= length:
                return self._complete(key)
            return None
        pending = self.pending.get(key)
        if pending is None:
            self._issue(
                f"orphaned fast-packet fragment pgn={pgn} source={source} index={index}"
            )
            self.dropped_frames += 1
            return None
        if now - pending.started > self.timeout or now < pending.started:
            self.dropped_frames += pending.frames + 1
            del self.pending[key]
            self._issue(
                f"timed out incomplete fast packet pgn={pending.pgn} source={pending.source} sequence={pending.sequence}"
            )
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
            return self._complete(key)
        return None

    def _complete(self, key):
        pending = self.pending.pop(key, None)
        if pending is None:
            return None
        self.assembled += 1
        return N2KMessage(
            pending.started_at,
            pending.priority,
            pending.pgn,
            pending.destination,
            pending.source,
            pending.source_timestamp_ms,
            bytes(pending.data[: pending.length]),
            pending.direction,
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
            pending = self.pending.pop(key)
            self.dropped_frames += pending.frames
            self._issue(
                f"timed out incomplete fast packet pgn={pending.pgn} "
                f"source={pending.source} sequence={pending.sequence}"
            )

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
