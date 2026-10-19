# Weather scheduling: coalesce active demand, reject the wider survival policy

> **Status: HISTORICAL.** The weather scheduling change, written 2026-10-17; the behaviour it
> describes is still current, and this document is kept for its reasoning.
> The R0–R5 sequence in the implementation plan is the authority.
> Evidence, not a plan.

This follow-up changes display scheduling, not weather sampling or routing.
Predictive prefetch and a cross-view data working set are **not implemented**.

## Enabled behavior

The shared overlay queue now distinguishes active work from the latest queued
request. Source/style/DPR changes cancel incompatible active work even after
`state.requested` has moved on to a queued job. Incompatible queued work is also
discarded during animated zoom rather than starting after the active callback.
Returning to an exact content key/view already being prepared reattaches demand
to that active job and cancels the duplicate queued request. A cancelled request
cannot suppress its replacement. Other maps retain their queued slots/FIFO order.

The existing half-margin refresh/cancellation threshold remains the default.
Continuous time scrubbing can still publish compatible older-time previews, while
the newest time remains queued; those previews are not counted as current content.
Hide/close/source removal cancel both active and queued demands. Queues remain
bounded at one active worker and one pending request per map, with no new image
or decoded-data cache and no network speculation.

Jobs have monotonic request/start/finish/cancel/publication timestamps, request IDs
and cancellation reasons. An optional event hook feeds benchmark tracing; the app
retains no event history. The harness records content-key fingerprints, viewports,
queue waits, publications and cancelled worker wall time. Its prepared-view probe
checks exact forecast/style/source identity, native zoom, and enclosing image
extent. **This is geometric preparation, not proof that forecast data exists at
every location, nor proof of a displayed frame.** Existing missing-data/source
policies and separate weather-availability checks remain authoritative.

## Rejected trial: keep active work to its full image extent

An explicitly opted-in trial retained active weather jobs beyond the normal
128px refresh threshold whenever their 256px image margin still enclosed demand.
A controlled return-pan fixture benefits from this, but the saved startup replay
does not. Three native Metal/DPR-2 runs per scenario, 96 frames each:

| Long pan, three runs | Legacy scheduling | Full-extent trial |
|---|---:|---:|
| Started weather jobs | 21 | 15 |
| Cancelled weather jobs | 15 | 9 |
| Cancelled worker wall time, summed | 8.03 s | 9.17 s |
| Median first prepared view after gesture | 1.23 s | 1.63 s |
| Median worker idle after gesture | 1.45 s | 1.84 s |

Fewer cancellations did **not** mean less wasted work or earlier coverage. The
policy is off in the application. It remains available only as a private renderer
trial / benchmark `--preserve-covered-weather-jobs` override, not a user setting.
Historical trial files are preserved and must not be presented as an accepted
candidate. Reverse-pan runs also showed no prepared-coverage gain.

## Accepted coalescing measurement — 2026-10-16

Same verified October 16 private snapshot, frozen initial clock, GPU presenter,
base-chart worker and weather job-resource reuse in both modes. No network,
credentials or instrument connections. Three isolated runs per mode.

`scrub_return` uses 12 frames: request initial forecast +15 minutes, then +30,
then return to +15 before the first decode finishes. The final target remains
stable. `--legacy-weather-scheduling` is the control; default uses coalescing.

| Median per-run metric | Legacy | Coalesced |
|---|---:|---:|
| Post-gesture worker idle | 2.56 s | 1.42 s |
| First exact-content prepared view | 1.19 s | 1.19 s |
| Grab/readback p99 | 30.6 ms | 32.3 ms |

Across three runs, started weather jobs fell from 12 to 9, and private file opens
from 378 to 285. Three duplicate +15-minute jobs were eliminated; initial startup
cancellations remained 3 in both modes. Maximum process peak RSS was 1,994 MiB in
both modes. The strict viewport/**forecast-time** comparison validates all nine
baseline publications against the six candidate publications, including duplicate
baseline renders: weather images, dimensions, native wind-vector bytes and counts
are identical. There is **no first-coverage or frame-speed gain claimed** here;
the measured benefit is avoiding a redundant second job after usable work finishes.

A separate 96-frame long-pan check confirms the conservative default: prepared
view latency 1.232 → 1.225 s; worker idle 1.451 → 1.445 s; 21 started jobs and 15
cancellations in both modes. Per-run p99 medians were 42.8 → 45.2 ms, within broad
run ranges (41.7–56.6 / 39.5–48.4 ms); no normal-pan speedup is claimed. Exact
weather content comparisons pass. Peak RSS was 2,123 → 2,118 MiB, not evidence of
a long-session plateau or general memory saving.

These are synchronous grab/readback and worker/prepared-viewport measurements,
not GPU execution or scanout timings. The snapshot has zero marine fields; do not
generalize its results to marine-heavy workloads or other native backends.

Private evidence in the approved OpenCode temporary directory:

* `r4-weather-scheduling-baseline.json`, `r4-weather-scheduling-candidate.json`:
  original wider-survival trial, **rejected** (the old candidate label is historical).
* `r4-coalesce-baseline.json`, `r4-coalesce-candidate.json`,
  `r4-coalesce-comparison.json`: accepted forecast-return control/candidate/gate.
* `r4-default-pan-baseline.json`, `r4-default-pan-candidate.json`,
  `r4-default-pan-comparison.json`: conservative default pan check.

Reproduce with `tools/benchmark_startup_rendering.py --snapshot <private-snapshot>
--map-renderer gpu --scenarios scrub_return --frames 12 --repeats 3`, then add
`--legacy-weather-scheduling` for the control. Pan verification uses
`--scenarios pan --frames 96 --repeats 3`. The default scenario list is unchanged;
the forecast-return scenario is explicit so historical trajectories remain intact.

## Validation and next boundary

Eleven deterministic scheduling tests exercise active/queued separation, return
coalescing, animated-zoom source changes, forecast previews, cancellation/replacement,
resize/native-zoom enclosure, both maps, and the explicitly opted-in rejected trial.
137 targeted rendering/sampling/benchmark checks and 128 Qt regressions passed;
native GPU and Qt base-worker checks also passed. Formatting/diff checks pass.
The full repository suite was not run; numerical sampling/routing code is unchanged.

The next substantive pan improvement needs reusable, byte-accounted geographic
data chunks and measured missing/current coverage, not a wider cancellation margin.
Predictive local prefetch should build on that working set and reserve capacity
for visible gaps. No automatic remote prefetch is authorized by this implementation.
