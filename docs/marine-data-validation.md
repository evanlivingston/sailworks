# Vancouver Island marine data validation

Research snapshot: 2026-10-11. This records data availability and unresolved
integration questions, not validated navigation predictions.

## Run the probe

The standalone tool uses the application's GDAL/NumPy environment and does not
launch GTK, change application settings or feed data into routing:

```sh
.venv/bin/python tools/marine_probe.py \
  --output /private/tmp/gwr-marine-probe-20261011 --date 2026-10-11

.venv/bin/python tools/marine_probe.py \
  --output /private/tmp/gwr-marine-probe-20261011 --date 2026-10-11 --offline
```

Use a new output directory for a fresh snapshot. The cache is immutable by URL
within a snapshot, including the mutable Datamart `today` directory listings.
Replay with the same date and forecast-hour arguments. `--date` selects the CHS
UTC day; CIOPS uses the latest cycle advertised in each domain's live listing.
The report records actual runs and valid times rather than implying these match
the requested CHS date.

Default CIOPS samples are hours 0, 6, 12, 18 and 24; choose up to nine hours within
0–48 using `--forecast-hours`. Only 0.5 m velocity files are downloaded. Requests
are separated by at least 2.1 seconds, have a 30-second socket timeout and an
8 MiB response limit. Errors remain in `report.json` and produce a nonzero exit
status. A complete collection still has `routing_ready: false`.

Output contains raw responses, per-response URLs, retrieval times, byte sizes,
SHA-256 checksums and available HTTP validators. Offline replay verifies checksums
and refuses missing responses. NetCDF inspection reports coordinate bounds,
valid/masked cell counts, validity timestamps, variable/global attributes and
nearest-cell availability at four representative positions. It never searches
across land for a replacement wet cell, interpolates currents or treats a masked
cell as zero velocity.

Keep downloaded CHS responses and generated reports local. Repository tests use
authored JSON samples and a synthetic NetCDF grid, without redistributing CHS
predictions or adding binary fixtures. Review the provider terms before using or
distributing outputs.

## Observed capabilities

The live run collected 60 responses (about 9.3 MB total), including 20 surface
velocity files from the 2026-10-11 12Z CIOPS cycle. The sample window is too sparse
to validate slack timing or a complete tidal cycle at passage resolution.

| CHS station | Identifier | Observed series |
| --- | --- | --- |
| Victoria Harbour | 07120 | Water-level predictions and high/low events |
| Point Atkinson | 07795 | Water-level predictions and high/low events |
| Campbell River | 08074 | Water-level predictions and high/low events |
| Seymour Narrows, water level | 08105 | Water-level predictions and high/low events |
| Seymour Narrows, currents | 08108 | Continuous speed/direction predictions and slack/flood/ebb events |
| Tofino | 08615 | Water-level predictions and high/low events |

