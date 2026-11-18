#!/usr/bin/env python3
"""Live six-cell Strait baseline for issue #213 (state/trace only).

This drives the *real* viewport widget (the same class the opt-in demo runs)
through a fixed camera gesture and reads back the opt-in JSONL trace
(``SAILWORKS_ENC_TRACE``).  It records the viewport's own state probes -
``shown_vertex_count()`` and ``scene_covers_view()`` - and the demand-scoped
milliseconds the trace already carries.  It never reads a framebuffer, never
takes a screenshot, and never reports a grab rate as display FPS.

Gesture (one repeat)::

    zoom 13.0 at the Strait  ->  zoom 12.0 (six cells)  ->  hold
      ->  pan +PAN_DEG east (past the composed margin)  ->  back

Cold and warm are the caller's choice:

* cold: one measured repeat per **process**; the caller removes the persisted
  ``cache/enc-scenes`` store first, so every cache starts empty.
* warm: a warmup gesture then N measured repeats in one process, so the mesh,
  scene and store caches are resident.

Scheduling mode comes from the environment, like every other ENC switch:
``SAILWORKS_ENC_HEAD_FIRST`` is ``1`` (default, head-first) or ``0``
(whole-window submission, the previous scheduling).

Latency is measured on the trace's wall-clock ``time`` stamps from the first
demand of a camera change, because the widget can issue a second demand for the
same camera (a source refresh or a resize) and the trace's demand-scoped
``elapsed_ms`` restarts at that second demand.  The trace's own ``elapsed_ms``
is kept beside the wall-clock figure.  A run whose layer publishes no cells
fails loudly: a data failure must never be written down as a rendering
interval.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOOL = "tools/enc_strait_baseline.py"

#: The six-cell Strait view from the issue.
STRAIT_LAT = 48.00842
STRAIT_LON = -124.74315
START_ZOOM = 13.0
VIEW_ZOOM = 12.0
WIDTH = 960
HEIGHT = 693
#: A pan large enough that the stitched margin no longer covers the demand:
#: one z12 viewport is about 0.33 degrees wide, and the margin is a fraction
#: of that, so a fifth of a degree leaves it behind.
PAN_DEG = 0.20
HOLD_S = 0.6

#: The interactive gesture reproduced from the live report: a wheel zoom out
#: from 12.0 to ~11.4 while panning, applied through the widget's own
#: ``zoom_at``/``pan_pixels`` entry points rather than by jumping the camera.
OUT_ZOOM = 11.4
ZOOM_STEP = 0.1
#: Seconds of event pumping between two interaction steps. Long enough for the
#: frame to present, short enough that the next step lands mid-flight, which is
#: what the manual gesture did.
STEP_PUMP_S = 0.15

#: A mouse-like pan, in device pixels per move.  7.5 px is deliberately not a
#: whole device pixel: every step lands on a fractional offset, which is the
#: input that used to be rejected outright ("fractional_offset", regions 0,
#: reused pixels 0).  A reuse here can only come from the snapped blit.
PAN_STEP_PX = 7.5
#: Moves in one mouse drag.  Each move is followed by a pause long enough for
#: the chart's own idle settle, the way a hand moves and holds rather than
#: one camera jump.  Sixteen moves is 120 px, inside the composed margin at
#: z12, so most settled frames can be covered by the retained raster.
PAN_STEP_COUNT = 16
#: A hand's move-then-hold cadence.  Longer than the chart's compose time, so
#: each hold settles and publishes a frame; a 30-per-second stream never
#: settles inside the drag and hides the reuse behind one final composition.
PAN_STEP_PUMP_S = 0.5

#: The four gesture steps, in order, and the camera each one asks for.
GESTURE_LABELS = (
    "zoom_start",
    "zoom_six_cell",
    "pan_past_margin",
    "pan_back",
)


def _trace_lines(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return [line for line in handle if line.strip()]
    except OSError:
        return []


def _records(path):
    records = []
    for line in _trace_lines(path):
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def _parse_time(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _ms_between(start, end):
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() * 1000.0, 3)


def _camera_key(record):
    return tuple(record.get(key) for key in ("lat", "lon", "zoom", "width", "height"))


def _readable_manifest_cells(data_dir):
    """The enabled ENC entries that actually exist, by the viewport's rule."""
    from sailworks.qt.viewportenc import is_enc_path, read_manifest

    manifest = read_manifest(data_dir)
    entries = [
        path
        for path, enabled in manifest.entries
        if enabled and is_enc_path(path) and os.path.isfile(path)
    ]
    return manifest, entries


def _readable_catalog_cells(data_dir):
    """The ENC cells the application catalog resolves under ``<data>/enc``."""
    from sailworks.core.enccharts import ENCCatalog

    catalog_path = Path(data_dir) / "enc" / "catalog.xml"
    if not catalog_path.is_file():
        return None, []
    catalog = ENCCatalog(Path(data_dir) / "enc")
    catalog.load()
    paths = [catalog.downloaded_path(cell) for cell in catalog.cells]
    return catalog, [path for path in paths if path and os.path.isfile(path)]


def _hidden_without_coverage(record):
    """A raster-off sample where the ENC frame above it does not cover the view.

    ``enc_covers_view`` is ``None`` when the item cannot answer, which is not
    evidence of coverage, so only an explicit ``False`` or a zero vertex count
    counts as "not covered".  This mirrors the pre-fix baseline's definition.
    """
    if record.get("visible") is not False:
        return False
    covers = record.get("enc_covers_view")
    vertices = record.get("enc_vertices")
    return covers is False or vertices == 0


