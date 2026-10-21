# Documentation index

**The architecture decision is [ADR-001](adr-001-grib-rendering-architecture.md).
The work program that implements it is epic #51, issues
[#62–#70](milestones.md).** Read ADR-001 first, then work one issue at a time.

This directory has three parts, and each part answers a different question:

| Part | Contents | Use it when |
| --- | --- | --- |
| This file and the five documents below it | the current decision, roadmap, spec and issue index | **You are starting or doing work.** |
| The other `docs/*.md` files | reference for how the application behaves today | You need the current behaviour of a specific area |
| [`history/`](history/) | completed-work records and experiment evidence, including every rejected result | You need to know *why* a decision was made, or what was already tried |
| [`profiles/`](profiles/) | machine-readable recorded results | You need a number; do not re-measure it |
## Start here

Read these five, in this order, and nothing else until you need more:

1. **[ADR-001](adr-001-grib-rendering-architecture.md)** — the accepted architecture
   decision (D1–D5), the disposition of the seven structural gaps, and the
   mapping onto issues #62–#70.
2. **[`rendering-performance-plan.md`](rendering-performance-plan.md)** — the
   canonical roadmap: current checkpoint, hypothesis ledger, milestone status and
   the A0 decision log.
3. **[`rendering-implementation-plan.md`](rendering-implementation-plan.md)** — the
   R0–R5 sequence, the layer/ownership model, the non-negotiable invariants and
   the measurement contract.
4. **[`milestones.md`](milestones.md)** — the issue index for #62–#70.
5. Your issue's own body, which states its gate, evidence and validation command.

Everything else in this directory is either background you may not need, or
evidence for a decision that has already been made.

## Which document do I read for X?

| If you want to know… | Read |
| --- | --- |
| What the app does at start-up, and the opt-in early-weather path | [`early-weather-startup.md`](early-weather-startup.md) |
| The target rendering architecture, and what was decided (D1–D5) | [`adr-001-grib-rendering-architecture.md`](adr-001-grib-rendering-architecture.md) |
| The layer/ownership model, the R0–R5 sequence, the invariants and the measurement contract | [`rendering-implementation-plan.md`](rendering-implementation-plan.md) |
| The current performance roadmap, milestone status and the A0 decision log | [`rendering-performance-plan.md`](rendering-performance-plan.md) |
| Which issue to work on next, and in what order | [`milestones.md`](milestones.md) — GitHub is the tracker, this is the local index |
| The current GRIB loading architecture, and its seven structural gaps | [`grib-architecture-analysis.md`](grib-architecture-analysis.md) |
| The reference techniques (T1–T8) a decision may draw on | [`reference-architecture-analysis.md`](reference-architecture-analysis.md) |
| What has already been measured, so you do not measure it again | [`profiling-priorities.md`](profiling-priorities.md), [`rendering-startup-baseline-2026-10-15.md`](rendering-startup-baseline-2026-10-15.md) |
| What the instrumentation can and cannot prove | [`rendering-m0-measurement-audit.md`](rendering-m0-measurement-audit.md) |
| How routing works, including current-assisted routing and coastline checks | [`current-routing.md`](current-routing.md) |
| The routing safety limits (wind cap, motoring fallback, speed reduction) | [`routing-safety.md`](routing-safety.md) |
| How weather is selected, decoded, interpolated and time-blended | [`weather-sampling.md`](weather-sampling.md) |
| Where the chart controls live in the UI | [`chart-layers.md`](chart-layers.md) |
| Experimental complete ENC integration into the independent viewport (#78) | [`enc-viewport-integration.md`](enc-viewport-integration.md) — contract and evidence; not a default rollout |
| Default application ENC viewport rollout (#70 slice) | [`enc-default-rollout.md`](enc-default-rollout.md) — bounded rollout contract and fallback |
| All forecast points wind support (#79) | [`wind-all-points.md`](wind-all-points.md) — explicit unthinned legacy presentation and validation |
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
| `HISTORICAL` | Experiment record or completed work. Read it for evidence; do not follow it as a plan. |
| `EXPERIMENT` | Research code, not shipped. Not imported by the application. |

## The five canonical documents

| Document | Status | What it is |
| --- | --- | --- |
| [`adr-001-grib-rendering-architecture.md`](adr-001-grib-rendering-architecture.md) | `CANONICAL` | The accepted architecture decision: D1–D5, the seven structural gaps, the amendments to the R0–R5 spec, and the mapping to issues #62–#70. Read this first. |
| [`rendering-performance-plan.md`](rendering-performance-plan.md) | `CANONICAL` | The canonical performance roadmap: current checkpoint, reference workload, milestones, hypothesis ledger, experiment-contract index and the A0 decision log. |
| [`rendering-implementation-plan.md`](rendering-implementation-plan.md) | `CANONICAL` | The detailed R0–R5 sequence, layer/ownership model, non-negotiable invariants and measurement contract, as amended by ADR-001 §6. |
| [`milestones.md`](milestones.md) | `CANONICAL` | Local index of epic #51 and issues #62–#70. GitHub remains the authoritative tracker. |
| [`current-routing.md`](current-routing.md) | `CANONICAL` | Current-aware routing: the Currents tab, coastline checks, source selection, and how currents enter the calculation. |

## The reference documents

These describe how the application behaves today. Read the one that covers the
area you are changing; skip the rest.

| Document | Status | What it is |
| --- | --- | --- |
| [`grib-architecture-analysis.md`](grib-architecture-analysis.md) | `REFERENCE` | Current-state GRIB loading and visualization architecture with file/line citations, and the seven structural gaps ADR-001 disposes of. |
| [`reference-architecture-analysis.md`](reference-architecture-analysis.md) | `REFERENCE` | Reference techniques T1–T8 from a read-only analysis of a locally installed the reference application build. A hypothesis source, not a benchmark. |
| [`profiling-priorities.md`](profiling-priorities.md) | `REFERENCE` | Already-measured cost slices and their limits. Do not re-measure what is recorded here. |
| [`rendering-m0-measurement-audit.md`](rendering-m0-measurement-audit.md) | `REFERENCE` | Source audit of what the instrumentation can and cannot prove, and why readback is not presentation. |
| [`rendering-startup-baseline-2026-10-15.md`](rendering-startup-baseline-2026-10-15.md) | `REFERENCE` | The normal start-up rendering baseline — the reference workload for comparisons. Do not recapture it. |
| [`early-weather-startup.md`](early-weather-startup.md) | `REFERENCE` | How to run the opt-in `--early-weather` start-up, and what it does and does not change. |
| [`gpu-embedded-rendering.md`](gpu-embedded-rendering.md) | `REFERENCE` | The embedded GPU presenter: host choice, resource ledger and the matched CPU/GPU baseline. |
| [`gpu-weather-interpolation.md`](gpu-weather-interpolation.md) | `REFERENCE` | The opt-in numerically interpolated wind-barb path and its explicit limits. |
| [`rendering-streamlines.md`](rendering-streamlines.md) | `REFERENCE` | The static wind-streamline style, its presets and its density semantics. |
| [`rendering-wind-shoreline-mask.md`](rendering-wind-shoreline-mask.md) | `REFERENCE` | The windbarb land-visibility mask, and the rule that it must not change selection. |
| [`chart-layers.md`](chart-layers.md) | `REFERENCE` | Where the chart controls live in the Data Library, and what the old Chart Layers dock became. |
| [`chart-quilting-plan.md`](chart-quilting-plan.md) | `REFERENCE` | The the reference application-style chart quilting design plan. Not implemented; the `chartQuilting` setting restores today's stacking. |
| [`tides-currents-plan.md`](tides-currents-plan.md) | `REFERENCE` | The tides and currents implementation plan and its milestone status. |
| [`tides-currents-manager.md`](tides-currents-manager.md) | `REFERENCE` | The Tides & currents palette: its tabs, docking behaviour and controls. |
| [`marine-data-validation.md`](marine-data-validation.md) | `REFERENCE` | Marine data availability, the research probe, and the questions still open. Not validated navigation predictions. |
| [`wcofs-currents.md`](wcofs-currents.md) | `REFERENCE` | NOAA WCOFS currents: subset downloads, the netCDF4 reader dependency and its platform gap. |
| [`chs-current-directions.md`](chs-current-directions.md) | `REFERENCE` | Seymour Narrows current directions checked against the published CHS tables. |
| [`qt-first-pass.md`](qt-first-pass.md) | `REFERENCE` | The Qt frontend migration guide and how the two frontends relate. |
| [`qt-parity.md`](qt-parity.md) | `REFERENCE` | The GTK→Qt interface parity checklist and its current status. |
| [`qt-default-validation.md`](qt-default-validation.md) | `REFERENCE` | Validation of the default Qt launcher, and the explicit GTK entry points. |
| [`tool-panels.md`](tool-panels.md) | `REFERENCE` | The GTK `ToolDock` extension point for registering new tool panels. |
| [`panel-docking.md`](panel-docking.md) | `REFERENCE` | How panel docking, tab reordering, detaching and floating work. |
| [`weather-sampling.md`](weather-sampling.md) | `REFERENCE` | Record selection, bilinear interpolation and time blending in `core/gribsampling.py`, and what shares them. |
| [`routing-safety.md`](routing-safety.md) | `REFERENCE` | The optional routing limits: maximum true wind, motoring fallback and wind-based speed reduction. |

## History

[`history/`](history/) holds 28 records of work that is finished and decisions
already made. They are preserved because the repository's rules require it:
rejected approaches and their reasons are evidence, and historical reports are
never overwritten with candidate results.

**Read them only to find out why a decision was made or what was already
tried.** Every file there opens with a status header saying what it is, when it
was written and what supersedes it. None of them is a plan, and no A0 slice may
be started from one.

| Group | Files |
| --- | --- |
| A0 experiment record | [`rendering-architecture-evaluation.md`](history/rendering-architecture-evaluation.md), [`rendering-a0g-progress.md`](history/rendering-a0g-progress.md), [`rendering-a0h-resident-worker.md`](history/rendering-a0h-resident-worker.md), [`rendering-a0i-independent-numerical.md`](history/rendering-a0i-independent-numerical.md), [`rendering-a0j-early-display.md`](history/rendering-a0j-early-display.md), [`rendering-a0k-shader-safety.md`](history/rendering-a0k-shader-safety.md), [`early-weather-startup-integration.md`](history/early-weather-startup-integration.md) |
| M0 mechanism probes | [`rendering-m0-native-probe.md`](history/rendering-m0-native-probe.md), [`rendering-m0-float-textures.md`](history/rendering-m0-float-textures.md), [`rendering-m0-residency.md`](history/rendering-m0-residency.md), [`rendering-m0-retained-field.md`](history/rendering-m0-retained-field.md), [`rendering-m0-persistent-field.md`](history/rendering-m0-persistent-field.md), [`rendering-m0-app-persistent-field.md`](history/rendering-m0-app-persistent-field.md) |
| GPU experiments and PR #50 | [`gpu-rendering-experiment.md`](history/gpu-rendering-experiment.md), [`gpu-texture-parity-r0.1.md`](history/gpu-texture-parity-r0.1.md), [`gpu-weather-residency.md`](history/gpu-weather-residency.md) |
| Baselines and dated profiles | [`rendering-baseline-2026-10-15.md`](history/rendering-baseline-2026-10-15.md), [`rendering-performance.md`](history/rendering-performance.md), [`routing-profile-2026-10-12.md`](history/routing-profile-2026-10-12.md) |
| Completed implementation slices | [`rendering-base-chart-worker.md`](history/rendering-base-chart-worker.md), [`rendering-weather-job-reuse.md`](history/rendering-weather-job-reuse.md), [`rendering-weather-crossview.md`](history/rendering-weather-crossview.md), [`rendering-weather-working-set.md`](history/rendering-weather-working-set.md), [`rendering-weather-prefetch.md`](history/rendering-weather-prefetch.md), [`rendering-weather-scheduling.md`](history/rendering-weather-scheduling.md), [`rendering-pan-prediction.md`](history/rendering-pan-prediction.md), [`rendering-zoom-target.md`](history/rendering-zoom-target.md), [`rendering-frame-spikes.md`](history/rendering-frame-spikes.md) |

## Machine-readable evidence and research code

| Path | Status | What it is |
| --- | --- | --- |
| [`../tools/gate_runner.py`](../tools/gate_runner.py) and [`../tools/gates.py`](../tools/gates.py) | `CANONICAL` | The performance gate runner and its registry. One command gates a rendering change against the acceptance thresholds, with each threshold's recorded-evidence provenance kept in the registry. See the "Performance gate runner" section of the [root README](../README.md). |
| [`profiles/rendering/`](profiles/rendering/) | `REFERENCE` | Recorded machine-readable experiment results. These are the reference baselines — do not re-measure or edit them. |
| [`profiles/2026-10-12/`](profiles/2026-10-12/) | `REFERENCE` | The recorded routing/profile run for 2026-10-12. |
| [`../research/gpu-weather-texture/`](../research/gpu-weather-texture/) | `EXPERIMENT` | Archived experiment: pre-computing each forecast hour's U/V bands as GPU textures so a time change becomes a texture lookup. Never merged, never imported by the application, and unmeasured. It is prior art for ADR-001 D4 (resident time windows). |
| [`source/`](source/) | `REFERENCE` | The Sphinx source for the published user documentation, separate from this index. |
