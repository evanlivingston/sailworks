# Qt frontend migration

The optional PySide6/Qt Widgets frontend now uses the full application Core and
shared production Cairo chart rendering. GTK is the reference for workspace
organization and workflows; see [the parity checklist](qt-parity.md).
GTK remains the default application;
the frontends run in separate processes and never share GUI event loops.

## Try it

Use Python 3.10 or newer in the development environment:

```sh
.venv/bin/python -m pip install -e '.[qt]'
.venv/bin/gweatherrouting-qt --offline --log tools/samples/data_12_12_2021.nmea
```

The equivalent module command is:

```sh
.venv/bin/python -m gweatherrouting.qt --offline
```

`--offline` leaves instrument connections closed and disables Canadian online
chart tiles. Without it, Qt opens the saved instrument connections and honors
the Canadian charts preference. Close GTK's live connections before opening
the same serial device or listening port in Qt. Use File → Settings or the
NMEA panel’s Configure connections button to edit instruments directly in Qt.

The extra installs PySide6 Essentials. Base package dependencies still include
PyGObject because GTK remains supported. The Qt frontend does not import GTK
or require a running GLib main loop.

## Implemented

- Charts, Logs and Polars in the workspace header, with view-specific toolbars
  and a shared sidebar whose panels can tab, split, hide or float. Polars shows
  its table beside the full-circle chart; Logs shows a map above the plots.
- The full Core: shared instrument/boat state, connections, NMEA session, and
  existing track, route, POI, GRIB, marine, AIS, and polar models. Position and
  speed over ground appear in the status bar.
- Native file and folder pickers for NMEA logs, forecasts and charts. Playback transport,
  automatic timestamped interpreted/raw recording files, full-log plots,
  blitted playback cursors, and retained captures on either Stop control.
- Pan, anchored wheel zoom, fit-to-track, full track and playback marker.
- Production GSHHS coastlines with the existing zoom-dependent resolution
  selection; installed OSM, CM93, vector and raster layers; asynchronous ENC
  and Canadian tile rendering; asynchronous wind portrayal from enabled GRIBs.
  Saved chart enable flags and palettes are retained. Missing saved chart
  files are logged and reported on the map while other layers remain usable.
- Shared forecast-time model with transport controls and a timeline, wind-file loading, and
  chart palette selection. Opening a forecast imports a persistent library copy,
  enables it and selects its initial time. File → Data Library manages imported
  files, enable flags and provider downloads.

Qt renders Cairo into an owned QImage buffer at the canvas's pixel density.
The production renderers live in `rendering/`; GTK compatibility modules retain
existing imports and module identities. GTK's chart manager remains a GObject
adapter with its existing download dialogs. Settings are shared through
`core/settings.py`.

Core notifications and background overlay/raster completions accept an injected
scheduler. GTK defaults to GLib idle callbacks; Qt queues callbacks through a
QObject signal. Source resets reject stale Core position notifications. Closing
Qt cancels map jobs/timers and discards queued UI callbacks.

## Fidelity and measurements

The Mercator extraction changes no formulas or constants: the first pass
compared 2,000 coordinates across 20 center/zoom combinations with zero-pixel
differences. This slice retains production ENC scale ranking, coverage clipping,
half-pixel geometry tolerance, GSHHS quality selection, and wind source ordering
and interpolation. Routing calculations remain unchanged.

S57 PNG sprites now decode directly with Cairo rather than GdkPixbuf. A comparison
of all 2,030 day/dark sprites at fractional screen coordinates found zero changed
pixels. Raster palette conversion is unchanged; it runs on the existing loader
thread, removing a nested subprocess that cannot spawn on macOS with a local
worker function and GDAL handles. Simple coastline polygons now draw all rings
as one even-odd path so interior water stays visible, including fallback maps.

A native macOS run loaded the installed GSHHS base, 31 saved vector layers and
three wind sources, then played a synthetic 180-point log. Locally measured
cached chart image construction was 6.2 ms and ten small pans averaged 7.44 ms.
Full plot redraw was 86.4 ms; cursor updates averaged 6.32 ms. Regression checks
were running concurrently, so these numbers describe one small-sample run,
not a controlled performance comparison or a large-chart benchmark. Native
window controls, chart detail, wind, full curves, playback cursors and boat
state were visually checked; GTK was absent from the Qt process.

