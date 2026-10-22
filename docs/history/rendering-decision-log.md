# Rendering decision log

> **Status: HISTORICAL.** This is a completed-work record, not a plan.
> It was consolidated from `docs/rendering-performance-plan.md` on 2026-10-22.
> Nothing here is a current work item; the live work program is issues
> [#62-#73](../milestones.md) and the open follow-on issues [#86-#88].
> Read it only to find out why a decision was made or what was already tried.
> Superseded by [`../rendering-performance-plan.md`](../rendering-performance-plan.md),
> which holds the current state, the invariants and the next decision.

## Archived checkpoint notes (2026-10-21 and earlier)

**#64 next decision:** scoping and the subsequent user-authorized compatibility/
ownership investigation are complete. Proper Python texture-item sources with
explicit public wrapper adoption passed 19/19 checks in two fresh native processes
on Cocoa/Metal/Qt 6.12. Preserve the CPU field store; the proposed next slice adds
explicitly owned render-thread field textures. See
[Native residency follow-up scope](#native-residency-follow-up-scope-64).
The user authorized that bounded product implementation on 2026-10-21. N1's
shared native store passed 16/16 native checks, including two widgets, forced
eviction/reload and full cleanup; 45 targeted checks passed. The shader-readiness
blocker was an overstrict cached-status guard in the harness, not a host rewrite.
N1 and N2 are complete locally. **The user subsequently authorized making native
weather fields the default on 2026-10-21**; the application and standalone viewport
now enable them without a flag. `--no-native-weather-fields` or
`GWR_NATIVE_WEATHER_FIELDS=0` restores Image sources (an explicit CLI choice wins).
N2's real-HRRR application/layer gate passed 18/18 checks, including two
application maps on one budget and real A → B → A native reuse. Its bounded budget
is consumed; N3 acceptance/review remains unstarted. This authorized default switch
does not constitute #64 acceptance or closure.
See the N1 completion checkpoint and evidence below.
#64 is not complete. Earlier default-OFF decisions below are historical checkpoints.

**#78 user-revised functional and combined-experience acceptance passed.** The
experimental viewport now composes enabled ENC with the original source ranking,
multi-cell clipping and Cairo portrayal, asynchronously and off by default.
The user deferred strict visual acceptance in favor of functional display and
lifecycle completion. `enc-viewport-functional` passed 13/13 checks on the verified
fixture: SF, Puget and a real multi-cell boundary, twice in reversed order;
blocked-chart weather responsiveness, stale-image hiding, cancellation and cleanup.
All six observed readbacks also matched exactly, without claiming catalogue or
platform acceptance. Targeted checks: 102 passed, one known base failure.
Subsequent user-authorized combined-experience gate passed 14/14: charts, fill and
barbs together; fractional zoom and fixed-size soundings; resize; rapid reversals;
coverage miss/recovery; blocked-chart responsiveness and cleanup. That round had
104 targeted passes and the same known base failure. No legacy pixel comparison
was required. See the [contract and evidence](../enc-viewport-integration.md).
Default rollout, full catalogue/platform coverage, RSS/VRAM and presentation
performance remain separate #70 work; these functional results are not FPS.

**2026-10-20 integrated state:** PR #72 merged at `9d1b384`; PR #75 merged at
`bbbeac0`. The code currently remains standalone and default OFF. During review of
PR #76, the user **rejected continuing standalone-only use** and directed us to start
using the viewport in the application, with **forecast selection controlled by the
user in the Weather panel**. This is the accepted next direction (ADR D7), not a
claim that integration or open fidelity/performance gates are complete. No routing
change or #51 closure is authorized.

**Priority now:** integrate the viewport into the normal application and add the
explicit Weather-panel file/run/member selector, preserving navigation/interaction
contracts, selected-source consistency and legacy renderer recovery. Further branch
cleanup is secondary. Trace integration boundaries and record an implementation
budget before coding; use the [coherence/fidelity contract](../rendering-performance-plan.md#forecast-display-coherence)
to validate what is displayed, not to postpone application use indefinitely.

**Decision-revision scope:** update PR #76's existing documentation only, plus a
bounded read-only integration trace; one changed-file documentation validation round,
zero native attempts or new performance claims. Runtime implementation belongs on a
focused branch from integrated `dev`, not inside this documentation PR.

**Issue reconciliation:** #68 (scoped fill), #69 (explicitly rescoped viewport
Part 1/2), #71 (bounded transport answer) and #73 (precursor cleanup) have merged
implementation/evidence. #66 stays open for matched comparative dispatch evidence;
#67 stays open for actual upload/presentation acceptance, which provider counters
and render callbacks do not prove. #64 retains original residency/eviction/lifecycle
acceptance; #65 retains original-sampler resampling/fidelity acceptance; #70 retains
matrix/rollout gaps. #51 remains open. No criteria are removed to obtain closure.

**Documentation consolidation contract:** four existing Markdown files only;
at most two changed-file validation rounds, zero native runs or new performance
claims. Publish a regular fork PR against `dev`, unmerged for human review. Retire
only authorized, verified redundant branches/worktrees; retain the documentation
branch until its PR merges and leave unrelated work untouched.

**Executed cleanup:** #68/#69/#71/#73 closed with scoped evidence and remaining
epic gaps; #66/#67 explicitly retained for their unmet acceptance gates. Removed
the clean `agent-66-double-buffer` worktree and local/remote
`feature/async-double-buffered-66` branch after verifying head `502d9a3` is an
ancestor of `dev` and the primary session had moved away. The clean #64 worktree
(`36a5712`, ancestor) and spike worktree (`ab89697`, two patch-equivalent commits)
remain: other-session ownership is unconfirmed. No force removal/deletion. The
spike's non-ancestor original history must remain reachable if retired later.

**Documentation validation:** the subagent's initial runner launch was blocked by
the harness depth limit, with zero commands executed. The parent then delegated
validation successfully: four documents, eight added local links and seven anchors
checked with zero failures; changed-file whitespace passed. No runtime tests, Black,
native measurements or base-CI replication were needed for this documentation PR.

## Earlier consolidation notes (2026-10-20)

The queue/contracts below describe the pre-merge sequence, not current blockers.
PRs #72/#75 and their corrections are now merged. Final PR #75 checks passed 118
targeted tests and changed-file Black/Flake8/mypy; CI run `39482098900` retained
only known base lint/type findings, while Ubuntu GIS `resolute` PPA 404s prevented
tests/build execution. No full-suite success or post-integration native result is
claimed. Preserve earlier budgets, failures and stop/continuation decisions.

- **Current milestone:** consolidate the experimental viewport work, not another
  renderer feature. Review PR #72, resolve integration blockers, then review the
  remaining stacked viewport program. Human approval is required before merging;
  experimental integration does not authorize switching the application default.
- **#70 checkpoint (2026-10-20):** repeated candidate availability and accounted
  resource soak are recorded; **keep-experimental** is proposed, not user-approved
  rollout. See [epic acceptance](#epic-51-acceptance-checkpoint).
- **Next after consolidation:** one representative-view fidelity checkpoint with
  [coherent forecast selection](../rendering-performance-plan.md#forecast-display-coherence). That policy is accepted
  but unimplemented. Mixing has not been established as the cause of resampling or
  visual differences. Routing stays unchanged.
- **Consolidation contract:** documentation-only local cleanup, two changed-file
  validation rounds maximum, zero native attempts. Preserve negative results and
  profiles. Stop before runtime removal, remote publication, merge, issue closure or
  branch/worktree deletion without the applicable review/authorization.

### Consolidation queue (2026-10-20)

**One active task:** review PR #72's published correction and fresh CI results. **Next:**
review the remaining viewport stack as one experimental follow-up PR. Fidelity
implementation waits for a consolidated base; no new rendering technique is queued.

| Work | Verified relationship | Disposition |
| --- | --- | --- |
| PR #72, `feature/resident-weather-fields` at `36a5712` | Four foundation commits through `6407727`, plus the review-correction commit. The full viewport branch contains the foundation but not yet this correction. | Review fresh CI and the correction, then merge only with user approval. |
| `feature/async-double-buffered-66` at `dc52123` | 39 commits after `dev`, of which 35 follow PR #72. | Review one follow-up PR after the foundation; keep experimental/default-off. Do not rewrite shared history or silently expand #72 to the full stack. |
| `feature/float-transport-71` at `ab89697` | `git cherry` finds `d591905` equivalent to `5b37dc7` and `ab89697` equivalent to `9e50a10` in the viewport stack. PR #74 is closed, unmerged. | No second spike integration. Retain original head until equivalent work is accepted; retire its branch/worktree only with authorization and confirmed ownership. |
| This local documentation branch | Four existing documents modified; the two initially added standalone summaries have been folded into this roadmap. | Review as a small documentation follow-up to the integrated state. Do not leave parallel acceptance/implementation roadmaps. |
| Chart quilting and unrelated feature branches | Separate workstreams; clean worktrees are not proof that their work is obsolete. | Leave untouched; require a separate ownership/ancestry review before removal. |

**PR #72 CI triage:** PR CI run `39370012177` and Qt run `39370012116` were
compared to base `1c7fa88` CI run `39342228025` and Qt run `39342228063`.
The Qt workflow failures and the Python 3.13 failures in `test_isolated_qt_frontend`
and `test_hardware_status_does_not_imply_numerical_symbols_active` have matching
base failures. Do not replicate those failing suites locally. Base lint/typecheck
also fail, but the PR adds lint findings in `gribviewport.py`/`weatherbarbs.py` and
type errors in `gpuweatherfields.py`, `gribviewport.py` and `weatherbarbs.py`
(20 reported errors versus one on base). Those added findings are merge blockers,
not excused by a red baseline. Other cancelled test jobs provide no passing result.
Use targeted validation of fixes and changed-file Black/whitespace checks; retain
CI links/status rather than claim a whole-suite pass.

**Focused PR #72 correction contract:** local corrections in its existing clean
worktree, limited to newly introduced lint/type findings in the three files above.
Preserve lazy Qt loading, event override names, source/portrayal semantics and
existing baseline failures; no shared sampling/routing change or broad refactor.
Allow at most two targeted validation rounds, zero native runs, no full-suite/base
CI replication, commits, pushes or merge. Stop if runtime behavior or scope must
change. Keep these corrections separate from this documentation cleanup and from
the later 35-commit viewport review.

**Local correction checkpoint:** two targeted rounds completed in the PR #72
worktree, with no native runs or shared-sampler changes. Latest results: 55 targeted
tests passed (one warning), Black passed on three files, whitespace passed. Changed-
file lint retains only the known base `stage_barbs` complexity finding. Targeted
mypy retains the known base optional-identity error and one additional local Qt-stub
error: `QQuickItem` has no declared `readyChanged` signal at `gribviewport.py:603`.
The QML signal is dynamic; the new attribute annotation alone did not resolve it.
**Decision: narrow/stop at this budget**, not merge-ready. Next smallest correction
is to represent that dynamic QML signal access explicitly, followed by a separately
authorized bounded type-check round; do not weaken checker thresholds. Corrections
remain uncommitted in the three allowed files, and the 35-commit follow-up branch is
untouched. This is not a whole-repository CI pass or fresh performance evidence.

**User-directed continuation:** one final corrective pass is authorized after the
stop above: represent the dynamic QML signal access explicitly, then run one targeted
validation round (changed modules, Black, whitespace and focused lint/type checks),
zero native runs and no scope expansion. Preserve base failures. On success, publish
the focused PR #72 correction for review; merging still requires explicit user
approval. The old two-round budget remains recorded, not retroactively extended.

**Continuation result:** correction `36a5712` is committed and pushed to PR #72;
its worktree is clean. The final targeted round passed 55 tests, Black on three
files and whitespace; lint/typecheck retain only their confirmed base findings.
Fresh full CI run `39475133947` likewise reports only the base `stage_barbs`
complexity and optional-identity errors in the code-check jobs. The Python 3.13
build and Python 3.12 tests failed before execution because the Ubuntu GIS PPA
has no `resolute` Release file (apt exit 100); cancelled jobs provide no result.
Do not broaden this correction into CI infrastructure or claim a full-suite pass.
[PR review note](https://github.com/evanlivingston/gweatherrouting/pull/72#issuecomment-6298180755).
Merge approval remains pending. Incorporate the accepted correction into the later
viewport stack without rewriting shared history, resolving/validating any conflicts;
do not assume the old stacked HEAD already includes it.

**Issue reconciliation after accepted integration:** #66/#67/#68/#71/#73 are
candidates for scoped closure only after checking each implemented contract and
recording remaining epic-level limitations. #73 is currently **open**, with cleanup
recorded locally; implementation is not tracker closure. #64's original residency
acceptance remains open; #65 retains end-to-end resampling/fidelity questions;
#69 needs its standalone/default-path rescope reconciled explicitly. #70 has a
keep-experimental proposal awaiting the user, not default-on acceptance. If its
bounded validation/decision task is closed, unresolved acceptance must remain
explicitly owned and linked from #51. Do not silently delete criteria to close it.
Keep #51 open until its outcome is satisfied or explicitly revised by the user.

**Cleanup boundary:** no runtime/probe code was proven safely removable in this
audit. Active gate imports, compatibility accounting, CPU fallback, historical
experiments and machine-readable negative evidence stay. Remove a specific symbol
only after caller coverage and appropriate focused validation, not because it is
labelled legacy. No branch, worktree or remote issue has been removed/closed by this
documentation checkpoint.

### Earlier checkpoint notes (historical; not the current work queue)

The original baseline/experiment status below is retained as provenance. In
particular, its M0 and "not completed" wording must not override the accepted ADR
or the dated implementation/acceptance checkpoint above.

## Epic #51 acceptance checkpoint

**2026-10-20 decision:** substantial implementation progress, incomplete whole-epic
acceptance. The objective remains fluid **complete map/weather** interaction on the
user's forecast selection, not merely a green standalone mechanism gate.

**Evidence identity:** integrated `dev` is `bbbeac0` (PRs #72 and #75 merged).
The pre-integration viewport record was reviewed at `dc52123`. Its
`docs/profiles/rendering/2026-10-20/viewport-validation-summary.json` records
`commit=a008bbb46f9279f29738c64d7a43711fae4962d7`, campaign
`351eaa12c17e58f0ccb1438a27cec2cee333e072` and platform measurements
`b27d33ed1598a4b0d8b0dfc32a61ba8158501dfa`; do not attribute every measurement to
the later HEAD. Fixture hash:
`1ed1bca1281ad4096713ce20af9c1473e70bd4eb0766a54965849b7ace698a03`
(5,693 files; saved 22-source selection). Those candidate artifacts are now merged,
but not remeasured on the integrated base. Public record:
[#70 result](https://github.com/evanlivingston/gweatherrouting/issues/70#issuecomment-6296980259).
This checkpoint reuses evidence; it is not a fresh validation run.

| Acceptance area | Supported progress | Still open |
| --- | --- | --- |
| Architecture (M0) | ADR and gate runner; retained fields, async preparation, time windows and independent chart generation implemented in the candidate. | Whole-application acceptance is not implied. |
| Interaction (M1/M2) | Six candidate repetitions: each 0/120 pan, 0/24 zoom, 0/18 time inputs loading; three before and three after a soak. | These are not alternating baseline/candidate pairs. Availability is not cadence or latency; comparative improvement remains open. |
| Presentation | Viewport `afterRendering` proxy; app grab/readback diagnostic. | Presentation/input-to-present and refresh-budget frame times unmeasured; endpoints are not interchangeable. |
| Resources (M1/M4) | 21,216 steps/900 s plus 20,000-step soak; flat accounted store/memo/provider payloads. | Not RSS/VRAM or global resource stability. Plateau tolerance is unratified; zero evictions do not stress eviction. A fresh source-subset session is not in-session source churn. |
| Loading transitions | Cold ready 1,429.9 ms; coverage recovery 265.2 ms; interval recovery 172.9 ms, with explicit loading and hidden stale weather. | No ratified limits; a +12 h interval miss is not exhaustive model-run-boundary coverage. |
| Selection | Shared sampler separates model/member identities and direct cross-run temporal blending. | Accepted one-file/run/member display policy below is unimplemented; cross-file pairing and pointwise fallback remain permitted. Actual fixture mixing is unmeasured. |
| Numerical/mask fidelity | GPU agrees tightly with CPU interpolation of the same staged lattice. | Original-sampler resampling differences need reproducible U/V, speed, mask and provenance measurements; same-lattice arithmetic does not approve the approximation. |
| Portrayal/layers (M2/M3) | Fill/barb/chart controls, ordering and camera/time/weather preservation; measured chart publication improvement. | Visual verdict failed/deferred; glyphs, density, labels, masks and composition not accepted. Streamlines absent from this viewport. |
| Full workload | Saved overlapping sources and limited source-change/missing-region controls. | Marine-heavy and both-map candidate workloads not covered. |
| Platform/lifecycle | Metal/DPR 2 and resize; negative/non-equivalent OpenGL, Vulkan and software observations. | DPR change/device loss untested; resize is not complete docking/lifecycle coverage. |
| Layer state/fallback | Independent layer state; retained application's CPU renderer current in 24/24 frames. | Not automatic fallback/recovery inside the standalone viewport, which has no such fallback. |
| Rollout (M4) | #70 proposes keep-experimental; default application unchanged. | User approval required; no merge/default-on/epic closure follows automatically. |

Preserve negative evidence: pan dispatch lateness p95 48.7 ms/max 78.7 ms (not
presentation latency), unsupported backends, unratified bounds, deferred fidelity,
and #65/#69 attempt-budget overruns. No completion percentage substitutes for these
gates. GitHub's older opening status/checklists must be reconciled with delivered
scope before closing issues, not marked done merely because code merged.

## Decision log

The A0 slice contracts, budgets, gates, negative results and per-slice decisions,
in reverse chronological order. This log is the canonical record of the A0
program; the narrative version of the same evidence is
[`rendering-architecture-evaluation.md`](rendering-architecture-evaluation.md),
with the per-slice reports and the machine-readable summaries under
`docs/profiles/rendering/` alongside it. Do not start new A0 slices from here,
and do not read any single entry as a current work item — the live work program
is issues [#62–#70](../milestones.md).

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
  [A0k correction](rendering-a0k-shader-safety.md).
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
  Evidence: [A0j report](rendering-a0j-early-display.md).
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
  [restoration summary](../profiles/rendering/2026-10-19/fixture-restoration-summary.json).

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
  42 local links pass. Evidence: [A0i checkpoint](rendering-a0i-independent-numerical.md),
  [summary](../profiles/rendering/2026-10-19/a0i-independent-startup-summary.json).

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
  Evidence: [A0h report](rendering-a0h-resident-worker.md),
  [summary](../profiles/rendering/2026-10-18/a0h-resident-worker-summary.json).

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
  Full progress and limits: [A0g checkpoint](rendering-a0g-progress.md),
  [A0g summary](../profiles/rendering/2026-10-18/a0g-unattended-summary.json).

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
  Evidence: [A0f partial summary](../profiles/rendering/2026-10-18/a0f-persistent-summary.json).

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
  Evidence: [A0e follow-up summary](../profiles/rendering/2026-10-18/a0e-followup-summary.json).

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
  [A0e failure summary](../profiles/rendering/2026-10-18/a0e-coverage-load-summary.json).

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
  [A0d evidence](../profiles/rendering/2026-10-18/a0d-coverage-safety-summary.json).

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

## Native residency follow-up scope (#64)

**Status: proposed after user-authorized scoping, 2026-10-20.** One source/API
investigation pass; zero renderer edits, tests of rendering, or native attempts
in this scoping pass. Existing uncommitted acceptance work is retained unchanged.
The previous four-attempt extension is exhausted, not renewed by this proposal.

### Findings and ownership boundary

| Current owner | Source evidence | Consequence |
| --- | --- | --- |
| Numerical worker | `gribviewport.py: GribViewportModel`, `window_fields`; `gpuweatherfields.py: GeographicField`, `FieldStore` | Retains NumPy arrays, provenance, masks, packed QImages and lookup metadata. Its 128 MiB limit is accounted **CPU payload**, not native texture residency. Keep this implementation. |
| GUI image provider | `gpuweather.py: WeatherImageProvider.publish_field`, `_trim_fields`, `requestImage` | Retains QImages under stable URLs with a separate 32 MiB staging limit. Returning an existing URL does not allocate or pin a GPU texture. Its trimming is publication-order based, not the worker's LRU. |
| QML image slots | `WeatherBarbs.qml` and `WeatherFill.qml`, four `Image` objects with `cache: false`; `gribviewport.py: _publish_barbs`, `_publish_fill` | Bind the current field. Publishing nearby window fields to the provider does not bind them to separate native textures. Inactive fields have no explicit GPU-residency owner in product code. |
| Qt scene graph | `gpuscene.py: ImageItem.updatePaintNode`; prior `residency-extended-summary.json` | Qt creates/releases native resources and may atlas images. The existing map-image path shows the render-thread creation pattern, but does not implement a weather field store. A global atlas-disable diagnostic is not proof of default-path attribution. |

The installed `PySide6/QtQuick.pyi` exposes `QQuickItem.isTextureProvider`,
`textureProvider`, `updatePaintNode`, `releaseResources`, `QSGTextureProvider.texture`
and `textureChanged`, `QQuickWindow.createTextureFromImage`, `scheduleRenderJob`
and `sceneGraphInvalidated`. **Exposed signatures are not proof that Python
overrides and lifetime/ownership transfers work through ShaderEffect.**

Prior #71 evidence must not be mistaken for that proof:
`tools/experiment_float_transport_qsg.py: bind_texture_provider` passed a bare
QSGTextureProvider object, not a QQuickItem texture source; the recorded
`float_through_texture_provider` result failed. Its texture-factory experiment
also preconstructed textures and returned a shared pointer from factories, so it
does not establish safe production thread ownership or a byte-bounded native LRU.
Do not repeat the bare-provider approach or copy experiment tooling into product code.

### Recommended approach (conditional on probe)

**Add a render-thread-owned `NativeFieldStore`, rather than replacing `FieldStore`
or changing the weather calculations.** Use one store per QQuickWindow/scene-graph
generation, with explicit application-wide reservations for weather payloads.

- Key a native entry by source epoch + existing field identity + representation
  (barb nodes versus fill grid) + scene-graph generation. Never share GPU pointers
  between windows/devices. Independent map clocks bind independent entries.
- Retain the current RGBA8888 packed images, opaque channels, nearest sampling,
  shader byte decoder and interpolation/missing-data logic. Native float transport
  and original-sampler resampling are not reopened in #64.
- Create numerical field textures with `createTextureFromImage` on the render
  thread, without requesting atlas placement. Expose borrowed current textures
  through proper QQuickItem/QSGTextureProvider sources to the existing shaders.
  Keep per-view selection images separate. **Only numerical fields** bypass the
  atlas: no global `QSG_ATLAS_SIZE_LIMIT` change to weather selections or charts.
- Admission reserves a whole endpoint group before allocation; partial allocation
  rolls back. LRU evicts only unpinned native entries. Pin current/front/back
  consumers; speculation loses priority. CPU readiness alone must not be labelled
  GPU-current: source epoch, field, selection and GPU-ready state commit together.
- Proposed trial native-weather payload cap: **32 MiB application-wide**, including
  both maps and node/grid representations. This is a conservative configurable
  allocation choice, **not an empirically ratified working-set bound** and not
  the existing provider's CPU ledger. Count texture dimensions × four bytes with
  no mipmaps; include reserved, live and pending-retirement payloads without
  double-counting shared leases. Keep CPU decoded/memo/staging ledgers separate.
  `GpuPresenter.GPU_LIMIT` is a legacy presenter estimate; it does not currently
  enforce a shared viewport cap. Claim no whole-app VRAM/RSS bound.
- A reservation broker must never delete another window's resources on its own
  thread. Request retirement on that owner's render thread; do not admit a new
  allocation on credit for an unconfirmed release. If visible pins cannot fit,
  reject/defer explicitly; never silently thin, increase the limit, or present
  old weather as current. Oversized single fields report a recoverable budget
  failure rather than loading forever.
- Field eviction, source replacement, window destruction and scene-graph
  invalidation detach consumers and release native handles on the owning render
  thread. Invalidation clears the native epoch; CPU payloads may rehydrate it.
  GUI methods enqueue releases, never join workers or delete native textures.
  Track Qt native object lifetime separately from driver-deferred physical frees.

**Why not the simpler alternatives?** Stable URLs/`cache: true` and a retained pool
of QML Images still delegate eviction to Qt and provide no demonstrated native
quota. A QQuickTextureFactory supplies textures but does not, by itself, establish
safe shared ownership across changing Image consumers. Tracing atlas subregions
alone would improve measurement without implementing native residency. A native
C++ bridge may be needed, but adding its build/package pipeline is an explicit
scope decision, not an automatic fallback if the Python-item probe fails.

### Next step: compatibility probe, not renderer implementation

**Question:** can the installed Python bindings expose render-thread-owned packed
textures through a proper QQuickItem to ShaderEffect, retain A and B, and bind
**A → B → A** without recreating/re-uploading A or transferring its ownership to Qt?

**Probe authorized by the user (2026-10-20):** two small implementation/targeted-check
iterations and **two native processes, each <=120 s**. Synthetic packed fields,
no download, charts, sampler, worker scheduler or app-default change. Reuse #71's
recorded negative results, not its unsafe ownership shortcuts.

The probe lives only under `tools/` and targeted test files; it changes no product
renderer code. Use the installed shader baker, one QQuickWidget per native run,
and the existing explicit LLDB QRhi submission trace. Keep normal global atlas
settings. Known constant packed fields A=8 m/s, B=4 m/s and all-missing supply
independent finite/nonblank and zero-alpha controls. Observe native texture
destruction, not just Python references, on detach and actual scene invalidation.
Stop after the two attempts or on an unresolved ownership/dispatch failure; the
later implementation budget remains unapproved. Gate: `native-field-item` in the
existing runner. No presentation/physical-VRAM claim.

**Probe iteration 1:** 21 headless checks and Black on three files passed; native
attempt 1 completed cleanly but failed 2/16 gate checks (detach/reload). Proper
item/provider virtual dispatch, finite/missing pixels, non-atlased A/B/A identity
and upload controls passed: field submissions `[1, 1, 0, 1, 0]` for A/B/A/missing/A.
The scheduled AfterRendering retirement job did not run during the detached empty
frame; it ran after the next bind, invalidating the intended reload. Final scene
invalidation released all three observed textures, but that did not make detach
correct. Revise only the retirement handoff: consume a request in a direct
afterSynchronizing callback, after consumer detachment, with a guard against
deleting a bound texture. One targeted round and one native attempt remain.

**Final probe result — stop/defer adoption:** both authorized iterations/native
attempts are consumed. Headless selection: **21 passed**; final Black four files
and changed-document/tracked-tool whitespace passed. Attempt 2 exited cleanly
with persisted native exit status 0, but the gate failed **2/17** checks: native
resource release and ownership. Rendering, proper item/provider virtual dispatch,
normal global atlas settings, non-atlased textures, known finite/missing pixels,
byte-identical A return, native identity reuse and zero return upload all passed
in both runs. QRhi submissions were `[1, 1, 0, 1, 0]` for A/B/A/missing/A.

The afterSynchronizing handoff fixed detach: three textures were explicitly
destroyed after unbinding, and a new fourth texture reloaded A correctly. At actual
widget/scene invalidation, that fourth texture's Python wrapper was already
invalid before the owning bank could retire it, and its destruction callback was
not observed. Four creations versus three observed destructions **does not prove
a physical GPU leak**, nor does it identify Qt versus binding ownership as the
cause. It does mean the safe-lifetime contract is unestablished. Do not suppress
the check or infer ownership from a clean process exit. Callback operations all
ran on the same GUI/render-callback thread on this QQuickWidget host, not a
separately exercised threaded render loop.

**Next decision:** authorize a narrowly scoped investigation of Qt/Shiboken return
ownership and wrapper invalidation at the scene teardown boundary, or select a
different bridge design explicitly. The product implementation slice below is
**not started or authorized** by these partial mechanism results. A C++ bridge
and a further native retry need review; neither is an automatic next step.
Evidence: [native-field-item-summary.json](../profiles/rendering/2026-10-20/native-field-item-summary.json).

**Ownership investigation authorized by the user:** read-only audit first, then
at most two targeted validation/fix rounds and two native processes <=120 s if a
concrete fix is identified. Still experiment-only; no product integration/C++ bridge.
The installed PySide 6.12 `QtQuick.abi3.so` generated
`Sbk_QQuickWindowFunc_createTextureFromImage` calls `Shiboken::Object::setParent`
with the window wrapper and returned texture wrapper (both overloads; arm64 call
sites 0x52cf8 and 0x52de0). `shiboken6/include/shiboken6/basewrapper.h` documents
recursive child-wrapper invalidation on parent death. Installed
`typesystem_core_common.xml: QObject.setParent(QObject*)` explicitly updates the
wrapper's parent association. This is a concrete binding-lifetime hypothesis,
not proof that Qt deleted or leaked the native fourth texture.

Test the public `texture.setParent(None)` immediately after render-thread creation,
retain bank ownership, and keep all existing release checks. Record native parent,
Python ownership, wrapper dump before/after adoption, validity at retirement and
actual destruction callbacks. Require warm-positive/zero-return uploads, pixel
controls, explicit detach/reload and real scene invalidation to pass unchanged.
Stop at the budget or if public adoption cannot establish safe ownership. Do not
use private binding calls, pointer resurrection or waive the missing destruction.

**Ownership result — adopt the compatibility mechanism on this host:** the audit
and runtime metadata agree. The factory-return binding associated wrappers with
the window even though the actual QObject parent was null. Public
`texture.setParent(None)` removed the wrapper association and changed Python
ownership false → true without changing the native parent. Strong bank references
then survived window-wrapper invalidation, allowing explicit render-thread
destruction at scene invalidation. Keep that adoption invariant separate from
QObject parenting and from the application's source/scene-generation keys.

Two fresh native processes passed **19/19 checks each** (18.903 s and 17.014 s).
Each observed four creations, all four actual destruction callbacks, valid and
Python-owned wrappers at every explicit deletion, successful detach/reload, one
actual scene invalidation and persisted native exit 0. Existing finite/missing,
identity/byte-equality and warm-positive/zero-return upload checks passed unchanged:
QRhi submissions `[1, 1, 0, 1, 0]` for A/B/A/missing/A. Headless validation:
**23 passed**, Black three changed Python files and whitespace passed before this
evidence update. No product renderer was changed by this investigation, no
full suite or unchanged-baseline benchmark was run, and no physical VRAM,
presentation or general threaded-render-loop acceptance is inferred.

**Decision:** the specific wrapper-lifetime obstacle is resolved for the installed
Qt 6.12 Cocoa/Metal QQuickWidget path. Adopt this mechanism for the implementation
design; do not add a C++ bridge to fix a defect now resolved through the public
API. Both native slots are consumed. The older failed probes remain preserved in
their own evidence. The next separately authorized step is the bounded opt-in
native store below, including budget/eviction/two-map and generation/lifecycle
integration. **It is not started by this investigation.**
Evidence: [native-field-ownership-summary.json](../profiles/rendering/2026-10-20/native-field-ownership-summary.json).

Probe success requires: independently identifiable non-atlased field resources
with the normal global atlas settings; correct known finite/missing pixels;
unchanged field native identities and zero field upload submissions on return
to A after warmup; a working creation/upload positive control; and render-thread
cleanup on detach plus scene invalidation without crashes, double deletion or
leaked handles. Retained wrapper references are not ownership evidence. Record
whether each callback actually runs on the expected thread.

**Stop/review immediately** if virtual dispatch, ownership or safe render-thread
destruction cannot be established. Do not spend the next milestone's budget on
debugging this probe, switch to C++, or reopen float transport without approval.

### Production implementation plan (authorized 2026-10-21; N1 complete, budget consumed)

Planning pass requested 2026-10-21: documentation only, one changed-document
validation round, zero renderer changes or native runs. The subsequent user
instruction to get started authorizes the implementation and budget below.

**Authorized budget:** three targeted implementation/validation rounds and four
native application processes <=120 s each, including retries and subprocesses
launched by a gate. Four gate invocations that each launch several native children
would exceed this budget. The compatibility/ownership mechanism is adopted for
this measured host. This new budget starts at zero native attempts/targeted rounds;
the previous probe/investigation attempts remain recorded separately. N1 uses
headless lease/rollback controls and an early two-widget native gate, preserving
packed values, normal global atlas settings and known finite/missing pixels.
Use earlier ownership evidence as reference, not another unchanged baseline.
Stop on the stated scope conditions or exhausted budget.

**Outcome:** an opt-in native cache retains multiple prepared weather fields,
including inactive fields, and binds them without re-upload until explicit
eviction. Preserve the existing CPU store, selected file/run/member, independent
map clocks, shader arithmetic and per-view selection. No new renderer technology,
native-float format, chart work, default switch or changes to routing/#79.

Work in three coherent slices on the #64 feature branch; keep prior probe/cleanup
work distinguishable from the product change. Coordinate with #78 only at shared
viewport/QML binding sites; never include its chart migration. Do not merge or
push unrelated work or claim the earlier partial gates close #64.

#### N1 — Explicit native ownership and accounting

- Add a production-only module, not an import from `tools/`. Separate a headless
  reservation/LRU policy from render-context objects so accounting can be tested
  without a GPU. GUI/worker code hands off immutable image payloads and epochs;
  native texture creation, adoption and destruction remain render-thread-only.
- Create **one weather reservation owner for the application/session**, injected
  into both map presenters and their per-window stores. Do not independently give
  each map a full 32 MiB allocation. A standalone viewport owns a single equivalent
  session budget. Native texture pointers never cross contexts.
- Use exact field identity plus source/scene epoch and node/grid representation
  as the cache key. Current selection/view revision is a binding token, not the
  field's residency key. Atomically reserve/create all four endpoint textures or
  roll back; use the tested public `setParent(None)` adoption and validate ownership.
- Track reserved/live/pending-retirement payloads; transfer each byte between
  categories once. Pin visible and in-flight consumers. Evict inactive local LRU
  entries before refusing visible demand; request another context's unpinned
  retirement only through that context, and wait for acknowledgement before
  counting its bytes as available. Denied speculation must not disturb current
  content. A too-large field must fail explicitly, not wait forever.
- Reuse the tested afterSynchronizing retirement handoff and actual invalidation
  cleanup; do not assume AfterRenderingStage jobs run during an empty QQuickWidget
  frame. Hidden/no-next-frame retirement needs a tested disposal/invalidation path,
  not GUI-thread deletion or credit for a release that has not happened.

**Gate N1:** headless admission/rollback/pinning/cross-context accounting tests,
then an early native synthetic control using **two QQuickWidgets in one process**:
known pixels, separate native identities, a shared enforced cap and safe teardown.
The old `_Session` documentation in `test/qt_gribviewport_checks.py` reports a
second-widget rendering limitation. Exercise it directly; two independent
processes are not a substitute for proving two maps in the same application.
If that limitation appears and requires a host/controller rewrite, stop before N2.

**N1 checkpoint, iteration 1:** new production headless reservation/store policy
and explicit Qt adapter implemented locally; not yet wired into the default app.
36 targeted policy/control checks passed, Black four files and whitespace passed.
Native process 1 completed cleanly, but failed the early two-widget known-pixel
check: the first widget uploaded four endpoints and drew correctly; the second
allocated four native objects but never met the harness's uploaded-handle/shader
readiness predicate. Both contexts destroyed all four objects and the shared
ledger ended empty. Pressure/reload cases were not run, so native eviction is
not accepted. Diagnose harness paint/readiness ordering before attributing this
to the host. The correction forces framebuffer generation before waiting for
uploaded handles (fidelity only, not FPS), and records both GUI/native readiness
and shader state. N2 remains unstarted. Next run debits process 2 of 4 and
targeted round 2 of 3; do not silently bypass the two-widget check.

**N1 iteration 2 — narrow/stop:** three new headless gate-oracle tests, Black two
files and changed-document whitespace passed. Explicit framebuffer generation
did not resolve the early gate failure. Native process 2 exited 0, but the second
widget reported native-ready/QML-field-ready **true** and shader-compiled **false**,
with zero field upload submissions; the first widget drew known pixels and
uploaded all four endpoints. No initiating exception or ownership error appeared.
Both contexts observed four creations/four destructions and one actual scene
invalidation; the shared ledger ended with zero reserved/live/retiring bytes and
zero registered contexts. Native pressure/reuse/reload cases were not exercised.

The new headless policy and explicit native context adapter are local candidates,
not app integration. The symptom resembles the old harness's recorded
first-widget-only shader limitation; its root cause is still unclassified, not
proof that a host rewrite is required. **Stop before N2/N3.** Two of four native
processes and two of three targeted rounds are used; the stop is the milestone
gate/scope boundary, not exhausted budget. Next smallest decision: isolate a
minimal two-widget shader-readiness control with geometry/status/provider
observations and determine whether this is harness or Qt lifecycle. Do not
substitute two processes for the requirement or spend the remaining budget on
broader renderer/controller changes without review.
Evidence: [native-weather-N1-summary.json](../profiles/rendering/2026-10-21/native-weather-N1-summary.json).

**User-authorized continuation of N1 diagnosis (2026-10-21):** use the remaining
one targeted round and at most two native processes (3/4 and 4/4 overall), not a
new budget. Native process 3 is diagnostic only: capture widget/root/effect
geometry, visibility, raw shader status/log and provider dispatch. Independently
try explicit root geometry, a known-color non-shader rectangle, reactivation of
the original shader URL, and identical QSB bytes at a distinct URL, in the same
two-widget process. Preserve the original gate failure even if a diagnostic
intervention works. No host rewrite, backend/atlas change or separate-process
substitution. Only a concrete narrow correction identified by those controls may
be applied/tested in process 4. Otherwise stop with the remaining cause unclassified.

**Process 3 diagnostic result:** original second-widget geometry was already
64×48 with visible root/effect, empty shader log, four dispatched providers and
actual native texture handles. It drew the correct A pixel `[255,64,128,255]`
while reporting `ShaderEffect.Uncompiled`. Explicit geometry and reactivating the
original URL did not change that metadata. A known-color Rectangle independently
rendered correctly. Identical QSB bytes at a distinct URL changed the shader
status to Compiled but did not change the already-correct pixel result. The
original gate remains FAIL: its status-only readiness condition was overstrict,
not evidence of an unrenderable second widget.

**Final allowed correction:** keep the original shared shader URL and normal
global atlas settings. Readback readiness requires native endpoint handles and
no explicit shader error; independent known-pixel/field-upload gates remain
mandatory. Add headless controls for cached Uncompiled metadata versus errors or
missing resources. The synthetic missing control uses the existing A0k integer
exponent rejection before float arithmetic. Process 4 must exercise the full N1
reuse/pressure/reload matrix, not diagnostic substitutions. Targeted round 3 and
native process 4 exhaust the current implementation budget; no N2 start follows
automatically even if this milestone passes.

**N1 completion — adopt foundation, stop before N2:** process 4 passed **16/16**
checks in 17.002 s with persisted native exit 0, original shared shader URLs and
normal global atlas settings. Both widgets drew independently. Four-endpoint
field upload submissions were `[4,4,4,0,4,4,4]` for left A, right A, left B,
resident return A, pressure C, evicted B reload and all-missing. Native reservation
serials `[1,2,3,1,4,5,6]` separately prove reuse versus reload even when allocator
addresses are reused. The sibling's pinned A retained its handles/pixels while
the left store evicted inactive fields. Shared payload peak was exactly the
384-byte test cap (three 128-byte groups), with three evictions. All 24 native
objects were destroyed (20 left/4 right), both contexts invalidated, and the
ledger ended with zero reserved/live/retiring bytes and registered contexts.
Final targeted validation: **45 passed**, Black five files and whitespace passed.

**Correction to prior failed-run interpretation:** an empty mapped handle set
produced the earlier reported zero upload count when the compiled-status guard
returned no group. That was unattributable, **not a measured zero**. The diagnostic
proved real second-widget rendering despite stale Uncompiled metadata; no engine,
global atlas, shader URL or host-architecture workaround is needed for N1.
Older failed verdicts remain preserved, with this correction linked. Do not use
the old generic "second widget never presents" hypothesis as a plan. N2 must
handle cached status honestly instead of labelling a valid native frame loading
solely because `ShaderEffect.status` did not transition to Compiled.

**Reevaluation:** the production component's native reuse, admission/pressure,
two-context isolation and ownership mechanism are supported for this synthetic
workload. This is not full application/barb/fill/sampler fidelity or performance
acceptance. All three targeted rounds and all four native processes are used.
N2/N3 remain unstarted; request their next bounded integration budget rather
than starting them provisionally. No product default or rollout change.
Evidence: [native-weather-N1-completion-summary.json](../profiles/rendering/2026-10-21/native-weather-N1-completion-summary.json).

#### N2 — Opt-in application and layer binding

**Authorized 2026-10-21:** three targeted validation/fix rounds and four native
application processes <=120 s each, including retries. No default switch, #79
change, chart migration or routing/sampler policy change. Use the existing local
real HRRR download read-only, with checksum recorded, and synthetic missing-data
controls separately. Gate the actual viewport/presenter layers, not just scalar
probe shaders. Baselines unchanged by N2 are not rerun. Stop at the budget or
scope expansion. N2 cache consumers are separate node/grid binding owners on
the same render window sharing one session ledger; never duplicate the cap for
layers or maps. Keep admission, native frame observation and publication counts
separate, and do not gate valid cached frames solely on Compiled status.

- Add a distinct `--native-weather-fields` option (and matching explicit environment
  opt-in), default OFF, to the Qt/standalone entry points. Do not reinterpret the
  existing `--gpu-resident-fields` legacy-frame flag. The normal default remains
  its current viewport/Image path, with legacy recovery retained.
- Pass the shared budget and native option through controller → WeatherPresenter
  → viewport. Add endpoint texture-item bindings to existing barb/fill QML behind
  the option; keep the Image bindings intact when it is off. Do not enable the
  standalone numerical fill in the normal app as a side effect of this work.
- Introduce an immutable native binding request: source/scene epochs, exact field
  identity, endpoint group, selection generation and current demand. Stage/lease
  native endpoints before committing the selection. Show current only when all
  required native textures are admitted and the matching selection is ready;
  reject stale native completions just as stale worker completions are rejected.
- GPU-resident windows are an **admitted set**, not every field returned by the
  CPU `window_fields()` accessor. Warm eligible fields in bounded order, then
  expose readiness/observations for that set. A CPU hit whose GPU entry was evicted
  requires a counted GPU reload, not a false zero-upload claim or GRIB re-decode.
- Field changes bind retained endpoints; camera/time-uniform updates do not
  recreate them. Node/barb and grid/fill representations share the same ledger
  while remaining separate texture groups. Selection image updates retain normal
  atlas behavior and remain outside field-upload counters.
- Wire source replacement, suspension, hide/show, widget teardown and scene-epoch
  invalidation to the native owner. Hide obsolete weather immediately, detach
  consumers, retire safely, and ignore queued old acknowledgements. No GUI join.
  Carry explicit allocation/backend failure through existing honest recovery.

**Gate N2:** targeted presenter/model/QML regression tests plus real-HRRR native
checks of covered pan, within-bracket interpolation and resident A → B → A.
Require positive cold uploads, zero warm field uploads/re-decodes, correct
source identity and independent fill/barb bindings; keep chart rendering unchanged.

**N2 completion — adopt opt-in integration, stop before N3 (2026-10-21):**

- `--native-weather-fields` / `GWR_NATIVE_WEATHER_FIELDS=1` now propagate through
  the controller/presenter to the viewport. One controller-owned 32 MiB native
  payload cap is shared across application maps. The standalone fill remains
  standalone; neither default behavior nor packed shader arithmetic changes.
- Native requests retain immutable source/scene epoch + exact field identity
  keys and sequence rejection. Selection publication is synchronous on the GUI
  thread; queued native acknowledgements update readiness only and cannot publish
  an old selection. Readiness separates admission from observed native handles
  and rejects explicit shader errors without relying solely on Compiled metadata.
- Admission is demand-driven: CPU `window_fields()` does not imply GPU residency
  and is not eagerly uploaded. Separate node/grid consumers share one ledger;
  active and retained native groups are exposed by `native_observations()`.
- Source removal/replacement, suspension and hidden weather retire the native
  widget asynchronously. Widget destruction supplies the scene invalidation
  boundary even when a hidden widget cannot synchronize. Presenter generations
  reject delayed callbacks from retired hosts; existing legacy recovery remains.
- Native processes 1–4 used the existing local HRRR file read-only, SHA-256
  `16e21c3cf2747a364f028a2aae0aaa0640d3a84051e9f6ef6c737c990698a6a3`.
  Process 1 passed presenter checks but failed the layer harness because its model
  omitted `grid=True`; it remains FAIL. Process 2 passed 15/15, process 3 passed
  15/15 with two real application maps, and process 4 passed the expanded 18/18
  including distinct real forecast brackets A → B → A in 22.838 s; native exit 0.
- Final field upload submissions were **8 cold, 0 covered time/pan, 8 new bracket,
  0 return to A**. Decoded-band counters stayed unchanged during covered and return
  phases. Finite framebuffer difference was **0 bytes**, and all-missing alpha was
  **0 pixels**, using the shipped barb/fill shaders. Selection traffic is excluded
  from field attribution; missing current native handles yield unavailable counts,
  not numeric zero.
- Two live application map groups shared 92,160 B. The separate two-layer
  comparison/control workload peaked at 571,200 B under the same declared 32 MiB
  cap and released all 24 textures, ending at zero bytes/groups/contexts.
  This is payload accounting and upload submission/readback evidence, not physical
  VRAM, presentation FPS, end-to-end latency or a demonstrated workload-fit bound.
- Targeted round 1 passed 73 tests. Round 2 passed 82 but failed two new harness
  tests on tool-module imports; the fix uses the repository's existing explicit
  module loader. Round 3 passed all four affected integration tests (0.63 s),
  Black on 11 files and changed-file whitespace checks. This leaves 84 unique
  targeted tests supported by passing results (82 unchanged round-2 results plus
  the two repaired tests). No fourth round or fifth native process is authorized.
- Evidence: `docs/profiles/rendering/2026-10-21/native-weather-N2-summary.json`;
  full failed/passing artifacts remain under the approved temp directory in
  `native-weather-N2-1/` through `native-weather-N2-4/`.

**Decision: continue only to separately authorized N3 acceptance/review.** The
product opt-in is usable; the broader forced eviction/reload, source/provenance,
numerical oracle and scene-recreation matrix below remains open. No commit, push,
PR, merge, default switch, chart migration or #79 change was made by N2.

Reproduce N2 on Cocoa/Metal with the installed LLDB trace support:

```sh
PYTHONPATH=. GWR_NATIVE_WEATHER_GRIB=/path/to/forecast.grib2 \
  python tools/gate_runner.py --gate native-weather-integration --output /path/to/results
```

#### User-authorized default switch (2026-10-21, after N2)

The user explicitly requested the native-cache viewport as the default, rather
than requiring a flag. Both launcher entry points and direct controller/viewport
construction now resolve an unspecified native option to enabled. An explicit
False/`--no-native-weather-fields` overrides the environment; otherwise
`GWR_NATIVE_WEATHER_FIELDS=0` provides Image-path recovery. The positive option
remains accepted for compatibility, but is no longer required. Existing legacy
weather recovery and the CPU renderer remain available; numerical fill/chart
flags and calculations are unchanged.

Scope: default selection and recovery precedence only. Validation budget: one
targeted headless round plus Black/whitespace on changed files; **zero new native
processes**, relying on N2's passing current-renderer evidence, not remeasuring it.
N3 remains open, and no expanded fidelity/performance acceptance is claimed.
Record this later rollout choice separately from the preserved default-OFF N2
evidence; no commit/push/merge is authorized by it.

Validation passed: **95 targeted tests**, one unchanged pre-existing #77 test
deselected, Black on six changed Python files, and changed-file whitespace checks.
The round took 7.29 s; no native rendering process or baseline was rerun. Native
rendering evidence remains N2's 18/18 gate. N3 acceptance is still unstarted.

#### N3 — Acceptance and review, not rollout

**Authorized 2026-10-21 after the default switch:** three targeted validation/fix
rounds and four native application processes, each <=120 s including retries.
Scope is the remaining #64 acceptance matrix and NaN overlay triage; #78/#79 and
sampler/source policy fixes are non-goals. Use the saved N2 HRRR checksum and small
synthetic missing/pressure controls. Preserve N1/N2 results instead of remeasuring
unchanged baselines. First integrate current `dev` chart changes into this feature
branch without changing chart behavior. Require attributable positive/zero QRhi
uploads, CPU payload/mask/provenance/selection equality, pressure-derived budget,
honest misses, independent clocks, replacement/scene-epoch rejection and complete
cleanup. Retain the 2e-5 numerical bound and disclose the pre-existing #65 oracle
dependency. Stop on budget exhaustion or any required unrelated policy rewrite.
At the gate record adopt/revise/reject and continue/narrow/pivot/stop. The user
authorized coherent commits, push and a normal fork PR to `dev` after acceptance;
**merging remains unapproved**. The default-on choice is already user-authorized,
superseding the original default-OFF scoping text below, not a new N3 change.

- Extend the gate runner with a final `residency-native` acceptance gate. Separate
  native field allocations/binds/releases and actual QRhi upload submissions from
  the existing publication-based counters. Positive trace controls must fail
  closed if the debugger/instrument is unavailable or attribution is ambiguous.
- Exercise forced eviction/reload with a test budget derived from actual field
  sizes, source replacement with stale completions, honest geographic/time misses,
  independent clocks, hidden/suspended views, actual scene invalidation and final
  destruction. Include zero-alpha all-missing and unchanged finite controls using
  the production packed shader paths, not just the scalar compatibility shader.
- Compare cached/reloaded fields with the unchanged CPU reference: endpoint bits,
  missing masks, provenance, native point membership and selection anchors/counts.
  Preserve the existing 2e-5 gate wherever GPU numerical comparison applies.
  This change must not introduce or hide the separate original-sampler resampling
  discrepancy tracked in #65; record any acceptance dependency rather than waiving it.
- Record native payload high-water and final zero outstanding reservations/handles
  for both maps. Do not label that physical VRAM, FPS or an end-to-end speedup.
  Retain the current default/legacy recovery and publish a focused review PR only
  when authorized; no merge or default-on rollout as part of acceptance.

**Finish line:** one gate-runner command over the final native path, with all
required workload cases/current-code evidence and explicit native process counts.
Planned command **after the new gate is implemented**:

```sh
GWR_RESIDENCY_FORECAST=/path/to/forecast.grib2 \
  python tools/gate_runner.py --gate residency-native --output /path/to/results
```

Retain usable matching evidence for unchanged modules rather than remeasuring
their baselines; rerun affected cases after a fix. The fourth native slot is the
maximum retry allowance, not permission to exceed the total when a case launches
multiple children. If required cases cannot all be validated inside the budget,
stop and report the unclosed cells. Only passing the complete agreed acceptance
below makes #64 eligible for closure; a compatibility gate or partial native
control is not sufficient. Before closing, reconcile the issue's older
chart-inclusive `residency` command with the new gate explicitly, preserving its
original failing evidence and thresholds, not silently swapping the definition.

Likely file boundaries (new names are proposals):

| Files | Change |
| --- | --- |
| New `gweatherrouting/qt/gpuweathertextures.py` | Native store, reservation broker, render-thread provider items and resource telemetry. No GDAL or sampler handles. |
| `gweatherrouting/qt/gribviewport.py` | Immutable resource demands/leases, readiness, source/scene epochs, invalidate/teardown; retain worker and existing CPU store. |
| `WeatherBarbs.qml`, `WeatherFill.qml` | Opt-in texture-source bindings; preserve shader arithmetic, selection membership and existing default Image path. |
| `gpuweather.py`, `gpuweatherfields.py` | Minimal identity/accounting handoff if needed; preserve encoder and CPU payload semantics. |
| `weatherpresenter.py` | Flag propagation and replacement/hide/show cleanup only if the existing lifecycle API cannot carry leases. No controller rewrite. |
| `qt/controller.py`, `qt/__main__.py`, viewport standalone entry point | Shared session budget injection and the distinct opt-in native-fields option; preserve old flag meaning and independent clocks. |
| `tools/gates.py`, residency tools, targeted tests | Final field-specific upload/lifetime/eviction gate and evidence. Keep old failing historical gate evidence; no threshold relaxation. |

New native path remains **opt-in/default OFF** until human review; retain explicit
legacy recovery. No routing, source/run/member policy, forecast acquisition,
portrayal density (#79), glyph redesign, new fill technique or chart migration.

Acceptance must separately establish:

1. Cold A/B admission records expected positive uploads. Covered pan, same-bracket
   time change and resident A → B → A record zero **field** uploads/re-decodes;
   selection uploads remain separately visible. Do not call a CPU-window member
   GPU-resident until its native admission has completed.
2. Force the native budget to hold A+B but not A+B+C; pin visible A, evict inactive
   B, load C, and return to B. Verify the expected reload, unchanged endpoint bits,
   masks/provenance and original reference values; live+reserved+retiring accounting
   stays within the declared cap. CPU decode-cache hits and GPU reloads are distinct.
3. All-missing native rendering contributes exactly zero alpha; finite controls
   stay byte-identical; existing 2e-5/original-sampler and selection gates remain
   unchanged. Record pre-existing #65 fidelity gaps separately, not as passes.
4. Source generation rejection, outside-window honest loading, two independent
   map clocks/contexts, repeated replacement, hide/show, teardown and exercisable
   scene invalidation pass. Unexercisable device loss remains an explicit gap.
5. With **normal global atlas settings**, each new numerical texture is non-atlased
   by design, identified at the QRhi upload boundary, and trace positive controls
   pass. Counters at creation/destruction remain distinct from measured upload
   submissions. Byte accounting is payload/estimated storage, not physical VRAM.

Use one final gate-runner command for the new native path, with cached real-HRRR
identity/checksum recorded; reserve small synthetic controls for forced pressure
and missing data. Do not remeasure the unchanged chart-inclusive scrub baseline
or run the full suite. Stop at budget exhaustion and record adopt/revise/reject
before any rollout decision. **#64 remains open throughout scoping/probing.**

**Implementation stop conditions:** a C++ extension/build pipeline, sampler or
source-policy change, global atlas disabling, widening #78/#79, a controller/host
rewrite to make two maps render, unsafe ownership on another callback path, or
budget exhaustion. Report the smallest concrete next decision; do not substitute
two separate processes for the two-map requirement or silently raise memory caps.