def summarise_raster(records):
    """Fold the ``raster_visibility`` transitions into outage intervals.

    An outage starts at the first hidden-without-coverage sample and ends at
    the next sample where the raster is visible again, so the interval is the
    time the user actually saw no chart, not the time between trace lines.
    Recovery latency is the same span measured from the ENC publish/present
    that lost coverage rather than from the raster sample that noticed it.
    """
    events = [
        record
        for record in records
        if record.get("event") == "raster_visibility"
        and _parse_time(record.get("time")) is not None
    ]
    outages = []
    open_at = None
    for record in events:
        stamp = _parse_time(record.get("time"))
        if _hidden_without_coverage(record):
            if open_at is None:
                open_at = stamp
            continue
        if record.get("visible") is True and open_at is not None:
            outages.append(
                {
                    "start": open_at.isoformat(),
                    "end": stamp.isoformat(),
                    "ms": _ms_between(open_at, stamp),
                }
            )
            open_at = None
    durations = [outage["ms"] for outage in outages if outage["ms"] is not None]
    recovery = []
    for record in records:
        if record.get("event") not in ("scene_publish", "scene_presented"):
            continue
        covers = record.get("covers_view")
        shown = record.get("shown_vertices")
        if covers is not False and shown != 0:
            continue
        lost_at = _parse_time(record.get("time"))
        if lost_at is None:
            continue
        for later in events:
            later_at = _parse_time(later.get("time"))
            if (
                later_at is not None
                and later_at > lost_at
                and later.get("visible") is True
            ):
                recovery.append(
                    {
                        "event": record.get("event"),
                        "state": record.get("state"),
                        "shown_vertices": shown,
                        "covers_view": covers,
                        "lost_at": lost_at.isoformat(),
                        "recovered_at": later_at.isoformat(),
                        "ms": _ms_between(lost_at, later_at),
                    }
                )
                break
    recovery_ms = [entry["ms"] for entry in recovery if entry["ms"] is not None]
    return {
        "events": len(events),
        "hidden_without_coverage": sum(
            1 for record in events if _hidden_without_coverage(record)
        ),
        "outage_count": len(outages),
        "outage_total_ms": round(sum(durations), 3) if durations else 0.0,
        "outage_longest_ms": max(durations) if durations else 0.0,
        "outages": outages,
        "recovery_count": len(recovery),
        "recovery_longest_ms": max(recovery_ms) if recovery_ms else None,
        "recoveries": recovery,
    }


