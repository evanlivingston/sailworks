# R2 slice: background Qt base-chart portrayal

> **Status: HISTORICAL.** The R2 base-chart-worker slice, written 2026-10-17; the
> behaviour it describes is still current, and this document is kept for its
> reasoning. The R0–R5 sequence in the implementation plan is the authority.
> Evidence, not a plan.

The Qt map now prepares an audited prefix of base chart layers on a background
worker. GSHHS and `SimpleChartDrawer` vectors are supported. OSM, raster, CM93,
custom subclasses and all layers above the first unsupported layer stay on the
existing synchronous path, preserving layer order. ENC and CHS retain their
existing workers. Both CPU and GPU Qt presentation use this adapter; GTK does not.

## Ownership, fidelity, and limits

The GUI snapshots paths, source-instance generations, palette, downscale state,
country labels and initial GSHHS quality timings. No GUI-owned GDAL handle, drawer,
settings object, symbol atlas or QWidget is used for worker portrayal. Private
handles/drawers are retained in an eight-source LRU. Original portrayal routines
and scale/quality rules are reused, including GSHHS's existing timing-dependent
full/high-quality choice (now timed on the worker). No weather sampling, source
priority, wind density, or routing behavior changes.

The worker uses the existing bounded `OverlayRenderer` queue: one active job and
one pending request per map, coalesced by content and coverage. Previous matching
content can be transformed while work completes; source/palette generations
cannot preview or publish under a new content key. Initial cold coverage is
progressive, not synchronous. Hide/close forget demand and reject late results.
The worker margin is 256 logical pixels versus the old base cache's 128; both
remain in addition to Qt's existing padded-image margin. This increases retained
coverage and storage, but is not predictive prefetch.

Surface allocation is rejected above 128 MiB per image and takes the old
synchronous path instead. Driver/portrayal failures also fall back for that
content key rather than endlessly showing missing coastlines. This is not a
global memory ledger: old/new surfaces, the main-thread composite, decoded driver
caches and both maps can coexist. Cancellation is checked between charts, not
inside every feature operation. General priority scheduling, split invalidation,
offscreen prediction and data-pipeline restructuring remain unimplemented.

## Matched measurement — 2026-10-16

Same private October 16 snapshot and GPU startup replay as
[embedded rendering](../gpu-embedded-rendering.md), native Metal/DPR 2. Three runs per
scenario, **48 frames** each. Baseline uses `--synchronous-base-charts`; candidate
uses the worker. No network or instrument access. These results must not be
compared numerically to the earlier 24-frame runs as though the protocol matched.

| Median per-run p99 grab/readback | Synchronous base | Worker prefix |
|---|---:|---:|
| Pan | 66.2 ms | 51.3 ms |
| Zoom out | 56.1 ms | 46.1 ms |

These are synchronous preparation/readback timings, not GPU execution, scanout,
or FPS. Maximum inclusive UI base-image bake during replay fell from 51.6 to
33.9 ms in pan, and 70.1 to 52.9 ms in zoom. UI chart portrayal/compositing still
reached 23.5/32.1 ms respectively: this is a partial R2 improvement, not its exit.
The candidate moved GSHHS to the worker and left one GDAL vector layer synchronous;
24 base jobs across all phases/runs published, none were cancelled. Post-gesture
worker idle remained about 3.2 s pan / 4.3 s zoom; weather settling is unchanged.

Peak process RSS increased: pan 2,005 → 2,101 MiB; zoom 2,012 → 2,135 MiB (maximum
of three runs). The explicit worker surface was about 40.2 MiB. These high-water
figures do not establish a memory plateau, nor measure VRAM. Follow-up retention
work must account for this cost rather than calling the worker a memory saving.

Private raw reports in the approved OpenCode temporary directory:

* `r2-base-worker-baseline.json`: synchronous control.
* `r2-base-worker-prefix-candidate.json`: measured worker prefix.
* `r2-base-worker-candidate.json`: preserved earlier all-or-nothing adapter trial;
  it fell back on the mixed workload, submitted zero base jobs and did not improve
  performance. It is not the accepted candidate.

The harness now records base worker jobs, inclusive chart/static-chart/wind/marine
composite spans, and audited/synchronous source counts. Reproduce using the same
snapshot with `--map-renderer gpu --scenarios pan,zoom_out --frames 48 --repeats 3`,
then add `--synchronous-base-charts` for the control. Do not use worker-idle as a
substitute for usable-coverage or visual assertions.

## Validation

`test/qt_basechart_checks.py` tests worker/private-handle portrayal against the
existing simple-vector and five-resolution GSHHS paths, both palettes, multiple
zooms; delayed workers leave Qt callbacks serviceable; stale palette/source-instance
results and forgotten demand cannot publish; mixed source ordering, unsupported
types, image limits and error fallback retain the old path. The existing polygon
hole check now waits explicitly for the asynchronous frame before checking both
sea and land pixels. Embedded GPU and the Qt regression checks remain required.

Physical interaction, long sessions/memory plateau, larger source mixes and
marine-heavy startup remain broader rollout gates. The experimental GPU toggle
remains default off; this change does not automatically enable GPU rendering.
