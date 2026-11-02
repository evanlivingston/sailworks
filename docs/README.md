# Documentation index

**The architecture decision is [ADR-001](adr-001-grib-rendering-architecture.md);
its work program is epic #51, issues [#62-#73](milestones.md).**

**The issue body is the spec for a task.** It states the gap, scope, non-goals,
acceptance gate and validation command. Read a document from this index only
when the issue body points at it.

| Part | Contents | Use it when |
| --- | --- | --- |
| This file | the router: one line per document | You need to locate a document |
| [`history/`](history/) | completed-work records and experiment evidence, including every rejected result | You need to know *why* a decision was made, or what was already tried |
| [`profiles/`](profiles/) | machine-readable recorded results | You need a number; do not re-measure it |

## Status labels

| Label | Meaning |
| --- | --- |
| `CANONICAL` | Authoritative. Read this one; other documents defer to it. |
| `REFERENCE` | Accurate background. Read it if it is relevant to your change. |
| `HISTORICAL` | Experiment record or completed work. Read it for evidence; do not follow it as a plan. |
| `EXPERIMENT` | Research code, not shipped. Not imported by the application. |

## Canonical documents

| Document | What it is |
| --- | --- |
| [adr-001-grib-rendering-architecture.md](adr-001-grib-rendering-architecture.md) | The accepted architecture decision (D1-D7), the disposition of the seven structural gaps, and the mapping onto issues #62-#70. |
| [rendering-performance-plan.md](rendering-performance-plan.md) | Current renderer state, the invariants, the acceptance gates and the one next decision. |
| [rendering-implementation-plan.md](rendering-implementation-plan.md) | The R0-R5 sequence, the layer/ownership model, the non-negotiable invariants and the measurement contract. |
| [milestones.md](milestones.md) | Local index of epic #51 and issues #62-#73. GitHub remains the authoritative tracker. |
| [current-routing.md](current-routing.md) | Current-aware routing: the Currents tab, coastline checks, source selection, and how currents enter the calculation. |

## Reference documents

Read the one that covers the area you are changing; skip the rest.

