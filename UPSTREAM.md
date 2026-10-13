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

## Measured performance on polar charts

- Status: candidate; local work on `feature/polar-performance` from `dev`.
  Functional commit: `55481bf`. No review PR yet.
- Scope: full-circle Polars comparison with instrument scatter points colored
  by nearest predicted wind-speed band; separate STW/SOG histories; live
  collection with freshness/validity checks; interpreted NMEA log import;
  wind-band filtering, instant curve/sample hover inspection with curve dimming,
  corrected boat-speed rings, and a dockable/floating NMEA performance panel.
- Dependencies: existing NMEA packet delivery. NMEA 2000 support uses the
  existing interpreted conversions; basic NMEA 0183 operation is independent
  of that contribution. Raw PGN import and automatic apparent-to-true wind
  derivation are outside this change.
- Validation: 129 focused tests pass (performance collection and GTK controls,
  boat information, polar wrapper, data sources, connection management, and
  tool docking). Sidebar sizing and hide/float/redock behavior preserve samples
  and collection settings. Floating palettes use rounded client-side decorations
  without the outer white outline; native window exterior verified on macOS.
  Black, Flake8, scoped mypy, and diff checks pass. Native GTK preview verified
  the complete Polars tab with 2,200 synthetic observations. No onboard data
  validation or full-suite run performed for this change.
- Intended upstream scope: reusable sample collection, polar visualization,
  controls, deferred initial dock sizing, tests, and user documentation; exclude
  this fork tracking entry.

## Polar Manager component migration

- Status: candidate; local dependent work on `feature/polar-performance`,
  using its Polars tool dock. Functional commit: `55481bf`. No review PR yet.
- Scope: reusable Polar Manager panel with Local polars and ORC catalog tabs;
  docked/floating integration in Polars and the routing wizard; persistent
  search/selection across hide/show; asynchronous downloads with lifecycle
  guards; immediate list updates and model-based toggles after sorting.
- Validation: 40 focused tests pass across Polar Manager, performance controls,
  tool docking, and current-routing/wizard integration. Imports, removal,
  enable/disable, download completion, destroyed-panel callbacks, and reuse
  are tested with file dialogs and network workers mocked. Native dark-theme
  previews verified docked Local polars and floating ORC catalog layouts.
  Black, Flake8, scoped mypy, and diff checks pass. Live ORC service access was
  not exercised for this migration.
- Intended upstream scope: reusable panel and component integration, the
  shared fix selecting newly opened component tabs, tests, and documentation.
  Keep this fork tracking entry out of an upstream contribution.

## Polar selector component

- Status: candidate; dependent local work on `feature/polar-performance`.
  Functional commit: `55481bf`. No review PR yet.
- Scope: move the existing filename/sail/name/type selector into a reusable
  dockable/floating component, with a toolbar visibility toggle. Preserve
  selection by filename across list refreshes; retain the speed table in the
  main view and keep chart/table updates connected to selection changes.
- Validation: 41 focused GTK and routing tests pass, including sorted selection,
  float/hide/redock, refresh while hidden, and fallback after polar removal.
  Native dark-theme previews verified sidebar and floating selector layouts.
  Black, Flake8, scoped mypy, and diff checks pass.
- Intended upstream scope: selector extraction, toolbar integration, selection
  preservation, lifecycle cleanup, tests, and documentation; exclude this entry.

## Vertically stacked component groups

- Status: candidate; local dependent work on `feature/polar-performance`.
  Functional commit: `55481bf`. No review PR yet.
- Scope: shared component bar supports resizable vertical groups alongside
  tabs and floating windows. Named tabs move individual components; dotted
  grips move entire groups. Upper/lower drops split vertically with blue
  insertion feedback; middle/tab-strip drops group or reorder tabs. Hidden
  components retain their group and state.
  Polars and Charts keep their tab toolbars above the main content and
  component bar, below the application's horizontal toolbar; status bars
  also span the tab width.
- Pointer fix: the initial implementation reparented docked groups on the
  first mouse motion, unmapping the grip and losing its release event on
  macOS. Tabs and grips now use explicit GTK pointer handling, retain the
  mapped event source until release, and only then transfer components.
  Drop feedback changes only when the destination changes, avoiding repeated
  layout changes during motion. GTK's platform drag-and-drop transport is
  no longer required for component placement.
