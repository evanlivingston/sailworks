# Qt default launcher validation

Qt is the default for `gweatherrouting` and `python -m gweatherrouting`.
`gweatherrouting-qt` remains an alias. Python 3.10+ and PySide6 Essentials are
normal installation requirements; the old `[qt]` extra remains compatible.
GTK is explicit: `gweatherrouting-gtk` or `python -m gweatherrouting.gtk`.

This pass covers the default launch and migration validation. App About/name
identity and Qt distributable bundles are separate work. Existing GTK-only
AppImage/Flatpak/Windows bundle recipes select GTK explicitly.

## Local results

Checked on macOS 26.6.2 arm64, Python 3.11 and PySide6/Qt 6.12.0, October 14, 2026.

| Check | Result and evidence |
| --- | --- |
| Installed default command | Editable install regenerated the console scripts. `gweatherrouting --help` exposes `--offline`/`--log`; installed metadata includes Qt and Python 3.10+. |
| Native startup | The actual installed console script opened Qt at DPR 2, honored `--offline`, and did not import `gi.repository.Gtk`. Private settings/layout copies protected the original workspace. |
| Full suite | `python -m pytest -q`: 826 tests and 106 subtests pass in 99.36s. This includes 99 isolated Qt checks; the POSIX pseudo-terminal check skips on Windows. |
| Scoped checks | Black, Flake8 and mypy with silent imports pass for the changed production modules. Changed YAML parses and the AppImage shell recipe passes `bash -n`. |
| Workflow/provider parity | The Qt suite covers navigation/import/export, full log plots/cursor, automatic recording, preferences, chart selection/NOAA staging, vector/raster geometry, weather/marine overlays, polars, routing/comparisons/cancellation and saved workspace. Live provider downloads are not revalidated by these fixture-based checks. |
| Live TCP/UDP transport | Real localhost sockets feed production readers, ConnectionManager, Core, UI-thread status and timestamped recording files. TCP input is deliberately split across writes. Shutdown and retention after Stop are checked. These are simulated instrument feeds. |
| Serial transport | A real POSIX pseudo-terminal feeds the production serial reader and Qt UI. A shared regression demonstrates that split sentences were parsed prematurely on the base; buffering now waits for CR/LF and bounds corrupt input. All 55 connection checks pass. |
| Large playback | A 30,000-point regression retains every map point and plot value as playback advances. A separate 100,000-sentence run is measured below. |
| Dateline | Coordinates are retained, but fitting `(10, 179.8)` and `(10, -179.8)` chooses a wide view (zoom 1.58). This is a pre-existing unresolved map-framing limitation, not a successful dateline-parity result. No projection/routing calculation changes are included in this pass. |

The full pytest result precedes a test-only cleanup refinement that ignores
expected socket-close errors after polling has stopped; its focused TCP/UDP
check passes again. Production code is unchanged after the full run.

## Large-file measurement

A generated 100,000-RMC file spans 100,000 seconds and contains 4.96 MiB of input.
It goes through the asynchronous production file loader, then presents all
100,000 map points and every value in each enabled Matplotlib series. Advancing
playback retains the complete plot curves.

- Load and presentation: 3.30 seconds.
- Mean plot-cursor update over 100 updates: 0.42 ms.
- Peak total process RSS: 446 MiB, including Core, Qt, Matplotlib and fixtures.
- Largest measured event-loop gap during loading/presentation: about 601 ms.

These are local fixture measurements, not timing guarantees. In particular,
large-file presentation still causes a noticeable main-thread pause. The
one-million-sentence playback limit is unchanged; this pass does not claim a
successful million-sentence or arbitrarily large-file stress test. The existing
20,000 polar-performance-sample history bound is unchanged; map/plot point data
in this check is complete.

## Cross-platform and physical validation

The dedicated [Qt validation workflow](../.github/workflows/qt-validation.yaml)
installs Qt and runs the default-launcher and isolated workflow checks on
Ubuntu, macOS and Windows/MSYS2 UCRT64. It imports Qt explicitly so an unavailable Qt
installation cannot be reported as a skipped, successful frontend check.
Remote results are tracked on fork PR #25; pending jobs are not local passes.
The Windows runtime uses the existing MSYS2 dependency approach and its
[PySide6 package](https://packages.msys2.org/packages/mingw-w64-ucrt-x86_64-pyside6).

Physical NMEA hardware has not been confirmed. The saved serial configuration
uses a local pseudo-terminal; it was not consumed or reconfigured for these
checks. Local sockets and newly created pseudo-terminals establish the software
transport path, not onboard USB/gateway behavior, baud-rate reliability, cable
loss/recovery or long-duration operation.

Still required before claiming full platform/hardware parity:

- Review the new remote Qt job results and perform native Windows/Linux checks
  for file dialogs, docks, scaling, gestures and shutdown.
- Connect actual instruments and verify live state, automatic recording,
  disconnect/reconnect and prolonged operation on supported platforms.
- Correct dateline framing/portrayal and validate relevant charts and routing
  scenarios across ±180° separately.
- Recheck real provider downloads with representative accounts/data. Existing
  fixture results do not establish current provider service availability.

Legacy CI dependency/lint/type failures are a separate known task. This pass
adds self-contained Qt validation and drops Python 3.9 from the install/test
matrix because the default Qt application requires Python 3.10+; it does not
claim to clean up the other legacy CI jobs.
