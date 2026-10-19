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

For opt-in early-weather startup, use `python -m gweatherrouting --early-weather`
(add `--offline` to keep instrument connections closed). It defers unused log
plots and stages chart initialization after weather preparation in the real map.
The default startup and renderer selection are unchanged. See
[early-weather startup](docs/early-weather-startup.md) for limits and validation.

See [default-launcher validation](docs/qt-default-validation.md) for platform and hardware validation notes.

## Performance gate runner

`tools/gate_runner.py` gates a rendering change with **one command**. It composes the
existing benchmark harnesses — it does not replace or re-tune them — and evaluates the
acceptance threshold from the JSON those tools already emit.

```sh
# list the registered gates (instant, no GDAL/Qt import)
python tools/gate_runner.py --list

# run one gate
python tools/gate_runner.py --gate residency --snapshot <snapshot> --output <dir>

# the whole matrix is opt-in and never the default
python tools/gate_runner.py --gate validation --snapshot <snapshot> --output <dir> --full

# prove the runner fails (non-zero) when a threshold is violated
python tools/gate_runner.py --self-test
```

The runner prints one summary line per gate, writes `<dir>/<gate>.result.json` next to the
raw tool report, and exits non-zero on `FAIL` so CI can use it.

### Verdicts

The four outcomes are deliberately distinct, in the summary line and in the result JSON
(`"verdict"` plus `"evaluated"`):

| Verdict | Meaning | Exit |
| --- | --- | --- |
| `PASS` | The gate was evaluated and every ratified check passed. | 0 |
| `PASS-WITH-OPEN-CHECKS` | The gate was evaluated and every ratified check passed, but some checks are unratified, not comparable or not evaluated. | 0 |
| `FAIL` | The gate **was** evaluated and a ratified threshold was violated. Failing checks are prefixed with `!`. | 1 |
| `NOT-EVALUATED` | The gate **could not be** evaluated — no usable interpreter, the tool crashed or timed out, or it wrote no report. No threshold was compared. | 2 |

A `FAIL` and a `NOT-EVALUATED` must never be confused: the first is a real result about
the change, the second means the gate did not run.

### Interpreter

The composed tools need the project's dependencies (PySide6, NumPy, Cairo, GDAL). The
runner checks the interpreter **before** it invokes a tool, and never reports a missing
dependency as a threshold failure:

* if the current interpreter cannot import what the gate needs, the runner re-executes
  itself under the project virtualenv (guarded so it cannot loop) and prints a note on
  stderr;
* if no usable interpreter is found, the gate is reported `NOT-EVALUATED` with the
  interpreter to use.

On this machine the project virtualenv is
`/Users/elivingston/gweatherrouting/.venv/bin/python`; the default `python` is a pyenv
3.9.17 without PySide6 or GDAL, so use the virtualenv interpreter explicitly when the
automatic re-exec is not wanted:

```sh
/Users/elivingston/gweatherrouting/.venv/bin/python tools/gate_runner.py --gate residency --snapshot <snapshot> --output <dir>
```

| Option | Meaning |
| --- | --- |
| `--gate ID` | Gate to run. Repeat for several. |
| `--list` | List the gates and exit. |
| `--snapshot PATH` | Verified startup snapshot fixture the gate replays. |
| `--output DIR` | Where reports and results are written (a temporary directory by default). |
| `--reference PATH` | Baseline artifact for gates whose reference is a measured baseline. |
| `--full` | Opt in to the full scenario matrix. |
| `--frames`, `--repeats` | Override the workload size. |

One gate per milestone of the GRIB rendering program:

| Gate | Milestone | Tool | What it decides |
| --- | --- | --- | --- |
| `residency` | #64 | `benchmark_startup_rendering.py` | A resident field is not re-uploaded during a time scrub, and the decoded-band budget holds. |
| `interpolation` | #65 | `experiment_float_textures.py` | GPU spatial sampling keeps the unchanged 2e-5 error bound. |
| `pipeline` | #66 | `benchmark_startup_rendering.py` | Preparation overlaps presentation: no input is dropped before it renders. |
| `scrub` | #67 | `benchmark_startup_rendering.py` | A forecast-time change inside the resident window uploads nothing. |
| `fill` | #68 | `benchmark_startup_rendering.py` | The numerical path is active and the barb selection/count gate is unchanged. |
| `chart-separation` | #69 | `check_early_weather_startup.py` | Weather is presented before chart readiness and survives the chart bind. |
| `validation` | #70 | `benchmark_startup_rendering.py` | Per-layer GPU/loading/fallback reporting, no silent CPU fallback, no overlay errors. |

Every threshold states where its reference value comes from — a **recorded evidence**
document under `docs/`, or a **measured baseline** the caller supplies with `--reference`.
The runner never re-measures a baseline on its own. Render callbacks are proxies, not
presentation FPS, and readback is fidelity, not timing; every gate labels its endpoint.

See [`docs/adr-001-grib-rendering-architecture.md`](docs/adr-001-grib-rendering-architecture.md)
for the architecture these gates protect and
[`docs/rendering-performance-plan.md`](docs/rendering-performance-plan.md) for the
canonical roadmap.

## License

Read the LICENSE file.
