# R3 slice: bounded cross-view weather source retention

`WindOverlayRenderer` now retains up to 64 selected forecast handles across
display jobs instead of two. Decoded `GridIndex` bands stay under the existing
128 MiB retention budget; deselected or file-changed sources are closed
explicitly. No sampling, routing, source-priority, interpolation, mask, density
or contour code was changed. Both Qt and GTK use this renderer; CPU/GPU
selection is unrelated.

## Implementation boundary

Each job still opens only its selected sources through a job-local scope, with
one shared sampler per job and explicit close of nonretained handles on success,
cancellation and error. The wider cache only affects what survives *between*
jobs: previously the third distinct source evicted the first, so consecutive pan
jobs reopened the same 33 files. Now the full selected set survives while the
view moves, and decoded bands/indexes survive with it.

Source identity includes path, name, centre, issue/valid times, bounds and file
stat (dev/ino/size/mtime). A changed file is closed and reopened; a deselected
source is closed rather than leaked. Eviction respects leased providers so an
active pass cannot lose its handles. Completed jobs retain images, vectors and
numeric counters, never the temporary handle set. No speculative or remote
acquisition was introduced.

This is not a geographic tile cache or a prefetch: there is no predicted extent,
no adjacent-zoom retention beyond existing image margins, and no usable-coverage
tracking beyond the existing prepared-view probe. Driver caches, geometry, Cairo
surfaces and other maps remain outside the explicit ledger.

## Matched replay — 2026-10-16

Same verified private snapshot, frozen clock, native Metal, DPR 2, GPU presenter,
base-chart worker and job-local reuse in both modes. Three isolated runs per
scenario, 48 frames each. Control uses `--weather-source-limit 2`; candidate
uses the default 64. No network, credentials or instruments.

| Median per-run metric | Limit 2 | Limit 64 |
|---|---|---:|
| Pan post-gesture worker idle | 1.44 s | 1.10 s |
| Reverse-pan post-gesture worker idle | 0.93 s | 0.96 s |
| Pan grab/readback p99 | 48.5 ms | 44.6 ms |
| Reverse-pan grab/readback p99 | 47.9 ms | 42.7 ms |
| Startup worker idle | 11.2 s | 10.4 s |

Private file opens across three pan runs fell from 360 to 99; reverse-pan from
283 to 99. The retained cache on this snapshot is 33 sources, about 92 KiB of
explicit decoded bands plus about 296 KiB of coordinate vectors — far below the
128 MiB decoded budget. Peak process RSS was 2,099 vs 2,086 MiB pan and
2,068 vs 2,082 MiB reverse-pan: no general memory saving is claimed.

The strict comparison validates all nine published weather frames by viewport
and forecast time: image bytes, dimensions, native wind-vector bytes and counts
are identical. These are worker-settling and synchronous grab/readback numbers,
not GPU execution, scanout or FPS. Cancellation counts are unchanged (6 per
mode); scheduling and prefetch remain separate work. The snapshot has zero
marine fields; do not generalize to marine-heavy workloads.

Private evidence in the approved OpenCode temporary directory:

* `r3-crossview-baseline.json`, `r3-crossview-candidate.json`: controls.
* Comparison via `tools/benchmark_startup_rendering.py --compare` passes both
  timing compatibility and the weather-content identity gate.

Reproduce with `--snapshot <private-snapshot> --map-renderer gpu --scenarios
pan,reverse_pan --frames 48 --repeats 3`; add `--weather-source-limit 2` for
the control.

## Validation

New resource tests cover second-view handle/band reuse without reopening,
deselection close, failed-index retry without extra opens, and zero-budget
equivalence. Existing sampling, isobar, wind, marine, chart, ENC and benchmark
checks pass (138 tests). All 128 Qt regressions and native GPU/base-chart
checks pass. Formatting and diff checks pass. Shared sampling/routing code is
unchanged, so the full suite was not run.
