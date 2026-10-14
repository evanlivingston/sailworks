# Qt frontend migration

The optional PySide6/Qt Widgets frontend now uses the full application Core and
shared production Cairo chart rendering. GTK remains the default application;
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

- Native Qt main window and toolbar, full-size Charts/Logs views, and a grouped
  NMEA/Routing sidebar whose panels can still float.
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
- Shared forecast-time model with a date/time control, wind-file loading, and
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
The routing form scrolls inside its Plan page on small windows; progress and
Calculate/Cancel remain outside the scrolling area. Completed routes open Results.

Chart palettes are independent of the workspace theme. This pass keeps the
production Cairo chart/wind code, geometry and source selection unchanged.
Route segments now use the same speed-color helper as GTK, and NMEA tracks use
GTK's shared track style. Plots use GTK's speed, true-wind and heading colors
with amber playback cursors. Qt-native layout/fonts still differ from GTK;
this is a visual integration pass, not complete application parity.

## Routing workflow

The Routing dock has Plan and Results pages for one complete wind-only workflow.
Open a GRIB file,
choose a local polar, set the departure time and start/destination coordinates,
then calculate. Right-click the map to set either position, use the current
boat position as the start, or choose a saved POI as the destination. Departure
is independent of the toolbar's forecast viewing time.

Calculation runs on a worker with cooperative cancellation and approximate
progress. A temporary path appears while searching. Successful routes save
through the same Core collection and JSON schema used by GTK. The result list
includes existing saved routes; selecting one shows its distance, last-point
time, map path and waypoint table. Selecting a table row highlights that point
on the map. Fit route includes both the computed path and the requested start/
destination. Cancellation, errors and closing the window preserve prior results
and discard late worker completions.

The shared job in `core/routingjob.py` uses Core's existing wind-only engine,
parameters, source ordering, spatial/time sampling and point serialization.
Each job snapshots the selected forecast files, opens private GDAL handles and
keeps two forecast hours in a run-local cache; cleanup releases them on success,
error or cancellation. A direct engine comparison produces identical route
points, and a real raster fixture matches legacy wind samples at four times.
Existing arrival tolerances are retained, so the last engine point can precede
the requested destination. The UI reports that point's time rather than claiming
an exact arrival.

This slice uses the available algorithms with their existing defaults. Algorithm
parameter editing, current-aware routes, automatic forecast/current downloads,
scenario comparisons, track destinations and route editing/export remain pending.

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

Data Library has Forecasts and Charts pages. Forecast imports validate wind
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

## Workspace preferences

Qt automatically saves its window’s normal size/position and maximized state,
sidebar placement/sizes, detached panel bounds, hidden panels/toolbar, active
sidebar and Charts/Logs view. It also remembers the map center/zoom, Routing
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

## Still to migrate

- Advanced routing controls, POI and track tools, polar panels and marine
  current/tide overlays.
- Base coastline installation/download UI and additional chart providers.
- Smooth/inertial gestures, dateline behavior, large-file performance, live
  hardware validation and distributable macOS/Linux/Windows bundles.
- A compact macOS toolbar with traffic lights in the same row. Standard native
  window buttons work; the current preview keeps the normal title bar.

The next slice can bring current-aware routing or polar management onto these
shared models/renderers. GTK stays available until Qt's workflows
and packaging reach parity.

## Validation

The full repository suite passes 776 tests and 79 subtests locally.
The optional Qt wrapper runs twenty-nine integration checks in a separate
process. Coverage includes transport/recording, queued Core notifications,
stale-source rejection, overlay completion without GLib, callback cancellation
on shutdown, real OGR ENC/raster fixtures, forecast/palette changes, projection,
polygon holes and absence of GTK imports. Tests skip Qt when it is not installed.
Core scheduler and routing-job tests also run without Qt. Routing checks cover
engine parity, forecast sampling/private handles, completion, cancellation,
shutdown, unique names, prior-result retention and on-disk save/reload.
Workspace checks verify grouped panels, view transitions, full playback curves,
retained map position, independent chart palette and reachable compact-window
Calculate/Cancel controls. Settings/library checks add disk reloads, legacy N2K
editing, offline saves, imports/toggles/removal, duplicate filenames, real vector/
raster indexing, provider failure/staging, NOAA coverage, live connection worker
responsiveness and shutdown guards. Workspace checks cover on-disk restart,
floating/hidden panels and toolbar, active views, normal/maximized bounds,
disconnected monitors, debounce during drag/playback, atomic save failure/retry
and excluding instrument history.

```sh
.venv/bin/python -m pytest
.venv/bin/python -m pytest test/test_qt_preview.py test/test_core_scheduling.py
QT_QPA_PLATFORM=offscreen QT_STYLE_OVERRIDE=Fusion \
  .venv/bin/python -m unittest test.qt_preview_checks -v
```

See `UPSTREAM.md` for full-suite results and known baseline lint/type failures.
