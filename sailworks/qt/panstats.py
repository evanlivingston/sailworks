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

A long gap between two presented frames is a *hitch*.  Each hitch keeps the
gap and the markers (publications, holds, resumes, camera adoptions) that
occurred between the two frames, so a spike in the frame percentiles can be
attributed to the work that caused it instead of being a bare number.

The per-camera handler is timed by its caller and handed in as
``elapsed_ms``, so the collector never owns a second clock over the work.
Those times are summarised (p50/p95/max) and attributed to the frame window
they fell in: ``camera_busy_pct`` is handler time over moving-frame wall time,
and each hitch record carries the handler time inside its gap.

The viewport's own paint is timed by its caller (``painted``) and Qt Quick's
render by the ``beforeRendering``/``afterRendering`` pair, so a slow frame can
be split between compositing the rendered texture and producing it.  Both are
summarised as p50/p95 and summed inside each hitch gap.

Every repaint request is counted by its caller (``repaint_requested``), split
by whether the camera was moving at the time.  Comparing the request rate with
the presented-frame rate shows whether frames are rare because requests are
rare or because Qt coalesces or drops the requests we do make.  Each hitch
record also carries the requests that fell inside its gap.
"""

from __future__ import annotations

import faulthandler
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time

#: Set this to enable the collector.  Unset (or empty) is off.
ENV_VAR = "SAILWORKS_PAN_STATS"

#: No frame for longer than this, with the camera still warm, is a stall.
PAN_STATS_STALL_S = 0.5

#: Override the stall threshold (seconds), e.g. to skip startup gaps.
ENV_STALL_S = "SAILWORKS_PAN_STATS_STALL_S"

#: Ignore stall captures during the first N seconds of a session.
ENV_STALL_AFTER_S = "SAILWORKS_PAN_STATS_STALL_AFTER_S"

#: How often the watchdog thread samples the frame and camera timestamps.
PAN_STATS_STALL_TICK_S = 0.1

#: How often the GUI thread refreshes the heartbeat.
HEARTBEAT_MS = 100

#: How many stall captures a session may write before the watchdog gives up.
PAN_STATS_STALL_DUMPS = 5

#: The macOS sampler; missing on other platforms, where capture is skipped.
SAMPLE_BIN = "/usr/bin/sample"

#: A frame is moving when the camera changed inside this window.
MOVING_WINDOW_MS = 100.0

#: A moving-frame interval longer than this is recorded as a hitch.
HITCH_MS = 100.0

#: How many recent markers the attribution ring keeps.
RING_SIZE = 64

#: How many recent paint/render samples the attribution ring keeps.
SAMPLE_RING_SIZE = 256

#: How many markers a single hitch record may carry.
HITCH_MARKERS_MAX = 8

#: How many hitch records the JSON detail keeps.
HITCH_RECORDS_MAX = 20

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
_raster_skips = 0
_repaints = 0
_repaints_moving = 0
_repaint_ring = None
_holds = 0
_resumes = 0
_marker_ring = None
_hitch_count = 0
_hitch_max_ms = 0.0
_hitch_kind_counts = {}
_hitch_records = []
_connections = {}
_quit_hooked = False
_camera_ms = []
_camera_ms_pending = 0.0
_camera_ms_moving = 0.0
_paint_ms = []
_paint_ring = None
_render_ms = []
_render_ring = None
_render_basis = None
_render_connections = {}
_heartbeat_at = None
_stall_dumped = False
_stall_dumps = 0
_watchdog = None
_watchdog_stop = None
_heartbeat_timer = None
_sampler = None


def reset():
    """Drop all collected state; safe whether enabled or not."""
    global _started, _last_event_at, _last_camera_at, _camera_changes
    global _last_frame_at, _moving_intervals, _idle_intervals
    global _moving_seconds, _idle_seconds, _publishes_moving, _raster_skips
    global _repaints, _repaints_moving, _repaint_ring
    global _holds, _resumes, _marker_ring, _hitch_count, _hitch_max_ms
    global _hitch_kind_counts, _hitch_records
    global _camera_ms, _camera_ms_pending, _camera_ms_moving
    global _paint_ms, _paint_ring, _render_ms, _render_ring
    global _render_basis, _render_connections
    global _heartbeat_at, _stall_dumped, _stall_dumps
    _stop_heartbeat_timer()
    _stop_watchdog()
    _stop_sampler()
    _heartbeat_at = None
    _stall_dumped = False
    _stall_dumps = 0
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
    _raster_skips = 0
    _repaints = 0
    _repaints_moving = 0
    _repaint_ring = None
    _holds = 0
    _resumes = 0
    _marker_ring = None
    _hitch_count = 0
    _hitch_max_ms = 0.0
    _hitch_kind_counts = {}
    _hitch_records = []
    _camera_ms = []
    _camera_ms_pending = 0.0
    _camera_ms_moving = 0.0
    _paint_ms = []
    _paint_ring = None
    _render_ms = []
    _render_ring = None
    _render_basis = None
    _render_connections = {}


def enabled():
    """Whether the collector was asked for.  Reads no clock."""
    return bool(os.environ.get(ENV_VAR, "").strip())


def _stall_threshold_s():
    """The stall threshold: the env override when valid, else the default."""
    raw = os.environ.get(ENV_STALL_S)
    if raw:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0.0:
            return value
    return PAN_STATS_STALL_S


def _stall_after_s():
    """How long to ignore stall captures at session start, in seconds."""
    raw = os.environ.get(ENV_STALL_AFTER_S)
    if raw:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0.0:
            return value
    return 0.0


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


def _mark(kind, now):
    """Append one attribution marker to the bounded ring."""
    global _marker_ring
    if _marker_ring is None:
        from collections import deque

        _marker_ring = deque(maxlen=RING_SIZE)
    _marker_ring.append((kind, now))


def camera_changed(camera=None, elapsed_ms=None):
    """Record one camera adoption and the handler time it took, if known."""
    global _last_camera_at, _camera_changes, _camera_ms_pending
    if not enabled():
        return
    now = time.perf_counter()
    _stamp(now)
    _camera_changes += 1
    _last_camera_at = now
    if elapsed_ms is not None:
        elapsed_ms = float(elapsed_ms)
        _camera_ms.append(elapsed_ms)
        _camera_ms_pending += elapsed_ms
    _mark("camera", now)


def frame_presented(render_ms=None):
    """Record one presented frame and its interval since the previous one.

    ``render_ms`` is the before->after render time when the caller knows it;
    a sample taken by the ``beforeRendering`` hook wins over one handed in.
    The summary rates are built from presentation intervals, not from it.
    """
    global _last_frame_at, _moving_seconds, _idle_seconds
    global _camera_ms_pending, _camera_ms_moving, _stall_dumped
    global _render_basis, _render_ring
    if not enabled():
        return
    now = time.perf_counter()
    _stamp(now)
    # A presented frame ends any stall episode, so the next drag may capture.
    _stall_dumped = False
    previous = _last_frame_at
    _last_frame_at = now
    render_sample = None
    if _render_basis is not None:
        render_sample = max(0.0, (now - _render_basis) * 1000.0)
        _render_basis = None
    elif render_ms is not None:
        render_sample = max(0.0, float(render_ms))
    if render_sample is not None:
        if _render_ring is None:
            from collections import deque

            _render_ring = deque(maxlen=SAMPLE_RING_SIZE)
        _render_ms.append(render_sample)
        _render_ring.append((now, render_sample))
    if previous is None:
        # Nothing has been presented yet, so no window has opened: handler
        # time before the first frame belongs to no interval.
        _camera_ms_pending = 0.0
        return
    interval_ms = (now - previous) * 1000.0
    camera_ms = _camera_ms_pending
    _camera_ms_pending = 0.0
    if _moving(now):
        _moving_intervals.append(interval_ms)
        _moving_seconds += interval_ms / 1000.0
        _camera_ms_moving += camera_ms
    else:
        _idle_intervals.append(interval_ms)
        _idle_seconds += interval_ms / 1000.0
    if interval_ms > HITCH_MS and (_moving(previous) or _moving(now)):
        _record_hitch(interval_ms, previous, now, camera_ms)


def _record_hitch(gap_ms, previous, now, camera_ms=0.0):
    """Keep one hitch, attributed to the markers inside its window.

    ``camera_ms`` is the total camera-handler time inside the gap, so a hitch
    caused by handler saturation can be read directly instead of inferred
    from the marker kinds alone.  ``paint_ms`` and ``render_ms`` likewise sum
    the viewport paint and Quick render samples taken inside the same gap,
    and ``repaints`` counts the repaint requests made inside it.
    """
    global _hitch_count, _hitch_max_ms
    _hitch_count += 1
    if gap_ms > _hitch_max_ms:
        _hitch_max_ms = gap_ms
    recent = [entry for entry in (_marker_ring or ()) if entry[1] > previous]
    window = recent[-HITCH_MARKERS_MAX:]
    markers = [
        {"kind": kind, "age_ms": round((now - at) * 1000.0, 1)} for kind, at in window
    ]
    _hitch_records.append(
        {
            "gap_ms": round(gap_ms, 1),
            "camera_ms": round(camera_ms, 1),
            "paint_ms": round(_window_sum(_paint_ring, previous, now), 1),
            "render_ms": round(_window_sum(_render_ring, previous, now), 1),
            "repaints": _window_count(_repaint_ring, previous, now),
            "markers": markers,
        }
    )
    if len(_hitch_records) > HITCH_RECORDS_MAX:
        del _hitch_records[: len(_hitch_records) - HITCH_RECORDS_MAX]
    for kind in dict.fromkeys(marker["kind"] for marker in markers):
        _hitch_kind_counts[kind] = _hitch_kind_counts.get(kind, 0) + 1


def painted(elapsed_ms):
    """Record one viewport widget paint, timed by its caller.

    The caller owns the clock so the collector keeps a single one over the
    paint: the wrapper reads ``perf_counter`` around ``super().paintEvent``
    and hands the duration in.  The same sample is kept for the hitch window
    it fell in, so a spike can be split between paint and Quick render.
    """
    global _paint_ring
    if not enabled():
        return
    now = time.perf_counter()
    _stamp(now)
    value = max(0.0, float(elapsed_ms))
    if _paint_ring is None:
        from collections import deque

        _paint_ring = deque(maxlen=SAMPLE_RING_SIZE)
    _paint_ms.append(value)
    _paint_ring.append((now, value))


def _render_started():
    """Stamp the start of a Quick render; only ever connected while enabled."""
    global _render_basis
    if not enabled():
        return
    _render_basis = time.perf_counter()


def published(kind=None):
    """Record a real publication; ``kind`` names the source for the caller."""
    global _publishes_moving
    if not enabled():
        return
    if kind == "raster_skip":
        # A skip publishes nothing: it would otherwise inflate the count.
        skipped(kind)
        return
    now = time.perf_counter()
    _stamp(now)
    if _moving(now):
        _publishes_moving += 1
    _mark("published(%s)" % (kind or "unknown",), now)


def skipped(kind=None):
    """Record a publication deliberately not made, e.g. a covered raster."""
    global _raster_skips
    if not enabled():
        return
    _stamp(time.perf_counter())
    _raster_skips += 1


def repaint_requested():
    """Record one repaint request made for the map.

    The caller owns the request - this only counts it - so the summary can say
    whether frames are rare because requests are rare or because Qt coalesces
    or drops them.  Requests are also kept in a bounded ring so a hitch can
    carry the ones that fell inside its gap.  No clock is read while disabled.
    """
    global _repaints, _repaints_moving, _repaint_ring
    if not enabled():
        return
    now = time.perf_counter()
    _stamp(now)
    _repaints += 1
    if _moving(now):
        _repaints_moving += 1
    if _repaint_ring is None:
        from collections import deque

        _repaint_ring = deque(maxlen=SAMPLE_RING_SIZE)
    _repaint_ring.append(now)


def held(kind=None):
    """Record a publication held back, e.g. for a camera pause."""
    global _holds
    if not enabled():
        return
    now = time.perf_counter()
    _stamp(now)
    _holds += 1
    _mark("held", now)


def resumed(kind=None):
    """Record a held publication released to the ordinary path."""
    global _resumes
    if not enabled():
        return
    now = time.perf_counter()
    _stamp(now)
    _resumes += 1
    _mark("resumed", now)


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


def _window_sum(ring, previous, now):
    """Total of the samples taken in ``(previous, now]``; zero without any."""
    if not ring:
        return 0.0
    return sum(ms for at, ms in ring if previous < at <= now)


def _window_count(ring, previous, now):
    """How many entries landed in ``(previous, now]``; zero without any."""
    if not ring:
        return 0
    return sum(1 for at in ring if previous < at <= now)


def _hitch_top():
    """The marker kind most often present in a hitch window, or ``none``."""
    if not _hitch_kind_counts:
        return "none"
    return max(_hitch_kind_counts, key=_hitch_kind_counts.get)


def _camera_busy_pct():
    """Camera-handler time as a percentage of moving-frame wall time."""
    if _moving_seconds <= 0.0:
        return 0.0
    return 100.0 * (_camera_ms_moving / 1000.0) / _moving_seconds


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
        "hitches": _hitch_count,
        "hitch_max_ms": round(_hitch_max_ms, 1),
        "hitch_top": _hitch_top(),
        "raster_skips": _raster_skips,
        "camera_ms_p50": round(_percentile(_camera_ms, 50), 1),
        "camera_ms_p95": round(_percentile(_camera_ms, 95), 1),
        "camera_ms_max": round(max(_camera_ms), 1) if _camera_ms else 0.0,
        "camera_busy_pct": round(_camera_busy_pct(), 1),
        "paint_ms_p50": round(_percentile(_paint_ms, 50), 1),
        "paint_ms_p95": round(_percentile(_paint_ms, 95), 1),
        "render_ms_p50": round(_percentile(_render_ms, 50), 1),
        "render_ms_p95": round(_percentile(_render_ms, 95), 1),
        "repaints": _repaints,
        "repaints_hz": round(_rate(_repaints, elapsed), 1),
        "repaints_moving": _repaints_moving,
        "repaints_moving_hz": round(_rate(_repaints_moving, _moving_seconds), 1),
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
        "session_s={session_s:.1f} "
        "hitches={hitches:d} "
        "hitch_max_ms={hitch_max_ms:.1f} "
        "hitch_top={hitch_top} "
        "raster_skips={raster_skips:d} "
        "camera_ms_p50={camera_ms_p50:.1f} "
        "camera_ms_p95={camera_ms_p95:.1f} "
        "camera_ms_max={camera_ms_max:.1f} "
        "camera_busy_pct={camera_busy_pct:.1f} "
        "paint_ms_p50={paint_ms_p50:.1f} "
        "paint_ms_p95={paint_ms_p95:.1f} "
        "render_ms_p50={render_ms_p50:.1f} "
        "render_ms_p95={render_ms_p95:.1f} "
        "repaints={repaints:d} "
        "repaints_hz={repaints_hz:.1f} "
        "repaints_moving={repaints_moving:d} "
        "repaints_moving_hz={repaints_moving_hz:.1f}"
    ).format(**record)


def flush():
    """Print the summary and append the JSON record when a path is set.

    The printed line and the summary fields stay in their existing order; the
    JSON record gains a bounded ``hitch_records`` detail list, because a
    single-line summary cannot carry per-hitch attribution.
    """
    if not enabled():
        return ""
    record = summary()
    line = summary_line(record)
    print(line, flush=True)
    path = _path()
    if path:
        detail = dict(record)
        detail["hitch_records"] = [dict(item) for item in _hitch_records]
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(detail) + "\n")
        except OSError:
            pass
    return line


def _default_frame_slot():
    frame_presented()


def heartbeat():
    """Mark the GUI thread alive so a recovered stall may capture again.

    Written from a coalesced ``QTimer`` on the GUI thread.  It is not the
    stall signal itself (frame and camera ages are), but it clears the latch
    as soon as the event loop runs again.
    """
    global _heartbeat_at, _stall_dumped
    if not enabled():
        return
    _heartbeat_at = time.perf_counter()
    # A beat proves the GUI thread recovered: the next stall may dump again.
    _stall_dumped = False


def _stall_path():
    """The dump destination: beside the stats file, or ``None`` for stderr."""
    path = _path()
    return None if path is None else path + ".stall"


def stall_header(age, camera_age, record=None, sample="ok"):
    """The one line that precedes a stall capture: ages plus summary fields."""
    record = summary() if record is None else record
    return (
        "panstats-stall: age_ms={age_ms:.1f} camera_ms={camera_ms:.1f} "
        "dump={dump:d}/{cap:d} sample={sample} {line}"
    ).format(
        age_ms=max(0.0, age) * 1000.0,
        camera_ms=max(0.0, camera_age) * 1000.0,
        dump=_stall_dumps,
        cap=PAN_STATS_STALL_DUMPS,
        sample=sample,
        line=summary_line(record),
    )


def _dump_to_stderr(header):
    """Write the header and every thread's stack to stderr, best effort."""
    try:
        sys.stderr.write(header + "\n")
        sys.stderr.flush()
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
    except (AttributeError, OSError, ValueError):
        # A captured stream has no descriptor; the header still went out.
        pass


