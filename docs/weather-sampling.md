# Weather sampling

Wind and mean sea-level pressure use `core/gribsampling.py`. GDAL continues to
handle GRIB decoding. `Grib`, `GribManager`, private cursor/display handles,
ordinary routing, current-assisted routing, and frontier batches all use the
same record selection, bilinear interpolation, and time blending rules.

## Records and selection

Each relevant band is indexed once by element, level, reference time, valid
time, originating center/subcenter, generating process, product status, and
ensemble member where GDAL provides that metadata. U/V wind is selected at
10 m above ground (`10-HTGL`); pressure uses PRMSL/MSLMA at mean sea level
(`0-MSL`). Other levels and interval-statistic products are excluded. Metadata
without a level retains the existing imported-file assumption for that field.

Instantaneous GRIB2 templates 4.0 and 4.1 are supported. Ensemble members remain
separate candidates; they are not averaged. NCEP GFS analysis process 81 and
forecast process 96 are recognized as one model family. Other process IDs remain
separate. When process identity is absent (as with some GDAL GRIB1 exports),
files remain separate unless their source supplies an explicit `model` identity.
This avoids guessing that unrelated models from one center are compatible.

The newest enabled reference time wins at each location. Enabled-file order
breaks ties between models from the same issue time and between duplicate
records. Older runs fill unavailable coverage. Separate files from the same
identified model/run can provide either time bracket or separate U/V components.
Complete-file U/V pairs are preferred, so a masked component does not splice a
partial vector from one complete file into another complete file's vector.
Sources that contribute are recorded for routing provenance.

Time brackets are chosen per location, considering usable spatial samples, so a
missing record or masked cell in one file can fall through to another checked
file. Different model runs and ensemble members never form a temporal blend.

## Numerical behavior

For native cell coordinates x,y, bilinear sampling uses the four surrounding
sample centers and weights `(1-x)(1-y)`, `x(1-y)`, `(1-x)y`, and `xy`. The full
inverse affine transform is used; projected grids transform query coordinates
into their native CRS. Longitudes wrap onto regional grid coordinates, and
full-circle grids interpolate across their storage seam.

GDAL's geotransform describes pixel corners. Samples are at pixel centers
(offset 0.5 in each raster axis). The former corner interpretation shifted
weather by half a cell; correcting it changes both display and routing values.
Wind components are taken as east/north components supplied by the provider;
this work does not add decoding/rotation for grid-relative vector products.

For bracketing valid times t0 and t1, `f=(t-t0)/(t1-t0)` and
`value=value0+(value1-value0)*f`. Timestamps retain fractional seconds. Wind U/V
components are blended before conversion to speed and meteorological direction;
angles and speeds are not averaged independently. Pressure is interpolated as a
scalar and exposed in hPa (Pa records are converted once).

An exact valid-time hit returns that record. If only one usable bracket exists,
its value is held within the selected run's declared coverage. Queries before
reference time, after declared coverage, or outside spatial sample support are
unavailable. A multi-run file's source-wide end time cannot extend a different
run's record coverage. Single-bracket holding is a defined fallback, not an
extrapolated trend, and may prefer a newer run over an older complete bracket.

GDAL NoData values, masks, NaN, and infinity are unavailable. Missing corners
with positive weight invalidate the cell; weights are not renormalized across
holes. Zero-weight corners do not invalidate exact-node/edge queries. Index
roundoff tolerances are 1e-7 of a cell at boundaries and 1e-12 for zero weights.
Missing values remain NaN internally and `None` in point-query APIs; no numeric
sentinel is allowed into wind speed, pressure contours, or routing calculations.

The display evaluates this sampler at native nodes in the visible area. Existing
barb, color-fill, and contour portrayal remains; interpolating raster colors is
only a visual approximation to the continuous vector field. The cursor and
routing sample the continuous numerical field at their exact locations.

## Performance and validation

Each private handle caches at most six decoded bands and 4096 exact-coordinate
spatial stencils; coordinates are not quantized. Scalar point queries avoid
constructing display arrays. Frontier batches sample positions/timestamps in
arrays using the same weights and source rules, with cancellation checks between
record groups. Workers retain private GDAL handles. Evicted handles stay alive until the
cross-file query completes, then close explicitly; idle renderers release their
remaining cached handles when their last map is forgotten.

On `gfs_20261014_12z_72h.grib2` (61 by 61 nodes), a 200-query local benchmark
measured median warm point queries of 0.23 ms versus 0.43 ms on `dev`. The first
query took 53 ms versus 15 ms due to indexing and decoding. These are local
measurements, not a general performance guarantee. Numerical results change
intentionally because minutes/seconds, spatial weights, and pixel centers are
now used. No spatial downsampling or interpolation of direction angles was added.

Analytical synthetic tests cover fractional and subhour record times, bilinear
weights, vector direction wrapping, split files/components, exact/single-record
paths, masks/NoData, level filtering, distinct models/members, run precedence,
reversed/rotated/projected grids, longitude seams, batch/scalar consistency,
consumer agreement, pressure units, provenance, and bounded caches.

References: [GDAL geotransforms](https://gdal.org/en/stable/tutorials/geotransforms_tut.html),
[GDAL GRIB metadata](https://gdal.org/en/stable/drivers/raster/grib.html),
[GRIB2 template 4.0](https://codes.ecmwf.int/grib/format/grib2/templates/4/0/),
[NCEP process identifiers](https://www.nco.ncep.noaa.gov/sib/grib2/grib2_all_tables_module.pdf).
