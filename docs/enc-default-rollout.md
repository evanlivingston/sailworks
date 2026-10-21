# Default application ENC rollout (#70 slice)

User authorized enabling ENC in the normal application viewport, retaining legacy
recovery. Baseline: dev d56dbc6, #78 functional and combined-experience evidence.
This is not closure of the #70 soak/platform/presentation program.

## Contract before implementation/runs

- Move only ENC presentation into the existing Quick child. Preserve world,
  raster/CHS, MBTiles, weather background/isobars, marine and navigation ordering;
  never cover those layers with the standalone viewport's coastline composition.
- The viewport worker prepares an ENC-only transparent image. An interstitial
  image contains the existing above-ENC CPU layers (MBTiles, weather background,
  marine), placed between ENC and barbs. Navigation remains above both.
- Honor chart switches/palette, Weather-panel selection and independent map time.
  Source replacement must safely replace weather independently of ENC. The
  enc-only mode can retain the chart worker because it never samples isobars or
  borrows weather sources; full standalone chart composition still requires
  whole-child replacement. Charts work without a selected forecast or
  visible wind. Legacy recovery/unsupported backends keep the existing full map.
- Non-goals: routing/sampling changes, new portrayal, full catalogue/platform
  campaign, pixel matching, FPS claims, default GPU fill or deleting legacy code.
- Workload: existing verified private fixture and native application TrackMap,
  covered Puget view; startup, pan/zoom/resize, forecast time/source changes,
  empty selection, wind hidden, chart switch and legacy recovery.
- Gates: targeted presenter/layer-split/lifecycle tests, changed-file Black and
  whitespace checks, one-command native app-rollout gate. Existing static/native
  composition evidence is reused, not remeasured as a baseline.
- Budget: three targeted fix rounds, two native processes <=110 seconds. Stop
  and report if exhausted, fallback loses content, or additional scope is needed.
  Report accounted image bytes separately from unmeasured RSS/VRAM; process wall
  spans are not presentation latency/FPS. Record adopt/revise/reject before done.

The user subsequently authorized committing/pushing this work to open a PR,
not merging it. The old
worktree's local runner configuration is preserved; this worktree also uses the
user-requested free runner locally, excluded from product changes.

## Results / decision: adopt, corrected application check passed

The user authorized one additional native process after the earlier budget stop.
The corrected `enc-app-rollout` gate **passed 13/13**, complete, in 18.143 seconds
process wall time. The readback/current-view guard was active: chart request
counts advanced from 6 (forecast) to 7 (pan/zoom), 8 (resize), 9 (time), 11
(chart reenabled) and 12 (source cleared). All image/provider error strings and
fallback reasons were empty. No forecast/source-clear correctly reported weather
unavailable while ENC remained ready and the weather worker was not busy.

Confirmed: default app ENC without a forecast, explicit forecast + ENC, no
duplicate legacy ENC, interstitial/foreground layers present in their intended
order, current pan/zoom/resize/time views, wind-hidden charts, chart switches,
source clear and legacy recovery. No legacy pixel comparison was required.
Final static evidence remains 140 targeted passes, one known base failure,
changed-file Black/scoped flake8/whitespace clean. No rerun was needed for those
unchanged files. Raw final evidence is `enc-app-rollout-3/` beneath the approved
OpenCode temporary directory.

Decision: **adopt the application wiring**, ready for review. The prior hold and
invalid early transition evidence below are preserved as history, not current
blockers. Implementation is prepared for a PR from `feature/enc-default-rollout`;
the normal app on `dev` is unchanged until merge.
The #70 soak/platform/presentation/RSS/VRAM work is not claimed complete.

### Earlier rounds (superseded hold, preserved evidence)

- Round 1 targeted tests: 137 passed; the unchanged known base import-invariant
  failure. Native app process failed (99.0 s): environment image URLs used the
  chart provider namespace; corrected to a named environment provider. Also fixed
  a real no-forecast startup bug: failed source initialization left its pending
  demand marked busy, indefinitely blocking independent chart preparation.
- Round 2: 139 targeted passes and the same known base failure; Black, scoped
  flake8 and whitespace clean. The app gate reported PASS 13/13 in 15.2 s,
  including native ENC without a forecast, selected forecast + ENC, visibility
  switches, independent layers, source clear and legacy recovery.
- Review found that the gate's pan/zoom/resize/time stages could accept the last
  ready frame before the coalesced application sync ran (unchanged chart-request
  counts). That PASS is **not accepted as complete application-transition
  evidence**. Preserve the raw result; do not infer transition correctness from
  the earlier standalone viewport checks. Readiness now requires the actual
  application camera/time/dimensions, reusable chart image and current environment
  image key, with a regression test for old-camera acceptance.
- Two native processes are spent. No third native process is authorized by this
  contract. Stop measurement and leave the implementation uncommitted in
  `feature/enc-default-rollout`; the normal app on dev remains unchanged. A
  corrected end-to-end native run is still required before claiming adoption.
- Round 3 (static only): **140 passed**, same known base failure; changed-file
  Black/scoped flake8/whitespace passed. A failed source initializer is now
  terminal across subsequent camera changes too, so an empty selection cannot
  become permanently busy again after a pan. New tests reject stale application
  camera and environment-image readiness. No native evidence is claimed for
  these final changes.

Raw evidence: `enc-app-rollout-1/` and `enc-app-rollout-2/` beneath the approved
OpenCode temporary directory. Native spans are whole-process wall time, not FPS.
This does not complete #70 or authorize a merge/default switch into dev.

### Earlier blocker report (resolved by the authorized corrected run)

**Expected:** default app ENC works through current pan/zoom/resize/time/source
transitions, preserving foreground layers and legacy recovery.
**Observed:** implementation and 140 targeted checks are ready; the second native
tool reported PASS, but its transition wait could accept an earlier ready frame.
**Root cause:** app updates are coalesced; readiness must bind to the app's actual
camera/time/dimensions and environment image, not just any ready chart. The wait
is corrected and unit-tested; native budget is exhausted.
**Next concrete step:** authorize one corrected `enc-app-rollout` native process
(<=110 s), then adopt or stop based on that result. Until then, do not merge or
claim default rollout completion.