## Presentation

The Qt workspace now follows GTK's dark presentation: dark controls and plots,
blue accents, rounded controls, consistent spacing and the application's existing
symbolic icons. Native macOS window buttons and default filesystem dialogs are
retained. Qt uses Fusion controls with an explicit dark palette so the styling
works consistently across platforms.

Charts and Logs have separate full-size views. NMEA and Routing share a tabbed
right sidebar; panels can still float, close and reopen through View or the
toolbar. Opening a log selects Logs. Starting a routing calculation selects
Charts. View/panel switching keeps playback, plots, map position and results.
Charts also has GTK's Navigation tabs on the left. Tabs shows/hides this panel;
it hides outside Charts and retains the user's visibility choice on return.
The routing form scrolls inside its Plan page on small windows; progress and
Calculate/Cancel remain outside the scrolling area. Completed routes open Results.

Chart palettes are independent of the workspace theme. This pass keeps the
production Cairo chart/wind code, geometry and source selection unchanged.
Route segments now use the same speed-color helper as GTK, and NMEA tracks use
GTK's shared track style. Plots use GTK's speed, true-wind and heading colors
with amber playback cursors. Qt-native layout/fonts still differ from GTK;
this is a visual integration pass, not complete application parity.

## Routing workflow

The Routing dock has Plan, Results, Compare and Currents pages.
Open a GRIB file,
choose a local polar, set the departure time and start/destination coordinates,
then calculate. Right-click the map to set either position, use the current
boat position or a saved POI as the start, or choose a saved POI or track as
the destination. First track point follows the complete saved track. Departure
is independent of the toolbar's forecast viewing time.

Calculation runs on a worker with cooperative cancellation and approximate
progress. A temporary path appears while searching. Successful routes save
through the same Core collection and JSON schema used by GTK. The result list
includes existing saved routes; selecting one shows its distance, last-point
time, map path and waypoint table. Selecting a table row highlights that point
on the map. Fit route includes both the computed path and the requested start/
destination. Cancellation, errors and closing the window preserve prior results
and discard late worker completions.

The shared job in `core/routingjob.py` uses Core's existing wind and current
engines, source ordering, spatial/time sampling and point serialization.
Each job snapshots the selected forecast files, opens private GDAL handles and
keeps two forecast hours in a run-local cache; cleanup releases them on success,
error or cancellation. A direct engine comparison produces identical route
points, and a real raster fixture matches legacy wind samples at four times.
Existing arrival tolerances are retained, so the last engine point can precede
the requested destination. The UI reports that point's time rather than claiming
an exact arrival.

Plan exposes the selected algorithm's parameter names, ranges, steps and tooltips.
Each run owns its parameters, forecast handles and track coordinates; editing
controls does not mutate engine defaults. Concurrent wind queries serialize GDAL
reads while retaining the engine's calculation behavior.

Compare uses GTK's departure offset, wind intensity, wind direction and polar
efficiency axes. Enable axes and enter comma-separated values. Calculate runs
their Cartesian product sequentially, saving each completed scenario separately.
Cancel preserves completed scenarios. Invalid values and batches above 256
scenarios are rejected before starting.

Currents enables the existing current-aware engine and defaults automatic data
downloads on. It uses the same latest-wind checks, coverage warnings, bounded
recovery and prefetch as GTK. Blocked coverage cannot be overridden; other
warnings offer Run anyway or Cancel. Cancellation and window closure also dismiss
pending coverage dialogs. Download persistence and route commits happen on the UI
thread. Current results retain UTC timestamps, source provenance and coverage notes.
Display overlays remain independent of routing options; tides do not add depth
or clearance calculations.

Workspace preferences retain algorithm parameters, comparison values and current
options. Restoring them starts no calculations or downloads. Direct editing of
computed route waypoints remains separate from this routing-control migration.

