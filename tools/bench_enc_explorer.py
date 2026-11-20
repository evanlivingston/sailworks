"""Repeatable coverage/CPU measurement harness for the ENC chart explorer.

The ENC scene layer composes geometry over the camera's view grown by
``enc_compose_margin()`` and keeps the presented frame attached while the
camera pans.  This tool measures what a user actually sees: how much of the
viewport the presented frame covers, how long a recompose takes, and the
present-to-present frame interval, for fixed camera scripts.

Driving
-------
One camera step is applied per ``frameSwapped`` signal from the window, never
from a ``QTimer``: Python timer slots starve while a chart worker holds the GIL
inside a blocking cairo call.  A frame that takes a long time is recorded, not
retried.  The whole process is bounded by ``signal.setitimer(SIGALRM)``.

Metric
------
Coverage is geometric, not pixel-readback.  A presented scene was composed for
a recorded camera over a rectangle ``margin`` logical pixels past each edge.
Mapping that rectangle into the current camera's screen frame (including a
zoom scale change and a pan translation) gives the uncovered fraction of the
viewport.  This is the guaranteed coverage: the meshed granule can be larger
than the margin, so the metric never overstates coverage.

Output schema (``schema_version`` 1)
-----------------------------------
::

    {
      "schema_version": 1,
      "generated_at": ISO-8601 UTC,
      "target": "current",
      "target_description": str,
      "run": {
        "commit": str, "dirty": bool, "platform": str,
        "anchor": {"lat": f, "lon": f, "zoom": f},
        "window": {"requested": [w, h], "width": w, "height": h,
                   "dpr": f, "honoured": bool},
        "alarm_seconds": f,
        "scenarios": [str, ...]
      },
      "scenarios": [
        {"name": str, "cache": "cold" | "warm", "frames": [...],
         "requests": [...], "summary": {...}}
      ]
    }

Per-frame record: ``frame``, ``step``, ``t_ms``, ``interval_ms``,
``camera`` (lat/lon/zoom), ``presented_generation``, ``presented_partial``,
``pending``, ``requests_issued``, ``uncovered_fraction``.  Per-request record: ``generation``,
``request_ms``, ``presented_ms``, ``latency_ms``, ``outcome``.  Per-scenario
summary: ``frames``, ``interval_ms`` {p50,p95,p99,max,mean}, ``uncovered``
{max,mean,frames_gt_zero}, ``recompose`` {requests,pending_frames}, and
``recompose_latency_ms`` {p50,p95,p99,max,mean}.

The schema names no implementation classes, so a later
``--target resident-geometry`` or ``--target cached-tiles`` run compares
directly with ``--target current``.

Ring size
---------
``--margin <px>`` sets ``SAILWORKS_ENC_COMPOSE_MARGIN`` for the run (running
the tool with that variable exported is equivalent), which replaces the ring
that ``enc_compose_margin()`` derives.  Unset, the shipped rule applies.
``--matrix`` sweeps a list of ring sizes and prints one coverage/cost table
plus the prefetch arithmetic: at ``pan_fast``'s 45 px per frame, how long
before the ring is exhausted, against the measured recompose latency.

Each scenario summary also carries ``geometry``: the scene layer's reported
``vertices``/``triangles`` for the last complete composition, and
``estimated_bytes`` - composed vertices x :data:`VERTEX_STRIDE_BYTES` x
:data:`MEMORY_COPY_MULTIPLIER`, an estimate rather than a measurement.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ.pop("QSG_RENDER_LOOP", None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

TMP = Path("/private/var/folders/w7/hb5wv1x1715821rqd1_3xf4r0000gn/T/opencode")
SNAPSHOT = Path.home() / "Library/Caches/sailworks-bench/snapshot"

SCHEMA_VERSION = 1

#: Fixed camera anchor: San Francisco Bay, over US5OAKFF / US5OAKFG.
ANCHOR = {"lat": 37.81, "lon": -122.42, "zoom": 10.0}

#: Requested window size.  The window server may refuse it; the actual size is
#: recorded and drives every camera in the run.
REQUESTED_WINDOW = (1200, 800)
PALETTE = "dark"
DENSITY = os.environ.get("ENC_DENSITY", "very_dense")
STAMP = datetime(2026, 11, 15, 18, tzinfo=timezone.utc)

#: Per-frame swap deadline and the whole-process SIGALRM budget.
FRAME_TIMEOUT_S = 20.0
ALARM_S = 600.0
INITIAL_SCENE_TIMEOUT_S = 90.0

TARGETS = {
    "current": "camera-grown, generational scene geometry (baseline)",
}

#: The pan step in logical pixels for the two pan scenarios.
PAN_SLOW_STEP = 15.0
PAN_FAST_STEP = 45.0
PAN_SLOW_FRAMES = 60
PAN_FAST_FRAMES = 20
ZOOM_OUT_STEP = -0.5
ZOOM_OUT_STEPS = 3
ZOOM_SETTLE_FRAMES = 10
PAN_ZOOM_SETTLE_FRAMES = 12

#: Ring sizes ``--matrix`` sweeps when given no explicit list.
MATRIX_MARGINS = (256.0, 400.0, 600.0, 900.0, 1200.0)

#: Scenarios the matrix table needs at minimum.
MATRIX_SCENARIOS = ("pan_fast", "zoom_out")

#: Bytes per composed vertex: two ``float32`` positions plus four RGBA bytes
#: (see ``sailworks.qt.encgpu`` module notes).
VERTEX_STRIDE_BYTES = 12

#: Multiplier applied on top of the composed vertex array for the copies that
#: also hold it: the scene layer's retained source array and the graphics
#: buffer upload.  An estimate, not a measurement.
MEMORY_COPY_MULTIPLIER = 3.0

#: Display refresh the pan arithmetic assumes, in Hz.
DISPLAY_HZ = 60.0


# ------------------------------------------------------------------ pure API
def _clamp_latitude(latitude):
    return max(-85.051129, min(85.051129, float(latitude)))


def world_x(longitude, zoom):
    """Mercator world pixels for a longitude at ``zoom`` (256 px tile)."""
    return (float(longitude) + 180.0) / 360.0 * 256.0 * 2.0 ** float(zoom)


def world_y(latitude, zoom):
    """Mercator world pixels for a latitude at ``zoom``."""
    radians = math.radians(_clamp_latitude(latitude))
    mercator = math.log(math.tan(math.pi / 4.0 + radians / 2.0))
    return (1.0 - mercator / math.pi) / 2.0 * 256.0 * 2.0 ** float(zoom)


def centre_at_world(x, y, zoom):
    """``(lat, lon)`` whose Mercator centre is world pixel ``(x, y)``."""
    span = 256.0 * 2.0 ** float(zoom)
    lon = float(x) / span * 360.0 - 180.0
    ratio = 1.0 - 2.0 * float(y) / span
    lat = math.degrees(2.0 * math.atan(math.exp(math.pi * ratio)) - math.pi / 2.0)
    return lat, lon


def pan_camera(camera, dx, dy):
    """Camera state panned by ``(dx, dy)`` logical pixels at fixed zoom."""
    zoom = float(camera["zoom"])
    x = world_x(camera["lon"], zoom) - float(dx)
    y = world_y(camera["lat"], zoom) - float(dy)
    lat, lon = centre_at_world(x, y, zoom)
    moved = {"lat": lat, "lon": lon, "zoom": zoom}
    for key in ("width", "height"):
        if key in camera:
            moved[key] = float(camera[key])
    return moved


def uncovered_fraction(composed, current, margin):
    """Viewport fraction not covered by the scene composed at ``composed``.

    ``composed`` is the camera the presented scene was composed for plus its
    ``width``/``height``; ``current`` is the camera now shown.  Both are dicts
    with ``lat``/``lon``/``zoom``/``width``/``height``.  ``margin`` is the
    logical-pixel ring the composition reaches past each edge.  Pure geometry:
    the composed rectangle is mapped through the zoom ratio and the camera
    translation, clipped to the current viewport, and the uncovered area is
    returned in ``[0, 1]``.  Nothing presented means ``1.0``.
    """
    width = float(current["width"])
    height = float(current["height"])
    if not (width > 0.0 and height > 0.0):
        raise ValueError("current viewport must have a positive size")
    if composed is None:
        return 1.0
    scale = 2.0 ** (float(current["zoom"]) - float(composed["zoom"]))
    margin = max(0.0, float(margin))
    half_w = float(composed["width"]) / 2.0 + margin
    half_h = float(composed["height"]) / 2.0 + margin
    cx = world_x(composed["lon"], composed["zoom"])
    cy = world_y(composed["lat"], composed["zoom"])
    cur_x = world_x(current["lon"], current["zoom"])
    cur_y = world_y(current["lat"], current["zoom"])
    left = (cx - half_w) * scale - cur_x + width / 2.0
    right = (cx + half_w) * scale - cur_x + width / 2.0
    top = (cy - half_h) * scale - cur_y + height / 2.0
    bottom = (cy + half_h) * scale - cur_y + height / 2.0
    covered_w = max(0.0, min(right, width) - max(left, 0.0))
    covered_h = max(0.0, min(bottom, height) - max(top, 0.0))
    covered = covered_w * covered_h / (width * height)
    return min(1.0, max(0.0, 1.0 - covered))


def percentile(values, q):
    """Nearest-rank percentile of an unsorted sequence, or ``None``."""
    ordered = sorted(values)
    if not ordered:
        return None
    index = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[index]


def summarise_ms(values):
    """``{count,p50,p95,p99,max,mean}`` in milliseconds, or ``None``."""
    if not values:
        return None
    seconds = list(values)
    return {
        "count": len(seconds),
        "p50": round(percentile(seconds, 0.50) * 1000.0, 3),
        "p95": round(percentile(seconds, 0.95) * 1000.0, 3),
        "p99": round(percentile(seconds, 0.99) * 1000.0, 3),
        "max": round(max(seconds) * 1000.0, 3),
        "mean": round(statistics.fmean(seconds) * 1000.0, 3),
    }


def build_steps(name):
    """The fixed camera script for ``name``: a tuple of step dicts.

    Each step is ``{"kind": "pan", "pan": (dx, dy)}``,
    ``{"kind": "zoom", "zoom": delta}``, or ``{"kind": "settle"}``.  No
    randomness and no wall-clock input: the same name always yields the same
    sequence.
    """
    if name == "pan_slow":
        return tuple(
            {"kind": "pan", "pan": (PAN_SLOW_STEP, 0.0)} for _ in range(PAN_SLOW_FRAMES)
        )
    if name == "pan_fast":
        return tuple(
            {"kind": "pan", "pan": (PAN_FAST_STEP, 0.0)} for _ in range(PAN_FAST_FRAMES)
        )
    if name == "zoom_out":
        steps = []
        for _ in range(ZOOM_OUT_STEPS):
            steps.append({"kind": "zoom", "zoom": ZOOM_OUT_STEP})
            steps.extend({"kind": "settle"} for _ in range(ZOOM_SETTLE_FRAMES))
        return tuple(steps)
    if name == "pan_zoom_settle":
        steps = [{"kind": "pan", "pan": (20.0, 0.0)} for _ in range(20)]
        steps.append({"kind": "zoom", "zoom": 0.5})
        steps.extend({"kind": "settle"} for _ in range(PAN_ZOOM_SETTLE_FRAMES))
        steps.extend({"kind": "pan", "pan": (-20.0, 0.0)} for _ in range(20))
        return tuple(steps)
    raise ValueError("unknown scenario %r" % (name,))


SCENARIOS = ("pan_slow", "pan_fast", "zoom_out", "pan_zoom_settle")


def resolve_target(name):
    """Validate a ``--target`` value, returning it unchanged."""
    if name not in TARGETS:
        raise ValueError(
            "unknown target %r; supported targets: %s"
            % (name, ", ".join(sorted(TARGETS)))
        )
    return name


# ------------------------------------------------------------------ fixtures
def prepare_data_dir():
    """A settings sandbox over the shipped ENC snapshot, ENC scene enabled."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "startup_bench", ROOT / "tools/benchmark_startup_rendering.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    fixture.verify_snapshot(SNAPSHOT)
    root = Path(tempfile.mkdtemp(prefix="enc-explorer-", dir=str(TMP)))
    data_dir = Path(fixture.prepare_sandbox(SNAPSHOT, root))
    settings_path = data_dir / "settings.json"
    settings = json.loads(settings_path.read_text())
    settings["chartENCVisible"] = True
    settings_path.write_text(json.dumps(settings, indent=1))
    return data_dir


