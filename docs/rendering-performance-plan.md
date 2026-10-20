# Rendering performance: canonical plan

Tracking epic: [#51](https://github.com/evanlivingston/gweatherrouting/issues/51).
This is the current roadmap and decision ledger. Earlier implementation plans
and reports remain historical specifications/evidence, not competing task lists.

## How to read this document

This file does three jobs, and each has one home:

| What you want | Read |
| --- | --- |
| The canonical roadmap, current status and decision ledger | this file, starting at [Current checkpoint](#current-checkpoint) |
| The architecture decision, and the work program that implements it | [ADR-001](adr-001-grib-rendering-architecture.md), then issues [#62–#70](milestones.md) |
| The A0a–A0l experiment record, including the negative results | [`rendering-architecture-evaluation.md`](history/rendering-architecture-evaluation.md), the per-slice reports, and the machine-readable summaries under `docs/profiles/rendering/` |
| The M0 mechanism-probe records (B1, E1-E3) | the per-experiment records linked from [Experiment contracts and results](#experiment-contracts-and-results-b1-e1-e3) |
| The R0–R5 implementation detail, invariants and measurement contract | [`rendering-implementation-plan.md`](rendering-implementation-plan.md) |

Nothing is deleted evidence: every decision, threshold, negative result and
budget stays either in this file or behind a link to the document that records it.
Where a section below is a pointer, the linked document is the canonical record
for that experiment; this plan keeps the roadmap entry and the decision.

For the wider documentation map, see [the documentation index](README.md).

## Current checkpoint

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
  [viewport-continuous-summary.json](profiles/rendering/2026-10-19/viewport-continuous-summary.json).
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
  Evidence: [`viewport-tryout-summary.json`](profiles/rendering/2026-10-19/viewport-tryout-summary.json).
- **Architecture decided:** [`adr-001-grib-rendering-architecture.md`](adr-001-grib-rendering-architecture.md).
  Read it first. It reconciles the R0-R5 architecture in
  `docs/rendering-implementation-plan.md` with the 2026-10-19 current-state and
  reference-technique analyses, and records decisions D1-D5.
- **Implementation program:** epic #51, issues #62-#70. One issue at a time; each
  states its own gate, evidence and validation command. See
  [`docs/milestones.md`](milestones.md) for the index.
- **Analysis evidence:** [`grib-architecture-analysis.md`](grib-architecture-analysis.md)
  (current state, seven structural gaps) and
  [`reference-architecture-analysis.md`](reference-architecture-analysis.md)
  (reference techniques T1-T8).
- **D2 decided 2026-10-19:** GPU spatial interpolation, bounded by the existing
  2e-5 gate. The R3 sampling invariant is amended, not relaxed — see ADR-001 §4.2
  and §6. Issue #65 is unblocked; the CPU sampler remains the reference authority
  and the fallback path.
- **Milestone:** M0 — reproducible baseline and architecture decision.
- **A0 experiment record (closed):** A0 evaluated coherent alternative interaction
  architectures, not isolated transport/worker optimizations. A0a-A0l are complete
  as experiments. The per-slice contracts, budgets, gates, negative results and
  decisions are in the [decision log](#decision-log) below; the narrative record is
  [`rendering-architecture-evaluation.md`](history/rendering-architecture-evaluation.md)
  (A0a, A0b, A0c and the A0d-A0i checkpoints), the per-slice reports
  ([A0g](history/rendering-a0g-progress.md), [A0h](history/rendering-a0h-resident-worker.md),
  [A0i](history/rendering-a0i-independent-numerical.md),
  [A0j](history/rendering-a0j-early-display.md), [A0k](history/rendering-a0k-shader-safety.md),
  [A0l](history/early-weather-startup-integration.md)) and the machine-readable summaries
  under `docs/profiles/rendering/`
  ([A0b](profiles/rendering/2026-10-18/a0b-wholepath-summary.json),
  [A0c](profiles/rendering/2026-10-18/a0c-timeline-scrub-summary.json),
  [A0g](profiles/rendering/2026-10-18/a0g-unattended-summary.json),
  [A0h](profiles/rendering/2026-10-18/a0h-resident-worker-summary.json)).
  Do not start new A0 slices from this file, and do not adopt the whole
  architecture yet: chart publication/startup, correct visual portrayal,
  scrub/layer/zoom, global memory and presentation acceptance stay open.
- **Delivered production slice:** the #50 residency work is **merged to `dev`** —
  merge commit `b9e2471`, reaching `dev` via PR #61 `1a90515` on 2026-10-19. It
  remains opt-in. Its content is commits
  [`21b40d5`](https://github.com/evanlivingston/gweatherrouting/commit/21b40d5)
  (user-authorized scope extension) and
  [`175096d`](https://github.com/evanlivingston/gweatherrouting/commit/175096d)
  (CI-review correction). See its [residency evidence](history/gpu-weather-residency.md).
- **Starting code:** fork `origin/dev`, `1f1464971db340d5c5581a4d6452fc61e9d7754a`.
  PR #50 was a separate experimental candidate, not this baseline.
- **Completed:** B1 render-signal instrumentation, E3a/E3b real-data residency and
  staging probes, E1/E1b native float transport/sampling probes, E2a/E2b retained
  field controls and E2c native field/selection indirection. See
  [B1 evidence](history/rendering-m0-native-probe.md), [residency evidence](history/rendering-m0-residency.md)
  [texture evidence](history/rendering-m0-float-textures.md),
  [retained-field evidence](history/rendering-m0-retained-field.md) and
  [persistent-resource evidence](history/rendering-m0-persistent-field.md).
- **Not completed:** statistically useful baseline, presentation measurement,
  full E2/host integration, target ratification, broad E3 coverage or demonstrated
  end-to-end performance improvement. M0 is not complete merely because these
  mechanism probes and functional activation checks passed.
- **Scope:** Qt display pipeline only. No routing changes, GTK migration,
  automatic downloads, unrelated persistence fixes, or default-on GPU change.

## Reference workload and acceptance

Reuse a verified private startup snapshot, including its source ordering,
checksums, frozen forecast clock, saved settings/layout, density, and layers.
The [October 15 report](rendering-startup-baseline-2026-10-15.md) is historical
evidence, not a current baseline. B1 located and verified the existing October 16
snapshot referenced by [continuous-pan work](history/rendering-pan-prediction.md). Its
content hash matches the historical snapshot, but its frozen clock is October 16
08:00; clock and content identity must both match for comparison. If unavailable
or no longer representative, record the blocker and version any replacement;
never silently substitute a new workload.

Replay pan/zoom/reversal and forecast scrubs within/across intervals, charts
alone and with fill/barbs/contours/streamlines, then both maps visible. The
existing snapshot has no marine fields according to the October 16 report;
marine-heavy acceptance needs a separately identified fixture.

Record input dispatch, GUI work, rendering/submission callbacks, cadence,
preparation/publication delay, uploads, memory, backend/fallback, and usable-data
coverage. Distinguish warm interaction, initial load, new coverage, and interval
crossings. A prepared image enclosing the viewport is not proof of usable
forecast coverage. Missing content must not count as a speed improvement.

Proposed targets (pending explicit ratification before acceptance):

- warm p95 frame time within the reference display refresh budget;
- p95 input-to-present below 50 ms, using a defensible presentation endpoint;
- camera movement remains responsive while preparation is deliberately stalled;
- bounded memory with a measured plateau under repeated travel/time/source changes;
- separate numerical limits for cold load, settling, memory, and coverage gaps,
  to be chosen after the baseline, not retrofitted to candidate results.

Qt render/swap callbacks are proxy endpoints, **not** scanout/input-to-photon
proof. For a QQuickWidget, its offscreen scene rendering alone does not establish
the top-level widget's presentation. Label each endpoint precisely; leave true
presentation acceptance unmeasured if the available instrumentation cannot prove
it. Do not rename request-to-render or grab duration as input-to-present.

All numerical/source-selection and portrayal invariants in the historical
[implementation specification](rendering-implementation-plan.md#reference-and-invariants)
remain applicable. Intentional display approximations need documented bounds
and explicit review. Routing remains unchanged.

## Milestones

| Milestone | Exit gate | Status |
| --- | --- | --- |
| M0 Baseline + architecture decision | Reproducible real-workload evidence; E1–E3 decisions; selected host/ownership/residency design | In progress |
| M1 Bounded residency | Real forecasts work inside an explicit budget with sampler/provenance equivalence and resource release | Provisional |
| M2 Production fill + barbs | Retained numerical layers, cheap camera/time changes, accurate loading state, matched fidelity/performance gates | Provisional |
| M3 Remaining bottlenecks | Reprofile, then retain contours/streamlines and address charts/navigation only as justified | Provisional |
| M4 Validation + rollout | Soak, lifecycle/DPR/context/backend coverage and explicit approval for rollout | Provisional |

Do not interpret a later milestone as an instruction to implement it before its
predecessor passes. A negative experiment can close an uncertainty successfully.

## Hypothesis ledger

| ID | Hypothesis | Evidence / decision | State |
| --- | --- | --- | --- |
| B0 | Existing startup tool already measures native input-to-present | Rejected by source audit: every measured step forces grab/readback; retain for synchronous diagnostics only | Rejected |
| B1 | Embedded native replay can expose useful asynchronous timing without readback | Two native runs and deterministic attribution checks; usable only as a render-signal proxy, not presentation | Supported (limited) |
| E1 | Native numerical textures are suitable for production fields | Float transport and explicit interpolation passed in native QRhiWidget; hardware filtering failed original gate; map integration/lifecycle open | Supported mechanism only |
| E2 | Retained camera/time state removes preparation from warm interaction | Exact retained selection and native GPU indirection pass; worker-only app control did not demonstrate latency win; full-app persistent-resource/atomic handoff test next | Supported mechanism; interaction unproven |
| E3 | Actual forecast selection fits bounded on-demand residency | Saved-view lattice and actual #50 staging fit; values/masks/provenance checked; broader coverage/eviction/soak open | Supported saved-view slice |
| A0 | A coherent decoupled renderer improves whole map/weather interaction | A0g coverage/freshness correctness passes. A0h retained numerical owner and clean timing pass two matched pairs with lower preparation cost and exact deferred fidelity. Empty-only oracle correction documented. Legacy chart/startup, broader workloads, memory and presentation remain open. | Numerical ownership/measurement supported; whole-path architecture acceptance open |

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

Canonical records: the [E2 review and next gate](history/rendering-m0-retained-field.md)
and the [residency review](history/rendering-m0-residency.md).

### E2a contract: retained field, current selection (not a frozen scene)

- **Canonical record:** [E2a result and review](history/rendering-m0-retained-field.md)
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

- **Canonical record:** [E2b result](history/rendering-m0-retained-field.md) holds the
  full scope, workload, gates, budget, decision and outcome.
- **Question:** does E2a's mechanism improve numerical-ready render observations
  during real input, or does GUI assembly/image upload remain dominant?
- **Decision:** improved numerical readiness with unchanged view/content is
  mechanism support only. A worse GUI/input tail is a mixed result, not a
  performance win. Native texture residency and complete-frame latency remain
  separate gates, and no end-to-end speedup follows from one process pair.

### E2c contract: persistent GPU field / exact selection indirection

- **Canonical record:** [E2c result](history/rendering-m0-persistent-field.md) holds the
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

- **Canonical record:** [E2d result](history/rendering-m0-app-persistent-field.md) holds
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

- **Canonical record:** [E3a residency finding](history/rendering-m0-residency.md) holds
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

- **Canonical record:** [E3b follow-up](history/rendering-m0-residency.md) holds the full
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

- **Canonical record:** [E1/E1b result](history/rendering-m0-float-textures.md) holds the
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

- **Canonical record:** [E1b result and budget review](history/rendering-m0-float-textures.md).
- **Change:** a separately identified manual sampling shader was added to the same
  generated-field probe; the hardware-linear mode and the unchanged 2e-5
  accuracy threshold were both retained.
- **Budget:** one implementation/validation iteration and one native process, at
  most 120 seconds. No shader precision/binding investigation beyond that
  attempt, no compiler/backend switch, and no target relaxation.
- **Decision:** support explicit shader sampling only — not hardware filtering,
  not production missing-corner semantics, and not map integration or speedup.

### B1 experiment contract and outcome

- **Canonical record:** [B1 probe result and review](history/rendering-m0-native-probe.md)
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
[ADR-001](adr-001-grib-rendering-architecture.md). **ADR-001 wins where they
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

## Evidence and comparison rules

- Every result records code commit/dirty diff identity, harness identity, snapshot
  identity, backend, hardware/display/DPR, environment, commands, raw samples,
  repetitions, warm-up, failures and limitations. Keep private paths/data local.
- Compare matched workloads and instrumentation; use interleaved baseline and
  candidate runs where practical. Profile runs are attribution, not timing runs.
- Version changed measurement semantics. Historical grab p99 is not native FPS.
- A synthetic mechanism pass cannot replace real-forecast acceptance. Tests prove
  correctness, measurements prove performance, and coverage checks prove neither
  was achieved by losing content.
- Resource estimates and RSS high-water are not measured GPU residency or a
  memory plateau. Windowed GDAL reads may still decode full bands: measure it.
- Use architectural observations from supplied decompilation only as hypotheses;
  independently implement techniques, not recovered proprietary code.

## Working/review rules

Each slice identifies its milestone and hypothesis, then records question,
scope/non-goals, baseline/protocol/success criterion, fidelity gates, budget/stop
condition, and result/decision. Each PR summarizes:
**milestone → hypothesis → evidence → decision → remaining gap**.

After each experiment/milestone or exhausted budget, record:

1. Did the actual workload improve, or did we only establish a mechanism?
2. Is the original bottleneck still dominant?
3. What uncertainty remains, and is the next step still justified?
4. **Continue / narrow / pivot / stop**, with rationale and any new budget.

No silent scope expansion or moving acceptance criteria to match results. Keep
one current milestone and one next decision here and on the epic. Link detailed
results/child issues instead of proliferating roadmaps. Existing uncommitted work
and open PRs retain their own scope; this epic does not authorize their overwrite
or merge. Ask before pushing/creating PRs when not already authorized.

## Decision log

The A0 slice contracts, budgets, gates, negative results and per-slice decisions,
in reverse chronological order. This log is the canonical record of the A0
program; the narrative version of the same evidence is
[`rendering-architecture-evaluation.md`](history/rendering-architecture-evaluation.md),
with the per-slice reports and the machine-readable summaries under
`docs/profiles/rendering/` alongside it. Do not start new A0 slices from here,
and do not read any single entry as a current work item — the live work program
is issues [#62–#70](milestones.md).

### A0l — opt-in application startup integration

- User authorized integrating early weather into the real application, behind an
  opt-in flag. Existing startup stays default; no routing calculations, forecast
  selection, PR #50 edits, downloads, commits/pushes/merges or rollout by default.
- Hypothesis: separating blocking model/chart initialization from creation of the
  actual Qt application window can publish current weather before chart readiness
  without replacing its map/camera or sharing worker GDAL resources with the GUI.
  First measure Core/forecast and chart/window initialization separately; the
  earlier chart-host readiness includes all those phases and is not chart-only.
- Scope: application entry/controller/window lifecycle, independent preparation
  and explicit ready/loading/error states; source/time/view generations, shutdown,
  handoff. Preserve complete saved portrayal and source order or show unsupported
  state/fall back explicitly; never silently select a faster subset.
- Baseline: existing approved `1ed1bca1...` fixture, 22 sources, saved current style
  and layout, frozen October 19 08:00 for validation. No old/new fixture comparison.
- Gates: default startup unaffected; opt-in creates the actual usable Qt window,
  wind before chart readiness, no GUI initialization stall hidden behind a splash;
  exact weather/selection with explicit chart loading, camera/clock preservation,
  safe failure/close/supersession and private resource ownership. Record endpoint
  definitions and initialization phases; never label readback or timer FPS as
  presentation, nor an application shell as completed main-map integration.
- Budget: two initialization diagnostics <=120 s, three implementation/check
  iterations and three actual-app workload attempts <=120 s, 60 minutes wall time.
  Stop and report if safe integration requires changing routing/portrayal policy,
  unsupported dependencies, unsafe resource transfer or a larger subsystem rewrite.
  Reevaluate bottleneck and adopt/revise/reject before claiming completion.

### A0k — bounded nonempty/empty GPU overlay diagnosis and repair

- User authorized fixing A0j's inherited red flooding. Current milestone remains
  M0/A0: establish correct portrayal before further startup integration.
- Hypothesis: missing node/endpoint NaNs crossing shader varyings are not safely
  rejected by the Metal path; isolate finite, all-missing and mixed cases first.
  Do not assume root cause from screenshots or use the bad GPU oracle as truth.
- Scope: experiment-only shader transformation/diagnostic, independent CPU barb
  geometry/transparency checks, then corrected early-display handoff. Preserve
  numeric staging, source order, routing, historical readbacks and PR #50.
  No production rollout, downloads, new fixtures or global performance claims.
- Workload: existing verified current fixture `1ed1bca1...` and A0j fresh field;
  synthetic isolated finite/missing/mixed glyph cases over transparent background.
- Gates: all-invalid layers contribute exactly zero pixels; finite control glyphs
  stay unchanged; mixed real glyph support/colors/orientation match independent
  CPU geometry within explicitly measured rasterization tolerance, no red flood;
  new early worker data remains numerical-reference-exact and precedes chart
  readiness, late charts preserve weather/camera. Pixel/readback timings are not
  presentation; old original-GPU equality is superseded ONLY with evidence.
- Budget: three implementation/check iterations, three native shader-diagnostic
  attempts <=120 s and two full early-display attempts <=120 s (90 s producers),
  45 minutes wall time. Stop on unbounded portrayal differences or scope growth.
  Record adopt/revise/reject and continue/narrow/pivot/stop at the gate.
- Shader diagnostic attempts 1/2 stopped at the second-window all-missing case:
  cached ShaderEffect `Compiled` readiness was not re-emitted despite all images
  ready. Only finite control was captured (563 GPU vs 557 CPU significant ink
  pixels; zero outside reciprocal 3-physical-pixel support). Revise the diagnostic
  endpoint to native readback after image readiness, enforce shader errors and
  positive/CPU-support controls rather than treating cached status as proof.
  Preserve both rejected attempts; the missing-data hypothesis remains unproved.
- Planned repair avoids NaN varyings using integer IEEE exponent validation,
  finite invalid-cell outputs and explicit validity discard. Opt-in shader
  transformation only; finite numerical interpolation/portrayal math unchanged.
- Diagnostic 3 writes all 14 original/corrected case results with no gate
  failures. The runner's shell wrapper then misreports a zsh read-only `status`
  variable error as pre-execution failure; an authorized attempted relaunch
  refuses the existing directory before any rendering. Preserve these facts:
  complete results/readbacks exist, but the successful diagnostic's process exit
  was not preserved by the wrapper. No fourth native diagnostic is authorized.
- Recorded results: original all-missing layers paint 65,536 pixels; corrected
  layers have exactly zero alpha pixels. Finite control is byte-identical.
  Real original paints 2,126,929 significant pixels outside CPU support;
  corrected real has zero outside support in either direction, within the
  declared 3-physical-pixel (1.5 logical at DPR 2) AA/geometry tolerance.
- Add independent palette check on preserved transparent readbacks: opaque GPU
  ink must match CPU ink within the same 3-pixel neighborhood and <=8/255 per
  RGB channel (CPU alpha >64, GPU alpha >127). This is an explicit raster/color
  tolerance, not permission to change weather vectors, selection or buckets.
  Full early-display safe mode checks CPU support, not the known-bad original
  GPU oracle. Preserve original behavior as default for historical experiments.
- Corrected full-display attempt 1 passes: fresh numerical arrays/selection remain
  reference-exact, early readback ends 2.730 s, charts ready 28.387 s, bind 31.882 s;
  exclude 3.530 s preflight. Weather-only pixels stay unchanged before chart bind;
  four covered pans return exactly; independent CPU support passes both early
  and final composition with zero out-of-tolerance pixels. Both children exit 0.
  Read-only palette checks on seven corrected transparent cases also have zero
  mismatches. This does not mean Cairo/GPU pixels are identical: AA tolerance
  remains 3 physical pixels / 8 RGB levels under the documented ink thresholds.
- **Gate decision: adopt the focused repair provisionally in the experimental
  renderer; stop this bounded slice.** Missing-data NaNs are not safely excluded
  by the original shader on this Metal path. Integer exponent checks, finite
  missing-cell outputs and explicit validity discard eliminate the measured
  flood without changing finite interpolation, native selection or colors.
  Precise compiler/driver causality remains unproved; no Qt/Metal rewrite is
  indicated. Keep `--safe-barbs` opt-in, preserve old GPU assets and production/#50.
  One of two full-display slots used; no fourth diagnostic or unused full repeat.
  Reevaluate next work around application integration/chart startup, not more
  transport optimization or global performance claims. Evidence:
  [A0k correction](history/rendering-a0k-shader-safety.md).
- Final A0k targeted validation: 43 tests/38 subtests, Black on five changed Python
  files, whitespace/JSON, 44 local links and raw-evidence consistency pass. Seven
  corrected transparent cases pass CPU support/palette gates; the fresh-display
  gate and both clean child exits are rechecked without another native launch.

### A0j — early wind publication while chart host starts

- Hypothesis: the A0i headless result can drive the independent Qt viewport before
  chart readiness; late chart publication need not recreate the viewport or wind.
  Continue existing M0/A0 work in this dirty experiment worktree; preserve all
  earlier work and keep production, routing and PR #50 untouched.
- Scope: experiment-only live QQuickWidget, fresh numerical worker and concurrent
  chart host, immutable off-GUI decode/encoding, explicit wind/charts/other-layer
  loading states, covered drag/pan and late chart composition. No startup-cache
  substitution, full-app rollout, zoom/scrub, new-coverage loading or routing.
- Baseline: verified current fixture `1ed1bca1...`, 22 ordered sources, October 19
  08:00 clock, 902×735 logical viewport, very-dense barbs/hide over land; use the
  fresh A0i reference solely for matched scalar setup and deferred validation,
  never its cached numerical/chart arrays as early display input.
- Gates: real nonblank weather readback before chart-ready marker, matching exact
  headless/reference arrays/land masks/nodes/anchors; covered A→B→A input while
  charts load; no camera/clock/weather replacement on chart bind; final scene
  pixels equal concurrent chart oracle; both workers exit cleanly; bounded
  64 MiB payloads and 80 s controller deadline. Heartbeat/render callbacks and
  readbacks are proxies, NOT presentation FPS. No global memory plateau claim.
- Budget: three implementation/targeted-check iterations, three real-workload
  attempts <=120 s each with 90 s producer limits, 60 minutes wall time. Stop
  on unresolved correctness/startup-order failure or scope expansion. At the gate
  record adopt/revise/reject and continue/narrow/pivot/stop before more work.
- Attempt 1 rejected: repeated geographic round trips introduce ~1e-14 degree
  camera drift, violating exact return. Revise this prototype to retain an
  absolute screen displacement from the camera origin, including exact zero.
  Numerical child exited 0; chart startup was deliberately cancelled on failure
  (exit -15), not a successful run. Inspecting the preserved early and reference
  readbacks also shows inherited red GPU overlay flooding, even with nonempty
  wind. Finish the handoff mechanism gate but do NOT equate reference-pixel
  equality with usable visual portrayal; shader repair is a separate scope.
- Attempt 2 passes the publication MECHANISM: fresh wind readback ends 2.449 s,
  chart host ready 28.538 s, charts bind 32.037 s; 2.498 s preflight excluded.
  The extra chart-export stage contains readbacks and 25 numerical oracle checks,
  so chart-bind latency is not a clean production chart-publication measurement.
  Covered four-step pan/return preserves exact camera, weather-only pixels stay
  equal before chart bind, final pixels equal concurrent oracle, both children
  exit 0. GUI heartbeat max 301 ms includes explicit readback/comparison/save
  overhead: no smoothness or presentation acceptance.
- **Gate decision: stop/pivot at visual fidelity.** Adopt only the experiment's
  independent publication mechanism provisionally. Reject visual acceptance:
  nonempty reference and early/final GPU outputs have red flooding. No broad
  rollout, new milestone or production/#50 fix here. Next work needs a bounded
  portrayal diagnosis/repair with an independent CPU/transparent-background
  oracle, rather than preserving this flawed GPU reference. Two of three native
  attempt slots used; do not consume the third just to repeat a known-bad view.
  Evidence: [A0j report](history/rendering-a0j-early-display.md).
- Final targeted validation: 37 tests/38 subtests, Black on two changed Python
  files, whitespace/JSON and 39 local links pass. Raw-evidence checks confirm
  process exits, early-readback ordering, pan/return, exact final pixels and the
  visual blocker (red-dominant pixels exceed half of both early and original
  reference images). No third native attempt or full suite.

### A0i rebaseline — user-authorized fresh current workload

- Outcome: independently verified 5,693-file fixture, hash `1ed1bca1...`, frozen
  clock October 19 08:00. One reference-export attempt and one concurrent startup
  attempt succeeded; unused attempt slots are not consumed by default. References
  validate 12 times × 25 pan positions with the actual hide-over-land setting.
  Headless first field matches endpoints, masks, native nodes, packed arrays,
  immutable land decisions and selected anchors. Numerical open/task/close share
  one owner thread, no Qt application/window, both processes exit 0.
- **Gate decision: continue narrowly to a separately scoped display-publication
  experiment, not architecture adoption.** Weather field readiness no longer
  waits for chart startup in this slice. Chart-host startup remains the measured
  long pole, and the earlier weather result is NOT yet visible in the live map.
  No production/PR #50 change, no cross-fixture performance comparison.

- User explicitly chose regeneration over exact recovery of old cache metadata.
  Capture current application data into a NEW fixture; preserve the old manifest,
  partial restoration and historical results. No old/new timing speedup claims.
- Current saved selection has 22 forecasts, very-dense barbs and wind-over-land
  disabled. Preserve those settings/source order and the newly captured clock;
  do not silently force the old 33-source/over-land functional workload.
- Scope: fresh independent reference exports and the headless/concurrent-chart
  startup check. Extend experiment-only retained selection with an immutable
  native-point land mask, computed by the existing worker land policy, so the
  actual saved hide-over-land portrayal remains exact and no land/GDAL handle
  crosses to GUI. No routing or production/#50 edits, downloads or rollout.
- Gates: new fixture verifies independently; current source order/style recorded;
  exact masks/native nodes/endpoints/anchors/counts vs reference at 25 positions,
  immutable land flags and missing-coordinate rejection. Headless fields must
  match newly captured references, with no QApplication/window in the service;
  first field available before concurrent chart readiness. Not full-map startup
  or early-display integration acceptance.
- Budget: three implementation/check iterations, two reference-export attempts,
  two numerical/concurrent-chart attempts, each <=120 s (producer lifetime 90 s),
  60 minutes wall time. Stop on unresolved fidelity or resource disagreement.
  Record new fixture identity before comparisons; no cache reconstruction required.

### Restore original fixture — authorized recovery, no new baseline

- User authorized restoring the original fixture. Scope is missing files only,
  from local copies matching each original manifest byte count and SHA-256.
  Preserve the manifest, frozen clock/source order, existing snapshot files and
  live application data. No downloads, fallback sources or replacement baseline.
- First candidate source is the live application-data directory, which still has
  managed GRIBs/charts. Plan recovery read-only before copying; reject changed
  existing targets, path escapes/symlinks, unexpected files or incomplete recovery.
  Copy only with exclusive destination creation and verify the complete original
  inventory/hash after restoration. Never hardlink to live inputs.
- Budget: two utility/test iterations, two restore attempts, 45 minutes. Stop if
  exact copies cannot be found; report missing paths rather than altering the
  manifest. Once checksum 97cfda3c... is restored, resume A0i within its existing
  unconsumed workload budget, not an automatically expanded performance experiment.
- Recovery planning found 4,006 exact copies in live application data but 30
  marine/source-cache JSON sidecars are unavailable there and in the checked
  older snapshots. Narrow recovery to explicitly partial restoration of those
  exact available files: preserve the original manifest, exit incomplete, retain
  the full benchmark checksum gate, and report the remaining 30 paths. This
  remains recovery of original bytes, NOT acceptance of a replacement fixture.
- Recovery outcome: copied 4,006 individually size/hash-verified original files;
  preserved 533 existing original files and the manifest, for 4,539 verified files
  present. No downloads, source modifications or hardlinks. The remaining 30
  marine/source-cache JSON sidecars were not found in the checked sources. Current
  partial inventory hash is 14507922..., not the original 97cfda3c.... Restore
  exits 1 intentionally with `complete=false`; full benchmark gate remains strict.
  Five utility tests/five subtests and Black on two Python files pass.
- Final read-only verification confirms 4,539 present files match the original
  4,569-entry manifest exactly, with 30 missing metadata sidecars and no changed
  or added files. Earlier prose summarized the missing-path list as 29; that was
  a counting error, corrected from the actual list. Recovery reports themselves
  retain the full original list; future utility reports include an integer count.
- **Decision: partial recovery, still blocked.** Do not fabricate the sidecars or
  silently drop them from the manifest. Need exact backup copies or explicit
  authorization for a new separately identified fixture. A0i workload process
  budget remains unconsumed because none was launched. Evidence:
  [restoration summary](profiles/rendering/2026-10-19/fixture-restoration-summary.json).

### A0i contract — numerical startup independent of the chart application

- Continue M0/A0: remove the legacy application's startup dependency from the
  numerical producer, not just source reopening during requests. Keep all source
  ordering, exact clock/native-node/mask/value/selection behavior and budgets.
- Scope: experiment-only headless numerical service using the real retained
  worker and saved managed forecast list, with no QApplication, window, routing
  manager or chart initialization in that service. The chart process remains
  separate and is launched concurrently to test actual readiness independence.
  This slice does not claim full-map startup or early numerical display integration
  is solved merely because numerical resources become available first.
- Workload: same verified derived fixture; initial and +576/+1152-pixel views
  plus a return, matched against A0h's independently validated assets. Parse the
  saved source list in order, fail any missing source, retain 128 MiB decoded-band
  budget. Only immutable numerical arrays/metadata leave the worker.
- Gates: cold-ready and request/publication timestamps independent of chart-ready,
  exact field/coordinate dtypes, endpoint values/NaN masks, provenance generation,
  native selection/count/anchor equivalence against the saved A0h references;
  ownership/open/close and clean process exits. Reference-empty remains explicit;
  no approximation, hidden source change or claim of presentation FPS.
- Budget: three implementation/check iterations, four numerical workload process
  attempts and at most two concurrent native chart-host attempts, each <=90 s,
  45 minutes wall time. Stop on unresolved fidelity mismatch or exhausted budget.
  Record early-field availability separately from full interactive/map readiness.
- Decision: if headless startup and exact resources pass, wire that service into
  independent numerical/chart publication in a separately bounded integration
  slice; do not claim the reference application equivalence or production rollout from startup alone.
- Outcome: first attempted workload rejected snapshot identity before sandbox or
  process launch. Read-only inventory identifies 4,036 missing files, no changed
  or added files; expected hash 97cfda3c... vs observed 20d8f92c.... GRIB directory
  is empty. No source was skipped and no cached numerical output substituted for
  actual inputs. An inventory diagnostic's large output was truncated by the
  tool; a later bounded parse of the retained JSON confirmed counts/hash changes.
- **Decision: stop at unavailable-artifact gate.** Headless implementation is a
  draft, not a validated startup or early-display result. Restore original inputs
  or obtain explicit approval for a new, separately identified fixture/oracle.
  Focused source/protocol tests and a no-worker-on-invalid-fixture regression are
  included; 42 tests/39 subtests, Black on two Python files, whitespace/JSON and
  42 local links pass. Evidence: [A0i checkpoint](history/rendering-a0i-independent-numerical.md),
  [summary](profiles/rendering/2026-10-19/a0i-independent-startup-summary.json).

### A0h contract — resident data worker, deferred fidelity, matched timing

- User authorized the next performance slice. Active milestone/hypothesis remains
  M0/A0: retain numerical source resources on one owning thread, separate oracle
  validation/readbacks from measured input, and attribute remaining GUI tails.
- Scope: experiment-only real numerical preparation worker with 128 MiB decoded
  residency, same frozen source ordering and exact field/selection semantics.
  Legacy app remains a chart producer and out-of-window oracle in an isolated
  process; this does not replace all chart work or solve its startup cost. No
  routing/shared sampling edits, #50 expansion, downloads or rollout.
- Matched controls: same fixture/DPR/view/clock/layers and fixed 32-input partial
  pan trajectory, two new-area loads and A→B→A replacement. Compare a worker that
  reopens private sources per request against the same worker retaining sources.
  Both use the same no-oracle/no-readback measured protocol. Record source opens,
  worker thread identity/close, startup, per-request work, GUI callbacks/binds and
  tick/render proxy endpoints separately. Never call them presentation FPS.
- Fidelity: after measured input stops, run all exact numerical checks and
  reference-scene readbacks separately for the unchanged prepared resources.
  Nonempty scene pixels exact; preserve the prior documented proven-empty-layer
  correction and explicit host badge. Check original timed asset hashes/arrays
  against later oracle exports, stale-generation rejection, fixed input identity
  across controls and no GUI-thread GDAL. Immutable data only leaves data worker.
- Measurement gates: no grab/framebuffer readback or 25-view oracle invocation in
  either measured window; timed publications are provisional until post-validation
  passes. Initial/post checks and oracle export costs are separate. No speedup
  acceptance merely because checks moved outside measurement or data is absent.
- Budget: six implementation/targeted-check iterations; up to six native
  controllers with one native producer each (twelve native processes), 120 s per
  controller, 90 s producer lifetime, 75 min wall time. Plan one matched pair and
  one repeated pair; two diagnostic slots. Stop on unresolved fidelity mismatch,
  unavailable artifacts or exhaustion. Record decision before any scope change.
- Gate decision: matched real-workload evidence, synthetic ownership correctness
  and architecture adoption stay separate. Do not start later milestones or claim
  whole-application/the reference application performance from numerical worker timing alone.
- Iteration 1 / matched pair 1: 37 focused tests/30 subtests and Black on seven
  changed Python files passed. Reopen and resident controllers/producers exited 0;
  strict pair gate accepted identical fixed pan and A→B→A states, unchanged timed
  assets vs deferred oracle, exact numerical/dtype/provenance metadata, scene
  pixels, owner-thread lifecycle and zero guarded timed readbacks. Timed exports
  perform zero oracle checks; four exports receive 25-position checks each after
  all measured input loops stop. The host is still the legacy chart/oracle app,
  with ~31 s cold startup recorded separately.
- Pair 1: reopen source opens=8 vs resident=1 (four timed jobs + four validation
  jobs). Measured new-area preparation 1.963/1.795 s vs 1.254/1.184 s. Resident
  GUI publication 3.58/3.24 ms and heartbeat max 38.97/40.25 ms; p95 17.58/18.34
  ms. This is not presentation FPS and cannot be directly compared as a speedup
  against A0g's oracle-contaminated timing. Continue to an unchanged-harness pair
  in reversed candidate/control order within the remaining budget.
- Pair 2 (resident then reopen) also passed unchanged-runtime-harness gates.
  Resident preparation 1.310/1.189 s vs reopen 1.942/1.828 s; resident GUI bind
  5.90/2.20 ms and heartbeat max 49.29/38.49 ms. Both cases have exact deferred
  numerical/pixel/asset checks, native freshness and zero forbidden readbacks.
  Strengthened validator/tests additionally require eight owner-thread tasks,
  matching opens/closes and measured-window render signal evidence; both recorded
  pairs pass these final gates without rerunning unchanged native code.
- Final validation: 39 targeted tests/34 subtests and Black on eight changed
  Python files pass. Four controllers/four producers all exit 0; 400 post-window
  numerical comparisons pass. Two implementation/check iterations and four of six
  native slots used. Source-open counts refer to source-resource-group factory
  calls, not individual GDAL operations. No full-suite/CI pass is claimed.
- **Decision: support/narrow; close successful A0h without spending spare slots.**
  Source residency lowers measured preparation costs in both matched pairs;
  short samples do not establish statistical/application/presentation acceptance.
  The earlier ~295 ms tail does not recur under the clean protocol but its old
  cause is not isolated. Legacy chart work and ~31 s startup remain the next
  bottlenecks; propose independent chart/numerical publication and removal of the
  legacy startup dependency under a new contract. No automatic later milestones,
  production/#50/routing edits, commits, pushes, PR changes or merges.
  Evidence: [A0h report](history/rendering-a0h-resident-worker.md),
  [summary](profiles/rendering/2026-10-18/a0h-resident-worker-summary.json).

### A0g unattended continuation contract — diagnose, finish repeated loads, freshness

- User authorized uninterrupted continuation and progress documentation while
  away. This is still M0/A0, not production rollout or approval to commit/push.
- Scope: determine actual counts/masks at A0f's second destination; fix an
  experiment/harness bug only if supported by differential evidence; complete
  same-process repeated loads and rapid request replacement/return while keeping
  visible resident coverage honest. Qt, all sources, exact clock/style selection,
  routing, #50 and existing per-bundle limits remain unchanged.
- Baseline: same verified derived fixture/export and +576/+1152-pixel east
  destinations. Initial diagnostic native attempt uses the unchanged strict count
  gate with newly detailed errors. Do not silently change destination or accept
  blank data. If both sides legitimately select zero, first document exact
  endpoint/mask/anchor/count equivalence and explicit no-usable-data UI semantics;
  if counts differ, preserve failure and fix selection before any acceptance.
- Workload extension after the cause is understood: fixed-time destination
  sequence and explicit rapid reversals/newer generations in one producer. No
  prefetched destination exports, layer/zoom changes or new dataset. Compare every
  accepted destination to reference pixels and exact numerical selection; report
  cold startup separately from subsequent preparation/export/publication costs.
- Freshness gates: one active/one latest pending request; obsolete completion
  never binds after view revision changes, including A→B→A. Explicit missing,
  loading and error states. Confirm graceful shutdown and preserve raw failure
  diagnostics, commands/hashes/input states outside Git. Readbacks validate
  fidelity, not presentation/FPS. No speedup claim from unpaired short runs.
- Overall unattended budget: six implementation/targeted-check iterations, at
  most eight controller attempts (one native producer each; sixteen native
  processes maximum), 120 seconds/controller and 90 seconds/producer lifetime,
  90 minutes wall time. Diagnostic sub-gate uses one native attempt; record its
  outcome and next hypothesis before another. Stop at overall exhaustion,
  unresolvable fidelity disagreement, unavailable artifacts or scope expansion;
  leave no child running. Do not silently extend budgets or start later milestones.
- Review each completed sub-gate and keep an incremental progress log here.
  Correctness, synthetic freshness, real-workload timing and architecture adoption
  remain distinct. Complete useful bounded work, not an indefinite unattended job.
- Diagnostic sub-gate outcome (`rendering-a0g-diagnosis-1`): candidate=0 and
  reference=0 at the original second destination (revision 0), after exact
  reconstructed endpoint/mask/anchor equality. This identifies the harness's
  unconditional nonblank requirement, not a demonstrated selection mismatch.
  Continue within the overall budget: permit reference-empty only in this
  explicitly opted-in persistent producer, require equal counts and no drawable
  finite vectors in both exact endpoint atlases, record each empty case, and
  show an explicit host status badge outside the map scene. Scene pixels still
  compare byte-for-byte against the reference; the added status UI must not be
  called numerical-weather-ready or evidence of usable forecast coverage.
- Empty-view attempt (`rendering-a0g-empty-2`): all 25 second-destination
  numerical/count checks passed, but complete-pixel comparison rejected. Saved
  reference readback is red while candidate's no-symbol map is brown. Preserve
  this as a real visual disagreement, not a pass. Next diagnostic sub-gate checks
  the actual reference frame's atlas count and finite endpoint vectors, and saves
  a second reference readback with an independently proven empty symbol layer
  suppressed. Do not change the oracle or production shader until this separates
  stale frame, incorrect numerical binding and empty-layer rendering behavior.
- Empty GPU diagnostic (`rendering-a0g-empty-gpu-3`): reference frame atlas count
  is 0, both uploaded endpoint atlases have 0 finite vectors, independent count is
  0. Hiding that empty layer changes all 2,087,400 scene pixels. The original GPU
  oracle is therefore not a faithful no-symbol portrayal for this case (precise
  shader/driver cause unclassified). Preserve original and suppressed readbacks.
  Continue with an explicitly versioned empty-layer oracle correction only when
  all three independent/frame/finite-vector checks prove emptiness. Nonempty
  reference readbacks stay unchanged, count/mask/anchor/value checks stay exact,
  and this is NOT byte-equivalence to the erroneous original empty-GPU output.
  No production/#50 change; record the empty-layer problem for focused follow-up.
- Two-load sub-gate (`rendering-a0g-two-loads-4`) now passed with controller and
  producer exit 0: same producer serves both original destinations, 25 exact
  checks each (second 25 reference-empty), complete scene pixels exact under the
  explicitly recorded empty-layer oracle semantics, and the host badge labels
  reference-empty wind. This closes the repeated-load correctness blocker, not
  performance acceptance. Continue within the same unattended budget to a native
  A→B→A replacement test: ensure an active old A and an intermediate B cannot bind
  after the newer A request. Preserve generation IDs even when view keys repeat.
- Freshness sub-gate (`rendering-a0g-freshness-5`) passed with clean controller
  and producer exits. Native A→B→A discarded the old A generation, coalesced B,
  published only the newer A and matched destination pixels; the two preceding
  area loads also passed. Thirty focused tests/21 subtests passed. Continue within
  the same overall budget to two same-protocol repeats with phase attribution:
  separate view application, first request/readback, overlay settling, settled
  readback and oracle export. This attributes the prototype's remaining seconds;
  it does not call diagnostic readbacks production latency, nor establish a
  paired speedup or presentation FPS. No later milestone starts by default.
- Repeated-workload sub-gate: `rendering-a0g-repeat-6` and `-7` both exited 0
  under identical v3 instrumentation. Both destination loads, explicit empty
  status, pixel motion/masks and native A→B→A passed. In repeat 7, new-area export
  was ~3.5–3.6 seconds, of which ~2.4 seconds was private preparation/oracle export
  and ~1.0 second overlay settling; final A export was ~2.7 seconds. Heartbeat
  p95 remained near 17–18 ms but a 277 ms gap occurred, so do not claim the UI
  meets responsiveness acceptance. Continue only to the final available native
  slot with finer source-open/staging/oracle and GUI-bind attribution; no behavior
  optimization or further repetitions in this budget. Preserve the v3 repeats as
  separate from that changed v4 instrumentation.
- Final attribution sub-gate (`rendering-a0g-attribution-8`) passed all correctness
  and native freshness gates, controller/producer exit 0. Private source reopening
  costs 621–658 ms/export; field staging 58–72 ms; the 25-view numerical oracle
  costs 1,385–1,555 ms. GUI bundle construction/bind measured 21.6/16.1 ms for
  the two destination loads, but the heartbeat still has a 295 ms tail. The tail
  is not explained by those bind measurements alone and is not presentation.
- **Final decision: support correctness/ownership; narrow performance work.**
  Eight of eight native attempts and six implementation/check iterations used;
  stop this overall budget, leave no child running. Thirty core tests/21 subtests
  pass; Black on all five changed Python files passes. Preserve rejected attempts
  and original empty-GPU readbacks. Next scoped work should retain source resources
  in one owning worker thread, keep oracle validation outside timed interaction,
  and attribute GUI/render tails without correctness readbacks. No automatic
  later milestones, production/#50 edits, source/routing changes or commits/pushes.
  Full progress and limits: [A0g checkpoint](history/rendering-a0g-progress.md),
  [A0g summary](profiles/rendering/2026-10-18/a0g-unattended-summary.json).

### A0f contract — persistent producer and visible, explicitly partial loading

- M0/A0, continuation authorized by user. Hypothesis: a preparation app retained
  across requests avoids repeated initialization, while valid resident image
  coverage can remain visibly movable without claiming missing weather is ready.
- Scope: experiment-only persistent offline producer, two sequential newly
  requested destinations in one process, bounded latest-request scheduler, exact
  source/time/style/geometry identity and per-bundle 64 MiB ceiling unchanged.
  Producer retains its existing app/renderer resources, not an accepted total
  memory bound. No production/#50/routing edits, downloads, source pruning, zoom,
  layer toggles or approximate numerical selection.
- Visible loading: translate retained same-time image layers only within their
  actual geographic image coverage. Mask uncovered pixels and explicitly label
  missing numerical wind symbols; never classify this preview as complete/current
  weather. All views remain at the exact frozen forecast time. Verify pixel motion
  of the covered region separately from complete destination post-load fidelity.
- Workload: existing derived fixture/export; first target +576 logical pixels
  east of initial, second another +576 east; no pre-exported destinations. Same
  process ID on both exports, separate startup/request/export/publication timings.
  GUI camera changes while preparation is deliberately held; raw input/tick
  diagnostics, explicit child status and exact destination oracle comparisons.
- Gates: masked missing coverage, pixel-exact movement of valid resident image
  content, exact loaded complete pixels and numerical selection at both targets,
  same producer PID, bounded request ownership and clean shutdown. No native
  cancellation/freshness acceptance inferred from synthetic tests. No FPS claim.
- Budget: three implementation/check iterations, up to four controller attempts
  each with one native producer (eight native processes maximum), 120 seconds per
  controller, 90 seconds producer lifetime, 45 minutes wall time. Stop on fidelity
  mismatch or exhausted budget. Do not widen scope or relax fidelity gates.
- Decision: correctness/mechanism and measured within-process request costs are
  separate from repeated paired performance/visible presentation acceptance.
- Attempt 1 passed first-destination nondegenerate preview motion and exact loaded
  pixels, then rejected the second preview's featureless land crop. This is not
  evidence of a changed weather answer. Keep all exact pixel comparisons; require
  nondegenerate geographic-detail motion on the first leg and additionally verify
  translated coverage-boundary pixels actually move on both legs. A uniform crop
  is recorded as inconclusive for geographic-detail motion, not promoted as proof
  by lowering the distinct-color threshold. No numerical/source gate is relaxed.
- Attempt 2: same producer PID handled both requests and shut down with exit 0;
  controller exited 1. First destination: 25 exact numerical checks, complete
  pixels exact, 166-color preview crop translates exactly, coverage boundary moves
  exactly and missing coverage is masked. Preparation/export cost 3.66 seconds
  excluding producer startup; request-to-verification still 34.55 seconds including
  waiting for initial startup. GUI tick p95/max 17.67/92.71 ms are event-loop
  diagnostics, not presentation. A successful fast second load is NOT established.
- Second destination stopped at `Selection count differs or is blank at time
  step 0`. The preceding endpoint/anchor comparison did not reject, but the old
  error did not capture actual counts, so empty reference coverage vs count
  mismatch remains unknown. Do not accept zero symbols by deleting this gate.
  Final diagnostic-only change now includes candidate/reference counts, revision
  and exact view in future errors; no native rerun after that change.
- Validation: 32 distinct focused tests/13 subtests passed (26 core tests rerun
  after preview diagnostics; six startup tests unchanged since their pass), Black
  on six changed Python files. **Decision: stop/narrow at the count gate**, not
  spend remaining native slots or start later milestones. A0f remains partial,
  not an accepted persistent-load performance pass. Next bounded diagnosis must
  distinguish missing forecast coverage from altered selection before changing
  workload or missing-data policy. No production/#50 changes or commits/pushes.
  Evidence: [A0f partial summary](profiles/rendering/2026-10-18/a0f-persistent-summary.json).

### A0e follow-up contract — settle geometry, then prove destination publication

- M0/A0 remains active. Local the reference application binary inspection supports separating
  camera redraw, asynchronous preparation and bounded publication; it does not
  establish our correctness/performance or require replacing Qt.
- Scope: fix the preparation/oracle viewport lifecycle, retain exact geometry,
  source/clock/style/DPR checks, and complete actual new-area loading/publication.
  Preserve failures and heartbeat/input diagnostics even on rejected runs. No
  production/#50 edits, routing/source-selection changes, or display approximation.
- Workload: same checksummed derived fixture and initial export, fixed exact
  forecast time, destination 576 logical pixels east, prepared only after request.
  Initial saved layout must settle before applying target; re-settle overlays
  afterward. Do not resize/stretch output or weaken identity checks to pass.
- Gates: explicit loading with no stale content; GUI ticks and camera input state
  changes while preparation is held; bounded scheduler/cancellation tests; exact
  new-area numerical selection and complete-composition comparison against the
  reference. Offscreen readbacks are correctness only, never presentation/FPS.
  Full-screen loading currently hides the map; input-state responsiveness is not
  proof of smooth visible map movement while loading.
- Budget: three implementation/targeted-check iterations, at most four controller
  attempts with one native preparation child each (eight native processes maximum),
  120 seconds/controller, 90 seconds/preparation, 40 minutes wall time. Stop on
  fidelity disagreement or exhausted budget; reevaluate/narrow rather than expand
  into layer toggles/zoom or a backend rewrite. Prior failed runs remain separate.
- Acceptance: actual destination publication + exact pixels is limited mechanism
  support, not paired speedup, memory plateau, the reference application equivalence or rollout.
- Outcome: applying the requested view after initial layout/overlays settle passes
  the unchanged exact 710x735/zoom check; overlay preparation is then re-settled.
  Follow-up attempt 1 completed destination preparation and exact pixel checks,
  but controller exit was 1 because the heartbeat summary contained a NumPy int64
  that could not serialize. Saved diagnostics retained that partial result.
  Converting heartbeat inputs to native Python scalars fixed report writing and
  has a focused strict-JSON regression check.
- Follow-up attempt 2: controller and producer exited 0 (42.7/38.6 seconds). All
  33 forecasts retained; 25 new-area native-selection/endpoint/anchor/count checks
  exact, complete destination composition byte-exact. GUI heartbeat: 2,448 ticks,
  p95 interval 17.2 ms, maximum 79.8 ms, two intervals over 50 ms. Thirty-two
  camera input states were exercised during a deliberate preparation hold.
  These are event-loop/input-state observations, **not visible map frames or
  presentation timing**. The preparation command includes snapshot verification,
  sandbox/app initialization and reference export, not just forecast decoding.
- Validation: 29 focused tests/13 subtests, Black on three changed Python files;
  two implementation/check iterations and two of four controller slots used.
  **Decision: support/narrow; close this gate without consuming unused slots.**
  Asynchronous destination publication is viable in Qt with current fidelity.
  The next bottlenecks are the throwaway whole-app producer and absence of visible
  resident-map drawing during loading. Propose a separately bounded long-lived
  preparation/independent-chart slice, not architecture adoption, backend rewrite
  or automatic later milestones. No production/#50 changes, commits or pushes.
  Evidence: [A0e follow-up summary](profiles/rendering/2026-10-18/a0e-followup-summary.json).

### A0e contract — asynchronous new-area preparation and publication

- Active milestone/hypothesis: M0/A0. Test that the independent Qt viewport can
  remain responsive while a new-area resource generation is actually prepared,
  then publish a current generation with reference-exact complete pixels.
- Scope: experiment-only, one active preparation process and one replaceable
  pending request; cancellation/generation rejection, explicit loading/error,
  checksum/identity checks and GUI-thread publication. Retain the legacy app in a
  disposable offline subprocess as preparation/oracle only. This first producer
  is deliberately expensive, not the intended production scheduler/backend.
- Workload: existing derived fixture (97cfda3c...), saved dimensions/DPR, fixed
  initial exact forecast time, pan 576 logical pixels outside initial coverage.
  Generate fresh resources only after the miss; no prefetched destination bundle.
  No source pruning, routing, production/#50 edits, downloads or rollout.
- Gates: GUI heartbeat and camera changes during a deliberately held preparation;
  bounded one-active/one-pending ownership, obsolete result rejection and explicit
  subprocess termination/status. Verify snapshot/view/clock/style/DPR identity,
  resource checksums and a 64 MiB per-bundle serialized payload ceiling before
  decode. This is not a total RSS/GPU memory bound. On completion compare full
  composition byte-for-byte against the independently rendered destination oracle.
  Readbacks are correctness only, never presentation timing or FPS.
- Budget: two implementation/targeted-check iterations, two native controller
  attempts each with at most one native preparation child (four native processes
  total), 120 seconds/controller, 90 seconds/preparation, 40 minutes wall time.
  Synthetic scheduler/process tests do not use forecast/native attempts. Stop on
  fidelity disagreement, timeout or exhausted budget; record narrow/revise/stop.
- Decision: mechanism support only if all gates pass. No paired performance win,
  the reference application equivalence, memory plateau or architecture adoption from this probe.
- Outcome: 27 focused tests/ten subtests and Black on six changed Python files
  passed. Synthetic tests cover one-active/one-pending/latest-only publication,
  cancellation/reaping, explicit errors, identity/checksum/budget rejection, and
  preservation of uint8 image bytes and float64 native-node coordinates.
- Native attempt 1 exited 1 before preparation (wrong protocol helper import,
  corrected). Attempt 2 controller/preparation both exited 1: the early
  pre-first-grab callback rejected mismatched viewport dimensions/scale before
  export. Exact actual geometry was not logged, so early/unsettled layout is a
  hypothesis, not a confirmed cause. No destination bundle/publication, accepted
  heartbeat report or post-load fidelity result exists.
- **Decision: stop/narrow.** Two implementation/check iterations and two
  controller attempts consumed (three native processes actually launched). Keep
  strict geometry gates. Next focused follow-up: log actual/requested geometry,
  apply the target after layout settles, and re-settle overlays before exporting.
  Do not widen into host replacement or performance comparisons. Evidence:
  [A0e failure summary](profiles/rendering/2026-10-18/a0e-coverage-load-summary.json).

### A0d restart — coverage-loss safety, not a cold-load speedup

- M0 / A0 remains the active milestone/hypothesis. Earlier interrupted drafts
  produced two clean reference exits and two failed candidate exits (trajectory
  length, then prepared-image coverage escape); none is an accepted comparison.
- Scope: repair experiment-only coverage detection, explicitly hide all stale
  chart/weather/navigation content on a miss, show an honest unavailable state,
  and verify exact initial composition returns after a matched out/back camera
  trajectory. No production, routing, source-selection or #50 changes.
- Baseline/workload: existing checksummed derived fixture and exported resources,
  fixed forecast time, 24 relative pan steps (12 outward, 12 return), 48 logical
  pixels each. This is a standalone safety mechanism check, not a paired speedup
  comparison. Keep strict comparison gates; reject unsupported cold-load pairs.
- Fidelity/measurement gates: coverage includes both images and numerical field;
  no zero-valued fake weather, stale background or silent clipped success. Verify
  visible unavailable content outside timed input and byte-exact composition on
  return. Preserve all input revisions and explicit child exit status. Presentation
  and post-load composition at a new area remain unmeasured.
- Restart budget: two implementation/targeted-validation iterations, at most two
  candidate native processes, 110 seconds each, 25 minutes total. Stop on exhausted
  budget or fidelity failure. No extra reference run or async scheduler by default.
- Decision gate: continue/narrow/stop from safety evidence. Loading new resources
  requires a separately scoped scheduler; do not label an idle unavailable state
  as loading or count missing content as faster rendering.
- Outcome: both restart native attempts exited 0 on Cocoa/Metal/DPR 2; final
  run had 24 submissions (15 unavailable, 9 ready). Initial and resident-return
  readbacks were byte-exact against the reference, including after the full
  replay. Unavailable readback verified the explicit status composition with all
  stale layers hidden. Fifteen targeted tests/ten subtests and Black on four
  files passed. Two iterations/two native attempts consumed; stop this budget.
- **Decision: narrow.** Safety mechanism supported, no cold-load or speedup pass.
  The current blocker is missing preparation/resource publication, not evidence
  that Qt blocks the target. Next justified work is a separately contracted
  bounded asynchronous coverage scheduler and new-area post-load fidelity check,
  not layer toggles or architecture adoption. See
  [A0d evidence](profiles/rendering/2026-10-18/a0d-coverage-safety-summary.json).

### 2026-10-17 — Start M0; narrow first work to measurement

- Audited startup replay, standalone GPU experiment, comparison gates, and the
  embedded presenter at baseline commit above.
- Existing isolation/checksum/gesture/worker telemetry can be reused. Timed
  readbacks and callback ambiguity prevent claiming current presentation latency.
- **Decision: narrow.** Establish B1 before renderer migration. E1–E3 remain
  unproven; no performance improvement or architecture choice is claimed.
- Governance/audit slice budget: one source audit and one documentation validation
  pass; no benchmark matrix. Validation: whitespace check passed; five changed
  Markdown files checked, 25 local link targets exist, and the review heading is
  unique. No application tests/benchmarks run; Black is inapplicable because no
  Python files changed. M0 remains open; B1 is the next implementation slice.

### 2026-10-17 — B1 budget review

- Added separately versioned native render-signal replay with fixed input
  deadlines, direct callbacks, submitted-frame attribution, explicit unobserved
  revisions, resource identity metadata, and replay-window upload deltas.
- Final checks: 21 passed; Black passed on three changed Python files; whitespace
  check passed. Both native attempts completed on Cocoa/Metal, DPR 2.
- Each smoke observed 17/24 input revisions; seven were superseded without
  observation. These short, different-scenario/harness runs are not a speedup
  comparison and do not meet M0 acceptance.
- **Decision: continue, narrowly.** Use this proxy to design E2's stalled-worker
  control; retain presentation acceptance as open and separately assess platform
  tracing. Do not pursue texture-format or prefetch tuning from these results.
- The source audit's concern remains: GUI frame assembly/image replacement is
  still present. Measured rebuild/upload activity is an attribution lead, not
  proof of causality. E1–E3 remain unproven.

### 2026-10-18 — Approved residency slice delivered; no pan victory claim

- User chose **extend #50**, leaving it unmerged. Added worker-private
  source/index subclasses with global 128 MiB retained-band accounting, pre-read
  oversized-band rejection, cross-source LRU, worker-side invalidation release
  and resource-error propagation that cannot silently change model selection.
- Validation: 40 worker/cache/staging/transport checks, 11 benchmark/telemetry
  checks, and one native map integration check passed in separate invocations.
  Seven changed Python files passed Black; final whitespace/docs/JSON checks
  passed. No shared sampler/routing change and no full-suite run for this slice.
- Native/real attempt budget used: four. One synthetic integration passed; the
  original real-data fixture failed verification before launch (3,608 missing
  files); two explicitly derived functional replays passed. The original was
  not rewritten. All recoverable original inputs were checksum-matched into a
  new fixture, omitting only 370 unavailable marine download-cache files. Its
  distinct content hash is recorded; it is not an identical timing baseline.
- Actual 33-forecast numerical shader activation: 734 symbols, 60,512 decoded
  bytes; 23/24 scrub frames numerical, with an initial endpoint-pair transition.
  No warnings or CPU symbols when active. Images were retained across the pair.
- **Pan remains unresolved:** zero of 24 moving frames used numerical symbols;
  522 numerical symbols returned after settling, decoded peak 61,296 bytes.
  Do not claim fluid pan, presentation FPS, total-memory plateau or speedup.
- **Decision: close this narrow implementation slice for review.** #50 stays
  opt-in/unmerged. E2's next experiment must preserve current screen-cell thinning
  and distinguish valid retained preview/coverage from current data. The broader
  epic and M0 remain open. Do not automatically extend the exhausted validation
  budget or widen #50 into the rest of the renderer.

> **Status update (2026-10-19):** PR #50 was merged to `dev` via `b9e2471` / PR #61
> `1a90515`. The entry above was recorded when it was still unmerged; the
> measurements are unchanged.

### 2026-10-18 — CI-review corrections delivered

- CI identified new cache exception-name/concrete-interface typing issues and
  provider annotation/worker complexity in changed code. A separate review budget
  was recorded: one focused correction/validation iteration, one real-data native
  attempt; no memory-limit increase or unrelated dependency repair.
- `175096d` preserves the concrete OrderedDict contract without type ignores or
  casts, accounts for the same arrays in local/global dictionaries, and extracts
  worker-only resource and request handoff helpers. Shared sampler code is intact.
- Revalidation: 40 targeted checks, lint/type checks on both production modules,
  Black on four newly changed files and whitespace passed. Fresh derived native
  scrub: 734 symbols, 23/24 numerical frames, 60,512 retained decoded bytes,
  no warnings. Pan was not rerun in this follow-up budget.
- Unrelated repository CI failures (including missing `nmeatoolkit` during test
  collection and other lint/type diagnostics) remain outside this slice. No full
  repository CI pass is claimed. #50 remains opt-in; it was subsequently merged to
  `dev` (merge commit `b9e2471`, via PR #61 `1a90515`).

### 2026-10-18 — E2 native gate rejects a worker-only pan claim

- E2a: 27 interleaved view pairs matched complete atlas/staging/provenance hashes
  while numerical preparation was blocked. Worker median 20.1 → 2.7 ms, initial
  wider field build 80.8 ms. Seven tests/three subtests passed.
- E2b: two native processes and strict comparison passed the identity gates.
  All 24 inputs retained; camera/time trajectories match. One wider field was
  built/reference-verified before motion, and 24 retained selections completed
  without field rebuilding during input. Eighteen comparison/recorder tests pass.
- Native outcome is **mixed**: clean numerical-ready submitted-state render
  observations 0/24 → 1/24, any-render observations 18 → 17, observed-only render
  p95 21.9 → 33.0 ms. Dispatch lateness p95 improved 23.9 → 17.7 ms, so do not
  infer a stable regression or win from a single small process pair.
- Four retained-control submissions had numerical pairs alongside CPU batches;
  24 had no pair. Raw observations are conservative submission-state proxies,
  not compositor/pixel proof. Faster preparation did not remove view-key/frame
  assembly/portrayal handoff pressure. Exact causality needs the next design gate.
- **Decision: narrow/pivot.** Keep numerical data reuse, reject the worker-only
  prototype as a pan fix. Next test persistent field resources with a small
  per-view selection payload and atomic CPU/GPU symbol mode. Keep chart/weather
  invalidation and GUI costs visible. Do not add this broader work to #50 under
  the previous residency-only approval, or silently extend exhausted experiments.
