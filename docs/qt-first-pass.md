# Qt frontend: first migration pass

This is an optional, working PySide6/Qt Widgets experiment. GTK remains the
normal application. The Qt preview launches as a separate frontend and does
not embed Qt widgets inside GTK or run both GUI event loops together.

## Try it

Use Python 3.10 or newer in the existing development environment:

```sh
.venv/bin/python -m pip install -e '.[qt]'
.venv/bin/gweatherrouting-qt --offline --log tools/samples/data_12_12_2021.nmea
```

The equivalent module command is:

```sh
.venv/bin/python -m gweatherrouting.qt --offline
```

`--offline` leaves instrument connections closed. Without it, the preview
opens the existing saved NMEA connections. Close the GTK application's live
connections before using the same serial device or listening port in Qt.
There is no Qt connection editor yet; use the existing settings in GTK.

The extra installs PySide6 Essentials, rather than the WebEngine/PDF modules.
The current project's base dependencies still include PyGObject: this pass
has not redesigned installation or removed GTK from the default package.
The Qt frontend itself does not import GTK.

## Implemented

- A native Qt main window, toolbar, split map/plot view, and an NMEA dock that
  can float, close, and reopen through the View menu.
- The existing `NMEASession`, log reader, connection manager, wind translation,
  performance ingestion, and persistent recording implementation.
- Live/source controls, asynchronous file loading, Play/Pause/Stop/Restart,
  playback speed only for loaded logs, and explicit recording status/path.
- Automatic timestamped interpreted/raw recording files; neither Stop nor
  closing the preview discards captures. No Save or Clear action.
- Full-log Matplotlib plots in a native Qt canvas. Animated playback cursors
  use blitting, so cursor updates retain the full curves and fixed axes.
- A Qt map widget with pan, pointer-anchored wheel zoom, fit-to-track, the
  complete track, and a playback position marker.
- The exact Mercator projection extracted from the GTK canvas into
  `core/mapviewport.py`; the GTK import paths continue to re-export the same
  `MapPoint` and `MapViewport` types.
- A Cairo-to-QImage bridge with owned image buffers and device-pixel scaling.
  The map uses the installed `gshhs/GSHHS_shp/l/GSHHS_l_L1.shp` coastline and
  the existing shoreline geometry repair/clip cache. If that dataset is
  absent, it displays the track against a plain background without downloads.

## What this establishes

Ordinary controls, docking, NMEA playback/recording, and plotting can move to
Qt while retaining their existing Python models. Qt signals provide the
worker-to-UI handoff; a Qt timer drives the existing playback clock. There is
no need to rewrite the NMEA parser, raw archive format, or playback timing.

The projection extraction changes no formulas or constants. A comparison
against the pre-extraction GTK implementation used 2,000 coordinates across
20 center-latitude/zoom combinations and found a maximum difference of zero
pixels. Existing GTK map interaction, overlay, and chart tests still pass.

Cairo images can be displayed correctly in Qt without rewriting all drawing
in QPainter. However, this preview's coastline renderer is deliberately small:
it does not establish parity with the production chart stack. It uses a fixed
low-resolution shoreline source rather than GTK's zoom-dependent selection.
The new Qt plots/map currently display every translated track point; the
large-log rendering workload has not been optimized or benchmarked.

A native macOS run with a synthetic 180-point/540-sentence log measured about
40.3 ms for a full plot redraw, 1.84 ms per cursor update, and 2.9 ms for a
coastline image with cached repaired geometry. These are local, small-sample
measurements, not a promise for large logs or detailed ENC charts.

## Still to migrate or investigate

- Detailed S57/ENC portrayal, raster/CHS imagery, wind/current/tide overlays,
  chart discovery/downloads, and production background-rendering scheduling.
- Routing controls and results, POIs, track editing/export, polar tools,
  settings editors, and persistence of the Qt workspace.
- The full `Core` still schedules boat notifications through GLib when it is
  installed. This preview uses its toolkit-independent NMEA session directly;
  moving the entire application needs an explicit scheduler adapter for Core.
- Qt's standard native macOS window buttons work. A fully compact toolbar with
  traffic lights in the same row needs further macOS layout work; the preview
  currently keeps the standard title bar. Changing toolkits alone does not
  finish that design.
- Smooth/inertial map gestures, dateline behavior, large-file performance,
  live hardware validation, and distributable macOS/Linux/Windows bundles.

The next useful technical slice is to adapt one production Cairo chart
renderer to the Qt map and measure it while panning. That will provide a better
estimate of the difficult rendering work than replacing more ordinary forms.

## Validation

The full repository suite passes 762 tests and 71 subtests locally. The Qt
integration wrapper runs seven checks in an isolated offscreen/Fusion process:
playback/full plots, recording and shutdown persistence, queued worker data,
failed asynchronous loads, projection/pan/image ownership, coastline polygon
holes, and absence of GTK imports. Tests skip this optional frontend when Qt
is not installed.

```sh
.venv/bin/python -m pytest test/test_qt_preview.py
QT_QPA_PLATFORM=offscreen QT_STYLE_OVERRIDE=Fusion \
  .venv/bin/python -m unittest test.qt_preview_checks -v
```

Changed-file Black, Flake8, and scoped mypy pass. Editable installation and a
wheel build include the Qt package, shared projection, optional dependency,
and separate launcher. Native macOS screenshots verified readable controls,
full curves, cursors, track, coastline, and system window buttons. Existing
baseline lint/type failures are recorded in `UPSTREAM.md`; this experiment
has not mixed unrelated repairs into the migration.
