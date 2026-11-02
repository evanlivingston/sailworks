# Sailworks

![CI Status](https://github.com/evanlivingston/sailworks/actions/workflows/ci.yaml/badge.svg)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg?logo=gnu&logoColor=white)](https://www.gnu.org/licenses/gpl-3.0)

Sailworks plans sailing routes through real weather — on your own desktop, with your own
charts, your own polars, and your own instrument feed.

Pick a boat, load a forecast, set where you want to go, and Sailworks works out when to
leave and which way to go. Then watch it run on a live chart next to your instruments,
analysis tools and log replays.

It is a fork of [GWeatherRouting](https://github.com/dakk/gweatherrouting) by Davide Gessa,
built and extended as Sailworks since. See [NOTICE](NOTICE) for attribution and licensing.

![Routing tracks over a wind forecast in Sailworks](docs/source/_static/images/readme-routing.png)

*Explore routes and race tracks alongside weather forecasts.*

![Recorded boat track with speed and wind graphs in the log viewer](docs/source/_static/images/readme-logs.png)

*Review a recorded sail with track and instrument graphs.*

![Boat polar curves and recorded sailing data plotted by wind angle](docs/source/_static/images/readme-polars.png)

*Compare boat polars with recorded sailing data.*

## What it does

**Routing**
- Multi-point weather routing with configurable routing calculators
- Current-aware routing using Copernicus Marine, NOS OFS and ROMS current models
- Boat polar library with a built-in editor — import your own, or start from the bundled
  polars (Class40, Mini 650, First 317, Bavaria 38, CAT 20 and more)
- Route through a forecast, compare options, and keep the results in a session

**Weather**
- GRIB1 and GRIB2 support
- Downloader for NOAA models (GFS, HRRR, GFS wave, AIGFS), ECCC RDPS and ECMWF
- GPU-accelerated wind overlays that stay responsive while you pan, zoom and scrub time
- Forecast timeline with per-model time ranges, so marine and wind data do not fight

**Charts**
- GSHHS world basemap with a download helper, and GeoJSON coastlines included
- KAP, GeoTIFF and any other raster format GDAL can read
- NOAA ENC with zoom-aware selection: the chart whose scale best matches your zoom owns the
  screen, and other charts fill the gaps
- On-demand ENC download from the hover card, so you can fetch the next scale while sailing
- OpenSeaMap seamarks, and Canadian CHS chart imagery that loads as you browse Canadian waters
- Offline caches for charts, ENC and imagery

**Navigation and analysis**
- NMEA instrument support (serial and network sources), configurable in Settings → Connections
- AIS target rendering
- Realtime boat dashboard
- Session manager for POIs, tracks and routings, with GPX import and export
- NMEA and GPX log analysis, with a log library to browse and replay recorded sessions

**Desktop**
- Native Qt Widgets interface on macOS, Linux and Windows
- Smooth, cursor-centred zoom and gentle pan inertia, each switchable independently

## What it does not do

Sailworks is a planning and analysis aid, **not a primary navigation system**. Use official,
up-to-date charts and your own seamanship for navigation.

- ENC downloading currently covers NOAA's U.S. catalog; downloaded charts are snapshots
- CHS imagery is a viewing service — it does not supply depths to the routing engine
- Depth-aware routing and automatic chart updates are not implemented yet

## Installation

Sailworks runs from source: Python 3.11 or newer, the GDAL Python bindings built against a
matching native GDAL, and Cairo for the chart renderer.

Install those with your OS package manager first (Homebrew, apt, pacman — package names vary),
then:

```bash
git clone https://github.com/evanlivingston/sailworks.git
cd sailworks
pip install .
sailworks
```

### macOS

Homebrew and the repository's setup script do the whole thing:

```bash
make setup-macos
source .venv/bin/activate
sailworks
```

The script installs GDAL, creates a local `.venv`, and installs Sailworks and its Python
dependencies. To install only the Homebrew dependencies, use
`brew bundle --file=tools/macos/Brewfile`.

### Launching

Qt is the default for both the installed command and module launch:

```sh
sailworks
python -m sailworks
```

`sailworks-qt` remains an alias, and the old `[qt]` install extra still works but is no longer
needed — PySide6 is a base dependency.

Useful options:

- `--log FILE` opens an interpreted NMEA log
- `--early-weather` stages chart initialisation after weather preparation; the default
  startup is unchanged. See [early-weather startup](docs/early-weather-startup.md) for limits.

## Getting started

1. Open **Settings → Connections** and add an instrument source, or load a recorded log from
   the **Log library**.
2. Choose or import your boat polar.
3. Download a forecast from the weather panel, or load a GRIB file you already have.
4. Place your start and destination, add any waypoints you want to force, and run the router.
5. Step through departure times and compare routes; save the ones worth keeping in a session.

Choose **Live data source** in the NMEA panel to read from your instruments, or **Loaded log**
to replay one.

## Development

Run the test suite and linters with tox:

```bash
tox
```

The project also ships a rendering performance gate runner, `tools/gate_runner.py`, which
evaluates one rendering change with one command and exits non-zero on failure so CI can use
it:

```sh
python tools/gate_runner.py --list
python tools/gate_runner.py --gate residency --snapshot <snapshot> --output <dir>
python tools/gate_runner.py --gate validation --snapshot <snapshot> --output <dir> --full
```

Each gate prints one summary line, writes `<dir>/<gate>.result.json` next to the raw tool
report, and returns one of four verdicts: `PASS` (exit 0), `PASS-WITH-OPEN-CHECKS` (exit 0),
`FAIL` (exit 1, a ratified threshold was violated) or `NOT-EVALUATED` (exit 2, the gate could
not run — never confused with a failure). The tools need PySide6, NumPy, Cairo and GDAL; if
the current interpreter lacks them the runner re-executes itself under the project
virtualenv, and reports `NOT-EVALUATED` with the interpreter to use if it cannot.

The architecture these gates protect is in
[`docs/adr-001-grib-rendering-architecture.md`](docs/adr-001-grib-rendering-architecture.md);
the current renderer state and next decision are in
[`docs/rendering-performance-plan.md`](docs/rendering-performance-plan.md). The rest of the
project documentation is indexed in [`docs/README.md`](docs/README.md).

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Every commit needs a
`Signed-off-by` trailer (`git commit -s`).

## License and attribution

Sailworks is licensed under the GNU General Public License, version 3 or later
(`GPL-3.0-or-later`). Read [LICENSE](LICENSE) for the full text and [NOTICE](NOTICE) for the
fork point and upstream attribution. Bundled third-party assets — chart symbols, icons,
seamark data — are listed with their licenses in
[`sailworks/data/COPYRIGHT`](sailworks/data/COPYRIGHT).