def summarise_demands(records):
    """One row per camera change, with the trace's own generation-tagged chain.

    Two demands for the same camera are one cycle: the widget re-demands after a
    chart-source refresh or a resize, and the trace's ``elapsed_ms`` restarts
    there.  Wall-clock ``time`` stamps give the latency from the demand the
    gesture actually made.
    """
    cycles = []
    current = None
    for record in records:
        kind = record.get("event")
        if kind == "demand":
            key = _camera_key(record)
            if current is not None and key == current["camera_key"]:
                current["demands"].append(
                    {"time": record.get("time"), "t_demand": record.get("t_demand")}
                )
                continue
            current = {
                "camera_key": key,
                "camera": {
                    name: record.get(name)
                    for name in ("lat", "lon", "zoom", "width", "height")
                },
                "demands": [
                    {"time": record.get("time"), "t_demand": record.get("t_demand")}
                ],
                "demand_time": record.get("time"),
                "publishes": [],
                "adopts": [],
                "presents": [],
                "states": [],
                "slows": [],
            }
            cycles.append(current)
            continue
        if current is None:
            continue
        if kind == "scene_publish":
            current["publishes"].append(
                {
                    "generation": record.get("generation"),
                    "state": record.get("state"),
                    "cells": record.get("cells"),
                    "cells_order": record.get("cells_order"),
                    "assembly_ms": record.get("assembly_ms"),
                    "shown_vertices": record.get("shown_vertices"),
                    "covers_view": record.get("covers_view"),
                    "elapsed_ms": record.get("elapsed_ms"),
                    "time": record.get("time"),
                }
            )
        elif kind == "scene_adopt":
            current["adopts"].append(
                {
                    "generation": record.get("generation"),
                    "cells": record.get("cells"),
                    "shown_vertices": record.get("shown_vertices"),
                    "covers_view": record.get("covers_view"),
                    "elapsed_ms": record.get("elapsed_ms"),
                    "time": record.get("time"),
                }
            )
        elif kind == "scene_presented":
            current["presents"].append(
                {
                    "generation": record.get("generation"),
                    "elapsed_ms": record.get("elapsed_ms"),
                    "shown_vertices": record.get("shown_vertices"),
                    "covers_view": record.get("covers_view"),
                    "time": record.get("time"),
                }
            )
        elif kind == "state":
            current["states"].append(
                {
                    "state": record.get("state"),
                    "generation": record.get("generation"),
                    "elapsed_ms": record.get("elapsed_ms"),
                    "time": record.get("time"),
                }
            )
        elif kind == "slow":
            current["slows"].append(
                {
                    "reason": record.get("reason"),
                    "elapsed_ms": record.get("elapsed_ms"),
                    "dominant": record.get("dominant"),
                    "time": record.get("time"),
                }
            )

    for index, cycle in enumerate(cycles):
        cycle["index"] = index
        start = _parse_time(cycle["demand_time"])
        for key in ("publishes", "adopts", "presents", "states", "slows"):
            for row in cycle[key]:
                row["since_demand_ms"] = _ms_between(start, _parse_time(row["time"]))
        presents = cycle["presents"]
        # "Drawable" is the after-rendering geometry probe: the presented frame
        # drew at least one vertex.  Full coverage is a separate sample; a
        # demand can be drawable long before it is complete.
        draws = [
            present for present in presents if (present.get("shown_vertices") or 0) > 0
        ]
        full = [present for present in draws if present.get("covers_view") is True]
        first_draw = (
            min(draws, key=lambda row: row["since_demand_ms"]) if draws else None
        )
        first_full = min(full, key=lambda row: row["since_demand_ms"]) if full else None
        cycle["first_drawable_ms"] = (
            first_draw["since_demand_ms"] if first_draw else None
        )
        cycle["first_drawable_generation"] = (
            first_draw["generation"] if first_draw else None
        )
        cycle["first_full_ms"] = first_full["since_demand_ms"] if first_full else None
        cycle["first_full_generation"] = (
            first_full["generation"] if first_full else None
        )
        # The measured stretch with no drawable chart: from the first demand of
        # the camera change to the first presented frame that drew a vertex.
        cycle["no_drawable_ms"] = cycle["first_drawable_ms"]

        complete = [
            publish
            for publish in cycle["publishes"]
            if publish["state"] == "complete" and (publish.get("cells") or 0) > 0
        ]
        complete_publish = complete[-1] if complete else None
        complete_generation = (
            complete_publish["generation"] if complete_publish else None
        )
        cycle["complete_generation"] = complete_generation
        cycle["complete_cells"] = (
            complete_publish["cells"] if complete_publish else None
        )
        cycle["complete_cells_order"] = (
            list(complete_publish["cells_order"] or ()) if complete_publish else None
        )
        cycle["complete_published_ms"] = (
            complete_publish["since_demand_ms"] if complete_publish else None
        )
        cycle["complete_published_trace_ms"] = (
            complete_publish["elapsed_ms"] if complete_publish else None
        )
        cycle["assembly_ms"] = (
            complete_publish["assembly_ms"] if complete_publish else None
        )
        adoptions = [
            adopt
            for adopt in cycle["adopts"]
            if adopt["generation"] == complete_generation
        ]
        cycle["complete_adopt_ms"] = (
            adoptions[-1]["since_demand_ms"] if adoptions else None
        )
        complete_presents = [
            present
            for present in presents
            if present["generation"] == complete_generation
            and complete_publish is not None
            and present["since_demand_ms"] is not None
            # A present before the complete publication is the partial frame of
            # the same generation; the complete frame presents after it.
            and present["since_demand_ms"] >= complete_publish["since_demand_ms"] - 5
        ]
        cycle["complete_presented_ms"] = (
            min(present["since_demand_ms"] for present in complete_presents)
            if complete_presents
            else None
        )
        cycle["complete_presented_trace_ms"] = (
            min(present["elapsed_ms"] for present in complete_presents)
            if complete_presents
            else None
        )
        # Whether the frame already on screen kept a chart up while the new
        # scene composed: the trace samples the previous frame at the first
        # publication of the cycle.
        prefix = cycle["publishes"][0] if cycle["publishes"] else None
        cycle["retained_drawable"] = bool(
            prefix
            and (prefix.get("shown_vertices") or 0) > 0
            and prefix.get("covers_view") is True
        )
        # Strict: demand -> first presented frame that drew a vertex.  Aware:
        # zero when the retained frame was still drawable and reached the view
        # when the new composition landed.
        cycle["no_drawable_ms_strict"] = cycle["first_drawable_ms"]
        cycle["no_drawable_ms_retained_aware"] = (
            0.0 if cycle["retained_drawable"] else cycle["first_drawable_ms"]
        )
        cycle["no_drawable_ms"] = cycle["no_drawable_ms_strict"]
        cycle["present_chain"] = [
            {
                "generation": present["generation"],
                "since_demand_ms": present["since_demand_ms"],
                "trace_elapsed_ms": present["elapsed_ms"],
                "shown_vertices": present["shown_vertices"],
                "covers_view": present["covers_view"],
            }
            for present in presents
        ]
        # A partial presented as if it were the whole view: a generation was
        # presented with full viewport coverage yet only ever published partial
        # scenes.
        published = {}
        for publish in cycle["publishes"]:
            published.setdefault(publish["generation"], []).append(publish["state"])
        cycle["partial_presentations"] = [
            present["generation"]
            for present in presents
            if present["generation"] in published
            and "complete" not in published[present["generation"]]
        ]
        cycle["partial_presented_as_complete"] = any(
            present.get("covers_view") is True
            and present["generation"] in published
            and "complete" not in published[present["generation"]]
            for present in presents
        )
        cycle["complete_presented_with_partial_publish"] = any(
            publish["state"] == "partial"
            for publish in cycle["publishes"]
            if publish["generation"] == complete_generation
        )
        cycle["publish_chain"] = [
            {
                "generation": publish["generation"],
                "state": publish["state"],
                "cells": publish["cells"],
                "cells_order": list(publish["cells_order"] or ()),
                "assembly_ms": publish["assembly_ms"],
                "since_demand_ms": publish["since_demand_ms"],
                "trace_elapsed_ms": publish["elapsed_ms"],
            }
            for publish in cycle["publishes"]
        ]
        # What the previous frame drew while this one lands: the trace samples
        # it at each publish, before the new scene is adopted.
        cycle["publish_prefix_samples"] = [
            {
                "since_demand_ms": publish["since_demand_ms"],
                "state": publish["state"],
                "cells": publish["cells"],
                "cells_order": list(publish["cells_order"] or ()),
                "shown_vertices": publish["shown_vertices"],
                "covers_view": publish["covers_view"],
                "assembly_ms": publish["assembly_ms"],
            }
            for publish in cycle["publishes"]
        ]
    return cycles


