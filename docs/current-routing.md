# Routing with currents

Current-aware routing is opt-in in the routing dialog's **Currents** tab. Enable
**Include currents**. **Download data automatically** is selected when that mode is enabled, and can be turned off.
The existing wind-only routing remains available. Source selection comes from
the Currents panel; Copernicus login comes from application Settings → Currents.
Enabling currents also enables coastline checks by default. Keep those checks
enabled to reject land-crossing search paths before sampling model currents.
The shoreline checker repairs invalid source polygons in memory, checks the
whole candidate segment, and caches local clips and prepared geometry without
simplifying the installed routing shoreline. Masked current cells can still
occur in water near shore; candidates that cannot sample them are rejected while
the search continues through usable coverage.

Before the current-aware search starts, an offline pre-check samples the track
and water up to five nautical miles either side, at departure and hourly through
the next 24 hours. It uses roughly 500 m sample spacing for local tracks, with a
bounded along-track sampling budget for long tracks. Known masked cells, absent
coverage, missing forecast times and disabled coastline checks trigger a warning
that distinguishes downloadable gaps from unusable areas. The user can cancel before searching or run anyway.
An unresolved masked departure, unavailable departure data with auto-download disabled, or a
land waypoint prevents starting. Automatic downloads can still recover absent
departure coverage when enabled.

This screen is not an arrival-time prediction or a guarantee: the search can
explore beyond the checked corridor, between samples, or beyond the checked
window. Moving track segments off land does not prevent candidate paths from
reaching coastal model masks. The screen makes no downloads, does not change the
run's current selection or provenance, and applies the same runtime sampling policy.

Current sampling tries the next enabled field when a preferred model has a masked
cell or a forecast time gap. It keeps the finest available valid sample, without
blending, extrapolating, or substituting zero. Each sample records its actual
source. Fallback can use a coarser grid and changes model fidelity at that point;
for example, CIOPS-West (2 km) can fill gaps in CIOPS-SalishSea (500 m). Disabled
fields are never used.

If interpolation fails in every selected field, routing can use the nearest valid
corner of the containing cell, within one nominal model grid spacing. Both current
components must be finite at the same corner at both bracketing forecast times;
time interpolation remains unchanged, with no extrapolation or bridging time gaps.
This fallback requires point and segment shoreline checks: the query and source
must be water and the connecting segment must not intersect land according to the
routing shoreline. If coastline checks are disabled, nearest fallback is disabled.
The saved samples mark `nearest_wet_cell` and record source coordinates and distance.
This is a bounded spatial approximation; it cannot recreate unresolved coastal
flow or correct shoreline inaccuracies. Ordinary valid interpolation in any
selected model takes precedence. If no acceptable corner exists, the gap remains.

Automatic coverage recovery downloads absent coverage, but stops on masks that
none of the selected models can fill instead of downloading the same forecast
again. A recovery must make at least one failed sample available to continue. Repeated retrieval of identical
forecast contents reuses the existing stored dataset and its provenance ID;
disabled datasets stay disabled. Existing saved datasets are preserved.

Regional forecast discovery selects a retained run covering the requested time
window, rather than a newer run starting after departure.

Regional source files use a 256 MiB disk cache shared across tile requests.
Each new download client revalidates cached files using HTTP ETag or Last-Modified
headers, so provider corrections are observed; without validators it downloads
fresh bytes. A client keeps its validated bytes stable for that download.
Cached bytes are checked against their recorded checksum before reuse.
Directory listings are always refreshed. Worldwide subsetting retains its
existing download behavior.

## Physical model and algorithm choices

Route search and vessel-motion integration are separate decisions. Isochrones
expand reachable positions at equal elapsed times. Time-dependent graph search
and dynamic programming are other established approaches, with different
discretization, pruning and performance tradeoffs. An algorithm menu is not an
accuracy guarantee. This increment supplies one current-aware isochrone search,
rather than presenting unvalidated alternatives as equivalent user choices.

For each candidate heading, the polar returns boat speed through water. We use:

```
wind_toward_water = wind_toward_ground - current_toward_ground
boat_toward_ground = boat_toward_water + current_toward_ground
```

