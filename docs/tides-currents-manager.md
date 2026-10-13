# Tides & currents manager

Open **Tides & currents** next to the GRIB manager or from the chart toolbar.
This toggles a compact palette with **Currents** and **Tides** tabs. Drag a tab
out to separate it; use the grip at the right of the tab strip to move the whole
group. Drop at the right edge of the chart workspace to dock. Floating palettes
have no native title bar and there is no Dock button. The close button hides the
group without changing layers or cancelling downloads. Reopening retains its
controls and placement for the session.

The **Currents** tab controls arrows and current-model downloads. **Coverage at
chart centre** checks enabled model data at the selected time. Green indicates
a valid sample; amber explains missing sources, spatial coverage, time coverage
or masked data. This is a point check, not coverage of an entire view or route.

The **Tides** tab controls station markers and tide-height predictions. It counts
enabled stations in view with predictions at the chart time. **Download tides
for chart area** selects CHS Pacific water-level stations in the current view,
downloads the requested interval and enables the saved predictions. No station
names are required. Requests exceeding 50 stations require a smaller view.
Saved predictions have individual Use controls and show their UTC intervals.
Tide-height acquisition currently covers CHS Pacific stations, not the globe.

Tides and currents are separate products even when supplied by the same provider:
tide heights describe water level; current vectors describe water movement.
Station water levels are not interpolated into a spatial surface. Current-model
forecasts include tidal flow where supported by the model. Individual station
predictions and the full source inspector remain available through the source
details buttons in each tab.

## Download coverage

1. Choose an existing track, saved route, or **Current map area**.
2. Set the UTC start time and coverage duration (up to seven days).
3. Choose **Download data**. Providers are selected automatically for the area.
4. Enable or disable saved models using **Use** and inspect their **Source details**.

**Start (UTC)** defaults to the chart time and follows it until edited. The
entry’s **Select now** icon sets the download start to the current UTC time. With
**Current map area**, Download uses the latest viewport. Download progress and cancellation stay visible below the scrollable
controls. The advanced window also offers a waypoint current preview.

The area includes a small margin around track waypoints. Requests currently use
its bounding rectangle rather than a narrow route corridor. Bounds are derived
automatically from the chart or selected route. Route bounds crossing the date line are split
into requests on either side. Very large worldwide subsets are rejected before
requesting data; select a smaller area or shorter time window.

The preview samples every displayed position at the same departure time. It is
not an arrival-time forecast, transit simulation or route optimization.

## Chart display

Use **Current arrows** in Currents and **Show station markers** in Tides to
control chart visibility independently.
Downloaded spatial models marked **Use** appear automatically as filled current
arrows. Arrows point in the direction water flows. Size and color increase with
speed on a fixed scale: green at zero, yellow at 1 knot, orange at 2 knots, and
red at 3 knots or more. Size continues increasing up to 5 knots. The pointer
readout shows exact speed, true bearing toward, and model name. A small dot
indicates less than 0.05 knots.

Spatial arrows are also clipped against the installed detailed GSHHS shoreline,
including arrowheads that would extend onto land. Country outlines are used if
that shoreline is unavailable. This display mask is cached during playback; it
does not change model samples or increase the model's coastal resolution.

The existing chart time controls and playback drive this layer. That clock uses
local wall time; the layer converts it to UTC for sampling. Set the clock within
the downloaded forecast interval. No valid data, expired forecasts, and masked
cells remain blank rather than showing zero flow.
The source selection and model ranking match the route/area preview, without
blending models or adding tides a second time.

The time slider below the chart controls spans the earliest to latest times in
enabled GRIBs, spatial currents, and station downloads. Drag it to scrub through
forecasts; dragging pauses playback. Its endpoints show local time and timezone.
Buttons and the time picker keep the slider in sync, and playback stops at the
end of the loaded range. The slider is disabled
until a source is enabled. This combined range can include gaps or sources for
different areas; it does not guarantee coverage at every position and time.

Enabled individual stations have amber markers at their actual positions, with
station current arrows or predicted tide heights in metres above the source's
datum, identified in the station label. Datum verification remains incomplete.
Station predictions are not interpolated over surrounding water. Tide curves and automatic
station association are not part of this display layer.

Sampling and rendering run in the background; changes to forecast time or enabled
sources request new images. During time changes within loaded coverage, the last
completed image remains visible until its replacement is ready. Source changes or
times outside coverage clear the previous image. Source details and attribution
remain available in the Tides & currents window. The map display does not change route calculations.

## Automatic sources

