# A0g unattended progress and checkpoint

> **Status: HISTORICAL.** The A0g unattended-continuation checkpoint, written
> 2026-10-18. The A0 program is closed as an experiment; superseded by
> [ADR-001](adr-001-grib-rendering-architecture.md) and issues #62-#70. Evidence,
> not a plan.

## Outcome

**Persistent new-area loading, honest partial coverage, reference-empty wind and
native A→B→A publication now pass the experiment's correctness gates.** Two runs
with identical instrumentation repeat the complete workload successfully. This
does **not** establish the reference application-like performance or accept the renderer architecture.

The top-line goal remains fluid pan, scrub, layer and zoom interaction on the
actual forecast selection without routing changes or silent portrayal/source
degradation. All changes here are experiment-only. Qt remains; production and
PR #50 are untouched. No commits, pushes, PR changes or merges were performed.

Canonical contract and incremental decisions: [performance plan](rendering-performance-plan.md).
Machine-readable evidence: [A0g summary](profiles/rendering/2026-10-18/a0g-unattended-summary.json).
Raw reports, reference images, app data and process logs remain outside Git.

## Progress through the bounded investigation

| Attempt | Finding / result | Controller / producer exit |
| --- | --- | --- |
| 1 — diagnosis | Second destination has candidate=0 and reference=0 after exact endpoint/mask/anchor comparison. The unconditional nonblank guard is the first blocker. | 1 / 0 |
| 2 — explicit empty support | All 25 empty-view numerical checks pass, but reference GPU image is red while no-symbol candidate is brown. Visual gate correctly rejects. | 1 / 0 |
| 3 — empty-layer diagnostic | Reference frame atlas count=0; both uploaded endpoint atlases have zero finite vectors. Suppressing that proven-empty layer changes all 2,087,400 pixels. Preserve both images; original GPU output is not a faithful no-symbol oracle. | 1 / 0 |
| 4 — two loads | Same producer loads both original destinations; 25 exact checks each. Empty view is explicitly labeled. Scene pixels pass under the recorded empty-layer oracle correction. | 0 / 0 |
| 5 — native A→B→A | Old A generation 3 is discarded; pending B generation 4 never executes; only newer A generation 5 binds, with exact pixels. | 0 / 0 |
| 6–7 — identical v3 repeats | Both repeat the complete two-load + freshness workload successfully with identical harness hashes. These are candidate correctness repeats, not a paired speedup comparison. | 0 / 0 each |
| 8 — detailed v4 attribution | Same correctness gates pass; captures private-source, numerical/oracle and GUI-bind timings separately. | 0 / 0 |

Eight controller attempts and eight producer processes were used, with six
implementation/check iterations. All producers exited normally. The overall
unattended attempt budget is exhausted; no native process remains running.

## Fidelity and missing-data semantics

- Keep every selected source in its existing order and the exact forecast clock.
  Compare native-point membership, selected counts, both reconstructed endpoint
  arrays, NaN masks and anchors at 25 positions per export. The final native run
  performs four exports: 100 comparisons, including 25 explicitly reference-empty
  comparisons. One export is obsolete work and does not publish.
- Positive symbol counts must still match exactly. Empty acceptance is opt-in for
  the persistent producer only: both counts must be zero and neither endpoint
  atlas may contain a drawable finite vector. Calm `(0, 0)` wind is valid data and
  cannot pass this missing-vector gate.
- The empty reference GPU layer contributes an erroneous red image despite zero
  symbols/vectors. The exact shader/compiler/driver cause is **not established**.
  For this narrowly proven case, preserve the original readback and compare against
  the same reference scene with only that empty symbol layer hidden. Metadata and
  hashes record the correction. **This is not byte-equivalence to the erroneous
  original empty-GPU output.** Nonempty reference scenes remain unchanged.
- An explicit host badge says “No wind symbols in the reference selection for
  this view/time.” It does not claim that no forecast exists anywhere or infer
  full usable-data coverage. The badge is intentionally outside the Quick map
  scene; scene readback equality does not compare this added status UI to a
  reference badge. A full widget screenshot preserves its visible state.
- Loading uses same-time retained image coverage only, masks the uncovered area,
  and labels missing wind barbs. A partial preview is never recorded as complete
  numerical weather. Geographic-detail motion passes on the first, nondegenerate
  crop. The second crop is uniform land: detail motion is inconclusive there,
  while exact movement of the coverage boundary is separately verified.

The reference empty-layer problem should receive a focused #50 follow-up before
claiming its empty-data portrayal is validated. This investigation did not alter
that branch or expand its PR.

## Measured attribution, not presentation performance

Final detailed run (v4), same Cocoa/Metal/DPR-2 fixture:

| Span | First destination | Reference-empty destination | Latest A export |
| --- | ---: | ---: | ---: |
| Preparation/export command, excluding initial producer startup | 3.623 s | 3.487 s | 2.670 s |
| Overlay settling | 1.106 s | 1.015 s | 0.214 s |
| Export + numerical oracle | 2.398 s | 2.337 s | 2.408 s |
| Private source open, inside export | 641 ms | 621 ms | 658 ms |
| Stage field, inside export | 71.6 ms | 57.9 ms | 71.1 ms |
| 25-position selection oracle, inside export | 1,555 ms | 1,385 ms | 1,552 ms |
| GUI bundle preparation/bind (first two publications instrumented) | 21.6 ms | 16.1 ms | not separately recorded |

Nested rows overlap; do not add them twice. Producer startup/readiness still costs
about 30–31 seconds including sandbox/app initialization. Request-to-verified
times additionally include the deliberate preparation hold, resource transport,
and correctness readbacks. These are not production cold-load targets.

The repeated v3 runs place much of the multi-second request cost in the oracle/
export path rather than numerical field staging. The detailed v4 run confirms
that validation itself takes ~1.4–1.6 seconds per export. Reopening private sources
also costs ~0.6 seconds despite retaining the preparation app. A future real data
worker must retain its own resources on one owning thread, not transfer GDAL
handles between threads or silently skip validation to report a win.

GUI heartbeat p95 remains around 17–18 ms, but tails are not acceptable proof of
fluidity: one v3 run has a 277 ms gap, and v4 has a 295 ms gap. Measured GUI bind
costs alone do not explain those tails. Rendering/readback contention, scheduling,
allocation and host behavior remain attribution questions. **Heartbeat is not
presentation FPS, input-to-present, or proof that every camera state was shown.**

## Validation and decision

- 30 focused tests and 21 subtests pass; five changed Python files pass Black.
  Changed-file whitespace, evidence identities, statuses and explicit rejection
  of the failed runs are checked separately. Shared sampling/routing is unchanged;
  no full-suite or full CI pass is claimed.
- Native freshness is demonstrated for fixed source/time/style A→B→A view changes,
  not a complete source/time/zoom cancellation matrix. An active export finishes;
  its obsolete result is discarded, while only one latest pending request survives.
- No memory plateau, backend/DPR matrix, layer/zoom acceptance, presentation
  measurement, head-to-head the reference application benchmark or statistically useful application
  speedup comparison has been completed.

**Decision: support the correctness/ownership mechanism; narrow the next work to
a real long-lived preparation worker and GUI-tail attribution.** Separate oracle
validation from the timed candidate path without removing differential checks;
retain source resources on their worker thread; measure publication/rendering
without correctness readbacks in the timed interval. Do not start later milestones,
rewrite the host, adopt the architecture or extend this exhausted budget silently.

The repository is left uncommitted with all earlier user/research changes preserved.
This is a documented checkpoint, not a claim that the top-line goal is finished.
