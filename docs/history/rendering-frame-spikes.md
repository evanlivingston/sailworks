# UI-thread frame-spike investigation

> **Status: HISTORICAL.** The UI-thread frame-spike investigation, written 2026-10-17; its
> conclusions still stand, and this document is kept for the evidence behind
> them. The R0–R5 sequence in the implementation plan is the authority.
> Evidence, not a plan.

This follows the pan/zoom readiness improvements on the same feature branch.
It targets movement-time UI work, not general CI cleanup. CPU presentation
remains the default.

## Attribution

The startup harness now records per-input-frame stage totals and optional
replay-only UI cProfile output (`--profile-ui`). Profiling excludes startup and
settling and is marked in workload metadata, so profiled reports cannot be
silently compared with unprofiled timings. `pan_zoom_reverse` combines pixel-delta
pan, off-center +1/-1 wheel zoom, and reverse pan, without waiting for speculative
preparation between phases. Grab/readback timings remain synchronous, not FPS.

The diagnostic replay found synchronous base-image rebuilding (about 23–25 ms
per rebuild), including native OSM feature reads (about 10–16 ms), as well as
periodic coverage-panel work (roughly 150–170 ms total per replay). Some long
preparation/readback stalls remain unattributed; these results do not establish
one universal source of all tail latency. Frame-stage totals include work during
event delivery as well as the explicit grab and may include nested stages; do
not sum them as exclusive timings inside `grab_ms`.

## Retained improvement

The currents panel re-sorted immutable current-field metadata every 250 ms as
the view moved. In the profiled workload, repeated `manifest()` calls caused
thousands of JSON parses per replay. Retain only the ordered field tuple in the
panel until `FieldStore.changed` invalidates it. Every coverage refresh still
constructs a fresh `FieldSelection` and samples the actual position/time;
used/missing bookkeeping is not retained between queries. Enable/disable, add
and remove events clear the tuple, and panel disposal releases it.

`current_coverage` accepts an optional preordered selection; callers that do not
supply it keep the old behavior. No sampling, priority, masks, interpolation,
glyph density, frame scheduling or source-selection rules change. Clock changes
still query new times, and labels still refresh while the panel is visible.

The candidate diagnostic reduced combined panel-refresh CPU work from about
150–170 ms to 11–12 ms per replay; `current_coverage` itself fell to about 6 ms
total. These are profiled CPU-work observations, not frame-time acceptance data.

## Matched application timings

Three isolated native Metal/DPR-2 runs per scenario and policy, with the same
verified October 16 snapshot, pan/zoom improvements enabled, 96 input frames and
a 33.333 ms target interval. Controls ran before candidates; these are not
randomized trials. Both use harness hash
`e89b4d172b06b3c3487755a79cd22ef5969797d9ca4e99a83929d7d52fd0ff21`.
No competing test suites ran during measurement.

| Median per-run metric | Re-sort every query | Retain ordering |
|---|---:|---:|
| Straight-pan grab/readback p99 | 69.2 ms | 53.6 ms |
| Straight-pan p95 | 41.0 ms | 39.3 ms |
| Mixed pan/zoom/reversal p99 | 63.7 ms | 70.0 ms |
| Mixed p95 | 43.3 ms | 47.4 ms |
| Straight-pan prepared frames | 93/96 | 91/96 |
| Mixed prepared frames | 73/96 | 73/96 |

Straight-pan tails improved in this batch, but mixed movement did **not** show
a consistent frame-time gain. The retained change removes demonstrated redundant
UI work; it is not a claim that all frame spikes or mixed-gesture latency are
solved. Prepared counts describe geometric enclosure, not usable marine/weather
data. The existing weather-content gate passed 21 shared viewport/time frames
bit-for-bit, with three unmatched publications in each mode; those unmatched
frames are not parity matches. Current-coverage output parity and source/time
invalidation have separate targeted tests.

Validation passed: 140 targeted rendering/scheduling/benchmark/marine checks and
six Qt/GPU integration launchers, including the now 129-test Qt preview module
and native GPU checks. Black checks on 13 changed Python files and whitespace
checks passed. No full-suite or repository-wide CI pass is claimed.

Private evidence: `frame-spikes-ui-profile.json`,
`frame-spikes-order-profile.json`, `frame-order-control.json`,
`frame-order-candidate.json`, `frame-order-comparison.json` in the approved
OpenCode temporary directory. Reproduce the final comparison with:

```sh
python tools/benchmark_startup_rendering.py \
  --snapshot <private-snapshot> --map-renderer gpu \
  --scenarios trackpad_pan,pan_zoom_reverse --frames 96 --repeats 3 \
  --interval-ms 33.333 --weather-prefetch --output <candidate.json>
```

Add `--uncached-coverage-fields` for the control. Add `--profile-ui` only for a
separate diagnostic run, not one side of a timing comparison.

## Rejected trials and remaining work

* Skipping hidden-panel polling did not address this saved layout: the currents
  panel is exposed. That trial and its benchmark flag were removed, rather than
  shipping a neutral change as a frame-time improvement.
* A bounded OGR memory-window cache passed synthetic feature/pixel tests but
  failed exact feature parity on the real seamark PBF: one native layer/view
  returned 741 records versus 94 through the candidate. Driver/filter interaction
  was not diagnosed. The entire cache trial was removed; no seamark selection,
  filtering or portrayal code changes remain. Do not weaken the gate or infer
  that offscreen features could safely be dropped.

Synchronous seamark portrayal and intermittent GPU preparation/readback stalls
still need investigation. Marine-heavy render replay, long-session memory and
wider GPU backend validation remain incomplete.
