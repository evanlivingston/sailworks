# Tides and currents implementation plan

Status: implementation in progress, 2026-10-11.

Milestone 1 has a working [research probe and capability report](marine-data-validation.md).
Live data collection and offline replay pass. The user accepts CHS use for their
own route planning; licensing is not a blocker for that development. Preserve
provider terms and attribution. [Seymour Narrows direction semantics are resolved](chs-current-directions.md)
against the published 2026 tables. Individual station qualification and CIOPS
physical validation remain open for qualified narrow-passage timing.

The first source-management increment is implemented: a persistent, GTK-independent
marine cache and source selection; CHS Pacific station discovery and bounded,
cancellable prediction downloads; and a **Tides & currents** panel beside the GRIB
manager. Dataset identities include normalized samples and response checksums.
Station current sampling returns true east/north vectors and never supplies a
spatial field. Immutable selections support explicit fallback and per-query
overrides. Routing results can now preserve optional provenance, with older
routes remaining explicitly unknown. Opt-in current-aware routing now consumes
spatial fields, checks coverage, supports automatic gap downloads and populates
provenance. This is model-based planning, not completed passage qualification. See
[the manager guide](tides-currents-manager.md) for usage and current limits.

The default manager workflow now selects spatial current sources from a route or
map area and a UTC window. CIOPS-West/SalishSea and a Copernicus SMOC worldwide
adapter are implemented, with source overrides, offline masked-grid sampling and
position-by-position previews. Individual stations are an advanced supplement.
CIOPS passed a live short-window download; SMOC catalogue and offline adapter
checks pass, with an authenticated subset still to validate. Current-aware
routing is implemented as an opt-in mode; regional physical qualification remains
outstanding. See [routing algorithms, recovery and limits](current-routing.md).

## Objective

Add tides and currents to passage planning and weather routing, starting around
Vancouver Island while keeping the data interfaces usable worldwide. Users must
be able to choose their sources and inspect the data actually used for each
calculation. Source management and saved provenance are initial requirements.

The first useful release will show tide/current information and compare departure
times along a fixed route. Route optimization follows once the data and vessel
motion calculations have been validated.

## Existing integration points

- `core/gribmanager.py` and `gtk/gribmanagerwindow.py` provide the existing pattern
  for downloading, enabling and managing forecast datasets.
- `core/core.py:create_routing` constructs the external `weatherrouting` engine.
  It currently supplies a wind provider, polar and coastline validity callbacks.
- `gtk/routingwizarddialog.py` and `gtk/chartstack_routing.py` handle routing setup
  and results.
- `core/geo/routing.py` serializes routing results and now accepts optional dataset
  provenance; calculation-time population remains to be implemented.
- The installed `weatherrouting` router advances candidates using boat speed
  without current vectors and rejects candidates that move farther from the next
  waypoint. Its current algorithm cannot simply be assumed to support waiting
  or useful tidal detours.

Confirm these integration points against the dependency version selected for
implementation. Change dependency source and packaging explicitly; do not patch
the installed virtual environment as the implementation.

## Data candidates and validation gates

These are candidates from the initial research, not validated navigation coverage
or promises about future service availability. Recheck terms and sample data in
the first milestone.

| Provider | Intended role | Questions to resolve before integration |
| --- | --- | --- |
| CHS IWLS | Canadian station water levels, current predictions and passage events | Which Vancouver Island stations expose continuous currents versus events? What are the units, datum, depth, quality flags, time limits and cache/redistribution terms? |
| ECCC CIOPS-West and Salish Sea | Spatial ocean-current forecasts around Vancouver Island | Inspect velocity variables, vector reference, depth, land masks, actual coverage, file sizes and download strategy. Assess the 2 km west-coast and 500 m Salish Sea products against named passages. |
| AVISO FES | Global astronomical tide/current fallback | Assess storage and regional extraction, coastal masks, prediction dependencies and distribution terms. FES2022 heights and FES2014 currents are separate candidates; do not assume generally available FES2022 currents. |
| NOAA CO-OPS | Next regional adapter for neighbouring US waters | Verify harmonic versus subordinate station capabilities, event-only series and depth/bin selection. |

Reference entry points:

