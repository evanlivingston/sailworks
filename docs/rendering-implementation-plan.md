# Rendering implementation specification

Status: staged implementation authorized; the production default remains the CPU
renderer until the rollout gates below pass. This document is the consolidated
plan; measurements and historical experiment reports remain separate evidence.

Implementation started: [R0.1 texture parity](gpu-texture-parity-r0.1.md) fixes the
reproduced 2x Metal regression with unchanged quality gates. The opt-in
[embedded presenter](gpu-embedded-rendering.md) now works in the application;
wider backend/resource gates remain open. A complete R0 exit is not claimed yet.

R2 has a measured [base-chart worker slice](rendering-base-chart-worker.md):
audited Qt coastlines/simple vectors prepare off-thread with private handles.
Other base types and main-thread composition remain synchronous. Split invalidation,
predictive prefetch and cross-view data retention are still pending. R3 now has a
[weather job-resource reuse slice](rendering-weather-job-reuse.md), cutting repeated
source opens/indexing within a frame without changing sampling or forecast selection.
A [bounded cross-view retention slice](rendering-weather-crossview.md) keeps up to
64 selected handles/bands across jobs; deselected/changed sources close explicitly.
A [field working-set slice](rendering-weather-working-set.md) keys sampled fields by
source-set/time/field/extent with containment reuse (zero overfetch); a quantized
superset trial was measured and rejected. R4 begins with a [bounded directional
prefetch slice](rendering-weather-prefetch.md): one speculative frame ahead of
release motion, idle-only scheduling, promotion on arrival with zero waste.

[Scheduling follow-up](rendering-weather-scheduling.md) adds active-request
coalescing and lifecycle/coverage telemetry. The measured wider-margin survival
trial was rejected; conservative pan thresholds remain the default. Cross-view
working sets and predictive prefetch are still pending.

## Reference and invariants

Use the [saved startup snapshot](rendering-startup-baseline-2026-10-15.md), without
asking the user to assemble a stress scene. Preserve its enabled sources, dense
barbs, 1 hPa isobars, saved view/layout, and marine/navigation settings. Reuse its
checksums and frozen forecast clock for comparisons; do not recapture different
data to make a candidate look faster. Synthetic scenes supplement this workload.

The [GPU experiment](gpu-rendering-experiment.md) demonstrated a retained-scene
benefit, not a complete data-pipeline solution. It also found two controlled
fractional-pan composite failures. Historical reports must not be overwritten
with candidate results or have their failed quality flags reinterpreted.

Non-negotiable invariants:

* Preserve numerical sampling, vector reference, native masks, interpolation,
  newest-run priorities, selected-model ordering, and fallback behavior.
* Preserve displayed density, sample selection, glyph size/width, directions,
  speeds, opacity/order, labels, chart scale selection, masks, and attribution.
  Any intentional display approximation requires a documented error bound and
  explicit review; a faster blank or lower-detail frame is not an improvement.
* No credential/instrument access or automatic network acquisition in benchmark
  replays. Local prefetch and remote download policies remain separate.
* Do not mutate user data or rewrite persisted navigation/routing state as part
  of this work. Startup persistence is an independent follow-up.
* Keep the existing CPU path available. Do not migrate the legacy GTK frontend
  or rewrite routing as a side effect of Qt presentation work.

## Architecture and ownership

The architecture below remains the target. The embedded presenter implements
only the presentation boundary; view/content generations, worker restructuring,
and coverage scheduling are not implemented yet.

```text
input / settings / clock / selected sources
              |
              v
Map scene controller -- immutable ViewState + content generations
       |                         |
       v                         v
coverage planner           interaction overlays / picking
       |
visible work > predicted work > nearby work > adjacent zoom
       |
bounded worker-owned data preparation and chart portrayal
       |
immutable LayerFrame / geometry / coverage metadata
       |
CPU presenter OR feature-flagged Qt Quick presenter
```

* **Map scene controller:** owns camera, gestures, public map signals, lifecycle,
  selection/picking, and layer invalidation. Start with the existing contracts in
  `gweatherrouting/qt/mapcanvas.py`; do not replace controller/navigation APIs at
  the same time as introducing presentation.
* **ViewState:** center/world copy, fractional zoom, logical viewport, actual DPR,
  and view revision. Camera motion changes transforms immediately; it must not
  synchronously open files, sample weather, or rebuild static chart imagery.
