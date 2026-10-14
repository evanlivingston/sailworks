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
the same serial device or listening port in Qt. There is no Qt connection
editor yet; use the existing settings in GTK.

The extra installs PySide6 Essentials. Base package dependencies still include
PyGObject because GTK remains supported. The Qt frontend does not import GTK
or require a running GLib main loop.

## Implemented

- Native Qt main window and toolbar, split map/plots, and floating NMEA dock.
- The full Core: shared instrument/boat state, connections, NMEA session, and
  existing track, route, POI, GRIB, marine, AIS, and polar models. Position and
  speed over ground appear in the status bar.
- Native file pickers for NMEA logs and wind forecasts. Playback transport,
  automatic timestamped interpreted/raw recording files, full-log plots,
  blitted playback cursors, and retained captures on either Stop control.
- Pan, anchored wheel zoom, fit-to-track, full track and playback marker.
- Production GSHHS coastlines with the existing zoom-dependent resolution
  selection; installed OSM, CM93, vector and raster layers; asynchronous ENC
  and Canadian tile rendering; asynchronous wind portrayal from enabled GRIBs.
  Saved chart enable flags and palettes are retained. Missing saved chart
  files are logged and reported on the map while other layers remain usable.
- Shared forecast-time model with a date/time control, wind-file loading, and
  chart palette selection. Opening a forecast selects its initial time. Files
  opened here are session additions; the complete GRIB library UI is pending.

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

## Still to migrate

- Routing controls/results, POI and track tools, polar panels, marine current/tide
  overlays, settings/connection editors and workspace persistence.
- Chart discovery/download/import UI and the full GRIB library/download UI.
  This slice loads charts already configured in GTK.
- Smooth/inertial gestures, dateline behavior, large-file performance, live
  hardware validation and distributable macOS/Linux/Windows bundles.
- A compact macOS toolbar with traffic lights in the same row. Standard native
  window buttons work; the current preview keeps the normal title bar.

The next slice can move one complete user workflow, such as routing, onto these
shared models/renderers. GTK stays available until Qt's workflows and packaging
reach parity.

## Validation

The full repository suite passes 765 tests and 73 subtests locally.
The optional Qt wrapper runs twelve integration checks in a separate
process. Coverage includes transport/recording, queued Core notifications,
stale-source rejection, overlay completion without GLib, callback cancellation
on shutdown, real OGR ENC/raster fixtures, forecast/palette changes, projection,
polygon holes and absence of GTK imports. Tests skip Qt when it is not installed.
Core scheduler tests also run without Qt.

```sh
.venv/bin/python -m pytest
.venv/bin/python -m pytest test/test_qt_preview.py test/test_core_scheduling.py
QT_QPA_PLATFORM=offscreen QT_STYLE_OVERRIDE=Fusion \
  .venv/bin/python -m unittest test.qt_preview_checks -v
```

See `UPSTREAM.md` for full-suite results and known baseline lint/type failures.
