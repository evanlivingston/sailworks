# M0 measurement audit — 2026-10-17

Part of [epic #51](https://github.com/evanlivingston/gweatherrouting/issues/51).
See the [canonical plan](rendering-performance-plan.md) for current decisions.
This is a source audit of `dev` commit `1f14649`, not a new performance result.

## What we can reuse

`tools/benchmark_startup_rendering.py` already provides:

- private copy-on-write/ordinary-copy snapshots, path remapping, file checksums,
  and verification (`capture`, `verify_snapshot`, `prepare_sandbox`);
- real SessionController/PreviewWindow restoration, frozen time, offline/cache-only
  policy and disabled credentials/instrument acquisition;
- pan, reversal, scrub, pixel-delta trackpad and wheel/combined zoom trajectories;
- worker timings and scheduling/publication telemetry, stage timings, retained
  surface estimates, RSS high-water, GPU upload counters/resource estimates;
- viewport/time-aware hashes for completed/published weather frames.

`tools/benchmark_rendering.py:compare_reports` checks schema, workload,
environment, harness identity, repetitions, and complete grab samples. Preserve
these protections; add a distinct protocol for new endpoint semantics instead of
reusing `grab_ms` for unrelated timing.

## Measurement blockers

| Finding | Code evidence | Consequence |
| --- | --- | --- |
| Timed replay forces rendering/readback | startup `grab()` calls `gpu._present()`, `grabFramebuffer()`, and `canvas.grab()`; replay appends its duration to `grab_ms` | These numbers are synchronous probes, not natural frame cadence/presentation latency |
| Input pacing depends on completed work | startup loop performs input, grab, processEvents, then waits until the target interval | Slow rendering delays subsequent input; not an independent input producer |
| Event-loop delay is not presentation | `queued_callback_ms` times a zero-delay QTimer callback | Useful GUI service metric, not proof the input's frame appeared |
| Embedded render status is not presentation | `qt/gpupresenter.py` binds `quickWindow().afterRendering` to `_rendered`, which reports backend readiness | Neither a revision-tagged input receipt nor proof the top-level QQuickWidget composition was displayed |
| Standalone swap experiment is different | `tools/experiment_gpu_rendering.py` uses QQuickView `frameSwapped`, waits per submission, and performs quality readback after timing | Useful mechanism precedent, but serial producer and standalone fixture cannot establish real embedded-app responsiveness |
| Coverage gates are incomplete | completed-image hashes and prepared-view enclosure do not establish per-field usable data at each presented revision | Missing weather can still make interaction cheaper; retain explicit coverage limitations |
| Memory is partial | retained-surface/resource estimates and process peak RSS | No current-memory plateau, measured GPU allocation, or complete in-flight ledger |
| Layer/workload matrix is incomplete | startup worker selects one active canvas; no independent charts/fill/barbs/contours matrix in CLI; October 16 report has no marine fields | Both-map and marine-heavy acceptance remain separate work |

## Baseline identity and private fixtures

The historical October 15 report records snapshot
`bd9464bf39629676d34fc570c2f8b6d647a8e6ff897176fd94e0a17200f51dac`,
4,939 files and 1,668,793,568 bytes. Later reports reference an October 16
snapshot. They are not automatically interchangeable. Local availability and
manifest verification are **not yet established by this audit**. Do not recapture
installed data or pool either fixture into a new baseline without recording its
identity and justification. Private routes/forecasts must stay outside Git.

## Decision

Reuse isolation, workload identity, gesture paths, and diagnostic telemetry.
Do not rerun the old matrix and relabel it as an input-to-present baseline.
Implement/evaluate B1's separate non-readback protocol first, preserving raw
input/revision/callback attribution and honest endpoint labels. If only an
input-to-render proxy is available, retain that limitation and separately decide
whether platform presentation tracing is necessary.

No renderer/backend choice, E1–E3 success, new measured bottleneck ranking, or
performance improvement follows from this audit.

## Follow-up

B1 subsequently located and verified the October 16 fixture, implemented a
separate render-signal proxy, and completed two native smoke attempts. See
[the evidence and budget review](history/rendering-m0-native-probe.md). The audit's
presentation/coverage limitations still apply; historical grab reports were not
reclassified as native presentation measurements.
