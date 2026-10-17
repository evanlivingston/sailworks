# GWeatherRouting

![CI Status](https://github.com/dakk/gweatherrouting/actions/workflows/ci.yaml/badge.svg)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg?logo=gnu&logoColor=white)](https://www.gnu.org/licenses/gpl-3.0)
[![GitHub Release](https://img.shields.io/github/v/release/dakk/gweatherrouting?logo=github&color=orange)](https://github.com/dakk/gweatherrouting/releases/latest)

GWeatherRouting is an open-source sailing routing and navigation software written using Python and Qt.

![GWeatherRouting](https://github.com/dakk/gweatherrouting/raw/master/docs/source/_static/images/gweatherrouting_levante.png)

![Polars](https://github.com/dakk/gweatherrouting/raw/master/docs/source/_static/images/gww_polars.png)

![Log analysis](https://github.com/dakk/gweatherrouting/raw/master/docs/source/_static/images/loganalysis.png)

## Features

- Multi-point sailing weather routing
- Boat / polar integrated database
- Grib1 and Grib2 support, with downloader for OpenSkiron and NOAA
- GPX support for importing and exporting POIs and tracks
- Session manager for storing POIs, tracks and routings
- Chart rendering: 
    - GeoJSON (coastlines included)
    - GSHHS basemap (download helper included)
    - KAP, GeoTiff charts and other raster formats supported by GDAL
    - OpenSeaMap seamarks rendering
    - Canadian CHS chart imagery loads automatically as you browse Canadian waters.
      Background PNG tile loading, Retina resolution, zoom previews, short
      crossfades between detail levels, and a local
      offline cache keep map interaction responsive. Toggle it under Settings →
      Charts → Canadian charts (CHS). Cached tiles refresh after 14 days when online;
      previously cached imagery stays available offline. This CHS viewing service
      is not for navigation and does not supply depths to the routing engine.
    - Zoom-aware ENC selection: overlapping charts use the compilation scale closest
      to the current zoom, with other downloaded charts filling coverage gaps.
    - On-demand NOAA ENC downloads: pause the pointer to check for a chart better
      suited to the current zoom, even where another chart is already displayed.
      The hover card offers to download it or use an existing disabled/cached copy.
      Charts load immediately and remain available offline. Basic ENC rendering
      includes depth soundings in meters, depth contours, and shallow-water shading.
      Download discovery currently covers NOAA's U.S. chart catalog. Downloaded
      charts are snapshots; automatic chart updates and depth-aware routing are
      not yet implemented.
- Qt Widgets interface
- Display settings for smooth, cursor-centered zoom and gentle pan inertia;
  both can be switched off independently and apply immediately.
- Wind overlays crop GRIB samples to the view and render in the background;
  cached images keep panning and zooming responsive while detail refreshes.
- NMEA instruments support
- NMEA / GPX log analysys
- AIS rendering
- Boat dashboard and other realtime navigation support
- Customizable routing calculator



## Installation

Install this fork from source. The former GTK-only portable build recipes have been retired.

### From source

1. Make sure to have Python >= 3.10 installed and the `pip` package manager.
2. Install the following dependencies using your OS package manager (e.g: archlinux=pacman, debian=apt, macos=homebrew). Be aware that some packages have different names with different package managers.
   - GDAL and its Python bindings (built against the same native GDAL version)
   - Cairo (used by the shared chart renderer)

3. Checkout the repository and run: ```pip install .```
4. Start the software running: ```gweatherrouting```

### macOS development setup

On macOS, install [Homebrew](https://brew.sh/) and run the setup script from the repository root:

```bash
make setup-macos
source .venv/bin/activate
gweatherrouting
```

The script installs the native GDAL dependency with Homebrew,
creates a local `.venv`, and installs GWeatherRouting and its Python dependencies. To install the
Homebrew dependencies separately, run `brew bundle --file=tools/macos/Brewfile`.

### Launching the application

Qt is the default for both the installed command and module launch:

```sh
gweatherrouting --offline
python -m gweatherrouting --offline
```

Omit `--offline` to open saved instrument connections. `--log FILE` opens an
interpreted NMEA log. `gweatherrouting-qt` remains an alias; the old `[qt]` install
extra is compatible but no longer needed because PySide6 is a base dependency.

See [default-launcher validation](docs/qt-default-validation.md) for platform and hardware validation notes.

## License

Read the LICENSE file.
