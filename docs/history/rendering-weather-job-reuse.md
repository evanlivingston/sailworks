# R3 slice: reuse weather resources within a display job

> **Status: HISTORICAL.** The R3 weather job-resource reuse slice, written 2026-10-17; the
> behaviour it describes is still current, and this document is kept for its
> reasoning. The R0–R5 sequence in the implementation plan is the authority.
> Evidence, not a plan.

`WindOverlayRenderer` now reuses private forecast handles, metadata indexes, and
one shared `WeatherSampler` across a job's native wind, native pressure, mixed-grid
pressure resampling, and mixed-grid wind-fill passes. No shared sampling/routing
code was changed. Both Qt and GTK use the display renderer; CPU/GPU selection is
unrelated to this optimization.

## Finding and implementation

The diagnostic worker-thread profile on the private October 16 startup snapshot
found **132 file opens for one completed frame with 33 selected files**: each of
four passes reopened the selection through a two-entry cache. A completed startup
job spent about 1.68 seconds in GDAL open calls and constructed the sampler four
times. This was repeat work, not evidence that older selected forecasts could
safely be removed.

The new job-local scope pins successful private opens until all passes finish.
It preserves source order, the complete selected set, space/time interpolation,
native masks, vector rotation, run priorities, older-run fallback, displayed
density, and existing fill/contour grid choices. Failed opens are retried on the
next pass, as before. A partially constructed metadata index also triggers a
next-pass sampler rebuild rather than caching an omitted source.

Only the existing two-entry handle cache survives a job. Nonretained handles are
explicitly closed on success, cancellation, and errors. The two previous cache
entries are pinned before opening new sources, avoiding eviction of an indexed
handle into a reference cycle that would otherwise await garbage collection.
Job-local provider/sampler references are removed before publication; completed
jobs retain images, vectors and numeric counters, not the temporary handle set.

Decoded `GridIndex` arrays are trimmed between passes to a **128 MiB retention
budget**, with a zero-budget differential test. This is not a peak allocation or
RSS/VRAM bound: the unchanged sampler/GDAL may transiently decode more inside a
pass. Driver caches, geometry, Cairo surfaces and other maps are outside this
ledger. An eviction only forces a later decode; it does not prune source coverage
or alter numerical selection. No speculative/remote acquisition was introduced.

## Matched unprofiled replay — 2026-10-16

Same verified private snapshot, frozen forecast clock, native Metal, DPR 2, GPU
presenter and base-chart worker in both candidates. Three isolated runs per
scenario, 48 replay frames each. Control uses `--uncached-weather-passes`; candidate
uses the default job scope. Both include explicit handle cleanup, so the control
already reuses two previous handles per pass: **124 opens / 4 sampler builds** per
completed warm job versus **31 opens / 1 sampler build**. A fully cold scoped job
opens all 33 files once.

| Median across runs | Per-pass control | Job reuse |
|---|---:|---:|
| Pan post-gesture worker idle | 3.05 s | 1.44 s |
| Zoom-out post-gesture worker idle | 4.36 s | 2.61 s |
| Pan grab/readback p99 | 51.2 ms | 43.1 ms |
| Zoom-out grab/readback p99 | 47.2 ms | 47.1 ms |

Completed weather-job median duration fell from 3.28 to 1.58 seconds for pan runs
and 3.61 to 1.82 seconds for zoom runs (startup and replay publications included).
Startup worker idle fell from about 13.0 to 11.2–11.3 seconds. These are worker
settling and synchronous grab/readback measurements, **not time to usable coverage,
GPU execution, scanout, or FPS**. Zoom frame latency is effectively unchanged.

The strict comparison matched all **12 published weather frames** by viewport and
run: Cairo weather-image bytes, image dimensions, flattened native wind-vector
bytes, and wind sample counts are identical. Profiled runs are separate diagnostics
and are not pooled into these timings. The harness now marks profiling in its
workload identity so profiled/unprofiled reports cannot be compared accidentally.

Cancellation counts did not improve: each scenario had 12 weather jobs across its
three runs, including 6 cancelled jobs, in both modes. This slice removes repeated
work *within* surviving jobs; it does not implement view-independent working sets,
reuse of cancelled chunks, predictive prefetch, or new cancellation policy.

Maximum process peak RSS across pan runs was 2,104 → 2,061 MiB, and zoom runs
2,108 → 2,090 MiB. Job reuse retained about 90 KiB of explicit decoded arrays on
this snapshot. These high-water observations are not a memory plateau or a general
memory-saving claim; the prior chart-worker retention cost remains outstanding.
As noted in the embedded-renderer report, this snapshot has zero marine fields;
these results do not validate marine-heavy weather preparation.

Private evidence in the approved OpenCode temporary directory:

* `r3-weather-profile-before.json`, `r3-weather-profile-after.json`: diagnostics.
* `r3-weather-baseline.json`, `r3-weather-candidate.json`: unprofiled controls.
* `r3-weather-comparison.json`: compatible-protocol timing comparison and strict
  weather-content identity gate.

Reproduce with `tools/benchmark_startup_rendering.py --snapshot <private-snapshot>
--map-renderer gpu --scenarios pan,zoom_out --frames 48 --repeats 3`; add
`--uncached-weather-passes` for the control. `--profile-weather` is diagnostic only.
The comparison gate fails on changed hashes/counts, missing hashes or unmatched
published viewports rather than treating less content as an acceleration.

## Validation and next slice

Seven new resource tests cover exact pixels, all four sampled fields and per-point
source provenance for split-file, masked, mixed-resolution and projected sources;
zero-retention equivalence; cancellation/error cleanup; transient open/index retry;
old indexed-handle eviction; and empty/precancelled jobs. Existing sampling,
isobar, wind-rendering and benchmark tests also pass: 87 checks in total. The 128
Qt regression checks and embedded native GPU checks pass. The full repository
suite was not run; shared sampling/routing code is unchanged.

Next: measure usable/current coverage and cancellation waste explicitly before
changing active-job supersession, then introduce a byte-accounted cross-view
working set. Offscreen prediction should build on that, not on larger images or
an unbounded collection of opened forecasts.