Wind directions are meteorological FROM bearings; current components are true
east/north TOWARD vectors. Polar angles and speeds use water-relative wind,
radians and knots. Model currents use m/s. WGS84 geodesic displacements use metres.
The formulas are supported by the OpenCPN implementation linked below; they are
not an average of nearby station predictions or an along-track speed adjustment.

Explicit midpoint (RK2) samples the velocity at a predicted half-step position
and time, then advances using that velocity. Euler is cheaper but first order;
RK4 evaluates more stages. Higher integration order does not fix missing or
inaccurate current forecasts. We use steps of at most five minutes, shortened
with speed to target half the finest selected grid resolution, and ten-minute
isochrone intervals. Search headings are five degrees apart, with an additional
course-to-steer candidate; departure-bearing bins are two degrees. These are
bounded, approximate search settings, not a proof of global optimality.

Coastline checks, when enabled, apply to integration half-steps and endpoints.
Current sampling preserves masks and temporal gaps without extrapolation.
The final approach uses the same integration, stopping within 0.05 nautical
miles of the waypoint. It does not snap to an uncomputed endpoint or use the
legacy wind-only final-leg shortcut. The saved last position is the calculated
position. Route-table speed is average speed over ground for the leg and bearing
is course over ground; sampled heading and speed through water are in provenance.

The wind adapter retains the application's existing spatial GRIB sampling, but
uses fractional forecast hours and rejects queries outside the available forecast
hours. Its source list is frozen for the run. Current source ordering defaults to finest resolution, then newest run, with
fallback to the next valid selected field and without blending; the routing
panel's Advanced tab offers newest-run-first instead for offshore work. Existing current selections stay
frozen; recovery downloads may extend them. The legacy router's numerical
parameters are hidden while current-aware mode is selected.
Wind lookups read bounded, cached raster windows instead of filtering an entire
GRIB grid at each integration sample. Forecast-hour metadata and the forecast
start timestamp are captured once per adapter. These optimizations preserve the
existing spatial sampling order and fractional-time interpolation; they do not
reduce heading or integration precision. Regression tests compare the local reads
with the original full-raster sampling, including reversed axes and boundaries.
Current-aware calculations and saved timestamps use offset-aware UTC; chart
playback and the route table convert these back to the application's local time.

## Calculation progress and performance

Routing already runs on a background thread. The chart progress bar shows a
distance-based percentage estimate (marked ≈), plus the number of frontier paths
checked within a current-aware search step. The estimate includes remaining
waypoints; it is not a prediction of elapsed CPU time and may retreat when a
detour becomes necessary. Only a completed calculation shows 100%.
Within-step updates are throttled, coalesced and delivered on the GTK main thread;
queued updates from finished, cancelled or replaced calculations are ignored.
Error dialogs, chart-time changes, route-list updates and downloaded-field
notifications also run on the main thread. The worker waits for those operations
and releases that wait if the chart is destroyed; a GTK thread lock alone does
not make worker-thread window creation valid on macOS.

Identical environmental queries reuse a bounded run-local cache. Selection
changes during coverage recovery use distinct cache keys, and cancellation is
checked even on cache hits. Frontier pruning retains the same winning candidates
while discarding rejected trajectory samples sooner. These optimizations do not
change numerical step sizes or heading resolution. Extra Python worker threads
are not used for the CPU-heavy search; sharing mutable GDAL coastline datasets
between additional workers would also require a separate design.

The current router batches all headings across the whole frontier with NumPy.
Current samples and microsecond clocks stay in numeric arrays; detailed sample
records are created only for retained candidates and the arrival path. Wind
queries share exact GRIB windows by cell and forecast interval, including scenario
speed/direction overrides. Polar interpolation is vectorized for the standard
table and efficiency wrapper; custom polars keep their own speed implementation.
WGS84 displacement is batched. Coastline checks first exclude polygons whose
envelopes cannot intersect a candidate, then use the same exact OGR containment
and intersection predicates.

Each candidate keeps its own RK2 substep duration, timestamp, coverage checks and
provenance. No environmental factor, resolution, heading or integration sample
is removed. Unresolved masks remain gaps rather than zero currents, and the
bounded nearest-wet-corner fallback retains its shoreline checks and provenance.
Cancellation is checked during integration and wind lookups. Progress counts
frontier parents whose trajectories have finished. Local clock transitions use
the previous per-parent integrator to preserve wall-clock behavior.

