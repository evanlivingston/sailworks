# M0 / B1: embedded non-readback timing probe

> **Status: HISTORICAL.** The B1 embedded timing-probe record, written 2026-10-17.
> Superseded by [ADR-001](../adr-001-grib-rendering-architecture.md) and the issue
> program (#62-#70). Evidence, not a plan.

2026-10-17; [epic #51](https://github.com/evanlivingston/gweatherrouting/issues/51),
[plan experiment index](../rendering-performance-plan.md#experiment-contracts-and-results-b1-e1-e3).

## Outcome

**B1 supports a limited render-signal proxy, not presentation measurement.**
The real embedded application can be driven without timed readback, retaining
which input revisions were and were not observed at the render callback. This
is sufficient for carefully controlled mechanism experiments. It is not a
complete M0 baseline, a renderer optimization, or evidence of the reference application parity.

The two-iteration/two-native-attempt budget is consumed. The written review
below closes this experiment rather than extending it into a larger matrix.

## Implementation and endpoint

`tools/benchmark_startup_rendering.py --measurement-mode native-render` reuses
the private-copy/checksummed/offline startup sandbox. Its timed replay calls
`tools/native_rendering_probe.py` instead of forcing `_present()`, QWidget grabs,
or framebuffer readback. Startup, warm-up and final diagnostic grabs remain
**outside** this timed window.

- A precise single-shot QTimer dispatches input against fixed absolute deadlines,
  without waiting for render completion. GUI stalls still make dispatch late;
  intended and actual timestamps are both retained. Overdue input is delivered,
  not silently dropped or allowed to reset the schedule.
- The adapter tags accepted scene submissions with input revision, MapFrame
  sequence, view/time, image cache keys and geometry counts. These resource
  identifiers are not usable-data coverage or semantic field provenance.
- Direct `beforeSynchronizing` freezes the submitted-frame attribution for a
  render; direct `afterRendering` timestamps that render's signal. An older render
  cannot acknowledge the newest input merely because that input arrived before
  callback processing. Thread IDs and raw callbacks are retained.
- Per-input status is observed, superseded-unobserved, or timeout-unobserved.
  Duplicate callbacks do not become extra input samples. Preserve the full input
  denominator; observed-only percentiles are conditional, not all-input latency.
- The endpoint is the **offscreen embedded scene's Qt render signal**, not GPU
  completion, top-level composition, compositor presentation, or scanout. Both
  presentation and GPU-completion flags are explicitly false.
- The mode requires native GPU presentation and currently supports stationary,
  pixel-delta pan/reversal, and second-resolution scrub/return. Wheel/combined
  zoom, both-map timing, marine-heavy fixtures, and platform presentation tracing
  remain open. Native reversal uses pixel-delta wheel input, unlike the legacy
  grab-mode reversal's direct pan calls; do not compare their trajectories.
- New output uses schema 2. The legacy comparison command rejects it, including
  schema-2-to-schema-2 comparisons; a vetted asynchronous comparison protocol is
  still required before acceptance benchmarking. Empty legacy `grab_ms` arrays
  in native reports must not be interpreted as zero-cost rendering.

## Reproduce the smoke, not a full baseline

Run serially with the project Python environment; substitute the verified
private fixture and an external output path. No capture/installation/network
operation is required. The native window will appear.

```sh
env -u QT_QPA_PLATFORM -u GWR_GPU_WEATHER PYTHONPATH="$PWD" \
  python tools/benchmark_startup_rendering.py \
  --snapshot <verified-private-october-16-snapshot> \
  --map-renderer gpu --measurement-mode native-render \
  --scenarios reverse_pan --frames 24 --repeats 1 --warmup-frames 2 \
  --interval-ms 16.667 --native-drain-timeout-s 2 \
  --settle-timeout-s 60 --worker-timeout-s 110 \
  --output <external-output.json>
```

The validation runner additionally bounded each full smoke command to 120
seconds. Attempt 1 used `trackpad_pan`; attempt 2 used `reverse_pan`. No further
native runs were launched under this contract.

## Evidence

Base commit: `1f1464971db340d5c5581a4d6452fc61e9d7754a`, with local changes;
exact dirty-diff and harness hashes are recorded in each private raw report.
No commits/pushes or production renderer changes were made for this experiment.

Both runs verified snapshot content
`bd9464bf39629676d34fc570c2f8b6d647a8e6ff897176fd94e0a17200f51dac`,
frozen at `2026-10-16T08:00:00` local. The directory date is not a dataset identity:
this content hash also appears in the older October 15 report, whose clock differs.

Environment: macOS ARM64, Cocoa, Qt 6.12.0, Metal, DPR 2. The second run recorded
a nominal 120 Hz display refresh rate; the replay requested 60 Hz input. This is
not a claim of 120 Hz rendering. Workload: 33 forecasts, 32 enabled charts,
dense barbs, 1 hPa isobars, six tide datasets, zero marine fields; existing saved
navigation/layout retained. No logged warnings or GPU fallback in either run.

| Smoke | Inputs observed / total | Superseded unobserved | Render callbacks | Scene submissions |
| --- | --- | --- | --- | --- |
| 1: pan | 17 / 24 | 7 | 24 | 25 |
| 2: reversal | 17 / 24 | 7 | 21 | 27 |

Attempt 2's **observed-input-only** dispatch-to-signal p50/p95 was
14.8/38.0 ms. Intended-deadline-to-signal p50/p95 was 27.9/72.7 ms; dispatch
lateness p50/p95 over **all 24 inputs** was 12.3/43.4 ms. Thus excluding dispatch
delay would conceal material GUI/input scheduling delay. These tiny samples
are mechanism diagnostics, not statistically useful acceptance percentiles.

Attempt 2 recorded five texture uploads totaling 97,934,880 bytes and 11,697,600
vertex-upload bytes over input **plus the two-second callback drain**. Two
base-image rebuilds consumed 52.8 ms combined (maximum 30.8 ms), including nested
chart/composition work. Do not sum nested spans or attribute all upload cost to
CPU assembly. These observations justify further attribution, not a causal claim.

Attempts used different scenarios and harness revisions (attempt 2 added intended
latency, replay-window counters, content identifiers and refresh metadata). They
are deliberately **not** a before/after optimization comparison.

Private raw artifacts are `rendering-m0-native-smoke-1.json` and
`rendering-m0-native-smoke-2.json` in the local OpenCode temporary workspace.
Keep these reports outside Git: they contain local paths/view/navigation state.
Harness SHA-256 values, respectively:

```text
e2e757979cf5e227b68884b4a7043bbc2796b7ce406ffb7bb7695fefe5a28382
d58a7a536bfff7199cb99a69a345eddc77c4ff4b257d9d00fb5680f698997ca4
```

## Validation and review

Final targeted checks: **21 passed**, including deterministic attribution,
duplicate/stale/out-of-order/missing callbacks, denominator preservation, bounded
event-loop exit, adapter restoration on fallback, no forced paint/readback calls,
legacy comparison rejection, and existing snapshot/replay/protocol checks.
Black passed on three changed Python files; whitespace check passed. Two native
smoke processes exited 0. This is not a full-suite or platform-matrix pass.

**Decision: continue narrowly.** Keep the render-signal proxy for E2's retained
camera/stalled-preparation control. Before starting that experiment, specify its
matched scene/trajectory, valid retained coverage, deliberately blocked work,
baseline/candidate behavior, and budget. Keep top-level presentation acceptance
open; do not choose a backend, raise resource limits, or tune prefetch from this
smoke. E1 and E3 remain unproven, and M0 remains open.
