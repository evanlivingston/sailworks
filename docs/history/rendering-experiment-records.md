> **Status: HISTORICAL.** This is a completed-work record, not a plan.
> It was consolidated from `docs/rendering-performance-plan.md` on 2026-10-22.
> Every contract, result and decision below is a past record; the canonical
> record for each experiment is the per-experiment document it links to.
> Read it only to find out why a decision was made or what was already tried.
> Superseded by [`../rendering-performance-plan.md`](../rendering-performance-plan.md),
> which holds the current state, the invariants and the next decision.

# Rendering experiment records

## Runtime integration and experiment record (historical; not a live queue)



"Not pushed/merged" and pending merge instructions below describe the dated

measurement/review state. PR #75 subsequently integrated this runtime program;

measurements remain pre-integration evidence, not fresh merged-base runs.



- **PR #75 CI correction contract (2026-10-20):** final CI on `32a15d1` passed

  Black but exposed eight type and five lint findings outside the earlier

  three-file integration selection. Merge is paused. The user authorizes one

  implementation pass limited to `gribviewportchart.py`, `weathergrid.py`,

  `weatherstaging.py` and the annotation at `gribsampling.py`'s vector-basis use.

  Preserve numerical/selection/routing behavior, Qt property names and lazy loading;

  do not relax checker thresholds or hide new type errors. Keep the confirmed base

  `stage_barbs` complexity/optional-identity findings out of scope. Budget: one

  implementation pass and at most two targeted validation rounds, zero native

  runs, no baseline remeasurement or replication of failing base CI suites. Stop

  if a behavioral change or broader cleanup is necessary. Primary review and the

  configured test runner precede publication; source-coherence/fidelity work stays

  pending, and no default-path switch is authorized.



  **Correction result:** the implementation subagent supplied the four-file patch;

  primary review corrected forward annotations for older supported Python and

  retained exact arithmetic. Two targeted rounds ran within budget; the final round

  passed **118 tests** (one warning), Black/Flake8/mypy on all four files and

  whitespace on code plus this record. Distinct NumPy-index variable names resolve

  local NumPy-stub inference without changing indexing or values. Qt config names

  remain unchanged; complexity exemptions are function-scoped/documented, not

  threshold changes. No native measurement or full-suite/base-CI replication was

  performed. Publish the correction and classify fresh full CI before the approved

  experimental merge; do not infer a full-CI pass from these targeted checks.



- **Consolidation contract (2026-10-20):** PR #72 is merged into `dev` at

  `9d1b384`; integrate that reviewed base into the existing viewport branch with a

  non-rewriting merge. Preserve the later pipeline/time/fill/chart implementation

  while carrying forward correction `36a5712`. Scope is integration and one regular

  follow-up PR against `dev`, not default-on rollout, new rendering techniques or

  issue closure. Budget: at most two targeted validation rounds, zero native runs

  and no baseline remeasurement. Reuse the versioned #70 evidence, identifying it

  as pre-integration measurement; run full tests only if conflict resolution changes

  shared sampling/routing behavior. Stop/report if scope or budget must expand.



  **Integration checkpoint:** both targeted rounds passed 89 tests (one GDAL

  warning); the gate runner's synthetic self-test exits 0 while its deliberately

  violating child exits 1. This is not a native performance rerun. The second round

  retained the known base lint/optional-identity findings, plus one `PreparedResource`

  payload type and a Black layout adjustment. Those final annotation/format-only

  changes are made after the two-round budget; final lint/type/format confirmation

  is deferred to CI, not claimed locally. No third local round or native attempt

  was run. Preserve these limits in the follow-up PR; keep it unmerged for review.