- [CHS web services](https://www.tides.gc.ca/en/web-services-offered-canadian-hydrographic-service)
- [Seymour Narrows current station](https://www.tides.gc.ca/stations/08108/currents)
- [CIOPS documentation](https://eccc-msc.github.io/open-data/msc-data/nwp_ciops/readme_ciops_en/)
- [CIOPS downloadable products](https://eccc-msc.github.io/open-data/msc-data/nwp_ciops/readme_ciops-datamart_en/)
- [AVISO tide products](https://www.aviso.altimetry.fr/en/data/products/auxiliary-products/global-tide-fes.html)
- [NOAA API](https://api.tidesandcurrents.noaa.gov/api/dev)

## Data architecture

Introduce a proposed `core/marine/` package, independent of GTK. Keep downloads
and authentication separate from sampling cached datasets. Provide three
capabilities rather than requiring every adapter to implement everything:

1. **Water levels:** station series or supported spatial predictions, with height
   in metres and an explicit vertical datum; high/low events where available.
2. **Current fields:** east/north velocity in metres per second at a position,
   UTC time and stated depth, with valid spatial and temporal coverage.
3. **Passage events:** slack/turn and maximum flood/ebb predictions, plus explicit
   station-to-passage associations and user transit constraints.

Normalize coordinates to WGS84, use timezone-aware UTC internally, and convert
to local time and preferred units for display. Preserve original metadata.
Convert provider vectors to true east/north before sampling; distinguish current
direction toward from meteorological wind direction from. Handle longitude wrap
for global providers.

Each dataset needs an immutable identity, provider/model/version, forecast run or
prediction retrieval time, valid interval, coverage/mask, resolution, depth,
datum where applicable, quality information, attribution and cache location.
Label currents as tidal-only, non-tidal or total; unknown composition cannot be
silently combined with another field. Harmonic datasets also need constituent
dataset and prediction-library versions.

Sampling must return a value with provenance or an explicit reason for missing
data. Missing data is never zero current. Interpolation must respect land masks,
connected water and time boundaries; do not extrapolate expired forecasts or
spread a station current into neighbouring channels. Event-only stations do not
automatically provide a continuous velocity field.

## Source selection and route provenance

Add a **Tides & Currents manager** following the GRIB manager's interaction
pattern. Show provider, dataset/run, capabilities, coverage, resolution, valid
times, download age, size and offline status. Support downloads, cancellation,
enable/disable, removal and an explicit ordered fallback policy for each
capability. Removing cached data must leave saved route metadata intact.

The routing setup shows wind, current, water-level and passage sources together.
Offer per-calculation overrides without changing manager defaults. Clearly show
which capabilities are unused by a calculation mode; displaying water levels
does not mean a route was checked for depth clearance.

Select one current field per sample using the configured policy. Fallback occurs
only under the selected rules and is recorded. Do not blend providers by default
or add tidal currents to total currents. A passage station can constrain a named
passage or provide a local validated replacement; proximity alone is insufficient.
Evaluate gridded currents and passage transit constraints as separate capabilities
and report disagreement rather than silently replacing either prediction.

Before calculation, freeze source selection and dataset identities, including the
wind GRIBs. Resolve overlapping wind files deterministically as well. Background
downloads or manager changes must not alter an active calculation. Prefetch the
required data and identify known coverage limits; an optimizer can discover more
gaps as it explores, so enforce coverage while sampling too.

Default to stopping a calculation that requires unavailable current data. Allow
an explicit mode that continues without currents in uncovered sections and label
the result partial. Record every fallback and uncovered section. Wind-only
routing remains an explicit available mode.

Persist a versioned calculation manifest containing selected sources, actual
datasets sampled, segment/time coverage, gaps/fallbacks, engine version, vessel
and polar settings, departure time, integration settings and constraints. Distinguish
sources used for search from sources used along the final route. Include the
dataset identifiers/checksums needed for repeatability; metadata alone cannot
reproduce a calculation after cached data disappears.

Results expose a concise source summary and detailed segment provenance. Old
saved routes remain readable and show provenance as unknown. Native save/load
preserves the manifest; document GPX metadata limitations and provide a manifest
export alongside GPX if needed.

## Delivery milestones

### 1. Validate Vancouver Island data

Create a small command-line probe that downloads limited CHS and CIOPS samples
without launching GTK. Use Victoria/Juan de Fuca, the Strait of Georgia, Seymour
Narrows and a west-coast location to exercise different coverage conditions.

Record actual payloads, capabilities, units, masks, valid times, download sizes,
licensing and dependencies. Compare CHS results with its published tables and
compare CIOPS timing/direction with local current stations where appropriate,
accounting for station depth and spatial differences. Document acceptance
criteria and limitations before enabling passage use. This is a plausibility and
coverage investigation, not proof of navigation accuracy.

**Done when:** there are reproducible sample requests, small permitted test
fixtures, a capability matrix and a decision about where each source can be used.
Unresolved passage coverage remains explicitly unsupported.

### 2. Build providers, caching, manager and provenance

Implement normalized capabilities and the CHS/CIOPS adapters, asynchronous
downloads, cache validation and deterministic selection. Add the manager, routing
source summary and versioned manifest storage. Extend wind selection/provenance
only as needed for consistent calculation records; avoid an unrelated GRIB UI
rewrite. Add station markers, curves and current overlays using existing forecast
playback where practical. Keep network and heavy sampling work off the GTK thread.

**Done when:** users can select and inspect sources, download a bounded forecast,
restart offline, query displayed values and save/reopen provenance. Manager changes
cannot change an in-progress calculation, and old route files still open.

### 3. Compare departure times on a fixed route

Start with a stated constant speed through water and integrate each leg against
the time-varying current. Solve the heading required to hold the leg, account for
cross-current, and identify legs that cannot be held or cannot make forward
progress. Sample along each leg using short substeps rather than applying one
station value to the entire leg.

Compare a configurable range of departure times. Show ETA, current contribution,
passage arrival windows, coverage and sources. Passage thresholds come from
explicit user settings; distinguish a prediction from an acceptable transit
window. Later add polar-based sailing leg evaluation with explicit handling of
headings the boat cannot sail directly.

**Done when:** zero-current results match baseline travel times; following,
opposing, cross and reversing current cases match controlled expectations; a
Vancouver Island example shows traceable departure comparisons and coverage gaps.

### 4. Extend weather routing to currents

Provide the engine with an optional current sampler. Calculate ground velocity
as through-water vessel velocity plus current. Convert earth-relative forecast
wind to water-relative wind for the polar using vectors. Preserve heading versus
course over ground and speed through water versus speed over ground in results.

Integrate motion with spatial/time substeps, check the traversed path against
coastlines, and correct endpoint arrival/ETA calculations. Audit the existing
engine's unit and time conventions before extending them. Keep wind-only
behaviour compatible when currents are disabled.

First validate fixed-departure optimization. Then replace the monotonic-distance
pruning assumption with a bounded search that retains useful detours and different
arrival times. Add departure search and waiting only at user-designated suitable
locations, with time-dependent passage constraints and explicit planning horizons.
Waiting must not model an unanchored boat as stationary in flowing water.

**Done when:** controlled cases demonstrate beneficial detours, a later arrival
that catches a better current, legitimate waiting and infeasible passages;
existing wind-only cases pass; CPU/memory remain bounded on representative routes.

### 5. Expand coverage and add water-level constraints

Add NOAA and a validated global harmonic adapter without changing the routing
interface. Describe actual coverage rather than promising coverage at every coast.
Handle transitions between regional and global datasets through explicit fallback.

Separately add depth/draft and bridge-clearance constraints only where chart and
water-level datums can be reconciled and bathymetry is adequate. Treat waves and
wind-against-current effects as subsequent work needing their own data and model.

**Done when:** another region works through the same contracts, and any clearance
feature has datum-aware acceptance tests and explicit unsupported cases.

## Validation and contribution workflow

Use deterministic offline fixtures for adapter tests, including masks, missing
values, timezones/DST, vector conventions, forecast expiry and malformed downloads.
Keep live-provider checks opt-in. Test source priority, overlapping datasets,
double-count prevention, cancellation, source snapshots and provenance round trips.
Use analytical current fields for motion and optimizer tests. Perform native GTK
checks for manager responsiveness, playback, cancellation and saved-route details.

Run targeted checks during development and the repository's required lint,
typecheck and unit-test checks before integration. A documentation-only plan needs
diff review and whitespace checks, not a full application test run.

Implement coherent feature branches from `dev`: data validation; provider/cache
contracts; source manager/display/provenance; fixed-route departure evaluation;
engine current support; detours/waiting; additional providers and clearance.
Integrate validated work into `dev` without waiting for upstream review. Prepare
focused app and routing-library contributions separately, documenting dependency
versions and pending prerequisites. Verify remotes and fetch parent `master` when
preparing upstream branches. Commits, pushes, PR creation and upstream merges
follow the user's authorization; this plan does not authorize publication.

## Immediate next action

Use milestone 1's probe findings to build provider contracts and source-selection
storage. Validate individual station bearings and CIOPS
vector semantics and passage performance before integrating those data into routing.
The initial probe establishes availability; it does not qualify a navigation source.
