#!/usr/bin/env python3
"""Scripted-camera measurement and capture harness (issue #213, Phase 1).

The real viewport is built in this process with the same sandboxed fixture and
the same chart layer the acceptance gate uses.  A fixed camera sequence is
driven through the application's own ``set_camera``/``request`` path and every
step is captured from the scene graph's framebuffer against a Cairo reference
assembled by ``tools/check_enc_viewport.py``'s own composition.

Three timing classes are recorded and kept apart:

* per-frame interaction (input -> afterRendering): wall-clock proxies sampled
  from the view's own ``afterRendering`` signal.  They are not presentation
  FPS, not GPU time and never thresholded.
* the chart worker's own ``composition_ms`` and ``queue_ms`` from the layer's
  composition metadata.
* ready-to-ready, generation tagged: ``request_to_publish_ms`` and
  ``publish_to_adopt_ms`` from the widget's own ``chart_latencies`` records.

The device pixel ratio is driven from outside the process by
``QT_SCALE_FACTOR``.  The harness tags every sample with the ratio it actually
measured and refuses to report samples when the measured ratio differs from
the one requested, so DPR 2 is never claimed from a DPR 1 process.

No renderer, default setting or production code is modified here.
"""

import argparse
import hashlib
import importlib.util
import json
import math
import os
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECK_ENC_PATH = ROOT / "tools" / "check_enc_viewport.py"
FIXTURE_PATH = ROOT / "tools" / "benchmark_startup_rendering.py"

TOOL = "tools/measure_enc_phase1.py"
SCHEMA_VERSION = 1
ISSUE = 213
PHASE = 1

DEFAULT_SCENARIOS = ("san-francisco", "puget")
KNOWN_SCENARIOS = ("san-francisco", "puget", "multi-cell")
#: The camera that selects several cells at once (issue #213's Gate 3 view).
#: When it is one of the requested scenarios the switch crosses to it, so the
#: sequence exercises a multi-cell view and not only the other anchor.  A run
#: that does not list it keeps its original switch target.
MULTI_CELL_SCENARIO = "multi-cell"
#: The density under test names the fidelity camera it composes against.
DENSITY_SCENARIO = {"sparse": "puget", "dense": "san-francisco"}
DENSITIES = ("sparse", "dense")
STEPS = (
    "cold",
    "warm",
    "pan",
    "pan_back",
    "zoom_out",
    "zoom_back",
    "switch_cell",
    "return",
)
PAN_DEGREES = 0.02
ZOOM_OUT_STEPS = 1.5
#: ``tools/check_enc_viewport.py``'s RATIO_LOW / RATIO_HIGH, kept beside the
#: acceptance they mirror so the harness can be imported without Qt.
COVERAGE_BAND = (0.95, 1.05)
COVERAGE_SPREAD_LIMIT = 0.05
#: Pixels the reference's own ink is grown by before it is used as the ENC
#: extent mask: antialiased cell edges fall just outside the strict ink test.
ENC_MASK_DILATE_PX = 3
READY_TIMEOUT_S = 60.0
WEATHER_TIMEOUT_S = 120.0
SETTLE_MS = 100
#: Counter-only steps read no frame, so the chart layer's raster ENC
#: readiness conjuncts can never hold: the ENC scene layer supersedes the
#: raster ENC source (``resolve_enc_modes``).  Waiting ``READY_TIMEOUT_S`` per
#: step stalled a real run for a minute on each of its ~80 steps.  Counter-only
#: readiness waits only for the scene worker's own publication, bounded here,
#: and records an unready step instead of blocking.
COUNTER_READY_TIMEOUT_S = 5.0
#: The event-loop pump after a counter-only step.  The scene worker publishes
#: from its own thread, and the GPU item uploads on a presented frame; pumping
#: longer than ``SETTLE_MS`` lets both land before the counters are read.
COUNTER_SETTLE_MS = 400
CHECK_NAMES = ("valid_capture", "coverage_band", "structure_exact", "reproducible")
#: Checks that only a counter-only run can answer.  They are added beside
#: ``CHECK_NAMES`` so a Phase 1 baseline report keeps its original schema.
COUNTER_CHECK_NAMES = (
    "revisits_add_no_geometry",
    "no_resident_geometry_reupload",
    "per_move_cost_flat",
    "same_camera_no_rebuild",
)
#: Gate 3 (issue #213) multi-cell checks.  They are added beside the Phase 1
#: names only for an ENC-scene pixel run, so a Gate 1/2 baseline report keeps
#: its original schema.
GATE3_CHECK_NAMES = (
    "ordered_identity_exact",
    "coverage_regions_match",
    "switch_return_no_retessellation",
    "memory_cap_reported",
    "no_stale_frame",
    "missing_cell_explicit",
)
#: Per-cell states that mean a planned cell produced no geometry.  Each one
#: must be named explicitly with its reason: a missing cell is a state, never
#: a silent absence.
MISSING_CELL_STATES = ("unavailable", "oversized", "error")
#: The worker's cache byte caps, mirrored from
#: ``sailworks/qt/encscenelayer.py`` (MESH_CACHE_BYTES, SCENE_CACHE_BYTES,
#: SCENE_STORE_BYTES) so the harness can report them without importing Qt.
#: The layer's own live values are used when its caches expose them.
CACHE_BYTE_CAPS = {
    "mesh_cap_bytes": 256 * 1024 * 1024,
    "scene_cap_bytes": 64 * 1024 * 1024,
    "store_cap_bytes": 256 * 1024 * 1024,
}
#: Steps whose re-tessellation Gate 3 judges.  ``return`` always re-issues a
#: camera the repeat already painted; a switch to a camera painted earlier in
#: the same repeat is judged the same way.
SWITCH_RETURN_STEPS = ("switch_cell", "return")
#: Counters a revisit to an already-visited camera must not move: new
#: triangulations (meshing) and a mesh/scene/store lookup miss would show up
#: here.  Cells genuinely entering a wider view are not a revisit.
COUNTER_RETESSELLATION_KEYS = ("mesh_triangulations", "scene_misses", "store_misses")
#: The GPU item's upload count: one publication per new scene, never a
#: re-upload of resident geometry on a camera move.
COUNTER_REUPLOAD_KEYS = ("upload_count",)
COLD_STEP = "cold"
#: Steps whose assembly cost must not scale with the view.  ``cold`` is the
#: build baseline and the same-camera no-op step assembles nothing, so both are
#: excluded; every remaining step is a camera move.
COUNTER_FLAT_STEPS = (
    "pan",
    "pan_back",
    "zoom_out",
    "zoom_back",
    "switch_cell",
    "return",
)
#: How far one camera-move class's *median* assembly time may sit above the
#: median move of the same density.  A per-class median, not the max of a
#: handful of samples: a single host-contention outlier cannot fail the gate.
ASSEMBLY_FLATNESS_RATIO = 3.0
#: The larger-view step (``zoom_out``, 1.5x the same-zoom field) may not cost
#: more than this multiple of the same-zoom moves.  This states the Gate 2
#: criterion directly: assembly cost does not grow with the view.
ASSEMBLY_VIEW_SCALING_RATIO = 2.0
#: Repeats excluded from flatness: repeat 0 is the warm-up pass, whose first
#: builds pay for cache fill, shader compilation and pipeline start-up.  Those
#: costs are real but they are not per-move view scaling.
ASSEMBLY_WARMUP_REPEATS = (0,)
#: A density needs at least this many steady-state moves to yield a verdict;
#: below it the density is not evaluated rather than failed.
ASSEMBLY_FLATNESS_MIN_SAMPLES = 4
#: A per-class median needs at least this many moves to stand in for its class
#: and to be compared against the same-zoom moves.
ASSEMBLY_CLASS_MIN_SAMPLES = 2
TIMING_KEYS = (
    "static_ms",
    "columns_ms",
    "copy_ms",
    "isobars_ms",
    "composition_ms",
    "queue_ms",
)
#: Stage keys the ENC scene layer publishes in ``metadata["timings"]``.  The
#: value is milliseconds rounded to three decimals, the same as ``assembly_ms``.
#: ``cell_open`` contains ``scene_cache``/``scene_load``, which contains the
#: store/OGR split (``store_get``/``decode_scene`` on a hit, ``store_build``
#: with ``ogr_open``/``ogr_scan`` on a miss), so the breakdown nests.
ENC_TIMING_KEYS = (
    "assembly_ms",
    "cell_open",
    "scene_cache",
    "scene_load",
    "store_get",
    "decode_scene",
    "store_build",
    "ogr_open",
    "ogr_scan",
    "query",
    "wkb",
    "simplify",
    "clip",
    "triangulate",
    "stroke_line",
    "stroke_outline",
    "markers",
    "soundings",
    "colour",
    "account",
    "assemble",
)
REQUIRED_KEYS = (
    "tool",
    "schema_version",
    "issue",
    "phase",
    "fixture_sha256",
    "dpr_requested",
    "dpr_measured",
    "scenarios",
    "repeats",
    "sequence",
    "repeats_summary",
    "timings",
    "checks",
    "complete",
    "notes",
)
SAMPLE_KEYS = (
    "step",
    "density",
    "repeat",
    "dpr",
    "camera",
    "ready",
    "selected",
    "coverage_selection",
    "coverage",
    "coverage_enc_extent",
    "enc_extent",
    "cells_order",
    "cell_states",
    "nonblank",
    "stable_readback",
    "generation",
    "interaction_ms",
    "composition_ms",
    "queue_ms",
    "request_to_publish_ms",
    "publish_to_adopt_ms",
    "enc_scene_counters",
)
NOTES = (
    "Spans are wall-clock proxies, not presentation FPS.",
    "MAE/p99 are diagnostics under reviewed-differences policy.",
    "coverage_band is evaluated on the reference's own ENC extent; the "
    "full-frame coverage is kept as a diagnostic because the application "
    "correctly fills the whole viewport outside that extent.",
    "Per-frame interaction is input\u2192afterRendering; it is not presentation FPS.",
    "Device pixel ratio is set outside the process by QT_SCALE_FACTOR; every "
    "sample is tagged with the ratio measured in the process.",
    "No renderer, default setting or production code is changed by this harness.",
)


# --------------------------------------------------------------------------
# Pure helpers: imported and tested without Qt.
# --------------------------------------------------------------------------


def _percentile(ordered, fraction):
    """Linear-interpolated percentile of an already sorted sequence."""
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def summary(values):
    """p50/p95/max of a sample, or ``{"count": 0}`` when nothing was recorded."""
    ordered = sorted(float(value) for value in values if value is not None)
    if not ordered:
        return {"count": 0}
    return {
        "count": len(ordered),
        "p50": round(_percentile(ordered, 0.5), 3),
        "p95": round(_percentile(ordered, 0.95), 3),
        "max": round(ordered[-1], 3),
    }


def build_sequence(base, other):
    """The fixed camera sequence as plain camera dicts.

    ``base`` is the step's fidelity camera and ``other`` the cell a switch
    crosses to.  The result is deterministic and does not mutate its inputs.
    """
    start = {
        "lat": float(base["lat"]),
        "lon": float(base["lon"]),
        "zoom": float(base["zoom"]),
    }
    alternate = {
        "lat": float(other["lat"]),
        "lon": float(other["lon"]),
        "zoom": float(other["zoom"]),
    }
    return (
        {"step": "cold", "camera": dict(start)},
        {"step": "warm", "camera": dict(start)},
        {"step": "pan", "camera": dict(start, lat=start["lat"] + PAN_DEGREES)},
        {"step": "pan_back", "camera": dict(start)},
        {
            "step": "zoom_out",
            "camera": dict(start, zoom=start["zoom"] - ZOOM_OUT_STEPS),
        },
        {"step": "zoom_back", "camera": dict(start)},
        {"step": "switch_cell", "camera": dict(alternate)},
        {"step": "return", "camera": dict(start)},
    )


def switch_scenario(scenarios, anchor):
    """The scenario the fixed sequence crosses to from ``anchor``.

    A requested multi-cell camera wins, so the sequence exercises a view that
    selects several cells.  Otherwise the first listed scenario that is not
    the anchor is used, and the known scenarios stand in when the run listed
    only its anchor.  The fallback therefore still prefers a scenario other
    than the anchor; because multi-cell wins whenever it is a candidate, an
    anchor-only lan run crosses to multi-cell rather than to itself.  The
    default two-scenario run keeps the switch target it always had.
    """
    others = [name for name in scenarios if name != anchor]
    if not others:
        others = [name for name in KNOWN_SCENARIOS if name != anchor]
    if MULTI_CELL_SCENARIO in others:
        return MULTI_CELL_SCENARIO
    return others[0]


def reproducible(coverage, selections):
    """A step is reproducible when coverage stays in a narrow band and the
    selected cells are identical across every repeat.

    Coverage that was never measured (``--experience-only``) contributes a
    zero span; the selection identity still decides.
    """
    values = [float(value) for value in coverage if value is not None]
    span = (max(values) - min(values)) if values else 0.0
    identities = {tuple(sorted(str(cell) for cell in row)) for row in selections}
    return bool(identities) and span <= COVERAGE_SPREAD_LIMIT and len(identities) == 1


def _counter_value(sample, name):
    """One integer residency counter, or ``None`` when it was not recorded."""
    counters = (sample or {}).get("enc_scene_counters") or {}
    value = counters.get(name)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def counter_step_groups(samples):
    """Samples grouped by the repeat they belong to (density and repeat)."""
    groups = {}
    for sample in samples or ():
        key = (sample.get("density"), sample.get("repeat"))
        groups.setdefault(key, []).append(sample)
    return groups


