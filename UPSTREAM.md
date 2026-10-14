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

- Status: candidate; merged into `dev` through fork [PR #4](https://github.com/evanlivingston/gweatherrouting/pull/4)
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

- Status: candidate; merged into `dev` through fork [PR #4](https://github.com/evanlivingston/gweatherrouting/pull/4).
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

## Shared map projection and Qt first pass

- Status: reusable projection is an upstream candidate; the Qt frontend is a
  fork-only migration experiment on `feature/qt-first-pass` from merged `dev`.
- Scope: extract existing Mercator/viewport math into a toolkit-independent core
  module, retaining GTK import compatibility. Add an optional PySide6 frontend
  with shared NMEA sessions, native file dialogs, timestamped recordings,
  full-log Matplotlib plots with blitted cursors, a floating NMEA dock, and a
  Cairo/QImage coastline/track canvas. GTK remains the default launcher.
- Dependencies: shared NMEA session, persistent recordings, and viewport work
  already integrated in `dev`. A complete application migration still needs
  production chart/overlay adapters and toolkit-neutral Core scheduling.
- Validation: 762 repository tests and 71 subtests pass; the optional Qt wrapper
  executes seven isolated integration checks. Changed-file Black/Flake8 and
  scoped mypy pass; editable installation and wheel contents are verified.
  Native macOS window controls and playback were visually checked. Projection
  parity: 2,000 coordinates, zero-pixel differences. Small native 180-point
  run: full plot 40.3 ms, cursor 1.84 ms, cached coastline image 2.9 ms. Detailed
  ENC/source-selection parity and hardware input are not established by this pass.
- Review: [fork PR #5](https://github.com/evanlivingston/gweatherrouting/pull/5),
  merged into `dev`. Functional commits: `20ba52e` (projection) and
  `ec0bd44` (optional Qt frontend, tests, packaging, and assessment).
- Intended upstream scope: shared projection extraction only; keep the optional
  Qt experiment and fork documentation out of parent-project PRs unless invited.
- Assessment and launch instructions: `docs/qt-first-pass.md`.

## Qt production charts and Core integration

- Status: shared Core scheduling/chart extraction are upstream candidates;
  the Qt adapter/UI is fork-only. Branch: `feature/qt-production-charts`,
  dependent on `feature/qt-first-pass` and fork PR #5.
- Review: [fork PR #6](https://github.com/evanlivingston/gweatherrouting/pull/6),
  retargeted to `dev` after PR #5 merged, then merged into `dev`.
- Scope: inject Core notification scheduling while preserving GTK defaults;
  extract production Cairo chart/wind rendering and settings from GTK; retain
  compatibility imports, palettes, coastline quality thresholds, ENC source/
  scale selection and wind interpolation. GTK retains its GObject chart adapter
  and download dialogs. Qt uses these renderers and the full Core with queued
  worker completions, callback cancellation, chart lifecycle/tick adapters,
  installed chart preferences, wind forecast loading/time selection and boat
  status. The GTK launcher remains the default.
- Functional commits: `4a05215` (Core scheduler and connection-start control),
  `3f99e48` (shared rendering/settings, Cairo S57 sprites and raster worker
  portability), `7f077d9` (interior water in simple coastline polygons),
  `be0b73a` (Qt Core/chart/wind integration and regression coverage).
- Fidelity: all 2,030 S57 day/dark sprites at fractional coordinates match the
  previous GdkPixbuf renderer pixel for pixel. Production projection, source
  selection, half-pixel ENC tolerance and routing calculations are retained.
  The simple coastline renderer now fills polygon rings together with even-odd
  winding, preserving holes; fill/stroke ordering changes with that fix. Raster
  conversion preserves existing palette values and replaces a nested local
  multiprocessing worker with the existing loader thread, allowing macOS spawn.
- Validation: twelve isolated Qt checks pass, including full Core worker delivery,
  stale notifications, callback cancellation, production OGR ENC/raster fixtures,
  palette/forecast changes, retained recordings/full plots and no GTK imports.
  Two toolkit-independent Core scheduler tests and a polygon/multipolygon hole
  regression with two subtests pass. Final full suite: 765 tests and 73
  subtests pass. An earlier concurrent run hit Qt subprocess and existing GTK
  dialog timeouts; both passed in isolation, and the final full run passed.
  The expanded Qt wrapper allows 120 seconds for twelve full-Core checks;
  fixtures isolate user settings and local forecast/polar scans.
  Changed-source Black/Flake8 pass. Full local mypy retains the same 32 baseline
  errors in eight files (symbol parser errors move with its implementation);
  no additional errors. Wheel contents include rendering, settings and schedulers.
  Native macOS QA loaded GSHHS, 31 saved vector layers and three wind sources:
  cached chart image 6.2 ms, ten small pans averaging 7.44 ms; small-log full plot
  86.4 ms and cursor 6.32 ms. Other checks ran concurrently, so these are local
  sample measurements rather than controlled performance comparisons. Hardware,
  current/tide UI and large-chart stress tests are pending.
- Integration: review against `feature/qt-first-pass`; merge PR #5 first, then
  retarget this PR to `dev` and verify the resulting diff/checks. Leave both
  unmerged until approved. No upstream submission is authorized by fork review.
- Intended upstream scope: extract scheduler injection and toolkit-neutral Cairo
  rendering separately after their shared prerequisites land; keep Qt UI,
  migration assessment and fork tracking out of parent-project contributions.
- Assessment and launch instructions: `docs/qt-first-pass.md`.

Qt pre-merge CI review: targeted naming exceptions allow Qt virtual methods.
CI test collection fails because the existing tox environment omits runtime
`nmeatoolkit`; this predates Qt. Builds/docs pass; existing GTK complexity/type
failures remain. Local full-suite results above remain valid.

Qt integration: fork PRs #5 and #6 merged into `dev` with user approval.
After retargeting #6, 46 focused tests and two subtests passed; new CI runs
were pending at merge. The Qt naming violations are fixed; prior CI test
collection fails on the omitted `nmeatoolkit` tox dependency, with baseline
GTK complexity/type failures unchanged. GTK remains the default frontend.

## Qt wind-only routing workflow

- Status: Qt UI is fork-only; the UI-neutral routing job is an upstream candidate.
  Branch: `feature/qt-routing`, started from `dev` after approved merges of fork
  PRs #5 and #6. No pending branch dependency.
- Review: [fork PR #7](https://github.com/evanlivingston/gweatherrouting/pull/7),
  merged into `dev` with user approval (merge `d24a092`).
- Scope: add a Routing dock with polar/algorithm selection, local departure time,
  start/destination coordinates, boat-position and saved-POI shortcuts, map
  context actions, coastline checks, background calculation/progress/cancel,
  automatically saved results, a result selector, fit-to-route and a waypoint
  table with map highlighting. Existing saved routes use the shared Core model.
  Cancellation/errors/window closure retain prior routes and drop late results.
- Functional commits: `ca88f0a` (shared routing job and engine/sampling tests),
  `3943d63` (Qt workflow and isolated integration coverage). Follow-up commits
  `f0cb4c9` and `3823537` keep compact map/table labels visible and handle the
  nullable Qt header API. Final focused tests and scoped mypy pass.
- Fidelity: uses Core's existing wind-only routing algorithm, defaults, source
  priority, interpolation, coastline hooks and result serialization. A direct
  constant-wind calculation matches the existing engine's route points exactly;
  real raster sampling matches at four times including fractional hours. Private
  GDAL handles and two-hour job-local caches are closed on success/error/cancel.
  Existing arrival tolerance is retained, so the last generated point can be
  before the destination; the UI displays its time and keeps the requested
  destination visible when fitting, without synthesizing a final route leg.
- Validation: full suite passes 769 tests and 73 subtests. Sixteen isolated Qt
  checks include actual engine completion, distinct result names, result/waypoint
  selection, map fitting/highlighting, cancellation, shutdown, prior-result
  retention, missing wind and invalid positions. Temporary on-disk route storage
  reloads through the same collection used by GTK, retaining points, active route
  and provenance. Four toolkit-independent job tests cover parity, sampling,
  cancellation and absent forecasts. The final targeted rerun passes after
  improving the empty-wind error message and algorithm label. Changed-source
  Black/Flake8 (including pep8-naming) pass; mypy retains the same 32 baseline
  errors in eight files, with no new errors. Native macOS QA calculated against
  installed forecasts and inspected charts, the dock, points and route display;
  user settings writes were disabled and GTK was absent from the Qt process.
- Limits: wind-only with existing algorithm defaults. Current-aware routing,
  automatic downloads, parameter editing, scenario comparison, track destinations
  and route editing/export remain pending. The default launcher remains GTK.
- Intended upstream scope: extract the cancellable routing job separately if
  useful upstream; keep Qt controls and fork migration documentation out of
  parent-project contributions. No upstream publication is authorized here.
- Assessment and launch instructions: `docs/qt-first-pass.md`.

## Qt visual integration

- Status: Qt presentation is fork-only; the shared route-speed palette extraction
  is an upstream candidate. Branch: `feature/qt-visual-integration`, dependent on
  `feature/qt-routing` and fork PR #7. After #7 merged, retargeted to `dev`
  and verified the focused presentation diff before integration.
- Review: [fork PR #8](https://github.com/evanlivingston/gweatherrouting/pull/8),
  merged into `dev` with user approval (merge `c246cfc`).
- Scope: align the optional Qt workspace with GTK's dark presentation using a
  dark Fusion palette, rounded controls, blue accents, consistent spacing,
  symbolic icons and dark Matplotlib plots. Replace the fixed map/plots split
  and two opposing sidebars with full-size Charts/Logs views and grouped NMEA/
  Routing panels on the right. Keep native window controls and filesystem
  dialogs. Routing separates Plan and Results, automatically shows completed
  results and keeps progress/Calculate/Cancel outside the scrolling form.
  Loading a log selects Logs; routing calculation selects Charts. Native File/
  Navigate menus, shortcuts and toolbar panel access make the migrated views
  easier to reach. Dock floating/hiding remains available.
- Functional commits: `ba0abe1` (shared route speed colors; GTK compatibility),
  `1acd87c` (Qt theme, plots, view/sidebar integration and regression coverage),
  `916ec79` (compact form wrapping and bounded combo sizing).
- Fidelity: map route segments use GTK's existing speed thresholds/colors;
  NMEA tracks use the shared GTK log-track style. Plot series use GTK's speed,
  true-wind and heading colors with amber cursors. Chart palettes remain
  independent of the UI theme. Production Cairo chart/wind rendering, source
  selection, projection, routing calculations and playback data are unchanged.
- Validation: 769 repository tests and 73 subtests pass. The optional Qt wrapper
  executes seventeen isolated checks, including view/panel transitions while
  retaining playback, full curves, map position and chart palette; results-page
  selection, route-speed data and visible compact-window Calculate/Cancel.
  Changed-file Black/Flake8 including naming checks and scoped Qt mypy pass.
  Full local mypy retains 32 baseline errors in eight files, with no new errors.
  Native macOS screenshots checked routing results, Charts, Logs, dark plots,
  transport glyphs, native window buttons and the Plan form at 1280×820 and
  1000×650. At 1000×650 both action buttons are visible without scrolling the
  form; final native and isolated checks also verify zero horizontal scrolling.
  A real forecast route and 180-point playback ran without GTK imports;
  user settings writes were disabled during QA.
- Limits: platform fonts/decorations still differ; Qt workspace persistence,
  unmigrated tools and compact traffic-light/toolbar layout remain pending.
  GTK remains the default launcher. No upstream publication is part of this task.
- Assessment and launch instructions: `docs/qt-first-pass.md`.

Integration verification for fork PRs #7 and #8: the final targeted run of
`test_qt_preview.py`, `test_geomaplayer.py` and `test_routingjob.py` passes
11 tests and 9 subtests, including the isolated Qt integration checks. The
retargeted presentation diff passes `git diff --check`. GitHub builds pass;
CI test collection still lacks `nmeatoolkit`, and lint/type jobs retain the
previously documented GTK complexity and four type errors. Both PRs are merged
into `dev`; no upstream PR was created or updated.

## Qt connection settings and data library

- Status: Qt UI is fork-only. Connection configuration validation, replacement
  lifecycle and nonblocking status snapshots are an upstream candidate.
  Branch: `feature/qt-settings-data`, started from `dev` after approved merges
  of fork PRs #7 and #8. No pending branch dependency.
- Review: [fork PR #9](https://github.com/evanlivingston/gweatherrouting/pull/9),
  merged into `dev` with user approval (merge `ac8beef`).
  Merge audit confirmed CI failures remain the missing `nmeatoolkit` dependency,
  existing GTK complexity and four existing type errors; all builds pass.
- Scope: serial/TCP/UDP connection add/edit/remove with draft Apply, legacy N2K
  format loading, input-only N2K validation and offline-safe persistence; Qt
  display preferences; a Forecasts/Charts library with local imports, enable
  flags, removal and provider downloads. Native system pickers remain the
  default. Start/reconnect work, forecast I/O and chart indexing run in workers;
  chart/forecast model installation and Qt notifications run on the UI thread.
- Functional commits: `28417f0` (shared connection configuration/lifecycle and
  unit tests), `f66165b` (Qt settings/library, background work and integration
  checks). Follow-ups `e86df9c` (palette-raster validation) and `26c072d`
  (stable chart ordering) pass the final isolated Qt reruns.
- Fidelity: preserves existing instrument data sources, GRIB source priority,
  wind sampling, chart rendering and routing calculations. Connection edits
  use the same storage as GTK. GRIB imports stage and validate before installing
  library copies, preserving original files and adding timestamp suffixes on
  filename collisions. Chart toggles/reimports preserve layer ordering.
  NOAA GFS/OpenSkiron use existing provider APIs; GFS bounds
  are explicit. NOAA ENC uses the existing coverage catalog/cache and atomic
  exchange-set installer. Imported chart metadata and enable flags use GTK's
  settings schema. Chart removal retains files; forecast-copy removal asks
  confirmation and protects externally opened files. Base charts are read-only.
- Validation: full suite passes 776 tests and 79 subtests. The optional Qt wrapper
  runs 24 isolated checks, adding shared disk reloads, legacy N2K editing/offline
  saves, forecast import/toggle/removal/collisions, real vector/raster indexing,
  palette callback attachment, staged provider failure/success, NOAA coverage,
  connection-worker responsiveness with playback and late-completion rejection.
  Final scoped Black/Flake8 and mypy pass; full local mypy retains the same 32
  baseline errors in eight files. Native macOS QA checked Settings/Display and
  both library pages with installed data and compact chart actions; GTK remained
  absent and user settings writes were disabled. Provider responses and physical
  instrument hardware were not tested live. Existing CI dependency/lint/type
  failures remain documented above.
- Limits: provider-specific timeout/cancellation behavior is retained; no new
  download Cancel control. Base GSHHS installation, polar management, current-
  aware routing and workspace persistence remain separate migration slices.
  GTK remains the default frontend. No upstream publication is authorized.
- Intended upstream scope: extract connection validation/replacement/status
  handling independently if useful. Keep Qt controls and fork documentation
  out of parent-project submissions.
- Usage and assessment: `docs/qt-first-pass.md`.

## Qt workspace parity with GTK

- Status: Qt UI is fork-only; the toolkit-neutral polar renderer extraction is
  an upstream candidate. Branch: `feature/qt-workspace-parity`, based on `dev`
  at `77a9e2b` after the approved merge of fork PR #11. No pending dependency
  or upstream submission.
- Review: [fork PR #12](https://github.com/evanlivingston/gweatherrouting/pull/12),
  merged into `dev` with user approval (merge `f5f18d6`). Merge audit confirmed
  the existing connection-dialog complexity and four baseline CI type errors;
  docs passed, with build/test jobs still running at integration. Local validation
  is recorded below.
- Functional commits: `7f34bb8` (shared production polar renderer and GTK
  adapter), `1bec52e` (Qt workspace, panels, plots and preference integration).
- Scope: preserve GTK's Charts/Logs/Polars workspace organization and contextual
  toolbars. Polars shows its speed table beside the full-circle chart, with
  shared-dock Polar manager, Polar selector, NMEA and Performance panels.
  Performance uses the shared active history, STW/SOG basis and wind-band policy;
  clearing performance samples retains NMEA logs and plots. Logs shows an
  independent map above full-data plots, GTK series switches/colors and local
  time labels, with independent forecast time and camera. Preferences retain
  both map views, splits, series switches and Polars choices; older Qt toolbar
  layouts migrate to the new workspace header.
- Fidelity: original Cairo polar curves, colors, grid, mirroring, sample rendering
  and hover logic are shared without changing formulas or constants. Six direct
  baseline comparisons at three sizes, with and without hover/measured samples,
  produced zero changed pixels. Table row stride and speed values match GTK;
  polar parsing, enabled flags and routing calculations remain unchanged.
- Validation: full suite passed 783 tests and 84 subtests before the final
  manager-dock/time-label polish; the final Qt wrapper passed all 38 isolated
  checks. Targeted GTK performance/manager and Qt checks passed (14 tests and
  three subtests). Scoped Black/Flake8 and mypy pass; full mypy retains 32
  baseline errors in eight files. Native macOS GTK/Qt comparison verified the
  Polars table/chart, Performance and Logs layout at 1280×820; Qt also fits
  1000×650. QA disabled user-settings writes and used temporary workspace paths;
  GTK was absent from the Qt process. Live hardware was not exercised.
- Limits: remaining POI/track/AIS tools, marine overlays, dashboard, forecast
  controls and routing parameters are listed in `docs/qt-parity.md`. Qt native
  chrome/fonts differ; this is structural parity, not complete UI parity.
  GTK remains the default launcher. Automatic recording, retained playback
  plots/cursors and native filesystem pickers remain in place.
- Intended upstream scope: the shared polar renderer can be reviewed separately
  from Qt UI and fork migration documentation. No upstream publication is
  authorized by this slice.
- Usage and assessment: `docs/qt-first-pass.md`, `docs/qt-parity.md`.

## Qt workspace persistence

- Status: fork-only Qt UI. Branch: `feature/qt-workspace`, started from `dev`
  after the approved merge of fork PR #9 and its integration tracking update.
  No pending branch dependency; no upstream submission is part of this slice.
- Review: [fork PR #10](https://github.com/evanlivingston/gweatherrouting/pull/10),
  merged into `dev` with user approval (merge `6d08001`).
  Merge audit confirmed the existing dependency/lint/type CI failures only;
  all build jobs pass.
- Scope: restore and autosave normal/maximized window bounds, dock placement/
  sizing/visibility, detached panel bounds, Navigation toolbar visibility,
  active sidebar and Charts/Logs view, map center/zoom, routing view/polar/
  algorithm/coastline preferences and selected saved route, NMEA speed and
  live-stream preference. View can reopen hidden controls or reset the layout.
- Functional commit: `0014dd1` (Qt preferences, viewport notifications, close/
  application-quit flushing and five isolated integration checks).
- Storage: reuse `WorkspaceStateStore`'s versioned atomic writes in a separate
  `workspace-qt.json`; GTK retains `workspace.json`. Save after 750 ms idle,
  defer during map drag/animation, skip identical states and flush on close.
  Failed writes retain the previous file and can retry. Qt layout decoding is
  bounded/validated; malformed data falls back safely. Main/detached bounds are
  fitted to available monitor work areas. Maximization waits for native show
  so returning to normal mode retains the saved size.
- Fidelity: no calculation, forecast, instrument or chart-rendering changes.
  Preferences exclude loaded log paths/samples, playback cursor, recording state,
  route draft coordinates/departure and running jobs. Restoration does not
  reopen logs or start recording/playback/calculation. A route selection is a
  name referencing the existing shared route collection.
- Validation: full repository suite passes 776 tests and 79 subtests. The Qt
  wrapper now runs 29 isolated checks. New checks cover disk restart with hidden/
  floating docks and toolbar, normal/maximized bounds, selected views/options,
  missing monitors and malformed fields/layout, reset recovery, debounce during
  drag/playback, atomic save failure/retry and exclusion of instrument history.
  Final targeted run passes after adding toolbar recovery coverage. Changed-file
  Black/Flake8 and scoped Qt mypy pass; full mypy retains 32 baseline errors in
  eight files. Native macOS QA reopened the workspace repeatedly and verified
  1000×650 normal bounds, active Routing/Results, map 48.3/-123.5/9.5, detached
  430×500 panel bounds and maximize-to-normal restoration. GTK was absent and
  user settings writes were disabled; workspace writes used a temporary path.
- Limits: layout is Qt-specific rather than translated between frontends. Dialog
  positions, log reloads and route drafts are not restored. GTK remains the
  default launcher; polar management and current-aware routing are pending.
- Intended upstream scope: keep Qt UI/preferences and migration documentation
  fork-only unless the parent adopts a Qt frontend. The shared atomic store is
  reused without changes.
- Usage and assessment: `docs/qt-first-pass.md`.

## Qt polar management

- Status: Qt UI is fork-only. Shared polar-library lifecycle fixes and validated,
  staged file/provider helpers are upstream candidates. Branch: `feature/qt-polars`,
  started from `dev` after the approved merge of fork PR #10 and its tracking
  update. No pending branch dependency; no upstream submission is authorized.
- Review: [fork PR #11](https://github.com/evanlivingston/gweatherrouting/pull/11),
  merged into `dev` with user approval (merge `f52fc91`).
  Merge audit confirmed existing dependency/lint/type failures only; builds pass.
- Scope: Data Library Polars page, File → Polars and Routing → Manage polars;
  native `.pol` imports, enable/remove controls, curve/full-table previews,
  enabled-polar routing selection and searchable ORC catalog/downloads. Provider
  work and parsing run in workers; shared manager updates run on the UI thread.
  Mutation controls serialize against data operations and route calculations;
  window shutdown drops late notifications and model installation.
- Functional commits: `782ae24` (shared manager/file lifecycle and seven tests),
  `690557b` (Qt workflows and four additional isolated integration checks).
  Follow-up `f422ced` defers table refresh until checkbox events finish;
  the final Qt/shared tests pass with actual checkbox toggles.
- Selection fix: the previous shared refresh appended every local file to both
  installed and enabled lists. Refresh now rebuilds the installed index without
  duplicating or enabling files; explicitly empty enabled selections survive
  restart. Deletes remove enable flags/metadata and stage the file for recovery
  if persistence fails. Qt routing now lists enabled files only, refreshes after
  changes and preserves an available selection; no enabled file disables Calculate.
- Fidelity: imports preserve bytes and use the same routing engine parser and
  interpolation. Validation rejects invalid axes, ragged tables and negative/
  nonfinite speeds before installing. Collision suffixes preserve existing files.
  Sixteen interpolation probes and full table/axis comparisons match the original
  file exactly. ORC conversion reuses `json_to_polars` unchanged, including its
  rounding/endpoints; invalid converted tables are rejected. Requests use the
  existing ORC endpoints with explicit timeouts. Preview curves use stored points
  directly; no calculation formulas, efficiencies or approximations change.
- Validation: full suite passes 783 tests and 84 subtests. The Qt wrapper runs 33
  isolated checks, including native-picker import, shared disk reload, immediate
  route-selection updates, exact preview/table values, ORC proxy filtering and
  download metadata, network failure, mutation blocking and late-close delivery.
  Shared tests cover duplicate refresh, empty enabled reload, collision-safe
  imports, unchanged interpolation, invalid data, failed-copy/delete recovery,
  ORC conversion and retaining symlink targets. Scoped Black/Flake8 and mypy pass;
  full mypy retains the same 32 baseline errors in eight files. Native macOS QA
  inspected library/search, curves, full table and selecting an imported polar
  for Routing. User storage writes were disabled, polar/workspace writes used
  temporary paths and GTK was absent. Live ORC responses were not exercised.
- Limits: ORC catalog is fetched on demand without a persistent catalog cache;
  requests have timeouts but no new download Cancel control. Live polar
  performance, efficiency controls, polar editing/export and current-aware
  routing remain pending. GTK remains the default frontend.
- Intended upstream scope: prepare focused shared library/file-validation changes
  separately from Qt controls and fork documentation. No upstream publication
  is part of this migration slice.
- Usage and assessment: `docs/qt-first-pass.md`.

## Qt chart navigation

- Status: Qt controls and migration documentation are fork-only. Shared GTK
  navigation/AIS renderers and routing summary are upstream candidates. Branch:
  `feature/qt-navigation`, based on `dev` at `6fa2aad` after the approved merge
  of fork PR #12. No pending dependency or upstream submission.
- Review: [fork PR #13](https://github.com/evanlivingston/gweatherrouting/pull/13),
  merged into `dev` with user approval (merge `bebac38`). Build and docs jobs
  passed. Merge audit confirmed the existing missing-`nmeatoolkit` test-environment
  failure, connection-dialog complexity warning and four baseline CI type errors;
  the other test-matrix jobs were cancelled. Local validation is recorded below.
- Functional commits: `8ec23d3` (shared production Cairo route/track/POI/AIS
  portrayal and routing summary, preserving GTK adapters/import paths),
  `45110ee` (Qt Navigation panel, map actions, native GPX/PFEC workflows and
  workspace preferences).
- Scope: GTK's Tracks/Routings/POIs/AIS tab order in a left Navigation panel;
  Charts-only visibility with the Tabs toggle, draggable/floating docks and
  restart preferences. Shared collections support editable names/visibility,
  active tracks, waypoint addition/order/duplication/removal, map placement and
  Escape/view-change cancellation, route selection/summary/waypoints/removal,
  POI routing-destination refresh and fit actions. GPX parsing/validation runs
  on a worker, installs tracks/waypoints on the UI thread and drops late close
  completions. Native import/export pickers use shared converters/GPX objects.
  AIS uses existing active-target expiry, GTK distance ordering and category
  colors, with map visibility for both Charts and Logs.
- Fidelity: no routing formulas, navigation coordinates, JSON schemas or AIS
  decoding/source selection change. Track/POI/routing/AIS drawing reuses the
  original GTK code, including styles, route-time interpolation, exclusion marks,
  marker orientation/vectors and label spacing. Twelve direct comparisons to
  pre-extraction GTK at three sizes and two pixel scales, with/without selection,
  produced zero changed pixels. Track distances display two decimals with nm;
  stored coordinates and underlying length calculations retain full precision.
  Routing summaries and PFEC conversion retain the existing GTK behavior.
- Validation: full suite passed 783 tests and 84 subtests; final Qt wrapper passed
  all 43 isolated checks, followed by a passing targeted route-waypoint selection
  check after the final signal connection. Five new checks cover track/POI/map
  editing/cancel, shared disk reloads, native-picker GPX import/export, invalid
  coordinates, route controls, AIS ordering/expiry, workspace restart and late
  import shutdown. Scoped Black/Flake8 and mypy pass; full mypy retains 32 baseline
  errors in eight files. Native macOS GTK and Qt comparison verified the left
  navigation/tab organization and all four Qt tabs at 1280×820; Qt also fits
  1000×650. A stacked Navigation/Routing layout regression was caught and fixed
  with scrolling content before the final checks. QA disabled user-settings
  writes and used temporary workspace/navigation storage or in-memory fixtures;
  GTK was absent from the Qt process. Live hardware was not exercised.
- Limits: GPX route elements are not imported, matching the existing GTK importer;
  tracks and waypoints are supported. Download/provider controls, marine layers,
  dashboard, forecast shortcuts and advanced routing remain separate slices.
  The existing Routing plan/results dock remains available alongside the new
  navigation results tab. GTK remains the default frontend; native Qt widgets
  and table formatting differ while workspace structure/data remain aligned.
- Intended upstream scope: extract the shared portrayal/summary separately from
  Qt controls and fork documentation if authorized. No upstream publication is
  part of this slice.
- Usage and assessment: `docs/qt-first-pass.md`, `docs/qt-parity.md`.

## Qt map instruments and cursor wind

- Status: Qt controls/preferences and migration documentation are fork-only.
  Shared dashboard/gauges, responsive layout and cursor worker lifecycle are
  upstream candidates. Branch: `feature/qt-map-instruments`, based on `dev`
  at `c837717` after the approved merge of fork PR #13. No pending dependency
  or upstream submission.
- Review: [fork PR #14](https://github.com/evanlivingston/gweatherrouting/pull/14),
  merged into `dev` with user approval (merge `748e5c7`). Merge audit confirmed
  the existing missing-`nmeatoolkit` test-environment failure, connection-dialog
  complexity and four baseline CI type errors. Docs and completed builds passed;
  macOS/Python 3.10 builds were still running at integration.
- Functional commits: `e80d964` (shared production gauges/dashboard and injectable
  cursor scheduling, retaining GTK adapters), `85f3aa4` (wrap all gauges on narrow
  maps, shared by GTK/Qt), `55c53ff` (private cursor forecast metadata and explicit
  dataset release), `b4df8f3` (Qt instrument/readout integration and preferences).
  Validation follow-up `6e6668c` probes distinct raster cells as well as times.
- Scope: GTK's seven instrument gauges and connection summary on Charts/Logs;
  Core playback/live readings, retained Stop readings and source-reset clearing.
  Independent Dashboard/Grib toggles persist for both maps. Immediate pointer
  coordinates and debounced asynchronous forecast wind appear in the status bar;
  connection refresh leaves an active readout visible. Forecast/source/view changes,
  map motion/leave/hide, point placement and shutdown cancel stale queries.
  Instrument updates reuse chart images and status polling uses the existing
  nonblocking connection snapshot.
- Fidelity: reported true wind and the original apparent-wind/SOG fallback,
  units, color thresholds and missing/zero handling are retained without changing
  recorded data. Sixteen comparisons against original GTK gauge/dashboard code
  at two sizes, two pixel scales and four states produce zero changed pixels on
  wide maps. Narrow maps intentionally wrap the same gauges in order instead
  of clipping the fixed row. No routing, forecast sampling or source-order changes:
  the cursor retains GTK enabled-file order/fallback and the 1.94384 knots
  conversion; the wind overlay retains its separate newest-run ordering.
  Twelve time/position probes across distinct real raster cells match legacy
  wind values exactly. Each cursor worker owns at most two private files/four
  array hours per file; eviction and shutdown release datasets, avoiding the
  inherited shared forecast-hour cache retaining the last handle.
- Validation: full suite passed 791 tests and 101 subtests before final status-line
  visibility polish; final Qt wrapper passed all 49 isolated checks afterward.
  The raster-gradient follow-up passed three tests and 12 subtests. Shared dashboard/
  cursor/GTK tests passed 20 tests and 17 subtests; existing GTK workspace/cursor
  checks also passed. Six new Qt checks cover playback/Stop/reset readings, chart
  image reuse, independent map preference restart, hidden/moving query suppression,
  worker/UI delivery and debounce, stale-result cancellation, real forecast values,
  blocked-worker close and visible cursor text during refresh. Scoped Black/Flake8
  and mypy pass; full mypy retains 32 baseline errors in eight files. Native macOS
  GTK/Qt comparison checked wide/wrapped dashboards, Logs, toggles and synthetic
  pointer readout on a real local GDAL fixture at 1280×820 and 1000×650; Qt imports
  no GTK and closes its cursor cache. QA disabled user-settings writes and used
  temporary workspace/raster data. Live hardware/providers were not exercised.
- Limits: marine cursor values/overlays, forecast timeline shortcuts, advanced
  routing parameters and full GTK control parity remain separate slices. Cursor
  file priority intentionally follows existing GTK cursor behavior rather than
  changing it to the wind overlay's issue-time ordering. GTK remains the default
  frontend; both processes continue to run independently.
- Intended upstream scope: review extraction, responsive dashboard and cursor
  resource fixes independently from Qt UI and fork documentation if authorized.
  No upstream publication is part of this migration slice.
- Usage and assessment: `docs/qt-first-pass.md`, `docs/qt-parity.md`.

## Qt forecast timeline and floating panel integration

- Status: shared forecast metadata/stepping is an upstream candidate; Qt controls,
  panel chrome/theme and migration documentation are fork-only. Branch:
  `feature/qt-forecast-timeline`, based on `dev` at `e9555fe` after the approved
  merge of fork PR #14. No pending prerequisite or upstream submission.
- Review: [fork PR #15](https://github.com/evanlivingston/gweatherrouting/pull/15),
  open and unmerged for human review. CI was still running at handoff.
- Functional commits: `0a54892` extracts GTK's enabled-source intervals, union
  bounds, step choices and bounded/unbounded stepping without changing semantics;
  `949b7b7` adds the Qt timeline/preferences and integrates the other agent's
  floating panel chrome, scrolling/theme and Window menu changes. That integration
  also fixes child-relative resize coordinates, duplicate resize helpers, lint/
  typing issues and teardown callbacks accessing already-deleted Qt objects.
- Scope: GTK-ordered forecast transport and step interval, local-time picker and
  Charts-only range slider on the existing toolbar row. Play advances once per
  second and pauses at the endpoint; slider changes coalesce at 75 ms. Metadata
  bounds include enabled GRIBs, marine fields and stations, support dates beyond
  2038, and do not sample/open data. Manual dates outside the range remain valid
  with a caption. NMEA transport/recording and routing departure stay independent.
  Restore retains only the step interval, with no autoplay or selected-time restore.
  Floating panels retain dock tabs/splits, add compact headers, edge resize/snap,
  double-click docking and Window → Reset Panels. Thin scrolling bars and the
  unfocused dropdown/spinner wheel guard are included, with dark native macOS
  appearance matching the existing dark palette. Filesystem dialogs remain native.
- Fidelity: shared bounds/stepping retain GTK's local time, hourly Now rounding,
  elapsed-time bounded steps and wall-time unbounded steps, including DST folds.
  Union bounds can include gaps; they do not promise continuous coverage. No
  forecast interpolation, route calculation or source priority changes.
- Validation: full suite passes 794 tests and 101 subtests; final Qt wrapper passes
  all 57 checks and exits normally after the destruction regression test was
  strengthened. Shared timeline/GTK/time-control checks pass 31 tests. Scoped
  Black/Flake8 and mypy (23 files) pass. Full mypy retains 32 baseline errors in
  eight existing files. Sphinx was unavailable locally; documentation CI is pending.
  Native macOS Qt checked 1280×820 and 1000×650, metadata
  ranges, scrubbing, endpoint pause, unchanged routing departure, manual outside
  dates, hidden Logs controls, floating panel/reset and clean shutdown; GTK
  reference checked at 1280×820. QA used temporary workspace/raster data with
  user-settings writes disabled. Live hardware/providers were not exercised.
- Limits: marine controls/overlays, broader settings and advanced routing remain
  pending. GTK remains the default frontend and retains its controls. Floating
  snap behavior has not been validated on Linux/Windows or multiple monitors.
- Intended upstream scope: consider the shared forecast extraction separately
  from Qt UI and fork documentation if authorized; no upstream publication is
  part of this slice.
- Usage and assessment: `docs/qt-first-pass.md`, `docs/qt-parity.md`.
