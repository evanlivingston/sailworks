# Complete faithful ENC viewport integration (#78)

Issue: https://github.com/evanlivingston/gweatherrouting/issues/78, under #51.
Experimental integration only; default use/rollout is separate from this issue.

## Implementation contract (before implementation/runs)

**Question:** can the independent viewport present complete installed/enabled ENC
charts with the authoritative selection, coverage clipping and Cairo portrayal,
while its camera/weather continue independently of chart preparation?

**Scope:** worker-private ENC handles, original rank/visibility/coverage/portrayal,
correct DPR, explicit coverage/loading states, bounded latest-wins preparation,
safe cancellation/close/source replacement, and experimental viewport wiring.
Reference semantics live in `rendering/encrenderer.py`, not in the unpublished
experimental Qt Shapes pipelines. Reuse faithful composition where appropriate;
no raster-resampled zoom frame may be reported as current, and no background-only
image may count as successful ENC. Screen-space labels require exact-scale
composition after zoom; current pan-only imagery can retain translation.

**Non-goals:** default switch, routing or weather calculation/source-selection
changes, downloader/catalog policy, full S-52, custom tessellation, font/style
changes or importing `tools/` into the product. Preserve uncommitted experiments
in the sibling worktree. No commits/push/PR/merge without authorization.

**Baseline/workload:** `dev` c3a6f3a, verified private snapshot SHA256
`1ed1bca1281ad4096713ce20af9c1473e70bd4eb0766a54965849b7ace698a03`, all
enabled chart sources, detailed SF/Puget views, real multi-cell boundaries and
synthetic coverage/holes/scale-ranking negatives. Original rendering is the
pixel reference; existing weather evidence is not remeasured as an unchanged
baseline. Native readbacks outside timed inputs at 710x743 logical/DPR 2/Metal.

**Gates:** targeted tests of source selection, exact worker composition,
latest-wins cancellation and cleanup; native one-command `enc-viewport` gate for
real ENC/nonblank/identity/multi-cell masks and faithful chart/weather composition.
Use existing R0.1 MAE <=1/255, p99 <=8/255 and annotation silhouette/coverage
criteria, exact-zero-alpha checks; preserve source/feature/label semantics.
Deliberately stall chart preparation while camera/weather advance; report loading
and settling, not phantom responsiveness. Account retained payloads separately
from unmeasured RSS/VRAM. Timing is callback/wall evidence, not presentation FPS.
No invented accepted speed threshold. NOT-EVALUATED is not success.

**Delegation:** Kilo Auto Free creates ticket, implements and independently audits
fidelity/coverage. Primary owns scope/review/integration/gate selection. Only the
configured free test runner executes checks/native commands; no paid fallback.

**Budget/stop:** issue phase 1: <=4 targeted validation/fix rounds, <=3 native
processes of <=110s. At the gate explicitly choose continue/narrow/pivot/stop;
phase 2 integration acceptance is <=3 rounds/2 native processes <=120s if phase 1
supports continuing. Phase 3 is evidence/report only, no remeasurement. Stop and
report on budget exhaustion, missing fixture/API, scope expansion or any need to
relax gates. Correctness, native fidelity and whole-experience performance are
separate outcomes. Completion requires honest coverage of acceptance criteria;
do not close #78 because the integration merely draws a coastline.

## Phase 1 amendment (recorded before any run)

The user directed complete integration and the rejected alternate representations
(DPR-2 resampled raster, per-label outlines, constant cosmetic strokes) already
supply selection context. Phase 1 therefore no longer searches representations or
forbids product integration. It composes the **reference Cairo portrayal and
coverage kernels** at the **exact current scale and DPR**, inside the viewport's
own scheduler, behind a **disabled-by-default** flag:

- the reference renderer and the coverage/ranking/styles/draw-order/label rules
  are unchanged and remain the oracle;
- a retained image is valid only for the current camera — **a fractional zoom
  never publishes a previous scale's image as current**, and re-prepares the
  faithful current-scale output asynchronously (latest-wins), with explicit
  loading/stale/coverage-miss states meanwhile;
- no experiment tooling is imported into `gweatherrouting/`, no legacy worker
  scheduling is reused, and no custom vector/tessellator/stroker is introduced.

Acceptance A/B/C, the fixed image bounds, the experimental status, the budget
(4 validation/fix rounds, 3 native processes <=110 s) and the
`enc-viewport` one-command gate are unchanged. No run had executed under the
earlier draft wording, so nothing was discarded or re-measured by this amendment.

## Current state

### Functional scope revision authorized by the user