def _spawn_sampler(native_path):
    """Start the native sampler beside the stacks; never waits, never raises."""
    global _sampler
    _stop_sampler()
    try:
        _sampler = subprocess.Popen(
            [SAMPLE_BIN, str(os.getpid()), "1", "1", "-file", native_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        _sampler = None
        return False
    return True


def _stop_sampler():
    """Terminate the child sampler if it is still running; idempotent."""
    global _sampler
    process, _sampler = _sampler, None
    if process is None:
        return False
    try:
        if process.poll() is None:
            process.terminate()
    except OSError:
        pass
    return True


def _capture_stall(age, camera_age):
    """Write one header plus all-thread stacks, then sample the natives.

    The Python stacks are written first so a native grab cannot delay them;
    the sampler runs beside the sidecar as ``<stats>.stall.native``.
    """
    global _stall_dumps
    _stall_dumps += 1
    path = _stall_path()
    sample = "unavailable" if path is None else "ok"
    if path is not None and shutil.which(SAMPLE_BIN) is None:
        sample = "missing"
    header = stall_header(age, camera_age, sample=sample)
    if path is None:
        _dump_to_stderr(header)
        return "stderr"
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(header + "\n")
            handle.flush()
            faulthandler.dump_traceback(file=handle, all_threads=True)
    except OSError:
        return None
    if sample == "ok":
        _spawn_sampler(path + ".native")
    return path


def check_stall(now=None):
    """Capture the stacks when a drag has stopped presenting frames.

    Stalled means both: no frame for more than the stall threshold and a
    camera change that arrived *after* the last presented frame.  The first
    separates a hang from an idle window; the second separates a drag from a
    freeze that began before it.  A camera event may itself be seconds old by
    the time the watchdog checks: the drag still has no frame, so the age of
    the event must not hide the stall.  One capture per episode, capped per
    session.  Returns the capture destination, or ``None``.
    """
    global _stall_dumped
    if not enabled():
        return None
    if now is None:
        now = time.perf_counter()
    frame_at = _last_frame_at
    camera_at = _last_camera_at
    if frame_at is None or camera_at is None:
        return None
    if _started is not None and now - _started < _stall_after_s():
        # Startup and first-load gaps are not interaction stalls.
        return None
    age = now - frame_at
    if age <= _stall_threshold_s():
        _stall_dumped = False
        return None
    if camera_at <= frame_at:
        # The camera changed before the last frame, so this is not a drag
        # waiting for a frame: the episode is over and a later drag may capture.
        _stall_dumped = False
        return None
    if _stall_dumps >= PAN_STATS_STALL_DUMPS:
        _stop_watchdog()
        return None
    if _stall_dumped:
        return None
    _stall_dumped = True
    return _capture_stall(age, now - camera_at)


def _watchdog_loop(stop):
    """Sample the heartbeat until ``stop`` is set or the dump cap is hit."""
    while not stop.wait(PAN_STATS_STALL_TICK_S):
        try:
            check_stall()
        except (OSError, RuntimeError, ValueError):
            _stop_watchdog()
            return


def _start_watchdog():
    """Start the daemon watchdog once; a no-op while disabled."""
    global _watchdog, _watchdog_stop, _heartbeat_at, _stall_dumped, _stall_dumps
    if not enabled():
        return None
    thread = _watchdog
    if thread is not None and thread.is_alive():
        return thread
    _heartbeat_at = time.perf_counter()
    _stall_dumped = False
    _stall_dumps = 0
    stop = threading.Event()
    thread = threading.Thread(
        target=_watchdog_loop, args=(stop,), name="panstats-stall", daemon=True
    )
    _watchdog_stop = stop
    _watchdog = thread
    thread.start()
    return thread


def _stop_watchdog():
    """Ask the watchdog to finish; safe from the watchdog itself, twice."""
    global _watchdog, _watchdog_stop
    stop, _watchdog_stop = _watchdog_stop, None
    _watchdog = None
    if stop is not None:
        stop.set()
    return stop is not None


def _start_heartbeat_timer():
    """Start the coalesced GUI heartbeat timer, or a no-op without Qt."""
    global _heartbeat_timer
    if not enabled() or _heartbeat_timer is not None:
        return _heartbeat_timer
    try:
        from PySide6.QtCore import QCoreApplication, Qt, QTimer
    except ImportError:
        return None
    if QCoreApplication.instance() is None:
        return None
    timer = QTimer()
    try:
        timer.setTimerType(Qt.TimerType.CoarseTimer)
        timer.setInterval(HEARTBEAT_MS)
        timer.timeout.connect(heartbeat)
        timer.start()
    except (RuntimeError, TypeError):
        return None
    _heartbeat_timer = timer
    _start_watchdog()
    return timer


def _stop_heartbeat_timer():
    """Stop and drop the heartbeat timer; safe to call twice."""
    global _heartbeat_timer
    timer, _heartbeat_timer = _heartbeat_timer, None
    if timer is None:
        return False
    try:
        timer.stop()
    except RuntimeError:
        pass
    return True


def shutdown():
    """Stop the sampler, the watchdog and the heartbeat timer; idempotent."""
    _stop_sampler()
    _stop_heartbeat_timer()
    _stop_watchdog()


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


def _render_source(window):
    """The Quick window's ``beforeRendering`` signal, or ``None``."""
    getter = getattr(window, "quickWindow", None)
    quick = getter() if callable(getter) else None
    if quick is None:
        return None
    signal = getattr(quick, "beforeRendering", None)
    if signal is not None and hasattr(signal, "connect"):
        return signal
    return None


def _bind_render_clock(window, key):
    """Connect ``beforeRendering`` once per window; a no-op while disabled."""
    if not enabled() or key in _render_connections:
        return None
    source = _render_source(window)
    if source is None:
        return None
    connection = _direct_connection()
    try:
        if connection is None:
            source.connect(_render_started)
        else:
            source.connect(_render_started, connection)
    except (RuntimeError, TypeError):
        return None
    _render_connections[key] = (source, _render_started)
    return key


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
    shutdown()


def connect_window(window, on_frame=None):
    """Follow ``window``'s presented frames; returns a key or ``None``.

    Mirrors the trace's ``afterRendering`` hook: connected directly, once per
    window, and disconnected again by ``disconnect_window`` or by the window's
    own ``destroyed`` signal.  While disabled nothing is connected.
    """
    if not enabled() or window is None:
        return None
    # The heartbeat and its watchdog ride the same wiring: the timer fires on
    # the GUI thread, so a blocked event loop stops refreshing the heartbeat.
    _start_heartbeat_timer()
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
    _bind_render_clock(window, key)
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
    render_entry = _render_connections.pop(id(window), None)
    if render_entry is not None:
        source, slot = render_entry
        try:
            source.disconnect(slot)
        except (RuntimeError, TypeError):
            pass
    entry = _connections.pop(id(window), None)
    if entry is None:
        return False
    source, slot = entry
    try:
        source.disconnect(slot)
    except (RuntimeError, TypeError):
        pass
    if not _connections:
        shutdown()
    return True
