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
    if args.temperature == "warm":
        # One unmeasured warmup so the caches are resident before repeat 0.
        for _label, spec, complete in gesture:
            demand_and_wait(spec, complete)

    measured = []
    for _ in range(args.repeats):
        start = len(_records(args.trace))
        for _label, spec, complete in gesture:
            demand_and_wait(spec, complete)
        measured.append(start)

    records = _records(args.trace)
    cycles = summarise_demands(records)
    labels = [entry[0] for entry in gesture]
    warmup = len(labels) if args.temperature == "warm" else 0
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
        "env": {
            key: os.environ.get(key)
            for key in (
                "SAILWORKS_ENC_HEAD_FIRST",
                "SAILWORKS_ENC_MESH_WORKERS",
                "SAILWORKS_ENC_COMPOSE_MARGIN",
                "SAILWORKS_ENC_BAKED_DIR",
                "SAILWORKS_ENC_BAKED_ZOOM",
                "SAILWORKS_ENC_BAKED_BANDS",
            )
        },
        "repeat_index": args.repeat_index,
        "warmup_demands": warmup,
        "cycles": cycles,
        "layer": layer_state,
        "measured_record_slice": measured,
        "records": len(records),
        "valid": enc_cells > 0 and len(cycles) >= len(labels),
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
    if len(cycles) < len(labels) * args.repeats:
        raise SystemExit(
            "only %d camera cycles for %d repeats of %d steps"
            % (len(cycles), args.repeats, len(labels))
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
