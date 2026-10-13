# Development version and upstream contributions

`dev` is this fork's complete working version. It preserves the history of
`charts/fluid-map-and-downloads`, including the macOS setup, detailed charts,
smooth navigation, wind rendering, playback fixes, and subsequent validation
fixes. Linux and macOS CI run when `dev` is pushed.

The legacy chart branch is retained for history. Its fork PR is superseded by
the focused upstream contributions below; it is not the integration branch.

| Contribution | Branch | Upstream PR | Independent local validation |
| --- | --- | --- | --- |
| macOS setup and GTK threading fixes | `macos-dev-setup` | [#267](https://github.com/dakk/gweatherrouting/pull/267) | Existing contribution; retained in `dev` |
| Smooth zoom, pan inertia, Display preferences | `upstream/smooth-navigation` | [#268](https://github.com/dakk/gweatherrouting/pull/268) | 328 tests, 38 subtests; Black, Flake8, mypy |
| NOAA discovery/downloads, ENC portrayal, Canadian CHS imagery | `upstream/downloadable-charts` | [#269](https://github.com/dakk/gweatherrouting/pull/269) | 375 tests, 38 subtests; Black, Flake8, mypy |
| Background wind rendering, cursor queries, forecast playback | `upstream/wind-display-performance` | [#270](https://github.com/dakk/gweatherrouting/pull/270) | 347 tests, 38 subtests; Black, Flake8, mypy |

The three new contributions were independently prepared from parent `master`
at `946e66c`. Each includes the support it needs, so none requires another
pending PR or the macOS setup to merge first. Frozen map viewports, windowless
test fixtures, and the GTK test environment have identical support patches
across these branches. The chart and wind branches also share an identical
generic overlay worker. Once shared support lands, synchronize the remaining
branches with parent `master` so their diffs show only the remaining changes.
Resolve any overlapping settings or chart-stack integration changes and rerun
the relevant checks; do not assume every merge order is conflict-free.

The complete `dev` version passes 414 tests and 38 subtests, plus Black, Flake8,
and mypy. Native playback with a real GRIB and forced rendering delays verified
that forecast transitions do not show blank wind frames. These are local
validation results; upstream CI and review are tracked on the PRs.

New work starts on `feature/<topic>` from `dev`. Apply upstream review fixes on
the relevant contribution branch, validate that branch, and bring the fixes
back into `dev`. Keep `AGENTS.md`, this tracking file, and fork-only CI triggers
out of upstream PRs. Preserve `master` as the parent-tracking branch.

## NMEA 2000 reception and recording

- Status: candidate for upstream; integrated locally on `dev` from
  `feature/nmea2000-actisense`.
- Functional commit: `19128ad` (`Add multi-format NMEA 2000 recording`).
- Scope: transport-independent PGN messages and instrument conversions;
  Actisense BST and N2K ASCII over serial/TCP/UDP; raw CANboat-format session
  archives; and a pseudo-terminal simulator. SocketCAN and Yacht Devices RAW
  remain future adapters.
- Validation: 60 focused NMEA 2000 tests pass, including serial/network input,
  plus a GTK settings smoke test. Black, Flake8, scoped mypy, and diff checks
  pass. The full suite was attempted but the sandbox aborted while importing
  native GDK in chart tests; no assertion failure was reported. Onboard NGT-1
  compatibility and Polarize import have not been verified.
- Intended upstream scope: separate reusable receiver/conversion work from
  fork-specific recording and simulator features. No upstream PR is open.