- Validation: 60 focused tests pass across dock lifecycle, Polars controls,
  manager/selector integration, routing wizard, and Currents/Tides panels.
  Regression tests send GTK press/motion/release events without mocking drag
  transport and assert that the source stays mapped and its grab is released.
  Seven native macOS mouse-event gestures independently verify below/above
  stacking, tab merging, floating a tab, and grip docking/floating/merging.
  Native rendering verifies two live Polars components in a vertical stack.
  Full-app rendering verifies both tab toolbars above their sidebars, with
  a geometry regression assertion for the Polars toolbar and sidebar.
  Black, Flake8, scoped mypy, and diff checks pass.
- Intended upstream scope: shared dock stacking and pointer fix, tests, and
  usage documentation; exclude this entry and unrelated Polars feature work.

## Shared NMEA streaming and log playback component

- Status: candidate; local dependent work on `feature/polar-performance`,
  building on its performance controls and shared tool dock.
  Functional commit: `55481bf`. No review PR yet.
- Scope: one Core-owned NMEA session, with dockable/floating NMEA views in
  Charts, Logs, and Polars. Share live/log selection, stream pause, loaded
  file, playback position, play/pause/restart, and speed across tabs. Keep
  polar display filters in a separate Performance component. Load logs in
  the background, provide a full analysis overview, and replay through the
  same selected stream used by instruments, maps, and measured polars.
- Input policy: live input is excluded while replay is selected; pausing input
  or changing away from live stops active recording. NMEA UTC timestamps set
  playback timing, including midnight rollover; untimestamped logs explicitly
  use ten sentences per second. Maximum file size is one million valid
  sentences. Polar sample pairing retains the existing five-second live and
  fifty-line imported freshness policies, with no new wind approximation.
- Logs: consume the selected stream without a separate polling thread, discard
  queued packets after a source reset, and throttle plot refreshes. Preserve
  recording and raw PGN archives; save cropped loaded logs without modifying
  their source. Normalize UTC crop bounds and plot short logs without dropping
  nearly all their points. Automatic loading of the previous temporary
  recording at startup is replaced by explicit shared source selection.
- Validation: 183 focused tests and 8 subtests pass across shared session/views,
  performance, docking, Polar Manager, current-routing integration, NMEA 2000
  conversion/recording, and AIS. Coverage includes pause/resume/end/restart,
  speed, midnight rollover, dense replay batches, malformed/failed/stale loads,
  preservation across tab/hide/float/recreation, load completion after view
  destruction, stale queued packets, and cropped saves with raw companions.
  Black, Flake8, scoped mypy, and diff checks pass. Native full-app rendering
  and assertions exercise all three tabs, overview, replay, source reset, and
  synchronized controls with network polling and settings writes disabled.
  Live hardware streaming was not exercised.
- Intended upstream scope: reusable session and controls, tab integration,
  necessary Logs lifecycle/save fixes, regression tests, and documentation.
  Queue extraction until the performance/tool-dock prerequisites are reviewed;
  exclude this fork tracking entry and unrelated manager or routing changes.

## Sidebar persistence across workspace tabs

- Status: candidate; dependent local work on `feature/polar-performance`,
  following the shared tool dock and NMEA component. Functional commit: `55481bf`. No review PR yet.
- Scope: Charts, Logs, and Polars use a single workspace dock and one NMEA
  widget. The same sidebar follows the active tab below its toolbar, retaining
  open/hidden components, selected component tabs, vertical groups, sidebar
  width, divider positions, and floating windows. Components opened from any
  tab stay available on the other tabs, and NMEA toolbar toggles stay synchronized.
  Tab-specific content stays alive in the shared dock's content stack.
- Layout: preserve vertical divider positions when toolbar heights differ and
  avoid revealing intentionally hidden view controls during remounting. Retain
  standalone tab construction for embedding and focused tests.
- Validation: 72 focused GTK, NMEA session/performance, manager, and routing
  tests plus 8 subtests pass. Regression checks use different tab toolbar heights
  and verify the same mapped sidebar, selected tabs, split widgets/positions,
  width, floating placement, hidden components, and singleton NMEA widget.
  Native pointer tests now wait for frame-clock allocations before using geometry,
  fixing intermittent failures caused by reading layout before its next frame.
  Black, Flake8, scoped mypy, and diff checks pass. Full-app preview checks
  all three tabs, toolbar/sidebar ordering, split sizes, hidden routing controls,
  floating windows, and synchronized toolbar toggles with polling and settings
  writes disabled.