* **ContentKey:** layer identity, source-set revision, forecast instant/bracket,
  palette/style/density revision, and geographic/scale coverage. View revision is
  separate: a useful completed chunk must not become disposable merely because
  the camera moved a few pixels.
* **LayerFrame:** immutable pixels or geometry plus content key, geographic/world
  extent, portrayal scale, DPR, valid-time/source provenance, coverage/missing
  masks, byte cost, and publication generation. Freeze backing arrays/images;
  workers must not mutate buffers retained by a presenter.
* **UI/worker boundary:** retain `UiScheduler` delivery on the GUI thread. GDAL
  handles stay private to their worker; never move a UI-owned dataset onto a new
  thread. No worker touches QWidget/QQuickItem or scene-graph resources.
* **GPU boundary:** texture/buffer creation and destruction obey Qt's scene-graph
  thread/context rules. GUI updates hand off immutable payloads. Retain Python
  attribute/geometry/material wrappers for the C++ node lifetime; exercise scene
  invalidation, hide/show, window destruction, and backend failure explicitly.
* **Publication:** reject wrong source/time/style generations. Camera-only
  supersession may retain useful cache content, but cannot publish it under an
  incorrect content identity. Keep last valid coverage while a replacement is
  prepared; distinguish preview, current, missing, and unavailable coverage.

Do not copy the experiment directly into the application: it excludes dynamic
route markers, UI overlays, progressive data updates, and backend fallback.

## Implementation sequence

Every slice should be independently testable and reviewable. Do not combine
unrelated storage refactoring, sampler changes, and renderer integration.

### R0 — Close experiment/design gates

1. **R0.1 Texture parity (first implementation slice):** isolate the fractional-
   translation difference in the controlled fixture. Correct coordinate/sampling
   behavior or propose an explicitly reviewed sampling-policy change. Preserve
   the declared gates: foreground MAE <=12/255; full composite MAE <=1/255 and
   p99 channel error <=8/255; silhouette precision/recall >=99% within one physical
   pixel and alpha coverage within 95–105%. Do not weaken them to get a pass.
   Store candidate results alongside the historical failures. Make the native
   regression test require the complete composite gate once corrected.
2. **R0.2 Embedding:** compare `QQuickWidget` and an embedded `QQuickWindow` inside
   the real workspace. Prefer widget-compatible integration if its extra pass is
   acceptable; do not assume the standalone QQuickView result survives embedding.
   Test docks, stacking, menus/tooltips, focus, resize, native pinch/scroll, and
   multiple map views. Record the chosen host and render-loop decision.
3. **R0.3 Resource envelope:** profile GPU execution, textures, MSAA intermediates,
   swapchain, upload stalls, current RSS, and peaks on the reference machine.
   Choose and record numeric CPU/GPU/in-flight limits before enabling retained
   caches. The prototype's 37.6 MiB image figure is not total GPU memory.
4. **R0.4 Matrix:** validate native 1x/2x DPR, fractional zoom, calm/strong/overlapping
   glyphs, dark/day palettes, opacity, masks, and image edges. Inspect the supported
   PySide/Qt minimum as well as the measured Qt 6.12 environment. Unsupported
   backends must be recognized, not mislabeled as hardware acceleration.

Exit: closed visual regression, documented embedding/render-loop choice and
numeric resource envelope, no unexplained missing content. R0.1 can land before
R0.2–R0.4; it does not authorize turning the GPU renderer on by default.

### R1 — Feature-flagged GPU presentation, existing pipeline

1. Extract only the scene/presenter boundary needed to keep the current QWidget
   painter as the reference. Test the CPU presenter before adding a GPU host.
2. Add an explicit opt-in Qt renderer choice (`cpu` / experimental `gpu`), default
   `cpu`. Record requested/actual backend and fallback reason in diagnostics.
   No automatic production migration or unrelated settings changes.
3. Present retained base and navigation images as independent textures. Render
   fixed-screen-size wind geometry using the validated opacity/antialias policy.
   Upload changed content only; camera motion should update transforms/geometry,
   not recreate unchanged QImages. Apply frame generations atomically.
4. Restore all previously excluded behavior: route-plan/selected-point markers,
   tracks, AIS, dashboard/caption, coverage notice/actions, picking/placement,
   cursor readouts, timeline and live isochrones, main/log maps, and attribution.