def _percentile(values, fraction):
    """The linear-interpolated percentile of a small sample, or ``None``."""
    ordered = sorted(value for value in values if value is not None)
    if not ordered:
        return None
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * weight, 3)


def _compositions(records):
    """One mode per published generation, from the compose or publish line."""
    modes = {}
    for record in records:
        kind = record.get("event")
        if kind == "chart_compose":
            modes[record.get("generation")] = {
                "mode": record.get("static_mode"),
                "reason": record.get("static_reason"),
                "regions": record.get("regions"),
                "composed_pixels": record.get("composed_pixels"),
                "reused_pixels": record.get("reused_pixels"),
            }
        elif kind == "chart_publish" and record.get("generation") not in modes:
            modes[record.get("generation")] = {
                "mode": record.get("static_mode"),
                "reason": record.get("static_reason"),
                "regions": None,
                "composed_pixels": record.get("composed_pixels"),
                "reused_pixels": record.get("reused_pixels"),
            }
    return modes


def _visibility(records):
    return [
        record
        for record in records
        if record.get("event") == "raster_visibility"
        and _parse_time(record.get("time")) is not None
    ]


def summarise_deferral_loops(records):
    """Contiguous ``chart_deferred cause:scene_complete`` runs and their span."""
    loops = []
    run = None
    for index, record in enumerate(records):
        if record.get("event") != "chart_deferred":
            continue
        if record.get("cause") != "scene_complete":
            if run is not None:
                loops.append(run)
                run = None
            continue
        if run is None:
            run = {
                "started_at": record.get("time"),
                "deferrals": 0,
                "ended_at": None,
                "end_event": None,
            }
        run["deferrals"] += 1
        for later in records[index + 1 :]:
            if later.get("event") in (
                "chart_publish",
                "chart_compose",
                "scene_publish",
            ):
                run["ended_at"] = later.get("time")
                run["end_event"] = later.get("event")
                break
        if run["ended_at"] is None:
            run["ended_at"] = record.get("time")
            run["end_event"] = "still_deferred"
        run["ms"] = _ms_between(
            _parse_time(run["started_at"]), _parse_time(run["ended_at"])
        )
    if run is not None:
        loops.append(run)
    return loops


def summarise_startup(records):
    """Time from the first demand to the first painted raster, and its cost.

    The startup question is not when the widget existed but when the user
    first had chart pixels: the first demand of a cold session is the start,
    the first ``raster_visibility`` with ``visible`` true and a painted
    generation is the end.  ``hidden_total_ms`` is the layer's own accumulated
    hidden time at that instant, so it is not reconstructed here.
    """
    demands = [record for record in records if record.get("event") == "demand"]
    first_demand = demands[0] if demands else None
    first_painted = next(
        (
            record
            for record in _visibility(records)
            if record.get("visible") is True
            and record.get("painted_generation") is not None
        ),
        None,
    )
    startup = None
    if first_demand is not None and first_painted is not None:
        startup = _ms_between(
            _parse_time(first_demand.get("time")),
            _parse_time(first_painted.get("time")),
        )
    loops = summarise_deferral_loops(records)
    durations = [loop["ms"] for loop in loops if loop["ms"] is not None]
    return {
        "first_demand_at": first_demand.get("time") if first_demand else None,
        "first_raster_at": first_painted.get("time") if first_painted else None,
        "first_demand_to_first_raster_ms": startup,
        "first_raster_hidden_total_ms": (
            first_painted.get("hidden_total_ms") if first_painted else None
        ),
        "scene_complete_loop_count": len(loops),
        "scene_complete_loop_max_ms": max(durations) if durations else None,
        "scene_complete_loops_over_1s": [
            loop for loop in loops if (loop["ms"] or 0.0) > 1000.0
        ],
        "scene_complete_loops": loops,
    }


