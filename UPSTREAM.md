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

## Fork review before upstream submission

New work is reviewed through regular PRs in this fork first. Independent PRs
start from `dev`; dependent PRs can target their prerequisite feature branch so
the review shows only the additional work. Nothing is merged or submitted to
upstream until the user approves that action. After approval, extract focused
changes onto fresh `upstream/<topic>` branches and validate them independently.

Current review order:

1. `feature/latest-wind` → `dev`: newest enabled wind model run, fallback to
   older coverage, duplicate-free weather grids, and gradient seam fixes.
2. `feature/tides-currents-plan` → `feature/latest-wind`: marine data providers,
   tide caching, current-aware routing, coverage recovery/prefetch, batched
   calculation, and Currents/Tides controls. Retarget this PR to `dev` after
   the wind PR merges and verify the diff again.

The full marine feature is a fork integration review, not one proposed upstream
patch. Its data/provider layer, UI, current-aware routing, geometry/performance
work, and forecast recovery may need separate upstream contributions. The
newest-run wind policy relates to upstream issue #185; marine routing relates to
#158 but does not implement arbitrary current GRIB imports. Existing upstream
PRs #267–#270 are unchanged by these fork reviews.

## Contribution queue

This queue records review and extraction boundaries. Local development continues
while an item waits for human review, a dependency, or upstream feedback.
"Candidate" means a possible future submission, not authorization to publish it.
Keep validation tied to the tested snapshot; rerun relevant checks after review
fixes, retargeting, or adapting code to upstream.

| Contribution | Fork review | Functional commits | Local validation | Upstream disposition |
| --- | --- | --- | --- | --- |
| Latest wind selection and gradient fixes | [#2](https://github.com/evanlivingston/gweatherrouting/pull/2), targets `dev` | `5646ea4` | 420 tests, 38 subtests; Black and Flake8 pass | Candidate: focused adaptation for issue #185; coordinate display changes with existing upstream PR #270 |
| Tides and current-aware routing | [#3](https://github.com/evanlivingston/gweatherrouting/pull/3), targets `feature/latest-wind` | `0dc8392`, icon follow-up `9b97439`; depends on `5646ea4` | 624 tests, 54 subtests; Black and Flake8 pass; wheel build passes. Icon follow-up: 23 GTK tests pass; Black/Flake8 pass; native preview verifies both top buttons, light/dark rendering, accessibility and floating/redocking. | Candidates: separate data/provider, UI (including the shared tides/current icon), routing, recovery and performance contributions; assess prerequisites before submission; related to #158 |
| Local profiling records | Included in fork #3 | `f662d8f` | Historical measurements and reproduction notes | Fork-only artifacts; extract portable benchmark tools separately if useful |
| Fork workflow and PR template | Included in fork #3 | Separate policy commits | Documentation and diff checks | Fork-only; exclude from parent-project PRs |

Both code snapshots reproduce the same 31 repository-wide mypy errors in six
unchanged files as baseline `dev` in the current native environment, with no new
errors. Earlier validation results above used a different dependency environment;
they are historical records rather than evidence that the current full type
check passes.

After fork #2 merges with user approval, retarget #3 to `dev`, verify that its
diff contains only the remaining work, and rerun relevant checks before merging.
Upstream submission requires separate authorization. Neither fork PR has been
merged as part of establishing this workflow.
