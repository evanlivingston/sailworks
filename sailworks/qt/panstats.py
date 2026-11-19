"""Opt-in pan-smoothness metrics.

The collector is off unless ``SAILWORKS_PAN_STATS`` is set, and while it is
off every entry point returns before it reads a clock, grows a list or
connects a signal, so an ordinary session pays nothing for it being present.

The value of the variable is either a switch - ``1`` / ``true`` / ``yes`` /
``on`` print the summary at application exit - or a file path, which prints
the same summary and appends the JSON record to that file as well.

Motion is classified by the camera, not by the frame: a presented frame (or
any other recorded event) counts as *moving* when the last ``camera_changed``
was less than ``MOVING_WINDOW_MS`` earlier.  The window matches the chart
settle clock's own idle window, so "moving" means the same thing to both.
"""

from __future__ import annotations

import json
import math
import os
import time

#: Set this to enable the collector.  Unset (or empty) is off.
ENV_VAR = "SAILWORKS_PAN_STATS"

#: A frame is moving when the camera changed inside this window.
MOVING_WINDOW_MS = 100.0

#: Values that mean "enabled, print only" rather than a file path.
_SWITCH_VALUES = frozenset({"1", "true", "yes", "on", "0", "false", "no", "off"})

_started = None
_last_event_at = None
_last_camera_at = None
_camera_changes = 0
_last_frame_at = None
_moving_intervals = []
_idle_intervals = []
_moving_seconds = 0.0
_idle_seconds = 0.0
_publishes_moving = 0
_holds = 0
_resumes = 0
_connections = {}
_quit_hooked = False


def reset():
    """Drop all collected state; safe whether enabled or not."""
    global _started, _last_event_at, _last_camera_at, _camera_changes
    global _last_frame_at, _moving_intervals, _idle_intervals
    global _moving_seconds, _idle_seconds, _publishes_moving, _holds, _resumes
    _started = None
    _last_event_at = None
    _last_camera_at = None
    _camera_changes = 0
    _last_frame_at = None
    _moving_intervals = []
    _idle_intervals = []
    _moving_seconds = 0.0
    _idle_seconds = 0.0
    _publishes_moving = 0
    _holds = 0
    _resumes = 0


def enabled():
    """Whether the collector was asked for.  Reads no clock."""
    return bool(os.environ.get(ENV_VAR, "").strip())


def _path():
    """The JSONL destination, or ``None`` when only printing."""
    value = os.environ.get(ENV_VAR, "").strip()
    if not value or value.lower() in _SWITCH_VALUES:
        return None
    return value


def _moving(now):
    if _last_camera_at is None:
        return False
    return (now - _last_camera_at) * 1000.0 < MOVING_WINDOW_MS


def _stamp(now):
    global _started, _last_event_at
    if _started is None:
        _started = now
    _last_event_at = now


def camera_changed(camera=None):
    """Record one camera adoption (``camera`` is accepted for readability)."""
    global _last_camera_at, _camera_changes
    if not enabled():
        return
    now = time.perf_counter()
    _stamp(now)
    _camera_changes += 1
    _last_camera_at = now


def frame_presented(render_ms=None):
    """Record one presented frame and its interval since the previous one.

    ``render_ms`` is the before->after render time when the caller knows it;
    it is accepted so the wiring can pass what it already computed, but the
    summary rates are built from presentation intervals, not from it.
    """
    global _last_frame_at, _moving_seconds, _idle_seconds
    if not enabled():
        return
    now = time.perf_counter()
    _stamp(now)
    previous = _last_frame_at
    _last_frame_at = now
    if previous is None:
        return
    interval_ms = (now - previous) * 1000.0
    if _moving(now):
        _moving_intervals.append(interval_ms)
        _moving_seconds += interval_ms / 1000.0
    else:
        _idle_intervals.append(interval_ms)
        _idle_seconds += interval_ms / 1000.0


def published(kind=None):
    """Record a publication; ``kind`` names the source for the caller."""
    global _publishes_moving
    if not enabled():
        return
    now = time.perf_counter()
    _stamp(now)
    if _moving(now):
        _publishes_moving += 1


def held(kind=None):
    """Record a publication held back, e.g. for a camera pause."""
    global _holds
    if not enabled():
        return
    _stamp(time.perf_counter())
    _holds += 1


def resumed(kind=None):
    """Record a held publication released to the ordinary path."""
    global _resumes
    if not enabled():
        return
    _stamp(time.perf_counter())
    _resumes += 1


def _rate(count, seconds):
    if count <= 0 or seconds <= 0.0:
        return 0.0
    return count / seconds


def _percentile(values, percent):
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, int(math.ceil(percent / 100.0 * len(ordered))))
    return ordered[rank - 1]