def commit_info():
    try:
        sha = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(ROOT),
                    "status",
                    "--porcelain",
                    "--untracked-files=no",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return None, False
    return sha, dirty


class BenchError(RuntimeError):
    """The harness cannot produce trustworthy numbers."""


def _camera_dict(camera):
    return {
        "lat": float(camera.center_lat),
        "lon": float(camera.center_lon),
        "zoom": float(camera.zoom),
        "width": float(camera.width),
        "height": float(camera.height),
    }


def _summary(frames, requests):
    intervals = [frame["interval_s"] for frame in frames if frame["interval_s"]]
    uncovered = [frame["uncovered_fraction"] for frame in frames]
    latencies = [item["latency_s"] for item in requests if item["latency_s"]]
    return {
        "frames": len(frames),
        "interval_ms": summarise_ms(intervals),
        "uncovered": {
            "max": round(max(uncovered), 6) if uncovered else None,
            "mean": round(statistics.fmean(uncovered), 6) if uncovered else None,
            "frames_gt_zero": sum(1 for value in uncovered if value > 0.0),
        },
        "recompose": {
            "requests": len(requests),
            "pending_frames": sum(1 for frame in frames if frame["pending"]),
        },
        "recompose_latency_ms": summarise_ms(latencies),
    }


# ------------------------------------------------------------------ Qt run
def _geometry_estimate(composed):
    """Composed mesh size and an order-of-magnitude memory estimate.

    ``vertices``/``triangles`` are what the scene layer reports for the last
    complete composition the scenario presented.  ``estimated_bytes`` is an
    estimate, not a measurement: the composed vertex array at
    :data:`VERTEX_STRIDE_BYTES` per vertex, times
    :data:`MEMORY_COPY_MULTIPLIER` for the retained source array and the
    graphics-buffer upload that hold the same data again.
    """
    vertices = int(composed.get("vertices") or 0)
    return {
        "cells": int(composed.get("cells") or 0),
        "vertices": vertices,
        "triangles": int(composed.get("triangles") or 0),
        "vertex_stride_bytes": VERTEX_STRIDE_BYTES,
        "copy_multiplier": MEMORY_COPY_MULTIPLIER,
        "estimated_bytes": int(vertices * VERTEX_STRIDE_BYTES * MEMORY_COPY_MULTIPLIER),
        "estimate": True,
    }


