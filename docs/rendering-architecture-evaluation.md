# A0: compare coherent interaction architectures

**Status:** historical evidence. The A0a-A0k slices below are retained as the
experiment record, including the negative results. Planning and decisions now live in
[`adr-001-grib-rendering-architecture.md`](adr-001-grib-rendering-architecture.md) and
epic #51; the implementation program is issues #62-#70. Do not start new A0 slices
from this document.

Status: experimental candidate, not a production design selection. Tracking:
[#51](https://github.com/evanlivingston/gweatherrouting/issues/51).

## Latest checkpoint — A0i blocked by unavailable fixture inputs

A headless numerical-service draft is implemented, but its first real-data
attempt rejected the original fixture before launching numerical or chart workers.
Initially 4,036 recorded inputs were missing, including all GRIB-directory files.
Authorized recovery has since restored 4,006 exact files, with the manifest and
existing inputs preserved. Thirty marine cache metadata sidecars still lack
exact local copies. The source of the loss is unknown; no fixture was overwritten
or silently replaced. Full verification remains blocked; see the
[partial restoration summary](profiles/rendering/2026-10-19/fixture-restoration-summary.json).
This historical recovery blocker is superseded by the user's authorization for a
new current-data baseline. Its first headless numerical/chart-startup mechanism
passes, without yet integrating early live display; see the
[current A0i report](rendering-a0i-independent-numerical.md).

Restore the original verified inputs or explicitly authorize a new versioned
fixture/oracle before validating independent numerical startup. Cached A0h output
arrays cannot replace the original input forecasts for a startup/decoding test.
See [A0i checkpoint](rendering-a0i-independent-numerical.md) and
[summary](profiles/rendering/2026-10-19/a0i-independent-startup-summary.json).
No early numerical-display integration or startup improvement is established.

## Previous checkpoint — A0h

A0h adds a real single-owner numerical resource worker and separates measured
input from oracle/readback checks. Two matched source-reopening/resident pairs
(reversed order on pair 2) pass identical input, native A→B→A, ownership and exact
deferred fidelity gates. Retained-source preparation is 1.18–1.31 s vs 1.79–1.96 s
for the reopening control; measured resident GUI publication is 2.2–5.9 ms.
No guarded explicit readback or numerical oracle ran in a measured window.
Full oracle checks remain after measurement on the unchanged assets (400 numerical
comparisons across four cases). Empty-only reference correction remains explicit.

The legacy application still prepares charts and the out-of-window oracle; ~31 s
startup and remaining chart/CPU weather work are not solved. The old ~295 ms tail
does not recur in this sample, but heartbeat/render signals are not presentation
and broader tail behavior is unproven. Global memory and scrub/layer/zoom acceptance
remain open. See the [A0h report](rendering-a0h-resident-worker.md) and
[summary](profiles/rendering/2026-10-18/a0h-resident-worker-summary.json).
Decision: support numerical ownership/clean timing, not whole-architecture rollout.

## Previous checkpoint — A0g

The earlier A0f second-destination blocker is resolved in experiment code:
reference and candidate both legitimately select zero symbols, and an empty GPU
layer in the reference incorrectly colors the frame. A0g preserves that original
readback and explicitly corrects the oracle only for independently proven-empty
layers. Nonempty scene comparisons remain unchanged and exact. Empty wind is
visibly labeled; it is not counted as numerical-weather-ready or faster content.

Persistent repeated loading and visible resident-map coverage pass correctness,
as does native A→B→A generation rejection/coalescing. Two same-protocol repeats
pass; a final attributed run also passes. Still not an architecture adoption:
startup is ~30–31 s, much per-request work is oracle validation/source reopening,
and heartbeat tails reach ~295 ms. No presentation, global memory plateau or
the reference application-equivalent performance acceptance exists. The overall unattended budget
is exhausted and all children are reaped. See the
[progress/checkpoint](rendering-a0g-progress.md) and
[machine-readable summary](profiles/rendering/2026-10-18/a0g-unattended-summary.json).
Earlier sections below are historical contracts and outcomes, not current blockers.

## Architectural question

Can independently owned camera/time state, bounded asynchronous preparation and
persistent display resources improve the **complete input-to-render path** over
the current renderer without changing weather/source-selection fidelity?

E1/E2/E3 mechanism results are design inputs, not architecture acceptance. E2d
retained the current frame-assembly pipeline, so its useful readiness observations
do not answer this question. Its failed process exits also block acceptance.

## Candidate boundaries

| Boundary | Owner / responsibility | Forbidden dependency |
| --- | --- | --- |
| ViewState | GUI input controller; camera/time revision, never weather-job identity | Waiting for GDAL or a full-frame bake to move the camera |
| PreparationRequest | Scheduler; source/time/coverage generation, bounded replaceable work | One uncancellable job for every pixel/time input |
| PreparedResources | Worker-published immutable fields, coverage, provenance, reservations | Live GDAL/sampler objects reaching GUI/render thread |
| ViewSelection | Exact current native membership/thinning/mask/anchors, referencing field IDs | Resampling or republishing field endpoints for covered motion |
| RenderState | Render host; persistent resource ownership, transforms/uniforms and selection | Legacy whole-frame assembly on each camera update |
| PresentationState | Explicit ready/loading/unsupported state, one symbol mode per revision | Mixed CPU/numerical foreground or silent clipped/stale success |
| Lifecycle | Stop input → stop/cancel preparation → disconnect callbacks → release scene/resources → destroy host | Restoring hooks while live Qt callbacks can reenter another ownership model |

The candidate is not a claim to duplicate the reference application implementation. Qt remains the
first host; native float transport and alternate hosts are options if measured
costs require them. Use independent implementations informed by supplied evidence.

## Vertical slice A0a: complete covered-pan composition

First remove the old frame-assembly path **entirely from timed input**, rather
than installing another faster job inside it. A standalone Qt Quick viewport
owns camera input and composes persistent background/navigation textures and
native-field barbs. The existing application is an **offline preparation/oracle**,
not the candidate's input controller, frame assembler or scheduler.

The initial slice deliberately covers fixed-time, same-zoom, covered pan/reversal.
It retains a reference-produced padded CPU chart/weather background and navigation
as immutable image resources, with exact current barb selection on top. This
includes real fill/isobars/charts in the composed view, but **does not separate
their preparation or turn fill into a numerical GPU layer**. Do not call that the
complete target architecture or infer time/coverage-miss behavior from it.

Why this slice: it tests whether the whole warm composition path can respond
independently of legacy frame assembly and blocked preparation. It does not
pretend a frozen, thinned wind scene is equivalent; glyph membership/anchors are
recomputed per current view. Explicit resource-coverage checks reject escapes.

Subsequent A0 slices must separate chart/weather preparation and cover exact-time
scrubs, interval/source changes, coverage misses and replacement/soak before any
architectural adoption. These are not automatically authorized production tasks.

## A0a contract and budget

- **Baseline/workload:** same versioned 33-source derived functional fixture and
  saved view; immutable resources exported from the existing #50 app at `175096d`.
  Reference uses the existing native replay. Candidate uses the same logical
  viewport/DPR, 24 eight-pixel forward/reverse inputs and 16.667 ms intended cadence.
- **Mechanism:** prepared padded layers plus immutable wider numerical field;
  independent viewport input/render path, no TrackMap/MapFrame assembly in replay.
  No preparation allowed during timed motion. Source/clock/zoom/coverage escape
  cannot silently reuse an ineligible resource.
- **Fidelity/lifecycle gates:** compare initial complete composed pixels to the
  reference plus an independent nonblank guard. Require matching input trajectories,
  exact node/anchor/vector selection inputs, resident coverage, no mixed symbols,
  clean process exit and owned-resource teardown. Readback stays outside timing.
- **Measurement:** all intended/dispatched inputs, synchronized render callbacks,
  missed/coalesced revisions, GUI selection cost, field/background/selection
  publications and resource bytes. Rendering callbacks are not presentation FPS.
  Frozen offline preparation is an extreme stall control, not evidence of a
  working asynchronous miss scheduler.
- **Budget:** up to three implementation/targeted-check iterations, one reference
  preparation/export process and three native candidate/reference attempts, each
  bounded to 120 seconds. No unrelated cleanup or silent attempt extensions.
- **Decision:** adopt/revise/reject the warm interaction boundary only. Full A0
  adoption still requires asynchronous cold/miss/time/source behavior and memory
  plateau. One process pair cannot establish statistically useful end-to-end gain.
- **Scope protections:** local standalone research only; leave #50 untouched and
  unmerged, preserve existing dirty work, no routing/source-priority changes, no
  default-on switch, no downloads or approximation-gate relaxation.
- **Stop:** fidelity/source/coverage mismatch, native lifecycle failure not resolved
  within budget, resource rejection, or exhaustion. Record reason and next decision.

## Checkpoints

Checkpoint: the export/reference process exited 0, verified exact selection
inputs at all 25 initial/input positions, and released the private preparation
thread. The first independent-host attempt rejected initial full-composition
pixel equality before input. Source review found prototype layers enabled
smoothing while reference `ImageFrame` defaults to nearest sampling. **Narrow**
to correcting this implementation discrepancy and repeating the unchanged exact
gate inside the original remaining budget; do not relax pixel fidelity or treat
the failed run as an interaction result.

The second attempt still rejected full composition. Further source review found
the reference presenter explicitly requests 4× MSAA and an 8-bit alpha buffer;
the independent host had left both at defaults. The final permitted iteration/
native attempt matches this host configuration and preserves private pixel-diff
diagnostics on failure. This is the last bounded configuration/fidelity check,
not permission to weaken equality or continue tuning after exhaustion.

## A0a outcome: coherent warm path demonstrated, performance inconclusive

The final artifact passes the unchanged **complete-composition byte-equality**
check against the original app, plus an independent nonblank guard. Matching the
reference MSAA/alpha configuration resolved the remaining initial discrepancy.
No pixel tolerance was increased. The independent viewport then completed all 24
inputs with matching camera/time trajectories and no runtime preparation. The
offline oracle verified exact endpoint values/masks/anchors/counts for the initial
view and all 24 future positions, with candidate numerical sampling blocked.

This is a complete warm **composition/input path**, not a complete replacement
renderer: background fill/isobars/chart preparation is still a combined immutable
CPU-produced resource, time/zoom are fixed and preparation is unavailable during
motion. No cold/miss scheduler, independently changing weather fill, interval/
source transition or memory plateau has been demonstrated.

### Diagnostic comparison

Same derived functional fixture, saved view, logical viewport and DPR 2.0. The
candidate is a separate QQuickWidget containing only retained image nodes and
field/selection barb portrayal. It instantiates no TrackMap, MapFrame assembler or
legacy MapScene; the original app is offline preparation/reference only. GDAL open
entrypoints raise during the candidate run. All resource checksums are verified.

| Observation | Current app | Independent vertical viewport |
| --- | ---: | ---: |
| Input revisions | 24 | 24 |
| Input-phase observed revisions | 16 | 14 |
| All-input dispatch lateness p50 / p95 | 10.2 / 37.4 ms | 36.7 / 85.9 ms |
| Observed-only input-to-render callback p50 / p95 | 13.2 / 29.4 ms | 4.9 / 9.3 ms |
| Candidate GUI selection p50 / p95 | — | 1.07 / 1.16 ms |

**Do not call this a speedup.** Faster callbacks for the observed subset coexist
with worse all-input dispatch lateness and fewer observed revisions. The smaller
conditional render latency can reflect coalescing/backlog, not a better complete
interaction path. This is one sequential reference/candidate pair; repetitions,
equivalent warm-up and defensible presentation endpoints remain open. Candidate
timing begins after static readback, without a validated stable warm plateau;
reference uses the original harness warm-up. Warm comparability is not accepted.

The candidate requested four field images once and 75 selection images (three
initial plus 24 × three updates). Existing image-node counters show zero
**background/navigation** uploads during motion. They do not count ShaderEffect
texture uploads; do not report zero total GPU traffic. Qt still owns actual
texture allocation/upload in this prototype. No draw/performance claim follows
merely from lower publication bytes or bypassing old frame assembly.

### Validation and exit-status limitation

- Five targeted camera/coverage/eligibility checks passed in each of the three
  recorded iterations. Black passed on both new Python files; whitespace passed.
- Reference/preparation export process: runner reports exit 0. All 25 selection
  oracle comparisons passed; private source handles and preparation thread closed.
- First two independent native attempts: exit 1, initial exact-composition gate
  rejected before any timed input. Neither is performance evidence.
- Final attempt: complete composition/nonblank/replay artifact was written and
  no exception/output was reported. **The runner tool response omitted exit
  status**, so clean process exit is **unconfirmed**, not inferred from artifact
  existence. This is an observability blocker, not a claimed lifecycle pass.
- All three implementation iterations and three candidate native attempts are
  consumed; no extra tuning runs were made. A future contract must print/persist
  child exit status explicitly so an empty shell result cannot obscure this gate.

The final viewport disconnects recorder signals/timers, removes its scene and
clears engine-owned providers before widget destruction. This independent
ownership avoids E2d's restoration of monkeypatches into a live legacy scene, but
the missing command status still prevents a clean lifecycle acceptance claim.

Private artifacts in the OpenCode workspace: `rendering-a0a-reference.json`,
`rendering-a0a-candidate.json`, and `rendering-a0a-resources/` (checksummed image/
numerical resources, oracle and metadata). They contain private scene data and
remain outside Git. The sanitized
[A0a summary](profiles/rendering/2026-10-18/a0a-vertical-summary.json) preserves key
evidence, limitations and command-status uncertainty.

## Architecture review / next decision

**Revise/continue the architecture evaluation; do not adopt this candidate yet.**
Independent camera/selection/composition is viable with exact initial content and
without legacy frame assembly. It has not demonstrated better whole interaction
or asynchronous preparation behavior, so neither Qt nor a native rewrite is
selected/rejected by this result.

Before the next A0 experiment:

1. Make complete-run exit status and environment/host/warm-up identity explicit.
   Distinguish resource priming, first motion, sustained motion and settling.
2. Attribute all-input GUI stalls and Qt image/selection publication costs rather
   than optimizing the already ~1 ms selector. Render-thread ownership and host
   submission need to be evaluated as architectural boundaries, not assumed cheap.
3. Define the asynchronous resource scheduler and split chart from changing weather
   layers, including fill. Exercise misses/stalled preparation/interval/source
   changes with accurate loading state before architectural adoption.
4. Compare the **whole path** again with all inputs, fidelity, resource reservations,
   memory plateau and sufficient paired repetitions. Keep all negative evidence.

These are supporting phases of A0, not a return to an endless mechanism checklist.
No production renderer changes, commits, pushes, PR expansion, merges or rollout
were performed for this architectural slice.

## A0d restart: coverage-loss safety only

Latest A0f continuation is **partial, not accepted**. A long-lived offline
preparation app now services requests through a latest-request mailbox; completed
exports are decoded off the interactive GUI thread and use the existing generation
gate before publication. Active exports complete rather than being interrupted;
only the newest pending request is retained by the scheduler. A stop marker and
supervised termination bound process lifetime, not total app memory.

Visible loading now retains same-time map/fill/navigation image content within
the actual common image coverage, masks uncovered pixels and explicitly labels
wind barbs unavailable. This is a **partial preview**, never complete/current
weather. First-destination geographic detail (166-color crop) and coverage boundary
move with byte-exact pixel translation, and full loaded pixels match the reference
after 25 exact numerical checks. The same process handled a second destination,
but that export failed the existing nonblank/selection-count gate. Both attempts
have controller exit 1; no complete persistent two-load run is accepted.

First-destination preparation/export cost was 3.66 s once the process was ready;
request-to-verified still took 34.55 s waiting for initial startup. These different
spans must not be compared as a speedup. Native cancellation/freshness, repeat
load latency, memory plateau and presentation remain unproven. The second preview
included uniform land: geographic detail motion is inconclusive there; exact
coverage-boundary translation is checked separately without claiming uniform
pixels prove movement. Selection/missing-data acceptance was not relaxed.

**Decision: stop/narrow at the second-destination count gate.** Diagnose actual
candidate/reference counts and missing coverage before changing workload or
portrayal. See [A0f evidence](profiles/rendering/2026-10-18/a0f-persistent-summary.json).
All work is experiment-only; Qt remains, production/#50 and routing are untouched.

Initial A0e was drafted but **not accepted**: see the
[new-area preparation result](profiles/rendering/2026-10-18/a0e-coverage-load-summary.json)
and the canonical plan's A0e contract/outcome. Scheduler/bundle/process checks
passed (27 tests, ten subtests), but both controller attempts failed. The second
reached the real preparation app and rejected its viewport dimensions/scale before
resource export. No complete new-area publication or post-load pixel comparison
has succeeded. Early layout timing is the next diagnosis, not grounds for
relaxing the identity gate. Both attempts and raw failures are preserved outside
Git; no native timing/performance result is claimed. The bounded budget is stopped.

### A0e follow-up: destination load/publication mechanism passes

A separately contracted follow-up moved target application to after initial
layout/overlay settling, kept the exact dimensions/scale check, and re-settled
the destination overlays. The first attempt reached byte-exact destination
composition but failed final JSON serialization; it remains rejected. After a
native-scalar serialization correction, the second controller and preparation
process both exited 0. All 33 forecasts were retained, 25 destination numerical
selection/endpoint/anchor/count comparisons passed, and full loaded pixels matched
the destination reference byte-for-byte. No destination bundle existed before
the request. See [follow-up evidence](profiles/rendering/2026-10-18/a0e-followup-summary.json).

The GUI recorded 2,448 heartbeat ticks (p95 gap 17.2 ms, max 79.8 ms, two gaps over
50 ms) and 32 camera input-state changes during a deliberate preparation hold.
The producer took 38.6 seconds including snapshot verification, sandbox creation,
app initialization and export; this is not an acceptable production cold-load
target or an isolated decode measurement. Full-screen loading hides the map, so
camera state processing does not establish visible panning or presentation.
Cancellation/latest-only ownership has synthetic tests, not a native cancellation
matrix. No memory plateau, repeated paired speedup or the reference application equivalence is
established. Qt remains suitable for this tested mechanism; no host rewrite is
justified by these results.

**Decision: support/narrow, close this gate.** Next justified design work is a
long-lived preparation producer and independently drawable resident map/chart
content during loading, under a separate bounded contract. Do not consume spare
attempts, start later layer/zoom phases, or adopt/roll out the architecture by
default. Twenty-nine tests/13 subtests and Black on three changed files passed;
all source changes remain experiment-only, with #50 untouched and unmerged.

The interrupted draft failed before producing an accepted candidate coverage
run. It also weakened trajectory matching and could retain stale CPU weather in
the background or encode zero wind as missing data. Those shortcuts are removed.
Whole-path comparison now rejects this safety-only scenario outright, and other
comparisons require matching camera **and** forecast-time input states.

The bounded restart contract and decision are in the canonical plan. Two native
candidate runs exited 0; the final Cocoa/Metal/DPR-2 run completed 24 inputs with
15 unavailable submissions and nine ready submissions. Images and numerical
fields are checked independently for coverage. A miss hides chart, navigation
and weather together behind an explicit **unavailable / preparation disabled**
screen; it does not claim loading is underway. Correctness-only readbacks verified
that unavailable composition and byte-exact original composition on resident
return, both before and after the timed replay. No timed input readbacks were
added. Fifteen targeted tests/ten subtests and Black on four files passed.

Evidence: [A0d safety summary](profiles/rendering/2026-10-18/a0d-coverage-safety-summary.json).
Raw resources/reports remain outside Git. The earlier interrupted command deleted
the prior `rendering-a0c-*` local raw artifacts; existing A0c versioned summaries
are historical evidence, not newly reverified results. The new resource export is
not a replacement for the lost original timing runs.

**Decision: narrow; safety mechanism only.** Returning to an already prepared area
is not loading a new one. No new-area preparation, post-load composition,
presentation timing, paired speedup or architecture adoption is established.
Stop the restart budget (two implementation/check iterations, two native attempts).
The next justified phase needs a separately bounded asynchronous preparation and
publication contract, preserving source ordering and exact new-area portrayal.
No production/#50 changes, commits, pushes or merges were made.

## A0b contract: comparable whole-path runs and explicit process outcomes

- **Question:** does independent input/composition improve sustained covered-pan
  behavior after equivalent native event-loop priming, and can the entire process
  close cleanly? A0a's conditional callbacks alone cannot answer this.
- **Scope:** same fixed-time covered-pan vertical path and checksummed real fixture;
  identical two-second non-readback priming of the reference/candidate Quick windows,
  with callback/GUI cadence evidence, then the same 24 input revisions. No new
  transport or selector optimization. Preserve all cold/priming spans separately.
- **Protocol:** two fresh reference/candidate pairs, alternating order. Explicit
  supervising parent persists child exit code, command/config/harness hashes,
  stdout/stderr, duration and timeout before accepting any replay artifact. Capture
  environment/backend/DPR and compare all camera/time trajectories. Never infer
  exit success from an empty tool response or a written report.
- **Gates:** existing exact complete-pixel/nonblank and all-position numerical
  checks remain unchanged. Both child exits must be zero; reject incomplete input,
  mismatched fixture/viewport/DPR or coverage. No presentation FPS claim. Record
  all-input dispatch lateness alongside observed-only callback timing and counts.
- **Budget:** three implementation/targeted-check iterations, up to six fresh
  native child processes (four planned + two diagnostic slots), each child capped
  at 110 seconds and each runner command at 120 seconds. No automatic extensions.
- **Decision:** continue/revise/stop A0's architecture from whole-path evidence,
  not best-case callback latency. Even a warm win does not accept the missing
  asynchronous cold/coverage/time scheduler, independent fill or memory plateau.
- **Follow-on phase:** define/implement bounded asynchronous resource scheduling
  and explicit chart/weather generations after this measurement gate. Do not
  silently present cached combined CPU weather as independently updated GPU fill.
- **Outcome:** in progress.

A0b checkpoint: both supervised first-pair children exited **0**. Comparison
rejected QSurfaceFormat query mismatch (`-1/-1` versus `0/8` samples/alpha on
Metal). Rather than discard the host gate, iteration 2 measures the actual
redirected QRhi render target during native render callbacks (sample count,
pixel size, DPR). Surface-format results remain diagnostic, not a claim to
measure the effective Metal target. Require matching nonempty effective target
metadata; no unsafe inference from requested defaults or equal screenshots.
Pair 1 is not accepted, and changed instrumentation requires fresh paired runs
inside the four remaining native slots. No extra attempts are authorized.

## A0b outcome: matched whole-path comparison, no adoption yet

Pair 2: both supervised children exited **0** (reference 38.8 s, candidate 7.7 s);
all comparison gates passed:

- Host identity: cocoa / Metal / DPR 2.0 on both sides; identical 2 s priming
  protocol (123 vs 118 GUI ticks).
- Content: byte-exact complete initial composition, nonblank guard, exact
  selection oracle at all positions, matching input trajectories and prepared
  resource identity (checksums verified).
- Whole-path diagnostics (render callbacks are not presentation FPS):

  | Diagnostic | Current app | Independent vertical viewport |
  | --- | ---: | ---: |
  | Clean child exit | yes | yes |
  | Inputs | 24 | 24 |
  | Revisions observed at render callbacks | 17 | 23 |
  | All-input dispatch lateness p50 / p95 | 6.7 / 16.7 ms | 1.3 / 2.7 ms |
  | First observed render p50 / p95 | 15.3 / 23.7 ms | 7.3 / 9.5 ms |
  | Candidate GUI selection p95 | — | 1.1 ms |
  | End-to-end speedup proven | no | no |

**Do not adopt on this result.** The candidate wins on every warm-path diagnostic,
but this is one pair; the earlier A0a run showed the same reference app at
dispatch p95 37.4 ms versus 16.7 ms here — run-to-run variance is large enough
that a single pair cannot establish a durable interaction improvement.
Presentational latency to the screen was not measured (`afterRendering` is not a
presentation timestamp). The slice is still warm only: fixed time/zoom, no
runtime preparation, coverage misses, time scrubs, interval/source transitions
or independent GPU fill; the candidate's upload counters show zero because Qt
owns texture allocation internally, not because no GPU traffic occurs.

**Decision: revise/continue A0; do not adopt the candidate.** Independent
camera/selection/composition is viable and content-exact, with better warm-path
diagnostics, but the architecture is not accepted without the follow-on phases.

### Follow-on phases (not yet authorized as implementation work)

1. **Bounded asynchronous resource scheduler:** preparation can be cancelled and
   replaced; coverage misses and stalled preparation show accurate loading state
   instead of silent stale or clipped success.
2. **Split chart from changing weather generations:** independently refreshed
   fill/isobar/chart layers with exact-time semantics — do not silently present
   cached combined CPU weather as GPU fill.
3. **Cold/time/source behavior:** exact-time scrubs, interval and source changes,
   plus memory plateau over soak, measured over repeated paired runs with stable
   warm-up and explicit exit status.
4. Re-compare the whole path after 1–3; adoption requires a demonstrated
   durable improvement across all of them, not a warm-slice diagnostic win.

## A0c: timeline scrub (forecast-time change) — fair head-to-head

Both the reference replay engine and the independent viewport now scrub the same
forecast-time trajectory: 12 hourly steps (08:00→19:00→08:00) at a fixed saved
view, using the `scrub_return` scenario (forward-then-back). The candidate
switches between pre-exported numerical fields with no runtime preparation; the
reference app rebuilds through its staging pipeline on each time change.

Both supervised children exited **0** (reference 53.3 s, candidate 7.8 s). All
comparison gates passed:

- Host identity: cocoa / Metal / DPR 2.0 on both sides; identical 2 s priming.
- Forecast-time trajectories match exactly (24 inputs each, identical timestamps).
- Content: byte-exact complete initial composition at initial time, nonblank
  guard, exact selection oracle verified at all 25 pan positions × 12 time steps
  = 300 checks during export; all field file checksums match.
- Whole-path diagnostics (render callbacks are not presentation FPS):

  | Diagnostic | Current app | Independent vertical viewport |
  | --- | ---: | ---: |
  | Clean child exit | yes | yes |
  | Inputs | 24 time scrub | 24 time scrub |
  | Revisions observed at render callbacks | 12 | 24 |
  | Numerical revisions rendered | 0 | 24 |
  | All-input dispatch lateness p50 / p95 | 252.7 / 357.7 ms | 1.0 / 2.0 ms |
  | First observed render p50 / p95 | 26.1 / 62.9 ms | 7.5 / 7.9 ms |
  | Candidate GUI selection p95 | — | 1.2 ms |
  | Texture uploads during input | 41 (568 MB) | 0 |
  | Field builds during input | 0 | 0 |
  | End-to-end speedup proven | no | no |

**Do not adopt on this one result.** The candidate renders every time-scrub
revision numerically with dispatch p95 2.0 ms vs the reference's 357.7 ms, but
this is one pair on one fixture. The reference's terrible time-scrub latency
(22 of 24 inputs over 50 ms) shows its staging pipeline blocks the GUI on every
time change — the candidate avoids this by switching pre-exported fields
instantly. Presentation to screen was not measured (`afterRendering` is not a
presentation timestamp). One pair, no statistical significance.

**Decision: A0c complete — candidate demonstrates a clear advantage on timeline
scrub, but adoption requires remaining follow-on phases:** coverage-miss loading
state, layer toggles, zoom, and memory plateau over soak — each measured as a
fair head-to-head on the same workload.

No production renderer changes, commits, pushes, PR expansion, merges or rollout
were performed for this architectural slice.
