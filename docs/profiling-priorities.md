# Profiling follow-up: measured slices

## Status

The two optimizations measured here are already part of `dev`: the world-pixel
projection reuse for a completed weather frame and the scalar finite-value
checks in column assembly both arrived with the current-wind vectorization
work, and the current `_columns` implementation already tests each sample with
`math.isfinite` instead of allocating a NumPy array per point.

What is still missing is coverage and instrumentation. The merged projection
cache has no regression test, so a change that dropped or mis-keyed it would
not be caught. This branch therefore adds:

- regression tests for the merged projection cache (exact geometry, reuse
  across a pan, replacement on a zoom change, per-frame separation, read-only
  arrays, and the oversized-frame fallback),
- `tools/profile_pan_stalls.py`, an isolated diagnostic that records the spans
  the measurements below depend on, and
- this record of what was measured, including the corrections to the earlier
  overstatements.

The measurements themselves were taken on the branch that carried the
optimizations, before they were merged. They are retained here because the
remaining stall is not the part that was optimized.

Base: `dev` at 1f14649; branch `feature/profile-pan-hotspots`.

## Scope and corrections

The initial conversational report overstated several conclusions:

- `settle()` and its 1 ms sleeps are benchmark machinery, not application polling.
  They do not establish application wakeups or power consumption.
- Stage arrays named `stages_ms` already contain milliseconds. Multiplying them
  by 1000 in the initial analysis was incorrect.
- The first dedicated cProfile driver did not isolate HOME. It ran against live
  application data and may have triggered startup/shutdown persistence. It must
  not be repeated or treated as an isolated baseline. The replacement launches
  the existing network-blocked worker with sandbox HOME/XDG paths and private
  snapshot copies. No restoration of live data was attempted.
- Cumulative profile spans overlap and include waiting; summing them is not CPU
  attribution. Worker-idle time is neither CPU utilization nor usable coverage.
- Storage save-call counts are not disk-write counts: many save calls return
  early. Storage/file-lock and crash explanations remain hypotheses.

## Slice 1: bounded foreground wind projection reuse

A corrected 60-frame cProfile pan replay attributes 0.841 s inclusive to
`WindOverlayRenderer.live_geometry_for_view`, including 0.429 s to Mercator
projection, versus 0.166 s to base images. Profiling overhead and contention
affect these values; they are attribution, not acceptance timings.

Cache world-pixel wind coordinates on each immutable completed weather job for
its current zoom. Pan/resize/density changes still recompute exact screen
positions and sample selection. A zoom change replaces the arrays, and a new
weather frame owns a separate cache. Arrays are read-only. At most 8 MiB is
retained per frame; larger frames take the original uncached path. This is not
a global memory ledger. No routing, sampler, source-selection, density, or glyph
policy changes are intended.

### Measurements

Same private October 17 snapshot, CPU renderer, offscreen Qt, scale 1,
287x592 logical map, 49,701 retained startup wind samples. Each scenario:
60 frames at 16.667 ms requested pacing, three warmups, two serial repeats.
Worker cProfile disabled for these timing runs.

| Scenario | Before p99 grab (ms) | Candidate p99 grab (ms) |
| --- | ---: | ---: |
| Stationary | 5.0 / 1.7 | 1.4 / 1.4 |
| Pan | 139.5 / 91.9 | 106.0 / 105.1 |
| Zoom out | 20.5 / 16.0 | 15.0 / 16.9 |

No convincing end-to-end pan-tail improvement is established by this slice
alone. These are forced offscreen grab timings, not native-input FPS or
GPU/energy measurements.

A synthetic projection-only probe (49,729 nodes, 20 warmups, 200 samples each)
measured median 0.713 ms uncached versus 0.030 ms cached, with 795,664 bytes of
retained arrays. This demonstrates reduced projection work, not equivalent
whole-application speed or battery savings.

Private raw artifacts remain outside tracked files in the approved scratch
directory: `pan-projection-before.json`, `pan-projection-after.json`,
`pan-projection-micro.json`, and `pan-sandbox-profile-60.log`.

