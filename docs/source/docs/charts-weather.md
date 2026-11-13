Charts and weather
==================

The chart is the main working surface in Sailworks. It draws the base map, the chart
layers you enable, your tracks and routes, and the weather fields you select. Forecasts
and chart downloads are managed from **File → Data Library…**.

```{image} ../_static/images/manual/chart-pnwo.png
:alt: Chart with the satellite basemap over the Pacific Northwest coast
:width: 640px
```

Chart layers
------------

The base map sits underneath everything else. On top of it the chart draws the enabled
chart layers, tracks, routes and the weather fields you select. Layers can be turned on
and off from the chart's display controls without removing the underlying data.

### Satellite basemap

The satellite basemap is optional. It needs `maplibre-native-ffi` with the platform
runtime described in the README, and it streams Esri World Imagery tiles, so it needs a
network connection while it is in use. When the runtime is not installed, the rest of
the chart works normally without satellite imagery.

The image above uses the satellite basemap over the Pacific Northwest. The race course
drawn on it is an illustrative display, not a live race tracking feed.

### Chart gestures

Pinch the trackpad to zoom around the pointer. Scroll with two fingers to pan the chart
horizontally or vertically; macOS scroll momentum is preserved. Control-scroll or
Command-scroll also zooms. A mouse wheel zooms as before.

Base maps and chart downloads
-----------------------------

Sailworks opens an empty chart on first run and does not prompt for downloads. To
install the base maps:

1. Open **File → Data Library…**.
2. Keep the **Charts** tab and select the **Downloads & base maps** section.
3. Press **Download World Coastlines**, then **Download OpenSeaMap** for lighthouses,
   buoys and other seamarks from the
   [OpenSeaMap project](https://openseamap.org/index.php?id=openseamap&no_cache=1).

Local chart files are added from the **Local files** section of the same tab. Each
download reports **Installed:** once complete, and the chart redraws with the new layers.

```{image} ../_static/images/manual/data-library-charts.png
:alt: Data Library, Charts tab, Downloads and base maps section
:width: 520px
```

::::{note}
Downloaded charts are not updated automatically. To refresh a chart, download it again
from the Data Library.

NOAA ENC charts cover United States waters only. CHS charts are provided for viewing
only. Check the coverage and edition of any chart you rely on before using it for
planning.
::::

Weather forecasts
-----------------

1. Open **File → Data Library…** and switch to the **Forecasts** tab, or pick
   **Open forecast…** from the File menu for a forecast file you already have.
2. Choose the weather model, the region and the time range, then start the download.
3. The forecast is loaded into the chart when the download finishes. Step through it
   with the time controls in the toolbar, and use the **Weather** panel to choose the
   displayed fields.

```{image} ../_static/images/manual/data-library-forecasts.png
:alt: Data Library, Forecasts tab
:width: 520px
```

```{image} ../_static/images/manual/panel-weather.png
:alt: Weather panel with forecast field controls
:width: 300px
```

Forecasts are predictions, not observations. A route calculated from a forecast is only
as good as that forecast: timing, wind strength and direction can all change. Treat the
result as a planning aid and keep checking the actual conditions on the water.

Next steps
----------

- [Plan a route](routing.md) from the loaded forecast.
- [Compare measured performance with your polars](polars-performance.md).
- [Record instruments and review a log](instruments-logs.md).