## Chart navigation

The left Navigation panel preserves GTK's tab order: Tracks, Routings, POIs and
AIS. It can dock, float or close; the Charts toolbar's Tabs button and View →
Navigation reopen it. Tracks shows the named track list above its waypoint table.
Rename tracks/POIs/routings in the Name cells and use Visible checkboxes to toggle
map drawing. Selection highlights the corresponding map item.

New creates an empty active track. Add Point and Add POI accept coordinates;
right-click the map to add either at that position. Track point menus provide
Move, Move Up, Move Down, Remove and Duplicate. POI menus provide Move and Remove.
Move consumes the next left-click on the map; Escape, switching views or opening
another map menu cancels it. Fit buttons and double-clicking a track/POI/routing
fit the map to that object. Routings shows the shared saved routes, GTK's summary
and waypoint columns, with remove/export controls. The existing Routing plan and
results stay synchronized with the selected route. POI edits refresh destination
choices without changing the route draft coordinates.

Import reads GPX on a worker, validates coordinates before installation and adds
tracks/waypoints to the existing Core collections on the UI thread. Repeated
names receive unique names. As in GTK, GPX route elements are not imported.
Export offers all navigation as GPX or POIs through the existing NMEA PFEC
converter; track/routing menus also export the selected object as GPX. All file
dialogs use the default native picker. Existing track/POI/routing JSON files and
calculation models are shared with GTK.

AIS refreshes every three seconds using Core's active-target timeout. The tab
appears when targets exist; valid-position targets list MMSI/name/type/SOG/COG
and distance, ordered by GTK's haversine distance from the boat. Double-click a
target to center it. The AIS toolbar switch controls map symbols and is saved.
The table and both map views use the same target set; no live hardware was used
for validation.

GTK and Qt share production Cairo track, POI, routing and AIS drawing, including
selected styles, speed colors, route-time interpolation, current-exclusion marks,
AIS orientation/vectors and label-spacing rules. Twelve comparisons against the
pre-extraction GTK code at three sizes, two pixel scales and with/without
selection produced zero changed pixels. Qt retains its selected-route-waypoint
marker and route-plan/search overlays.

## Settings and data management

File → Settings includes connection add/edit/remove and display preferences.
Connection edits remain drafts until Apply connections. Serial and TCP/UDP
transports support NMEA 0183 and the existing NMEA 2000 gateway formats; legacy
format identifiers load into the protocol/format controls. NMEA 2000 remains
input-only. Duplicate endpoints and invalid fields are rejected before stopping
instruments. Applying saves the shared connection configuration and reconnects
active instruments in a worker. Offline mode saves without opening transports.
Window shutdown waits for connection lifecycle work before closing instruments.
Status polling uses a cached snapshot while a transport is reconnecting.

Data Library manages Forecasts and Charts. Polar management lives in the
shared dock, reached from the Polars workspace toolbar. Forecast imports validate wind
metadata before installing a persistent copy; duplicate filenames receive a
timestamp suffix. Enable flags use the same GRIB storage as GTK. Removal deletes
only the library copy after confirmation; externally opened files are protected.
NOAA GFS downloads use explicit south/west/north/east bounds and the existing
model-run/duration choices. OpenSkiron is also available. Lists and downloads
run in workers; provider files are staged before importing, so a failed download
does not overwrite an installed forecast. Forecast times follow the existing
local-time model; NOAA remote run labels explicitly show UTC.

Charts support vector files, CM93 folders and raster folders. Raster registration
indexes palette-based KAP/BSB/TIFF/VRT charts, rejects rasters the existing
renderer cannot display and skips unrelated sidecars. Enable flags
and metadata persist in the settings used by GTK. Removing an imported chart
retains its source files. Base layers are read-only in this library. NOAA ENC
discovery searches coverage at a coordinate, with a Use map center shortcut;
downloads use the existing catalog/cache and atomic exchange-set installation.
Worker-local chart settings attach to live palette callbacks only on UI delivery.
Library mutations and routing calculations are serialized to retain a consistent
forecast/coastline selection. Closing the application drops late data results.

