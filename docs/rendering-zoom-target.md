# Preparing weather during zoom animation

This extends the pan feature on the same branch; no separate PR is opened.
CPU remains the default presenter. Sampling, forecast priority, masks, density,
contouring and geographic projection formulas are unchanged.

## Behavior

Wheel zoom already has a committed target and a fixed geographic cursor anchor.
After 100 ms without another input, prepare that target's weather through the
existing idle-priority speculative lane while the camera is still easing toward
it. Compute the future center on a temporary `MapViewport`; do not temporarily
mutate the live camera or change animation timing to wait for data.

Another wheel/pinch input cancels unfinished speculation immediately and
restarts the debounce. At the final native zoom, normal visible demand adopts
the running job or promotes its completed frame. Other scales continue to use
the existing scaled preview. Visible work keeps its priority.

Native pinch has no animated future target: a quiet interval inside the gesture
may prepare its current zoom instead. Ending the gesture stops the debounce;
hide/close use existing cancellation paths. Disabling smooth wheel animation
keeps the immediate visible render path rather than adding a speculative delay.

Zoom speculation is stricter than pan drift: changing target zoom, viewport size
or anchor-derived center replaces the old speculative job. An already-prepared
matching camera does not queue duplicate zoom work, including a reversal to the
original view. Existing pan drift tolerance is unchanged.

This is committed-target preparation, **not** arbitrary idle guesses at ±1 zoom
or a promise to remove animation/forecast latency entirely. No network requests
or additional completed-frame stores are introduced.

## Measurement protocol

The startup harness adds `wheel_zoom` and `wheel_zoom_reverse` scenarios, using
actual wheel events at an off-center anchor. A +1-level event starts the first
scenario. The reversal scenario adds a -1-level event at input frame eight.
Both modes share exactly the same input-frame schedule, frozen forecast time,
verified snapshot and render settings, without candidate-only preparation waits.

`first_prepared_zoom_ms` measures from the **last** zoom input to a prepared frame
at its requested zoom after animation converges. It measures geometric image
enclosure, not usable-data coverage, display scanout or FPS. Grab/readback p99,
job events, cancellations and weather-content hashes remain recorded separately.

```sh
python tools/benchmark_startup_rendering.py \
  --snapshot <private-snapshot> --map-renderer gpu \
  --scenarios wheel_zoom,wheel_zoom_reverse --frames 96 --repeats 3 \
  --interval-ms 16.667 --weather-prefetch --output <candidate.json>
```

Add `--disable-zoom-prefetch` for the control. Both policies retain the earlier
pan improvements. Raw reports and snapshot remain private.

## Validation

Focused tests cover off-center target geometry without live-camera mutation,
debouncing and timer disposal, target/anchor replacement, adoption only at the
native target scale, avoiding duplicate work on return to a prepared camera,
and identical pixels/vector arrays for untagged, pan-tagged and zoom-tagged
jobs at the same requested viewport.

Validation passed: 102 targeted rendering/scheduling/benchmark checks and six
Qt/GPU launchers (including the 128-test Qt preview suite and native GPU checks).
Black checks on nine changed Python files and whitespace checks passed. These
are local results, not a claim that repository-wide CI is green.

## Matched application results — 2026-10-16

Three isolated runs per scenario/policy, native Metal/DPR 2, on the verified
October 16 startup snapshot. Control runs preceded candidate runs; these are
not randomized trials. Both reports use harness hash
`339ce10553ca619dc09cd0e828ebf037887c67e019fec128ad2a7ac25843986d`.
No competing test suites were launched during these replays.

| Median per-run metric | Control | Zoom preparation |
|---|---:|---:|
| Requested zoom prepared after final input | 1,079 ms | 764 ms |
| Zoom grab/readback p99 | 24.1 ms | 27.4 ms |
| Reversed zoom prepared after final input | 330 ms | 341 ms |
| Reversal grab/readback p99 | 21.9 ms | 18.6 ms |
| Started weather jobs, normal zoom, per run | 1 | 1 |
| Started/cancelled weather jobs, reversal, per run | 0 / 0 | 1 / 1 |

Normal zoom reached prepared weather about 316 ms (29%) sooner, with one
speculative job adopted per run and no extra started jobs. This is a readiness
gain, not an established frame-rate gain: normal-zoom p99 increased by about
3.3 ms and one candidate run reached 41.5 ms p99. Reversal back to the original
cached view has no clear readiness improvement and costs one cancelled job
(58–68 ms execution) per run. No obsolete target was published, and no duplicate
job was rendered for the already-prepared return camera.

The existing weather-content gate passed all nine shared viewport/time frames
bit-for-bit, with zero baseline-only or candidate-only publications. The
separate same-viewport marker test also covers normal/pan/zoom-tagged jobs.
Peak normal-zoom RSS was 2,168,143,872 bytes (control) versus 2,181,185,536 bytes
(candidate); this does not establish a memory plateau or a total-VRAM bound.

Private evidence: `zoom-target-control.json`, `zoom-target-candidate.json`,
`zoom-target-comparison.json` in the approved OpenCode temporary directory.
This replay covers a single +1-level wheel event and an early reversal, not
every extended wheel stream or native pinch trajectory. Marine-heavy workloads,
non-Metal backends and long-session resource gates remain incomplete.
