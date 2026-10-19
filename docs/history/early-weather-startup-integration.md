# Rendering performance: application integration checkpoint

> **Status: HISTORICAL.** The M0/A0l integration checkpoint for the opt-in
> early-weather start-up slice, written 2026-10-19. Superseded by
> [ADR-001](../adr-001-grib-rendering-architecture.md) and issues #62-#70. Read for
> evidence; do not follow as a plan.

Current milestone: M0/A0l, opt-in early-weather startup on `dev` (1f14649).

This is the integration checkpoint for the early-weather startup slice. The
canonical plan and decision ledger for the wider rendering effort is
[the canonical performance plan](../rendering-performance-plan.md); the earlier
A0i-A0k experiment reports and their evidence are preserved alongside it.

## Integration contract

- User authorized real application integration, not enabling it by default.
  Hypothesis: create the actual Charts workspace before unused plot imports and
  stage chart attachment after current weather publication. Preserve map, camera,
  source selection, saved settings and normal weather portrayal; no routing math,
  new forecasts, missing-data substitutions or production shader changes.
- Initialization attribution on the verified current fixture found 18.04/25.01 s
  in imports (Matplotlib/font initialization), 5.25 s controller, 0.83 s window,
  0.89 s show/events. The earlier 28 s "chart host ready" was NOT chart-only time.
  This revises the integration hypothesis: retain the existing exact application
  weather worker, defer unused plot UI, and stage existing GUI-owned chart loaders
  across event turns. Do not import experiment tools or unmerged #50 modules into
  production, transfer GDAL handles across threads, or claim retained GPU transport
  has been integrated. This is a narrower first application slice.

  > **Status update (2026-10-19):** PR #50 was merged to `dev` via `b9e2471` / PR #61
  > `1a90515`. The integration contract above was recorded when it was still
  > unmerged; the measurements are unchanged.
- Baseline: approved private fixture `1ed1bca1...`, 22 sources, October 19 08:00,
  saved very-dense barbs/hide-over-land and layout. Actual validation on this new
  `dev`-based worktree is separate from old-branch timings/array evidence.
- Gates: flag off preserves eager startup behavior; flag on creates actual full
  Qt window/controls with explicit status, current weather before chart attach,
  atomic staged chart publication without resetting map/time/weather, bounded
  empty/error wait, close cancels pending callbacks, routing/library guarded by
  existing map-data busy state until chart initialization finishes. Lazy Logs
  state (data, series, cursor) is retained until opened. No screenshot/readback
  timing is presentation FPS; report GUI stalls honestly.
- Budget (continues A0l's prior contract): two initialization diagnostics <=120 s,
  three implementation/targeted-check iterations, three actual-app workloads
  <=120 s, 60 minutes wall time total. One attribution diagnostic already used.
  Stop if portrayal/routing policy must change, resources cannot remain correctly
  owned, or a larger subsystem rewrite is necessary. Reevaluate continue/narrow/
  pivot/stop at gate; no commits/pushes/PRs/merges authorized.

## Status

Opt-in application startup/lifecycle slice passes repeated matched default/opt-in
pairs, a real close-pending run and a Logs-open run (all exit 0). On the current
`dev` base, first window is observed at 3.72 s opt-in versus 21.08 s default and
first weather at 6.76 s versus 24.50 s, with exact weather-array/surface hashes
and source/view/clock preservation across the matched pair. All 39 saved charts
attach by 9.08 s without replacing weather/camera. Opening Logs later
materializes deferred plots (3 axes) and imports Matplotlib then, costing 28.47 s
in a cold sandbox that must build its font cache. These are isolated cold-cache
observations, not process-launch or presentation performance. Final targeted
validation on this branch: 15 tests passed, 1 skipped, 2 subtests passed; Black
and `git diff --check` (including new files) clean. The isolated Qt preview
suite is excluded because it times out on the base branch as well.

**Decision: adopt the opt-in application startup slice locally; stop this gate.**
The retained numerical/GPU architecture is still experimental and NOT integrated
as a main-application backend. Individual chart loaders/portrayal remain capable
of blocking GUI work; next work should target measured controller/chart costs,
not silently transplant the research harness. The branch is committed and pushed
for review in the fork; it is not merged.
See [integration behavior, evidence and limits](../early-weather-startup.md).