The user deferred exact visual matching in favor of charts displaying reliably
and independent weather/lifecycle behavior. The original `enc-viewport` fidelity
gate is unchanged. New `enc-viewport-functional` enforces its runtime checks and
complete view coverage without claiming the deferred pixel bounds passed.
Hypothesis: a weather miss hides the shared QML scene root, inadvertently hiding
ENC; keep the ENC scene visible and hide only weather children. Permit initial
chart preparation after a terminal weather miss, still yielding to active weather
work. Defaults and portrayal kernels remain unchanged.
Budget: two focused rounds, at most two native processes <=110 s each; stop on
remaining functional failures. Use the same fixture/SF/Puget/multi-cell cameras,
reversed repetitions and blocked-worker lifecycle probe. No new baseline timing,
rollout, full suite or performance claim.

### Functional outcome — PASS; decision: stop and offer review

The first revised round passed `enc-viewport-functional` (13/13 checks,
39.98 seconds process wall time, **not FPS**). Evidence is summarized in
`profiles/rendering/2026-10-20/enc-viewport-functional-summary.json`. Native raw
artifacts are under the approved OpenCode temporary directory in
`enc-viewport-functional-1/`.

- SF, Puget and the real multi-cell view displayed at Metal/DPR 2 in both
  forward and reverse orders. Selected cell/region identities matched.
- The weather-miss bug was the shared scene root being hidden; ENC now keeps
  that root visible and withdraws only weather children. A terminal weather
  miss no longer prevents an initial ENC request. Active weather still takes
  precedence over chart preparation.
- The deliberately blocked chart remained loading while camera and weather
  advanced. Old imagery was withdrawn, superseded work rejected, and resources
  released. All requested views completed; no forced readiness was used.
- Although exact matching was deferred by the user, all six recorded image
  comparisons happened to have MAE/p99 zero and annotation matching fraction
  1.0. This is measured evidence for these views, not broad catalogue/platform
  acceptance or a separate rerun of the strict gate.
- Targeted tests: 102 passed; one unchanged, previously recorded base failure
  (`test_the_application_never_imports_the_viewport`). Black (four changed
  Python files), tool/helper flake8 and scoped whitespace checks passed.

This completes the narrowed functional display/lifecycle task. Default rollout,
combined-experience/performance acceptance, full catalogue/platform testing and
Phase 2 remain separate. No commit, push, PR or merge is authorized or performed.

### User-authorized check completion

After three native attempts (the first two aborted in the harness), the user
authorized completing the outstanding checks. Scope: repair the oracle/readback
alignment and selection probes, exercise real multi-cell boundaries, and verify
stale imagery is withdrawn during a blocked exact-scale replacement. Budget:
two targeted validation rounds, at most two native processes of 110 seconds.
Use the same fixture and unchanged fidelity limits; no rollout or performance
claims. Stop after the second round if any acceptance criterion remains open.
The earlier MAE result is not accepted chart evidence: the harness could read
the previous camera before its queued GUI publication completed.

### Check-completion results — decision: stop, Phase 1 not accepted

The two authorized validation rounds/native processes are exhausted. Native
artifacts are in `enc-viewport-run-4/` and `enc-viewport-run-5/` under the approved
OpenCode temporary directory. The fixture matched the recorded SHA in both.

- Targeted checks: 99 passed, one known base failure
  (`test_the_application_never_imports_the_viewport`, unchanged
  `weatherpresenter.py` import). Changed-file Black, tool flake8 and whitespace
  checks passed in both rounds.
- Fixed harness camera/publication waiting, premultiplied readback format,
  independent clipped-region selection, annotation identity and pixel comparison,
  real multi-cell checking, and the previously ignored reversed repetition.
- Fixed product stale-image withdrawal: exact-scale/time replacement clears the
  displayed source immediately. Run 4 observed stale hiding, latest-wins and
  resource release passing. Run 4's weather-stall probe failed to force a new
  composition during retained pan; run 5 explicitly forces it.
- Run 4 (27.8 s): Puget and real multi-cell view matched pixel-for-pixel; SF
  readback was transparent while the weather viewport was hidden. The latter is
  invalid visual evidence, not an accepted portrayal mismatch or speedup.
- Run 5 (45.9 s): Puget and real multi-cell views matched pixel-for-pixel in both
  reversed repetitions (MAE/p99 zero, silhouette/ink/annotation coverage 1.0).
  Selected sources and clipped-region identities matched. SF failed the added
  visible-weather readiness condition in both repetitions, so SF visual evidence
  remains **not evaluated**. The gate remained FAIL.
- Because SF was incomplete, run 5 did not enter its behavioral stage. Its four
  false behavioral defaults are **not evaluated**, not four demonstrated product
  regressions. The forced-stall correction therefore remains unverified.

Remaining blockers: diagnose SF readiness without forcing or bypassing it;
obtain complete matched SF readbacks; run the corrected forced-stall probe and
repeat lifecycle checks in the same complete run. No further run is authorized
by this budget. No Phase 2 entry, rollout, performance acceptance, commit or issue
closure is claimed. Normal app defaults remain unchanged. Sibling uncommitted
experiment work is preserved.