### Coverage

The regression tests added by this branch pin the behavior the merged
implementation is supposed to have: cached and uncached projection produce
identical geometry across three zooms, three viewports and every density
preset; the projection is computed once across a pan, replaced on a zoom
change, and never shared with a new weather frame; the retained arrays are
read-only; and a frame above the 8 MiB bound retains no arrays and takes the
original path.

## Slice 2: attribute remaining pan stalls and reduce column allocations

Added `tools/profile_pan_stalls.py`, a diagnostic launcher that reuses the
startup snapshot sandbox and network guard. It records nested wall-time and
per-thread CPU spans plus GC start/stop events. These spans overlap and must
not be added together. Instrumented traces are not acceptance timing runs.

The largest traced wind-barb span took 135.8 ms wall time but only 3.4 ms of
main-thread CPU. Its geometry selection took 135.0 ms wall / 2.6 ms CPU; actual
glyph building took 0.8 ms. It overlapped a worker's generation-2 GC collection
lasting 63.1 ms. Other 97.2 ms and 60.1 ms barb spans consumed 3.4 ms and 2.6
ms of UI CPU, respectively, while rendering workers were active. This establishes
substantial off-CPU delay, with GC contributing to at least one major outlier;
it does not attribute every remaining millisecond to a particular worker or
prove that all waiting is GIL contention rather than OS scheduling.

`WindOverlayRenderer._columns` previously allocated a small NumPy array for
each tuple's finite-value check. Scalar `math.isfinite` checks retain the same
first-valid-sample duplicate policy and latitude/longitude sort order. No
sampling or source-selection code changed. No global GC or Python scheduling
settings changed.

### Measurements

Same snapshot, renderer, viewport, frame count and pacing as slice 1, with all
diagnostic instrumentation disabled for the following timing comparison:

| Scenario | Projection cache only p99 (ms) | Plus scalar columns p99 (ms) |
| --- | ---: | ---: |
| Stationary | 1.4 / 1.4 | 1.4 / 1.3 |
| Pan | 106.0 / 105.1 | 85.4 / 79.1 |
| Zoom out | 15.0 / 16.9 | 16.7 / 16.0 |

Pan p95 changed from 41.4 / 45.0 ms to 23.0 / 26.3 ms. There were still 5 total
grabs above 50 ms in 120 candidate pan frames, versus 6 before this slice.
The tail improvement in these two serial repeats is encouraging, not a
universal or native-input guarantee. Pan remains outside the frame budgets.

A projection-independent synthetic microbenchmark (49,729 tuples, five warmups,
30 repetitions) measured median column-assembly CPU time of 64.7 ms before and
15.4 ms after. The instrumented live diagnostic's median column CPU span fell
from 158.7 to 104.5 ms; these inclusive spans can contain GC work. The next trace
still showed a 96.6 ms wall / 2.3 ms UI-CPU barb stall overlapping a 61.7 ms full
GC collection. Reduced allocation overhead is not a complete GC/latency fix.

Private scratch artifacts: `pan-outlier-profile.json`, `pan-wall-cpu-trace.json`,
`pan-wall-cpu-columns.json`, `pan-columns-after.json`, and
`pan-columns-micro.json`. Raw traces contain private viewport data and are not
tracked. Actual energy use, idle wakeups, and a long-run memory plateau have
not been measured.

## Remaining order

1. Reduce the remaining object/GC pressure and
   attribute worker off-CPU delays before changing rebuild margins or adding
   broader caches. Do not disable global GC to conceal the stalls.
2. Profile weather decoding/transforms/column assembly and canceled-job CPU,
   preserving source ordering, interpolation and fallback behavior.
3. Measure actual idle CPU/wakeups and long-session memory/handle retention.
4. Separately address persistence transactions/atomicity and shutdown ownership,
   with focused tests. Do not attribute Windows failures or SIGSEGVs without
   reproduction evidence.
