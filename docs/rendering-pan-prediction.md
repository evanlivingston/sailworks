# Continuous-pan weather preparation

This follow-up targets scrolling/panning, not general CI cleanup. It starts from
merged `dev` in `feature/pan-prefetch-latency`; the unrelated uncommitted cleanup
work remains in its original worktree. CPU presentation stays the default.

## Problem observed

A new `trackpad_pan` startup replay sends pixel-delta wheel events through the
application's actual trackpad input path. It pans 16 pixels per input frame for
96 frames, targeting a 33.333 ms interval. Both modes use the same snapshot,
trajectory, frame count and cadence, with no candidate-only pause or wait for
speculation. This supplements rather than reinterprets the earlier
`pan_pause_pan` experiment.

The initial fixed-lookahead probe recorded geometric prepared coverage in
28/96 replay frames in each of three runs, eight speculative requests per run,
zero promotions/adoptions, and eight speculative cancellations. Weather jobs
were repeatedly cancelled before completion during the continuous pan.

These counts describe prepared-image enclosure, not usable forecast-data
coverage. Grab/readback timing is not display FPS or input-to-scanout latency.

## Candidate

* Learn an exponentially smoothed execution time from successful, non-cancelled
  weather jobs. Exclude queue and publication delays, failed jobs and empty
  results. Sources, DPR and zoom identify the timing context.
* Predict travel over `1.25 * measured_seconds + 0.1`, bounded to 0.25–2 seconds;
  unknown contexts retain the one-second fallback.
* Keep the existing 192-pixel minimum predicted travel, but cap the candidate
  displacement at the weather worker margin (256 pixels), also respecting the
  existing 0.75-viewport cap. The nearer prediction can enclose the current view
  while approaching, rather than always being discarded for visible work.
* During motion only, adopt/promote a tagged pan prediction when its actual
  image extent encloses the current viewport at native zoom. Use this same
  enclosure rule for its pending/fresh status, avoiding an immediate duplicate
  visible job after adoption. Ordinary visible jobs keep the existing threshold.
* After motion stops, tagged frames return to the ordinary refresh threshold.
  Content/scale mismatches and a reversal beyond enclosure still cancel work.

No sampler, forecast priority, interpolation, mask, contour or glyph-density
calculation changes. A prediction is an ordinary full weather render at another
camera position; its marker changes scheduling/reuse eligibility only. This
is a combined prediction/adoption trial, not an isolated measurement of the
timing estimator. It does not turn on adjacent-zoom prefetch or downloads.

## Reproduction

Use the verified private October 16 startup snapshot (33 selected forecasts,
32 enabled charts, dense barbs, 1 hPa isobars; no marine fields):

```sh
python tools/benchmark_startup_rendering.py \
  --snapshot <private-snapshot> --map-renderer gpu \
  --scenarios trackpad_pan --frames 96 --repeats 3 --interval-ms 33.333 \
  --weather-prefetch --output <candidate.json>
```

Add `--fixed-pan-lookahead` for the control. This disables the timing-based
prediction and the new enclosure-based adoption together, retaining the old
one-second/0.75-viewport policy. Both modes are measured using the same updated
harness. Reports record policy, estimated preparation time, per-input-frame
prepared status, scheduling events and weather-content hashes. Raw reports and
the startup snapshot stay in the approved private temporary directory.

## Matched application results — 2026-10-16

Three isolated runs per policy on native Metal/DPR 2, using the same updated
harness hash (`fe01a018d659ea85111dbb893c64a2252d85e91efa6d1f86adfa147b8e128cc5`)
and snapshot. Candidate runs preceded the final control batch; these are not
randomized trials. No competing test suites were launched during these runs.

| Metric | Fixed lookahead | Candidate |
|---|---:|---:|
| Prepared replay frames, all runs | 84/288 (29.2%) | 278/288 (96.5%) |
| Prepared frames per run | 28 / 28 / 28 | 91 / 95 / 92 |
| Started weather jobs per run during replay | 11 / 11 / 11 | 4 / 4 / 4 |
| Cancelled started weather jobs per run | 10 / 10 / 10 | 0 / 0 / 0 |
| Median per-run weather execution total | 3.90 s | 2.86 s |
| Median per-run grab/readback p50 | 14.1 ms | 13.1 ms |
| Median per-run grab/readback p95 | 37.5 ms | 41.6 ms |
| Median per-run grab/readback p99 | 48.7 ms | 50.8 ms |
| Pooled grab/readback p99 (288 samples) | 62.3 ms | 51.5 ms |
| Median post-gesture worker settling | 1.067 s | 1.068 s |

The repeatable gain is maintaining prepared weather while scrolling, and
avoiding cancelled preparation. Frame latency is mixed: per-run p95/p99 rose
slightly while pooled p99 fell, affected by one slower control run. Do not claim
an established FPS or p99 improvement. Actual mean input-frame intervals were
40.0–40.9 ms for the control and 37.2–37.8 ms for the candidate despite the same
33.333 ms target; this is a synchronous replay, not fixed-time device input.

The existing weather-content comparison passed: three shared viewport/time
frames were bit-identical, with three baseline-only and twelve candidate-only
publications. Those unmatched frames are **not** parity matches. The additional
same-viewport differential test isolates the prediction marker and checks exact
pixels/vectors through the unchanged full weather render path. Sampling
locations for a predicted camera naturally differ from a control camera; the
candidate does not reuse a different resample lattice as if it were identical.

Peak tracked surfaces were unchanged at 195,674,048 bytes. Peak process RSS was
2,247,557,120 versus 2,281,684,992 bytes (about 33 MiB higher); neither number
establishes a long-session memory plateau. Each candidate run recorded seven
adoptions (including queued requests, not seven completed frames), no prefetch
evictions or discarded stored-frame bytes, and no cancelled started weather
jobs. This does not establish zero wasted work for other trajectories.

Private reports: `pan-latency-fixed-final.json`, `pan-latency-adaptive.json`,
`pan-latency-comparison.json` in the approved OpenCode temporary directory.

## Checks and remaining limits

Tests cover estimate bounds/context changes, execution-only learning, failed-job
exclusion, direction/cap/threshold math, adoption without duplicate visible work,
post-gesture refresh, reversal/content/zoom/DPR invalidation, and production Qt
prediction geometry without camera mutation. A differential weather test checks
that toggling the prediction marker leaves pixels and vector arrays identical
at the same requested viewport.

Validation passed: 98 targeted rendering/scheduling/benchmark checks; 42 focused
checks rerun after adding timing-context cleanup; six Qt/GPU integration
launchers, including the 128-test Qt preview suite and native GPU checks.
Black checks on nine changed Python files and whitespace checks passed. These
are local results, not a claim that repository-wide CI is green.

Long-session memory, marine-heavy and non-Metal gates remain separate and
incomplete. The application replay covers continuous horizontal trackpad pan;
reversal invalidation is unit-tested, not yet measured in a native reversal
replay. Adjacent-zoom speculation remains pending.