def run_scenario(name, cache, data_dir, store_dir, window, item, geometry):
    """Drive one scenario against a fresh layer over the given store."""
    from sailworks.qt.encscenelayer import (
        STATE_READY,
        EncSceneLayer,
    )
    from sailworks.qt.gribviewport import ViewportCamera, enc_scene_catalog
    from sailworks.qt.viewporttransform import (
        PanReference,
        enc_compose_margin,
        enc_scene_cover_stale,
    )

    if cache == "cold":
        shutil.rmtree(store_dir, ignore_errors=True)

    width = geometry["width"]
    height = geometry["height"]
    steps = build_steps(name)
    clock = time.perf_counter
    started = clock()

    layer = EncSceneLayer(
        data_dir,
        PALETTE,
        worker_idle=lambda: True,
        dpr=1,
        catalog_factory=enc_scene_catalog,
        cache_dir=store_dir,
    )

    presented = {"generation": None, "camera": None, "margin": None, "partial": False}
    composed = {"cells": 0, "vertices": 0, "triangles": 0}
    requests = []
    open_requests = {}
    frames = []
    pending_generation = None
    cover_generation = None
    verified = {"ready": False}

    def pump():
        window.update()
        _QApp.instance().processEvents()

    def adopt():
        payload = layer.published
        if not payload:
            return
        generation, scene, metadata = payload
        if scene is None or metadata is None:
            return
        partial = bool(metadata.get("partial"))
        if partial and presented["generation"] is not None:
            # Production keeps the ready frame up; a partial is progress only.
            return
        if presented["generation"] is not None:
            if generation < presented["generation"]:
                return
            if generation == presented["generation"] and not (
                presented["partial"] and not partial
            ):
                return
        now = clock()
        if not verified["ready"] and not partial:
            report = metadata
            if layer.state != STATE_READY:
                raise BenchError("ENC scene did not reach ready: %s" % layer.error)
            if int(report.get("cells", 0)) <= 0 or int(report.get("vertices", 0)) <= 0:
                raise BenchError(
                    "ENC scene has no chart geometry at the anchor "
                    "(cells=%s vertices=%s); refusing to measure a blank scene"
                    % (report.get("cells"), report.get("vertices"))
                )
            verified["ready"] = True
        projection = metadata.get("projection")
        if projection is not None:
            item.set_projection(projection)
        item.setWidth(width)
        item.setHeight(height)
        item.set_compose_margin(metadata.get("compose_margin"))
        item.set_view_scene(scene)
        item.setVisible(True)
        presented.update(
            {
                "generation": generation,
                "camera": _camera_dict(metadata["camera"]),
                "margin": float(metadata.get("compose_margin") or 0.0),
                "partial": partial,
            }
        )
        if not partial:
            composed.update(
                {
                    "cells": int(metadata.get("cells") or 0),
                    "vertices": int(metadata.get("vertices") or 0),
                    "triangles": int(metadata.get("triangles") or 0),
                }
            )
        record = open_requests.get(generation)
        if record is not None and record["latency_s"] is None:
            record["presented_s"] = now - started
            record["latency_s"] = now - record["request_s"]
        window.update()

    def request(camera):
        nonlocal pending_generation
        generation = layer.request(camera)
        if generation is None:
            return
        if generation != pending_generation:
            for old, record in list(open_requests.items()):
                if old != generation and record["latency_s"] is None:
                    record["outcome"] = "superseded"
                    open_requests.pop(old)
            pending_generation = generation
            record = {
                "generation": generation,
                "request_s": clock(),
                "request_ms": None,
                "presented_ms": None,
                "latency_s": None,
                "outcome": "pending",
            }
            record["request_ms"] = round((record["request_s"] - started) * 1000.0, 3)
            open_requests[generation] = record
            requests.append(record)

    def make_camera(state):
        return ViewportCamera(
            center_lat=state["lat"],
            center_lon=state["lon"],
            zoom=state["zoom"],
            width=width,
            height=height,
            time=STAMP,
            density=DENSITY,
            over_land=False,
        )

    state = dict(ANCHOR)
    camera = make_camera(state)
    margin = enc_compose_margin(camera)
    request(camera)
    deadline = clock() + INITIAL_SCENE_TIMEOUT_S
    try:
        while presented["generation"] is None:
            pump()
            adopt()
            if presented["generation"] is None:
                if clock() > deadline:
                    raise BenchError(
                        "no ENC scene presented within %.0fs (state=%s err=%s)"
                        % (INITIAL_SCENE_TIMEOUT_S, layer.state, layer.error)
                    )
                time.sleep(0.0005)
        # Settle one swap before the timed script starts.
        seen = len(geometry["swaps"])
        while len(geometry["swaps"]) == seen:
            pump()
            if clock() > deadline + FRAME_TIMEOUT_S:
                raise BenchError("window never swapped its first frame")
            time.sleep(0.0002)
        previous_swap = geometry["swaps"][-1]

        for index, step in enumerate(steps):
            state = _advance(state, step)
            camera = make_camera(state)
            presented_camera = presented["camera"]
            if presented_camera is None:
                request(camera)
            elif cover_generation != presented["generation"]:
                # One replacement request per presented generation, however far
                # the gesture runs: this mirrors the viewport, which asks again
                # only after a new scene has been presented.
                frame_camera = _ReferenceCamera(presented_camera)
                stale = enc_scene_cover_stale(
                    _reference_from(presented_camera),
                    frame_camera,
                    _ReferenceCamera(_camera_dict(camera)),
                    presented["margin"],
                    1.0,
                )
                if stale:
                    cover_generation = presented["generation"]
                    request(camera)
            seen = len(geometry["swaps"])
            deadline = clock() + FRAME_TIMEOUT_S
            while len(geometry["swaps"]) <= seen:
                pump()
                adopt()
                if len(geometry["swaps"]) <= seen and clock() > deadline:
                    raise BenchError(
                        "%s frame %d never swapped within %.0fs"
                        % (name, index, FRAME_TIMEOUT_S)
                    )
                time.sleep(0.0002)
            now = geometry["swaps"][-1]
            interval = now - previous_swap
            previous_swap = now
            adopt()
            frames.append(
                {
                    "frame": index,
                    "step": step["kind"],
                    "t_ms": round((now - started) * 1000.0, 3),
                    "interval_s": interval,
                    "camera": {
                        "lat": state["lat"],
                        "lon": state["lon"],
                        "zoom": state["zoom"],
                    },
                    "presented_generation": presented["generation"],
                    "presented_partial": presented["partial"],
                    "pending": pending_generation is not None
                    and pending_generation != presented["generation"],
                    "requests_issued": len(requests),
                    "uncovered_fraction": round(
                        uncovered_fraction(
                            presented["camera"],
                            _camera_dict(camera),
                            presented["margin"],
                        ),
                        6,
                    ),
                }
            )
    finally:
        layer.close()

    for record in open_requests.values():
        if record["latency_s"] is None:
            record["outcome"] = "not_presented"
    for record in requests:
        if record["latency_s"] is not None:
            record["outcome"] = "presented"
            record["presented_ms"] = round(
                (record["request_s"] + record["latency_s"] - started) * 1000.0, 3
            )

    if not verified["ready"]:
        raise BenchError("%s never presented a complete ENC scene" % name)
    summary = _summary(frames, requests)
    summary["geometry"] = _geometry_estimate(composed)
    return {
        "name": name,
        "cache": cache,
        "ring_px": round(margin, 3),
        "frames": [
            {
                "frame": frame["frame"],
                "step": frame["step"],
                "t_ms": frame["t_ms"],
                "interval_ms": round(frame["interval_s"] * 1000.0, 3),
                "camera": frame["camera"],
                "presented_generation": frame["presented_generation"],
                "presented_partial": frame["presented_partial"],
                "pending": frame["pending"],
                "requests_issued": frame["requests_issued"],
                "uncovered_fraction": frame["uncovered_fraction"],
            }
            for frame in frames
        ],
        "requests": [
            {
                "generation": record["generation"],
                "request_ms": record["request_ms"],
                "presented_ms": record["presented_ms"],
                "latency_ms": (
                    None
                    if record["latency_s"] is None
                    else round(record["latency_s"] * 1000.0, 3)
                ),
                "outcome": record["outcome"],
            }
            for record in requests
        ],
        "summary": summary,
    }


