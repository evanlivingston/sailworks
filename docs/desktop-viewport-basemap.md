# Desktop viewport and world basemap (#88)

## Bounded implementation and acceptance contract

- Scope: normal Qt window, existing viewport host, world land/sea/coastline
  composition without weather or ENC; pan/zoom/resize, weather controls and
  navigation/route overlays; visible missing-data and loading errors.
- Non-goals: custom ENC engine, tile57, old-map removal, routing/sampling or
  forecast-selection changes, new rendering approximation, performance claims.
- Reference: current `dev` application wiring and the recorded #84 failure.
  Use installed local basemap/weather in an isolated settings/data sandbox;
  do not download data, access credentials or change the user's saved state.
- Gates: focused composition/presenter tests; changed-file Black/whitespace;
  one gate-runner command exercising the full `MainWindow`, capturing review
  images, current camera/overlay alignment, no-weather display, missing-data
  reporting and weather/navigation controls. Readbacks prove content, not FPS.
- Budget: three targeted validation rounds and two native app processes, each
  at most 120 seconds; one implementation pass before reevaluating scope.
  Stop if the budget is exhausted, full-app validation cannot run, or the fix
  requires ENC work, shared sampler/routing changes or renderer replacement.
- Gate decisions and results will be recorded here before claiming completion.

## Checkpoint: stop — fixture integrity blocker

- Focused synthetic composition: 6 passed (native DPR 1/2, stationary,
  fractional and integer pan). This does not reproduce #84 and does not prove
  application correctness; do not implement the suspected device-scale fix
  without reproducing it.
- First full-window command returned **NOT-EVALUATED**, exit 2, before creating
  the Qt application. The existing `wind-all-points-fixture-2026-10-21` snapshot
  fails `verify_snapshot`: `Startup snapshot has changed; capture a new baseline`.
  No baseline was recaptured and verification was not bypassed.
- Evidence: `desktop-basemap-88-1/desktop-basemap.tool.log` under the approved
  OpenCode temporary directory. No native window was launched, no content gate
  passed, no rendering performance measured, no product behavior changed.
- Decision: **stop** pending a verified fixture or authorization to prepare a
  replacement functional fixture (not a replacement performance baseline).
  The new gate is incomplete scaffolding: navigation/route alignment and
  missing-data checks still need implementation; Black/whitespace and broader
  targeted validation have not run. No completion, commit or PR is claimed.

## Continuation outcome

User authorized a fresh functional fixture and requested minimal testing/docs.
The full desktop window already displays the basemap: no speculative Cairo fix
was applied. Added visible missing-coastline/fallback and loading-failure messages.
Final gate: **10/10 passed**; focused tests: **17 passed, 14 subtests passed**;
Black (six files) and whitespace passed. Screenshots and raw results are in
`desktop-basemap-88-final/` under the approved OpenCode temporary directory.
Decision: adopt the focused error-reporting change; await interactive review.
An existing download-coverage banner can say “No weather models selected” while
the explicit display forecast is rendering; this is not changed forecast policy.
ENC remains untouched. No performance or wider platform acceptance is claimed.

User review rejected the result: no visible basemap. The earlier checks used
QWidget grabs, which repaint the widget tree and do not prove on-screen
composition. Reopen acceptance; bounded continuation: one on-screen reproduction
and one corrective native run, no broad suite or extra documentation program.

User requested code reasoning instead of visual inspection. The embedded Quick
scene previously had no base image and relied on the parent QWidget painting
through it. It now receives the retained base image below chart/weather layers,
with the same camera transform and explicit provider cleanup. Focused handoff/
lifecycle tests: 15 passed; changed-file Black and whitespace passed. This repair
has not yet been validated in the user's visible window; acceptance stays open.