Live provider responses and physical instrument hardware were not exercised in
this slice. Integration checks use deterministic provider downloads plus real
GDAL files and shared on-disk storage. Provider timeout/cancellation behavior is
retained: there is no new in-app download Cancel control. Qt still requires
installed GSHHS data for that coastline layer; the bundled fallback and NOAA
chart imports work without opening GTK.

## Polar management

Open the top-level Polars tab or File → Polars to see the table and chart
together. Polar selector changes the displayed enabled polar. Use the Polar
manager toolbar button (also reachable from Routing → Manage polars) for imports,
enable/remove controls and ORC downloads. It is a shared dock panel, matching GTK;
`.pol` imports still use the native picker and removal asks confirmation. Imports validate axes/table shape and finite, nonnegative
speeds before staging a copy. Duplicate filenames receive timestamp suffixes;
source files and existing installed copies are retained. The main table uses GTK’s row stride and colored wind headers. The optional
manager Preview shows every stored wind-angle/speed value. Use for routing
selects an enabled polar in the Routing plan without starting a calculation.

Load catalog fetches the existing ORC data source on demand. Search filters sail
identifier, boat name and type with Qt’s proxy model. Select an available boat
and Download selected to convert/install it and retain its metadata. Network work
runs in a worker; manager updates and Qt notifications run on the application
thread. Already installed ORC boats are marked and cannot be downloaded again.
Provider errors appear in the status label. Requests have connection/read timeouts;
there is no new download Cancel control. Catalog/download tests use deterministic
responses; live ORC service availability has not been verified in this slice.

The shared manager now separates installed files from enabled polars. Repeated
refreshes no longer duplicate entries or enable all installed files; an explicitly
empty enabled list survives restart. Deletes remove enabled state and metadata,
while a failed save restores the staged file. Qt routing choices now use only
enabled polars and refresh immediately, preserving an available selection. Disabling
or deleting the selected polar falls back to another enabled entry, or disables
Calculate when none remain. Polar/data mutations and route jobs are serialized.

Imports keep their original bytes and use the existing routing engine parser and
interpolation. The main full-circle chart uses GTK’s production Cairo renderer, including
mirrored tack curves, speed rings, wind colors, hover details and performance
points. Extracting this renderer produced zero changed pixels in six baseline
comparisons across three sizes, with and without hover and measured samples. ORC
conversion retains the shared converter’s rounding/endpoint behavior; malformed
or degenerate converted tables are rejected. The enabled-file selection fix also
applies to GTK through the shared manager. The Performance dock follows GTK: show actual performance, STW/SOG basis,
nearest-TWS wind filter, sample count and Clear samples. It reads the same active
NMEA history and wind-band policy; clearing observations retains logs/plots.
Efficiency controls and polar editing/export remain separate migration work.

## Map instruments and cursor wind

Charts and Logs show GTK's instrument dashboard at the bottom of their maps:
SOG, COG, AWS, AWA, TWS, TWA and depth, with connection status above. The Dashboard
toolbar switch hides/shows it. Each map remembers its own Dashboard and Grib
visibility choices. Polars hides these map-specific toolbar controls.

The dashboard uses the shared Core boat state at the current playback position
or from live connections. Stop retains the current readings; a source reset clears
them. Reported true wind takes precedence. When TWS is missing, the original GTK
apparent-wind/SOG fallback supplies TWS/TWA without modifying recorded data.
Unknown measurements display `---`, and valid zero measurements remain zero.
Connection summaries use the existing nonblocking status snapshot, including
packet counts and disconnected-source details.

The production GTK Cairo gauge styles, units, colors and formulas are shared.
Sixteen comparisons with pre-extraction GTK at two sizes, two pixel scales and
four instrument/connection states produced zero changed pixels on wide maps.
The same gauges now wrap in their original order on narrow maps in both
frontends; this fixes the clipped row when sidebars are open.