| Document | What it is |
| --- | --- |
| [grib-architecture-analysis.md](grib-architecture-analysis.md) | Current-state GRIB loading and visualization architecture with file/line citations, and the seven structural gaps ADR-001 disposes of. |
| [profiling-priorities.md](profiling-priorities.md) | Already-measured cost slices and their limits. Do not re-measure what is recorded here. |
| [rendering-m0-measurement-audit.md](rendering-m0-measurement-audit.md) | What the instrumentation can and cannot prove, and why readback is not presentation. |
| [rendering-startup-baseline-2026-10-15.md](rendering-startup-baseline-2026-10-15.md) | The October 15 start-up baseline, kept as historical evidence. The reference workload is the October 16 snapshot identified in the plan; do not recapture either. |
| [early-weather-startup.md](early-weather-startup.md) | The opt-in `--early-weather` start-up, and what it does and does not change. |
| [gpu-embedded-rendering.md](gpu-embedded-rendering.md) | The embedded GPU presenter: host choice, resource ledger and the matched CPU/GPU baseline. |
| [gpu-weather-interpolation.md](gpu-weather-interpolation.md) | The opt-in numerically interpolated wind-barb path and its explicit limits. |
| [rendering-streamlines.md](rendering-streamlines.md) | The static wind-streamline style, its presets and its density semantics. |
| [rendering-wind-shoreline-mask.md](rendering-wind-shoreline-mask.md) | The windbarb land-visibility mask, and the rule that it must not change selection. |
| [chart-layers.md](chart-layers.md) | Where the chart controls live in the Data Library, and what the old Chart Layers dock became. |
| [chart-quilting-plan.md](chart-quilting-plan.md) | The detail-chart chart quilting design plan. Not implemented. |
| [tides-currents-plan.md](tides-currents-plan.md) | The tides and currents implementation plan and its milestone status. |
| [tides-currents-manager.md](tides-currents-manager.md) | The Tides & currents palette: its tabs, docking behaviour and controls. |
| [marine-data-validation.md](marine-data-validation.md) | Marine data availability, the research probe, and the questions still open. |
| [wcofs-currents.md](wcofs-currents.md) | NOAA WCOFS currents: subset downloads, the netCDF4 reader dependency and its platform gap. |
| [chs-current-directions.md](chs-current-directions.md) | Seymour Narrows current directions checked against the published CHS tables. |
| [enc-viewport-integration.md](enc-viewport-integration.md) | Experimental complete ENC integration into the independent viewport (#78): contract and evidence; not a default rollout. |
| [enc-default-rollout.md](enc-default-rollout.md) | Default application ENC viewport rollout (#70 slice): bounded rollout contract and fallback. |
| [enc-app-latency.md](enc-app-latency.md) | Startup/pan/zoom chart delay reported during PR #81 review: bounded diagnostic contract. |
| [wind-all-points.md](wind-all-points.md) | All forecast points wind support (#79): explicit unthinned legacy presentation and validation. |
| [qt-first-pass.md](qt-first-pass.md) | The Qt frontend migration guide and how the two frontends relate. |
| [qt-parity.md](qt-parity.md) | The GTK to Qt interface parity checklist and its current status. |
| [qt-default-validation.md](qt-default-validation.md) | Validation of the default Qt launcher, and the explicit GTK entry points. |
| [tool-panels.md](tool-panels.md) | The GTK `ToolDock` extension point for registering new tool panels. |
| [panel-docking.md](panel-docking.md) | How panel docking, tab reordering, detaching and floating work. |
| [weather-sampling.md](weather-sampling.md) | Record selection, bilinear interpolation and time blending in `core/gribsampling.py`, and what shares them. |
| [routing-safety.md](routing-safety.md) | The optional routing limits: maximum true wind, motoring fallback and wind-based speed reduction. |

## History

[`history/`](history/) holds the completed-work records and experiment
evidence, including every rejected result. They are preserved because the
repository's rules require it: rejected approaches and their reasons are
evidence, and historical reports are never overwritten with candidate results.

**Read them only to find out why a decision was made or what was already
tried.** Every file there opens with a status header saying what it is, when it
was written and what supersedes it. None of them is a plan, and no slice may be
started from one.

The two consolidated records from the rendering program are
[`history/rendering-decision-log.md`](history/rendering-decision-log.md) (the
archived checkpoint notes, the consolidation notes, the #51 acceptance
checkpoint and the A0 decision log) and
[`history/rendering-experiment-records.md`](history/rendering-experiment-records.md)
(the runtime integration record and the M0 experiment contracts B1, E1-E3).
The rest are the per-slice reports.

## Machine-readable evidence and research code

| Path | Status | What it is |
| --- | --- | --- |
| [`../tools/gate_runner.py`](../tools/gate_runner.py) and [`../tools/gates.py`](../tools/gates.py) | `CANONICAL` | The performance gate runner and its registry. One command gates a rendering change against the acceptance thresholds, with each threshold's recorded-evidence provenance kept in the registry. See the "Performance gate runner" section of the [root README](../README.md). |
| [`../tools/check_doc_links.py`](../tools/check_doc_links.py) and [`../tools/check_doc_anchors.py`](../tools/check_doc_anchors.py) | `CANONICAL` | The documentation checkers. The first verifies that local link targets exist; the second verifies that `#anchor` fragments match a heading in the target file. Run both after any documentation change. |
| [`profiles/rendering/`](profiles/rendering/) | `REFERENCE` | Recorded machine-readable experiment results. These are the reference baselines - do not re-measure or edit them. |
| [`profiles/2026-10-12/`](profiles/2026-10-12/) | `REFERENCE` | The recorded routing/profile run for 2026-10-12. |
| [`../research/gpu-weather-texture/`](../research/gpu-weather-texture/) | `EXPERIMENT` | Archived experiment: pre-computing each forecast hour's U/V bands as GPU textures so a time change becomes a texture lookup. Never merged, never imported by the application, and unmeasured. It is prior art for ADR-001 D4 (resident time windows). |
| [`source/`](source/) | `REFERENCE` | The Sphinx source for the published user documentation, separate from this index. |