5. Handle hide/show, DPR/screen change, resize, palette/time/source change,
   shutdown, context loss, and failed initialization. Dispose old resources on
   the correct thread; maintain a tested CPU fallback without resetting user
   preferences, view, or data selections.

Likely production touch points: `qt/mapcanvas.py`, `qt/window.py`, `qt/workspace.py`,
`qt/controller.py`, `qt/scheduling.py`, and new Qt presenter/scene modules. Keep
projection/motion math shared; avoid a second subtly different gesture engine.

Exit: the full saved startup app works behind the flag, including real input and
dynamic updates; unchanged field/source semantics and visible coverage; measured
latency benefit inside the real window, not just the retained microbenchmark.
This milestone does not promise to eliminate the existing weather settling delay.

### R2 — Remove synchronous static rebuilds and split invalidation

1. Attribute chart drawing, allocation, compositing, ownership copy, and upload
   separately. Baseline ~45 ms base rebuilds are an inclusive span, not proof of
   one particular bottleneck.
2. Prepare static chart frames/chunks with private worker handles. Retain valid
   frames while work completes. Coalesce view requests without starving useful
   work; do not let paint callbacks become the only loading trigger.
3. Separate chart, weather fill/isobar, marine, navigation, and dynamic-UI
   revisions so an unrelated overlay completion does not invalidate everything.
4. Introduce progressive uploads without exceeding the in-flight byte reserve or
   causing a full viewport upload on every input event. Validate seams, label
   placement/collision, native chart selection, clipping, and transparency before
   adopting a fixed chunk/tile size.

Exit: no file access or expensive static portrayal on the interaction thread;
fewer synchronous rebuild/upload stalls; no holes or changed chart selection.

### R3 — View-independent data working set

1. Profile worker stages: source opening/indexing, decoded bands, transforms,
   sampling, masks, wind fill, and contour generation. Measure cache churn across
   the actual 33 selected files before choosing an eviction strategy.
2. Add worker-owned, byte-accounted source/index/band or geographic-chunk caches.
   Source resolution and needed time brackets remain authoritative; do not prune
   older runs merely because a newer one exists if it supplies fallback coverage.
3. Key reusable work by source-set/time/field/extent, not exact screen center.
   Include interpolation halos, projected grids, longitude wrapping, masks, and
   contour boundary context. Reuse complete useful work instead of canceling it
   at each small camera change. Cancellation must be bounded and observable.
4. Keep the routing/sampling numerical interface stable. If implementation must
   touch shared sampling/routing code, run the full suite and differential tests,
   including selected-source provenance and valid-time behavior.

Likely touch points: `rendering/windrenderer.py`, `rendering/overlayrenderer.py`,
`rendering/encrenderer.py`, and display-facing adapters around
`core/gribsampling.py`. Do not assume windowed GRIB reads avoid full decoding;
measure actual driver behavior before choosing that optimization.

Exit: repeated pan/return/time replays show less canceled/duplicated preparation
and faster usable weather coverage, with unchanged numerical/provenance results.

### R4 — Predictive local prefetch

1. Maintain separate visible, rendered, retained-data, and speculative extents.
   Track coverage by layer/source/time; a nonempty surface is not proof of coverage.
2. Queue priorities: visible gap > predicted destination > nearby region >
   adjacent zoom. Reserve worker capacity for visible work; deduplicate overlapping
   requests and promote existing speculative work rather than duplicating it.
3. Estimate lookahead from smoothed pan velocity, inertia destination, zoom
   direction, and measured preparation latency. Apply spatial/byte/work caps and
   hysteresis; do not allocate ever-larger viewport images.
4. On reversal, keep useful completed regions and reprioritize/cancel irrelevant
   speculation. A source/time/style change invalidates the affected generation.
   Closing/hiding a map releases its demand without disrupting another map.
5. Keep network acquisition explicitly separate: preserve existing user-triggered
   fetching and provider rate limits. Automatic remote prefetch requires its own
   settings, consent/policy, quotas, and validation; it is not enabled by R4.

Exit: fewer visible misses and better reverse-pan/adjacent-zoom reuse, measured
under controlled loading delays; no visible-request starvation or unbounded
speculative traffic. Report useful-prefetch hit rate AND wasted work/bytes.

