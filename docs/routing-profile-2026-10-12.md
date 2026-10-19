# Optimized routing profile — 2026-10-12

> **Status: HISTORICAL.** The 2026-10-12 routing profile and the optimization
> targets it identified. Current routing behaviour is
> [current-routing.md](current-routing.md); current priorities are
> [profiling-priorities.md](profiling-priorities.md). Evidence, not a plan.

The saved three-waypoint route completed in **64.010 and 63.571 seconds** without
instrumentation (median 63.791 seconds). Coastline checks and current sampling
account for approximately **75%** of the sampling profile. These are the first
targets for further optimization or a focused Rust/C++ implementation.

No production engine changes were made for this profiling exercise.

## Workload and measurement

- Apple M1 Pro, macOS 26.6.2, Python 3.11.17, NumPy 2.4.6, pyproj 3.7.2.
- Current array engine, saved `track`, Bavaria 38 polar, departure
  `2026-10-12T04:55:00+00:00`.
- Three waypoints: `(49.3315101522, -124.0169074487)`,
  `(49.1043397609, -123.4062347753)`, `(49.3719942234, -123.8032735030)`.
- Saved `gfs_20261010_12z_72h.grib2`, all 88 selected current fields, and high
  resolution GSHHS coastline with exact point/segment predicates.
- The completed route used 17 current sources and recorded 131 rejected current
  sample locations. All runs completed 94 steps.
- Includes current-model tidal components as supplied by the models. Station
  tide-height clearance and waves are not implemented routing inputs yet.
- Timed `Routing.step()` calls, excluding initial loading, downloads, GUI drawing
  and route persistence. This measures the engine, not end-to-end GUI latency.
- Two unprofiled runs, one Pyinstrument 5.1.3 sampling run at 1 ms, and one
  cProfile run, executed sequentially. Sampling took 81.766 seconds; cProfile took
  87.352 seconds. Instrumentation adds overhead, so those are not baseline timings.
- Exact path positions, timestamps and wind/current source sets matched between
  both baselines and cProfile. The sampling run completed the same number of
  steps; its path was not retained in the exported sampling file.

## Time distribution

Approximate exclusive groups from the sampling tree; coastline work called by
current fallback is assigned to coastline, so these rows do not double count.
The percentages describe this instrumented workload, rather than universal costs.

| Work | Sampling time | Share |
| --- | ---: | ---: |
| Coastline checks, including current-neighbor validation | 36.93 s | 45.2% |
| Current sampling and source selection, excluding coastline/geodesics | 24.82 s | 30.4% |
| Geodesic calculations | 9.12 s | 11.2% |
| Routing orchestration and other work | 7.03 s | 8.6% |
| Wind sampling | 1.98 s | 2.4% |
| Frontier points and provenance creation | 1.26 s | 1.5% |
| Batched polar interpolation | 0.63 s | 0.8% |

cProfile independently attributes 41.16 seconds to point/segment coastline checks
(47.1% of instrumented wall time), consistent with the sampling result.

## Concrete hotspots

cProfile recorded approximately 126 million calls. Counts below include rejected
candidates and coastal fallback, not just the final route.

| Operation | Calls | Implication |
| --- | ---: | --- |
| OGR geometry construction | 6,009,306 | Many temporary Python/SWIG geometry objects |
| Prepared geometry containment/intersection | 6,009,306 combined | Preserve exact predicates while reducing dispatch overhead |
| Coastline point/line validity entry points | 230,014 combined | Includes many single-neighbor requests |
| Missing-current resolution | 45,390 | Coastal fallback remains largely scalar |
| Per-field nearest-current attempts | 892,363 | Repeated source scanning and cell/corner setup |
| Current manifest construction | 126,831 | Repeated metadata decoding just to read grid resolution |

Manifest construction consumed about 5.04 sampling seconds (6.2%); its JSON
decoding dominates that cost. This time is already inside current sampling above.
Caching immutable resolution/metadata could remove redundant work without changing
source order, masks, samples or fidelity, but alone would only produce a modest gain.

## Ranked next work