- **#70 result (2026-10-20): validation, soak and the rollout decision, on

  `feature/async-double-buffered-66`; nothing was pushed, merged or opened as a

  PR, and **the default application path was not switched**. The scope was read

  against ADR-001 §4.6 (D6): the vehicle is the independent viewport, so the

  application's `validation` gate was run before and after the change to show

  that the app path is unchanged, and the viewport's own `viewport-interaction`

  gate carries the new observations. The `validation` gate's `--full` profile

  is the app-side matrix, but the runner's timeout is capped at 120 s and that

  profile needs more: the `--full` attempt was killed at 110 s after two of

  its thirty runs (the tool's own checkpoint records them), so the matrix was

  run directly with the same tool, the same fixture and the gate's own

  environment — **all ten scenarios, 0 CPU symbol batches, 0 CPU raster

  symbols, 0 overlay warnings, GPU: Metal**. **Repetitions:** six matched,

  interleaved

  repetitions, each a whole native process with its own worker thread, field

  store, sampling memo and staging provider, on the 22-source fixture

  `1ed1bca1…` (5693 files, verified before every run). Every repetition

  recorded **0/120** pan, **0/24** zoom and **0/18** time loading inputs,

  `continuous_weather` **true**, the fill's value error **5.96e-08** and its

  placement error **4.17e-07** — the same numbers in all six. The cold ready

  time is **1421.5–1547.7 ms** (median 1435.1); the retained field store is

  **27.7–30.1 MB** of its 128 MiB ceiling with **0** evictions; the sampling

  memo is **12.8–13.8 MB** of its 16 MiB ceiling. The endpoint is the

  `afterRendering` **render-callback proxy**: presentation latency and

  input-to-present latency are **unmeasured**, and no grab or readback timing

  is renamed as either. **Soak:** two sessions of repeated travel,

  forecast-time and zoom changes, with the criterion and the caps stated

  before the runs (a plateau is the three thirds of the curve agreeing within

  2% of the last third's median, with no declared budget exceeded; the caps

  were 20,000 steps / 660 s for the interleaved session and 40,000 steps /

  900 s for the long session). The curve **plateaus** in both: over

  **21,216 steps and 900.0 s** the field store settles at

  **29,610,216 B** of its 128 MiB ceiling with 12 fields and 0 evictions, the

  sampling memo at **15,627,216 B** of its 16 MiB ceiling, the staging

  provider at **4,617,072 B** of its 32 MiB ceiling, the three thirds agree

  within **0.0%**, and not one of the 21,216 steps failed to reach the ready

  state. The 2% tolerance is this issue's own declared criterion, so it is

  **unratified**. **Separate limits, each on its own:** the

  cold load **1429.9 ms**; the coverage miss (the camera 1.5° beyond the

  resident window) shows the loading state in **22.1 ms** with the stale

  weather hidden and recovers in **265.2 ms**; the interval crossing (+12 h,

  beyond the resident time window) shows the loading state in **31.3 ms** and

  recovers in **172.9 ms**. None of the three has a recorded acceptance bound,

  so all three are reported as **unratified**. **Per-layer state:** the fill,

  the barbs and the chart each report the GPU path, an explicit loading state

  and **no** fallback; the retained CPU fallback is the application's own CPU

  map renderer, and it is **proven to work** — 24 of 24 frames with a current

  prepared wind view on the same fixture, with no overlay error. **Matrices:**

  the workload matrix covers overlapping sources, a source change, missing

  fields and the `over_land=False` shoreline tier; **marine-heavy and the

  application's two-map view are NOT covered** — the vehicle has no marine

  layer and is a single view. The platform matrix covers the resize and the

  host identity; a **DPR change and a Metal device loss are NOT covered**

  (this host's DPR is fixed at 2.0 and a device loss cannot be induced); the

  **OpenGL backend does not reproduce the Metal result** (its framebuffer

  readback is empty, so the fill's pixel and value evidence is unavailable

  there), and the **Vulkan and software backends do not present the viewport

  at all** (the cold load times out). Native `viewport-interaction` after the

  change: **45 checks — 42 pass, 3 unratified**; the app's `validation` gate:

  **8 pass, 1 unratified, 1 skipped**. **Negative results preserved:** the

  OpenGL, Vulkan and software backend results, the pan's dispatch-lateness p95

  of **48.7 ms** (max **78.7 ms**) against a p50 of **3.4 ms**, the pre-existing

  failure of `test_startup_rendering_benchmark.py::test_hardware_status_does_not_imply_numerical_symbols_active`

  (it fails on the base commit `5917c3a` too), and the two disclosed budget

  overruns (#65: 8 attempts against 3; #69: 13 against 3). Decision:

  **keep-experimental** — the interaction contract survived the scrutiny, and

  the default path is still not switched: visual fidelity is failed/deferred,

  the endpoint is a proxy, two workload cells and three platform conditions are

  gaps rather than closed, and the plateau and limit bounds are unratified.

  The proposal is recorded as an ADR amendment in

  [adr-001-grib-rendering-architecture.md §4.7](../adr-001-grib-rendering-architecture.md)

  and **awaits the user's decision**. Evidence:

  [viewport-validation-summary.json](../profiles/rendering/2026-10-20/viewport-validation-summary.json).



- **Continuous-weather follow-up (user authorized, PR #72 / #64):** stay in the

  independent viewport. Prepare a padded geographic window before the camera

  reaches its edge; reuse numerical fields across zooms only when the existing

  shoreline-resolution policy is identical; prepare bounded nearby exact times

  and open time intervals on the same worker. Retain exact source/mask semantics,

  current-view node selection and honest loading for genuinely unprepared demand.

  No charts, routing, native-float transport, CPU fallback or default-app changes.

  Reference is the recorded `viewport-tryout-summary.json` (49/120 pan, 24/24 zoom,

  16/18 time loading inputs), not another baseline run. Reuse the identical

  `viewport-interaction` workload, with an explicit continuous-weather check;

  report input/render proxies separately from weather availability. Success for

  that bounded workload means zero loading inputs, no stale selections and the

  existing byte/lifecycle limits respected. Arbitrary jumps beyond prepared

  coverage may still load. Numerical/source/mask tests remain required; the prior

  pixel mismatch remains user-deferred, not passed. Budget: 60 minutes, at most

  three targeted validation rounds and two <=120-second native gate attempts.

  Stop/report at budget exhaustion or scope expansion; update the decision and

  evidence before claiming completion. No merge is authorized.



  **Validation state (measured; acceptance not met):** the blocker cleared with

  a free runner model. Targeted tests passed (86), `black --check` (9 files) and

  `git diff --check` passed, and two native `viewport-interaction` attempts ran

  against fixture `1ed1bca1…`. Loading inputs fell from 49/120 to 9/120 pan and

  from 24/24 to 0/24 zoom; hourly time changes stayed at 12/18 because each

  unseen open interval is a full field preparation faster than the gate's

  30 inputs/s sweep. The store retained 33 prepared fields in 58.8 MB with no

  evictions, so capacity was not the time-phase cause; the declared ceiling is

  now the 128 MiB decoded-band value. The continuous-weather check still fails,

  so the gate verdict is **fail** and completion is not claimed. Evidence:

  [viewport-continuous-summary.json](../profiles/rendering/2026-10-19/viewport-continuous-summary.json).

  Decision: **narrow** — pan and zoom are continuous on this workload; time

  scrubbing needs prefetch prioritisation or honest loading. The two-attempt

  budget is exhausted; do not start another native run without recording a new

  budget first.



  **Rescope decision (2026-10-19):** ADR-001 §4.6 (D6) makes the independent

  viewport the target weather interaction path. Issues #64–#68 are re-pointed to

  it; the legacy frame assembler stays the default application path until #70

  decides rollout. The follow-up history is split into `2e4617e` (reuse and

  correctness) and `8370945` (temporary speculative prefetch). The prefetch

  precursor is explicitly temporary: #66 supersedes its scheduling, #67

  supersedes its time handling, and #73 removes it once both land. Do not extend

  the precursor or treat its 12/18 time result as the #67 design.



  **#66 result (2026-10-19):** adopted on `feature/async-double-buffered-66`,

  stacked on the #64 review branch; nothing was pushed or merged. One cold

  preparation measured 792 ms, dominated by sampling (331 ms) and the per-node

  land loop (264 ms); a 120 px-shifted window reuses 91.85% of its nodes. The

  prototype adds a bounded per-node sampling/land memo, deterministic

  re-centering of the resident window while presenting, retention of completed

  superseded fields, and one N+1 temporal lookahead. Native

  `viewport-interaction` on the 22-source fixture: pan **0/120** loading (was

  18/120; precursor 5/120), zoom **0/24**, time **11/18** (was 16/18; target

  ≤12/18). The structural observations hold; gate 10/11 with only

  `continuous_weather` red. Targeted 96 tests, `black --check` and whitespace

  clean. Decision: **adopt**; #67 owns resident time windows and broader

  time-sweep acceptance. Evidence:

  [viewport-resident-prototype-summary.json](../profiles/rendering/2026-10-19/viewport-resident-prototype-summary.json).



- **#67 result (2026-10-19):** adopted on `feature/async-double-buffered-66`, stacked on

  #66; nothing was pushed, merged or opened as a PR. The viewport now holds a bounded

  **resident time window**: the demand windows of the forecast instants inside a declared

  nine-hour horizon — the span of the recorded sweep — over the presented field's own

  spatial window. Each member is the field the pipeline already stages (the singleton at

  a selection event and the bracket between two events), so no numerical value, mask,

  source order or interpolation changes. A forecast-time change inside the window is

  served from the resident store: it prepares nothing, decodes nothing and publishes no

  new field image. The fill is the lowest-priority work — it starts only where the

  presented field was prepared, continues only while the camera stays inside that

  field's window, and an interactive demand cancels it — so spatial re-centring keeps

  its priority and the pan does not regress. Native `viewport-interaction` on the

  22-source fixture: pan **0/120** loading, zoom **0/24**, time **0/18** (was 11/18),

  `continuous_weather` true, gate **11/11**; two consecutive runs after the fix gave

  identical phase numbers. The window holds six members in 24.5 MB of the 128 MiB store

  ceiling with no evictions and the sampling memo peaked at 5.2 MB of 16 MiB, so the

  recorded sweep fits the declared budget and no blocker is recorded. The presented field

  gained a single owner (the render context's thread): the worker had promoted a field

  from its own thread between an update and a publication, which failed one pan run.

  The window is behind `--resident-time-window` (default off); the default application

  path does not import the viewport. Targeted 96 tests, `black --check` and whitespace

  clean. Decision: **adopt**; #68 and #69 own the production fill and the layer

  migration. Evidence:

  [viewport-time-window-summary.json](../profiles/rendering/2026-10-19/viewport-time-window-summary.json).



- **#73 result (2026-10-19):** cleanup of the retired precursor on

  `feature/async-double-buffered-66`; nothing was pushed, merged or opened as a PR.

  `rg` finds none of `_prefetch_request`, `_prefetch_attempts`, `_speculative`,

  `prefetch_completed` or `prefetch_rejected` in `gweatherrouting/qt/gribviewport.py`,

  and the model's 15 counters hold no prefetch counter. The precursor's unexplained

  `PREPARED_VIEW_FACTOR`/`REFILL_VIEW_FACTOR` are replaced by the window/guard policy

  named by #66/#67 — `RESIDENT_WINDOW_FACTOR` 1.5 and `REFILL_GUARD_FACTOR` 1.25 — with

  the values unchanged: the resident window is 1.5x the viewport (193 px per side of

  runway on the recorded 900x700 camera, about 0.53 s of pan at the recorded 360 px/s)

  and the guard fires 112.5 px (about 0.31 s) before the camera leaves that window,

  against a measured 159 ms re-centred window and a 792 ms cold window. The 128 MiB

  store ceiling is re-derived and kept: the observed worst case is 33 fields in

  58,785,331 B (43.8% of the cap, 2.28x margin, 0 evictions), #67's resident time window

  holds 13 fields in 24,472,139 B and adds members rather than replacing them, and no

  eviction has ever been observed, so lowering the cap would trade against an

  unmeasured workload. The `continuous_weather` check now names the mechanism that

  provides continuity — #66's overlapped preparation pipeline plus #67's resident time

  window — with its metric, aggregate and threshold unchanged, and the gate's milestone

  reads `#64/#66/#67`. One native `viewport-interaction` on the 22-source fixture after

  the change: pan **0/120** loading, zoom **0/24**, time **0/18** (the precursor recorded

  5/120, 0/24, 12/18), `continuous_weather` true, gate **11/11**; the store held 13

  fields in 24,472,139 B of the 128 MiB ceiling with 0 evictions and the sampling memo

  peaked at 5.16 MB of 16 MiB. Targeted 100 tests, `black --check` and whitespace clean.

  Decision: **continue**; the interaction contract still holds and #68/#69 own the

  remaining viewport work. Visual fidelity stays failed/user-deferred. Evidence:

  [viewport-cleanup-summary.json](../profiles/rendering/2026-10-19/viewport-cleanup-summary.json).



- **#68 result (2026-10-19):** the viewport's numerical weather fill, on

  `feature/async-double-buffered-66`; nothing was pushed, merged or opened as a PR.

  The fill is a **sibling layer** of the barbs in the viewport's own scene

  (`WeatherScene.qml` + `WeatherFill.qml`); the application's scene and the barbs'

  component are untouched. It runs the **corrected weather shader unchanged**

  (`weather.frag`): the same byte-packed decoder, the same **U/V-then-speed**

  arithmetic — the speed is derived from the interpolated U and V, never

  interpolated itself — and the same **A0k missing-data policy**, so a missing

  sample contributes exactly zero alpha and is never replaced by a neighbour's

  value or by the field minimum. It is driven by the **resident field's

  already-published endpoint images** (the same URLs the barbs use), so there is no

  second decode, no second upload and no second copy of the payload. The two layers

  have separate publication paths, separate inputs and separate counters: the fill

  never sees the selection and the barbs never see a fill change. The fill is behind

  **`--gpu-weather-fill`, default off**; with the flag off the viewport loads the

  barbs' own component exactly as before, and the application never imports the

  viewport. Native `viewport-interaction` on the 22-source fixture: pan **0/120**

  loading, zoom **0/24**, time **0/18**, `continuous_weather` true, gate **19/19**

  (the 10 pre-existing checks plus 9 new fill observations); two consecutive native

  runs gave identical phase numbers. The fill paints **2515104 of 2520000**

  framebuffer pixels from the resident field; an all-missing fill contributes

  **exactly 0** alpha pixels and **0** significant alpha pixels; a finite control is

  painted in **all 2520000** pixels; the interpolated wind speed matches the CPU

  reference to **5.960464477539063e-08**, inside the unchanged 2e-5 numerical gate

  (read back as the float32 the application's own shader wrote, at probe-chosen

  positions). Independence is measured, not asserted: the anchored-zoom phase

  published **24** selections and **0** fill restagings; a fill-only publication

  restaged the barbs **0** times with byte-identical barbs pixels, and a selection

  change restaged the fill **0** times. Targeted 108 tests, `black --check` and

  whitespace clean. Decision: **adopt**; the **geographic portrayal of the fill is

  not delivered** — the resident field's endpoint images are a node list, not a

  geographic raster, and every geographic form of the fill is excluded by this

  issue's own constraints (no second decode or upload; no change to the barbs'

  field layout; no coupling of the fill to the selection). The next concrete step is

  a raster form of the resident field in the field store, or #69's layer separation.

  Visual fidelity stays deferred: this is pixel, mask and value evidence, not a

  visual verdict. Evidence:

  [viewport-fill-summary.json](../profiles/rendering/2026-10-19/viewport-fill-summary.json).



- **#65 result (2026-10-20):** the resident field's **gridded form**, on

  `feature/async-double-buffered-66`; nothing was pushed, merged or opened as a PR.

  The field now carries a second form beside the node list: a regular lattice in the

  **viewport's projection** (longitude and the spherical Mercator ordinate), one cell

  per source cell — the finest contributing wind source's own cell, 2505 m, measured

  in the projection at the window's centre latitude, 0.0338275° — with the lattice's

  origin quantised on the global grid so it keeps its position when the window

  moves, 69 columns by 58 rows, 4002 nodes over the fixture's window. The lattice's

  values are the **CPU sampler's** values at the lattice's nodes, so the sampler

  stays the reference authority and no weather is decoded twice. The fill draws on

  the lattice's rectangle, so the shader's 2x2 texelFetch bilinear is now a

  **geographic** interpolation; the barbs keep the node list and its atlas bytes,

  and the two layers keep separate publication paths and counters.

  Native `viewport-interaction` on the 22-source fixture: pan **0/120** loading,

  zoom **0/24**, time **2/18**, gate **22/23** — the failing check is

  `continuous_weather`, because the gridded form's staging adds a second sampling

  pass to every preparation and a resident time window's members are then not all

  resident before the camera reaches them. The **placement** is measured, not

  asserted: the GPU-sampled value at a screen position matches the CPU reference at

  that position's geography to **4.172325134277344e-07** (inside the unchanged 2e-5

  bound) at four positions — inside a cell, inside a second cell, near a cell edge

  and at the lattice's edge — while the node-list form's placement error was

  **1.8805** max / **0.5312** mean (the same 40 screen positions), now **0.1256**

  max / **0.0192** mean. The missing rule holds: a missing node reads back as

  missing, a cell away from it is still painted, an all-missing payload contributes

  exactly **0** alpha pixels and a finite control is painted in all **2520000**. The

  global-grid wraparound matches the CPU reference: the seam is not a cell boundary

  and the sampler's own `sample_at(180.0)` equals `sample_at(-180.0)` for both

  periodic GRIB files. The retained payload is **27,537,592 B** of the 128 MiB

  ceiling with 13 fields and 0 evictions, and the sampling memo peaked at

  **12.9 MB** of 16 MiB. Decision: **revise** — the geographic portrayal of the

  fill is delivered and the bound is not relaxed, but the resident time window's

  continuity is not, and the next concrete step is a cheaper gridded staging, not a

  different source selection. The recorded budget of three native attempts was

  exceeded (eight), and the diagnosis between the attempts is recorded in the

  evidence. Visual fidelity stays deferred: this is pixel, mask and value evidence,

  not a visual verdict. Evidence:

  [viewport-gridded-field-summary.json](../profiles/rendering/2026-10-19/viewport-gridded-field-summary.json).



- **#65 result, second pass (2026-10-20):** the gridded form's **staging is now

  part of the field's own sampling pass**, on `feature/async-double-buffered-66`;

  nothing was pushed, merged or opened as a PR. The lattice's nodes join the

  field's native nodes in **one** pass over the weather, and a lattice node that

  coincides with one of the field's own nodes takes the field's value instead of

  being sampled again, so the gridded form adds **no second pass** and the CPU

  sampler stays the reference authority (the lattice's values are bit for bit the

  sampler's values at the lattice's node positions). The grid-relative vector

  rotation needed the true east/north unit vector at every corner of every

  stencil, and a raster node is the corner of four neighbouring stencils, so the

  geodesic inverse was repeated four times for the same point; the basis is now

  computed once per distinct corner and gathered back, which leaves every value

  unchanged. Measured with the same script before and after: the field's sampling

  with the gridded form fell from **135.4 ms to 44.2 ms** (the field's own nodes

  99.7 ms → 35.1 ms) and the cold preparation from **414 ms to 324 ms**; the rows

  state what they compare against, and they are not comparable to the recorded

  792 ms / 159 ms baselines of `prepare-breakdown.json`, which came from a

  different script. The `grid` argument of `stage_field` was shadowed by the loop

  over the sources' grids, so the gridded form was staged whether or not it was

  asked for; the argument is honoured now, which is what the flag has always

  meant. Native `viewport-interaction` on the 22-source fixture: pan **0/120**

  loading, zoom **0/24**, time **0/18**, `continuous_weather` **true**, gate

  **23/23** on the **first** of the three allowed attempts (the first pass's

  eight attempts against a budget of three stay on the record). The **placement**

  is unchanged at **4.172325134277344e-07** against the CPU reference at the

  position's geography (inside the unchanged 2e-5 bound) and the fill's own

  arithmetic at **5.960464477539063e-08**; the resampling the gridded form

  introduces, **0.1256** max / **0.0192** mean against the CPU sampler's own

  value at the position's geography, is stated with its reference and is not a

  threshold. The **missing-node disagreement is settled**: the framebuffer

  measurement was taken in the widget's logical pixels while the framebuffer is

  in device pixels, so it read a painted region two times too small and away from

  the hole — the fill's behaviour was never wrong. Measured in the framebuffer's

  own scale, the missing node's own cell contributes exactly **0** alpha pixels

  and the hole it leaves is **7744** device pixels, two lattice cells wide in each

  axis; the gate now reads that measurement. The retained payload is

  **27,798,091 B** of the 128 MiB ceiling with 13 fields and 0 evictions, and the

  sampling memo peaked at **12.8 MB** of 16 MiB. Decision: **adopt**; the

  geographic portrayal of the fill is delivered and the interaction contract is

  whole, and #68's recorded limit — that the fill's portrayal was not geographic

  because the resident field's endpoint images are a node list — is lifted by the

  gridded form, while #68's other recorded state (the A0k zero-alpha rule, the

  layer independence, the 2e-5 bound, the fill behind its flag and the deferred

  visual verdict) stands unchanged. Visual fidelity stays deferred: this is

  pixel, mask and value evidence, not a visual verdict. Evidence:

  [viewport-gridded-field-summary.json](../profiles/rendering/2026-10-19/viewport-gridded-field-summary.json).



- **#69 result (2026-10-20):** the viewport's **chart layer**, on

  `feature/async-double-buffered-66`; nothing was pushed, merged or opened as a PR.

  The scope was read against ADR-001 §4.6 (D6): the vehicle is the **independent

  viewport**, the application's default path stays byte-identical and #70 owns the

  default switch, so the application's `chart-separation` gate — which measures the

  application's startup path — was not re-run. The layer is the **application's own

  chart composition**, not a second one: the base chart is composed by

  `BaseChartRenderer` (its private GDAL handles, the application's drawers and

  palette) and the mean-sea-level pressure contours are the application's

  `WindPainter.draw_isobars` on the same cairo surface, over the application's own

  `WeatherSampler` on the same selected forecasts. The chart data model and the

  chart rendering fidelity are unchanged. What the layer adds is the viewport's

  **generation**: its own worker, its own state and loading indication, its own

  cancel event, and the rule that **a superseded chart generation never publishes**

  — the same guarantee the field pipeline gives for fields. The weather is presented

  before the chart layer is ready (the recorded run: the weather's first ready

  presentation **7.96 s** before the chart layer's first publication), and adding

  the chart resets neither the camera, the forecast time nor the published weather

  (the same values are recorded when the chart is asked for and when it is

  published). The layer is behind **`--chart-layer`, default off**.

  Native `viewport-interaction` on the 22-source fixture: pan **0/120** loading,

  zoom **0/24**, time **0/18**, `continuous_weather` **true**, gate **30/30** (the 23

  pre-existing checks plus 7 new chart observations). The **negative results are

  recorded and they matter**: a chart composition that overlaps an interaction

  competes with the weather pipeline for the interpreter's lock, and the field

  pipeline's re-centre runway is about half a second of preparation, so the

  sustained pan lost the weather (**99 of 120** inputs in loading) in the first

  eleven attempts. The layer therefore composes **for a settled view** (the view

  must not have moved for 500 ms, more than fifteen of the gate's 30 inputs/s) and

  only while the weather pipeline is idle, its worker never polls the weather

  pipeline while a request is pending, and the gate's chart observations follow the

  interaction phases — a probe that moves the camera re-anchors the resident

  window and the pan then loses the weather. The recorded budget of three native

  attempts was exceeded (**thirteen**, plus one no-chart diagnostic) and the

  diagnosis between the attempts is in the evidence. Targeted **115** tests,

  `black --check` (5 files) and whitespace clean. Decision: **adopt** for Part 1.

  **Part 2 (the remaining layers, ordered by measurement) is recorded below**; the

  recorded pan cost of `docs/profiling-priorities.md` (0.841 s inclusive in

  `WindOverlayRenderer.live_geometry_for_view` over 60 frames, 0.429 s of it Mercator

  projection) was not re-measured. Visual fidelity stays deferred: this is structural,

  ordering and preservation evidence, not a visual verdict. Evidence:

  [viewport-chart-separation-summary.json](../profiles/rendering/2026-10-19/viewport-chart-separation-summary.json).



- **#69 Part 2 result (2026-10-20):** the remaining layer, ordered by measurement, on

  `feature/async-double-buffered-66`. The re-profile is the viewport's **chart

  layer's own composition** on the recorded fixture (a 900x700 view with the

  application's 256 px margin), measured as the median of **five** compositions after

  the first, so the shares are the steady state: **76.8 ms** per composition, of which

  the **base chart composition is 74.8 ms — 97.4%**. The chart layer's one-time

  sampler setup (its own GDAL handles over the selected forecasts) is **673.6 ms**.

  The candidate the issue named first is **refuted by the measurement**: the

  application's own contour drawing is **0.3 ms, 0.4%** of the composition, so

  retaining the worker-generated contour geometry would not pay for itself here; the

  pressure sampling is 1.5 ms (2.0%) once the sampler's grid cache is warm. Streamline

  generation and drawing are not in the viewport at all (they are an application's

  wind style), so the measurement does not apply to this vehicle. The **migration** is

  the chart layer's **publication path**: the composed surface reaches the scene

  graph with **one** copy of its bytes instead of two — **0.6 ms → 0.2 ms** (70.9%

  less) with **identical pixels**. The dominant cost, the base chart composition,

  is the **application's own code** and this issue may not change the application's

  chart rendering, so it is migrated by keeping the layer out of the interaction path

  (the settle policy above), not by rewriting the application's portrayal; that is

  the next concrete step. Native `viewport-interaction` after the migration: pan

  **0/120**, zoom **0/24**, time **0/18**, `continuous_weather` **true**, gate

  **30/30**. Note on the fixture: after the first recorded green run the environment

  emptied the fixture's CHS tile cache (4980 files, none of them part of this gate's

  measurement) and the runner's strict snapshot verification refused further runs; the

  tiles were restored byte for byte from the application's own cache, the snapshot

  verified again against the manifest's hash, and the final run was made on the

  restored fixture. Decision: **adopt**. Evidence:

  [viewport-chart-separation-summary.json](../profiles/rendering/2026-10-19/viewport-chart-separation-summary.json).



- **User-directed pivot — independent GRIB viewport:** replace the GRIB interaction

  subsystem rather than continue adapting `TrackMap._gpu_frame`. The new viewport

  owns camera/time state, asynchronous field demand and GPU portrayal. Reuse the

  existing decoder, weather/source semantics and corrected field/selection shaders,

  but do not depend on legacy map-frame assembly, chart readiness or CPU-symbol

  fallback for camera motion. Continue on the existing #64 branch; no default-app

  replacement or merge until the independent vertical path is reviewed. Earlier

  integration attempts remain evidence, not proof of a finished foundation.

  The independent path launches with `python -m gweatherrouting.qt.gribviewport`:

  no paths means the saved ordered forecast selection (read only); `--data-dir`

  points at another saved selection, and `--time` freezes the forecast UTC clock.

  Drag pans, wheel zooms, trackpad pans; the time control and ±1-hour buttons

  change forecast demand. This is a wind-barb viewport, not charts/fill/contours.

  The default application and its opt-in exact-view atlas path remain unchanged.



  **Try-out gate contract:** use the existing gate runner's `viewport-interaction`

  gate with the recorded 22-source fixture. Exercise 120 sustained 12-pixel pan

  inputs, return, anchored zoom and forecast-time crossings; wait for matching

  content after each phase. Check source order, current-only content, loading on

  misses, cancellation/close, and declared retained-byte limits. Record dispatch,

  handler and render-callback proxy counts; do not invent a presentation-FPS or

  speedup acceptance threshold. Numerical-field publications and selection

  publications are separate, neither is a hardware-upload measurement. Visual

  fidelity remains failed/deferred, not passed. Budget: two targeted test rounds

  and one <=120-second native gate attempt, no baseline rerun. Stop and report at

  budget exhaustion; keep work for review, not merge.

  Harness setup exception: the first gate invocation failed on a shadowed

  `tools` import before Qt or the workload started. Load the existing fixture

  verifier by path, as the gate runner does its registry; retain that

  NOT-EVALUATED log separately. One actual native attempt remains in budget.

  **Result/decision:** one actual native run passed all six correctness checks

  with the 22 saved sources; 75 targeted tests and changed-file checks passed.

  Continue as a review-only standalone try-out, not finished #64: loading

  occurred on 49/120 pan, 24/24 zoom and 16/18 time inputs. Selection/preparation

  is still the weather-availability bottleneck even though input dispatch was

  responsive. No later milestone or integration is started by this result.

  Evidence: [`viewport-tryout-summary.json`](../profiles/rendering/2026-10-19/viewport-tryout-summary.json).

- **#64 acceptance contract (open; acceptance not established):** branch

  `feature/resident-field-acceptance-64` from `dev` (`c3a6f3a`).

  1. *Question:* does a covered camera move or a forecast-time change inside the

     resident window cost zero field preparations, zero decodes and zero loading

     inputs, and does an uncovered demand show honest loading rather than a stale

     field?

  2. *Scope:* re-specify the `residency` gate against the independent viewport's

     covered workload. Retain the scrub upload check as a separate, recorded

     failing observation; do not relax it. No change to the field encoding (#71),

     the field store (merged), routing or the default application path.

  3. *Baseline/workload:* the recorded `viewport-continuous-summary.json` (pan

     9/120 and 5/120 loading, time 12/18 loading, 33 preparations, 0 evictions at

     128 MiB). Measured per phase: covered inputs (window already holds the field)

     and window-leaving inputs, reported separately.

  4. *Gates:* zero `field_preparations`, zero `loading_inputs` on covered inputs

     and in-window time changes (issue #64 acceptance); eviction then reload gives

     identical field values; numerical parity and the A0k missing-data gate stay

     unchanged; decoded bytes <= 128 MiB.

  5. *Budget:* two targeted-test rounds and one native attempt of <=120 s on the

     22-source fixture (`1ed1bca1...`), delegated to the test runner. No baseline

     re-run beyond that recorded evidence.

  6. *Stop:* budget exhausted, or a window-leaving input can only be fixed by a

     policy change, which is out of scope.

  **2026-10-20 continuation after user review:** the user approved an occasional

  real-download test, which now supplies HRRR data without the historical private

  snapshot. Earlier exploratory download/render checks passed, but they did not

  measure reuse. The all-points density defect is separate issue #79.

  Next bounded slice: one targeted headless check round and **one <=110 s native

  `residency-reuse` gate attempt**, with standard density. Warm one field, quiesce

  preparation and disable speculative recenter/lookahead in the harness; exercise

  two small covered pans and two within-bracket time changes. Block decoded-band

  access and measure field preparation/miss/publication deltas. Record forecast

  checksum, workload and limitations. This is a mechanism control, not a scheduler

  performance comparison or actual GPU-upload measurement. The historical

  `residency` gate stays unchanged; GPU upload/release, cross-field switching,

  eviction/reload and fidelity acceptance remain open. Stop/report after this

  attempt rather than extending the budget to implement GPU instrumentation.

  **Result/review:** 31 targeted checks passed, 2 opt-in network checks skipped

  in the headless selection; Black passed on four changed Python files and

  whitespace passed. The one permitted native attempt completed in 13.564 s:

  **6 checks passed, 3 failed**. Covered pans were ready, with no preparation or

  decoded-band access. Both time inputs instead selected a different resident

  field, causing one field publication. The harness mistakenly warmed at an exact

  hourly forecast event (singleton identity), then called +15/+30 minutes the

  same field. Those instants belong to a different interpolation bracket.

  **Decision: narrow/stop, native budget consumed.** This is a workload-design

  failure, not evidence that same-field temporal reuse fails or that GPU residency

  passes. Next smallest experiment, requiring budget review: warm strictly inside

  an interval (+1h15m), verify pre-input coverage, then stay inside that interval.

  Actual GPU upload/lifecycle acceptance remains open. The new headless eviction

  test actually forces eviction and verifies endpoint arrays (including NaNs),

  provenance and land flags on reload; it is CPU-payload evidence, not GPU eviction.

  Evidence: [residency-reuse-summary.json](../profiles/rendering/2026-10-20/residency-reuse-summary.json).

  **Blocker report:** Expected: four same-field covered inputs with no field

  publication. Observed: the pans reused the field; time changes switched to a

  different preloaded field and published it once. Root cause: the harness chose

  an exact forecast event rather than the interior of its interpolation interval.

  Next concrete step: approve one corrected-interval attempt; keep this negative

  result and do not weaken the gate. #64 remains open.

  **Budget extension, explicitly approved by the user (2026-10-20):** three

  implementation/targeted-validation rounds and four native gate attempts, each

  <=120 seconds. Fix the interval control, instrument actual Qt texture creation

  and destruction separately from image publication, exercise resource pressure,

  reload and teardown, and retain numerical/missing-data controls. Reuse a downloaded

  forecast when available and record its checksum. No chart migration, #79 fix,

  routing change or renderer rewrite. Stop on exhausted budget or if measurement

  requires a broader renderer change. Native texture creation/upload submission is

  not GPU completion, physical VRAM accounting or presentation latency. Record

  diagnostic-path differences explicitly; do not claim the stock Image-provider

  path is measured when only a replacement Texture-provider path was exercised.

  **Extension result — narrow/stop:** all three targeted rounds and all four native

  attempts are consumed. Final targeted selection: **93 passed, 2 skipped,

  1 deselected** (the known obsolete #77 import-isolation assertion); Black eight

  files and whitespace passed. The final native control passed **24/24** checks:

  four warm field upload submissions, zero field upload submissions during four

  covered inputs, nine changing-selection submissions, zero preparation/decoded-band

  access. The tracing boundary is Qt's actual `QRhiResourceUpdateBatch::uploadTexture`

  call, not image publication; it does not measure GPU completion or presentation.

  **Important control difference:** `residency-upload` explicitly disables Qt's

  atlas (`QSG_ATLAS_SIZE_LIMIT=1`) so field and selection resources are identifiable.

  It does not establish default atlas-enabled upload acceptance. The preceding

  atlas-enabled attempt failed attribution; its debugger/target monotonic clocks

  also differed, invalidating those phase counts. Both negative results are kept.

  Adopt the focused cleanup correction: source replacement/disposal now clears QML

  barbs/fill image bindings in addition to the CPU provider. Source-retirement,

  reload and teardown passed on the atlas-enabled and atlas-disabled paths; the

  final run ended with zero observed live QSG textures, zero retained field bytes

  and stopped workers. These are resource-lifetime checks, **not physical VRAM**.

  **Reevaluation:** covered-demand CPU reuse and controlled native upload behavior

  are supported. No real-workload performance improvement is claimed. The remaining

  bottleneck in acceptance is resource ownership/measurement, not more timing runs:

  `FieldStore` owns CPU payloads, whereas Qt owns texture/atlas lifetimes. A true

  native residency/eviction budget and cross-field resident-window acceptance remain

  open. Do not mark #64 complete or relax the historical `residency` gate (unchanged).

  Next decision needs a separately bounded design for atlas-region attribution and

  native field-residency ownership, not another retry of the same test.

  Evidence: [residency-extended-summary.json](../profiles/rendering/2026-10-20/residency-extended-summary.json).

- **Architecture decided:** [`adr-001-grib-rendering-architecture.md`](../adr-001-grib-rendering-architecture.md).

  Read it first. It reconciles the R0-R5 architecture in

  `docs/rendering-implementation-plan.md` with the 2026-10-19 current-state and

  reference-technique analyses, and records decisions D1-D5.

- **Implementation program:** epic #51, issues #62-#70. One issue at a time; each

  states its own gate, evidence and validation command. See

  [`docs/milestones.md`](../milestones.md) for the index.

- **Analysis evidence:** [`grib-architecture-analysis.md`](../grib-architecture-analysis.md)

  (current state, seven structural gaps) and

  [`reference-architecture-analysis.md`](../reference-architecture-analysis.md)

  (reference techniques T1-T8).

- **D2 decided 2026-10-19:** GPU spatial interpolation, bounded by the existing

  2e-5 gate. The R3 sampling invariant is amended, not relaxed — see ADR-001 §4.2

  and §6. Issue #65 is unblocked; the CPU sampler remains the reference authority

  and the fallback path.

- **Milestone:** M0 — reproducible baseline and architecture decision.

- **A0 experiment record (closed):** A0 evaluated coherent alternative interaction

  architectures, not isolated transport/worker optimizations. A0a-A0l are complete

  as experiments. The per-slice contracts, budgets, gates, negative results and

  decisions are in the [decision log](rendering-decision-log.md#decision-log) below; the narrative record is

  [`rendering-architecture-evaluation.md`](rendering-architecture-evaluation.md)

  (A0a, A0b, A0c and the A0d-A0i checkpoints), the per-slice reports

  ([A0g](rendering-a0g-progress.md), [A0h](rendering-a0h-resident-worker.md),

  [A0i](rendering-a0i-independent-numerical.md),

  [A0j](rendering-a0j-early-display.md), [A0k](rendering-a0k-shader-safety.md),

  [A0l](early-weather-startup-integration.md)) and the machine-readable summaries

  under `docs/profiles/rendering/`

  ([A0b](../profiles/rendering/2026-10-18/a0b-wholepath-summary.json),

  [A0c](../profiles/rendering/2026-10-18/a0c-timeline-scrub-summary.json),

  [A0g](../profiles/rendering/2026-10-18/a0g-unattended-summary.json),

  [A0h](../profiles/rendering/2026-10-18/a0h-resident-worker-summary.json)).

  Do not start new A0 slices from this file, and do not adopt the whole

  architecture yet: chart publication/startup, correct visual portrayal,

  scrub/layer/zoom, global memory and presentation acceptance stay open.

- **Delivered production slice:** the #50 residency work is **merged to `dev`** —

  merge commit `b9e2471`, reaching `dev` via PR #61 `1a90515` on 2026-10-19. It

  remains opt-in. Its content is commits

  [`21b40d5`](https://github.com/evanlivingston/gweatherrouting/commit/21b40d5)

  (user-authorized scope extension) and

  [`175096d`](https://github.com/evanlivingston/gweatherrouting/commit/175096d)

  (CI-review correction). See its [residency evidence](gpu-weather-residency.md).

- **Starting code:** fork `origin/dev`, `1f1464971db340d5c5581a4d6452fc61e9d7754a`.

  PR #50 was a separate experimental candidate, not this baseline.

- **Completed:** B1 render-signal instrumentation, E3a/E3b real-data residency and

  staging probes, E1/E1b native float transport/sampling probes, E2a/E2b retained

  field controls and E2c native field/selection indirection. See

  [B1 evidence](rendering-m0-native-probe.md), [residency evidence](rendering-m0-residency.md)

  [texture evidence](rendering-m0-float-textures.md),

  [retained-field evidence](rendering-m0-retained-field.md) and

  [persistent-resource evidence](rendering-m0-persistent-field.md).

- **Not completed:** statistically useful baseline, presentation measurement,

  full E2/host integration, target ratification, broad E3 coverage or demonstrated

  end-to-end performance improvement. M0 is not complete merely because these

  mechanism probes and functional activation checks passed.

- **Scope:** Qt display pipeline only. No routing changes, GTK migration,

  automatic downloads, unrelated persistence fixes, or default-on GPU change.

## Experiment contracts and results (B1, E1-E3)



These are the M0 mechanism probes. The full contract — question, scope,

non-goals, gates, budget, stop condition, result and decision — is recorded in

the per-experiment document linked from each entry. That document is the

canonical record; the entries below keep the roadmap question, the fixed

thresholds and the decision, and point to the evidence.



### E2 design review: avoid a misleading frozen-scene win



Source review of `WindRenderer.live_geometry_for_view` found that every pan

changes screen-cell thinning and exact glyph selection. Simply translating the

old whole scene can also expose clipped wind-layer edges. A frozen-scene/control

win would establish host mechanics, not production portrayal equivalence. The

existing standalone retained-scene experiment already establishes much of that

mechanism. Do not spend another prototype budget rediscovering it or silently

weaken the sample-selection/coverage gate.



**Decision: defer E2's full-scene prototype until residency/field ownership is

understood.** E2 remains unproven, and a retained preview may still be useful, but

must be distinguished from a current, correctly thinned scene. Investigate the

concrete real-forecast eligibility blocker first (E3a below). This reorders M0

experiments, not production milestones.



Canonical records: the [E2 review and next gate](rendering-m0-retained-field.md)

and the [residency review](rendering-m0-residency.md).



### E2a contract: retained field, current selection (not a frozen scene)



- **Canonical record:** [E2a result and review](rendering-m0-retained-field.md)

  holds the full question, mechanism, reference, preparation stall, workload,

  budget, success/stop conditions, result and decision.

- **Question:** can repeated weather sampling be removed from pan restaging while

  keeping the current native-node membership, screen-cell thinning, cap, anchors,

  vectors, masks and atlas bytes at each view?

- **Budget:** one implementation/targeted-validation iteration and one real-data

  process bounded to 120 seconds. No full native rendering run in this contract.

- **Decision:** retained numerical data is useful, but the worker-only cache is not

  a demonstrated pan-fluidity fix. Do not promote an isolated worker speedup as

  application performance, and do not relax the selection/coverage gate.



### E2b contract: native retained-field control, not production migration



- **Canonical record:** [E2b result](rendering-m0-retained-field.md) holds the

  full scope, workload, gates, budget, decision and outcome.

- **Question:** does E2a's mechanism improve numerical-ready render observations

  during real input, or does GUI assembly/image upload remain dominant?

- **Decision:** improved numerical readiness with unchanged view/content is

  mechanism support only. A worse GUI/input tail is a mixed result, not a

  performance win. Native texture residency and complete-frame latency remain

  separate gates, and no end-to-end speedup follows from one process pair.



### E2c contract: persistent GPU field / exact selection indirection



- **Canonical record:** [E2c result](rendering-m0-persistent-field.md) holds the

  full hypothesis, scope, fidelity reference, ownership/measurement rules, budget,

  stop condition, results table and next gate.

- **Hypothesis:** field values can remain in one GPU resource while a compact

  per-view selection references the exact current native nodes and anchors.

- **Fixed gates:** GPU interpolated vectors must match the reference sampler

  within **2e-5 m/s**; missing masks and anchors must match exactly; the reserved

  texture-payload envelope is 16 MiB including replacement overlap. None of these

  are relaxed.

- **Decision:** continue/narrow to a separately contracted full-app atomic

  handoff. The mechanism passed; adoption into the full renderer is not yet

  justified, and an atomic resource bind here is not atomic CPU/GPU handoff in

  the application.



### E2d contract: disposable full-app persistent-field / atomic-symbol control



- **Canonical record:** [E2d result](rendering-m0-app-persistent-field.md) holds

  the full hypothesis, scope, fidelity, boundary, measurement rules, budget,

  decision gate and the failed lifecycle record.

- **Hypothesis:** a published immutable wider field allows current-view selection

  without another weather job, while one symbol mode per frame prevents E2b's

  mixed CPU/numerical submissions.

- **Decision:** stop/narrow to lifecycle and nonblank/temporal fidelity. No

  full-app pass or speedup claim. The declared two-iteration, three-native-

  attempt budget is consumed; completed artifacts are diagnostic evidence, not

  successful native integration runs.



### E3a experiment contract: real-forecast decoded-band working set



- **Canonical record:** [E3a residency finding](rendering-m0-residency.md) holds

  the full question, scope, non-goals, budget, evidence list, fidelity gate,

  success/stop conditions, measurement table and review.

- **Question:** does the user's actual forecast selection require the potential

  six-bands-per-source allocation that rejects PR #50, and can a global on-demand

  retained-band budget preserve the reference sampler's answers?

- **Fixed budgets:** reference retention stops at 512 MiB; candidate retention is

  128 MiB. Both are recorded in the linked result table.

- **Decision:** continue narrowly to E3b. E3a proves the preflight estimate is

  not actual demand for this workload; it does not prove PR #50 staging fits.



### E3b contract: actual PR #50 staging, without changing the PR



- **Canonical record:** [E3b follow-up](rendering-m0-residency.md) holds the full

  question, scope, checks, budget, non-goals, success/stop conditions, results and

  decision.

- **Question:** do the actual native-node union, temporal endpoint selection, barb

  atlas and image encoding fit a 128 MiB global decoded-band budget on the saved

  view and clock?

- **Fixed gates:** decoded-band retention stays at 128 MiB, and staged values must

  match the reference sampler within **2e-5 m/s** with matching masks and

  provenance. Never skip a source to get a pass.

- **Decision:** bounded on-demand decoded-band ownership is justified for this

  saved-view staging workload. Do not increase the preflight limit; propose an

  explicit cache/ownership replacement with rejection behaviour and expanded

  coverage tests.



### E1 experiment contract: native float textures and sampling



- **Canonical record:** [E1/E1b result](rendering-m0-float-textures.md) holds the

  full question, scope, budget, fidelity requirements, evidence list, decision

  gate, attempt history and results.

- **Question:** can the installed PySide/Qt expose native float upload, retention,

  GPU sampling and readback without the PR's byte-packed image transport?

- **Fixed gate:** the **2e-5** maximum-error threshold applies to both attempts.

  Hardware linear filtering failed it (measured error 0.002604166666666824);

  explicit `texelFetch` plus manual bilinear arithmetic passed it (maximum error

  2.384185791015625e-7). The threshold is never relaxed.

- **Decision:** native float transport works through the installed PySide/Qt RHI

  bindings. A broad Metal/C++ rewrite is not justified, and production

  native-texture map integration is not proven.



### E1b contract: explicit interpolation, not a looser sampling gate



- **Canonical record:** [E1b result and budget review](rendering-m0-float-textures.md).

- **Change:** a separately identified manual sampling shader was added to the same

  generated-field probe; the hardware-linear mode and the unchanged 2e-5

  accuracy threshold were both retained.

- **Budget:** one implementation/validation iteration and one native process, at

  most 120 seconds. No shader precision/binding investigation beyond that

  attempt, no compiler/backend switch, and no target relaxation.

- **Decision:** support explicit shader sampling only — not hardware filtering,

  not production missing-corner semantics, and not map integration or speedup.



### B1 experiment contract and outcome



- **Canonical record:** [B1 probe result and review](rendering-m0-native-probe.md)

  holds the full question, scope, non-goals, budget, mechanism checks, timing

  endpoint, correctness requirements, success/stop conditions and the attempt

  outcomes.

- **Question:** can the embedded app replay timestamp input and its consequent

  rendering without forcing synchronous `_present()`, `grab()`, or framebuffer

  readback, and what endpoint can we honestly observe?

- **Budget:** at most two implementation/validation iterations and two native

  smoke attempts, each bounded to 120 seconds. No silent timeout increases and no

  full replay matrix while endpoint attribution is unresolved.

- **Decision:** adopt for bounded mechanism experiments only. The endpoint is the

  offscreen embedded scene's Qt render signal, never GPU completion, top-level

  presentation or scanout. True presentation and usable-data coverage remain

  unmeasured.



## Architecture recommendation after mechanism probes



These five recommendations were the input to

[ADR-001](../adr-001-grib-rendering-architecture.md). **ADR-001 wins where they

differ:** D1 (GPU-resident numerical field), D2 (GPU spatial interpolation,

bounded by the 2e-5 gate), D3 (frame N/N+1 overlap), D4 (resident time windows)

and D5 (GPU decode deferred) are the ratified decisions. Read ADR-001 for the

decisions and this section for the reasoning that produced them.



1. **Do not rewrite the app or switch to a macOS-only Metal backend.** Native

   numerical textures and a custom pipeline work through the installed Qt/PySide

   RHI bindings. That removes the assumption that a broad native rewrite is

   necessary; it does not prove our existing Qt Quick material path exposes all

   required hooks.

2. **Fix bounded data ownership before optimizing transport.** The actual #50

   staging fits comfortably on the saved view while the potential-cache estimate

   rejects it. A worker-private, global decoded-band budget with explicit resource

   rejection is the first justified production change. Keep every selected source

   and current selection semantics. Do not raise/remove the guard without its

   bounded replacement.

3. **Keep explicit shader interpolation for the fidelity-sensitive path.** Hardware

   linear filtering did not pass the declared numerical gate. Native float

   transport remains an option, not permission to change spatial/mask semantics.

4. **Do not prioritize a native scene-graph bridge yet.** The installed binding

   exposes QQuickWindow float-texture wrapping and QRhiWidget custom pipelines,

   but not QSGMaterialShader.updateSampledImage. A Python texture-provider path or

   thin C++ bridge may be needed for the existing host; neither is validated.

   The measured barb atlas is only 42 KB, so eliminating byte packing there is

   not established as a useful first optimization.

5. **Retained numerical fill is still the next major renderer layer**, after

   residency and host integration are proven. Separate charts/weather revisions;

   do not claim a barb-only fix makes the complete scene fluid.



The user explicitly approved extending #50 with the narrow residency follow-up

on October 18. It was implemented and pushed as `21b40d5`; the broader probes,

plan and architecture work remain in this separate worktree. No additional #50

scope expansion or merge is authorized by that choice.



States: unproven → experimenting → supported / rejected / inconclusive. Track

implementation and production validation separately from hypothesis status.

Preserve rejected approaches and reasons. E1–E3 budgets must be entered before

starting those experiments, after B1 identifies a usable measurement protocol.
