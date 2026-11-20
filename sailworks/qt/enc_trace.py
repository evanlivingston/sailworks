"""Opt-in JSONL trace of the standalone viewport's demand-to-present cycle.

Set ``SAILWORKS_ENC_TRACE=/path/to/trace.jsonl`` to append one JSON line per
event.  Unset (the default) makes every call here a no-op: the module reads the
environment once and never opens a file.  ``SAILWORKS_ENC_TRACE_SLOW_MS``
overrides the 500 ms slow-presentation threshold.

The module is deliberately dependency-free so the formatting and the slow
decision can be tested without Qt.
"""

import json
import os
import threading
import time
from datetime import datetime

ENV_PATH = "SAILWORKS_ENC_TRACE"
ENV_SLOW_MS = "SAILWORKS_ENC_TRACE_SLOW_MS"
DEFAULT_SLOW_MS = 500.0
PRESENT_TIMEOUT_MS = 5000.0

_lock = threading.Lock()
_config = None


def _read_config():
    path = os.environ.get(ENV_PATH) or None
    raw = os.environ.get(ENV_SLOW_MS)
    try:
        slow = DEFAULT_SLOW_MS if raw is None else float(raw)
    except (TypeError, ValueError):
        slow = DEFAULT_SLOW_MS
    return path, slow


def config():
    """Return ``(path, slow_ms)`` from the environment, read once."""
    global _config
    if _config is None:
        _config = _read_config()
    return _config


def reset():
    """Forget the cached environment (tests only)."""
    global _config
    _config = None


def enabled():
    return config()[0] is not None


def slow_ms():
    return config()[1]


def timestamp(now=None):
    return (now or datetime.now()).isoformat(timespec="milliseconds")


def format_line(kind, fields, now=None):
    """Render one trace record as a JSON line (no newline)."""
    record = {"time": timestamp(now), "event": kind}
    record.update(fields)
    return json.dumps(record, default=str, separators=(",", ":"))


def event(kind, **fields):
    """Append one record; a no-op unless tracing is enabled.

    Returns the written line (without newline), or ``None``.
    """
    path, _ = config()
    if path is None:
        return None
    line = format_line(kind, fields)
    with _lock:
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
        except OSError:
            return None
    return line


def slow(**fields):
    return event("slow", **fields)


def is_slow(elapsed_ms, limit_ms=None):
    """``True`` when a demand-to-present interval exceeds the threshold."""
    limit = slow_ms() if limit_ms is None else limit_ms
    return elapsed_ms is not None and elapsed_ms > limit


def present_timeout(elapsed_ms, presented, timeout_ms=PRESENT_TIMEOUT_MS):
    """``True`` when a demand still has no presented frame at the timeout."""
    return not presented and elapsed_ms is not None and elapsed_ms >= timeout_ms


def dominant_phase(phases):
    """Name of the longest positive phase, or ``None`` when none ran."""
    best = None
    best_value = None
    for name, value in (phases or {}).items():
        if value is None or value <= 0:
            continue
        if best is None or value > best_value:
            best, best_value = name, value
    return best


CELL_DETAIL_LIMIT = 12


def phase_ms(stages, already_ms=(), exclude=()):
    """Convert a diagnostic stage mapping into rounded milliseconds.

    The ``_stage`` helpers in the renderer accumulate ``perf_counter``
    differences, i.e. seconds, while every duration the trace records is in
    milliseconds.  Non-numeric entries are dropped, names in ``exclude`` are
    skipped, and names in ``already_ms`` pass through unchanged because their
    producer already reports milliseconds.
    """
    phases = {}
    passthrough = frozenset(already_ms)
    for name, value in (stages or {}).items():
        if name in exclude or isinstance(value, bool):
            continue
        if not isinstance(value, (int, float)):
            continue
        milliseconds = float(value) if name in passthrough else float(value) * 1000.0
        phases[name] = round(milliseconds, 3)
    return phases


def build_phases(stages):
    """The non-overlapping top-level phases, in milliseconds.

    ``stages`` is the diagnostic mapping in seconds.  ``mesh`` is the meshing
    loop with the per-cell acquire it contains removed, so no work is counted
    twice and the phases sum to no more than the assembly they belong to.
    """

    def seconds(name):
        value = (stages or {}).get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value) * 1000.0

    phases = {}
    plan = seconds("plan")
    if plan is not None:
        phases["plan"] = round(plan, 3)
    acquire = seconds("cell_open")
    if acquire is not None:
        phases["acquire"] = round(acquire, 3)
    mesh = seconds("mesh")
    if mesh is not None:
        phases["mesh"] = round(max(mesh - (acquire or 0.0), 0.0), 3)
    assemble = seconds("assemble")
    if assemble is not None:
        phases["assemble"] = round(assemble, 3)
    return phases


def cell_detail(cells, limit=CELL_DETAIL_LIMIT):
    """The bounded per-cell breakdown, heaviest first.

    ``cells`` maps a cell name to its already-millisecond ``open`` and ``mesh``
    durations.  Only the ``limit`` heaviest cells stay, so one composition
    reports a readable list however many cells the plan holds.
    """
    rows = []
    for name, stages in (cells or {}).items():
        if not isinstance(stages, dict):
            continue
        acquire = stages.get("open")
        mesh = stages.get("mesh")
        acquire = float(acquire) if isinstance(acquire, (int, float)) else 0.0
        mesh = float(mesh) if isinstance(mesh, (int, float)) else 0.0
        rows.append(
            {
                "cell": name,
                "ms": round(acquire + mesh, 3),
                "acquire_ms": round(acquire, 3),
                "mesh_ms": round(mesh, 3),
            }
        )
    rows.sort(key=lambda row: row["ms"], reverse=True)
    return rows[:limit]


def camera_fields(camera):
    """The camera identity every demand-scoped record carries."""
    if camera is None:
        return {}
    return {
        "lat": round(float(camera.center_lat), 5),
        "lon": round(float(camera.center_lon), 5),
        "zoom": round(float(camera.zoom), 4),
        "width": int(camera.width),
        "height": int(camera.height),
    }


def elapsed_ms(started, now=None):
    """Milliseconds from a ``time.perf_counter()`` value, shortened."""
    if started is None:
        return None
    return round(((now or time.perf_counter()) - started) * 1000.0, 3)