def _advance(state, step):
    kind = step["kind"]
    if kind == "pan":
        return pan_camera(state, step["pan"][0], step["pan"][1])
    if kind == "zoom":
        return {
            "lat": state["lat"],
            "lon": state["lon"],
            "zoom": state["zoom"] + float(step["zoom"]),
        }
    return dict(state)


def _reference_from(camera):
    """A ``PanReference`` for a recorded camera dict."""
    from sailworks.qt.viewporttransform import PanReference

    return PanReference(
        center_lat=camera["lat"],
        center_lon=camera["lon"],
        zoom=camera["zoom"],
        width=camera["width"],
        height=camera["height"],
        dpr=1.0,
    )


class _ReferenceCamera:
    """A camera-shaped view of a recorded dict for ``enc_scene_cover_stale``."""

    def __init__(self, camera):
        self.center_lat = camera["lat"]
        self.center_lon = camera["lon"]
        self.zoom = camera["zoom"]
        self.width = camera["width"]
        self.height = camera["height"]


_QApp = None


def parse_margin_list(matrix, margin):
    """Ring sizes to sweep, from ``--matrix`` (a list) or ``--margin`` (one).

    ``None`` means "the shipped rule": no override is applied.
    """
    if matrix is not None:
        chunks = [chunk for chunk in matrix.replace(" ", ",").split(",") if chunk]
        values = []
        for chunk in chunks:
            try:
                value = float(chunk)
            except ValueError:
                raise ValueError("--matrix ring %r is not a pixel count" % chunk)
            if not value > 0.0:
                raise ValueError("--matrix ring %r must be positive" % chunk)
            values.append(value)
        if not values:
            raise ValueError("--matrix needs at least one ring size")
        return values
    if margin is not None:
        if not margin > 0.0:
            raise ValueError("--margin must be a positive pixel count")
        return [float(margin)]
    return [None]


