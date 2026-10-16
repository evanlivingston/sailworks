# GPU rendering feasibility experiment — 2026-10-15

## Outcome

**GPU-backed presentation is worth pursuing, but this is not a production-ready
replacement.** Native Metal rendering materially reduced CPU preparation and Qt
swap-callback latency for the saved startup scene. The CPU batching alternative
also helped, but less. A controlled chart-resampling test failed its declared
pixel tolerance in this original experiment.

Follow-up: [R0.1 texture parity](gpu-texture-parity-r0.1.md) fixes those reproduced
2x Metal failures. The original measurements and failed reports below are kept
unchanged as evidence; embedding, resource, and wider platform/DPR gates still
block a production migration.

No application renderer, data sampler, settings, or dependencies were changed.
All implementation is in standalone experiment tools.

## Experiment

The private [startup snapshot](rendering-startup-baseline-2026-10-15.md) was
loaded once in an isolated offline copy. We exported its padded base and
navigation images and completed wind field: 710 x 743 logical viewport, DPR 2,
2,126 retained native wind samples, dense barbs, original directions/speeds,
colors, and 0.85 opacity. Base imagery retains charts, weather fill/isobars, and
marine portrayal already rendered by the application. Private images/arrays are
outside the repository; reports contain checksums and diagnostics.

Three implementations present through the **same native Qt Quick/Metal window**:

* `cpu_current`: invokes the production `TrackMap._paint_wind_barbs()` method on
  a QImage, composes retained images, then supplies that raster to a texture.
* `cpu_batched`: substitutes batched `drawLines()` for the stroked path; retains
  the reference ellipse/filled-flag path behavior and all selected geometry.
* `gpu_mesh`: retains base/navigation textures and renders stroked barbs/flags
  as QSG triangle geometry. Per-color stroke/fill layers preserve
  union-before-opacity behavior. Geometry is recomputed only when the view
  changes, matching the reference's stationary geometry cache.

This is real scene-graph geometry, **not** QQuickPaintedItem CPU painting under a
GPU label. Qt diagnostics confirmed **Metal, Apple M1 Pro**, native `cocoa`, Qt
6.12.0, DPR 2, four-sample MSAA. The experiment uses the `basic` render loop for
predictable Python scene-graph callbacks; threaded rendering is not validated.

### Equal content and bounded motion

Every mode uses the same production wind selector, density, screen dimensions,
and camera sequence. Every measured frame records selected sample count,
primitive counts, and a hash of the complete selected geometry. These match
across all modes and repetitions. The initial view selects **741 barbs**; pan
selects 710–744, and zoom selects 568–753, identically in every implementation.

The experiment deliberately isolates **retained presentation**, not data loading:
stationary, a reversible +/-96 px horizontal / +/-24 px vertical pan, and a
+/-0.2 zoom around the center. Each is 180 steps. All image rectangles must cover
the viewport or the run fails. This avoids claiming speed improvements because
charts or weather have disappeared. These bounded trajectories are **not** the
earlier full-application long pan/one-level zoom baseline.

Dynamic route-plan/track markers, dashboard, coverage notices, and dock UI are
excluded. No forecast preparation, chart rebuild, prefetch, or new texture
production occurs during these retained-scene replays.

## Measurements

Three serial fresh-process repetitions per mode/scenario, rotating mode order:
**27 startup-scene replays / 4,860 measured steps**. Each worker warms five
frames before measurement. [Raw startup report](profiles/rendering/2026-10-15/gpu-startup.json).

| Motion | Current CPU preparation median | Batched CPU preparation median | GPU preparation median | Qt swap-interval p99: current / batched / GPU |
| --- | --- | --- | --- | --- |
| Stationary | 23.46 ms | 15.05 ms | 0.06 ms | 37.1 / 28.4 / 19.6 ms |
| Pan | 24.93 ms | 16.47 ms | 2.04 ms | 39.9 / 29.2 / 19.7 ms |
| Zoom | 27.27 ms | 19.23 ms | 2.01 ms | 41.4 / 33.4 / 20.6 ms |

Preparation includes selecting/projecting wind geometry plus preparing the CPU
raster or GPU mesh. It **excludes** subsequent Qt scene-graph work and GPU
execution. Observed scene-node callback CPU time added approximately 0.11 ms per
moving GPU frame. No hardware GPU execution timestamps were collected.

Swap intervals are native `frameSwapped` callback intervals, with the display's
scheduling, not a forced 60 Hz timer. They are not scanout timestamps or
input-to-photon measurements. Do not turn the preparation speedup into a claimed
12x application FPS improvement. The production QWidget and this CPU-in-Quick
control also have different presentation paths; compare the three prototype
modes, not their swap statistics directly against the earlier QWidget grabs.

### Upload and memory implications

The GPU prototype initially supplies **37.6 MiB** of base/navigation images once
and supplies **zero new QImages during timed replay**. Moving pan geometry
supplies approximately **0.31 MiB/frame** of vertex data. Stationary frames
resupply neither images nor geometry. Each CPU prototype supplies an approximately
8.05 MiB whole-frame QImage per step (4.24 GiB over 540 steps).

Those are requested image/vertex resource sizes, **not measured bus traffic**.
Qt's automatic per-bucket MSAA/color-layer targets, swapchain resources, and
driver allocation are not included in the retained-image figure. Total GPU/UMA
memory was **not measured**. Process peak RSS is recorded, but includes setup and
post-measurement readbacks and cannot stand in for VRAM or steady-state memory.

