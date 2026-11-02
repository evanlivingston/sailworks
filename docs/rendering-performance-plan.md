# Rendering performance: current state

Tracking epic: [#51](https://github.com/evanlivingston/sailworks/issues/51).
This file states the current architecture decision, what shipped, the
invariants, the acceptance gates and the one next decision. It is not a work
queue and it is not a log: the issue body is the spec for a task, and past
experiment records live in [`history/`](history/).

## How to use this file

| You want | Read |
| --- | --- |
| What to build next | the issue body. It states the gap, scope, non-goals, acceptance gate and validation command |
| The accepted architecture decision (D1-D7) | [ADR-001](adr-001-grib-rendering-architecture.md), the section your task touches |
| The invariants and the measurement contract | [the implementation plan](rendering-implementation-plan.md), section `Reference and invariants` |
| What shipped and what is not claimed | this file, `Current state` |
| Why a past decision was made | [`history/rendering-decision-log.md`](history/rendering-decision-log.md) |
| What an earlier experiment measured | [`history/rendering-experiment-records.md`](history/rendering-experiment-records.md) |

Read only the section you need. Every result, threshold, negative result and
budget stays on the record: in this file, in the issue body, or behind a link
to the document that records it. Nothing is deleted evidence.

## Current state

**Decision (ADR-001 D1-D8).** The renderer is split into decoupled layers with
explicit ownership: weather preparation, chart composition and presentation are
separate, and the Qt viewport owns the scene graph. Issues #62-#73 are closed;
epic [#51](https://github.com/evanlivingston/sailworks/issues/51) stays
open for the follow-on work.

**What shipped.** Native weather fields are on by default: a GPU-resident field
cache under one shared session byte budget, merged in PR #82 for
[#64](https://github.com/evanlivingston/sailworks/issues/64) (closed
2026-10-21). `--no-native-weather-fields` or `GWR_NATIVE_WEATHER_FIELDS=0`
restores the Image path, and geographic barb layout is the default
(`--screen-barbs` restores the old). ENC charts are enabled by default in the
application viewport (PR #81);
[#78](https://github.com/evanlivingston/sailworks/issues/78) closed
2026-10-21 after 13/13 functional and 14/14 combined-experience checks plus 104
targeted integration passes.

**What is not claimed.** The normal Charts/Logs application now uses the
viewport (PR #91), while `ViewportMap` still inherits the `TrackMap` compatibility
host during cleanup in [#86](https://github.com/evanlivingston/sailworks/issues/86).
[#70](https://github.com/evanlivingston/sailworks/issues/70) closed
2026-10-21 with a **keep-experimental recommendation**; the user's later D7/D8
decisions direct application use and retirement of the legacy map. This does not
retroactively pass #70's visual, platform, workload, or performance gaps.
Presentation and input-to-present latency are unmeasured; the recorded endpoint
is a render-callback proxy, never FPS. #64's recorded limits stand:
stale in-flight completions are covered by headless tests only, true device loss
is not exercisable, there is no single repeatable `residency-native` gate, byte
figures are texture payload not physical VRAM, upload counts are QRhi
submissions not GPU completion, and the original-sampler
([#65](https://github.com/evanlivingston/sailworks/issues/65)) fidelity
dependency is unchanged.

**Display policy.** The user chooses the forecast in the Weather panel; the
application must not silently pick a different file/run/member while the user
pans or changes forecast time. See `Forecast display coherence` below.

**Next decision.** Complete the staged removal of the old default map under
[#86](https://github.com/evanlivingston/sailworks/issues/86), following
the user's explicit decision not to require legacy CPU recovery. The #70
evidence remains a recorded limitation, not a passed acceptance gate. The ENC
chart engine is under
evaluation against tile57
([#87](https://github.com/evanlivingston/sailworks/issues/87)); that is an
evaluation, not a removal.

**Evidence identity.** Per-experiment contracts and results are in the history
records linked below. Recorded evidence under
[`profiles/rendering/`](profiles/rendering/) is the reference: do not re-measure
a baseline the change does not touch, and do not attribute a measurement to a
later commit than the one it was taken on.

## Forecast display coherence

**Accepted user policy, 2026-10-20; shared path opt-in.** The Models panel and explicit
`SessionController` analysis paths use one `ForecastAnalysisState` tranche/entry for
display and routing; old jobs retain their binding. All models share one region captured
from the current map view, unchanged by later panning. Refresh intent stays separate.
No legacy files are adopted, deleted or used as fallback. Profiles remain legacy until
explicit Models opt-in; cleanup awaits validation. This is not viewport or
performance acceptance. Our static analysis does not prove whole-map pinning or
multi-run containers; our coherence rule remains stricter.

### Policy

- Add an explicit Weather-panel selector, distinct from loading/enabling forecast
  files in the data library. Show which file, model run and member are selected;
  unavailable data must remain visible as unavailable, not trigger substitution.
- Pin the user-chosen file, model, production/reference time, ensemble member and field/
  level. Requested forecast **valid time** is distinct from production time. Fields,
  barbs, contours, display point queries and labels must agree on selection.
- Spatial interpolation, same-file temporal interpolation and explicitly identified
  same-source single-endpoint holding remain allowed within declared coverage.
  A multi-run/member container must still respect the chosen identity.
- Do not implicitly borrow U/V components, time endpoints, fields or masked regions
  from another file/run/model. Missing remains unavailable. Shared model/run metadata
  alone does not authorize split-file pairing or temporal interpolation.
- Multiple files require explicit geographic stitching: region ownership, overlap
  priority and allowed model/run/member compatibility. Until specified and tested,
  stitching is unsupported. Do not spatially blend different forecast owners.
- Ensemble members stay separate; an explicitly selected mean/statistical product
  is different from silently combining enabled forecasts.

### Why this is separate from the pixel/value differences

The viewport's `viewport-gridded-field-summary.json` reports 0.1256 m/s maximum and
0.0192 m/s mean wind-speed difference against the original sampler at 40 positions.
The approximately 4.17e-7 m/s GPU placement/arithmetic check compares interpolation
of the **same staged lattice**, not the original sampler. Neither proves a general
resampling bound. The logical/device-pixel missing-node probe defect is separately
recorded as corrected; visual fidelity is still deferred.

**No inspected result establishes forecast mixing as the cause of any reported
resampling or visual mismatch.** Reproduce differences with coherent selection before
assigning a cause; preserve the old evidence instead of overwriting it.

### Code evidence and follow-up contract

Source audit of the viewport branch (initial inspected HEAD `b27d33e`):

- `core/gribsampling.py`, `GridIndex._records` groups by reference/model/level, not
  file. `WeatherSampler._wind_pairs` prefers complete same-file pairs but permits
  cross-file components; `_side`/`_blend` allow different files' temporal endpoints
  within one identity. Distinct identities/runs are not directly time-blended.
- `sample_timestamps` fills missing points from later eligible identities, including
  older runs. `rendering/weatherstaging.py` retains these rules. This policy predates
  the viewport; existing cross-file/fallback tests are intentional, not defective.
- `qt/shaders/weather.frag` receives values without file/run ownership and cannot
  reject cross-owner spatial blending. Generation rejection/source signatures ensure
  freshness, not coherence. Permitted mechanisms are not proof of actual fixture mixing.

**Implementation boundary:** select a display file/run before staging; constrain
components/endpoints; carry structured identity through publication/cache keys and
invalidate on switches. Do not globally change `WeatherSampler` or routing. Use
the integrated viewport base; #70's old measurements remain valid only for its
old candidate/policy, not a changed workload.

**Focused tests:** preserve same-source interpolation, exact-time masks and allowed
holding; reject unauthorized split-file components/endpoints, older-run gap fill and
mixed runs/members in one file; test pan/scrub pinning, transitions, provenance/cache
identity and stale-publication rejection across all displayed weather layers. Keep
stitching disabled until its boundary policy is explicit. Preserve routing tests.

**One-view fidelity gate:** freeze a representative view, chosen file/run/member,
timestamps and geographic probe coordinates. Compare direct sampler and staged-lattice
U/V, speed and validity with pointwise provenance, including gradients and missing/
coverage edges; assess matched fill/barb/chart captures separately from timed runs.
The reported 40-point summary lacks checked-in pointwise reproduction, so retain
coordinates/results and denominators. Existing numerical/mask/selection gates stay;
no new approximation tolerance is approved. Record an implementation/measurement
budget before starting, stop at an explicit pass/revise/blocked fidelity verdict,
and leave broad performance/rollout gates open. No native work is authorized by the
documentation-only consolidation phase.

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
marine-heavy acceptance therefore uses the separate `marine-current-scrub` gate
and an explicit `--snapshot` pointing at an installed marine field cache. That
gate compares warm field-sampling p95 with the legacy point-shoreline predicate
against unmasked Qt Quick sampling on identical sources, time, camera and
density; it does not measure viewport presentation callbacks or presentation
FPS. The companion `marine-current-correctness` gate covers station-current and
tide annotation geometry/labels and verifies the old Cairo marine modules are
absent.

The locally installed 162-field cache passed the marine scrub gate at dense
density and a 1024×768 Salish Sea view: masked baseline sampling p95 was
2647.8 ms, versus 126.9 ms unmasked (95.2% lower), with available field arrows
increasing from 26 to 27. A separate saved-view check found 11 unmasked samples
that the old GSHHS predicate classified as land; the selected policy accepts
that difference, so shoreline pixel fidelity is intentionally not preserved.
At that saved zoom (12.27), the same experiment measured 20.8 ms masked versus
28.9 ms unmasked (38.6% slower), while arrows increased from 147 to 156. The
no-mask policy is therefore a large improvement for the zoom-8 workload that
triggered multi-second stalls, but is not faster at every camera scale. These
are local-cache sampling measurements, not a substitute for full
presentation-path or cross-platform acceptance. The gate records a SHA-256
fingerprint of selected field manifests/arrays so a changed cache is not
mistaken for the same workload.

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
| M0 Baseline + architecture decision | Reproducible real-workload evidence; E1–E3 decisions; selected host/ownership/residency design | **Closed 2026-10-19** (ADR-001 accepted) |
| M1 Bounded residency | Real forecasts work inside an explicit budget with sampler/provenance equivalence and resource release | **Closed** (#64, #71) |
| M2 Production fill + barbs | Retained numerical layers, cheap camera/time changes, accurate loading state, matched fidelity/performance gates | **Closed** (#65-#69, #73) |
| M3 Remaining bottlenecks | Reprofile, then retain contours/streamlines and address charts/navigation only as justified | **Closed** (#69); contours and streamlines stay out of the viewport |
| M4 Validation + rollout | Soak, lifecycle/DPR/context/backend coverage and explicit approval for rollout | **Closed 2026-10-21: keep-experimental**; the default application path was not switched |

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

## History

- [`history/rendering-decision-log.md`](history/rendering-decision-log.md) - the
  archived checkpoint notes, the consolidation notes, the #51 acceptance
  checkpoint and the A0 decision log.
- [`history/rendering-experiment-records.md`](history/rendering-experiment-records.md) -
  the runtime integration record and the M0 experiment contracts (B1, E1-E3).
- [`history/`](history/) - every other completed-work record and experiment
  evidence, including the rejected results.