Move the pointer over a map to see its coordinates in the status bar. With Grib
enabled and forecast coverage available, a stationary pointer also shows wind
direction and speed in knots for that map's forecast time. Queries debounce for
120 ms and run on a worker using private GDAL handles. Enabled-file order,
coverage fallback, sampling and the `1.94384` knots conversion match GTK's cursor
sampler. This preserves the existing cursor priority; it does not change the
separate wind overlay's newest-run ordering.

Dragging, zooming, placing a point, leaving/hiding the map, changing time/sources
or hiding Grib cancels the readout. Late results cannot replace a newer position
or a different view. Forecast data never loads on the pointer event thread.
The worker keeps at most two private files and four array hours per file; file
eviction and shutdown explicitly release those handles. Twelve real-raster
time/position probes match legacy wind values exactly. Connection status refresh
does not hide an active cursor readout; instrument updates reuse the chart image.

## Workspace preferences

Qt automatically saves its window’s normal size/position and maximized state,
sidebar placement/sizes, detached panel bounds, hidden panels/toolbar, active
sidebar and Charts/Logs/Polars view. It also remembers the map center/zoom, Routing
Plan/Results page, polar/algorithm/coastline options, selected saved route and
NMEA playback speed/live-stream preference. View → Reset layout restores the
default grouped sidebar and toolbar; the View menu can reopen hidden panels
and the Navigation toolbar individually.

Preferences use `workspace-qt.json` in the application data directory and the
existing versioned atomic workspace store. GTK keeps its own `workspace.json`.
Changes save after 750 ms of idle time; held map drags/zoom animations postpone
writes, and playback cursor updates do not schedule saves. Close and application
quit flush pending preferences before session shutdown. Identical states are
not rewritten. Failed writes preserve the previous file, report the error and
can retry on the next change or close.

Normal and detached window bounds are fitted to available monitor work areas
on startup, including after disconnecting a display. Corrupt/unknown state falls
back to defaults. Maximization follows the initial native window show so restoring
to normal size keeps the saved dimensions. Qt dock layout bytes are bounded and
validated; a rejected layout restores the default arrangement.

Workspace preferences do not retain log filenames, samples, playback cursors,
recording state or running calculations. A selected saved route is only a name;
the route collection still owns its data. Route draft coordinates and departure
are initialized normally rather than restoring an old departure time. No log,
recording, playback or calculation starts because a workspace was restored.

## Forecast timeline and panels

Charts places forecast transport and the range slider at the right of its context
toolbar, alongside the chart controls. The controls follow GTK's order: step
backward, pause, play, step forward, Now and Time Picker. Choose the step interval
from 30 seconds through 24 hours (15 minutes by default). Playback takes one step
per second and pauses at the last available time; repeated Play keeps one timer.

The slider spans the earliest start through the latest end of enabled wind,
marine field and station metadata. Gaps between sources can still have no data.
Dragging pauses forecast playback and coalesces changes at 75 milliseconds;
release commits the final selection. No data is sampled to find these bounds.
Times use the local timezone. Manual dates outside the range remain selected with
an explanatory caption; the next step clamps into the range. Without a loaded
range, stepping and the time picker still work while scrubbing is disabled. Now
retains GTK's existing rounding to the hour.

Forecast controls are independent of NMEA recording/playback and routing departure.
Changing workspace pauses forecast playback. Workspace preferences retain only
the step interval; opening the app never resumes forecast playback or restores an
old selected time. Marine bounds and the overlays below use this same clock.

Floating tool panels use compact dark headers with a left close button. Drag the
header to move, double-click to dock, resize from the edges, or drag to a screen
edge to preview a snap. Window lists the panels and offers Reset Panels to redock
them and restore the default layout. Panel scrolling uses thin bars that appear
briefly during activity; scrolling over an unfocused dropdown/spinner scrolls its
panel instead of changing its value. Native filesystem dialogs remain enabled.

## Currents and tides

On Charts, **Tides & currents** opens the separate Currents and Tides panels,
matching GTK's organization. They also appear in the Window menu. They share the
existing marine cache and source policies, and hide while viewing Logs or Polars.
Their open state and independent display switches survive restart. Restoring a
workspace never starts a download. Reset Panels closes both marine panels.