def apply_margin_override(margin):
    """Set, or clear, the ring override for the next scenario.

    This is exactly what running with ``SAILWORKS_ENC_COMPOSE_MARGIN=<px>`` in
    the environment does, so either invocation reproduces a run.
    """
    from sailworks.qt.viewporttransform import ENC_COMPOSE_MARGIN_ENV

    if margin is None:
        os.environ.pop(ENC_COMPOSE_MARGIN_ENV, None)
    else:
        os.environ[ENC_COMPOSE_MARGIN_ENV] = "%g" % float(margin)


def prefetch_budget_ms(margin):
    """Milliseconds a ``pan_fast`` gesture leaves before the ring is exhausted.

    A replacement is requested once ``ENC_COMPOSE_TRIGGER`` of the ring is
    consumed, so the composer has that share of the ring, at
    ``PAN_FAST_STEP`` pixels per frame and :data:`DISPLAY_HZ`, to present.
    """
    from sailworks.qt.viewporttransform import ENC_COMPOSE_TRIGGER

    rate = PAN_FAST_STEP * DISPLAY_HZ / 1000.0
    return float(margin) * ENC_COMPOSE_TRIGGER / rate


def print_matrix(results):
    """One table for every ring size x scenario x cache in ``results``."""
    print(
        "\nest_MB is an estimate: composed vertices x %d B stride x %.1f copies"
        " (source array + graphics upload)"
        % (VERTEX_STRIDE_BYTES, MEMORY_COPY_MULTIPLIER),
        flush=True,
    )
    print(
        "ring     scenario          cache  max_unc     >0  lat_p50 lat_p95 "
        " iv_p50  iv_p95  iv_p99      verts       tris   est_MB",
        flush=True,
    )
    for result in results:
        summary = result["summary"]
        uncovered = summary["uncovered"]
        latency = summary["recompose_latency_ms"] or {}
        interval = summary["interval_ms"] or {}
        geometry = summary.get("geometry") or {}
        ring = result.get("ring_px")
        print(
            "%-8s %-16s %-5s %8.4f %6d %8s %7s %7s %7s %7s %10d %10d %8.2f"
            % (
                "auto" if ring is None else "%gpx" % ring,
                result["name"],
                result["cache"],
                uncovered["max"] or 0.0,
                uncovered["frames_gt_zero"],
                latency.get("p50", "-"),
                latency.get("p95", "-"),
                interval.get("p50", "-"),
                interval.get("p95", "-"),
                interval.get("p99", "-"),
                geometry.get("vertices", 0),
                geometry.get("triangles", 0),
                (geometry.get("estimated_bytes") or 0) / (1024.0 * 1024.0),
            ),
            flush=True,
        )