def summary():
    """The fields of the summary line, rounded to one decimal.

    Returns ``{}`` while disabled, without reading a clock.
    """
    if not enabled():
        return {}
    now = time.perf_counter()
    elapsed = 0.0 if _started is None else max(0.0, now - _started)
    return {
        "camera_hz": round(_rate(_camera_changes, elapsed), 1),
        "frames_moving_hz": round(_rate(len(_moving_intervals), _moving_seconds), 1),
        "frame_p50_ms": round(_percentile(_moving_intervals, 50), 1),
        "frame_p95_ms": round(_percentile(_moving_intervals, 95), 1),
        "frames_idle_hz": round(_rate(len(_idle_intervals), _idle_seconds), 1),
        "publishes_moving": _publishes_moving,
        "holds": _holds,
        "resumes": _resumes,
        "session_s": round(elapsed, 1),
    }


def summary_line(record=None):
    """The one comparison line, with a stable field order."""
    if not enabled():
        return ""
    record = summary() if record is None else record
    return (
        "panstats: "
        "camera_hz={camera_hz:.1f} "
        "frames_moving_hz={frames_moving_hz:.1f} "
        "frame_p50_ms={frame_p50_ms:.1f} "
        "frame_p95_ms={frame_p95_ms:.1f} "
        "frames_idle_hz={frames_idle_hz:.1f} "
        "publishes_moving={publishes_moving:d} "
        "holds={holds:d} "
        "resumes={resumes:d} "
        "session_s={session_s:.1f}"
    ).format(**record)


def flush():
    """Print the summary and append the JSON record when a path is set."""
    if not enabled():
        return ""
    record = summary()
    line = summary_line(record)
    print(line, flush=True)
    path = _path()
    if path:
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError:
            pass
    return line


def _default_frame_slot():
    frame_presented()


def _direct_connection():
    try:
        from PySide6.QtCore import Qt
    except ImportError:
        return None
    return Qt.ConnectionType.DirectConnection


def _frame_source(window):
    """The afterRendering signal, whether handed a widget or a window."""
    signal = getattr(window, "afterRendering", None)
    if signal is not None and hasattr(signal, "connect"):
        return signal
    getter = getattr(window, "quickWindow", None)
    quick = getter() if callable(getter) else None
    if quick is None:
        return None
    signal = getattr(quick, "afterRendering", None)
    if signal is not None and hasattr(signal, "connect"):
        return signal
    return None


def _hook_quit():
    """Print once at application exit; a no-op without a Qt application."""
    global _quit_hooked
    if _quit_hooked:
        return
    try:
        from PySide6.QtCore import QCoreApplication
    except ImportError:
        return
    app = QCoreApplication.instance()
    if app is None:
        return
    try:
        app.aboutToQuit.connect(_on_quit)
    except (RuntimeError, TypeError):
        return
    _quit_hooked = True


def _on_quit():
    flush()


def connect_window(window, on_frame=None):
    """Follow ``window``'s presented frames; returns a key or ``None``.

    Mirrors the trace's ``afterRendering`` hook: connected directly, once per
    window, and disconnected again by ``disconnect_window`` or by the window's
    own ``destroyed`` signal.  While disabled nothing is connected.
    """
    if not enabled() or window is None:
        return None
    key = id(window)
    if key in _connections:
        return None
    source = _frame_source(window)
    if source is not None:
        slot = on_frame if on_frame is not None else _default_frame_slot
        connection = _direct_connection()
        try:
            if connection is None:
                source.connect(slot)
            else:
                source.connect(slot, connection)
        except (RuntimeError, TypeError):
            return None
        _connections[key] = (source, slot)
    destroyed = getattr(window, "destroyed", None)
    if destroyed is not None and hasattr(destroyed, "connect"):
        try:
            destroyed.connect(lambda *_: disconnect_window(window))
        except (RuntimeError, TypeError):
            pass
    _hook_quit()
    return key


def ensure_connected(widget):
    """Wire ``widget``'s presented frames once, without ever raising.

    A no-op while the collector is disabled, idempotent by window identity,
    and tolerant of a missing or already destroyed wrapper, so a caller may
    invoke it on every camera adoption - including from a test double that
    knows nothing about metrics.  The widget's own
    ``_panstats_after_rendering`` slot is preferred when it has one, so the
    viewport keeps reporting its render time; otherwise the default slot is
    used.
    """
    if not enabled() or widget is None:
        return None
    try:
        on_frame = getattr(widget, "_panstats_after_rendering", None)
        return connect_window(widget, on_frame)
    except (RuntimeError, AttributeError, TypeError):
        return None


def disconnect_window(window):
    """Drop the connection made by ``connect_window``; idempotent."""
    if window is None:
        return False
    entry = _connections.pop(id(window), None)
    if entry is None:
        return False
    source, slot = entry
    try:
        source.disconnect(slot)
    except (RuntimeError, TypeError):
        pass
    return True
