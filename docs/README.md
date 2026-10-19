# Documentation index

**The architecture decision is [ADR-001](adr-001-grib-rendering-architecture.md).
The work program that implements it is epic #51, issues
[#62–#70](milestones.md).** Read ADR-001 first, then work one issue at a time.

This file is the index for `docs/`. It says which document is authoritative,
which is background, and which is a historical experiment record. It does not
replace any of them.

## Which document do I read for X?

| If you want to know… | Read |
| --- | --- |
| What the app does at start-up, and the opt-in early-weather path | [`early-weather-startup.md`](early-weather-startup.md) |
| The target rendering architecture, and what was decided (D1–D5) | [`adr-001-grib-rendering-architecture.md`](adr-001-grib-rendering-architecture.md) |
| The layer/ownership model, the R0–R5 sequence, the non-negotiable invariants and the measurement contract | [`rendering-implementation-plan.md`](rendering-implementation-plan.md) |
| The current performance roadmap, milestone status, hypothesis ledger and the A0 decision log | [`rendering-performance-plan.md`](rendering-performance-plan.md) |
| Which issue to work on next, and in what order | [`milestones.md`](milestones.md) — GitHub is the tracker, this is the local index |
| How routing works, including current-assisted routing and coastline checks | [`current-routing.md`](current-routing.md) |
| The routing safety limits (wind cap, motoring fallback, speed reduction) | [`routing-safety.md`](routing-safety.md) |
| How weather is selected, decoded, interpolated and time-blended | [`weather-sampling.md`](weather-sampling.md) |
| Where the chart controls live in the UI | [`chart-layers.md`](chart-layers.md) |
| The the reference application-style chart quilting design | [`chart-quilting-plan.md`](chart-quilting-plan.md) |
| Tides and currents: the plan, the manager UI, and CHS direction semantics | [`tides-currents-plan.md`](tides-currents-plan.md), [`tides-currents-manager.md`](tides-currents-manager.md), [`chs-current-directions.md`](chs-current-directions.md) |
| Marine data: availability, validation and WCOFS currents | [`marine-data-validation.md`](marine-data-validation.md), [`wcofs-currents.md`](wcofs-currents.md) |
| Extending the GTK tool dock with new tool panels | [`tool-panels.md`](tool-panels.md) |
| How panel docking and tab reordering behave | [`panel-docking.md`](panel-docking.md) |
| The Qt frontend, and GTK→Qt parity status | [`qt-first-pass.md`](qt-first-pass.md), [`qt-parity.md`](qt-parity.md), [`qt-default-validation.md`](qt-default-validation.md) |
| Wind style: streamlines, and the shoreline land mask | [`rendering-streamlines.md`](rendering-streamlines.md), [`rendering-wind-shoreline-mask.md`](rendering-wind-shoreline-mask.md) |
| The GPU renderer host, and the opt-in numerical weather path | [`gpu-embedded-rendering.md`](gpu-embedded-rendering.md), [`gpu-weather-interpolation.md`](gpu-weather-interpolation.md) |

## Status labels

| Label | Meaning |
| --- | --- |
| `CANONICAL` | Authoritative. Read this one; other documents defer to it. |
| `REFERENCE` | Accurate background. Read it if it is relevant to your change. |
| `HISTORICAL` | Experiment record or superseded document. Read it for evidence; do not follow it as a plan. |
| `EXPERIMENT` | Research code, not shipped. Not imported by the application. |

## Architecture, roadmap and program

| Document | Status | What it is |
| --- | --- | --- |
| [`adr-001-grib-rendering-architecture.md`](adr-001-grib-rendering-architecture.md) | `CANONICAL` | The accepted architecture decision: D1–D5, the disposition of the seven structural gaps, the amendments to the R0–R5 spec, and the mapping to issues #62–#70. Read this first. |
| [`rendering-performance-plan.md`](rendering-performance-plan.md) | `CANONICAL` | The canonical performance roadmap: current checkpoint, reference workload, milestones, hypothesis ledger, experiment-contract index and the A0 decision log. |
| [`rendering-implementation-plan.md`](rendering-implementation-plan.md) | `CANONICAL` | The detailed R0–R5 implementation sequence, layer/ownership model, non-negotiable invariants and measurement contract, as amended by ADR-001 §6. |
| [`milestones.md`](milestones.md) | `CANONICAL` | Local index of epic #51 and issues #62–#70 with milestone and dependency columns. GitHub remains the authoritative tracker. |
| [`grib-architecture-analysis.md`](grib-architecture-analysis.md) | `REFERENCE` | Current-state GRIB loading and visualization architecture with file/line citations, and the seven structural gaps ADR-001 disposes of. Evidence, not a plan. |
| [`../tools/gate_runner.py`](../tools/gate_runner.py) and [`../tools/gates.py`](../tools/gates.py) | `CANONICAL` | The performance gate runner and its registry. One command gates a rendering change against the acceptance thresholds. See the "Performance gate runner" section of the [root README](../README.md). |
| [`reference-architecture-analysis.md`](reference-architecture-analysis.md) | `REFERENCE` | Reference techniques T1–T8 from a read-only analysis of a locally installed the reference application build. A hypothesis source, not a benchmark and not implementation source. |
| [`profiling-priorities.md`](profiling-priorities.md) | `REFERENCE` | Already-measured cost slices and their limits. Do not re-measure what is recorded here. |
| [`rendering-m0-measurement-audit.md`](rendering-m0-measurement-audit.md) | `REFERENCE` | Source audit of what the instrumentation can and cannot prove, and why readback is not presentation. |

## Start-up and application lifecycle

| Document | Status | What it is |
| --- | --- | --- |
| [`early-weather-startup.md`](early-weather-startup.md) | `REFERENCE` | How to run the opt-in `--early-weather` start-up, and what it does and does not change. |
| [`early-weather-startup-integration.md`](early-weather-startup-integration.md) | `HISTORICAL` | The M0/A0l integration checkpoint for opt-in early-weather start-up. Superseded by ADR-001 and the issue program. |

## Rendering evidence — historical experiment records

These are the evidence base the repository's rules depend on ("preserve rejected
approaches and reasons", "historical reports must not be overwritten"). Read them
for the results; do not follow them as a plan.

| Document | Status | What it is |
| --- | --- | --- |
| [`rendering-architecture-evaluation.md`](rendering-architecture-evaluation.md) | `HISTORICAL` | The A0a–A0k experiment record, including every negative result. The authoritative narrative for the A0 program. |
| [`rendering-a0g-progress.md`](rendering-a0g-progress.md) | `HISTORICAL` | A0g: unattended continuation — persistent new-area loading, honest partial coverage, reference-empty wind, native A→B→A publication. |
| [`rendering-a0h-resident-worker.md`](rendering-a0h-resident-worker.md) | `HISTORICAL` | A0h: single-owner resident numerical worker with deferred fidelity checks and matched timing. |
| [`rendering-a0i-independent-numerical.md`](rendering-a0i-independent-numerical.md) | `HISTORICAL` | A0i: numerical startup independent of the chart application, and the authorized fixture rebaseline. |
| [`rendering-a0j-early-display.md`](rendering-a0j-early-display.md) | `HISTORICAL` | A0j: early weather publication while the chart host starts. Mechanism passed; visual acceptance blocked and later repaired by A0k. |
| [`rendering-a0k-shader-safety.md`](rendering-a0k-shader-safety.md) | `HISTORICAL` | A0k: the missing-data shader repair and its zero-alpha gate. The corrected renderer, not the original, is the reference for missing-data portrayal. |
| [`rendering-m0-native-probe.md`](rendering-m0-native-probe.md) | `HISTORICAL` | B1: the embedded non-readback timing probe. A render-signal proxy, not presentation measurement. |
| [`rendering-m0-float-textures.md`](rendering-m0-float-textures.md) | `HISTORICAL` | E1/E1b: native float transport. Hardware filtering failed the 2e-5 gate; explicit shader arithmetic passed it. |
| [`rendering-m0-residency.md`](rendering-m0-residency.md) | `HISTORICAL` | E3a/E3b: decoded-band residency and the actual PR #50 staging workload. |
| [`rendering-m0-retained-field.md`](rendering-m0-retained-field.md) | `HISTORICAL` | E2a/E2b: retained field versus native interaction. The worker-only cache is not a demonstrated pan-fluidity fix. |
| [`rendering-m0-persistent-field.md`](rendering-m0-persistent-field.md) | `HISTORICAL` | E2c: persistent numerical field with exact selection indirection. |
| [`rendering-m0-app-persistent-field.md`](rendering-m0-app-persistent-field.md) | `HISTORICAL` | E2d: the disposable full-app persistent-field control. Lifecycle gate failed; the artifacts are diagnostic evidence only. |
| [`gpu-rendering-experiment.md`](gpu-rendering-experiment.md) | `HISTORICAL` | The 2026-10-15 GPU feasibility experiment, including the reproduced fractional-pan composite failures. |
| [`gpu-texture-parity-r0.1.md`](gpu-texture-parity-r0.1.md) | `HISTORICAL` | R0.1: the texture-parity fix for the reproduced 2× Metal failures, with the unchanged quality gates. |
| [`gpu-weather-residency.md`](gpu-weather-residency.md) | `HISTORICAL` | PR #50: the bounded worker-private decoded-band residency change, now merged to `dev` as `b9e2471` via PR #61 `1a90515`. |
| [`rendering-baseline-2026-10-15.md`](rendering-baseline-2026-10-15.md) | `HISTORICAL` | The synthetic scaling baseline. The startup baseline, not this one, is the optimization priority for the installed app. |
| [`rendering-performance.md`](rendering-performance.md) | `HISTORICAL` | The historical benchmark protocol and its gate values. The canonical roadmap is `rendering-performance-plan.md`. |
| [`routing-profile-2026-10-12.md`](routing-profile-2026-10-12.md) | `HISTORICAL` | The dated routing profile and the first optimization targets it identified. |

## Rendering features and shipped slices

| Document | Status | What it is |
| --- | --- | --- |
| [`gpu-embedded-rendering.md`](gpu-embedded-rendering.md) | `REFERENCE` | The embedded GPU presenter: host choice, resource ledger and the matched CPU/GPU baseline. |
| [`gpu-weather-interpolation.md`](gpu-weather-interpolation.md) | `REFERENCE` | The opt-in numerically interpolated wind-barb path and its explicit limits. |
| [`rendering-startup-baseline-2026-10-15.md`](rendering-startup-baseline-2026-10-15.md) | `REFERENCE` | The normal start-up rendering baseline — the reference workload for comparisons. |
| [`rendering-base-chart-worker.md`](rendering-base-chart-worker.md) | `REFERENCE` | R2 slice: background preparation of the base chart layers, and what stays synchronous. |
| [`rendering-weather-job-reuse.md`](rendering-weather-job-reuse.md) | `REFERENCE` | R3 slice: reuse of forecast handles and indexes within one display job. |
| [`rendering-weather-crossview.md`](rendering-weather-crossview.md) | `REFERENCE` | R3 slice: bounded cross-view retention of selected forecast handles. |
| [`rendering-weather-working-set.md`](rendering-weather-working-set.md) | `REFERENCE` | R3 slice: the view-independent field working set, and the rejected superset trial. |
| [`rendering-weather-prefetch.md`](rendering-weather-prefetch.md) | `REFERENCE` | R4 slice: bounded directional prefetch of one speculative frame ahead. |
| [`rendering-weather-scheduling.md`](rendering-weather-scheduling.md) | `REFERENCE` | Weather scheduling: active-request coalescing, lifecycle telemetry, and the rejected wider-margin policy. |
| [`rendering-pan-prediction.md`](rendering-pan-prediction.md) | `REFERENCE` | Continuous-pan weather preparation and the latency-aware lookahead trial. |
| [`rendering-zoom-target.md`](rendering-zoom-target.md) | `REFERENCE` | Preparing weather for the zoom target during animation, with debounce and reversal cancellation. |
| [`rendering-frame-spikes.md`](rendering-frame-spikes.md) | `REFERENCE` | UI-thread frame-spike attribution, and the rejected seamark/hidden-panel trials. |
| [`rendering-streamlines.md`](rendering-streamlines.md) | `REFERENCE` | The static wind-streamline style, its presets and its density semantics. |
| [`rendering-wind-shoreline-mask.md`](rendering-wind-shoreline-mask.md) | `REFERENCE` | The windbarb land-visibility mask, and the rule that it must not change selection. |

## Routing

| Document | Status | What it is |
| --- | --- | --- |
| [`current-routing.md`](current-routing.md) | `CANONICAL` | Current-aware routing: the Currents tab, coastline checks, source selection, and how currents enter the calculation. |
| [`routing-safety.md`](routing-safety.md) | `REFERENCE` | The optional routing limits: maximum true wind, motoring fallback and wind-based speed reduction. |

## Weather data and sampling

| Document | Status | What it is |
| --- | --- | --- |
| [`weather-sampling.md`](weather-sampling.md) | `REFERENCE` | Record selection, bilinear interpolation and time blending in `core/gribsampling.py`, and what shares them. |

## Charts and base data

| Document | Status | What it is |
| --- | --- | --- |
| [`chart-layers.md`](chart-layers.md) | `REFERENCE` | Where the chart controls live in the Data Library, and what the old Chart Layers dock became. |
| [`chart-quilting-plan.md`](chart-quilting-plan.md) | `REFERENCE` | The the reference application-style chart quilting design plan. Not implemented; the `chartQuilting` setting restores today's stacking. |

## Tides, currents and marine data

| Document | Status | What it is |
| --- | --- | --- |
| [`tides-currents-plan.md`](tides-currents-plan.md) | `REFERENCE` | The tides and currents implementation plan and its milestone status. |
| [`tides-currents-manager.md`](tides-currents-manager.md) | `REFERENCE` | The Tides & currents palette: its tabs, docking behaviour and controls. |
| [`marine-data-validation.md`](marine-data-validation.md) | `REFERENCE` | Marine data availability, the research probe, and the questions still open. Not validated navigation predictions. |
| [`wcofs-currents.md`](wcofs-currents.md) | `REFERENCE` | NOAA WCOFS currents: subset downloads, the netCDF4 reader dependency and its platform gap. |
| [`chs-current-directions.md`](chs-current-directions.md) | `REFERENCE` | Seymour Narrows current directions checked against the published CHS tables. |

## Tool panels, docking and frontends

| Document | Status | What it is |
| --- | --- | --- |
| [`tool-panels.md`](tool-panels.md) | `REFERENCE` | The GTK `ToolDock` extension point for registering new tool panels. |
| [`panel-docking.md`](panel-docking.md) | `REFERENCE` | How panel docking, tab reordering, detaching and floating work. |
| [`qt-first-pass.md`](qt-first-pass.md) | `REFERENCE` | The Qt frontend migration guide and how the two frontends relate. |
| [`qt-parity.md`](qt-parity.md) | `REFERENCE` | The GTK→Qt interface parity checklist and its current status. |
| [`qt-default-validation.md`](qt-default-validation.md) | `REFERENCE` | Validation of the default Qt launcher, and the explicit GTK entry points. |

## Machine-readable evidence and research code

| Path | Status | What it is |
| --- | --- | --- |
| [`profiles/rendering/`](profiles/rendering/) | `REFERENCE` | Recorded machine-readable experiment results. These are the reference baselines — do not re-measure or edit them. |
| [`profiles/2026-10-12/`](profiles/2026-10-12/) | `REFERENCE` | The recorded routing/profile run for 2026-10-12. |
| [`../research/gpu-weather-texture/`](../research/gpu-weather-texture/) | `EXPERIMENT` | Archived experiment: pre-computing each forecast hour's U/V bands as GPU textures so a time change becomes a texture lookup. Never merged, never imported by the application, and unmeasured. It is prior art for ADR-001 D4 (resident time windows) — see the report for the recommendation. |
| [`source/`](source/) | `REFERENCE` | The Sphinx source for the published user documentation, separate from this index. |

## Superseded documents

If you land on one of these, it is evidence, not a plan. The current thinking is
in [ADR-001](adr-001-grib-rendering-architecture.md) and the issue program
(#62–#70, indexed in [`milestones.md`](milestones.md)).

| Superseded document | Superseded by |
| --- | --- |
| [`rendering-architecture-evaluation.md`](rendering-architecture-evaluation.md) | ADR-001 and the A0 decision log in [`rendering-performance-plan.md`](rendering-performance-plan.md) |
| [`rendering-a0g-progress.md`](rendering-a0g-progress.md) … [`rendering-a0k-shader-safety.md`](rendering-a0k-shader-safety.md) | ADR-001; the A0 program is closed as an experiment |
| [`rendering-m0-native-probe.md`](rendering-m0-native-probe.md), [`rendering-m0-float-textures.md`](rendering-m0-float-textures.md), [`rendering-m0-residency.md`](rendering-m0-residency.md), [`rendering-m0-retained-field.md`](rendering-m0-retained-field.md), [`rendering-m0-persistent-field.md`](rendering-m0-persistent-field.md), [`rendering-m0-app-persistent-field.md`](rendering-m0-app-persistent-field.md) | ADR-001 D1–D4 and issues #64–#67; the reports remain the result evidence |
| [`gpu-rendering-experiment.md`](gpu-rendering-experiment.md), [`gpu-texture-parity-r0.1.md`](gpu-texture-parity-r0.1.md) | [`rendering-implementation-plan.md`](rendering-implementation-plan.md) R0 |
| [`gpu-weather-residency.md`](gpu-weather-residency.md) | ADR-001 D1 and issue #64; PR #50 is merged to `dev` as `b9e2471` |
| [`early-weather-startup-integration.md`](early-weather-startup-integration.md) | ADR-001 and issues #62–#70 |
| [`rendering-performance.md`](rendering-performance.md) | [`rendering-performance-plan.md`](rendering-performance-plan.md) |
| [`rendering-baseline-2026-10-15.md`](rendering-baseline-2026-10-15.md) | [`rendering-startup-baseline-2026-10-15.md`](rendering-startup-baseline-2026-10-15.md) for the reference workload |
| [`routing-profile-2026-10-12.md`](routing-profile-2026-10-12.md) | [`current-routing.md`](current-routing.md) and [`profiling-priorities.md`](profiling-priorities.md) |

Nothing in this list has been deleted. They are the evidence base the
repository's rules depend on: rejected approaches and their reasons are
preserved, and historical reports are never overwritten with candidate results.