def print_prefetch(results, margins):
    """Can a recompose land before the pan reaches the ring boundary?"""
    latency = {}
    for result in results:
        if result["name"] != "pan_fast":
            continue
        measured = (result["summary"]["recompose_latency_ms"] or {}).get("p95")
        if measured is None:
            continue
        key = result.get("ring_px")
        latency[key] = max(latency.get(key, 0.0), measured)
    rate = PAN_FAST_STEP * DISPLAY_HZ / 1000.0
    print(
        "\nprefetch arithmetic: pan_fast moves %.0f px/frame at %.0f Hz = %.2f px/ms;"
        " a replacement is requested at half the ring, and %.0f px total is"
        " %.0f ms of gesture"
        % (
            PAN_FAST_STEP,
            DISPLAY_HZ,
            rate,
            PAN_FAST_STEP * PAN_FAST_FRAMES,
            PAN_FAST_STEP * PAN_FAST_FRAMES / rate,
        ),
        flush=True,
    )
    for margin in margins:
        if margin is None:
            continue
        budget = prefetch_budget_ms(margin)
        measured = latency.get(margin)
        verdict = "unmeasured"
        if measured is not None:
            verdict = "lands" if budget >= measured else "boundary first"
        print(
            "  ring %4g px: budget %6.1f ms (%.0f px / %.2f px/ms)"
            "  lat_p95 %8s ms  -> %s"
            % (
                margin,
                budget,
                margin * 0.5,
                rate,
                "n/a" if measured is None else "%.1f" % measured,
                verdict,
            ),
            flush=True,
        )


