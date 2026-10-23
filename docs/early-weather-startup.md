# Opt-in early-weather application startup

Launch from this branch with:

```sh
python -m gweatherrouting --early-weather
# Without instrument connections, use --log with a saved NMEA log:
python -m gweatherrouting --early-weather --log tools/samples/data_12_12_2021.nmea
```

This is integrated into the real `SessionController`/`PreviewWindow` and normal
application entry point, not a standalone viewport or splash. It remains opt-in;
default chart loading, renderer selection and plotting behavior are preserved.
No routing calculations or forecast-selection logic changed. PR #50 and the
retained numerical/GPU prototype remain separate and unmerged.

> **Status update (2026-10-19):** PR #50 was merged to `dev` via `b9e2471` / PR #61
> `1a90515`. The description above was recorded when it was still unmerged; the
> behaviour and measurements are unchanged.

## What is integrated

- The normal Charts workspace, controls, saved viewport and settings are restored.
- Unused Matplotlib log plots are deferred until Logs is visible; cached log data,
  series toggles and cursor position are replayed then. Polar curve preview also
  imports plotting code only when opened. This moves plot initialization cost;
  it does **not** make opening Logs/curve previews faster.
- Existing application weather preparation runs before saved charts attach. The
  current renderer (CPU or GPU host) and complete saved weather style are used;
  no experimental/native weather arrays or cached forecast images are substituted.
- Existing chart loaders run one per GUI event turn, using GUI-owned resources.
  The resulting list is staged and published atomically. Charts do not replace
  the map, time control or retained weather job. No GDAL handles cross threads.
- A status-bar message distinguishes weather preparation, pending/loading charts,
  and initialized charts/errors. "Prepared" describes current frame identity,
  not proof of forecast availability; existing coverage notices remain active.
- Existing map-data busy guards prevent routing/library operations against an
  incomplete chart/shoreline list. Shutdown stops pending chart callbacks. No
  forecasts, disabled weather or a hidden map bypass the initial weather wait;
  an unsuccessful weather preparation times out after five seconds so it cannot
  hide charts indefinitely.

## Why this differs from the prototype

Initialization attribution found most of the previous "chart host" delay in
Matplotlib/font imports for unused panels: 18.04 of 25.01 profiled seconds, versus
5.25 s controller and 0.83 s window construction. Chart constructors were under a
second. Those original measurements were attribution on the research branch,
**not a matched speedup baseline for this new branch**.

The first safe production slice therefore reuses the existing application weather
renderer and defers unnecessary work. It does **not** transplant experimental
worker scripts, reference bundles, PR #50 residency changes, runtime shader
compilation or the retained numerical/GPU backend into production. That backend's
integration, smoother broad interaction and startup below a few seconds remain
future work. The separate A0k shader fix is unnecessary for this branch's existing
CPU-geometry/image weather path and remains untouched.

## Validation on the actual entry point

Bounded native Cocoa runs call the real `main()` in isolated writable copies of
the same approved fixture:
`1ed1bca1281ad4096713ce20af9c1473e70bd4eb0766a54965849b7ace698a03`.
22 selected sources, frozen October 19 08:00, very-dense barbs/hide-over-land,
902×735 logical map viewport. Fixture copying/verification and instrumentation
imports precede the reported entry-point interval; this is not OS process-launch
latency. Each sandbox starts without a Matplotlib font cache.

The table below is the matched pair re-measured on the current `dev` base
(`default-1` and `app-1`, both exit 0). The other runs are `app-2` (opt-in
repeat), `close-1` (close while charts pending) and `logs-1` (opt-in, Logs
opened).

| Observation | Default (`default-1`) | Opt-in (`app-1`) |
| --- | ---: | ---: |
| First window/event-loop observation | 21.08 s | 3.72 s |
| First current weather frame | 24.50 s | 6.76 s |
| Attached charts at first weather | 39 | 0 |
| Matplotlib imported at first window | yes | no |

The opt-in path initializes/attaches all 39 charts by 9.08 s, with no chart errors.
This prioritizes early visible weather; it does not mean chart construction itself
got faster. Default constructors run before its first window; its harness's later
"charts initialized" observation is not a measured constructor-completion time.

Matched numerical flat-array hashes and rendered weather-surface hashes are
identical across modes. Source order, viewport, clock and 52,322 prepared samples
match. In the opt-in run chart publication also retains the same weather-job
object; no camera/clock/weather reset is accepted. These samples precede display
thinning, so the count is not a visible-symbol count. The prepared sample count
and flat-array hash differ from the values measured before this branch was
rebased onto the current `dev`, because the vectorized GRIB sampling pipeline
retained more samples; the rendered weather surface is unchanged.

The `close-1` run closes while charts are still pending: the real entry point exits
0, the controller closes and a deliberately late callback is rejected with no
charts attached. All five native runs exit 0. Unit coverage includes empty inputs,
weather timeout/disabled/hidden states, stale-current readiness, error handling,
atomic ordering/enabled flags, cancellation and lazy plot state replay.

The `logs-1` run opened the Logs workspace after charts were ready: the deferred
plot canvas materializes with three axes and imports Matplotlib at that moment,
not at startup. It took 28.47 s from entry in that cold sandbox because the font
cache had to be built on first use. The deferral therefore moves the plotting
cost to the moment Logs is opened; it does not remove it, and a warm cache is
much cheaper.

The `app-2` run reproduced `app-1` exactly on the earlier base: first window
3.87 s, first weather 6.62 s, charts 8.89 s, with identical weather
flat-array/surface hashes.

**Limits:** isolated cold-font-cache workloads, no statistical acceptance and no
presentation/FPS claim. Initial controller construction still precedes the window.
Individual chart loaders and later chart portrayal can still block the GUI;
staging them is not a fully asynchronous chart service. First weather is slower
here than the separate numerical prototype, and those different
architectures/timing protocols must not be compared as identical workloads.
Opening Logs still pays the plotting cost, including a cold font-cache build.

Private raw evidence is in `rendering-a0l-{app-1,app-2,default-1,close-1,logs-1}`
and `rendering-a0m-{default-1,app-1}` in the approved OpenCode temporary
directory. The validator is `tools/check_early_weather_startup.py`; its
fixture-tool argument points explicitly to the earlier capture utility, which
the application never imports. No private forecasts/charts/PNGs are versioned.

Targeted validation on this branch, run after the final code change: 15 tests
passed, 1 skipped, 2 subtests passed (early startup, base charts, workspace
state, GPU map contracts). Black on 43 checked Python files and `git diff
--check` (including new files) pass.

The isolated Qt preview suite is not counted here: `test_qt_preview.py` fails
on the base branch `dev` at 1f14649 because its isolated subprocess exceeds its
fixed 120 s timeout, and the same timeout is reached with and without this
branch. The suite exercises the default, non-opt-in path, which this branch
does not change.

## Gate decision

Adopt this **opt-in startup/lifecycle slice** locally. Stop at the completed gate;
do not silently start later GPU integration or a chart-service rewrite. Next
bottlenecks are initial controller work, deferred plotting cost and synchronous
chart/portrayal work. Routing, calculation fidelity, broad pan/scrub/zoom/layer
acceptance, memory plateau and presentation goals remain unchanged/open.

This work starts from `dev` (1f14649) on `feature/early-weather-startup`, preserving
the dirty research worktree. See the
[integration checkpoint](history/early-weather-startup-integration.md) for the measured
observations and the
[canonical performance plan](rendering-performance-plan.md) for the wider
rendering effort.
