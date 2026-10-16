# R3 slice: view-independent weather field working set

`WindOverlayRenderer` now keys sampled weather fields by source set, forecast
instant, field and geographic extent — not by exact screen center. Previously
sampled bounds are reused when they contain a new request; resampled fill grids
reuse on exact matches. No sampling, routing, priority, interpolation, mask,
density or contour code was changed. Both Qt and GTK use this renderer.

## Implementation boundary

Native wind/pressure columns cache under `(sources, instant, field, bounds)` in
a 64-entry / 32 MiB LRU. A new viewport reuses a cached entry only when the
cached geographic bounds enclose the requested bounds; returned columns are
filtered to the request, so there is no overfetch and no changed point can leak
in from outside. Mixed-grid resample grids reuse on exact key matches only,
since their regular grid depends on the precise bounds. Style-only changes
therefore skip resampling while time/source changes correctly miss.

Source versions include path, name, centre, issue/valid times, bounds and file
stat, so changed files and deselected sources miss rather than serving stale
data. Failed opens/indexes still retry on the next pass. Completed jobs retain
images, vectors and counters only. Cancellation checks are unchanged, and the
cross-view handle retention plus 128 MiB decoded budget from the earlier slices
remain in force. No speculative or remote acquisition was introduced.

On a miss, only newly exposed geographic strips are sampled: the request minus
previously cached bounds is partitioned into disjoint rects, each sampled once
and merged with reused cached points before column grouping. Partition unions
are exact, shared-edge nodes deduplicate deterministically, and merged columns
are proven identical to a fresh full-bounds sample. Resampled fill grids reuse
on exact key matches only — an earlier revision returned contained (non-equal)
grids, which would serve the wrong regular lattice, and this is now covered by
an explicit test.

An earlier trial in this same slice quantized bounds to a 1-degree grid so
small pans would share superset samples. It sampled up to ~2.3x the area per
miss with zero hits on the monotonic startup trajectories and made published
jobs ~40% slower, so it was replaced by containment reuse, which cannot
overfetch by construction.

## Follow-up: strip sampling on the same replay

With containment reuse in place, native-field misses sample only uncovered
strips merged with cached points (control: `--disable-weather-field-cache`).
Three runs per scenario, 48 frames each, same snapshot and GPU presenter:

| Median per-run metric | Disabled | Strips |
|---|---|---:|
| Pan post-gesture worker idle | 1.10 s | 1.10 s |
| Reverse-pan post-gesture worker idle | 0.98 s | 0.97 s |
| Published weather-job duration, pan | 793 ms | 766 ms |
| Published weather-job duration, reverse | 784 ms | 846 ms |

Strips fired (18 sampled / 18 reused extents pan; 12 / 6 reverse) and all nine
published frames are byte-identical, but settling is neutral: the mixed-grid
resample passes still sample their full regular grids on every miss and
dominate these trajectories. Peak RSS was 2,070 vs 2,049 MiB pan and 2,069 vs
2,061 MiB reverse-pan. Private evidence: `r3-strip-baseline.json`,
`r3-strip-candidate.json`. The resample lattice (snap regular-grid points to a
global lattice so overlapping views share samples) is the explicit next step;
it is not implemented here.

## Matched replay — 2026-10-16

Same verified private snapshot, frozen clock, native Metal, DPR 2, GPU
presenter, base-chart worker, job-local reuse, cross-view handle retention and
coalesced scheduling in both modes. Three isolated runs per scenario, 48
frames each. Control uses `--disable-weather-field-cache`; candidate uses the
default containment cache.

| Median per-run metric | Disabled | Containment |
|---|---|---:|
| Pan post-gesture worker idle | 1.10 s | 1.11 s |
| Reverse-pan post-gesture worker idle | 0.98 s | 0.99 s |
| Pan grab/readback p99 | 43.2 ms | 47.1 ms |
| Reverse-pan grab/readback p99 | 48.2 ms | 50.6 ms |
| Startup worker idle | 10.4 s | 10.4 s |

Field-cache hits were zero on both monotonic trajectories: new pan bounds
always extend beyond previous ones, and return trips reuse at the image level
before a field job starts. The cache is therefore neutral here by design — it
adds no sampling work when it misses (exact bounds, same as uncached) and pays
off on identical/return views, style toggles and repaints, as proven by the
unit tests below. Grab p99 differences are synchronous readback noise around
unchanged worker settling. Peak RSS was 2,085 vs 2,073 MiB pan and 2,093 vs
2,095 MiB reverse-pan: neutral.

The strict comparison validates all nine published weather frames by viewport
and forecast time: image bytes, dimensions, native wind-vector bytes and counts
are identical. These are worker-settling and grab/readback numbers, not GPU
execution, scanout or FPS. The snapshot has zero marine fields; do not
generalize to marine-heavy workloads.

Private evidence in the approved OpenCode temporary directory:

* `r3-contain-baseline.json`, `r3-contain-candidate.json`: controls.
* Comparison via `tools/benchmark_startup_rendering.py --compare` passes both
  timing compatibility and the weather-content identity gate.

Reproduce with `--snapshot <private-snapshot> --map-renderer gpu --scenarios
pan,reverse_pan --frames 48 --repeats 3`; add `--disable-weather-field-cache`
for the control. The rejected quantized-superset trial is not preserved as a
candidate; its numbers are reported above for the record.

## Validation and next boundary

New tests prove identical pixels/vectors on cache replay and strip merge,
exact-bounds sampling on miss (zero overfetch), return-view reuse without
resampling, exact-match resample reuse, rect-partition geometry, a
disabled-cache control, and byte-budget accounting. Targeted rendering,
sampling, marine, chart, ENC and benchmark checks pass (141 tests). All 128 Qt
regressions and native GPU/base-chart checks pass. Formatting and diff checks
pass. Shared sampling/routing code is unchanged.

What remains for R3/R4: a resample lattice so overlapping views share regular
grid points, measured usable/current data coverage beyond the geometric
prepared-view probe, and bounded offscreen prefetch built on this keyed
working set. No automatic remote prefetch is authorized.