- Intended upstream scope: workspace dock, tab integration, singleton component
  attachment, geometry/lifecycle regression tests, and usage documentation.
  Queue on the shared tool-dock and NMEA prerequisites; exclude this tracking entry.

## Recorded-track rendering during chart motion

- Status: candidate; local follow-up on `feature/polar-performance`, after the
  shared NMEA/sidebar work. Functional commit: `a498dc7`. No review PR yet. The rendering change
  itself does not require the sidebar or NMEA component for upstream extraction.
- Problem: drawing a long recorded boat track projected every point separately
  and reapplied its style for every segment on every pan/zoom frame, including
  entirely offscreen geometry. Route times were also reparsed for every segment
  on each frame.
- Scope: use the existing batch Mercator projection for the recorded track;
  reject only segments whose bounds lie wholly beyond a viewport edge, with
  padding for strokes; retain separate strokes and every original track point.
  Cache route datetime parsing in a bounded cache while converting timestamps
  during drawing, preserving legacy local-time and explicit-offset behavior.
  Rebuild track arrays from current points each frame, so live appends, edits,
  clear, and source replacement need no cache invalidation policy.
- Fidelity: no track simplification, dropped source points, source-selection
  changes, or new route approximation. Crossing segments remain visible when
  both endpoints are outside the viewport. Separate strokes preserve alpha
  overlaps, caps, and dash behavior.
- Validation: 43 focused geometry/rendering, map-motion, and shared-sidebar
  tests plus 9 subtests pass. Tests cover visible raster equivalence at fractional
  zooms and both pixel densities, offscreen/crossing segments, live mutations,
  route timestamp reuse, mixed local/UTC times, and daylight-saving transitions.
  Black, Flake8, scoped mypy, and diff checks pass.
- Measurements: a pinned 26,570-point recorded track with existing routes,
  634×750 logical pixels at 2× density, 15 measured draws after warmup,
  without profiler overhead: visible-track median 60.1→7.4 ms at zoom 14 and
  60.9→8.6 ms at zoom 13.6; offscreen-track median 56.3→2.6 ms. Pixel buffers
  are byte-identical in all three comparisons. A full-app motion run with
  cProfile enabled reduced average total map draw time from about 216 ms to
  16 ms. With pinned identical data, the previous per-tab dock and the shared
  sidebar had the same original bottleneck. Local profiling scripts, snapshots,
  and results remain in `/private/tmp` and are excluded from contributions.
- Intended upstream scope: geometry rendering, bounded timestamp parsing,
  regression tests; exclude fork tracking and unrelated UI changes.

## Workspace preferences between launches

- Status: candidate; dependent local work on `feature/polar-performance`,
  following the shared NMEA session, workspace dock, and Polar Manager panels.
  Functional commit: `55481bf`. No review PR yet.
- Scope: restore active tab, map centers and zoom, window geometry/maximization,
  sidebar visibility, groups, selected tabs, dividers and floating windows,
  chart/log display toggles, selected polar and performance filters, manager
  search/tab, and live input/playback speed preferences. Restore before showing
  the main window and fit floating windows to available monitor workareas.
- Storage: versioned `workspace.json`, atomic replacement, debounced saves after
  interaction settles, and a final save on exit. Pan/zoom frames perform no disk
  writes. Missing, corrupt, unsupported, or unavailable files are nonfatal.
  Samples, loaded-log contents/path, replay position, and recording are session
  state and are excluded. Startup uses live input with the saved stream preference.
  Account for the native divider width to prevent sidebar drift across launches.
- Validation: 85 focused tests plus 14 subtests pass across persistence, native
  workspace restoration, docking, NMEA controls, polar panels, map rendering,
  and routing integration. Black, Flake8, scoped mypy, and diff checks pass.
  Separate native setup/relaunch processes restored maps, grouped/split/floating
  panels, filters, paused live input, and exact sidebar width from a 1,972-byte
  file, with network polling and user settings writes disabled. No live hardware
  or full-suite validation performed.