Currents offers **Current arrows**, coverage at the chart center, **Coverage for**
(current map area or a saved track/route), **Start (UTC)**, Hours and Download data.
The start follows chart time until edited or Now is selected. Requests freeze the
selected area and interval before the worker starts. Progress and Cancel stay
outside the scrolling controls. Provider issues remain visible when another
provider succeeds; cancellation or closing the application discards late results.

Saved models are grouped by provider, model and resolution, with merged valid
intervals. Expand a model to enable or remove individual downloads; a mixed group
has a partial checkbox. Display switches do not disable downloaded sources for
other uses. Model sampling keeps the existing finest-resolution/newest-run order,
masked cells and missing-data behavior.

Tides offers **Show station markers** and the same area/route and UTC download
controls, plus saved station predictions and their height at chart time. Automatic
station acquisition remains CHS Canadian Pacific coverage. Saved predictions are
reused where usable samples cover the request; partial windows download only the
missing spans. New automatic downloads are enabled, as in GTK.

The gear button opens **Marine settings**. Currents provides the full model list
and source metadata. Tides provides saved station sources, Use, priority up/down,
explicit fallback, removal, UTC samples, units, QC/qualifiers and provenance.
**Settings → Currents** saves or forgets a Copernicus account using the existing
system credential store. Password drafts clear on save and hide; credentials are
excluded from workspace and downloaded metadata. Provider behavior and account
requirements are unchanged; see [Tides & currents](tides-currents-manager.md).

Chart arrows and station labels reuse GTK's production Cairo renderer, including
the fixed knots color/size scale, true flow-toward bearings, detailed shoreline
clipping and source-datum tide labels. Current speed, bearing and model appear
under the pointer. Rendering uses a worker and the existing bounded overlay cache;
time/source changes invalidate stale frames. Logs retains its independent NMEA
clock and does not gain a marine overlay. These display controls do not enable
current-aware routing or use tides for depth/clearance calculations.

## Base map setup

Settings → Map Data shows installed/missing world coastlines and optional
OpenSeaMap sea marks, using the same sources and data locations as GTK. An online
startup with missing data opens this page; offline startup does not prompt.
Downloads begin only when Download selected is clicked. Installed data remains
available offline.

Downloads run on a worker with progress, Cancel download and retry after failure.
Files are staged and checked using the production shapefile/OSM readers before
installation. Invalid or interrupted downloads preserve existing files. Completed
datasets remain available if a later download fails or is cancelled. Closing
Settings keeps the operation running; closing the application requests cancellation
and ignores late chart attachment. Network reads have bounded timeouts.

Installed coastlines replace the bundled fallback map, become available for
routing land checks, and refresh marine shoreline masks without an application
restart. User chart selections and existing base-layer objects are retained.
Map-data installation is serialized with routing and Data Library/Polar Manager
operations. Corrupt/incomplete base data falls back to the bundled map and can be
retried from Map Data. The native filesystem pickers are unchanged.

## Still to migrate

- Remaining general settings and computed-route waypoint editing;
  see the parity checklist.
- Additional chart providers.
- Smooth/inertial gestures, dateline behavior, large-file performance, live
  hardware validation and distributable macOS/Linux/Windows bundles.
- A compact macOS toolbar with traffic lights in the same row. Standard native
  window buttons work; the current preview keeps the normal title bar.

Subsequent slices should follow GTK’s existing organization for the remaining
controls instead of inventing a different Qt layout. GTK stays available until Qt's workflows
and packaging reach parity.

## Validation