The API's phenomena metadata supplies metres for water level, knots for current
speed and degrees for direction. Continuous samples at the requested 15-minute
resolution include both day boundaries. Quality flags are preserved; continuous
predictions and current events carry flag 2 in this snapshot, while the sampled
high/low events carry flag 1. Flag 2 is not proof that predictions are bad, nor is
it a passed quality check. Preserve it as reported. The
[CHS service documentation](https://www.tides.gc.ca/en/web-services-offered-canadian-hydrographic-service)
defines the flags and client rate limits.

The station's `operating` flag cannot be used to discard prediction stations:
both Seymour Narrows stations report false but return predictions. The global
time-series definitions response also omits the continuous `wcsp1` and `wcdp1`
codes advertised by the current station. Discover capabilities from station
metadata and successful series responses, retaining definition discrepancies.

| CIOPS sample location | Salish Sea 500 m | West 2 km |
| --- | --- | --- |
| Juan de Fuca, 48.25 / -123.5 | Valid nearest X/Y cell | Valid nearest X/Y cell |
| Strait of Georgia, 49.3 / -123.65 | Valid nearest X/Y cell | Valid nearest X/Y cell |
| Seymour Narrows, 50.133333 / -125.35 | Valid nearest X/Y cell; passage accuracy unresolved | Valid nearest X/Y cell; passage accuracy unresolved |
| Tofino offshore, 49.1 / -126.0 | Masked at sampled point | Valid nearest X/Y cell |

All five sampled hours showed these availability classifications. This is a point
check, not a polygon describing navigable coverage or proof that nearby cells are
connected by water. The two products differ substantially at Seymour Narrows;
preferential resolution alone is insufficient to establish a trustworthy passage
forecast. Nearest grid cells also represent different positions at each resolution.

Observed NetCDF details:

- Product metadata identifies `CIOPSW_240_F`, with CF-1.6 conventions.
- X is named `vozocrtx`; Y is named `vomecrty`. Both report `m s-1` and a
  missing-value sentinel around `1e20`. The published valid range is also applied
  when reporting cell availability.
- Coordinates are explicit one-dimensional latitude/longitude arrays with
  longitudes in 0–360. The sampled Salish Sea grid is 888 × 629 and the West grid
  is 785 × 600. GDAL's default raster geotransform is not a usable geographic
  transform for these files; read the coordinate arrays.
- Validity is stored as seconds since 1950-01-01 on a Gregorian calendar. The probe
  checks the validity timestamp against each filename's run and forecast hour.
- The variable standard names are `sea_water_x_velocity` and
  `sea_water_y_velocity`. Do not infer true east/north reference merely from X/Y
  naming. Verify reference/orientation against the product specification before
  constructing routing vectors.
- Files are instantaneous 0.5 m depth samples. They are not observations or a
  vertically averaged current, and do not validate performance at keel depth.

Source: [ECCC CIOPS product documentation](https://eccc-msc.github.io/open-data/msc-data/nwp_ciops/readme_ciops-datamart_en/)
and locally inspected downloads. Attribute future displayed or derived ECCC data
to Environment and Climate Change Canada and link the
[data-services licence](https://eccc-msc.github.io/open-data/licence/readme_en/).

## Integration gates and decisions

Update: [current-aware routing](current-routing.md) is now an experimental,
opt-in planning mode with analytical calculation tests and explicit gap handling.
The physical qualification gates below remain open for narrow-passage timing;
model manifests retain `routing_qualified=false`. These calculations do not
represent completed field validation or clearance checks.

1. **Proceed with CHS for the user's own route planning.** The user accepts that
   use and has directed that licensing not block development. Retain the
   [provider terms](https://www.tides.gc.ca/en/licence-agreement) and attribution
   in source metadata. This project decision does not change the provider's terms
   or establish permission for commercial distribution. No request has been sent
   to CHS. Physical validation remains a separate requirement.
2. **Seymour Narrows direction semantics are resolved.** The
   [direction investigation](chs-current-directions.md) confirms that the 2026
   published tables agree with the API: flood 180 true, ebb 0 true. The public
   page's 032/212 legend is inconsistent and also appears at other stations.
   Use verified API bearings without remapping; retain continuous directions,
   explicit event qualifiers and station-specific validation. Depth remains a
   separate question.
3. **Use CIOPS-West as the broad regional candidate, with explicit Salish Sea
   priority where validated.** The Salish Sea bounding rectangle includes masked
   west-coast locations. Select coverage from the actual masks and paired valid
   components, not the rectangle. No provider is yet approved for optimized transit
   of narrows; passages remain unsupported until compared with a suitable reference.
4. **Finish physical validation before a current sampler reaches routing.** Verify
   vector reference, current composition (including tidal forcing), coordinate
   conventions, matching X/Y grids, time interpolation and coastline connectivity.
   The probe deliberately reports X/Y independently and does not yet certify a
   usable vector field. Compare high-frequency passage predictions with an
   independently suitable reference over multiple conditions; the five snapshots
   cannot validate slack or strongest ebb/flood.
5. **Defer clearance calculations.** CHS metadata contains datum conversions at
   sampled water-level stations, but datum offsets alone do not establish the
   datum of every sample or compatibility with the chart. Validate those semantics
   before using height in depth or bridge-clearance calculations.

The next application milestone can build provider contracts, CHS downloads,
immutable source selection and provenance storage. Passage rules and current-driven
optimization still depend on physical validation and qualifying individual stations.

## Verification

Run the offline probe tests with:

```sh
.venv/bin/python -m unittest test.test_marine_probe -v
```

They exercise UTC offsets, empty/duplicate/missing samples, quality preservation,
cache integrity, network-free replay, download bounds, listing-link filtering,
longitude wrap and synthetic NetCDF time/mask handling for both component names.
They validate the research tooling, not marine forecast accuracy.

Validation including the direction audit: 424 tests and 38 subtests passed, along with Black,
Flake8 and the application mypy check. Live collection and checksum-verified
offline replay completed. Missing test dependencies were supplied from a temporary
directory; the application environment and dependency declarations were unchanged.