### R5 — Retention hardening and rollout

* One resource ledger with domain limits for decoded CPU data, prepared geometry,
  CPU images, GPU textures, transient MSAA/layer targets, and in-flight uploads.
  Account for shared buffers once, reserve allocations before work starts, and
  configure/measure relevant GDAL caches separately. Do not equate RSS with VRAM.
* Protect currently visible content and in-use uploads; retain recent geographic
  regions and useful adjacent zoom/time content within the budget. Evict
  speculative and least-useful content first. On pressure, reduce speculation,
  not selected-source fidelity; reject unsupported allocation or fall back with a
  diagnostic if the visible working set cannot be supported safely.
* Validate >10 minutes of repeated pan/zoom/return/time changes, multiple maps,
  resizes and visibility changes, then sustained cold-region travel. Require a
  measured memory plateau and no resource leaks or recurring upload spikes.
* Keep the flag until full-app coverage/quality, fallback, native input, and
  supported-platform gates pass. Default-on is a separate reviewed decision.
  CPU fallback removal, GTK migration, and automatic downloads are out of scope.

## Measurement and acceptance contract

Extend existing tools instead of creating incomparable benchmarks. Record a
protocol version, snapshot/fixture hashes, code revisions, actual backend, host,
DPR, viewport, selected/rendered content counts, pacing and repeat counts.
CPU QWidget grabs and Qt Quick swap callbacks remain separate metric families.
No claimed GPU execution or scanout timing without a suitable native probe.

Required observability for R2–R5: request ID, content key, view generation,
priority, coverage, creation/start/completion/publication/cancel timestamps,
cache hit/miss, byte reservations/eviction, and fallback reason. Capture source
and time provenance without dumping credentials or private route data.

| Area | Required acceptance evidence |
| --- | --- |
| Interaction | Warm same-workload p95 <=16.7 ms and p99 <=33.3 ms are provisional reference-machine targets; count/max stalls >50 ms and UI service latency too. Validate actual native input separately. |
| Content | Same sources/time/density and geometry; image/coverage checks prevent missing data from looking faster. Preserve chart seams, labels, masks, attribution, and dynamic overlays. |
| Pipeline | Compare time to usable/current coverage, queue wait, wasted/canceled work and completed publication, not just worker idle. Predeclare numeric coverage-latency goals after stage profiling; current 3–5 s idle probes are not coverage truth. |
| Prefetch | Controlled-delay offline replay demonstrates improved visible miss/return-view behavior without starving visible requests; report useful hits and wasted bytes together. |
| Memory | Recorded numeric budgets and peak/in-flight accounting, current RSS plus native GPU/UMA evidence, and a long-run plateau; process high-water RSS alone is insufficient. |
| Safety | Unsupported backend/context failure and cancellation/close paths retain state and fall back correctly; no unexpected download or instrument connection. |

Compare serial repeated runs on the same machine with the same data. Do not
rebaseline to conceal a failure, pool diagnostic profiled runs into acceptance
timings, or assert machine-dependent timing thresholds as universal CI tests.

## Validation, review, and immediate status

* R0.1: math tests and explicitly opted-in native GPU integration tests in
  `test/test_gpu_rendering_experiment.py`, plus controlled and saved-startup
  candidate reports. Existing visual gates remain unchanged.
* R1/R2: targeted Qt integration tests covering `test/qt_preview_checks.py`
  motion/pinch/trackpad, lifecycle, padded-frame containment, instruments,
  navigation/routing, marine and timeline behavior; new presenter contract tests.
* R3/R4/R5: deterministic scheduling/cache/pressure/source-generation tests,
  offline loading-delay fixtures, coverage assertions and numerical/provenance
  differential tests. Full suite when shared sampling/routing changes.
* For each code slice: targeted tests, `black --check`, and `git diff --check`
  on changed files; preserve previous recorded results and failed artifacts.
* Keep commits focused. Pushes/PR creation follow user authorization; fork PRs
  target `dev` and remain unmerged until approved. Do not fold unrelated storage
  fixes into the renderer change.

Immediate implementation order: **R0.1 texture parity**, then R0.2 host integration
and R0.3/R0.4 resource/platform gates, then the R1 feature-flagged presenter. R2–R5
follow as distinct measured slices. The CPU renderer remains the production
default throughout this first tranche.