The full repository suite passes 822 tests and 106 subtests locally.
The optional Qt wrapper runs seventy-eight integration checks in a separate
process. Coverage includes transport/recording, queued Core notifications,
stale-source rejection, overlay completion without GLib, callback cancellation
on shutdown, real OGR ENC/raster fixtures, forecast/palette changes, projection,
polygon holes and absence of GTK imports. Tests skip Qt when it is not installed.
Map setup adds ten core checks for streamed downloads, cancellation, HTTP errors,
truncation, archive validation, atomic replacement/rollback and partial completion;
five Qt checks exercise startup/selection, real shapefile and OSM-reader installs,
fallback removal, routing attachment, mask refresh, repair/idempotence, preserved
chart selections, serialization and cancel/close. Provider HEAD requests return
HTTP 200 for both existing URLs; full live datasets were not downloaded. Native
macOS Qt Settings was checked with temporary data and disabled settings writes.
Core scheduler and routing-job tests also run without Qt. Routing checks cover
engine parity, forecast sampling/private handles, completion, cancellation,
shutdown, unique names, prior-result retention and on-disk save/reload. Advanced
checks compare exact points with the original wind/current engines, validate
scenario products and parameter isolation, exercise track/POI starts and frozen
inputs, and cover preflight cancel/close, UI-thread download commits, partial
batch cancellation and options restoration. Live routing-provider downloads have
not been exercised in this slice.
Workspace checks verify grouped panels, view transitions, full playback curves,
retained map position, independent chart palette and reachable compact-window
Calculate/Cancel controls. Settings/library checks add disk reloads, legacy N2K
editing, offline saves, imports/toggles/removal, duplicate filenames, real vector/
raster indexing, provider failure/staging, NOAA coverage, live connection worker
responsiveness and shutdown guards. Workspace checks cover on-disk restart,
floating/hidden panels and toolbar, active views, normal/maximized bounds,
disconnected monitors, debounce during drag/playback, atomic save failure/retry
and excluding instrument history. Polar checks cover native-picker import,
shared disk reloads and enable flags, routing selection/removal, exact preview
values, ORC filtering/download metadata, provider failures and late completion.
Shared manager tests cover refresh/reload, collisions, interpolation parity,
invalid data, failed-copy/delete recovery and retaining symlink targets. Forecast
checks cover enabled-source bounds beyond 2038, step/play/Now/manual dates,
coalesced dragging, NMEA independence, step preferences and shutdown. Shared
timeline tests retain GTK bounds, endpoints and DST stepping semantics. Panel
checks cover repeated float/redock, reset, minimum size and child-relative resize
coordinates. Native macOS checks compare GTK/Qt at 1280×820 and verify Qt at
1000×650, manual dates, floating panels and clean shutdown.

Marine checks cover grouped source toggles/removal, coverage and tide values,
source priority/fallback/provenance, UI-thread download commits, frozen requests,
cancellation/close guards, dateline routes, saved prediction reuse, independent
display preferences, UTC start behavior, system account controls and worker/cursor
integration. Native macOS GTK/Qt comparison checked both panels and marine portrayal
at 1280×820, plus Qt at 1000×650, source details, floating panels and shutdown.
Sixteen direct image comparisons against the pre-migration GTK renderer at two
pixel scales and four times, with valid/masked fields, have zero changed pixels.

```sh
.venv/bin/python -m pytest
.venv/bin/python -m pytest test/test_qt_preview.py test/test_core_scheduling.py
QT_QPA_PLATFORM=offscreen QT_STYLE_OVERRIDE=Fusion \
  .venv/bin/python -m unittest test.qt_preview_checks -v
```

See `UPSTREAM.md` for full-suite results and known baseline lint/type failures.

Workspace parity checks additionally cover the primary Polars table/full-circle
chart, contextual toolbars, shared performance basis/bands, Logs map/plot layout
and absolute/relative wind switches, local-time tick labels, playback retention,
Polars/Logs preference restart and migrating the older Qt toolbar arrangement.
Native macOS comparison used both current GTK and Qt at 1280×820; Qt was also
checked at 1000×650. OS chrome and fonts differ, and unmigrated controls remain
explicitly recorded in `qt-parity.md`.

Navigation checks cover track/POI creation, rename/visibility, map placement and
cancellation, point ordering, shared collection disk reloads, GPX/native-picker
import/export, invalid coordinates, route selection/visibility/removal, AIS
distance ordering/expiry, workspace restart and late import shutdown. Native
GTK/Qt comparison also inspected Tracks, Routings, POIs and AIS; user-settings
writes were disabled and navigation/workspace QA used temporary or in-memory data.
