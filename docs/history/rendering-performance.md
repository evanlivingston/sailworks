# Rendering performance: baseline protocol and improvement plan

> **Status: HISTORICAL.** The historical benchmark protocol and its gate values.
> The canonical roadmap and decision gates are in
> [rendering-performance-plan.md](../rendering-performance-plan.md). Evidence, not a
> plan.

## Scope and decision gates

The [canonical performance plan](../rendering-performance-plan.md), tracking epic
[#51](https://github.com/evanlivingston/gweatherrouting/issues/51), defines current
milestones and experiment decisions. This document describes the historical
benchmark protocol; the [implementation specification](../rendering-implementation-plan.md)
retains its invariants and earlier architecture context. Neither supersedes the
canonical plan's evidence gates.

First establish repeatable measurements; do not optimize by increasing padding
or reducing source data ad hoc. The current implementation adds a **benchmark
tool only**. It does not change application rendering, sampling, source selection,
or caching behavior.

**Primary reference:** [normal saved startup state](../rendering-startup-baseline-2026-10-15.md).
The synthetic fixture remains a scaling diagnostic; it does not determine the
optimization priority for the installed app.

The [GPU feasibility experiment](gpu-rendering-experiment.md) compares retained
Metal geometry/composition with the existing painter and a batched CPU control.
It records a significant presentation benefit and an unresolved transformed-
texture parity failure in the historical report. The [R0.1 implementation](gpu-texture-parity-r0.1.md)
closes the reproduced 2x Metal failure without weakening its gates; no production
migration is enabled.

1. **Baseline (this change):** isolated, versioned camera replays; raw latency,
   expensive-stage, rebuild, and memory measurements; compatible-report comparisons.
2. **Representative coverage:** agree on real chart/forecast datasets and target
   hardware. Add native input/presentation measurements and quality assertions.
3. **Bound interaction work:** independently evaluate background navigation
   rendering, spatial wind selection, cached world geometry, and layer-specific
   invalidation. Measure each change against the same baseline.
4. **Prefetch and retention:** introduce independently scheduled visible,
   predicted, surrounding, and adjacent-zoom coverage in that priority order.
   Use measured latency and velocity to size lookahead, and byte budgets to
   constrain it. Separate local preparation from authorized network downloads.
5. **Acceptance:** compare latency tails, coverage readiness, retained memory,
   and rendering correctness before integrating improvements.

Provisional reference-machine targets: warm replay p95 <=16.7 ms, p99 <=33.3 ms,
no steady-state stalls >50 ms; memory plateaus during prolonged/repeated travel;
no unintended numerical, source-selection, or rendering-quality changes. These
are **not** universal CI timing assertions. Final native-display targets require
an agreed workload, reference machine, refresh rate, and memory budget.

## Running the baseline

### Normal startup state (recommended)

The user does not need to assemble a workload. Capture installed data once, then
reuse the snapshot for both baseline and candidate:

```sh
.venv/bin/python tools/benchmark_startup_rendering.py \
  --capture-current /private/path/to/startup-snapshot
.venv/bin/python tools/benchmark_startup_rendering.py \
  --snapshot /private/path/to/startup-snapshot --output /path/to/startup-baseline.json
.venv/bin/python tools/benchmark_startup_rendering.py \
  --snapshot /private/path/to/startup-snapshot --output /path/to/startup-candidate.json
.venv/bin/python tools/benchmark_startup_rendering.py \
  --compare /path/to/startup-baseline.json /path/to/startup-candidate.json \
  --output /path/to/startup-comparison.json
```

Capture auto-detects application storage on macOS/Linux; `--data-dir` can point
at a test installation. Keep snapshots outside the repository: they contain
private routes, preferences, and marine data. Do not share them automatically.
Capture refuses an existing destination, sources that change during copying,
symlinks, and chart paths outside the supported managed data directories. External
chart locations need explicit fixture support rather than silent omission.

Snapshots include saved settings/workspace, navigation collections, forecasts,
base/imported charts, marine data, polars, and CHS disk cache. Connections,
recordings, temporary downloads, and credentials are excluded. Replays disable
the shared system credential loader too. Source files are copied, never linked;
APFS uses private copy-on-write clones with normal copies as a fallback. Every
snapshot file is checksummed and verified before replay. Parent/worker storage
is isolated; saved app preferences are not modified.

This mode launches the real `SessionController` and `PreviewWindow`, restores the
saved active map and dock layout, and leaves enabled layers/density intact. It
does not inject synthetic data. Forecast time is frozen at capture's local hour.
CHS rendering stays enabled if saved that way, but fetches use captured cached
tiles only; missing tiles are counted, never downloaded. No instrument connection
is started. Default pacing is 16.667 ms; default scenarios are stationary repaint,
pan, reverse-pan, and one-level zoom-out, with 180 steps and three repetitions.

Native Qt is the default here; `--offscreen` is for automated validation and may
clamp the saved layout to its virtual screen. Actual viewport, native screen,
DPR, layer counts, retained initial wind samples, warnings, and worker jobs are
recorded. `--scale 1` preserves the native display's DPR (2 on the measured Mac);
it does not force a native Retina screen to DPR 1. All earlier `grab()`/pacing
limitations still apply: no claim of compositor presentation FPS is made.

The startup harness reuses the v1 statistical/comparison protocol and adds raw
overlay job durations, cancellation/publication status, viewport, and phase.
`replay_stage_counts` partitions stage spans before post-replay settling. Spans
may include extra native repaints as well as explicit grabs. Worker-idle waits
require 200 ms quiet and fail on timeout or observed overlay errors; worker-idle
does **not** certify coverage. Reports identify the startup and statistical
harnesses together so incompatible measurement code cannot be compared.

Long runs checkpoint completed repetitions to `OUTPUT.partial.json`. A failed
worker records its exit code and writes `OUTPUT.failure.log`; do not treat partial
reports as a completed baseline. Snapshots/checkpoints are not live app state.
An exploratory pre-checkpoint batch terminated early and was discarded; only the
12 completed fresh-process runs are included in the saved startup baseline.

### Synthetic scaling scenes

The first captured results and raw reports are summarized in
[the 2026-10-15 baseline](rendering-baseline-2026-10-15.md).

From the repository root with the normal application dependencies installed:

```sh
PYTHONPATH=. .venv/bin/python tools/benchmark_rendering.py \
  --offscreen --output /path/to/baseline-1x.json
PYTHONPATH=. .venv/bin/python tools/benchmark_rendering.py \
  --offscreen --scale 2 --output /path/to/baseline-2x.json
```

Run these **serially**, with other heavy tasks stopped. Defaults: 1280 x 800
logical pixels, 180 measured frames, five stationary warmup grabs, three fresh
process repetitions per scenario. Each scenario/repetition gets a temporary
HOME and XDG directories; installed settings, forecasts, charts, and credentials
are not loaded. Python socket connections are blocked. No downloads are enabled.
The sandbox/RSS implementation currently supports macOS and Linux, not Windows.

Omit `--offscreen` to use the native Qt platform. This still measures synchronous
`QWidget.grab()` rendering, **not** compositor presentation, input-to-photon
latency, or actual display FPS. `--interval-ms 16.667` paces the replay while
servicing Qt; default zero runs at throughput speed. Pacing is not vsync. Record
the actual DPR from the output, which may also reflect native display scaling.

Smoke run:

```sh
PYTHONPATH=. .venv/bin/python tools/benchmark_rendering.py \
  --offscreen --frames 24 --repeats 1 --output /path/to/smoke.json
```

The tool records commit, dirty status, tracked diff hash, harness hash, dependency
versions, host/platform, actual DPR, scene, and replay parameters. A dirty-tree
report is exploratory; record a clean revision for release comparisons. Run
timeouts fail explicitly rather than treating incomplete loading as success.

### Fixed scenes and trajectories

| Scenario | Synthetic content | Replay |
| --- | --- | --- |
| `empty_pan` | No navigation/weather/log fixture | Pan 8 logical pixels per frame |
| `navigation_pan` | 10,000-point navigation track | Same pan, crossing cache margins |
| `reverse_pan` | Same navigation track | Reverse direction halfway through |
| `zoom_out` | Same navigation track | Zoom out one level over the run |
| `wind_pan` | Retained 316 x 316 wind grid | Pan with production live barb geometry |
| `log_pan` | 100,000-point log track | Pan with production log overlay |
| `combined_pan` | Navigation + retained wind + log | Same pan |

The camera is driven directly, not through emulated mouse/trackpad events. The
moving flag defers worker publication as in a gesture. Wind fixtures inject a
completed retained field and intentionally omit background weather sampling,
fill, and isobars; they isolate live geometry scaling. Dashboard, marine data,
CHS network tiles, and coverage notices are excluded. Production renderers are
otherwise used without modification. Instrumentation exists only in the worker.

The first grab measures application-cold caches (not cold filesystem/GDAL/OS
caches). Initial overlay work settles before stationary warmup. Warm replay then
crosses cache boundaries; it is not restricted to best-case cached frames.
After movement the tool renders again and waits for worker idle. That duration
is **not** proof of spatial coverage or correct forecast availability.

### Local data manifests

`--manifest scene.json` overrides the version-1 defaults. Paths are relative to
the manifest; each input must exist locally and match its SHA-256. For example:

```json
{
  "version": 1,
  "center": [48.4, -123.4],
  "zoom": 9,
  "time": "2026-10-15T12:00:00",
  "navigation_points": 10000,
  "log_points": 100000,
  "wind_side": 316,
  "datasets": [
    {"kind": "vector", "path": "charts/coast.geojson", "sha256": "<64 hex characters>"}
  ]
}
```

`tools/samples/rendering-scene.json` contains the complete default synthetic
scene and can be copied as a starting point.

Supported dataset kinds are `vector` (application-supported local vector chart),
`grib` (read-only forecast), and `auxiliary` (checksum-only dependencies such as
shapefile sidecars or ENC update files). List **all** sidecars/updates that affect
rendering. Use a time/viewport inside the forecast coverage. There is no automatic
discovery or copying into the user's data library. Do not point at mutable data.
Checksum verification is outside the timed worker. Scene time is naive with
worker timezone fixed to UTC.

For real GRIBs, choose scenarios without injected wind fields:

```sh
PYTHONPATH=. .venv/bin/python tools/benchmark_rendering.py \
  --offscreen --manifest scene.json \
  --scenarios empty_pan,navigation_pan,reverse_pan,zoom_out,log_pan \
  --output /path/to/real-data.json
```

The baseline does not certify these inputs cover the view. Real-fixture coverage
and image correctness must be reviewed separately before drawing conclusions.

## Reading and comparing results

Each report retains raw per-frame grab durations, queued-callback delay,
replay-start intervals, stage timings, rebuild counts, and known retained surface
bytes. Summaries include p50/p95/p99/max and counts over 16.7/33.3/50 ms.

* **Grab duration:** synchronous rendering and grab overhead, not presented frame
  duration. Compare like-for-like; do not convert it into claimed display FPS.
* **Queued-callback delay:** a zero-delay Qt callback queued before the grab;
  measures event-loop service delay including synchronous painting, not real
  input latency. Start intervals include pacing and harness bookkeeping.
* **Stages:** base/navigation image rebuilds and live wind/log paint calls,
  including any extra Qt repaints while replaying. These are inclusive spans;
  do not sum them with grab time as independent costs.
* **Memory:** process peak RSS includes setup, libraries, source data, and transient
  allocations. Known retained image bytes deduplicate Qt/Cairo surfaces, but
  exclude decoded arrays, land masks, worker-in-progress surfaces, and temporary
  copies. Neither is a complete cache budget or current-RSS time series.

```sh
.venv/bin/python tools/benchmark_rendering.py \
  --compare /path/to/baseline-1x.json /path/to/candidate-1x.json \
  --output /path/to/comparison.json
```

Comparison refuses different environments, manifests, viewports, pacing,
repetition counts, measurement harnesses, and replay configurations. It compares pooled raw grab and
callback samples, peak memory/surface bytes, rebuild totals, and worker-idle
latency. Negative p99 change means faster. Inspect each repetition and raw tails
as well: a small p99 difference on a noisy machine is not evidence of improvement.

## Measurement extensions before prefetch implementation

The v1 tool intentionally does **not** report invented prefetch hit rates or
coverage percentages. The future loading controller needs observable request
IDs, layer/source/time keys, requested coverage, priority, bytes, timestamps,
publication, cancellation, and eviction events. Then add offline recorded-source
replays with controlled delays and measure:

* visible misses, time to usable/full coverage, and uncovered area per layer;
* useful-prefetch hits versus bytes/work never used;
* visible-request queue delay and interference from speculative work;
* reverse-pan/adjacent-zoom reuse, eviction churn, current RSS, and memory plateau;
* stale-source/time publication, seams, attribution, masks, and symbol alignment.

Add long native-display recordings of real input and a presentation-aware probe
before claiming smooth 60/120 Hz rendering. Keep numerical sampling fidelity
tests separate from display LOD tests. Retain all source points; any eventual
display simplification must specify and validate a screen-space error bound.