## Visual checks — including the failure

Image readbacks happen **after** timing and are reported separately. Four camera
positions per replay are compared against the current production painter's
glyphs and the same retained image composition. All checks use premultiplied RGBA.

Predeclared gates, unchanged after observing results:

* Identical selected geometry/sample/primitive counts across modes.
* Glyph silhouette precision and recall >=99% within **one physical pixel**.
* Total glyph alpha coverage within 95–105% of the reference.
* Foreground-only RGBA mean absolute error <=12/255, so empty background cannot
  dilute missing-symbol or color errors.
* Full-composite RGBA mean error <=1/255 and p99 channel error <=8/255.

All **108 startup-scene image comparisons** passed both glyph and composite
gates. For GPU images, maximum foreground error was 7.39/255; maximum full-scene
mean error was 0.214/255, and maximum composite p99 error was 7/255.

The separate controlled fixture exercises calm rings, cardinal/oblique directions,
50-knot flags, all speed colors, and a high-contrast background grid. It has nine
short validation replays (not pooled into the performance table):
[raw controlled report](profiles/rendering/2026-10-15/gpu-controlled-2x.json).
All 36 glyph checks passed. **Two GPU fractional-pan composite checks failed**:
p99 channel errors **12 and 19**, above the declared limit of 8, with mean errors
0.355 and 0.520. This exposes a texture-resampling parity issue that the smoother
real chart fixture did not trigger. It is not silently counted as a pass.

GPU calm rings approximate a radius-2, width-1 stroked circle using 64 segments;
the maximum chord error is **0.0031 logical pixels**. Lines retain one-logical-
pixel width and flat caps; flag triangle coordinates are unchanged apart from
conversion to float32 screen-space vertices. Four-sample
MSAA and raster QPainter antialiasing are not bit-identical. Directions, speeds,
density, and model/source selection are not approximated or reduced.

## Recommendation and next gates

The follow-on [implementation specification](rendering-implementation-plan.md)
turns these gates into staged production work, starting with texture parity.

Proceed to a **selective GPU presentation design**, not a wholesale application
rewrite. Keep chart interpretation and numerical/weather processing separate.

Before a production implementation:

1. Investigate transformed texture sampling/edge alignment, including controlled
   high-contrast images. Establish whether this is a coordinate defect or a
   different sampling policy; any intentional visual change needs explicit
   review and regression tests. Do not relax the gate to disguise the discrepancy.
2. Validate 1x/2x DPR, fractional zoom, overlap/opacity, glyph scale, masks,
   labels, attribution, and actual input/presentation behavior. This experiment
   validates native 2x Metal only, not other backends or a threaded scene graph.
3. Define byte budgets including intermediate MSAA targets and test extended
   travel/eviction. Capture GPU execution and memory with platform profiling.
4. Add progressive texture updates and bounded geographic chunks, then replay
   long pans beyond the retained region. Record upload stalls, usable coverage,
   and source/time correctness alongside frame pacing.
5. Address weather job cancellation and decode/sampling latency independently;
   GPU drawing does not make those disappear or bound selection over millions
   of retained samples.

The batched CPU alternative remains a potential smaller interim change, but also
needs production overlap/opacity validation. No migration or CPU optimization is
enabled by this experiment.

## Reproduce without touching the live app

Use the private snapshot created by the startup benchmark:

```sh
.venv/bin/python tools/experiment_gpu_rendering.py \
  --capture /private/path/to/startup-snapshot --fixture /private/path/to/new-gpu-fixture
.venv/bin/python tools/experiment_gpu_rendering.py \
  --fixture /private/path/to/new-gpu-fixture \
  --output /path/to/gpu-report.json --artifacts /private/path/to/images-and-logs
```

The export loads the real saved map once in isolated storage, disables network
and credentials, and refuses missing wind/data. Fixtures are checksummed; capture
destinations must be new. Run scripts from the repository with normal application
dependencies; Qt Quick is available in the measured environment. A hardware
scene graph is required; software/null backends fail rather than masquerade as GPU.
Native DPR must match the fixture. No new production dependency is added.

Controlled validation and tests:

```sh
.venv/bin/python tools/experiment_gpu_rendering.py \
  --synthetic --fixture /private/path/to/new-controlled-fixture
.venv/bin/python tools/experiment_gpu_rendering.py \
  --fixture /private/path/to/new-controlled-fixture --frames 8 --repeats 1 \
  --output /path/to/controlled-report.json --artifacts /private/path/to/controlled-images
GWR_RUN_GPU_TESTS=1 .venv/bin/python -m unittest test.test_gpu_rendering_experiment
```

Following R0.1, native tests require the complete glyph AND composite gates; the
historical composite-failure exemption has been removed. Ordinary runs execute
six mathematical/reference tests and skip the explicitly opted-in native Metal
test. Passing these tests is not a claim that production integration or the wider
platform matrix is complete. Timing thresholds are not CI tests.

Keep real scene images/arrays and logs private; do not commit the snapshot or
fixture automatically. Reports checkpoint to `.partial.json`; only complete
reports with matching fixture, code, environment, modes, trajectories, and repeat
counts are suitable for before/after analysis. This tool does not yet automate
cross-report comparison.