def main(argv=None):
    global _QApp
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--target",
        default="current",
        help="architecture under test (%s)" % ", ".join(sorted(TARGETS)),
    )
    parser.add_argument(
        "--out",
        default=str(TMP / "enc_explorer_bench.json"),
        help="JSON output path",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=ALARM_S,
        help="SIGALRM budget for the whole run, seconds",
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=None,
        help="fixed ENC compose ring in logical pixels for this run"
        " (sets %s; unset uses the shipped rule)" % "SAILWORKS_ENC_COMPOSE_MARGIN",
    )
    parser.add_argument(
        "--matrix",
        default=None,
        help="comma-separated ring sizes to sweep, one table for the set"
        " (e.g. %s)" % ",".join("%g" % value for value in MATRIX_MARGINS),
    )
    parser.add_argument(
        "--scenarios",
        default=None,
        help="comma-separated scenarios (default: %s; %s in matrix mode)"
        % (",".join(SCENARIOS), ",".join(MATRIX_SCENARIOS)),
    )
    args = parser.parse_args(argv)
    try:
        target = resolve_target(args.target)
        margins = parse_margin_list(args.matrix, args.margin)
        if args.scenarios is None:
            names = list(MATRIX_SCENARIOS if args.matrix is not None else SCENARIOS)
        else:
            names = [name.strip() for name in args.scenarios.split(",") if name.strip()]
            for name in names:
                if name not in SCENARIOS:
                    raise ValueError("unknown scenario %r" % name)
            if not names:
                raise ValueError("--scenarios needs at least one name")
    except ValueError as error:
        parser.error(str(error))

    import signal as _signal

    def alarm(_signum, _frame):
        print("ALARM: budget exceeded", flush=True)
        os._exit(3)

    _signal.signal(_signal.SIGALRM, alarm)
    _signal.setitimer(_signal.ITIMER_REAL, args.timeout)

    from PySide6.QtCore import QSize
    from PySide6.QtGui import QColor, QGuiApplication, QSurfaceFormat
    from PySide6.QtQuick import QQuickWindow, QSGRendererInterface

    from sailworks.qt.encgpu import EncMeshItem

    if sys.platform == "darwin":
        QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.Metal)
    fmt = QSurfaceFormat()
    fmt.setSamples(4)
    fmt.setAlphaBufferSize(8)
    QSurfaceFormat.setDefaultFormat(fmt)
    app = QGuiApplication.instance() or QGuiApplication([])
    _QApp = app

    window = QQuickWindow()
    window.resize(QSize(*REQUESTED_WINDOW))
    window.setColor(QColor.fromRgbF(0.1, 0.2, 0.3))
    item = EncMeshItem()
    item.setParentItem(window.contentItem())
    item.setWidth(REQUESTED_WINDOW[0])
    item.setHeight(REQUESTED_WINDOW[1])
    item.setVisible(False)

    swaps = []
    window.frameSwapped.connect(lambda: swaps.append(time.perf_counter()))
    window.show()
    window.update()
    deadline = time.perf_counter() + 30.0
    while not swaps:
        app.processEvents()
        window.update()
        if time.perf_counter() > deadline:
            raise BenchError("window never produced a frameSwapped frame")
        time.sleep(0.001)

    width = int(window.width())
    height = int(window.height())
    dpr = float(window.devicePixelRatio())
    item.setWidth(width)
    item.setHeight(height)
    geometry = {"width": width, "height": height, "swaps": swaps}

    data_dir = prepare_data_dir()
    store_dir = Path(args.out).resolve().with_suffix(".store")
    sha, dirty = commit_info()
    started = time.time()
    scenarios = []
    print(
        "window %dx%d (requested %dx%d), dpr %.2f, anchor %.4f,%.4f z%.2f"
        % (
            width,
            height,
            REQUESTED_WINDOW[0],
            REQUESTED_WINDOW[1],
            dpr,
            ANCHOR["lat"],
            ANCHOR["lon"],
            ANCHOR["zoom"],
        ),
        flush=True,
    )
    for margin in margins:
        apply_margin_override(margin)
        ring = "auto" if margin is None else "%gpx" % margin
        for name in names:
            for cache in ("cold", "warm"):
                began = time.perf_counter()
                try:
                    result = run_scenario(
                        name, cache, data_dir, store_dir, window, item, geometry
                    )
                except BenchError as error:
                    # A ring too large for this machine is data, not a reason
                    # to discard the rings that did run.
                    result = {
                        "name": name,
                        "cache": cache,
                        "ring_px": margin,
                        "error": str(error),
                        "summary": {},
                    }
                scenarios.append(result)
                elapsed = time.perf_counter() - began
                print(
                    "  ring %-6s %-16s %-4s %5.1fs  %s"
                    % (
                        ring,
                        name,
                        cache,
                        elapsed,
                        result.get("error") or json.dumps(result["summary"]),
                    ),
                    flush=True,
                )
                window.update()
    apply_margin_override(None)

    out = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "target_description": TARGETS[target],
        "run": {
            "commit": sha,
            "dirty": dirty,
            "platform": sys.platform,
            "anchor": dict(ANCHOR),
            "window": {
                "requested": list(REQUESTED_WINDOW),
                "width": width,
                "height": height,
                "dpr": dpr,
                "honoured": (width, height) == REQUESTED_WINDOW,
            },
            "alarm_seconds": args.timeout,
            "scenarios": list(names),
            "margins": [None if margin is None else margin for margin in margins],
        },
        "scenarios": scenarios,
    }
    path = Path(args.out).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1))
    print(
        "\nscenario          cache  max_unc   mean_unc  >0  req  pending  "
        "lat_p50  lat_p95  lat_max   iv_p50  iv_p95  iv_p99  iv_max",
        flush=True,
    )
    for result in scenarios:
        summary = result["summary"]
        uncovered = summary["uncovered"]
        latency = summary["recompose_latency_ms"] or {}
        interval = summary["interval_ms"] or {}
        print(
            "%-17s %-5s %8.4f %9.4f %4d %4d %8d %8s %8s %8s %7s %7s %7s %7s"
            % (
                result["name"],
                result["cache"],
                uncovered["max"] or 0.0,
                uncovered["mean"] or 0.0,
                uncovered["frames_gt_zero"],
                summary["recompose"]["requests"],
                summary["recompose"]["pending_frames"],
                latency.get("p50", "-"),
                latency.get("p95", "-"),
                latency.get("max", "-"),
                interval.get("p50", "-"),
                interval.get("p95", "-"),
                interval.get("p99", "-"),
                interval.get("max", "-"),
            ),
            flush=True,
        )
    if args.matrix is not None or len(margins) > 1:
        print_matrix(scenarios)
        print_prefetch(scenarios, margins)
    print("\nRESULT_PATH %s" % path, flush=True)

    worst = max(scenarios, key=lambda item: item["summary"]["uncovered"]["max"] or 0.0)
    print(
        "WORST_UNCOVERED %.4f %s/%s"
        % (
            worst["summary"]["uncovered"]["max"] or 0.0,
            worst["name"],
            worst["cache"],
        ),
        flush=True,
    )
    print("ELAPSED_S %.1f" % (time.time() - started), flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BenchError as error:
        print("FAIL:", error, flush=True)
        sys.exit(1)