The scalar integrator remains available as a reference (`router.use_numpy = False`).
Tests compare complete arrivals, several consecutive frontiers, parent links,
source selection, masks, gaps, scenario overrides and the actual GSHHS provider.
`tools/benchmark_frontier_routing.py` runs saved tracks with selected offline
current fields, GRIB winds, boat polars and coastline checks enabled. Its
`--engine both` option compares with the scalar reference. Inputs and calculation
must have coverage; the tool does not download fields or save routes.
`tools/benchmark_current_routing.py` also provides a smaller synthetic benchmark.

During integration on 2026-10-12, 15 matched saved-route steps took 8.55 seconds
with arrays versus 73.89 seconds with the previous per-parent routing and
shoreline implementation (8.6x). Both used the shared current-field API. Parent
links and timestamps matched; 33,231 retained sample records were compared, with
maximum position differences of 1.5e-13 degrees. The complete saved route finished
in 94 steps, about 63 seconds, using 17 current sources. These are local
measurements; the complete route was not timed against the previous engine.

Batch source selection converts timestamps once, filters fields by their exact
space/time bounds, and interpolates only the positions inside each field.
Scalar and batch queries follow the same valid-source fallback policy.
Interpolation arithmetic, heading spacing, and integration step lengths are unchanged.

## Missing data and recovery

Missing data stop a search step before it commits an isochrone. The notification
identifies data type, position, UTC time, cause and additional gap count. Missing
current data are never replaced with zero. After valid-source and bounded
nearest-wet-corner fallback, masked-current candidates are rejected like
land-crossing candidates. Other candidates continue. The router stops if no valid
candidate remains; absent coverage still triggers bounded downloads and wind gaps
still stop the search.

Rejected masked samples are marked with red crosses and translucent red halos in
the live isochrone display. Selecting a saved route shows its recorded rejection
locations too, with a legend. These marks identify tested positions, not the full
boundary of an unavailable region. Saved provenance records the exclusions and
states that optimization is constrained to usable environmental coverage. Repeated
hits within approximately 11 metres are merged; at most 10,000 locations are
retained, with an explicit truncation flag if more are encountered.

With automatic downloads enabled, the upfront corridor check queues up to 32 missing
tile/day requests through the regional/worldwide provider planner on a background
I/O worker while routing calculates. Completed fields are installed between search
steps. Overlapping coverage is reused; an actual gap joins an outstanding request
instead of starting another. Provider pacing remains in effect. The search still
checks its actual positions and times, including exploration outside that corridor.

Automatic recovery requests affected quarter-degree tiles and UTC days through
the existing regional/worldwide provider planner. Requests are deduplicated and
limited to 32 tiles per recovery; each step allows at most two download attempts.
Downloads are saved offline and the unchanged step retries with refreshed current
coverage. Stop cancels propagation and downloads. Explicitly disabled datasets
stay disabled. Provider errors, unavailable forecasts and persistent masked gaps
stop recovery with a message; they do not trigger an unbounded retry loop.
With **Download data automatically** enabled for
current-aware routing, missing NOAA GFS wind coverage is fetched too. Departure
wind gaps are filled before searching, and a route/time check queues forecast
coverage for the next day alongside calculation. If the search reaches a wind
gap first, it joins the outstanding download and retries the unchanged step.

Wind downloads request a route-area subset and only the forecast hours needed,
including interpolation brackets and a further day of coverage. Wind sources are
ordered by model issue time, newest first, with load order breaking ties. Older sources fill coverage gaps; scenario wind adjustments remain
unchanged. Before planning, automatic downloads check the latest eligible GFS run
for the route and next 24 hours. Saved forecasts from that run are reused before
HTTP; older cached runs do not suppress the refresh. Download filenames include geographic/time request identity, and transfers publish atomically
so cancellation or failure does not replace a saved GRIB with partial data.
Requests have connection/read timeouts and a 256 MiB transfer bound.

