# R4 slice: bounded directional weather prefetch

The shared overlay scheduler now supports speculative frames, and the wind
renderer prefetches one frame ahead of release motion. No sampling, routing,
priority, interpolation, mask, density or contour code was changed. Prefetch
uses the exact visible `_render` path, so promoted content is identical by
construction to what a visible job would have computed.

## Implementation boundary

`OverlayRenderer` gains a strictly lower-priority speculative lane: one queued
and one running speculative job per renderer, plus a per-canvas completed store
capped at two frames / 96 MiB with LRU eviction. Speculation only starts while
no visible work is pending or running anywhere on that renderer. A completed
speculative frame is stored, never published; a later draw promotes it when
content key and native-zoom enclosure match, recording a hit with no new worker
work. Queued speculation matching new demand is adopted as the visible request
instead of duplicating it.

Cancellation keeps visible work unstarvable: obsolete content cancels queued
and running speculation immediately; same-content drifted speculation yields
only when its canvas actually queues new visible work — never merely because
the view repainted, which previously killed every settled prefetch on the next
paint. Hide/close/empty-source paths cancel speculation and account stored
bytes as waste. Forgetting never touches another canvas's slots, and FIFO
order across canvases is preserved.

`WindOverlayRenderer.prefetch_at` snapshots the last drawn content
(sources/palette/DPR) and captures its viewport through the production padded
build path: an earlier revision built it outside base-image construction, where
the transient render padding is zero, producing a 1222px viewport that could
never promote onto 1606px build-time frames. Promotion requires an exact size
match, so padding-consistent capture is load-bearing and covered by benchmark
evidence below. `TrackMap` triggers one prefetch per gesture from release
velocity (one second of continued travel, 192px minimum, clamped to 0.75
viewports) using the same center math as panning, so prediction and arrival
agree exactly. Trackpad pans additionally speculate mid-gesture from
exponentially smoothed pixel-delta velocity, throttled to one request per
500ms; the throttle slot is consumed only by qualifying motion. No network or
instrument activity is involved; only locally selected forecasts are prepared.

## Matched replay — 2026-10-16

Same verified private snapshot, frozen clock, native Metal, DPR 2, GPU
presenter and all earlier R2/R3 slices in both modes. New `pan_pause_pan`
scenario, 96 frames: pan 32, pause with an explicit production-path prefetch
trigger, then pan 32 more the same way. Control omits `--weather-prefetch`;
candidate enables it. Three isolated runs per mode.

| Median per-run metric | Control | Prefetch |
|---|---|---:|
| Post-gesture worker idle | 1.11 s | 1.10 s |
| Grab/readback p99 | 47.7 ms | 49.1 ms |
| Visible weather jobs (3 runs) | 15 | 9 |
| Speculative jobs (3 runs) | 0 | 3 |
| Cancelled jobs (3 runs) | 6 | 3 |
| Prefetch promotions | 0 | 3 (one per run) |
| Prefetch waste/evictions/cancels | — | 0 / 0 / 0 |

Each run queued one speculative frame during the pause, completed it with zero
waste, and promoted it on arrival — eliminating the entire second-leg visible
rendering (two visible jobs per run saved, including their cancellations).
Settling and frame latency are unchanged because the pause absorbs the
speculative work by design; the win is fewer jobs and earlier arrival coverage,
not faster individual jobs. Peak RSS was 2,123 vs 2,007 MiB (noise, not a
memory claim).

The weather-content gate matches viewport/forecast intersections and counts
promotions via publish events: 6 matched frames, 3 baseline-only mid-leg
publications (replaced by shifted previews in the candidate) and 3
candidate-only promotions. Scheduling legitimately changes which intermediate
viewports get their own frame; shared viewports agree byte-for-byte, and
promotion content follows the identical render path as visible jobs.

Private evidence in the approved OpenCode temporary directory:
`r4-prefetch-baseline.json`, `r4-prefetch-candidate.json`. Reproduce with
`tools/benchmark_startup_rendering.py --snapshot <private-snapshot>
--map-renderer gpu --scenarios pan_pause_pan --frames 96 --repeats 3`, adding
`--weather-prefetch` for the candidate. The benchmark disables prefetch unless
flagged so existing scenarios stay comparable; production keeps it enabled
behind no new setting.

## Validation and next boundary

Four new scheduling tests (promotion without visible work, starvation-free
cancellation, preemption on real visible demand, store caps and forget
cleanup) plus Qt release/trackpad trigger tests (threshold, clamping,
one-shot consumption, predicted center) and gate promotion tests. Targeted
rendering/sampling/marine/chart/ENC/benchmark checks pass (146 tests). All 128
Qt regressions and native GPU/base-chart checks pass. Formatting and diff
checks pass. Shared sampling/routing code is unchanged.

What remains: adjacent-zoom and multi-direction speculation, hysteresis/tuning
of the lookahead from measured preparation latency, long-session plateau and
marine-heavy validation, then review and commit. No automatic remote prefetch
is authorized by this implementation.