def _camera_key(sample):
    """The camera of one sample, comparable across steps, or ``None``."""
    camera = (sample or {}).get("camera") or {}
    try:
        return (
            round(float(camera["lat"]), 9),
            round(float(camera["lon"]), 9),
            round(float(camera["zoom"]), 9),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _counter_delta(previous, row, name):
    """How far one cumulative counter moved between two samples, or ``None``."""
    before = _counter_value(previous, name)
    after = _counter_value(row, name)
    if before is None or after is None:
        return None
    return after - before


def _generation_value(sample):
    """The layer generation of one sample, preferring the scene counter."""
    value = _counter_value(sample, "layer_generation")
    if value is not None:
        return value
    try:
        return int((sample or {}).get("generation"))
    except (TypeError, ValueError):
        return None


def revisits_add_no_geometry(samples):
    """A return to an already-visited camera adds no meshing and no miss.

    The cumulative ``mesh_triangulations`` counter must be unchanged on such a
    step, and neither ``scene_misses`` nor ``store_misses`` may grow.  New
    cells entering a wider view are *not* a revisit: only a step whose camera
    equals an earlier step's camera of the same repeat is judged.  ``mesh_hits``
    growth is the reuse proof the run reports alongside the counters; it is not
    required here because a layer may answer a revisit from the scene cache
    without a mesh hit.
    """
    if not samples:
        return False
    found_revisit = False
    for rows in counter_step_groups(samples).values():
        seen = set()
        previous = None
        for row in rows:
            key = _camera_key(row)
            # A step's counters can only be attributed to it when the step
            # before it had settled: an unready step's build lands during the
            # next pump, so its geometry would be blamed on the revisit.
            previous_settled = (
                previous is not None and previous.get("ready") is not False
            )
            if key is not None and key in seen and previous_settled:
                for name in COUNTER_RETESSELLATION_KEYS:
                    if _counter_delta(previous, row, name) != 0:
                        return False
                found_revisit = True
            if key is not None:
                seen.add(key)
            previous = row
    return found_revisit


def same_camera_no_rebuild(samples):
    """A consecutive same-camera step builds and uploads nothing.

    ``warm`` is exactly such a step: it re-issues ``cold``'s camera.  The scene
    layer must reuse its publication, so neither the generation nor the
    triangulation or upload counter may move.
    """
    if not samples:
        return False
    found_noop = False
    for rows in counter_step_groups(samples).values():
        previous = None
        for row in rows:
            key = _camera_key(row)
            if (
                previous is not None
                and key is not None
                and key == _camera_key(previous)
            ):
                found_noop = True
                for name in ("mesh_triangulations",) + COUNTER_REUPLOAD_KEYS:
                    if _counter_delta(previous, row, name) != 0:
                        return False
                before = _generation_value(previous)
                after = _generation_value(row)
                if before is None or after is None or after != before:
                    return False
            previous = row
    return found_noop


def no_resident_geometry_reupload(samples):
    """A camera move transforms resident geometry instead of re-uploading it.

    ``upload_count`` is a publication counter (one build per new scene), so it
    may grow when a move genuinely builds a new scene.  It must not grow on a
    same-camera step.  The proof that the move used the resident geometry is
    the item's transform: ``matrix_update_count`` must advance on every camera
    change.
    """
    if not samples:
        return False
    found_move = False
    for rows in counter_step_groups(samples).values():
        previous = None
        for row in rows:
            key = _camera_key(row)
            if previous is not None:
                same = key is not None and key == _camera_key(previous)
                if same:
                    if _counter_delta(previous, row, "upload_count") != 0:
                        return False
                else:
                    found_move = True
                    if row.get("ready") is False:
                        # The step never reached a settled publication; its
                        # matrix update is still in flight, not missing.
                        previous = row
                        continue
                    delta = _counter_delta(previous, row, "matrix_update_count")
                    if delta is None or delta <= 0:
                        return False
            previous = row
    return found_move


def assembly_cost_is_flat(
    samples,
    steps=COUNTER_FLAT_STEPS,
    ratio=ASSEMBLY_FLATNESS_RATIO,
    view_ratio=ASSEMBLY_VIEW_SCALING_RATIO,
    warmup_repeats=ASSEMBLY_WARMUP_REPEATS,
    min_samples=ASSEMBLY_FLATNESS_MIN_SAMPLES,
    class_min_samples=ASSEMBLY_CLASS_MIN_SAMPLES,
):
    """True when per-move assembly time does not scale with the view.

    The question is whether assembling a move costs more as the view grows,
    not whether the host was momentarily busy.  One slow sample is a
    contention artefact (the counters show geometry is constant across the
    sequence), so every statistic here is a median and the warm-up repeat is
    excluded.  Per density, over the repeats that are not warm-up:

    * each camera-move class's median ``assembly_ms`` must stay below
      ``ratio`` times the median move of that density;
    * the larger-view step (``zoom_out``) must stay below ``view_ratio`` times
      the median of the same-zoom moves.  That is the criterion stated
      directly: a wider view must not assemble materially slower than a
      same-zoom move.

    ``None`` means "not evaluated": no assembly time was recorded, or no
    density left enough steady-state moves (``min_samples``, and
    ``class_min_samples`` per class) for a median worth comparing.  It is
    never a silent pass, and never a false failure from missing data.
    """
    if not samples:
        return None
    prepared = []
    for row in samples:
        if row.get("step") not in steps:
            continue
        if row.get("assembly_ms") is None or row.get("assembly_reused"):
            continue
        if row.get("ready") is False:
            continue
        repeat = row.get("repeat")
        try:
            repeat = int(repeat)
        except (TypeError, ValueError):
            repeat = None
        if repeat in warmup_repeats:
            continue
        prepared.append(
            (row.get("density"), str(row.get("step")), float(row["assembly_ms"]))
        )

    by_density = {}
    for density, step, value in prepared:
        by_density.setdefault(density, []).append((step, value))

    evaluated = False
    for rows in by_density.values():
        if len(rows) < min_samples:
            continue
        median = _percentile(sorted(value for _, value in rows), 0.5)
        if median <= 0:
            return False
        evaluated = True
        classes = {}
        for step, value in rows:
            classes.setdefault(step, []).append(value)
        for values in classes.values():
            if len(values) < class_min_samples:
                continue
            if _percentile(sorted(values), 0.5) >= ratio * median:
                return False
        larger = classes.get("zoom_out") or ()
        same_zoom = [
            value
            for step, values in classes.items()
            if step != "zoom_out"
            for value in values
        ]
        if len(larger) < class_min_samples or len(same_zoom) < class_min_samples:
            continue
        if _percentile(sorted(larger), 0.5) >= view_ratio * _percentile(
            sorted(same_zoom), 0.5
        ):
            return False
    return True if evaluated else None


def counter_only_checks(samples):
    """The Gate 2 checks that need counters only, not a framebuffer.

    ``per_move_cost_flat`` is ``None`` when no steady-state assembly time was
    recorded: the value is not silently reported as a failure, nor as a pass.
    """
    return {
        "revisits_add_no_geometry": revisits_add_no_geometry(samples),
        "no_resident_geometry_reupload": no_resident_geometry_reupload(samples),
        "per_move_cost_flat": assembly_cost_is_flat(samples),
        "same_camera_no_rebuild": same_camera_no_rebuild(samples),
    }


# --------------------------------------------------------------------------
# Gate 3: multi-cell identity, residency, memory and explicit missing cells.
# --------------------------------------------------------------------------


def ordered_cell_names(records):
    """The ordered cell names of a ``cells_order`` record list.

    A record is the scene layer's identity mapping (``path``/``name``) or a
    bare path.  The comparison is on the path stem, upper-cased, which is how
    ``check_enc._coverage_selection`` names a reference cell.
    """
    names = []
    for record in records or ():
        if isinstance(record, dict):
            path = record.get("path") or record.get("name") or ""
        else:
            path = record
        names.append(Path(str(path)).stem.upper())
    return tuple(names)


def reference_cell_names(cells):
    """A reference selection as upper-cased cell names, order preserved."""
    return tuple(str(cell).upper() for cell in cells or ())


def ordered_identity_exact(samples):
    """The scene's ordered cells are exactly the reference's ordered cells.

    Every step that carries either an ordered plan or a reference selection is
    judged: a step with a reference selection but no scene order fails, and any
    difference in order or membership fails.  ``None`` when no step carries
    either, so a missing measurement is not a silent pass.
    """
    rows = [
        row
        for row in samples or ()
        if row.get("cells_order") or row.get("coverage_selection")
    ]
    if not rows:
        return None
    for row in rows:
        if not row.get("cells_order"):
            return False
        if ordered_cell_names(row["cells_order"]) != reference_cell_names(
            row.get("coverage_selection")
        ):
            return False
    return True


def coverage_regions_match(samples):
    """The measured coverage of the reference's own extent stays in band.

    This aggregates the existing per-step comparison unchanged: the reference's
    selected cells (``structure_matches``) and its own ENC extent
    (``coverage_enc_extent`` inside :data:`COVERAGE_BAND`).  ``None`` when no
    step measured coverage.
    """
    rows = [row for row in samples or () if row.get("coverage_enc_extent") is not None]
    if not rows:
        return None
    for row in rows:
        if row.get("structure_matches") is not True:
            return False
        value = float(row["coverage_enc_extent"])
        if not COVERAGE_BAND[0] <= value <= COVERAGE_BAND[1]:
            return False
    return True


def switch_return_no_retessellation(samples):
    """A switch or return to a painted camera re-tessellates nothing resident.

    Gate 2's revisit rule, applied to the multi-cell sequence: a step whose
    camera the repeat already painted must not move ``mesh_triangulations``,
    ``scene_misses`` or ``store_misses``.  ``return`` always re-issues the
    anchor camera and is judged; the first ``switch_cell`` reaches a genuinely
    new camera, where cells that enter are recorded in
    :func:`gate3_report` and are not a revisit.  A switch back to a camera the
    repeat already painted is judged like any revisit.  ``None`` when no such
    step was measured, never a silent pass.
    """
    if not samples:
        return None
    judged = 0
    for rows in counter_step_groups(samples).values():
        seen = set()
        previous = None
        for row in rows:
            key = _camera_key(row)
            previous_settled = (
                previous is not None and previous.get("ready") is not False
            )
            if (
                row.get("step") in SWITCH_RETURN_STEPS
                and key is not None
                and key in seen
                and previous_settled
            ):
                judged += 1
                for name in COUNTER_RETESSELLATION_KEYS:
                    if _counter_delta(previous, row, name) != 0:
                        return False
            if key is not None:
                seen.add(key)
            previous = row
    return True if judged else None


def memory_cap_reported(samples):
    """Every step reports the cache caps, and no cache exceeds its cap.

    The caps mirror the worker's static budgets; the resident bytes are the
    layer's own accounting when it exposes them.  A run whose layer exposes no
    bytes still reports the caps with ``bytes_measured`` false, and the check
    is judged on the caps alone.  ``None`` when no step recorded a memory
    snapshot.
    """
    rows = [
        row
        for row in samples or ()
        if (row.get("enc_scene_counters") or {}).get("memory")
    ]
    if not rows:
        return None
    for row in rows:
        memory = row["enc_scene_counters"]["memory"]
        for prefix in ("mesh", "scene", "store"):
            cap = memory.get(f"{prefix}_cap_bytes")
            if not isinstance(cap, int) or cap <= 0:
                return False
            resident = memory.get(f"{prefix}_resident_bytes")
            if resident is not None and int(resident) > int(cap):
                return False
    return True


def no_stale_frame(samples):
    """Every new composition settled; every revisit re-presented that composition.

    A camera's first appearance in a repeat is a new composition, so that step
    must report ``stable_readback``.  A revisit to a camera the repeat already
    settled re-issues a camera that never changed, so its pixels must still be
    the same composition.  Window captures are not byte-reproducible (the
    window carries live content the chart does not own), so a revisit is
    judged on the coarse block-mean ``frame_signature`` within
    :data:`FRAME_SIGNATURE_TOLERANCE` and :data:`FRAME_SIGNATURE_MAX_FRACTION`
    as well as on the digest: identical digests pass, and a signature that
    drifted past tolerance is a genuinely different or stale frame and fails.
    A row with no camera key cannot be classified, so it keeps the strict
    stability requirement.  ``None`` when no frame was captured.
    """
    rows = [row for row in samples or () if row.get("stable_readback") is not None]
    if not rows:
        return None
    for group in counter_step_groups(rows).values():
        settled = {}
        for row in group:
            key = _camera_key(row)
            settle = row.get("capture_settle") or {}
            final = (settle.get("digests") or [None])[-1]
            signature = row.get("frame_signature")
            if key not in settled:
                # First occurrence in this repeat: a genuinely new composition.
                settled[key] = {"digest": final, "signature": signature}
                if not bool(row.get("stable_readback")):
                    return False
                continue
            if key is None:
                continue
            reference = settled[key]
            if signature is not None and reference["signature"] is not None:
                if not signature_drift(signature, reference["signature"])["matches"]:
                    return False
                continue
            if final is None:
                continue
            if reference["digest"] is not None and reference["digest"] != final:
                return False
    return True


def missing_cell_explicit(samples):
    """Every planned cell is named, and every missing one states why.

    ``cell_states`` must cover ``cells_order`` record for record, in plan
    order, so no cell is silently absent.  A cell whose state is
    ``unavailable``, ``oversized`` or ``error`` must carry a reason.  A Cairo
    per-cell raster fallback for a skipped cell was deliberately not built, so
    this check proves the state is explicit, not that raster covers the cell.
    ``None`` when no step recorded states.
    """
    rows = [row for row in samples or () if row.get("cell_states")]
    if not rows:
        return None
    for row in rows:
        order = row.get("cells_order") or ()
        states = row.get("cell_states") or ()
        if len(order) != len(states):
            return False
        for record, state in zip(order, states):
            record_path = (
                (record or {}).get("path") if isinstance(record, dict) else None
            )
            if record_path is not None and str(record_path) != str(state.get("path")):
                return False
            if (
                state.get("state") in MISSING_CELL_STATES
                and not str(state.get("reason") or "").strip()
            ):
                return False
    return True


def gate3_checks(samples):
    """Gate 3's verdicts, each ``True``, ``False`` or ``None`` (not measured)."""
    return {
        "ordered_identity_exact": ordered_identity_exact(samples),
        "coverage_regions_match": coverage_regions_match(samples),
        "switch_return_no_retessellation": switch_return_no_retessellation(samples),
        "memory_cap_reported": memory_cap_reported(samples),
        "no_stale_frame": no_stale_frame(samples),
        "missing_cell_explicit": missing_cell_explicit(samples),
    }


def gate3_report(samples):
    """The multi-cell evidence Gate 3 reports beside its verdicts."""
    rows = list(samples or ())
    with_order = [row for row in rows if row.get("cells_order")]
    missing = []
    for row in rows:
        for state in row.get("cell_states") or ():
            if state.get("state") in MISSING_CELL_STATES:
                missing.append(
                    {
                        "density": row.get("density"),
                        "step": row.get("step"),
                        "repeat": row.get("repeat"),
                        "name": state.get("name"),
                        "state": state.get("state"),
                        "reason": state.get("reason") or "",
                    }
                )
    switch_return = []
    for group in counter_step_groups(rows).values():
        seen_cells = ()
        previous = None
        for row in group:
            names = ordered_cell_names(row.get("cells_order"))
            if row.get("step") in SWITCH_RETURN_STEPS:
                new_cells = tuple(name for name in names if name not in seen_cells)
                switch_return.append(
                    {
                        "density": row.get("density"),
                        "step": row.get("step"),
                        "repeat": row.get("repeat"),
                        "cells": len(names),
                        "new_cells": list(new_cells),
                        "mesh_triangulations": _counter_delta(
                            previous, row, "mesh_triangulations"
                        ),
                        "scene_misses": _counter_delta(previous, row, "scene_misses"),
                        "store_misses": _counter_delta(previous, row, "store_misses"),
                    }
                )
            if names:
                seen_cells = tuple(dict.fromkeys(seen_cells + names))
            previous = row
    memory = next(
        (
            (row.get("enc_scene_counters") or {}).get("memory")
            for row in rows
            if (row.get("enc_scene_counters") or {}).get("memory")
        ),
        None,
    ) or {"caps": dict(CACHE_BYTE_CAPS), "bytes_measured": False}
    coverage = [
        float(row["coverage_enc_extent"])
        for row in rows
        if row.get("coverage_enc_extent") is not None
    ]
    captured = [row for row in rows if row.get("stable_readback") is not None]
    return {
        "steps": len(rows),
        "multi_cell_steps": sum(1 for row in with_order if len(row["cells_order"]) > 1),
        "cells_order_steps": len(with_order),
        "cell_states_steps": sum(1 for row in rows if row.get("cell_states")),
        "max_cells": max(
            (len(row.get("cells_order") or ()) for row in rows), default=0
        ),
        "missing_cells": missing,
        "switch_return": switch_return,
        "memory": memory,
        "coverage_enc_extent": {
            "min": round(min(coverage), 4) if coverage else None,
            "max": round(max(coverage), 4) if coverage else None,
        },
        "captured_steps": len(captured),
        "stable_steps": sum(1 for row in captured if row.get("stable_readback")),
    }


def summarise_repeats(samples):
    """Per-step statistics across repeats, keyed ``"<density>/<step>"``."""
    grouped = {}
    for sample in samples:
        grouped.setdefault(f"{sample['density']}/{sample['step']}", []).append(sample)
    report = {}
    for key in sorted(grouped):
        rows = grouped[key]
        coverage = [row.get("coverage") for row in rows]

        def band(name):
            values = [float(row[name]) for row in rows if row.get(name) is not None]
            return {
                "min": round(min(values), 4) if values else None,
                "max": round(max(values), 4) if values else None,
                "span": round(max(values) - min(values), 4) if values else None,
            }

        report[key] = {
            "count": len(rows),
            "ready": all(bool(row.get("ready")) for row in rows),
            "interaction_ms": summary(row.get("interaction_ms") for row in rows),
            "ready_ms": summary(row.get("ready_ms") for row in rows),
            "composition_ms": summary(row.get("composition_ms") for row in rows),
            "queue_ms": summary(row.get("queue_ms") for row in rows),
            "request_to_publish_ms": summary(
                row.get("request_to_publish_ms") for row in rows
            ),
            "publish_to_adopt_ms": summary(
                row.get("publish_to_adopt_ms") for row in rows
            ),
            "coverage": band("coverage"),
            "coverage_enc_extent": band("coverage_enc_extent"),
            "selections": [
                sorted(str(cell) for cell in row.get("selected") or ()) for row in rows
            ],
            "reproducible": reproducible(
                coverage, [row.get("selected") or () for row in rows]
            ),
        }
    return report


def empty_report(scenarios=DEFAULT_SCENARIOS, repeats=5, requested_dpr=1, **kwargs):
    """The report skeleton, so a failed run still says what it attempted."""
    experience_only = bool(kwargs.get("experience_only", False))
    return {
        "tool": TOOL,
        "schema_version": SCHEMA_VERSION,
        "issue": ISSUE,
        "phase": PHASE,
        "fixture_sha256": "",
        "snapshot_accepted": False,
        "dpr_requested": int(requested_dpr),
        "dpr_measured": None,
        "dpr_environment": {
            "QT_SCALE_FACTOR": f"{int(requested_dpr)}",
            "note": "The ratio is applied by Qt from the environment, before "
            "this process builds any widget.",
        },
        "experience_only": experience_only,
        "enc_scene_mode": bool(kwargs.get("enc_scene", False)),
        "counters_only": bool(kwargs.get("counters_only", False)),
        "capture_backend": kwargs.get("capture_backend", "qt"),
        "scenarios": list(scenarios),
        "repeats": int(repeats),
        "sequence": [],
        "repeats_summary": {},
        "timings": {},
        "checks": {name: False for name in CHECK_NAMES},
        "complete": False,
        "notes": list(NOTES),
    }


def validate_report(report):
    """Structural problems in a report; an empty list means it is well formed."""
    problems = [f"missing key: {key}" for key in REQUIRED_KEYS if key not in report]
    checks = report.get("checks") or {}
    problems.extend(
        f"missing check: {name}" for name in CHECK_NAMES if name not in checks
    )
    sequence = report.get("sequence") or ()
    gate3 = [name for name in GATE3_CHECK_NAMES if name in checks]
    if gate3 and len(gate3) != len(GATE3_CHECK_NAMES):
        problems.extend(
            f"missing check: {name}" for name in GATE3_CHECK_NAMES if name not in checks
        )
    for index, row in enumerate(sequence):
        problems.extend(
            f"sequence[{index}] missing {key}" for key in SAMPLE_KEYS if key not in row
        )
        stages = row.get("enc_timings")
        if stages is not None and not isinstance(stages, dict):
            # Optional: an older report or the raster mode has no ENC stages.
            problems.append(f"sequence[{index}].enc_timings must be a mapping")
    if not isinstance(report.get("notes"), list):
        problems.append("notes must be a list")
    if "timings" in report and not isinstance(report["timings"], dict):
        problems.append("timings must be a mapping")
    return problems


def _number(value, digits=3):
    """A JSON-friendly float, or None when the surface did not provide one."""
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _plain(value):
    """JSON-friendly values: tuples, mappings and numpy scalars converted."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def dump_directory(root, density, step, repeat):
    """The per-step directory below ``--dump-frames``; unique per repeat."""
    return Path(root) / f"{step}_{density}_r{repeat}"


def write_step_dump(directory, native, ref_on, ref_off, summary):
    """Write one step's three frames and a summary beside them.

    Diagnostic only, and deliberately outside the compared path: the arrays are
    saved exactly as they were compared (premultiplied BGRA bytes reordered to
    RGBA for a lossless PNG), so an offline diff reproduces ``_fidelity``.
    Nothing here runs unless ``--dump-frames`` was given.
    """
    from PySide6.QtGui import QImage

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, frame in (("native", native), ("ref_on", ref_on), ("ref_off", ref_off)):
        # QImage does not copy the buffer; save while it is still referenced.
        pixels = frame[:, :, [2, 1, 0, 3]].copy()
        height, width = pixels.shape[:2]
        image = QImage(
            pixels.data,
            int(width),
            int(height),
            int(width) * 4,
            QImage.Format.Format_RGBA8888,
        )
        if not image.save(str(directory / f"{name}.png"), "PNG"):
            raise RuntimeError(f"could not write {directory / f'{name}.png'}")
    (directory / "summary.json").write_text(
        json.dumps(_plain(summary), indent=2, default=str)
    )


# --------------------------------------------------------------------------
# Application surfaces, reused from the acceptance tool.
# --------------------------------------------------------------------------


def _load_tool(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_CHECK_ENC = None


def check_enc_module():
    """``tools/check_enc_viewport.py`` as a module, loaded once."""
    global _CHECK_ENC
    if _CHECK_ENC is None:
        _CHECK_ENC = _load_tool("enc_viewport_tool", CHECK_ENC_PATH)
    return _CHECK_ENC


# --------------------------------------------------------------------------
# OS-level capture (CoreGraphics).
#
# Qt's own grab paths are unusable while the ENC scene layer is live: the
# scene-graph grab (``grabToImage``) segfaults and ``grabWindow`` deadlocks on
# macOS.  CoreGraphics copies the window's pixels out of the window server
# without re-entering Qt, which is why the harness offers it as a second
# capture backend.  Nothing here imports Qt at module scope: the tests load
# this module without a Qt install, and ``--capture-backend qt`` never reaches
# this code path.
# --------------------------------------------------------------------------

#: No capture is taken while macOS Screen Recording is denied: a transparent
#: frame is indistinguishable from a valid one downstream, so it must not be
#: measured.  The message names the remedy and the check that failed.
CG_PERMISSION_ERROR = (
    "Screen Recording permission not granted "
    "(CGPreflightScreenCaptureAccess=false); grant it to the terminal/Python "
    "binary in System Settings > Privacy & Security > Screen Recording and "
    "re-run.  No measurement was taken: a blank capture is never compared."
)
#: Fraction of pixels that must carry ink before an OS capture is accepted.
CG_NONBLANK_THRESHOLD = 0.002

_CG_WINDOW_LIST_INCLUDING_WINDOW = 1 << 3
_CG_WINDOW_IMAGE_BOUNDS_IGNORE_FRAMING = 1
_CG_WINDOW_IMAGE_DEFAULT = 0

_CG = {}


def _cg_libraries():
    """The macOS frameworks the CG helpers need, or ``None`` off macOS.

    Loaded once and cached, including failure: a non-macOS host must not pay a
    library probe per call.  ``ctypes`` is imported here rather than at module
    scope so an import of this module stays cheap and Qt-free.
    """
    if "libraries" in _CG:
        return _CG["libraries"]
    libraries = None
    try:
        import ctypes
        import ctypes.util

        # ``find_library`` returns None inside a bare venv without a compiler;
        # the framework path is stable, so use it as the fallback.
        cg_path = ctypes.util.find_library("CoreGraphics") or (
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        cf_path = ctypes.util.find_library("CoreFoundation") or (
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        cg = ctypes.cdll.LoadLibrary(cg_path)
        cf = ctypes.cdll.LoadLibrary(cf_path)
        objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")

        class CGPoint(ctypes.Structure):
            _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

        class CGSize(ctypes.Structure):
            _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]

        class CGRect(ctypes.Structure):
            _fields_ = [("origin", CGPoint), ("size", CGSize)]

        infinite = float("inf")
        null_rect = CGRect(CGPoint(infinite, infinite), CGSize(0.0, 0.0))

        cg.CGWindowListCreateImage.restype = ctypes.c_void_p
        cg.CGWindowListCreateImage.argtypes = [
            CGRect,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        for name in (
            "CGImageGetWidth",
            "CGImageGetHeight",
            "CGImageGetBytesPerRow",
            "CGImageGetDataProvider",
            "CGDataProviderCopyData",
        ):
            function = getattr(cg, name)
            function.restype = ctypes.c_size_t
            function.argtypes = [ctypes.c_void_p]
        # CGDataProviderCopyData returns a CFDataRef, not a size.
        cg.CGDataProviderCopyData.restype = ctypes.c_void_p
        try:
            cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
            cg.CGPreflightScreenCaptureAccess.argtypes = []
        except AttributeError:
            # Older macOS: no preflight API.  ``_cg_available`` then reports
            # False via the AttributeError it raises, which is the safe answer.
            pass

        cf.CFDataGetLength.restype = ctypes.c_long
        cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
        cf.CFDataGetBytePtr.restype = ctypes.c_void_p
        cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [ctypes.c_void_p]

        # The capture asks the window server for the window's own frame, so
        # it needs that frame in screen points.  ``CGWindowListCopyWindowInfo``
        # is serialised through CoreFoundation's plist writer and read with
        # the standard library, which avoids hand-decoding CFDictionary.
        cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
        cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        cf.CFPropertyListCreateData.restype = ctypes.c_void_p
        cf.CFPropertyListCreateData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_long,
            ctypes.c_ulong,
            ctypes.c_void_p,
        ]

        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]

        libraries = {
            "ctypes": ctypes,
            "cg": cg,
            "cf": cf,
            "null_rect": null_rect,
            "cgrect": CGRect,
            "cgpoint": CGPoint,
            "cgsize": CGSize,
            # ``windowNumber`` returns an NSInteger, so it needs its own cast:
            # sending it with a pointer restype aborts with an exception.
            "msg_send": ctypes.CFUNCTYPE(
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
            )(("objc_msgSend", objc)),
            "msg_send_long": ctypes.CFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p
            )(("objc_msgSend", objc)),
            "selector": objc.sel_registerName,
        }
    except (OSError, AttributeError):
        libraries = None
    _CG["libraries"] = libraries
    return libraries


def _cg_available():
    """Whether CoreGraphics may capture the screen (Screen Recording granted)."""
    libraries = _cg_libraries()
    if libraries is None:
        return False
    try:
        return bool(libraries["cg"].CGPreflightScreenCaptureAccess())
    except (AttributeError, OSError):
        return False


def _cg_window_id(widget):
    """The widget's ``CGWindowID``, resolved once per widget.

    ``widget.winId()`` is the ``QNSView*``, not a window number, and sending
    ``windowNumber`` to the view raises ``NSInvalidArgumentException`` (which
    aborts the process).  The view's ``window`` is the ``NSWindow`` that owns
    the number, so the id is cached on the widget after one resolution.
    """
    cached = getattr(widget, "_cg_window_id", None)
    if cached is not None:
        return int(cached)
    libraries = _cg_libraries()
    if libraries is None:
        raise RuntimeError("CoreGraphics is unavailable on this platform")
    ctypes = libraries["ctypes"]
    view = int(widget.winId())
    if not view:
        raise RuntimeError("widget has no native NSView to capture yet")
    window = libraries["msg_send"](
        ctypes.c_void_p(view), libraries["selector"](b"window")
    )
    if not window:
        raise RuntimeError("NSView.window returned nil: the window is not on screen")
    number = int(
        libraries["msg_send_long"](
            ctypes.c_void_p(window), libraries["selector"](b"windowNumber")
        )
    )
    setattr(widget, "_cg_window_id", number)
    return number


#: ``kCGWindowListOptionAll``: every window, so a momentarily off-screen
#: window's frame is still reported.
_CG_WINDOW_LIST_ALL = 0
#: ``kCGWindowListOptionOnScreenOnly``: on-screen windows, front to back.
_CG_WINDOW_LIST_ONSCREEN_ONLY = 1 << 0
#: ``kCFPropertyListXMLFormat_v1_0``: the plist form ``plistlib`` reads.
_CF_PROPERTY_LIST_XML = 100


def _cg_window_list(option=_CG_WINDOW_LIST_ALL):
    """``CGWindowListCopyWindowInfo`` as a list of plist dictionaries.

    With ``_CG_WINDOW_LIST_ONSCREEN_ONLY`` the list is in front-to-back order,
    which is what an occlusion check needs.
    """
    import plistlib

    libraries = _cg_libraries()
    if libraries is None:
        raise RuntimeError("CoreGraphics is unavailable on this platform")
    ctypes = libraries["ctypes"]
    cf = libraries["cf"]
    array = libraries["cg"].CGWindowListCopyWindowInfo(option, 0)
    if not array:
        raise RuntimeError("CoreGraphics returned no window list")
    try:
        data = cf.CFPropertyListCreateData(None, array, _CF_PROPERTY_LIST_XML, 0, None)
        if not data:
            raise RuntimeError("CoreGraphics window list could not be serialised")
        try:
            length = int(cf.CFDataGetLength(data))
            pointer = cf.CFDataGetBytePtr(data)
            raw = ctypes.string_at(pointer, length) if length else b""
        finally:
            cf.CFRelease(data)
    finally:
        cf.CFRelease(array)
    return list(plistlib.loads(raw)) if raw else []


def cg_window_info(widget, entries=None):
    """What the window server reports for the widget's own window.

    ``window_id`` and ``window_owner_pid`` prove the capture target belongs to
    this process, and ``window_bounds`` is the screen rect
    ``CGWindowListCreateImage`` must ask for: with a null rect CoreGraphics
    returns the whole desktop and the window option is effectively ignored.
    Read per capture rather than cached on the widget, because the window can
    move or resize between steps and the frame has to match the image.
    """
    window_id = _cg_window_id(widget)
    entries = _cg_window_list() if entries is None else entries
    for entry in entries:
        if int(entry.get("kCGWindowNumber", -1)) == int(window_id):
            bounds = entry.get("kCGWindowBounds") or {}
            width = float(bounds.get("Width", 0.0))
            height = float(bounds.get("Height", 0.0))
            if width <= 0.0 or height <= 0.0:
                break
            return {
                "window_id": int(window_id),
                "window_owner_pid": int(entry.get("kCGWindowOwnerPID", -1)),
                "window_bounds": (
                    float(bounds.get("X", 0.0)),
                    float(bounds.get("Y", 0.0)),
                    width,
                    height,
                ),
                "window_layer": int(entry.get("kCGWindowLayer", 0)),
                "window_onscreen": bool(entry.get("kCGWindowIsOnscreen", 0)),
            }
    raise RuntimeError(
        f"the window server reports no on-screen frame for window {window_id}"
    )


def cg_window_frame(widget):
    """``(x, y, width, height)`` of the widget's window, in screen points.

    ``CGWindowListCopyWindowInfo`` reports the frame the window server
    composites.  That frame is what :func:`capture_window_cg` must ask for:
    ``CGWindowListCreateImage`` with a null screen rect returns the whole
    desktop regardless of the window option, so the window's own rect is the
    only way to get the window's pixels.
    """
    return cg_window_info(widget)["window_bounds"]


def _cg_frontmost_window_at(point, entries=None):
    """``(window_number, owner_pid)`` of the window at ``point``, or ``None``.

    ``entries`` must be in front-to-back order (``_CG_WINDOW_LIST_ONSCREEN_ONLY``).
    Only normal-layer, non-transparent windows are considered: the menu bar,
    the Dock and overlays sit on other layers and do not occlude content.
    """
    if entries is None:
        entries = _cg_window_list(_CG_WINDOW_LIST_ONSCREEN_ONLY)
    for entry in entries:
        if int(entry.get("kCGWindowLayer", 0)) != 0:
            continue
        try:
            if float(entry.get("kCGWindowAlpha", 1.0)) <= 0.0:
                continue
        except (TypeError, ValueError):
            pass
        bounds = entry.get("kCGWindowBounds") or {}
        left = float(bounds.get("X", 0.0))
        top = float(bounds.get("Y", 0.0))
        width = float(bounds.get("Width", 0.0))
        height = float(bounds.get("Height", 0.0))
        if left <= point[0] <= left + width and top <= point[1] <= top + height:
            return (
                int(entry.get("kCGWindowNumber", -1)),
                int(entry.get("kCGWindowOwnerPID", -1)),
            )
    return None


def chart_sample_points(frame, widget_offset, widget_size):
    """Five screen points inside the chart rectangle, for occlusion checks.

    The centre plus the middle of each half is enough to catch a foreign window
    sitting over the chart without mistaking a docked panel or a menu bar for
    one.
    """
    left = float(frame[0]) + float(widget_offset[0])
    top = float(frame[1]) + float(widget_offset[1])
    width = float(widget_size[0])
    height = float(widget_size[1])
    fractions = ((0.5, 0.5), (0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75))
    return [(left + fx * width, top + fy * height) for fx, fy in fractions]


def _occluding_window(entries, points, window_id):
    """The first sampled chart point covered by another window, or ``None``.

    ``entries`` is the on-screen window list in front-to-back order, so the
    first entry containing a point is what the window server composites there.
    A hit with a different window number -- or no hit at all -- means the
    captured rect would hold someone else's pixels.
    """
    for point in points:
        hit = _cg_frontmost_window_at(point, entries)
        if hit is None or hit[0] != int(window_id):
            return point, hit
    return None


#: How many raise/pump rounds to give the window server before calling a chart
#: occluded.  Activation is asynchronous and the first composite after a raise
#: often still shows the previous front window.
_FRONTMOST_ATTEMPTS = 12
#: Seconds to let the window server composite between attempts.
_FRONTMOST_PUMP = 0.05


def _ensure_window_frontmost(widget, info=None):
    """Raise the widget's window and prove nothing covers the chart.

    ``CGWindowListCreateImage`` returns what the window server composites in
    the requested rect, so a window that is not frontmost yields pixels of the
    window in front of it: an OpenCode terminal instead of the chart, with the
    same shape and scale.  The window is raised and activated, then the chart's
    own rectangle is sampled until every sample belongs to this window.
    Returns the fresh :func:`cg_window_info`; raises when the window cannot be
    brought forward, and the caller turns that into a failed capture rather
    than comparing a foreign frame.
    """
    raise_window = getattr(widget, "raise_", None)
    activate = getattr(widget, "activateWindow", None) or getattr(
        widget, "requestActivate", None
    )
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
    except ImportError:  # pragma: no cover - a Qt build without widgets
        app = None
    occluded = None
    current = info or cg_window_info(widget)
    for _attempt in range(_FRONTMOST_ATTEMPTS):
        if callable(raise_window):
            raise_window()
        if callable(activate):
            activate()
        if app is not None:
            app.processEvents()
        current = cg_window_info(widget)
        window_size, widget_offset, widget_size, _host = _widget_window_geometry(
            widget, current["window_bounds"]
        )
        points = chart_sample_points(
            current["window_bounds"], widget_offset, widget_size
        )
        occluded = _occluding_window(
            _cg_window_list(_CG_WINDOW_LIST_ONSCREEN_ONLY), points, current["window_id"]
        )
        if occluded is None:
            return current
        time.sleep(_FRONTMOST_PUMP)
    point, hit = occluded
    covered = f"window {hit[0]} of pid {hit[1]}" if hit is not None else "no window"
    raise RuntimeError(
        f"the chart of window {current['window_id']} (pid "
        f"{current['window_owner_pid']}) is not frontmost at {point}: {covered} "
        "is composited there; refusing to capture another window's pixels"
    )


def _capture_window_problem(info, window_id, pid=None):
    """Why a window-server entry cannot be this chart's capture, or ``None``.

    ``CGWindowListCreateImage`` returns the composite of the requested rect, so
    the only trustworthy frame is this process's own window: a foreign owner
    pid means the pixels belong to another application, and a changed window
    number means the window under the capture is not the one asked for.
    """
    pid = os.getpid() if pid is None else pid
    if int(info.get("window_id", -1)) != int(window_id):
        return (
            f"the frontmost window changed under the capture: expected window "
            f"{window_id}, the window server reported {info.get('window_id')}"
        )
    owner_pid = int(info.get("window_owner_pid", -1))
    if owner_pid != pid:
        return (
            f"CoreGraphics window {window_id} belongs to pid {owner_pid}, not to "
            f"this measurement process (pid {pid}); refusing to compare a "
            "foreign window's pixels"
        )
    return None


def capture_window_cg(widget):
    """Capture the widget's window through CoreGraphics as a ``QImage``.

    Returns ``(image, diagnostic)``.  The screen rect passed to
    ``CGWindowListCreateImage`` is the window's own frame
    (:func:`cg_window_frame`); with a null rect CoreGraphics returns the whole
    desktop and the window option is effectively ignored.  The image is the
    frame scaled by the display's pixels per point, still including the title
    bar, so :func:`_widget_window_geometry` insets the client area.  The image
    is premultiplied first, the same byte layout as the framebuffer.

    Before the capture the window is brought frontmost and the window server is
    asked which window actually owns the chart rect: the pixels
    ``CGWindowListCreateImage`` returns are the composite, so an occluding
    window (another application's terminal, for example) produces a frame with
    the right shape and scale that is not the chart at all.  A capture whose
    window belongs to another process, or whose chart is covered, raises
    instead of being compared.
    """
    from PySide6.QtGui import QImage

    libraries = _cg_libraries()
    if libraries is None:
        raise RuntimeError("CoreGraphics is unavailable on this platform")
    ctypes = libraries["ctypes"]
    cg = libraries["cg"]
    window_id = _cg_window_id(widget)
    info = _ensure_window_frontmost(widget)
    problem = _capture_window_problem(info, window_id)
    if problem is not None:
        raise RuntimeError(problem)
    owner_pid = int(info["window_owner_pid"])
    frame_rect = info["window_bounds"]
    screen_bounds = libraries["cgrect"](
        libraries["cgpoint"](frame_rect[0], frame_rect[1]),
        libraries["cgsize"](frame_rect[2], frame_rect[3]),
    )
    image = cg.CGWindowListCreateImage(
        screen_bounds,
        _CG_WINDOW_LIST_INCLUDING_WINDOW | _CG_WINDOW_IMAGE_BOUNDS_IGNORE_FRAMING,
        ctypes.c_uint32(window_id),
        _CG_WINDOW_IMAGE_DEFAULT,
    )
    if not image:
        # NULL with the window on screen means the capture was refused.
        raise RuntimeError(
            f"CGWindowListCreateImage returned NULL for window {window_id}"
        )
    try:
        width = int(cg.CGImageGetWidth(image))
        height = int(cg.CGImageGetHeight(image))
        stride = int(cg.CGImageGetBytesPerRow(image))
        provider = cg.CGImageGetDataProvider(image)
        if not provider:
            raise RuntimeError("CoreGraphics image has no data provider")
        data = cg.CGDataProviderCopyData(provider)
        if not data:
            raise RuntimeError("CoreGraphics data provider returned no pixels")
        try:
            length = int(libraries["cf"].CFDataGetLength(data))
            pointer = libraries["cf"].CFDataGetBytePtr(data)
            raw = ctypes.string_at(pointer, length) if length else b""
        finally:
            libraries["cf"].CFRelease(data)
    finally:
        libraries["cf"].CFRelease(image)
    if not raw:  # pragma: no cover - defensive: NULL is caught above
        raise RuntimeError("CoreGraphics capture returned no bytes")
    # QImage borrows ``raw``; ``copy`` detaches before it leaves scope.
    borrowed = QImage(
        raw, width, height, stride, QImage.Format.Format_ARGB32_Premultiplied
    )
    if borrowed.isNull():
        raise RuntimeError("CoreGraphics capture could not be wrapped as a QImage")
    frame = borrowed.copy()
    diagnostic = {
        "api": "CGWindowListCreateImage",
        "window_id": int(window_id),
        "window_owner_pid": owner_pid,
        "window_bounds": [round(value, 3) for value in frame_rect],
        "window_layer": int(info.get("window_layer", 0)),
        "width": width,
        "height": height,
        "stride": stride,
        "bytes": len(raw),
        "screen_bounds": [round(value, 3) for value in frame_rect],
    }
    return frame, diagnostic


def cg_frame_to_array(image, format_premultiplied=None):
    """A captured ``QImage`` as premultiplied BGRA ``(h, w, 4)`` bytes.

    That is the layout ``check_enc._grab`` returns and the fidelity math
    (``_image_frame``/``_surface_array``/``_fidelity``) consumes, so a captured
    frame is directly comparable with a reference surface.  ``format_`` may be
    passed explicitly by tests to avoid importing Qt.
    """
    import numpy as np

    if image is None or image.isNull():
        raise RuntimeError("CoreGraphics capture produced no image")
    if format_premultiplied is None:
        from PySide6.QtGui import QImage

        format_premultiplied = QImage.Format.Format_ARGB32_Premultiplied
    converted = image.convertToFormat(format_premultiplied)
    if converted is None or converted.isNull():
        raise RuntimeError(
            "CoreGraphics capture could not be converted to " "ARGB32_Premultiplied"
        )
    height = int(converted.height())
    width = int(converted.width())
    stride = int(converted.bytesPerLine())
    frame = (
        np.frombuffer(converted.constBits(), np.uint8)
        .reshape(height, stride)[:, : width * 4]
        .reshape(height, width, 4)
        .copy()
    )
    del converted
    return frame


def nonblank_fraction(frame):
    """Fraction of pixels carrying visible ink, premultiplied BGRA.

    A refused capture is fully transparent (every channel zero), so this is
    the check that keeps a blank OS capture from being compared as if it were
    a painted frame.
    """
    import numpy as np

    array = np.asarray(frame)
    if array.ndim != 3 or array.shape[2] < 4 or not array.size:
        return 0.0
    ink = (array[:, :, 3] > 8) | (array[:, :, :3] > 8).any(axis=2)
    return float(ink.mean())


#: Per-channel levels two consecutive OS captures may differ by and still
#: count as the same frame.  A framebuffer readback is byte-reproducible; a
#: window capture is not, because the window carries live content the chart
#: does not own (a caret, a clock, an antialiased edge re-composited by the
#: window server).  The Qt path keeps byte equality.
CG_SETTLE_TOLERANCE = 4
#: Fraction of pixels that may exceed :data:`CG_SETTLE_TOLERANCE` before the
#: pair is rejected.  This only decides whether a frame is settled enough to
#: measure; it never relaxes a fidelity or coverage threshold.
CG_SETTLE_MAX_FRACTION = 0.002
#: The capture's pixels per logical point may differ between the two axes by
#: at most this relative amount before the image is rejected as "not the
#: window's content" rather than silently stretched.
CG_CROP_SCALE_TOLERANCE = 0.02
#: A chart crop thinner than this fraction of the target is too small to
#: carry a meaningful comparison at the requested density.
CG_CROP_MIN_SCALE = 0.5

#: Spatial grid for the coarse revisit signature.  A window capture is not
#: byte-reproducible, so a revisit is proven by a small block-mean signature
#: rather than by digest equality.
FRAME_SIGNATURE_GRID = 8
#: Per-block (and per-channel) mean levels two captures of the same
#: composition may differ by.  Averaging over a block of pixels removes the
#: live, high-frequency content the chart does not own; a genuinely different
#: composition moves every block by far more than this.
FRAME_SIGNATURE_TOLERANCE = 2.0
#: Fraction of blocks that may exceed :data:`FRAME_SIGNATURE_TOLERANCE`
#: before the pair counts as a different composition.  This only decides
#: "is this the same chart composition"; it never relaxes a fidelity or
#: coverage threshold.
FRAME_SIGNATURE_MAX_FRACTION = 0.05


def frame_signature(array, grid=FRAME_SIGNATURE_GRID):
    """A coarse block-mean signature of one captured frame.

    Returns ``grid * grid * channels`` floats: the mean RGBA value of each
    block, row-major with the channels innermost, taken on the array the
    capture path already produces (premultiplied BGRA bytes).  Blocks are
    equal-sized crops of the frame, so the signature is shape-agnostic between
    two captures of the same composition.  Empty for an array that is not a
    non-empty 3-D image.

    This signature exists because a window capture carries live content the
    chart does not own, so five captures of one camera produce five different
    digests.  Averaging each block keeps the composition and discards the
    bytes.
    """
    import numpy as np

    array = np.asarray(array)
    if array.ndim != 3 or not array.size:
        return []
    height, width, channels = array.shape
    grid = max(int(grid), 1)
    rows = min(grid, height)
    columns = min(grid, width)
    block_h = height // rows
    block_w = width // columns
    if block_h <= 0 or block_w <= 0:
        return []
    trimmed = array[: block_h * rows, : block_w * columns, :channels]
    blocks = trimmed.reshape(rows, block_h, columns, block_w, channels)
    means = blocks.mean(axis=(1, 3), dtype="float64")
    return [round(float(value), 3) for value in means.reshape(-1)]


def signature_drift(
    first,
    second,
    *,
    tolerance=FRAME_SIGNATURE_TOLERANCE,
    max_fraction=FRAME_SIGNATURE_MAX_FRACTION,
):
    """How far two frame signatures drifted, and whether they still match.

    ``first`` is the signature under test and ``second`` the reference the
    revisit compares against.  Each block's drift is its worst channel
    difference.  Returns ``max_delta`` (the largest block drift),
    ``differing_fraction`` (the share of blocks past ``tolerance``), ``blocks``
    and ``matches``.  A missing or mismatched signature never matches: it was
    not a comparable frame.
    """
    import numpy as np

    left = np.asarray(first, dtype="float64").reshape(-1)
    right = np.asarray(second, dtype="float64").reshape(-1)
    if not left.size or left.shape != right.shape or left.size % 4:
        return {
            "max_delta": None,
            "differing_fraction": 1.0,
            "blocks": 0,
            "matches": False,
        }
    per_block = np.abs(left - right).reshape(-1, 4).max(axis=1)
    differing = int((per_block > float(tolerance)).sum())
    fraction = differing / int(per_block.size)
    return {
        "max_delta": round(float(per_block.max()), 3),
        "differing_fraction": round(fraction, 6),
        "blocks": int(per_block.size),
        "matches": bool(fraction <= float(max_fraction)),
    }


def frames_within_tolerance(
    first,
    second,
    *,
    tolerance=CG_SETTLE_TOLERANCE,
    max_fraction=CG_SETTLE_MAX_FRACTION,
    ignore_rows=0,
):
    """Whether two captures are the same frame within a pixel tolerance.

    Returns a diagnostic dict with ``settled``, ``differing_fraction`` (the
    share of pixels whose worst channel moved by more than ``tolerance``) and
    ``max_delta``.  ``ignore_rows`` drops that many rows from the top and
    bottom, for a capture that includes a thin animated strip; the default
    keeps every row.

    Comparison is on premultiplied BGRA bytes, the layout both capture paths
    produce.  A shape change is never "settled": the window geometry moved and
    the pair belongs to different demands.
    """
    import numpy as np

    left = np.asarray(first)
    right = np.asarray(second)
    if left.shape != right.shape:
        return {
            "settled": False,
            "differing_fraction": 1.0,
            "max_delta": 255,
            "compared_pixels": 0,
            "reason": "shape_mismatch",
        }
    rows = int(left.shape[0])
    band = min(int(ignore_rows), max(rows // 2 - 1, 0))
    if band:
        left = left[band : rows - band]
        right = right[band : rows - band]
    delta = np.abs(left.astype("int16") - right.astype("int16"))
    flattened = delta.reshape(delta.shape[0] * delta.shape[1], -1)
    worst = flattened.max(axis=1) if flattened.size else np.zeros(0, dtype="int16")
    if not worst.size:
        return {
            "settled": True,
            "differing_fraction": 0.0,
            "max_delta": 0,
            "compared_pixels": 0,
        }
    differing = int((worst > int(tolerance)).sum())
    fraction = differing / int(worst.size)
    return {
        "settled": bool(fraction <= float(max_fraction)),
        "differing_fraction": round(fraction, 6),
        "max_delta": int(worst.max()),
        "compared_pixels": int(worst.size),
        "tolerance": int(tolerance),
        "max_fraction": float(max_fraction),
        "ignore_rows": int(ignore_rows),
    }


def cg_chart_crop(
    image_shape,
    window_size,
    widget_offset,
    widget_size,
    *,
    scale_tolerance=CG_CROP_SCALE_TOLERANCE,
    min_scale=CG_CROP_MIN_SCALE,
    target_size=None,
):
    """The widget's own chart rectangle inside an OS window capture.

    ``kCGWindowImageBoundsIgnoreFraming`` drops the title bar and shadow, but
    the image is still the whole window: toolbars, panels and any desktop a
    translucent window shows through are in it.  The chart rectangle is
    derived from geometry Qt already knows -- the widget's offset and logical
    size in window coordinates, and the window's logical size -- scaled by
    the image's own pixels per logical point (the display and Qt scale
    factors can differ, and a maximised window can hold far more than the
    chart).

    Returns ``(rect, diagnostic)``.  ``rect`` is ``None`` when the mapping
    cannot be trusted: the two axes disagree on the scale (the image is not
    the window's uniformly scaled content), or the widget's rectangle is
    mostly outside the image.  Measuring then stops with that reason instead
    of comparing a guess.
    """
    image_height, image_width = int(image_shape[0]), int(image_shape[1])
    window_width, window_height = int(window_size[0]), int(window_size[1])
    widget_width, widget_height = int(widget_size[0]), int(widget_size[1])
    offset_x, offset_y = int(widget_offset[0]), int(widget_offset[1])
    diagnostic = {
        "image_size": [image_height, image_width],
        "window_size": [window_height, window_width],
        "widget_offset": [offset_y, offset_x],
        "widget_size": [widget_height, widget_width],
    }
    if min(image_width, image_height, window_width, window_height) <= 0:
        diagnostic["reason"] = "empty_geometry"
        return None, diagnostic
    if min(widget_width, widget_height) <= 0:
        diagnostic["reason"] = "empty_widget"
        return None, diagnostic
    scale_x = image_width / float(window_width)
    scale_y = image_height / float(window_height)
    diagnostic["scale"] = [round(scale_y, 6), round(scale_x, 6)]
    largest = max(scale_x, scale_y)
    if largest <= 0 or abs(scale_x - scale_y) / largest > scale_tolerance:
        diagnostic["reason"] = "non_uniform_scale"
        return None, diagnostic
    left = int(round(offset_x * scale_x))
    top = int(round(offset_y * scale_y))
    width = int(round(widget_width * scale_x))
    height = int(round(widget_height * scale_y))
    clipped = 0
    if left < 0:
        clipped += -left * height
        width += left
        left = 0
    if top < 0:
        clipped += -top * width
        height += top
        top = 0
    if left + width > image_width:
        clipped += (left + width - image_width) * height
        width = image_width - left
    if top + height > image_height:
        clipped += (top + height - image_height) * width
        height = image_height - top
    requested = max(
        int(round(widget_width * scale_x)) * int(round(widget_height * scale_y)), 1
    )
    diagnostic["clipped_fraction"] = round(clipped / float(requested), 6)
    if width <= 0 or height <= 0 or diagnostic["clipped_fraction"] > 0.02:
        diagnostic["reason"] = "widget_crop_outside_capture"
        return None, diagnostic
    if target_size is not None:
        target_height, target_width = int(target_size[0]), int(target_size[1])
        if width < target_width * min_scale or height < target_height * min_scale:
            diagnostic["reason"] = "capture_too_small_for_target"
            return None, diagnostic
    rect = {"x": left, "y": top, "width": width, "height": height}
    diagnostic["rect"] = [top, left, height, width]
    return rect, diagnostic


def crop_frame(frame, rect):
    """The ``rect`` region of a frame as a contiguous array."""
    import numpy as np

    top = int(rect["y"])
    left = int(rect["x"])
    height = int(rect["height"])
    width = int(rect["width"])
    return np.ascontiguousarray(
        np.asarray(frame)[top : top + height, left : left + width]
    )


def resize_frame_bilinear(frame, width, height):
    """Resize a premultiplied BGRA frame to ``width`` x ``height``.

    Bilinear sampling: the crop is a pixel grid the reference does not share
    (the window server's pixels per point need not equal the reference
    surface's), so it has to be resampled to the reference grid before the two
    can be compared.  An aspect mismatch is the caller's to reject; this never
    pads or letterboxes.
    """
    import numpy as np

    array = np.asarray(frame)
    source_height, source_width = int(array.shape[0]), int(array.shape[1])
    target_width, target_height = int(width), int(height)
    if (source_height, source_width) == (target_height, target_width):
        return array
    if min(source_width, source_height, target_width, target_height) <= 0:
        raise ValueError("cannot resize an empty frame")
    ys = np.clip(
        (np.arange(target_height) + 0.5) * source_height / target_height - 0.5,
        0,
        source_height - 1,
    )
    xs = np.clip(
        (np.arange(target_width) + 0.5) * source_width / target_width - 0.5,
        0,
        source_width - 1,
    )
    y0 = np.floor(ys).astype("int64")
    x0 = np.floor(xs).astype("int64")
    y1 = np.minimum(y0 + 1, source_height - 1)
    x1 = np.minimum(x0 + 1, source_width - 1)
    weight_y = (ys - y0)[:, None, None]
    weight_x = (xs - x0)[None, :, None]
    sampled = array.astype("float32")
    top = sampled[y0][:, x0] * (1.0 - weight_x) + sampled[y0][:, x1] * weight_x
    bottom = sampled[y1][:, x0] * (1.0 - weight_x) + sampled[y1][:, x1] * weight_x
    blended = top * (1.0 - weight_y) + bottom * weight_y
    return np.clip(blended + 0.5, 0, 255).astype(array.dtype)


def _widget_window_geometry(widget, frame):
    """``(window_size, widget_offset, widget_size, host_kind)``, in points.

    ``frame`` is the window-server frame ``(x, y, width, height)`` the capture
    was taken from.  The capture's pixels per point is derived from it, so the
    widget's rectangle has to be expressed in the same screen points: the
    frame's size is the window size, and the widget's client area sits at the
    bottom of the frame (a titled window keeps its title bar above the client
    area).

    A ``QWidget`` is a child of the native window, so its offset inside the
    client area comes from ``mapTo()``.  A top-level ``QQuickView`` *is* the
    window: ``QQuickWindow``/``QWindow`` has neither ``window()`` nor
    ``mapTo()``, so its client area starts at the frame origin and its
    logical size is the window's client size.  ``widget.winId()`` still
    resolves the NSView for the capture itself.

    Qt is imported here, not at module scope: the harness stays importable
    without a Qt binding, the same way the capture path does.
    """
    frame_width = float(frame[2])
    frame_height = float(frame[3])
    window = getattr(widget, "window", None)
    map_to = getattr(widget, "mapTo", None)
    try:
        from PySide6.QtQuick import QQuickWindow
    except ImportError:  # pragma: no cover - a Qt build without Quick
        QQuickWindow = None  # noqa: N806 - mirrors the imported name
    is_quick_window = QQuickWindow is not None and isinstance(widget, QQuickWindow)
    if callable(window) and callable(map_to) and not is_quick_window:
        from PySide6.QtCore import QPoint

        host_kind = "qwidget"
        parent = window()
        client_logical = (parent.width(), parent.height())
        offset = map_to(parent, QPoint(0, 0))
        origin_logical = (offset.x(), offset.y())
    else:
        # No QWidget geometry API: the host is its own top-level window.
        host_kind = "qquickwindow" if is_quick_window else "top_level"
        client_logical = (widget.width(), widget.height())
        origin_logical = (0, 0)
    if (
        frame_width <= 0
        or frame_height <= 0
        or client_logical[0] <= 0
        or client_logical[1] <= 0
    ):
        return (
            (frame_width, frame_height),
            origin_logical,
            (float(widget.width()), float(widget.height())),
            host_kind,
        )
    # The window's horizontal extent is its client width: CoreGraphics reports
    # the frame without side borders, so width is the safe anchor.
    points_per_logical = frame_width / float(client_logical[0])
    client_height = client_logical[1] * points_per_logical
    top_inset = max(frame_height - client_height, 0.0)
    widget_offset = (
        origin_logical[0] * points_per_logical,
        origin_logical[1] * points_per_logical + top_inset,
    )
    widget_size = (
        widget.width() * points_per_logical,
        widget.height() * points_per_logical,
    )
    return (frame_width, frame_height), widget_offset, widget_size, host_kind


def cg_grab(diagnostics=None):
    """A ``check_enc._grab``-compatible callable that captures via CoreGraphics.

    Each frame is cropped to the widget's own chart rectangle before it
    becomes a frame, so the title bar, toolbars, panels and any desktop behind
    a translucent window never reach ``_fidelity``.  The crop is recomputed
    per capture from the widget's current geometry, so a window that is
    resized mid-run still maps correctly.

    The capture's geometry and ink fraction are recorded in ``diagnostics`` so
    a sample can report what was captured.  A blank capture raises instead of
    returning, and so does a capture that cannot be mapped to the chart: a
    whole-window frame must never reach ``_fidelity`` looking like a valid
    one.
    """

    def grab(widget):
        image, diagnostic = capture_window_cg(widget)
        frame = cg_frame_to_array(image)
        window_size, widget_offset, widget_size, host_kind = _widget_window_geometry(
            widget, cg_window_frame(widget)
        )
        rect, geometry = cg_chart_crop(
            frame.shape,
            window_size,
            widget_offset,
            widget_size,
        )
        geometry["host_kind"] = host_kind
        if rect is not None:
            # The capture must hold the whole chart at its full size.  A
            # positive ``clipped_fraction`` means part of the chart fell
            # outside the captured rect (the window moved, or the capture was
            # taken from an occluding window at a different size), and a crop
            # shape that differs from the widget's scaled size is the same
            # failure stated in pixels.  Neither may reach ``_fidelity``.
            scale_y, scale_x = geometry["scale"]
            geometry["expected_chart_shape"] = [
                int(round(int(widget_size[1]) * scale_y)),
                int(round(int(widget_size[0]) * scale_x)),
            ]
            if geometry.get("clipped_fraction", 0.0) > 0.0:
                rect = None
                geometry["reason"] = "chart_crop_clipped"
            elif [rect["height"], rect["width"]] != geometry["expected_chart_shape"]:
                rect = None
                geometry["reason"] = "chart_shape_mismatch"
        if rect is None:
            if diagnostics is not None:
                diagnostics.clear()
                diagnostics.update(diagnostic)
                diagnostics.update(geometry)
            raise RuntimeError(
                "CoreGraphics capture could not be cropped to the chart "
                f"({geometry['reason']}: image={geometry['image_size']}, "
                f"window={geometry['window_size']}, host={host_kind}, "
                f"widget_offset={geometry['widget_offset']}, "
                f"widget_size={geometry['widget_size']}, "
                f"scale={geometry.get('scale')}); refusing to compare a "
                "whole-window frame as if it were the chart"
            )
        chart = crop_frame(frame, rect)
        fraction = nonblank_fraction(chart)
        if diagnostics is not None:
            diagnostics.clear()
            diagnostics.update(diagnostic)
            diagnostics.update(geometry)
            diagnostics["chart_shape"] = list(chart.shape[:2])
            diagnostics["nonblank_fraction"] = round(fraction, 6)
        if fraction <= CG_NONBLANK_THRESHOLD:
            raise RuntimeError(
                "CoreGraphics capture is blank after cropping to the chart "
                f"(nonblank_fraction={fraction:.6f}, threshold "
                f"{CG_NONBLANK_THRESHOLD}); refusing to compare a transparent "
                "frame.  Check Screen Recording permission, not the renderer."
            )
        return chart

    return grab


class _RenderingClock:
    """Input-to-frame stamps from the view's own ``afterRendering`` signal.

    ``afterRendering`` is emitted by Qt after the scene graph has rendered, so
    the first stamp after a mark is the first frame that could carry the input.
    The sampler only appends a timestamp; it never blocks the render thread.
    """

    def __init__(self, widget):
        self._stamps = []
        self.connected = False
        hook = getattr(widget, "afterRendering", None)
        if hook is not None and hasattr(hook, "connect"):
            hook.connect(self._record)
            self.connected = True

    def _record(self, *_args):
        self._stamps.append(time.perf_counter())

    def mark(self):
        del self._stamps[:]

    def since(self, started):
        for stamp in self._stamps:
            if stamp >= started:
                return (stamp - started) * 1000.0
        return None


class _NullProbe:
    """``_reference_composition`` records annotation identity through a probe.

    The Phase 1 harness measures the composed frame, not annotation identity,
    so the probe returns no records.
    """

    def end(self):
        return ()


def _reference(check_enc, cache, layer, camera, dpr, palette, data_dir, descriptors):
    key = (
        round(camera.center_lat, 6),
        round(camera.center_lon, 6),
        round(camera.zoom, 6),
        int(dpr),
    )
    if key not in cache:
        on, off, _annotations, _records = check_enc._reference_composition(
            layer, camera, dpr, palette, data_dir, descriptors, _NullProbe()
        )
        cache[key] = (on, off)
    return cache[key]


def _coverage(check_enc, cache, descriptors, camera, dpr, palette):
    key = (
        round(camera.center_lat, 6),
        round(camera.center_lon, 6),
        round(camera.zoom, 6),
        int(dpr),
    )
    if key not in cache:
        names, _identities = check_enc._coverage_selection(
            descriptors, camera, dpr, palette
        )
        cache[key] = tuple(str(name) for name in names)
    return cache[key]


CAPTURE_MAX_ATTEMPTS = 40
CAPTURE_TIMEOUT_S = 15.0
CAPTURE_PUMP_MS = 100

#: The content-item grab is the harness's only readback path on a native
#: viewport, and at DPR 2 every grab allocates a window-sized framebuffer
#: (1486x1420 logical -> 2972x2840 physical, ~34 MB).  A single miss is a
#: transient readback stall and is worth one retry after a forced frame.  A run
#: of misses means the grab pipeline itself is wedged -- the chart keeps
#: publishing generations while every grab times out -- and retrying only burns
#: the 5 s grab timeout on every later step.
READBACK_RETRY_ATTEMPTS = 2
READBACK_RETRY_PUMP_MS = 250
READBACK_DEAD_AFTER = 3

_UNKNOWN = object()


def frame_digest(frame):
    """A short shape-tagged digest of one readback frame.

    Pure Python on purpose: the harness tests must run without numpy or Qt.
    """
    shape = tuple(int(size) for size in frame.shape)
    digest = hashlib.sha256(frame.tobytes()).hexdigest()[:16]
    return "x".join(str(size) for size in shape) + ":" + digest


def settled_frames(frames):
    """True only when the last two frames match in shape and bytes."""
    frames = list(frames)
    if len(frames) < 2:
        return False
    previous, latest = frames[-2], frames[-1]
    if tuple(previous.shape) != tuple(latest.shape):
        return False
    return previous.tobytes() == latest.tobytes()


def settled_after_demand(frames, pre_demand_digest, *, allow_identical=False):
    """True only when the pair settled *and* the frame post-dates the demand.

    Two byte-identical readbacks are not proof that the new demand was
    painted: both can be the stale frame the view already held before the
    camera changed.  A frame whose digest equals the frame grabbed immediately
    before the demand therefore never counts, however stable it looks.
    ``pre_demand_digest`` of ``None`` means no baseline could be grabbed and
    the pair alone decides.

    ``allow_identical`` covers the one case where an identical frame does
    prove the demand: the demand re-affirmed the camera already on screen, so
    the pixels cannot change and the caller has proved the republish another
    way (an advanced generation).
    """
    frames = list(frames)
    if not settled_frames(frames):
        return False
    if pre_demand_digest is None or allow_identical:
        return True
    return frame_digest(frames[-1]) != pre_demand_digest


def same_view(pre_demand_camera, camera):
    """False only when the demand visibly changes what is on screen.

    ``warm`` re-issues the ``cold`` camera: its readback is identical to the
    pre-demand frame no matter how fresh it is, so its freshness has to be
    proved by the generation instead of the pixels.
    """
    if pre_demand_camera is None or camera is None:
        return False
    return tuple(pre_demand_camera) == tuple(camera)


# Cameras are compared to within the widget's float/rounding normalisation: a
# shift of 1e-6 degrees (about 0.1 m) or 1e-6 zoom is not a visible change, but
# it is exactly the drift that made a re-issued camera look like a new one.
_VIEW_TOLERANCE = 1e-6


def _views_close(first, second, tolerance=_VIEW_TOLERANCE):
    """True when two camera tokens demand the same visible view.

    ``same_view`` is exact; the widget normalises the camera it reads back, so
    a re-issued demand can differ in the last float digits.  Tokens that are
    not numeric (``repr`` fallbacks, ``None``) must match exactly.
    """
    if first is None or second is None:
        return False
    if tuple(first) == tuple(second):
        return True
    try:
        return len(first) == len(second) and all(
            math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=tolerance)
            for left, right in zip(first, second)
        )
    except (TypeError, ValueError):
        return False


def _camera_token(camera):
    """A comparable identity for the camera demand, without requiring ``__eq__``."""
    if camera is None:
        return None
    values = tuple(
        getattr(camera, name, None) for name in ("center_lat", "center_lon", "zoom")
    )
    if any(value is None for value in values):
        return repr(camera)
    return values


# The generation wait is bounded far below the capture timeout: a demand that
# never republishes (``pan_back`` to a camera that is already on screen) would
# otherwise spend the whole capture bound waiting for a generation that never
# comes.  After this cap a previously settled camera may fall back to the pixel
# rule; a camera the session has not painted yet keeps waiting.
GENERATION_WAIT_S = 2.0
GENERATION_WAIT_ATTEMPTS = 8


def _capture(
    check_enc,
    widget,
    *,
    pre_demand_digest=None,
    pre_demand_camera=_UNKNOWN,
    pre_demand_generation=_UNKNOWN,
    expect_generation=_UNKNOWN,
    expect_camera=_UNKNOWN,
    demand_camera=_UNKNOWN,
    revisits_settled_camera=False,
    max_attempts=CAPTURE_MAX_ATTEMPTS,
    timeout_s=CAPTURE_TIMEOUT_S,
    pump_ms=CAPTURE_PUMP_MS,
    pre_pump_ms=None,
    readback_misses=0,
    generation_wait_s=GENERATION_WAIT_S,
    generation_wait_attempts=GENERATION_WAIT_ATTEMPTS,
    grab=None,
    settle_tolerance=None,
    settle_max_fraction=CG_SETTLE_MAX_FRACTION,
    pre_demand_frame=None,
    painted_probe=None,
):
    """Grab until two consecutive readbacks are identical for one demand.

    The camera and the chart generation are held constant: only a byte-equal
    pair while neither has changed counts as settled.  A change of either
    restarts the comparison, because the earlier frame belongs to a different
    demand.  The wait is bounded; on timeout the last frame is returned with
    ``stable=False`` and the digests, never a silent claim of stability.

    A settled pair alone is not enough.  With a settled-frame gate the view
    can already hold the finished frame of the *previous* camera when the new
    demand arrives, and two grabs of that stale frame are byte-identical
    without containing anything new.  ``pre_demand_digest`` is the digest of
    the frame grabbed immediately before the demand: a frame equal to it is
    the pre-update frame and is never accepted.  ``pre_pump_ms`` pumps one
    further frame after readiness so the first grab does not read the
    pre-demand frame by construction.

    ``pre_demand_generation`` is the visible generation before the demand.  A
    frame only post-dates the demand once that generation has moved on: the
    underlay repaints as soon as the camera changes, so a frame can look new
    and settle while the chart is still the pre-demand composition.

    ``demand_camera`` is the camera the step actually asked for, compared with
    ``pre_demand_camera`` to decide ``reasserts_view``.  Comparing the demand
    against the pre-demand camera (instead of the token read back after
    readiness, which the widget may normalise) keeps a re-issued camera -- the
    ``warm`` step -- recognised as one that cannot change pixels.

    ``revisits_settled_camera`` says the demand re-issues a camera this run has
    already painted and settled (``warm``, ``pan_back``).  The caller compares
    the demand spec it built, which is exact; the widget's camera readback
    drifts in the last float digits, so the token comparison alone can miss it.
    Such a demand cannot change the pixels, so an identical frame is accepted
    once the generation has been republished, or -- when the composition is
    already on screen and the scene layer republishes nothing (the opening
    step of a later repeat) -- once the camera is known settled and the frame
    is tolerance-identical to the one already there.  It is also the only case
    where the pixel rule may stand in for a republished generation after the
    bounded wait expires; a newly demanded camera keeps waiting for the
    generation.
    The fallback still requires the frame to differ from ``pre_demand_digest``,
    so two grabs of a frame that predates the demand are never accepted.

    ``settle_tolerance`` switches the settle rule from byte equality to a
    per-pixel tolerance (``frames_within_tolerance``) for an OS capture, whose
    window carries live content the chart does not own.  ``pre_demand_frame``
    is that rule's pre-demand baseline.  ``painted_probe`` replaces the chart
    generation with another proof that the demand was painted (the ENC scene
    layer's own publication, which is what actually repaints in scene mode).
    """
    generation = (
        getattr(widget, "_chart_visible_generation", None)
        if expect_generation is _UNKNOWN
        else expect_generation
    )
    camera = (
        _camera_token(getattr(widget, "camera", None))
        if expect_camera is _UNKNOWN
        else expect_camera
    )
    demanded = camera if demand_camera is _UNKNOWN else demand_camera
    digests = []
    restarts = 0
    attempts = 0
    timeouts = 0
    readback_recoveries = 0
    generation_fallback = False
    # When the caller pins the demand, a frame from any other generation or
    # camera can never be adopted as the settled one.
    pinned = expect_generation is not _UNKNOWN or expect_camera is not _UNKNOWN
    reasserts_view = pre_demand_generation is not _UNKNOWN and (
        bool(revisits_settled_camera)
        or (
            pre_demand_camera is not _UNKNOWN
            and _views_close(pre_demand_camera, demanded)
        )
    )
    # Relaxing the generation requirement is only safe for a camera the run has
    # already painted: there the demanded composition is known to exist, so
    # fresh pixels are enough once the republish wait has expired.
    previously_settled = pre_demand_digest is not None and bool(revisits_settled_camera)
    generation_wait_attempts = min(generation_wait_attempts, max_attempts)
    generation_wait_s = min(generation_wait_s, timeout_s)
    previous = None
    last_pair = None
    stable = False
    frame = None
    changed_after_demand = None if pre_demand_digest is None else False
    started = time.perf_counter()
    waited_s = 0.0

    if pre_pump_ms is not None:
        # One extra frame after readiness: a stale frame must not be reachable
        # as the first grab of the pair.
        check_enc._pump(pre_pump_ms)
        waited_s = time.perf_counter() - started

    while attempts < max_attempts and waited_s < timeout_s:
        frame, readback_attempts, readback_error = _readback(
            check_enc, widget, readback_misses, grab=grab
        )
        if readback_error is not None:
            raise readback_error
        if readback_attempts > 1:
            readback_recoveries += 1
        attempts += 1
        digest = frame_digest(frame)
        digests.append(digest)

        current_generation = getattr(widget, "_chart_visible_generation", None)
        current_camera = _camera_token(getattr(widget, "camera", None))
        # A republished generation is the only proof that the demand was
        # painted.  The underlay repaints as soon as the camera moves, so a
        # fresh-looking frame can still be the pre-demand frame with the chart
        # not yet composed; pixels alone are therefore not enough.
        generation_advanced = pre_demand_generation is _UNKNOWN or (
            current_generation != pre_demand_generation
        )
        if painted_probe is not None:
            # In scene mode the chart generation does not describe the scene
            # layer, which is what repaints.  Its publication is the proof.
            generation_advanced = bool(painted_probe())
        if settle_tolerance is None:
            digest_differs = pre_demand_digest is None or digest != pre_demand_digest
        elif pre_demand_frame is None:
            digest_differs = True
        else:
            # An OS capture never repeats byte for byte, so "the demand
            # changed the pixels" is judged with the settle tolerance.
            digest_differs = not frames_within_tolerance(
                pre_demand_frame,
                frame,
                tolerance=settle_tolerance,
                max_fraction=settle_max_fraction,
            )["settled"]
        # The demanded camera was already painted and settled this run and the
        # frame is tolerance-identical to the pre-demand baseline: the finished
        # composition is already on screen, so no republish is expected.  This
        # is the opening ``cold`` of a later repeat, where the scene layer has
        # nothing new to publish.  A genuinely new camera was never settled and
        # keeps the strict generation rule.
        already_on_screen = (
            previously_settled and pre_demand_frame is not None and not digest_differs
        )
        # A republished generation proves the demand was painted, even when the
        # pixels are identical -- but only for a re-affirmed camera (``warm``
        # re-issues the camera already on screen, so its pixels cannot change).
        # For a genuinely new camera identical pixels are still the pre-demand
        # frame: that is the DPR 2 frame that scored 0.2525 coverage.  A camera
        # this run already settled keeps that proof when the composition is
        # already on screen and the scene layer republishes nothing (the
        # opening step of a later repeat): identical pixels at an already
        # painted camera cannot be a stale frame from an unknown composition.
        allows_identical = reasserts_view and (generation_advanced or already_on_screen)
        # The generation never came for a camera this session has already
        # painted (``pan_back``): once the bounded wait expires, fresh pixels
        # are enough.  A camera that was never settled keeps waiting, because
        # its different-looking frame can still be the pre-demand composition.
        fallback = (
            not generation_advanced
            and previously_settled
            and digest_differs
            and (attempts >= generation_wait_attempts or waited_s >= generation_wait_s)
        )
        if digest_differs or allows_identical:
            changed_after_demand = True
        postdates_demand = (
            (generation_advanced and (digest_differs or allows_identical))
            or already_on_screen
            or fallback
        )
        if fallback:
            generation_fallback = True

        if current_generation != generation or current_camera != camera:
            # The demand moved while we were waiting: start a fresh pair.  A
            # pinned demand is not replaced -- that would accept a frame the
            # caller never asked for.
            if not pinned:
                generation = current_generation
                camera = current_camera
            restarts += 1
            previous = None
        elif not postdates_demand:
            # Either the demanded generation is not on screen yet, or the
            # frame is byte-identical to the one painted before the demand:
            # neither can prove the demand was painted.
            previous = None
        else:
            if previous is not None:
                if settle_tolerance is None:
                    pair_settled = settled_after_demand(
                        (previous, frame),
                        pre_demand_digest,
                        allow_identical=allows_identical,
                    )
                else:
                    last_pair = frames_within_tolerance(
                        previous,
                        frame,
                        tolerance=settle_tolerance,
                        max_fraction=settle_max_fraction,
                    )
                    pair_settled = last_pair["settled"] and (
                        allows_identical
                        or pre_demand_frame is None
                        or not frames_within_tolerance(
                            pre_demand_frame,
                            frame,
                            tolerance=settle_tolerance,
                            max_fraction=settle_max_fraction,
                        )["settled"]
                    )
                if pair_settled:
                    stable = True
                    break
            previous = frame

        waited_s = time.perf_counter() - started
        if waited_s >= timeout_s or attempts >= max_attempts:
            break
        check_enc._pump(pump_ms)
        waited_s = time.perf_counter() - started

    if not stable:
        timeouts += 1

    generation_advanced = pre_demand_generation is _UNKNOWN or (
        getattr(widget, "_chart_visible_generation", None) != pre_demand_generation
    )
    settle = {
        "stable": stable,
        "pre_demand_digest": pre_demand_digest,
        "changed_after_demand": changed_after_demand,
        "reasserts_view": bool(reasserts_view),
        "generation_advanced": bool(generation_advanced),
        "generation_fallback": bool(generation_fallback),
        "generation_wait_s": generation_wait_s,
        "generation_wait_attempts": generation_wait_attempts,
        "pre_demand_generation": (
            None if pre_demand_generation is _UNKNOWN else pre_demand_generation
        ),
        "attempts": attempts,
        "restarts": restarts,
        "timeouts": timeouts,
        "readback_recoveries": readback_recoveries,
        "readback_misses": readback_misses,
        "waited_ms": round(waited_s * 1000.0, 3),
        "timeout_s": timeout_s,
        "pump_ms": pump_ms,
        "pre_pump_ms": pre_pump_ms,
        "digests": digests,
        "grab_digests": digests,
    }
    if settle_tolerance is not None:
        # The tolerance is part of the measurement: it says how much of the
        # window may have moved while the frame counted as settled.
        settle["settle_tolerance"] = int(settle_tolerance)
        settle["settle_max_fraction"] = float(settle_max_fraction)
        settle["settle_differing_fraction"] = (
            None if last_pair is None else last_pair["differing_fraction"]
        )
        settle["settle_max_delta"] = (
            None if last_pair is None else last_pair["max_delta"]
        )
        settle["settled_pair"] = (
            None if last_pair is None else bool(last_pair["settled"])
        )
    if not stable and not generation_advanced:
        settle["timeout_reason"] = (
            "the chart generation never advanced past the pre-demand "
            f"generation {pre_demand_generation}: the demanded chart was not "
            "adopted inside the bound"
        )
    elif not stable and changed_after_demand is False:
        settle["timeout_reason"] = (
            "the readback never differed from the frame grabbed before the "
            "demand: the demand was not painted inside the bound"
        )
    elif not stable:
        settle["timeout_reason"] = (
            "no settled pair was observed within the bound (restarts=" f"{restarts})"
        )
    return frame, stable, settle


def readback_should_retry(misses, attempts_left):
    """Pure retry policy for one readback.

    ``misses`` is the number of consecutive steps whose readback already
    failed.  A lone stall is retried; once ``READBACK_DEAD_AFTER`` steps in a
    row have failed the pipeline is gone, so a retry cannot recover it and
    would only pay the grab timeout again.
    """
    return int(attempts_left) > 0 and int(misses) < READBACK_DEAD_AFTER


def _readback(check_enc, widget, misses=0, grab=None):
    """One framebuffer readback, retrying a transient grab failure.

    Returns ``(frame, attempts, error)``.  ``frame`` is ``None`` and ``error``
    is the last exception only when every permitted attempt failed; the caller
    raises it.  A retry asks for another frame before re-requesting the grab: a
    grab whose completion was lost needs the scene graph to render again.

    ``grab`` overrides the capture path (``cg_grab`` for the CoreGraphics
    backend); ``None`` keeps the scene-graph grab the baseline uses.
    """
    attempts = 0
    error = None
    capture = check_enc._grab if grab is None else grab
    while True:
        attempts += 1
        try:
            return capture(widget), attempts, None
        except Exception as exc:  # a transient grab failure, not a fatal one
            error = exc
            if not readback_should_retry(misses, READBACK_RETRY_ATTEMPTS - attempts):
                return None, attempts, error
            update = getattr(widget, "update", None)
            if callable(update):
                try:
                    update()
                except Exception:
                    pass
            check_enc._pump(READBACK_RETRY_PUMP_MS)


def _pre_demand_digest(check_enc, widget, fallback=None, grab=None):
    """The digest of the frame on screen *before* the next demand is issued.

    Grabbed fresh so it describes what the view actually held, not what a
    previous step settled to.  ``fallback`` (the last settled digest) is used
    only when the grab fails, so a capture still has a baseline.
    """
    frame, _attempts, error = _readback(check_enc, widget, grab=grab)
    if error is not None:
        return fallback
    return frame_digest(frame)


def _pre_demand_capture(check_enc, widget, fallback=None, grab=None):
    """The pre-demand frame, its digest, and the frame itself.

    The OS-capture settle rule compares pixels with a tolerance, so it needs
    the frame, not only its digest: two digests never match there.  A failed
    grab falls back to the last settled digest and no frame, which keeps the
    generation requirement as the only proof of painting.
    """
    frame, _attempts, error = _readback(check_enc, widget, grab=grab)
    if error is not None:
        return fallback, None
    return frame_digest(frame), frame


def enc_extent_mask(ref_on, ref_off, epsilon=None, dilate=ENC_MASK_DILATE_PX):
    """The reference's own ENC extent as a boolean mask, plus its diagnostics.

    ``ref_on - ref_off`` is exactly the reference's ink: the same definition
    ``check_enc_viewport._fidelity`` uses (``INK_EPSILON``, any channel).  The
    mask is grown by ``dilate`` pixels so antialiased cell edges that fall just
    below the strict ink test stay inside the extent.  Growth is clamped at the
    frame: the mask never wraps around an edge.

    The application paints the whole viewport, so ink outside this extent is
    correct behaviour; only the extent itself is a like-for-like comparison.
    """
    import numpy as np

    if epsilon is None:
        epsilon = check_enc_module().INK_EPSILON
    on = ref_on.astype("int16")
    off = ref_off.astype("int16")
    mask = (np.abs(on - off) > epsilon).any(axis=2)
    ink_pixels = int(mask.sum())
    height, width = int(mask.shape[0]), int(mask.shape[1])
    if ink_pixels and int(dilate) > 0:
        grown = mask.copy()
        for _ in range(int(dilate)):
            previous = grown
            grown = previous.copy()
            grown[1:, :] |= previous[:-1, :]
            grown[:-1, :] |= previous[1:, :]
            grown[:, 1:] |= previous[:, :-1]
            grown[:, :-1] |= previous[:, 1:]
            grown[1:, 1:] |= previous[:-1, :-1]
            grown[1:, :-1] |= previous[:-1, 1:]
            grown[:-1, 1:] |= previous[1:, :-1]
            grown[:-1, :-1] |= previous[1:, 1:]
        mask = grown
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    bbox = (
        [int(rows[0]), int(rows[-1]), int(cols[0]), int(cols[-1])]
        if rows.size and cols.size
        else None
    )
    mask_pixels = int(mask.sum())
    diagnostic = {
        "bbox": bbox,
        "bbox_order": "row_min, row_max, col_min, col_max (inclusive)",
        "ink_pixels": ink_pixels,
        "mask_pixels": mask_pixels,
        "frame_pixels": height * width,
        "covered_fraction": (
            round(float(mask_pixels) / float(height * width), 4)
            if height * width
            else 0.0
        ),
        "dilate_px": int(dilate),
        "epsilon": float(epsilon),
        "used_full_frame": False,
    }
    return mask, diagnostic


def _fidelity_on_mask(check_enc, native, ref_on, ref_off, mask):
    """``check_enc._fidelity`` restricted to the pixels inside ``mask``.

    The selected pixels are folded into one row, so the gate's own metric (the
    same ``INK_EPSILON``, the same ink sums) is reused unchanged while every
    sum is taken over the shared ENC extent only.  ``None`` when the mask
    selects nothing.
    """
    import numpy as np

    mask = np.asarray(mask, dtype=bool)
    if mask.ndim != 2:
        raise ValueError("mask must be two-dimensional")
    if tuple(mask.shape) != tuple(native.shape[:2]):
        raise ValueError(
            f"mask shape {tuple(mask.shape)} does not match frame "
            f"{tuple(native.shape[:2])}"
        )
    if not mask.any():
        return None

    def folded(array):
        channels = int(array.shape[2])
        return np.ascontiguousarray(array[mask]).reshape(1, -1, channels)

    return check_enc._fidelity(folded(native), folded(ref_on), folded(ref_off))


def _align_shapes(native, ref_on, ref_off):
    """Crop a readback and its references to the shape all three share.

    The Metal readback can be one row or column larger than the Cairo
    reference: the layer rounds its device-pixel rectangle, so a 743-row
    widget reads back as 744 rows.  Cropping the overlap keeps the comparison
    honest; padding, resizing or rescaling would blur the edge and change the
    fidelity numbers.  The deliberate one-pixel crop is recorded in the sample
    so a larger mismatch stays visible in the report.
    """
    height = min(native.shape[0], ref_on.shape[0], ref_off.shape[0])
    width = min(native.shape[1], ref_on.shape[1], ref_off.shape[1])
    note = {
        "native": list(native.shape),
        "reference": list(ref_on.shape),
        "reference_off": list(ref_off.shape),
        "cropped": (
            tuple(native.shape) != (height, width) + tuple(native.shape[2:])
            or tuple(ref_on.shape) != (height, width) + tuple(ref_on.shape[2:])
            or tuple(ref_off.shape) != (height, width) + tuple(ref_off.shape[2:])
        ),
    }
    return (
        native[:height, :width],
        ref_on[:height, :width],
        ref_off[:height, :width],
        note,
    )


def _fit_capture_to_reference(native, reference, tolerance=CG_CROP_SCALE_TOLERANCE):
    """Resample an OS capture onto the reference's pixel grid.

    The captured chart crop is in the window server's pixels per point, which
    need not equal the density the reference surface was rendered at, so it is
    resampled to the reference's exact shape.  A crop whose aspect differs
    from the reference's is not the chart: it is reported in the returned
    note (``aspect_error``) instead of being silently stretched into a
    plausible-looking frame.
    """
    height, width = int(reference.shape[0]), int(reference.shape[1])
    source_height, source_width = int(native.shape[0]), int(native.shape[1])
    note = {
        "capture": [source_height, source_width],
        "reference": [height, width],
        "resized": False,
    }
    if (source_height, source_width) == (height, width):
        return native, note
    ratio = (source_width / float(source_height)) / (width / float(height))
    note["aspect_error"] = round(ratio - 1.0, 6)
    note["resized"] = True
    return resize_frame_bilinear(native, width, height), note


def _step_timings(layer, widget):
    """The three timing classes for one step, each from its own surface."""
    metadata = layer.enc_metadata or {}
    timings = metadata.get("timings") or {}
    generation = widget._chart_visible_generation
    record = {}
    for row in reversed(tuple(getattr(widget, "chart_latencies", ()) or ())):
        if row.get("generation") == generation:
            record = dict(row)
            break
    measured = {key: _number(timings.get(key)) for key in TIMING_KEYS}
    measured["request_to_publish_ms"] = _number(record.get("request_to_publish_ms"))
    measured["publish_to_adopt_ms"] = _number(record.get("publish_to_adopt_ms"))
    measured["swap_ms"] = measured["publish_to_adopt_ms"]
    return measured


#: Residency counters that prove the re-landed scene layer's Gate 2
#: behaviour: after the cold compose, none of these may move on pan or zoom.
ENC_SCENE_COUNTER_KEYS = (
    "mesh_triangulations",
    "mesh_hits",
    "mesh_misses",
    "scene_hits",
    "scene_misses",
    "store_hits",
    "store_misses",
)

#: Item-side counters: ``upload_count`` must stay flat across pan/zoom.
ENC_SCENE_ITEM_KEYS = (
    "upload_count",
    "matrix_update_count",
    "static_mode",
    "visible_cells",
)


def _scene_records(widget, attribute, property_name):
    """Ordered scene records from the layer, or from the viewport property.

    The layer stores the plan beside the scene it published; the viewport
    mirrors it as a property.  Both are read-only here: the first non-empty
    source wins and an absent layer reports an empty list.
    """
    model = getattr(widget, "_enc_scene_model", None)
    candidates = [getattr(model, attribute, None) if model is not None else None]
    reader = getattr(widget, "property", None)
    if callable(reader):
        try:
            candidates.append(reader(property_name))
        except Exception:  # a widget without the property reports no records
            pass
    for records in candidates:
        if records:
            return [_plain(record) for record in records]
    return []


def scene_cell_records(widget):
    """The scene layer's ordered cell identity records, in plan order."""
    return _scene_records(widget, "cells_order", "enc_scene_cells_order")


def scene_cell_states(widget):
    """The scene layer's per-cell states, in the same plan order."""
    return _scene_records(widget, "cell_states", "enc_scene_cell_states")


def _cache_memory(model):
    """Byte caps and resident bytes of the worker's caches, read-only.

    The caches are created on the scene worker thread and account themselves
    by bytes (``max_bytes``/``resident_bytes``).  They may still be absent
    before the first composition, and a cache need not expose the attributes
    at all, so a missing value stays ``None`` and the static caps stand in.
    Nothing here changes the caches or the layer.
    """
    record = dict(CACHE_BYTE_CAPS)
    record.update(
        {
            "mesh_resident_bytes": None,
            "scene_resident_bytes": None,
            "store_resident_bytes": None,
            "bytes_measured": False,
        }
    )
    for prefix, attribute in (
        ("mesh", "_mesh_cache"),
        ("scene", "_scene_cache"),
        ("store", "_scene_store"),
    ):
        cache = getattr(model, attribute, None) if model is not None else None
        if cache is None:
            continue
        cap = getattr(cache, "max_bytes", None)
        resident = getattr(cache, "resident_bytes", None)
        if cap is not None:
            record[f"{prefix}_cap_bytes"] = int(cap)
        if resident is not None:
            record[f"{prefix}_resident_bytes"] = int(resident)
            record["bytes_measured"] = True
    return record


def _enc_scene_counters(widget):
    """Residency counters for one step, or ``None`` without a scene layer.

    Reads the layer's own ``observations()`` and the GPU item's properties.
    Both may be absent on a path that never built the scene layer, so every
    read is defensive and a missing piece is reported as ``None`` rather than
    raising inside the capture loop.  Nothing here changes rendering.
    """
    model = getattr(widget, "_enc_scene_model", None)
    if model is None:
        return None
    reader = getattr(model, "observations", None)
    observations = dict(reader() or {}) if callable(reader) else {}
    layer_counters = observations.get("counters") or {}
    item = getattr(widget, "_enc_scene_item", None)
    record = {
        "item_present": item is not None,
        "layer_state": observations.get("state"),
        "layer_generation": observations.get("generation"),
        "layer_requests": layer_counters.get("requests"),
        "layer_reuses": layer_counters.get("reuses"),
        "layer_assemblies": layer_counters.get("assemblies"),
        "layer_publications": layer_counters.get("publications"),
    }
    for key in ENC_SCENE_COUNTER_KEYS:
        record[key] = observations.get(key)
    for key in ENC_SCENE_ITEM_KEYS:
        value = getattr(item, key, None) if item is not None else None
        if isinstance(value, tuple):
            value = list(value)
        record[key] = value
    record["item_generation"] = (
        getattr(item, "generation", None) if item is not None else None
    )
    # Gate 3 memory reporting: the cache budgets and, when the layer exposes
    # them, the resident byte counts.  Static caps stand in otherwise.
    record["memory"] = _cache_memory(model)
    return record


def _enc_scene_publications(widget):
    """The ENC scene layer's publication count, or ``None`` without a layer."""
    model = getattr(widget, "_enc_scene_model", None)
    if model is None:
        return None
    try:
        counters = model.observations().get("counters") or {}
    except Exception:  # an absent/closed layer is reported, never fatal
        return None
    return counters.get("publications")


def _enc_scene_assembly_ms(widget):
    """The scene layer's own published assembly time, or ``None``.

    ``_step_timings`` reads the chart layer's metadata, which is empty in scene
    mode: the raster ENC source is superseded, so the chart layer composes no
    timings.  The scene layer stores its timings beside the scene it published
    (``EncSceneLayer._published``), and ``published`` exposes them read-only.
    Nothing here changes rendering or the layer's state.
    """
    model = getattr(widget, "_enc_scene_model", None)
    if model is None:
        return None
    try:
        publication = getattr(model, "published", None)
        metadata = publication[2] if publication else None
        timings = (metadata or {}).get("timings") or {}
    except Exception:  # an absent/closed layer is reported, never fatal
        return None
    return _number(timings.get("assembly_ms"))


def _enc_scene_timings(widget):
    """The scene layer's per-stage ENC timings in milliseconds, or ``None``.

    The layer publishes stage durations in seconds beside ``assembly_ms``;
    every timing in the report is milliseconds, so the stages are scaled here
    and ``assembly_ms`` is passed through unchanged.  Read-only: nothing here
    changes the layer's state or the scene it published.
    """
    model = getattr(widget, "_enc_scene_model", None)
    if model is None:
        return None
    try:
        publication = getattr(model, "published", None)
        metadata = publication[2] if publication else None
        timings = (metadata or {}).get("timings") or {}
    except Exception:  # an absent/closed layer is reported, never fatal
        return None
    if not timings:
        return None
    measured = {}
    for key in ENC_TIMING_KEYS:
        value = timings.get(key)
        if value is None:
            measured[key] = None
        elif key == "assembly_ms":
            measured[key] = _number(value)
        else:
            measured[key] = _number(float(value) * 1000.0)
    return measured


def counter_readiness(
    check_enc, layer, widget, before_publications, before_scene, reused
):
    """Readiness conjuncts for one counter-only step.

    A counter-only run never grabs a frame, and the ENC scene layer supersedes
    the chart layer's raster ENC source, so the chart layer's
    ``enc_metadata_*`` conjuncts can never hold in this mode.  They are replaced
    by the scene layer's own publication, which is what Gate 2's residency
    question is about.  ``reused`` names a step whose camera the scene layer
    already presents (``warm``, and ``cold`` after the previous repeat's
    ``return``): those reuse the published scene and never publish again.
    Without a scene layer the chart layer's conjuncts still apply unchanged.
    """
    chart = getattr(widget, "_chart", None)
    conjuncts = {
        "chart_publications_advanced": (
            widget.counters["chart_publications"] > before_publications
        ),
        "chart_property_ready": (
            bool(chart.property("ready")) if chart is not None else True
        ),
    }
    model = getattr(widget, "_enc_scene_model", None)
    if model is None:
        conjuncts.update(
            check_enc._readiness_conjuncts(layer, widget, before_publications)
        )
        return conjuncts
    observations = model.observations()
    counters = observations.get("counters") or {}
    conjuncts["enc_scene_state_ready"] = observations.get("state") == "ready"
    conjuncts["enc_scene_publication_current"] = bool(reused) or (
        before_scene is not None and counters.get("publications", 0) > before_scene
    )
    return conjuncts


def uses_scene_readiness(counters_only, enc_scene, capture_backend):
    """Whether readiness must come from the ENC scene layer, not the chart.

    A counter-only run never grabs, and a CoreGraphics pixel run cannot wait
    on the raster chart layer's ENC conjuncts: ``resolve_enc_modes`` disables
    raster ENC whenever the scene layer is live, so those conjuncts can never
    hold and every step would burn ``READY_TIMEOUT_S``.  The scene layer's own
    publication is the authority in both modes.  A Qt pixel run keeps the
    chart-layer rule so the Gate 1 baselines stay reproducible.
    """
    return bool(counters_only or (enc_scene and capture_backend == "cg"))


def _scene_demand_painted(widget, before_publications, reused):
    """Whether the scene layer presents the demanded composition.

    A reused step (``warm``, and ``cold`` at the start of a later repeat)
    re-issues the camera the layer already presents, so the current
    publication is the demanded one and no new publication is expected.
    """
    if reused:
        return True
    current = _enc_scene_publications(widget)
    if before_publications is None or current is None:
        return True
    return current > before_publications


# --------------------------------------------------------------------------
# The session.
# --------------------------------------------------------------------------


def _session(
    report,
    widget,
    layer,
    base_camera,
    descriptors,
    palette,
    data_dir,
    scenarios,
    repeats,
    dpr,
    experience_only,
    dump_frames=None,
    enc_scene=False,
    counters_only=False,
    capture_backend="qt",
):
    from dataclasses import replace

    check_enc = check_enc_module()
    clock = _RenderingClock(widget)
    report["identity"]["interaction_hook"] = bool(clock.connected)
    references = {}
    coverage_cache = {}
    samples = []
    last_settled_digest = None
    # Consecutive steps whose readback failed.  Fed to ``_readback`` so a
    # wedged grab pipeline stops being retried on every later step.
    readback_misses = 0
    # The CoreGraphics backend replaces only the capture call: settling,
    # digests and fidelity still run on the returned array.  ``cg_capture``
    # carries the last OS capture's geometry and ink fraction into the sample.
    cg_capture = {}
    grab = cg_grab(cg_capture) if capture_backend == "cg" else None
    for density in DENSITIES:
        anchor_name = DENSITY_SCENARIO[density]
        alternate_name = switch_scenario(scenarios, anchor_name)
        sequence = build_sequence(
            check_enc.FIDELITY[anchor_name], check_enc.FIDELITY[alternate_name]
        )
        # Camera specs this run has already painted and settled.  Only those
        # may stand in for a republished generation (see ``_capture``).  This
        # spans repeats: the application keeps the composition across a repeat
        # boundary, so a repeat that opens on an already-settled camera is a
        # revisit and cannot change pixels either.
        settled_specs = []
        for repeat in range(repeats):
            # Camera signatures this repeat already captured, keyed by camera:
            # the coarse proof that a revisit repainted the same composition.
            settled_signatures = {}
            # Last assembly time seen in this repeat, carried across a step
            # whose publication was reused without a fresh timing.
            last_assembly_ms = None
            # The previous step's camera.  The scene layer reuses a published
            # scene when a step re-issues it (``warm``, and ``cold`` at the
            # start of a later repeat), so that step publishes nothing new.
            previous_spec = None
            for entry in sequence:
                spec = entry["camera"]
                camera = replace(
                    base_camera,
                    center_lat=spec["lat"],
                    center_lon=spec["lon"],
                    zoom=spec["zoom"],
                )
                sample = {
                    "step": entry["step"],
                    "density": density,
                    "repeat": repeat,
                    "dpr": dpr,
                    "camera": dict(spec),
                    "anchor": anchor_name,
                }
                before = widget.counters["chart_publications"]
                # The scene layer is the authority whenever the raster chart
                # layer cannot be: counter-only never grabs, and a
                # CoreGraphics pixel run cannot wait on the chart layer's ENC
                # conjuncts because the scene layer supersedes them
                # (``resolve_enc_modes`` disables raster ENC in scene mode).
                scene_authority = uses_scene_readiness(
                    counters_only, enc_scene, capture_backend
                )
                # A step that re-issues the previous camera reuses its
                # published scene and publishes nothing new.
                reused = scene_authority and previous_spec == spec
                before_scene = (
                    _enc_scene_publications(widget) if scene_authority else None
                )
                # The frame on screen before this demand: it can never prove
                # that the demand was painted, however stable it looks.  A
                # counter-only run never grabs, so it has no such digest.
                pre_demand_frame = None
                if counters_only:
                    pre_demand_digest = None
                elif capture_backend == "cg":
                    # The OS settle rule compares pixels, so it needs the
                    # frame itself and not only its digest.
                    pre_demand_digest, pre_demand_frame = _pre_demand_capture(
                        check_enc, widget, last_settled_digest, grab=grab
                    )
                else:
                    pre_demand_digest = _pre_demand_digest(
                        check_enc, widget, last_settled_digest, grab=grab
                    )
                pre_demand_camera = _camera_token(getattr(widget, "camera", None))
                pre_demand_generation = getattr(
                    widget, "_chart_visible_generation", None
                )
                clock.mark()
                started = time.perf_counter()
                widget.set_camera(camera)
                layer.request(camera, replace=True)
                if scene_authority:
                    ready = check_enc._wait(
                        lambda: all(
                            counter_readiness(
                                check_enc,
                                layer,
                                widget,
                                before,
                                before_scene,
                                reused,
                            ).values()
                        ),
                        COUNTER_READY_TIMEOUT_S,
                    )
                else:
                    ready = check_enc._wait(
                        lambda: all(
                            check_enc._readiness_conjuncts(
                                layer, widget, before
                            ).values()
                        ),
                        READY_TIMEOUT_S,
                    )
                ready_ms = (time.perf_counter() - started) * 1000.0
                # Weather callbacks may restore the barbs while a chart loads;
                # the readback is chart-only, as in the acceptance gate.
                if widget._barbs is not None:
                    widget._barbs.setVisible(False)
                check_enc._pump(COUNTER_SETTLE_MS if counters_only else SETTLE_MS)
                previous_spec = dict(spec)

                sample["ready"] = bool(ready)
                if scene_authority:
                    # Why a step was (or was not) ready, beside the counters it
                    # gates, so an unready step is diagnosable from the report.
                    sample["readiness"] = counter_readiness(
                        check_enc, layer, widget, before, before_scene, reused
                    )
                    sample["scene_reused"] = bool(reused)
                sample["ready_ms"] = _number(ready_ms)
                sample["interaction_ms"] = _number(clock.since(started))
                metadata = layer.enc_metadata or {}
                sample["selected"] = sorted(
                    str(cell) for cell in (metadata.get("selected") or ())
                )
                # Gate 3: the scene layer's own ordered plan and per-cell
                # states, in plan order.  They are recorded even when empty so
                # the sample schema does not depend on the mode.
                sample["cells_order"] = scene_cell_records(widget) if enc_scene else []
                sample["cell_states"] = scene_cell_states(widget) if enc_scene else []
                coverage = (
                    ()
                    if experience_only
                    else _coverage(
                        check_enc, coverage_cache, descriptors, camera, dpr, palette
                    )
                )
                sample["coverage_selection"] = list(coverage)
                if enc_scene and sample["cells_order"]:
                    # The scene layer supersedes the raster ENC source, so its
                    # ordered plan names the cells that produced geometry.
                    sample["structure_matches"] = sorted(
                        ordered_cell_names(sample["cells_order"])
                    ) == sorted(reference_cell_names(coverage))
                else:
                    sample["structure_matches"] = tuple(sample["selected"]) == tuple(
                        sorted(coverage)
                    )
                sample["generation"] = widget._chart_visible_generation
                sample.update(_step_timings(layer, widget))
                metadata_timings = (layer.enc_metadata or {}).get("timings") or {}
                sample["assembly_ms"] = _number(metadata_timings.get("assembly_ms"))
                scene_assembly = (
                    _enc_scene_assembly_ms(widget)
                    if (counters_only or enc_scene)
                    else None
                )
                if sample["assembly_ms"] is None and scene_assembly is not None:
                    sample["assembly_ms"] = scene_assembly
                sample["assembly_reused"] = False
                if sample["assembly_ms"] is not None:
                    last_assembly_ms = sample["assembly_ms"]
                    sample["assembly_reused"] = bool(scene_authority and reused)
                elif reused and last_assembly_ms is not None:
                    # A reused publication reports the timing it was built
                    # with; carry it forward so a no-op step is not a hole in
                    # the flatness sample.
                    sample["assembly_ms"] = last_assembly_ms
                    sample["assembly_reused"] = True
                sample["enc_timings"] = (
                    _enc_scene_timings(widget) if enc_scene else None
                )
                sample["enc_scene_counters"] = (
                    _enc_scene_counters(widget) if enc_scene else None
                )

                if counters_only:
                    # No framebuffer is read in this mode: the ENC scene item's
                    # grab path is unusable, and Gate 2's residency question is
                    # answered by the counters above alone.
                    sample["capture_skipped"] = True
                    sample["stable_readback"] = None
                    sample["capture_settle"] = None
                    sample["frame_signature"] = None
                    sample["signature_drift"] = None
                    sample["nonblank"] = None
                    sample["coverage"] = None
                    sample["coverage_enc_extent"] = None
                    sample["enc_extent"] = None
                    sample["mae_255"] = None
                    sample["p99_255"] = None
                else:
                    try:
                        native, stable, settle = _capture(
                            check_enc,
                            widget,
                            pre_demand_digest=pre_demand_digest,
                            pre_demand_camera=pre_demand_camera,
                            pre_demand_generation=pre_demand_generation,
                            expect_generation=sample["generation"],
                            expect_camera=_camera_token(
                                getattr(widget, "camera", None)
                            ),
                            demand_camera=_camera_token(camera),
                            # ``warm`` and ``pan_back`` re-issue a camera this run
                            # already settled: their pixels cannot change, so the
                            # generation is the only available proof.
                            revisits_settled_camera=any(
                                spec == settled for settled in settled_specs
                            ),
                            pre_pump_ms=CAPTURE_PUMP_MS,
                            readback_misses=readback_misses,
                            grab=grab,
                            settle_tolerance=(
                                CG_SETTLE_TOLERANCE if capture_backend == "cg" else None
                            ),
                            pre_demand_frame=pre_demand_frame,
                            painted_probe=(
                                (
                                    lambda: _scene_demand_painted(
                                        widget, before_scene, reused
                                    )
                                )
                                if scene_authority
                                else None
                            ),
                        )
                        sample["stable_readback"] = bool(stable)
                        readback_misses = 0
                        sample["capture_settle"] = settle
                        if settle["digests"]:
                            last_settled_digest = settle["digests"][-1]
                        if stable:
                            settled_specs.append(dict(spec))
                    except Exception as exc:  # a failed grab is recorded, not fatal
                        native = None
                        readback_misses += 1
                        sample["stable_readback"] = False
                        sample["capture_settle"] = None
                        sample["capture_error"] = f"{type(exc).__name__}: {exc}"

                    sample["capture_backend"] = capture_backend
                    # A window capture is not byte-reproducible, so the digest
                    # alone cannot show that a revisit repainted the same
                    # composition.  The coarse block-mean signature does, and
                    # ``signature_drift`` states how far it moved from the
                    # first capture of this camera in this repeat.
                    sample["frame_signature"] = (
                        frame_signature(native) if native is not None else None
                    )
                    sample["signature_drift"] = None
                    signature_key = _camera_key(sample)
                    if sample["frame_signature"] and signature_key is not None:
                        previous_signature = settled_signatures.get(
                            (density, repeat, signature_key)
                        )
                        if previous_signature is None:
                            settled_signatures[(density, repeat, signature_key)] = (
                                sample["frame_signature"]
                            )
                        else:
                            sample["signature_drift"] = signature_drift(
                                sample["frame_signature"], previous_signature
                            )
                    if capture_backend == "cg":
                        # Geometry and ink of the last OS capture.  ``None``
                        # when no capture succeeded, so a failed step never
                        # reports a fraction borrowed from an earlier one.
                        sample["cg_capture"] = dict(cg_capture) or None
                        sample["nonblank_fraction"] = cg_capture.get(
                            "nonblank_fraction"
                        )
                    sample["nonblank"] = False
                    sample["coverage"] = None
                    sample["coverage_enc_extent"] = None
                    sample["enc_extent"] = None
                    sample["mae_255"] = None
                    sample["p99_255"] = None
                    if native is not None and experience_only:
                        sample["nonblank"] = bool(native[:, :, 3].any())
                    elif native is not None:
                        ref_on, ref_off = _reference(
                            check_enc,
                            references,
                            layer,
                            camera,
                            dpr,
                            palette,
                            data_dir,
                            descriptors,
                        )
                        if capture_backend == "cg":
                            # The OS capture is in the window server's pixels
                            # per point; the reference is on its own grid.  The
                            # crop is resampled onto the reference's shape
                            # before anything is compared.
                            native, geometry = _fit_capture_to_reference(native, ref_on)
                            sample["cg_geometry"] = geometry
                        else:
                            geometry = None
                        native, ref_on, ref_off, shape_note = _align_shapes(
                            native, ref_on, ref_off
                        )
                        if geometry is not None:
                            shape_note = dict(shape_note, cg=geometry)
                        sample["readback_shape"] = shape_note
                        sample["cropped_shape"] = list(native.shape[:2])
                        metrics = check_enc._fidelity(native, ref_on, ref_off)
                        sample["nonblank"] = bool(metrics["nonblank"])
                        sample["coverage"] = _number(metrics["coverage"], 4)
                        sample["alpha_coverage"] = _number(metrics["alpha_coverage"], 4)
                        sample["mae_255"] = _number(metrics["mae_255"], 4)
                        sample["p99_255"] = _number(metrics["p99_255"], 4)
                        sample["native_ink_pixels"] = int(metrics["native_ink_pixels"])
                        # The application fills the whole viewport; the reference
                        # leaves everything outside the cells it drew fully
                        # transparent.  The coverage that is like-for-like is the
                        # one taken over the reference's own ENC extent, so that is
                        # the value ``coverage_band`` judges.  The full-frame value
                        # stays beside it as a diagnostic.
                        extent_mask, extent = enc_extent_mask(
                            ref_on, ref_off, check_enc.INK_EPSILON
                        )
                        sample["enc_extent"] = extent
                        sample["coverage_enc_extent"] = sample["coverage"]
                        extent_metrics = _fidelity_on_mask(
                            check_enc, native, ref_on, ref_off, extent_mask
                        )
                        if extent_metrics is None:
                            # No reference ink at all: keep the full-frame value so
                            # an empty reference cannot pass the band by default.
                            extent["used_full_frame"] = True
                        else:
                            sample["coverage_enc_extent"] = _number(
                                extent_metrics["coverage"], 4
                            )
                            sample["mae_255_enc_extent"] = _number(
                                extent_metrics["mae_255"], 4
                            )
                            sample["recall_enc_extent"] = _number(
                                extent_metrics["recall"], 4
                            )
                        if dump_frames is not None:
                            # Temporary diagnostic: the exact arrays that were
                            # compared, so the excess ink can be located offline.
                            try:
                                directory = dump_directory(
                                    dump_frames, density, entry["step"], repeat
                                )
                                write_step_dump(
                                    directory,
                                    native,
                                    ref_on,
                                    ref_off,
                                    {
                                        "density": density,
                                        "step": entry["step"],
                                        "repeat": repeat,
                                        "dpr": dpr,
                                        "camera": dict(spec),
                                        "selected": sample["selected"],
                                        "coverage_selection": sample[
                                            "coverage_selection"
                                        ],
                                        "coverage": sample["coverage"],
                                        "coverage_enc_extent": sample[
                                            "coverage_enc_extent"
                                        ],
                                        "enc_extent": sample["enc_extent"],
                                        "alpha_coverage": sample.get("alpha_coverage"),
                                        "mae_255": sample["mae_255"],
                                        "p99_255": sample["p99_255"],
                                        "shapes": dict(
                                            sample["readback_shape"],
                                            saved=[
                                                list(native.shape),
                                                list(ref_on.shape),
                                                list(ref_off.shape),
                                            ],
                                        ),
                                    },
                                )
                            except Exception as exc:  # a dump never fails the run
                                sample["dump_error"] = f"{type(exc).__name__}: {exc}"

                try:
                    sample["worker_counters"] = _plain(
                        layer.observations().get("counters", {})
                    )
                except Exception as exc:
                    sample["worker_counters"] = {"error": type(exc).__name__}
                samples.append(sample)

    unreadable = [s for s in samples if s.get("capture_error")]
    if unreadable:
        first = unreadable[0]
        report["notes"].append(
            f"readback instrument: {len(unreadable)} of {len(samples)} steps had "
            f"no framebuffer, first {first['density']}/{first['step']} "
            f"r{first['repeat']} ({first['capture_error']}); those steps carry "
            "no capture and no coverage, so valid_capture is limited by the "
            "grab path rather than by a blank frame."
        )
    return samples


def _measure(
    report,
    snapshot,
    data_dir,
    scenarios,
    repeats,
    requested_dpr,
    experience_only,
    dump_frames=None,
    enc_scene=False,
    counters_only=False,
    capture_backend="qt",
):
    from PySide6.QtWidgets import QApplication

    from sailworks.qt.gribviewport import (
        GribViewportModel,
        ViewportCamera,
        _viewport_widget_class,
        saved_chart_palette,
        saved_selection,
    )
    from sailworks.rendering.windrenderer import WindLandMask

    check_enc = check_enc_module()
    QApplication.instance() or QApplication([])
    paths, settings = saved_selection(data_dir)
    palette = saved_chart_palette(data_dir, settings)
    report["identity"] = {
        "cells": check_enc._cell_provenance(snapshot, data_dir, settings),
        "platform": {},
    }
    descriptors = check_enc._reference_descriptors(data_dir, settings)
    report["identity"]["reference_cells"] = [
        {"cell": Path(path).stem.upper(), "native_scale": scale}
        for path, scale, _envelopes in descriptors
    ]

    manifest_time = _manifest_time(snapshot)
    anchor = check_enc.FIDELITY["puget"]
    base_camera = ViewportCamera(
        center_lat=anchor["lat"],
        center_lon=anchor["lon"],
        zoom=9.5,
        width=check_enc.WIDGET_WIDTH,
        height=check_enc.WIDGET_HEIGHT,
        time=manifest_time,
        density="very_dense",
        over_land=False,
    )
    model = GribViewportModel(
        paths,
        land_mask=WindLandMask(data_dir),
        resident_time_window=True,
        grid=True,
    )
    widget = _viewport_widget_class()(
        model,
        gpu_weather_fill=False,
        chart_layer=True,
        chart_data_dir=data_dir,
        enc_chart_layer=True,
        enc_scene_layer=bool(enc_scene),
        chart_palette=palette,
    )
    widget.resize(check_enc.WIDGET_WIDTH, check_enc.WIDGET_HEIGHT)
    widget.show()
    app = QApplication.instance()
    try:
        widget.set_camera(base_camera)
        if not check_enc._wait(
            lambda: widget._weather_ready_at is not None, WEATHER_TIMEOUT_S
        ):
            raise RuntimeError(
                "Weather did not become ready; chart readiness is not forced"
            )
        if widget._barbs is not None:
            widget._barbs.setVisible(False)
        if widget._status is not None:
            widget._status.setVisible(False)
        layer = widget._chart_model
        if layer is None:
            raise RuntimeError("chart layer unavailable")

        measured = int(round(widget.devicePixelRatio()))
        report["dpr_measured"] = measured
        report["identity"]["platform"] = check_enc._platform(widget)
        report["identity"]["weather_ready"] = True
        if measured != int(requested_dpr):
            report["error"] = (
                f"measured device pixel ratio {measured} does not match the "
                f"requested {requested_dpr}; set QT_SCALE_FACTOR={requested_dpr} "
                "and re-run.  No sample is reported for a ratio that was not "
                "measured."
            )
            return

        samples = _session(
            report,
            widget,
            layer,
            base_camera,
            descriptors,
            palette,
            data_dir,
            scenarios,
            repeats,
            measured,
            experience_only,
            dump_frames,
            enc_scene,
            counters_only,
            capture_backend,
        )
        report["sequence"] = samples
        report["repeats_summary"] = summarise_repeats(samples)
        report["timings"] = {
            "per_frame_interaction_ms": dict(
                summary(row.get("interaction_ms") for row in samples),
                label="per-frame interaction (input\u2192afterRendering)",
            ),
            "composition_ms": summary(row.get("composition_ms") for row in samples),
            "queue_ms": summary(row.get("queue_ms") for row in samples),
            "request_to_publish_ms": summary(
                row.get("request_to_publish_ms") for row in samples
            ),
            "publish_to_adopt_ms": summary(
                row.get("publish_to_adopt_ms") for row in samples
            ),
            "swap_ms": summary(row.get("swap_ms") for row in samples),
        }
        report["checks"]["structure_exact"] = bool(samples) and all(
            row.get("structure_matches") for row in samples
        )
        if counters_only:
            # Gate 2's residency question is answered by counters alone: no
            # framebuffer exists in this mode, so the pixel checks are not
            # evaluated rather than passed by an empty measurement.
            report["checks"].update(
                {"valid_capture": None, "coverage_band": None, "reproducible": None}
            )
            if enc_scene:
                # The scene layer supersedes the raster ENC source, so the chart
                # layer composes no ENC metadata: its cell selection does not
                # exist to compare.  The scene layer reports counts, not cell
                # names, so structure is not evaluated here either.
                report["checks"]["structure_exact"] = None
                report["notes"].append(
                    "structure_exact is not evaluated in --counters-only "
                    "--enc-scene mode: the scene layer supersedes the raster "
                    "ENC source, so the chart layer has no ENC metadata to "
                    "name the selected cells."
                )
            report["checks"].update(counter_only_checks(samples))
            if report["checks"].get("per_move_cost_flat") is None:
                report["notes"].append(
                    "per_move_cost_flat is not evaluated: no camera move "
                    "recorded a non-null assembly_ms, so flatness cannot be "
                    "asserted.  It is reported as null rather than false."
                )
            report["notes"].append(
                "--counters-only: no screenshot, grab or fidelity reference "
                "was attempted; valid_capture, coverage_band and reproducible "
                "are not applicable and are recorded as null.  Pixel fidelity "
                "is not evaluated in this mode."
            )
            report["notes"].append(
                "Gate 2's 'no stale content' is not judged here: it is covered "
                "by the static-matrix and retention unit tests, not by this "
                "counter-only run."
            )
        else:
            measured_coverage = [
                row["coverage_enc_extent"]
                for row in samples
                if row.get("coverage_enc_extent") is not None
            ]
            report["checks"]["valid_capture"] = bool(samples) and all(
                row.get("nonblank") for row in samples
            )
            # The band is judged on the shared ENC extent: the application
            # filling the rest of the viewport is correct, and the reference
            # leaves it blank.  ``coverage`` remains in the report as the
            # full-frame value.
            report["checks"]["coverage_band"] = all(
                COVERAGE_BAND[0] <= value <= COVERAGE_BAND[1]
                for value in measured_coverage
            )
            report["checks"]["reproducible"] = bool(report["repeats_summary"]) and all(
                row["reproducible"] for row in report["repeats_summary"].values()
            )
            if enc_scene:
                report["checks"].update(gate3_checks(samples))
                report["gate3"] = gate3_report(samples)
                report["notes"].append(
                    "Gate 3 ordered identity is judged on the ENC scene layer's "
                    "own ordered cells_order/cell_states: ordered_identity_exact "
                    "compares the plan's order with the reference "
                    "coverage_selection, and coverage_regions_match aggregates "
                    "the unchanged structure and coverage-band comparison."
                )
                report["notes"].append(
                    "Gate 3 residency: switch_cell and return are judged by Gate "
                    "2's revisit rule (a step re-issuing a camera the repeat "
                    "already painted must not move mesh_triangulations, "
                    "scene_misses or store_misses).  Cells genuinely new at a "
                    "switch are listed in report['gate3']['switch_return']; they "
                    "are allowed and reported, not failed."
                )
                memory = report["gate3"]["memory"]
                report["notes"].append(
                    "memory_cap_reported: cache byte caps are "
                    f"{CACHE_BYTE_CAPS['mesh_cap_bytes']} (mesh), "
                    f"{CACHE_BYTE_CAPS['scene_cap_bytes']} (scene) and "
                    f"{CACHE_BYTE_CAPS['store_cap_bytes']} (store) bytes; "
                    + (
                        "resident bytes were read from the layer's own caches."
                        if memory.get("bytes_measured")
                        else "the layer exposed no resident bytes, so the caps "
                        "are reported and the resident size is not measured."
                    )
                )
                report["notes"].append(
                    "Gate 3 missing_cell_explicit: an unavailable, oversized or "
                    "error cell is named with its state and reason, never "
                    "silently absent.  A per-cell Cairo raster fallback for a "
                    "skipped cell was deliberately NOT built, so such a cell is "
                    "reported explicitly but not covered by raster: that is a "
                    "scoped residual of this phase, not a Gate 3 pass."
                )
            if not measured_coverage:
                report["notes"].append(
                    "coverage_band was not evaluated: no fidelity reference was "
                    "composed (--experience-only)."
                )
    finally:
        if counters_only:
            # A counter-only run never grabs a frame, and closing the
            # QQuickView then blocks in Qt's render-loop teardown: the
            # synchronous close waits for a render thread that is already
            # gone.  The counters are recorded by now, so drop the window
            # without that handshake rather than lose the whole report.
            widget.hide()
            if app is not None:
                app.processEvents()
        else:
            widget.close()
        model.close()
        if app is not None:
            app.processEvents()


def _manifest_time(snapshot):
    from datetime import datetime

    manifest = json.loads((snapshot / "manifest.json").read_text())
    return datetime.fromisoformat(manifest["forecast_time"])


# --------------------------------------------------------------------------
# Entry point.
# --------------------------------------------------------------------------


def run(
    snapshot,
    scenarios=DEFAULT_SCENARIOS,
    repeats=5,
    requested_dpr=1,
    experience_only=False,
    dump_frames=None,
    enc_scene=False,
    counters_only=False,
    capture_backend="qt",
):
    started = time.perf_counter()
    report = empty_report(
        scenarios=scenarios,
        repeats=repeats,
        requested_dpr=requested_dpr,
        experience_only=experience_only,
        enc_scene=enc_scene,
        counters_only=counters_only,
        capture_backend=capture_backend,
    )
    if capture_backend == "cg" and not _cg_available():
        # No widget is built and no frame is read: a denied capture returns
        # transparent pixels that would otherwise look like a blank chart.
        report["error"] = CG_PERMISSION_ERROR
        report["notes"].append(
            "--capture-backend cg: CoreGraphics capture is not permitted, so "
            "the run stopped before building the viewport.  No sequence, "
            "timing or fidelity sample was taken."
        )
        return report
    fixture = _load_tool("enc_phase1_fixture", FIXTURE_PATH)
    check_enc = check_enc_module()
    try:
        manifest = fixture.verify_snapshot(snapshot)
    except (ValueError, OSError) as exc:
        report["error"] = f"fixture: {exc}"
        return report
    report["fixture_sha256"] = manifest["content_sha256"]
    report["snapshot_accepted"] = (
        manifest["content_sha256"] in check_enc.ACCEPTED_SNAPSHOT_SHA
    )
    if not report["snapshot_accepted"]:
        report["error"] = "content_sha256 is not an accepted snapshot"
        return report

    socket.socket.connect = _no_network
    sandbox_root = Path(tempfile.mkdtemp(prefix="enc-phase1-"))
    try:
        data_dir = fixture.prepare_sandbox(snapshot, sandbox_root)
        settings_path = data_dir / "settings.json"
        sandbox_settings = json.loads(settings_path.read_text())
        sandbox_settings["chartENCVisible"] = True
        sandbox_settings["gribBarbDensity"] = "very_dense"
        settings_path.write_text(json.dumps(sandbox_settings))
        report["notes"].append(
            "Sandbox copy: chartENCVisible and gribBarbDensity set for the "
            "viewport path; the user's own settings are untouched."
        )
        report["enc_scene_mode"] = bool(enc_scene)
        if capture_backend == "cg":
            report["notes"].append(
                "Capture backend: CoreGraphics (CGWindowListCreateImage with "
                "kCGWindowImageBoundsIgnoreFraming).  Qt's own grab paths are "
                "not used: grabToImage segfaults and grabWindow deadlocks "
                "while the ENC scene layer is live.  A capture whose ink "
                f"fraction is at or below {CG_NONBLANK_THRESHOLD} is rejected "
                "so a blank OS capture is never compared."
            )
        report["notes"].append(
            "ENC scene layer enabled (the application's only ENC presenter, "
            "enc_scene_layer=True): per-step enc_scene_counters record "
            "residency.  With it off this tool exercises the standalone "
            "raster reference path."
            if enc_scene
            else "ENC scene layer disabled: the standalone raster reference "
            "path is measured, not the application presentation."
        )
        _measure(
            report,
            snapshot,
            data_dir,
            scenarios,
            repeats,
            requested_dpr,
            experience_only,
            dump_frames,
            enc_scene,
            counters_only,
            capture_backend,
        )
    except Exception as exc:  # the report must still say what happened
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(sandbox_root, ignore_errors=True)
    report["timings_s"] = {"total": round(time.perf_counter() - started, 3)}
    report["complete"] = not report.get("error") and all(
        value for value in report["checks"].values() if value is not None
    )
    return report


def _no_network(*_args, **_kwargs):
    raise RuntimeError("Network is disabled in the ENC Phase 1 harness")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument(
        "--scenarios",
        default=",".join(DEFAULT_SCENARIOS),
        help="Cells the fixed sequence may switch between.  The cold/warm "
        "anchor per density is fixed by DENSITY_SCENARIO; the switch crosses "
        "to the multi-cell camera when it is listed, otherwise to the first "
        "listed scenario that is not the anchor.",
    )
    parser.add_argument(
        "--dpr",
        type=int,
        choices=(1, 2),
        default=1,
        help="Device pixel ratio this process must measure.  The ratio is set "
        "outside the process: QT_SCALE_FACTOR=2 ... --dpr 2.  Samples are "
        "refused when the measured ratio differs.",
    )
    parser.add_argument(
        "--experience-only",
        action="store_true",
        help="Capture frames and interaction timings without the Cairo fidelity "
        "reference.",
    )
    parser.add_argument(
        "--dump-frames",
        type=Path,
        default=None,
        metavar="DIR",
        help="Temporary diagnostic: save native/ref_on/ref_off PNGs and a small "
        "JSON per step under DIR.  Off by default; no effect on the report.",
    )
    parser.add_argument(
        "--enc-scene",
        action="store_true",
        help="Measure the GPU ENC scene layer (the application's only ENC "
        "presentation) instead of the standalone raster reference path: "
        "builds the viewport with enc_scene_layer=True and records residency "
        "counters per step.",
    )
    parser.add_argument(
        "--counters-only",
        action="store_true",
        help="With --enc-scene: skip every screenshot, grab and fidelity "
        "reference and judge Gate 2 from residency counters alone.  The ENC "
        "scene item's grab path is unusable (segfault/deadlock), but "
        "re-tessellation, re-upload and source re-query are still measurable "
        "without a framebuffer.",
    )
    parser.add_argument(
        "--capture-backend",
        choices=("qt", "cg"),
        default=None,
        help="Framebuffer capture backend.  Defaults to 'cg' in --enc-scene "
        "pixel mode, where Qt's own grab paths (grabToImage, grabWindow) "
        "segfault or deadlock with the ENC scene live, and to 'qt' elsewhere, "
        "which leaves the Phase 1 baseline unchanged.  'cg' captures through "
        "CGWindowListCreateImage and needs macOS Screen Recording permission; "
        "when it is missing the run stops with an explicit error instead of "
        "measuring a blank frame.",
    )
    args = parser.parse_args(argv)
    scenarios = tuple(name for name in args.scenarios.split(",") if name)
    unknown = [name for name in scenarios if name not in KNOWN_SCENARIOS]
    if unknown:
        parser.error(f"unknown scenario(s): {', '.join(unknown)}")
    if not scenarios:
        parser.error("--scenarios must name at least one scenario")
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    if args.counters_only and not args.enc_scene:
        parser.error("--counters-only requires --enc-scene")
    # ENC scene pixel mode cannot use Qt's grab (it crashes), so it selects the
    # OS capture by default.  An explicit value always wins; Gate 1 keeps 'qt'.
    capture_backend = args.capture_backend or (
        "cg" if args.enc_scene and not args.counters_only else "qt"
    )
    if args.counters_only and capture_backend != "qt":
        parser.error("--capture-backend does not apply to --counters-only")
    report = run(
        args.snapshot,
        scenarios,
        args.repeats,
        args.dpr,
        args.experience_only,
        args.dump_frames,
        args.enc_scene,
        counters_only=args.counters_only,
        capture_backend=capture_backend,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str))
    for key, row in report.get("repeats_summary", {}).items():
        p50 = row["interaction_ms"].get("p50")
        extent = row.get("coverage_enc_extent") or {}
        print(
            f"{key}: interaction p50 {p50} ms, "
            f"coverage {row['coverage']['min']}-{row['coverage']['max']} "
            f"(enc extent {extent.get('min')}-{extent.get('max')}), "
            f"reproducible={row['reproducible']}",
            file=sys.stderr,
        )
    if report.get("counters_only"):
        print(
            "counter checks: "
            + ", ".join(
                f"{name}={report['checks'].get(name)}" for name in COUNTER_CHECK_NAMES
            ),
            file=sys.stderr,
        )
    if report.get("gate3"):
        print(
            "gate3 checks: "
            + ", ".join(
                f"{name}={report['checks'].get(name)}" for name in GATE3_CHECK_NAMES
            ),
            file=sys.stderr,
        )
    if report.get("error"):
        print(f"error: {report['error']}", file=sys.stderr)
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