Automatic wind fetching currently supports GFS inputs. Other selected wind models
are not silently replaced with GFS. Unavailable runs, forecast-horizon limits,
provider errors and persistent gaps still stop with a message; missing wind is
never extrapolated or treated as zero. The downloaded GRIBs are saved locally and
added to the app's enabled wind sources on the GTK thread. Forecast downloads and
parsing run off the GTK thread; frozen run coverage changes only between steps.

Saved routing provenance contains selected current manifests, actual source IDs,
wind source names/order, integration settings and samples along the chosen path.
It also records recovered search gaps and download IDs. The route list names
the actual current models used and indicates that data coverage was checked.
Coverage refers to sampled integration positions and times, not an assertion of
continuous coverage, forecast accuracy or navigational clearance.

## Qualification and tides

Model fields keep their existing `routing_qualified=false` metadata. This feature
provides experimental model-based passage planning; it does not complete the
physical qualification described in [the validation audit](marine-data-validation.md).
Narrow-passage timing, model fidelity and an authenticated live SMOC subset remain
to be independently checked. Mathematical tests do not establish those properties.

Tidal-current components already present in CIOPS/SMOC are used once. Independent
CHS station currents are not spread over the route or added again. Tide heights
are not yet used for under-keel or overhead clearance: that needs boat dimensions,
bathymetry/obstruction data and compatible vertical datums. No station-height
average is invented to supply those inputs.

## Primary references

- [OpenCPN vector addition, water-relative wind and Runge–Kutta propagation](https://github.com/seandepagnier/weather_routing_pi/blob/master/src/RouteMap.cpp).
- [VISIR-2: ship weather routing in Python, graph routing and current composition](https://gmd.copernicus.org/articles/17/4355/2024/).
- [the reference application ocean currents and waves](https://example.invalid/start/current.html).
- [Copernicus direction conventions](https://help.marine.copernicus.eu/en/articles/5046685-direction-conventions-of-currents-wave-and-wind-in-copernicus-marine-products).
- [Copernicus global product quality document, SMOC component composition](https://documentation.marine.copernicus.eu/QUID/CMEMS-GLO-QUID-001-024.pdf).
- [ECCC published geographic CIOPS grids and depths](https://eccc-msc.github.io/open-data/msc-data/nwp_ciops/readme_ciops-datamart_en/).

Analytical tests cover helping/opposing/cross currents, time-varying flow,
wind-frame conversion, real engine arrival times, coastal rejection, missing
times/masks/wind, bounded recovery, cancellation, frozen selections, UTC offsets
and the native routing dialog. They use synthetic fields, not fabricated claims
about the accuracy of a real forecast.

## UI responsiveness

Coastal current fallback batches wet-corner candidates and origin/endpoint/segment
validation, retaining field precedence, distance and time limits, corner tie order,
interpolation values and provenance. Immutable grid resolution is cached rather
than repeatedly decoding a full manifest. Cancellation remains checked between
field and corner batches. Scalar callbacks remain supported.

Shapely 2 supplies compiled GEOS point-containment and segment-intersection batches
over the same repaired, clipped GSHHS polygons. Prepared polygons are cached, and
point checks avoid allocating individual OGR geometries. Polygon boundaries keep
the existing strict containment and inclusive segment-intersection semantics.
Installations without Shapely retain the OGR path. No coastline simplification or
environmental-factor removal is involved. The saved full-route comparison improved
from a 63.79-second median to 41.39 seconds with identical path positions, times
and source sets; see the [profiling report](history/routing-profile-2026-10-12.md).

Live isochrones render on a background worker into a cached surface, using a
snapshot of the search history. Main-thread draws paint the completed surface;
pan and zoom can reuse it while a replacement renders. Projection and WGS84
join-distance calculations are batched, preserving every frontier point and the
existing 3 km connection cutoff. New updates replace queued work, and clearing a
run cancels obsolete frames. Red exclusion locations remain in the same overlay.
These display changes do not alter routing resolution, integration or sampling.

The app enables GTK's legacy GDK thread lock for existing dialogs and download
workers. Routing's main-thread dispatch enters and leaves that lock around UI
callbacks. In particular, closing a modal preflight dialog must release the lock
reacquired by `Gtk.Dialog.run`; otherwise the main loop deadlocks before routing
starts. A native regression test uses the same initialized lock and clicks the
warning's Run anyway button, then verifies a subsequent UI update completes.