def summarise_pan(records, runs):
    """What the pure pan steps painted, and how long any swap stayed blank.

    Composition modes are taken per generation from the layer's own
    ``chart_compose`` line (``regions`` is only there), the layer counters are
    deltas measured around the drag, and the swap gaps are raster-absent spans
    between two visible samples, so a replacement that stayed visible is not
    counted as a blink.
    """
    windows = []
    for run in runs:
        started = _parse_time(run.get("started_at"))
        ended = _parse_time(run.get("ended_at"))
        if started is None or ended is None:
            continue
        windows.append((started, ended, run))

    def in_window(record, stamp_name="time"):
        stamp = _parse_time(record.get(stamp_name))
        if stamp is None:
            return None
        for started, ended, run in windows:
            if started <= stamp <= ended:
                return run
        return None

    per_run = []
    for run in runs:
        per_run.append(
            {
                "steps": run.get("steps"),
                "returned": run.get("returned"),
                "counter_delta": run.get("counter_delta") or {},
                "final_raster_visible": run.get("final_raster_visible"),
            }
        )

    composed = {}
    painted = {}
    for record in records:
        run = in_window(record)
        if run is None:
            continue
        if record.get("event") == "chart_compose":
            composed[record.get("generation")] = record.get("static_mode")
        elif record.get("event") == "raster_visibility":
            mode = record.get("painted_mode")
            if mode is not None:
                painted[record.get("painted_generation")] = mode

    delta_totals = {}
    for run in runs:
        for key, value in (run.get("counter_delta") or {}).items():
            if isinstance(value, int):
                delta_totals[key] = delta_totals.get(key, 0) + value

    visibility = _visibility(records)
    gaps = []
    absent_at = None
    seen_visible = False
    for record in visibility:
        stamp = _parse_time(record.get("time"))
        if record.get("visible") is True:
            if absent_at is not None:
                gaps.append(
                    {
                        "start": absent_at.isoformat(),
                        "end": stamp.isoformat(),
                        "ms": _ms_between(absent_at, stamp),
                        "in_pan": any(
                            started <= absent_at and stamp <= ended
                            for started, ended, _run in windows
                        ),
                    }
                )
                absent_at = None
            seen_visible = True
        elif seen_visible and absent_at is None:
            absent_at = stamp
    durations = [gap["ms"] for gap in gaps if gap["ms"] is not None]
    pan_durations = [
        gap["ms"] for gap in gaps if gap.get("in_pan") and gap["ms"] is not None
    ]

    def distribution(values):
        counts = {}
        for value in values:
            counts[str(value)] = counts.get(str(value), 0) + 1
        return counts

    return {
        "runs": per_run,
        "counter_totals": {
            key: delta_totals.get(key)
            for key in (
                "static_reuses",
                "static_snapped_reuses",
                "static_region_compositions",
                "static_full_compositions",
                "static_fractional_rejections",
                "static_regions",
            )
        },
        "composed_mode_counts": distribution(composed.values()),
        "painted_mode_counts": distribution(painted.values()),
        "swap_gaps": {
            "count": len(gaps),
            "median_ms": _percentile(durations, 0.5),
            "p95_ms": _percentile(durations, 0.95),
            "worst_ms": max(durations) if durations else None,
            "pan_count": len(pan_durations),
            "pan_median_ms": _percentile(pan_durations, 0.5),
            "pan_p95_ms": _percentile(pan_durations, 0.95),
            "pan_worst_ms": max(pan_durations) if pan_durations else None,
            "intervals": gaps,
        },
    }


def summarise_presence(records):
    """Intervals where neither the raster nor the ENC drew anything.

    Raster presence comes from every ``raster_visibility`` transition; ENC
    presence from the geometry the same line samples (``enc_vertices``), which
    is also carried by ``scene_publish``/``scene_presented``.  A vertex count
    of zero, or a missing one, is an ENC that draws nothing; a nonzero count
    that does not cover the view still draws part of it and is not counted as
    absent.
    """
    timeline = []
    raster = None
    enc = None
    for record in records:
        kind = record.get("event")
        stamp = _parse_time(record.get("time"))
        if stamp is None:
            continue
        if kind == "raster_visibility":
            raster = record.get("visible") is True
            enc = record.get("enc_vertices")
        elif kind in ("scene_publish", "scene_presented", "scene_adopt"):
            enc = record.get("shown_vertices")
        else:
            continue
        timeline.append(
            {
                "at": stamp,
                "raster": raster,
                "enc_vertices": enc,
                "event": kind,
            }
        )
    intervals = []
    first_visible_at = next(
        (entry["at"] for entry in timeline if entry["raster"]), None
    )
    for previous, current in zip(timeline, timeline[1:]):
        both_absent = previous["raster"] is False and not previous["enc_vertices"]
        if not both_absent:
            continue
        intervals.append(
            {
                "start": previous["at"].isoformat(),
                "end": current["at"].isoformat(),
                "ms": _ms_between(previous["at"], current["at"]),
                "from": previous["event"],
                "to": current["event"],
                # The cold start has nothing painted by definition; an
                # interval that ends before the first visible raster is the
                # session beginning, not a gap between two presentations.
                "during_startup": first_visible_at is None
                or current["at"] <= first_visible_at,
            }
        )
    total = sum(interval["ms"] or 0.0 for interval in intervals)
    steady = [interval for interval in intervals if not interval["during_startup"]]
    return {
        "both_absent_count": len(intervals),
        "both_absent_total_ms": round(total, 3),
        "both_absent_worst_ms": max(
            (interval["ms"] or 0.0 for interval in intervals), default=0.0
        ),
        "both_absent_after_startup_count": len(steady),
        "both_absent_after_startup_total_ms": round(
            sum(interval["ms"] or 0.0 for interval in steady), 3
        ),
        "first_visible_raster_at": (
            first_visible_at.isoformat() if first_visible_at else None
        ),
        "intervals": intervals,
    }