| Source | Acquisition and selection |
| --- | --- |
| ECCC CIOPS-SalishSea | Automatically proposed where its geographic domain overlaps the area; nominal 500 m output, hourly velocities at 0.5 m depth. |
| ECCC CIOPS-West | Automatically proposed where its domain overlaps the area; nominal 2 km output, hourly velocities at 0.5 m depth. |
| Copernicus SMOC | Worldwide baseline: ocean circulation plus the supplied tidal-current component, without the separately supplied wave Stokes drift. Approximately 1/12 degree output; the displayed 9 km resolution is nominal. Requires a free account. |

CIOPS downloads need no account. Discovery uses retained dated ECCC directories
so downloads continue across the UTC `today` rollover, and skips unpublished
cycles. Source details preserve the dated URLs for the actual forecast files.

Use **Settings → Currents → Save account** to save a Copernicus username and
password in the operating system credential store (Keychain on macOS). The
account is restored when the application starts; **Forget account** removes it.
Credentials are passed to the download process through standard input and
excluded from saved dataset metadata. Previously configured Copernicus Toolbox
credentials can also be used. The Copernicus client is included in normal
installations on Python 3.10 and newer. An existing development environment can
install it with `pip install -e '.[marine-global]'`.

The coverage list shows model, resolution and actual valid interval. Downloads
are enabled for automatic spatial selection. At a position and time, the finest
available selected field wins; newer runs take priority within that resolution.
Providers are not averaged, and overlapping current fields are not added.
Regional fields containing tides are not combined with a second tidal model.

Uncheck **Use** to override selection, or remove a selected download. Snapshots
of selected fields remain valid if settings change or downloads are removed.
Source details retain provider attribution, composition, depth, source checksums,
requested area and original metadata. Global forecast issuance is left unknown
when not established by the subset; retrieval time is recorded separately.

Failures from one provider remain visible while other usable downloads can be
saved. Inspect actual valid intervals: CIOPS requests are clipped to the selected
run's 48-hour horizon. A seven-day request does not make a 48-hour forecast valid
for seven days. Worldwide downloads are bounded to three minutes and 128 MiB;
Canadian jobs have an aggregate 256 MiB transfer limit. Cancelled jobs do not add
downloads to the cache.

## Offline sampling and gaps

The cache stores cropped normalized grids, source identities, component masks
and checksums. Reopening the manager requires no network. Corrupt cached datasets
are excluded with visible errors. Bilinear spatial interpolation and linear
component interpolation in time use only valid neighbouring cells and hourly
intervals. There is no time extrapolation and no substitution of zero for missing
current. If the preferred field touches a masked cell, sampling reports missing
data instead of using a coarser field to fill a potentially land-masked location.

These rules preserve the grid's masks; they do not establish whether a model
resolves an individual narrow passage or every coastline feature within a cell.

## Individual stations

CHS station discovery and prediction downloads remain under **Individual
stations (advanced)**. They supplement spatial forecasts and are not needed to
choose or download current coverage. Search by name/code, select a UTC date and
one to seven days, then download. Predictions, units, quality flags and event
qualifiers are available offline. Source details retain response URLs and hashes,
station metadata, attribution and terms.

Tide downloads reuse saved predictions when their usable samples cover the requested
interval, including datasets whose display is disabled. Partial coverage downloads
only missing time spans, rounded outward to include interpolation samples. Compatible
saved windows are joined without changing sample values or quality flags; changed
station metadata or units require a fresh coherent window. Concurrent panel requests
share pending results. Null samples and interpolation gaps still count as missing.

Station downloads start disabled and have their own priority and explicit
fallback controls. Station sampling never supplies an arbitrary spatial current
field. Water-level datum and station current depth remain unverified. Station
current directions are bearings toward true north; QC 2 predictions retain their
unevaluated status.

## Validation and remaining work

The production CIOPS adapter passed a live one-hour Salish Sea download on
2026-10-11, persisted/reloaded its grid, and sampled a current in the Strait of
Georgia. Its exported geographic grid, coordinate order, units, matching X/Y
clocks and forecast identity are checked. The raw CF grid mapping supplies the
geographic X/Y reference; broader physical qualification remains open.

Copernicus dataset identity and component names were checked against its live
public catalogue with Toolbox 2.5.0. Download arguments, NetCDF normalization,
component composition and credential exclusion have offline integration tests.
An authenticated live subset still needs validation with an account.

**Current-aware routing is now opt-in** in the routing dialog's Currents tab.
It uses spatial current models, checks predicted positions/times, records source
provenance and can automatically download missing current coverage. Persistent
gaps stop the calculation. Fields retain `routing_qualified=false`; independent
qualification for narrow passages is still outstanding. Tide heights do not yet
constrain depth or clearance. See [routing details and limits](current-routing.md).
