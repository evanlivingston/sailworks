Quickstart
==========

This first session gets Sailworks open, shows charts and a forecast, and points you to
the rest of the manual. Sailworks is a planning and analysis aid, not a primary
navigation system: it helps you route through forecast weather, bring boat instruments
onto the chart, and review recorded sailing against predicted boat performance. Check
every plan against official charts and your own navigation.

```{image} ../_static/images/sailworks_levante.png
:alt: The Sailworks main window
:width: 720px
```

Install and launch
------------------

Install this fork from source following the
[README](https://github.com/evanlivingston/sailworks#installation). On macOS run
`make setup-macos` once to install the system dependencies, then start the application
with `sailworks`. The retired GTK build and its Flathub and AppImage packages are not part
of this fork.

First look at the chart
-----------------------

Sailworks opens an empty chart and does not prompt for downloads. To install the base
maps, open **File → Data Library…**, keep the **Charts** tab, select the
**Downloads & base maps** section, and press **Download World Coastlines**, then
**Download OpenSeaMap**. Each download reports **Installed:** when complete.

Pinch the trackpad to zoom around the pointer. Scroll with two fingers to pan the chart
horizontally or vertically; macOS scroll momentum is preserved. Control-scroll or
Command-scroll also zooms, and a mouse wheel zooms as before.

Load a forecast
---------------

Open **File → Data Library…** and switch to the **Forecasts** tab, or pick
**Open forecast…** from the File menu for a forecast file you already have. Choose the
weather model, the region and the time range, then start the download. When it finishes,
step through the forecast with the toolbar time controls and choose the displayed fields
in the **Weather** panel.

Next tasks
----------

Each task has its own page with the full detail:

- [Charts and weather](charts-weather.md) — chart layers, the satellite basemap, chart
  downloads, and forecast controls.
- [Routing and navigation](routing.md) — tracks, points of interest, GPX, and calculating
  a route from a forecast and a polar.
- [Polars and performance](polars-performance.md) — importing and selecting polars, and
  comparing measured speed with predicted boat speed.
- [Instruments and logs](instruments-logs.md) — recording NMEA 2000 data and replaying
  saved logs.
- [Race day](race-day.md) — how the pieces fit together for a race, and what is not
  available yet.
- [Workspace and panels](workspace.md) — what the app restores between launches and how
  to arrange panels.

Recording NMEA 2000 instruments
-------------------------------

Sailworks reads live instrument data from supported gateways and records it for review;
gateway setup, wiring, supported PGNs and the no-boat simulator are described in
{ref}`Recording NMEA 2000 instruments <recording-nmea-2000-instruments>`.