def _pump(app, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


def _wait(app, predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return predicate()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mode", choices=("headfirst", "windowed"), required=True)
    parser.add_argument(
        "--gesture",
        choices=("six_cell", "zoom_out_pan", "mouse_pan"),
        default="six_cell",
        help=(
            "six_cell: the fixed camera jumps this tool has always used. "
            "zoom_out_pan: the reported interactive gesture, driven through "
            "the widget's zoom_at/pan_pixels as small steps. "
            "mouse_pan: a mouse-like drag of fractional-pixel pan_pixels "
            "moves at the recorded 30 per second."
        ),
    )
    parser.add_argument(
        "--return-back",
        action="store_true",
        help="zoom_out_pan only: after settling, pan back west and zoom in again.",
    )
    parser.add_argument("--temperature", choices=("cold", "warm"), required=True)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--repeat-index", type=int, default=0)
    args = parser.parse_args(argv)

    os.environ["SAILWORKS_ENC_HEAD_FIRST"] = "1" if args.mode == "headfirst" else "0"
    os.environ["SAILWORKS_ENC_TRACE"] = str(args.trace)
    args.trace.parent.mkdir(parents=True, exist_ok=True)
    args.trace.write_text("", encoding="utf-8")

    # Both ENC discovery paths must resolve before a single frame is measured.
    manifest, readable = _readable_manifest_cells(args.data_dir)
    if not readable:
        raise SystemExit(
            "no readable ENC cell is enabled in {}{}: refusing to measure".format(
                args.data_dir,
                "" if not manifest.error else " (%s)" % manifest.error,
            )
        )
    catalog, catalog_readable = _readable_catalog_cells(args.data_dir)
    if catalog is not None and not catalog_readable:
        raise SystemExit(
            "EncCatalog resolved 0 readable cells under %s/enc: refusing to measure"
            % args.data_dir
        )
    print(
        json.dumps(
            {
                "readable_manifest_cells": len(readable),
                "readable_catalog_cells": len(catalog_readable),
                "cells": sorted(Path(path).stem for path in readable),
            }
        )
    )

    from PySide6.QtWidgets import QApplication

    from sailworks.qt.gribviewport import (
        GribViewportModel,
        ViewportCamera,
        _viewport_widget_class,
        saved_chart_palette,
        saved_selection,
    )
    from sailworks.rendering.windrenderer import WindLandMask

    app = QApplication.instance() or QApplication([])
    paths, settings = saved_selection(args.data_dir)
    palette = saved_chart_palette(args.data_dir, settings)
    model = GribViewportModel(
        paths,
        land_mask=WindLandMask(args.data_dir),
        resident_time_window=True,
        grid=True,
    )
    widget = _viewport_widget_class()(
        model,
        gpu_weather_fill=False,
        chart_layer=True,
        chart_data_dir=args.data_dir,
        enc_chart_layer=True,
        enc_scene_layer=True,
        chart_palette=palette,
    )
    widget.resize(WIDTH, HEIGHT)
    widget.show()
    _pump(app, 0.6)
    if (widget.width(), widget.height()) != (WIDTH, HEIGHT):
        widget.resize(WIDTH, HEIGHT)
        _pump(app, 0.4)

    def camera(lon, zoom):
        return ViewportCamera(
            center_lat=STRAIT_LAT,
            center_lon=lon,
            zoom=zoom,
            width=WIDTH,
            height=HEIGHT,
            density="very_dense",
            over_land=False,
        )

    def demand_and_wait(camera_spec, wait_complete=False):
        before = len(_records(args.trace))
        widget.set_camera(camera_spec)

        def new_rows():
            return _records(args.trace)[before:]

        def presented():
            return any(row.get("event") == "scene_presented" for row in new_rows())

        def complete_generation():
            generations = [
                row.get("generation")
                for row in new_rows()
                if row.get("event") == "scene_publish"
                and row.get("state") == "complete"
                and row.get("cells")
            ]
            return generations[-1] if generations else None

        _wait(app, presented, args.timeout)
        if wait_complete:
            _wait(app, lambda: complete_generation() is not None, args.timeout)
            generation = complete_generation()
            if generation is not None:
                # The complete frame must actually present: wait for the
                # after-rendering sample of that same generation.
                _wait(
                    app,
                    lambda: any(
                        row.get("event") == "scene_presented"
                        and row.get("generation") == generation
                        for row in new_rows()
                    ),
                    args.timeout,
                )
        _pump(app, HOLD_S)

    gesture = [
        ("zoom_start", camera(STRAIT_LON, START_ZOOM), True),
        ("zoom_six_cell", camera(STRAIT_LON, VIEW_ZOOM), True),
        ("pan_past_margin", camera(STRAIT_LON + PAN_DEG, VIEW_ZOOM), True),
        ("pan_back", camera(STRAIT_LON, VIEW_ZOOM), False),
    ]

    def pan_degrees(dlon):
        """Pan by a longitude delta through the widget's own pan path."""
        view = widget._camera.view()
        dx = view._lon_to_x(widget._camera.center_lon) - view._lon_to_x(
            widget._camera.center_lon + dlon
        )
        widget.pan_pixels(dx, 0.0)

    def scripted_zoom_out_pan(return_back=False):
        """Wheel out from VIEW_ZOOM to OUT_ZOOM while panning, as steps.

        The live report was a continuous wheel-zoom-and-drag; the widget cannot
        take a single combined camera animation, so this issues the same
        ``zoom_at``/``pan_pixels`` calls the mouse handlers make, one small
        step at a time, and pumps events in between.  The camera therefore
        moves continuously through the same path the user dragged, at whatever
        rate the event loop keeps up with.
        """
        before = len(_records(args.trace))
        started = time.perf_counter()
        started_at = datetime.now().isoformat(timespec="milliseconds")
        steps = max(1, int(round(abs(VIEW_ZOOM - OUT_ZOOM) / ZOOM_STEP)))
        per_step_zoom = (OUT_ZOOM - VIEW_ZOOM) / steps
        per_step_dlon = PAN_DEG / steps
        marks = []

        def step(dzoom, dlon, label):
            widget.zoom_at(dzoom)
            _pump(app, STEP_PUMP_S)
            pan_degrees(dlon)
            _pump(app, STEP_PUMP_S)
            current = widget._camera
            marks.append(
                {
                    "label": label,
                    "at_ms": round((time.perf_counter() - started) * 1000.0, 3),
                    "zoom": round(current.zoom, 4),
                    "center_lon": round(current.center_lon, 6),
                }
            )

        for index in range(steps):
            step(per_step_zoom, per_step_dlon, "zoom_out_pan_%d" % (index + 1))
        if return_back:
            for index in range(steps):
                step(
                    -per_step_zoom, -per_step_dlon, "zoom_in_pan_back_%d" % (index + 1)
                )
        presented = _wait(
            app,
            lambda: any(
                row.get("event") == "scene_presented"
                for row in _records(args.trace)[before:]
            ),
            args.timeout,
        )
        _pump(app, HOLD_S)
        item = getattr(widget, "_enc_scene_item", None)
        shown = getattr(item, "shown_vertex_count", None)
        covers = getattr(item, "scene_covers_view", None)
        return {
            "steps": len(marks),
            "returned": bool(return_back),
            "started_at": started_at,
            "ended_at": datetime.now().isoformat(timespec="milliseconds"),
            "requests": marks,
            "presented": presented,
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "final_zoom": round(widget._camera.zoom, 4),
            "final_center_lon": round(widget._camera.center_lon, 6),
            "final_shown_vertices": int(shown()) if callable(shown) else None,
            "final_covers_view": bool(covers()) if callable(covers) else None,
            "final_raster_visible": getattr(widget, "_chart_visible_generation", None)
            is not None,
        }

    def chart_counters():
        chart_layer_model = getattr(widget, "_chart_model", None)
        counters = getattr(chart_layer_model, "counters", None)
        return dict(counters or {}) if isinstance(counters, dict) else {}

    def scripted_mouse_pan(return_back=False):
        """A mouse drag in fractional-pixel moves at the sweep's own rate.

        The live input is a drag of many small moves, not one camera jump.
        Each ``pan_pixels`` call is exactly what the mouse handler makes, and
        7.5 px lands on a fractional device offset every time, so the retained
        raster can only be reused by the snapped blit.
        """
        before = len(_records(args.trace))
        counters_before = chart_counters()
        started = time.perf_counter()
        started_at = datetime.now().isoformat(timespec="milliseconds")
        marks = []
        moves = PAN_STEP_COUNT * (2 if return_back else 1)
        for index in range(moves):
            direction = 1.0 if index < PAN_STEP_COUNT else -1.0
            widget.pan_pixels(PAN_STEP_PX * direction, 0.0)
            _pump(app, PAN_STEP_PUMP_S)
            current = widget._camera
            marks.append(
                {
                    "label": "mouse_pan_%d" % (index + 1),
                    "at_ms": round((time.perf_counter() - started) * 1000.0, 3),
                    "direction": direction,
                    "zoom": round(current.zoom, 4),
                    "center_lon": round(current.center_lon, 8),
                }
            )
        presented = _wait(
            app,
            lambda: any(
                row.get("event") == "scene_presented"
                for row in _records(args.trace)[before:]
            ),
            args.timeout,
        )
        _pump(app, HOLD_S)
        counters_after = chart_counters()
        delta = {
            key: value - counters_before[key]
            for key, value in counters_after.items()
            if isinstance(value, int) and isinstance(counters_before.get(key), int)
        }
        item = getattr(widget, "_enc_scene_item", None)
        shown = getattr(item, "shown_vertex_count", None)
        covers = getattr(item, "scene_covers_view", None)
        return {
            "steps": len(marks),
            "returned": bool(return_back),
            "step_px": PAN_STEP_PX,
            "step_pump_s": PAN_STEP_PUMP_S,
            "started_at": started_at,
            "ended_at": datetime.now().isoformat(timespec="milliseconds"),
            "requests": marks,
            "presented": presented,
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "final_zoom": round(widget._camera.zoom, 4),
            "final_center_lon": round(widget._camera.center_lon, 6),
            "final_shown_vertices": int(shown()) if callable(shown) else None,
            "final_covers_view": bool(covers()) if callable(covers) else None,
            "final_raster_visible": getattr(widget, "_chart_visible_generation", None)
            is not None,
            "counter_delta": delta,
        }

    interactive_runs = []
    if args.gesture in ("zoom_out_pan", "mouse_pan"):

        def interactive_run():
            demand_and_wait(camera(STRAIT_LON, VIEW_ZOOM), True)
            if args.gesture == "mouse_pan":
                return scripted_mouse_pan(args.return_back)
            return scripted_zoom_out_pan(args.return_back)

        if args.temperature == "warm":
            # One unmeasured warmup so the caches are resident before repeat 0.
            interactive_run()
        measured = []
        for _ in range(args.repeats):
            start = len(_records(args.trace))
            interactive_runs.append(interactive_run())
            measured.append(start)
    else:
        if args.temperature == "warm":
            # One unmeasured warmup so the caches are resident before repeat 0.
            for _label, spec, complete in gesture:
                demand_and_wait(spec, complete)

        measured = []
        for _ in range(args.repeats):
            start = len(_records(args.trace))
            for _label, spec, complete in gesture:
                # Warm repeats reuse the same widget: every step must settle, or
                # the gesture loop runs ahead of the layer and manufactures
                # drifting intermediate cameras instead of measured repeats.
                demand_and_wait(spec, complete or args.temperature == "warm")
            measured.append(start)

    records = _records(args.trace)
    cycles = summarise_demands(records)
    labels = [entry[0] for entry in gesture]
    warmup = len(labels) if args.temperature == "warm" else 0
    if args.gesture == "six_cell":
        for cycle in cycles:
            cycle["label"] = labels[cycle["index"] % len(labels)]
            cycle["repeat"] = cycle["index"] // len(labels)
            cycle["measured"] = cycle["index"] >= warmup
            previous = cycles[cycle["index"] - 1] if cycle["index"] > 0 else None
            cycle["previous_frame"] = (
                previous["present_chain"][-1]
                if previous is not None and previous["present_chain"]
                else None
            )
    enc_cells = max(
        (
            publish.get("cells") or 0
            for cycle in cycles
            for publish in cycle["publishes"]
        ),
        default=0,
    )
    layer = getattr(widget, "_enc_scene_model", None)
    layer_state = {
        "error": getattr(layer, "_error", None),
        "state": getattr(layer, "_state", None),
        "counters": dict(getattr(layer, "counters", {}) or {}),
        "readable_manifest_cells": len(readable),
        "readable_catalog_cells": len(catalog_readable),
        "published_cells_peak": enc_cells,
    }
    chart_layer = getattr(widget, "_chart_model", None)
    chart_state = {
        "counters": dict(getattr(chart_layer, "counters", {}) or {}),
        "published_generations": sorted(
            getattr(chart_layer, "_published_generations", ())
        ),
        "rejected_generations": sorted(
            getattr(chart_layer, "_rejected_generations", ())
        ),
        "static_frames": getattr(chart_layer, "_static_count", None),
    }
    result = {
        "tool": TOOL,
        "mode": args.mode,
        "head_first": args.mode == "headfirst",
        "temperature": args.temperature,
        "pid": os.getpid(),
        "data_dir": str(args.data_dir),
        "cell_ids": sorted(Path(path).stem for path in readable),
        "viewport": {
            "requested": {"width": WIDTH, "height": HEIGHT},
            "actual": {"width": widget.width(), "height": widget.height()},
        },
        "camera": {
            "lat": STRAIT_LAT,
            "lon": STRAIT_LON,
            "start_zoom": START_ZOOM,
            "view_zoom": VIEW_ZOOM,
            "pan_deg": PAN_DEG,
        },
        "gesture": args.gesture,
        "gesture_spec": (
            {
                "out_zoom": OUT_ZOOM,
                "zoom_step": ZOOM_STEP,
                "pan_deg": PAN_DEG,
                "step_pump_s": STEP_PUMP_S,
                "return_back": bool(args.return_back),
                "driver": "zoom_at + pan_pixels",
            }
            if args.gesture == "zoom_out_pan"
            else (
                {
                    "step_px": PAN_STEP_PX,
                    "step_count": PAN_STEP_COUNT,
                    "step_pump_s": PAN_STEP_PUMP_S,
                    "return_back": bool(args.return_back),
                    "driver": "pan_pixels",
                }
                if args.gesture == "mouse_pan"
                else None
            )
        ),
        "interactive_runs": interactive_runs,
        "startup": summarise_startup(records[: measured[0]] if measured else records),
        "deferral_loops": summarise_deferral_loops(records),
        "pan": summarise_pan(records, interactive_runs),
        "presence": summarise_presence(records),
        "raster": summarise_raster(records),
        "env": {
            key: os.environ.get(key)
            for key in (
                "SAILWORKS_ENC_HEAD_FIRST",
                "SAILWORKS_ENC_MESH_WORKERS",
                "SAILWORKS_ENC_COMPOSE_MARGIN",
            )
        },
        "repeat_index": args.repeat_index,
        "warmup_demands": warmup,
        "cycles": cycles,
        "layer": layer_state,
        "chart": chart_state,
        "measured_record_slice": measured,
        "records": len(records),
        "valid": enc_cells > 0
        and (
            len(cycles) >= len(labels) or args.gesture in ("zoom_out_pan", "mouse_pan")
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "out": str(args.out),
                "cycles": len(cycles),
                "enc_cells_peak": enc_cells,
            }
        )
    )
    widget.close()
    if enc_cells == 0:
        raise SystemExit(
            "enc_cells == 0: the scene layer published no cells from %s; "
            "refusing to record this as a rendering interval" % args.data_dir
        )
    if args.gesture == "six_cell" and len(cycles) < len(labels) * args.repeats:
        raise SystemExit(
            "only %d camera cycles for %d repeats of %d steps"
            % (len(cycles), args.repeats, len(labels))
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