- Intended upstream scope: preference store, restore/save lifecycle, component
  serialization and stable widget IDs, regression tests, and documentation.
  Queue extraction on the shared dock/NMEA prerequisites; exclude this tracking
  entry and unrelated rendering work.

Final combined pre-commit validation: 103 focused tests and 17 subtests pass,
including NMEA session timing and measured polar collection in addition to the
workspace/UI/rendering checks above. The integrated component changes share
initialization and dock lifecycles in `55481bf`; upstream extraction should adapt
one focused contribution at a time and validate each resulting branch.

## NMEA recording controls

- Status: candidate; reviewed in fork [PR #4](https://github.com/evanlivingston/gweatherrouting/pull/4)
  on `feature/nmea-recording-controls` from `dev`. Functional commit: `e10f896`.
- Scope: move recording into the shared NMEA component with a DAW transport
  (Restart, Play/Pause, square Stop, red Record toggle). Both stop controls
  preserve the captured data and plots. Remove Save, Clear, and the Logs time
  cursor controls, crop markers, and crop action. Hide playback speed in live
  mode. Loaded logs retain their full plotted track throughout playback, with
  a moving cursor on each subplot.
- Recordings: continuously write interpreted sentences and raw NMEA 2000
  companion archives into the app data directory's `recordings` folder. Each
  capture uses a UTC timestamp and exclusive creation, with a suffix for name
  collisions. Starting another capture and closing the app retain prior files.
  Show the current file path and explicit automatic-saving/stopped status in
  the panel; the native load chooser starts in the recordings folder when it
  exists and retains the interpreted-log file filters.
- Dependencies: shared NMEA session/component and NMEA 2000 recording support
  already present in `dev`; queue upstream extraction on those prerequisites.
- Validation: 92 tests and 8 subtests pass across NMEA session/controls,
  transport/recording, workspace, and polar UI. Tests verify files are readable while
  capture is active, interpreted/raw archives survive shutdown and later
  recordings, name collisions preserve existing files, and both stop controls
  retain identical Matplotlib plots. Playback tests cover full-data plots,
  cursor visibility on every subplot with a Retina/high-DPI canvas, pause,
  restart, EOF, source switching, and hidden live-mode speed controls.
  Static plots are cached; a native 300-point preview measured 505 ms for the
  initial plot and 0.5 ms per cursor redraw including session notifications.
  Existing graph sampling is retained, with the final point always included.
  Black, Flake8, scoped mypy, native panel
  preview, and diff checks pass. Hardware recording has not been exercised.
- Intended upstream scope: session recording ownership, persistent timestamped
  captures, reusable NMEA controls, Logs cleanup, and regression tests.
  Exclude fork tracking.

## Native filesystem pickers

- Status: candidate; reviewed in fork [PR #4](https://github.com/evanlivingston/gweatherrouting/pull/4).
  Functional commit: `aa7cdf6`; branch: `feature/nmea-recording-controls`.
- Scope: use one GTK native-picker helper for all ten Open, Save, and folder
  chooser locations: NMEA, GRIB, polar, GPX/track/route/POI, and chart directories.
  Retain filters, default filenames/directories, modal parenting, and overwrite
  confirmation; handle native ACCEPT/CANCEL responses.
- Dependencies: existing picker migrations are independent; the NMEA callsite
  depends on the shared NMEA component.
- Validation: full suite passes 761 tests and 71 subtests. Native Open, Save,
  and Select Folder configuration smoke checks pass. Black, Flake8, scoped
  mypy, and diff checks pass. Polar import tests verify cancellation and import
  with the floating window as parent.
- Intended upstream scope: shared helper and focused callsite migrations,
  with their import/cancel regression coverage. Exclude unrelated NMEA and
  fork tracking changes.

Fork PR #4 pre-merge validation: 761 tests and 71 subtests pass locally.
CI reports an existing complexity failure in `ConnectionEditDialog.__init__`
and four type errors in NMEA 2000 source/settings code. These files are unchanged
by this PR; the complexity failure and all four type errors reproduce on `dev`
in an isolated baseline worktree. With installed optional libraries, local full
mypy reports additional existing library-typing errors on both branches; scoped
checks of the changed source pass. The two full-tree Black formatting differences
are also in unchanged baseline files. No new lint/type failures were introduced.
