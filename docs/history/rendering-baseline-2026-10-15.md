# Rendering baseline — 2026-10-15

> **Status: HISTORICAL.** The synthetic scaling baseline captured 2026-10-15; the
> reference workload is
> [rendering-startup-baseline-2026-10-15.md](../rendering-startup-baseline-2026-10-15.md).
> Superseded by [rendering-performance-plan.md](../rendering-performance-plan.md).
> Evidence, not a plan.

This is the synthetic scaling baseline. Use [the normal startup baseline](../rendering-startup-baseline-2026-10-15.md)
as the primary workload and optimization priority for the installed app.

## Protocol

Captured serially with `tools/benchmark_rendering.py` on this Mac, before any
renderer changes. The harness was uncommitted, so reports correctly record a
dirty tree plus its harness SHA-256. These are exploratory development baselines,
not release certifications.

* macOS 26.6.2, arm64, 8 logical CPUs; Python 3.11.17, Qt 6.12.0,
  NumPy 2.4.6, Cairo 1.18.6, GDAL 3.13.3.
* Offscreen Qt, 1280 x 800 logical pixels, separate 1x and 2x DPR runs.
* Three fresh sandboxed processes per scenario; 180 measured camera steps each,
  five stationary warmup grabs; unpaced throughput replay.
* Default synthetic scene, no installed/real chart, weather, or marine datasets.
* Values below pool the 540 measured grabs per scenario, not percentile averages.
  A grab is a synchronous rendering probe, **not a presented display frame**.

Raw reports, including every sample and workload/environment metadata:

* [1x baseline](../profiles/rendering/2026-10-15/baseline-1x.json)
* [2x baseline](../profiles/rendering/2026-10-15/baseline-2x.json)

See [the protocol and plan](rendering-performance.md) for commands, manifests,
comparison safeguards, excluded layers, and interpretation limits.

## Results

| Scenario | 1x median / p99 / max (ms) | 2x median / p99 / max (ms) | Grabs >50 ms (1x / 2x) |
| --- | --- | --- | --- |
| Empty pan | 0.9 / 2.9 / 5.1 | 3.4 / 10.5 / 15.0 | 0 / 0 |
| Navigation pan | 0.9 / 198.9 / 227.7 | 3.5 / 268.5 / 280.5 | 30 / 30 |
| Reverse pan | 0.9 / 190.9 / 209.0 | 3.5 / 266.8 / 269.6 | 30 / 30 |
| Zoom out | 1.3 / 115.9 / 220.6 | 5.0 / 163.7 / 321.8 | 6 / 6 |
| Retained wind pan | 4.3 / 8.2 / 11.6 | 11.3 / 28.3 / 48.0 | 0 / 0 |
| Log pan | 1.9 / 4.6 / 22.2 | 5.6 / 15.7 / 26.6 | 0 / 0 |
| Combined pan | 5.5 / 199.7 / 212.9 | 14.2 / 284.6 / 290.3 | 30 / 33 |

All pan scenarios rebuilt both base and navigation images **30 times across
three repetitions**. Zoom-out rebuilt each six times during movement, then
required a post-gesture sharp refresh. The zoom-out post-gesture render/worker-idle
probe had a median of **189 ms at 1x / 278 ms at 2x**.

Known retained surface storage was **25.6 MiB at 1x / 102.3 MiB at 2x** in these
fixtures. Combined-pan process peak RSS reached **306.5 MiB / 526.9 MiB**. RSS
includes Python/Qt/GDAL, fixture data, and transient allocations; it is not the
size of a rendering cache. No long-run memory plateau was measured.

## Conclusions and next experiments

1. **Navigation cache misses dominate stalls.** Median cached pan is cheap, but
   whole-image rebuilds make latency tails unacceptable. First optimization
   experiment: prepare/render navigation independently of synchronous paint,
   preserving geometry and source points. Compare counts, stage spans, latency
   tails, and visual alignment before accepting it.
2. **Reverse navigation is not a retained-region cache.** Returning toward the
   starting view still rebuilds images. Evaluate spatial retention only after
   establishing a memory budget and representative real chart workloads.
3. **Retina costs matter even without navigation.** The retained-wind scene
   exceeded 16.7 ms on 35/540 grabs at 2x. Separate geometry selection and raster
   compositing measurements before choosing an optimization.
4. **Settlement is part of responsiveness.** Zoom previews can look inexpensive
   while the sharp final image stalls. Track both replay and post-gesture costs.
5. **Prefetch efficacy is not measured yet.** Next add representative local data,
   native input/presentation traces, source coverage assertions, and observable
   loading events. Do not infer blank-free panning or successful speculative
   loading from this synthetic baseline.

No numerical approximation, source-selection behavior, or application rendering
behavior changed to obtain these results.