1. Batch coastline and nearest-current validation. Keep the same exact containment
   and intersection operations and the same wet-corner acceptance rules, while
   reducing temporary objects and individual Python/native calls. A native loop
   that handles an entire batch is a useful Rust/C++ prototype target.
2. Index current fields by spatial/time coverage, retaining original selection
   precedence. Batch coastal fallback and reuse immutable field metadata and grid
   geometry. This avoids scanning irrelevant sources without disabling any input.
3. Evaluate a combined native sampling/propagation kernel after those changes.
   Rewriting wind lookup or provenance alone has little overall upside here.

The profile does not measure a Rust/C++ speedup. It also cannot give an exact
Python-versus-native split: NumPy ufuncs, JSON decoding and pyproj may execute
compiled work inside a Python frame's self time. Treating all Python-frame time
as removable interpreter overhead would overstate the potential.

As an illustration, if the coastline/current portion (75.5%) became 5× faster
and everything else stayed constant, overall speedup would be approximately
`1 / (0.245 + 0.755 / 5) = 2.53×`, taking 63.8 seconds to roughly 25 seconds.
At 10× within those hotspots, overall improvement would be about 3.12×.
These are sensitivity calculations, not measured or promised gains. A further
10× overall would require substantial improvements beyond those two portions.

## Artifacts

- [Interactive sampling call tree](profiles/2026-10-12/sampling.html)
- [Sampling text](profiles/2026-10-12/sampling.txt)
- [cProfile cumulative and self times](profiles/2026-10-12/cprofile.txt)
- [Raw cProfile data](profiles/2026-10-12/routing.prof)
- [Baseline/result summaries](profiles/2026-10-12/summary.json)
- [Sampling breakdown](profiles/2026-10-12/breakdown.json)
- [Profiling harness](profiles/2026-10-12/run.py)

The harness uses the local saved inputs described above, writes only to
`/private/tmp/gwr-routing-profile`, and neither downloads data nor saves app routes.
For reproduction, install Pyinstrument into an isolated temporary directory and
run from the repository:

```sh
.venv/bin/python -m pip install --target /private/tmp/gwr-profile-deps pyinstrument==5.1.3
PYTHONPATH=.:/private/tmp/gwr-profile-deps .venv/bin/python docs/profiles/2026-10-12/run.py
```

Future runs use the then-selected offline datasets, so compare input selections
as well as timings when the local cache changes.

## Implemented coastal follow-up

The coastal batch implementation completed the same 94-step route in **41.328
and 41.445 seconds** (median 41.387 seconds), versus the original 63.791-second
median: **1.54× throughput, 35.1% less time**. No inputs, search resolution,
integration steps, coastline predicates, or sampling policies were removed.
Both new baselines and the follow-up cProfile run matched the original path
positions, timestamps and wind/current source sets exactly.
An additional comparison against saved pre-change field and coastline code matched
15 complete frontiers, including 33,231 marine sample records, parent links,
source selections and rejected current locations. The full suite passed 608 tests.

Changes: compiled Shapely/GEOS geometry batches over cached WKB conversions of the
same OGR polygons; batched wet-corner fallback and shoreline validation; cached
immutable grid resolution metadata. Shapely 2.2.0 / GEOS 3.14.1 was used locally;
the OGR reference used GEOS 3.15.0. OGR remains the fallback when Shapely is absent.

| Instrumented operation | Before | After |
| --- | ---: | ---: |
| Current missing-data resolution/fallback | 32.37 s | 3.89 s |
| Coastline point and segment checks, including fallback | 41.16 s | 9.60 s |
| Individual point/line validity calls | 230,014 | 20,858 |
| Total profiled function calls | 126,313,271 | 24,408,877 |

These cumulative categories overlap; do not add the rows. Profiling overhead
also changes as the number of calls falls, so use the unprofiled measurements
for the overall speedup. The remaining main costs include current-field
interpolation/selection, geodesics, and propagation bookkeeping.

Artifacts: [first run](profiles/2026-10-12/coastal-after/after-1-run.json),
[second run](profiles/2026-10-12/coastal-after/after-2-run.json),
[follow-up call profile](profiles/2026-10-12/coastal-after/cprofile.txt), and
[raw profile](profiles/2026-10-12/coastal-after/routing.prof).
