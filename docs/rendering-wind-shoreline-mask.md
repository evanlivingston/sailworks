# Windbarb shoreline masking

The land-visibility toggle removes already selected windbarbs. It must not
change the native forecast samples selected by density thinning or the symbol
count limit. Offshore barbs therefore retain their coordinates when land barbs
are hidden. The symbol's origin (the unfeathered end of the shaft), not its
entire 20-pixel extent, is tested against land.

## Worker preparation

`WindLandMask` prefers installed GSHHS L1 shoreline polygons, using the base
chart's scale tiers (`c`, `l`, `i`, `h`, `f`). The packaged country outlines
remain the fallback if shoreline data is absent. This is a display-only
source change; weather sampling, blending and routing calculations do not
change. GSHHS is not the land geometry of every ENC/raster chart, so this does
not promise exact pixel agreement with all chart products.

Containment uses the original polygon coordinates and holes, without raster
quantization or a new geometry simplification. The existing scale-tier choice
is itself a coastline-detail approximation. Prepared polygon containment and
one-degree candidate buckets avoid repeatedly querying the shapefile for
neighboring samples. Candidate buckets retain polygon envelopes; each point
rejects non-overlapping envelopes before fetching or preparing a geometry.
This is important in dense archipelagos: a bucket with more than 64 islands
otherwise cycles through and evicts the entire prepared cache for each water
sample. Envelope rejection is exact and does not change containment results.
Caches are bounded to 64 prepared polygons, 32 candidate
buckets and 16,384 point results. A worker checks for newly installed data once
per wind render job and clears caches when the selected source changes.

Each completed job publishes all native wind samples plus a Boolean land flag
per sample, even when land symbols are enabled. Preparing these flags up front
makes the land checkbox foreground-only: it is excluded from the weather-frame
content key and never triggers a new sampling or raster job. The foreground
geometry cache includes the checkbox state, so a toggle changes visibility
immediately, retaining offshore symbols and restoring all symbols on re-enable.
Foreground rendering applies the flags with NumPy only after selection.
Stationary views continue to reuse their geometry cache. No file
I/O or polygon containment is added to a paint or gesture handler. Hiding land
can intentionally display fewer symbols than before: empty cells are no
longer refilled with different nearby samples.

## Wind visibility

Hiding wind no longer calls `forget()`: that discarded the prepared frame and
decoded forecasts, forcing a rebuild on re-enable. When isobars remain visible,
the renderer retains at most one wind frame per canvas while rendering a
pressure-only frame. Re-enabling wind restores that frame only for an exact
content-key match (forecast time, sources, raster display settings and pixel
density; land-symbol visibility is applied separately).
Normal viewport checks still request new coverage after a pan or zoom. A
matching unchanged view restores without starting a worker, even if a hidden
pressure-only job is still running. `forget()` on source removal or canvas
disposal clears the retained frame as well as the normal renderer state.

## Repeatable measurement

Run `PYTHONPATH=. python tools/benchmark_wind_land_mask.py` with the application's
dependencies and installed coastlines. It compares the country-mask baseline
with detailed masking for 6,400 native nodes in the Puget Sound / Vancouver
Island region, including cold/warm worker preparation and uncached moving-view
geometry. The old and new outputs intentionally differ in coastline source
and sample-selection order; timings are not a claim of identical output or
an end-to-end UI frame benchmark.

Local measurement (macOS, installed GSHHS intermediate tier, 6,400 samples):

| Component | Country baseline | Shoreline + stable selection |
| --- | ---: | ---: |
| Cold worker classification | 126.82 ms | 153.15 ms |
| Warm worker classification, median of 10 | 0.97 ms | 1.01 ms |
| Moving-view geometry, median of 100 | 0.40 ms | 0.49 ms |

This is a small measured overhead, not a zero-cost claim. The cold comparison
is one run per implementation and includes initialization. Both paths retain
the existing stationary-geometry cache. Offshore symbol counts differ (199
versus 137) because shoreline accuracy and post-selection masking intentionally
change visibility without substituting another forecast point.

The small 6,400-node benchmark did not expose the dense-island cache churn.
A read-only replay with the user's 38 saved forecast files and saved view
produced 102,672 native wind samples and took 47.20 seconds before envelope
rejection. Wind sampling took 3.69 seconds, pressure preparation 1.05 seconds,
and fill resampling 0.09 seconds. These larger real-data timings supersede any
assumption that the earlier small benchmark demonstrated fast initial display.
`PYTHONPATH=. python tools/diagnose_wind_toggle.py` replays the locally saved
sources without modifying settings, forecasts or the workspace and reports
stage timings plus actual geometry counts with land symbols enabled/disabled.

After envelope rejection, the same saved-data replay completed in 7.19 seconds:
wind sampling 3.79 seconds, pressure preparation 1.12 seconds, fill resampling
0.10 seconds, land classification 1.60 seconds, and painting 0.38 seconds.
The number of land samples remained 71,165; geometry counts remained 11,844
line segments with land enabled and 2,908 with land disabled. These are initial
worker timings, not checkbox latency: toggles on a prepared frame still start
no worker. First display must still wait for initial forecast preparation.
