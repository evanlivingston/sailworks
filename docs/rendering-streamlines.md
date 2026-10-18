# Static wind streamlines

The Qt Weather panel's wind-style selector offers **Barbs** (the unchanged
default) and **Streamlines**. `gribWindStyle` persists the choice. Streamlines
are static spatial curves at the selected forecast time, not animated
particles or time-integrated air-parcel trajectories. Direction chevrons point
downwind; color uses the same speed buckets and opacity as barbs. The shared
Density control changes native-point thinning for barbs and path spacing for
streamlines. The final preset is called All forecast points for barbs and
Maximum for streamlines; Maximum is still bounded, not one path per sample.
Show wind and Show wind over land apply to both styles.

## Preparation and numerical scope

The existing wind worker traces curves; painting never loads data, traces
paths, or queries polygons. Streamline preparation is opt-in, so barb mode does
not pay for it. A style switch retains the previous symbols as a preview until
the new frame is ready. Once prepared, land toggles and stationary repaints use
cached vectors without another weather job. Existing pressure and wind-fill
portrayal is unchanged.

Curves use the same forecast selection/blending as the weather fill. Regular
native columns are used directly; mixed/projected grids reuse the existing
regular fill resampling (0.1-degree target spacing, 240-node axis limit and
30,000-node cap). No routing/shared sampler calculations change. The visual
approximation is bilinear interpolation of earth-relative U/V on projected
node positions, followed by normalized midpoint integration with four-pixel
steps in both directions. It is not a new meteorological interpolation
contract. Missing/nonfinite/calm cells, coverage boundaries and large coordinate
gaps stop a curve rather than extrapolating it.

Sparse/Standard/Dense/Very dense/Maximum use 64/44/28/16/12-pixel seed spacing.
Seed and segment budgets scale with density, bounded by 2,000 candidates,
160 steps per direction and 60,000 segments per prepared frame. Standard uses
600 candidates and 30,000 segments. An occupancy grid limits overlapping paths; center-first
seeding spends the budget on the visible area before overscan margins.
Cancellation is checked during integration and shoreline classification.

Streamline density changes queue worker preparation using the existing cached
weather field, retaining the previous paths until replacement geometry is
ready. They never trace paths in a paint handler. Barb density remains a
foreground-only setting and does not invalidate the sampled weather frame.
Streamline jobs skip the unused native-point barb land flags and classify only
the segments they actually display.

Each segment stores geographic endpoints, interpolated speed, a shoreline flag
and an optional chevron. Shoreline containment is tested at each short
segment's midpoint on the worker. This is display clipping sampled at four
pixels, not exact polygon/segment intersection: subpixel islands and glyph
extents are not guaranteed to be clipped. Keeping flagged segments allows
immediate land-visibility changes without regenerating the paths.

## Presentation and validation

Foreground code projects cached geographic endpoints with NumPy and batches
them into the existing colored-line representation for Cairo, Qt and the GPU
stroke mesh. Chevrons remain a fixed screen size when zooming. The geometry
cache includes viewport and land visibility. New views/times use the normal
bounded weather scheduling and refresh rules.

Tests cover uniform wind and meteorological direction, curved rotating flow,
geographic registration, calm/missing/gapped data, work budgets, cancellation,
shoreline visibility, cached repaint/zoom behavior, CPU/GPU stroke compatibility,
style persistence and show-wind restoration. The read-only saved-data replay is
`PYTHONPATH=. python tools/diagnose_wind_toggle.py --style streamlines`.

## Saved-data measurement

Initial fixed-standard streamline implementation, before the shared Density
control: single cold-process replay per style, same 38 enabled local forecast files,
saved map view and forecast time, 99,043 native wind nodes. Geometry medians
use 20 slightly shifted views to bypass the stationary cache; the saved barb
density was Very dense. These are component timings, not complete UI frames.

| Component | Barbs | Streamlines |
| --- | ---: | ---: |
| Full initial worker job | 7.071 s | 7.435 s |
| Moving geometry, land symbols enabled | 10.505 ms | 3.542 ms |
| Moving geometry, land symbols hidden | 4.667 ms | 2.027 ms |

Streamlines reached the 30,000-segment preparation cap. Native sample and land
counts were identical in both modes (99,043 and 65,338). Initial preparation
adds approximately 0.36 seconds in this replay; cached projection is cheaper
than Very dense barbs here, not necessarily cheaper than every barb preset.

After adding the shared Density control, a separate matched replay at the
user's wider saved view (38 files, 187,980 native nodes) measured:

| Component | Sparse | Maximum |
| --- | ---: | ---: |
| Full initial worker job | 12.603 s | 13.433 s |
| Prepared segments | 18,331 | 60,000 (cap) |
| Visible offshore line segments, including chevrons | 2,305 | 12,099 |
| Moving offshore geometry, median of 20 | 1.599 ms | 5.844 ms |

These are single cold-process preparations and do not measure density-switch
latency: switches reuse the field cache and keep the old paths visible while
rebuilding. The larger initial times are from the wider view's wind/pressure
sampling (about 10.8 seconds), not a repeated coastline-mask cache regression.
