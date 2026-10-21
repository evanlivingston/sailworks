# All forecast points in the application (#79)

User authorized adding this focused fix on `feature/enc-default-rollout` to PR
#81, separately committed. Baseline: dd94d28 and the passing default ENC gate.

## Contract before runs

The viewport symbol path explicitly rejects unthinned wind. Choose an explicit
legacy chart/weather presentation for `gribBarbDensity == "all"`, as already done
for streamlines, rather than repeatedly preparing an unsupported viewport demand.
Keep the density setting unchanged. The legacy path retains the selected
file/run/member, clock, missing-data and shoreline rules, opacity and visibility;
its `all` density is unthinned and uncapped. Other densities retain viewport ENC
and barbs; fallback must not leave duplicate overlays or stale wind on switch.

Non-goals: new GPU all-points representation, routing/sampling changes, hidden
thinning/caps, chart redesign, numerical/portrayal relaxations or closing #70.
This trades viewport presentation for the existing complete CPU chart/weather
path while All is selected; make this explicit in the status/UI and PR.

Correctness: unit tests through presenter mode selection and foreground dispatch;
native forecast-node membership including missing and shoreline filtering without
cell thinning/caps. Native application checks: All → thinned → All, nonblank wind,
chart/navigation retained, selected forecast/time identity, opacity and visibility.
Report dense-view point count, preparation/selection wall spans and accounted
bytes separately; no FPS, input-to-present, RSS/VRAM or plateau claims.

Workload: verified existing private snapshot, HRRR wind covering Puget, native
Metal/DPR 2 app. Recorded ENC rollout acceptance is reused, not remeasured as a
baseline. New one-command gate: `wind-all-points`.
Budget: two targeted validation/fix rounds and two native processes <=110 seconds.
Stop on exhaustion, lost charts, changed point membership or broader scope.
Only configured test-runner runs validation. Update PR #81 scope/results after
validation; no merge without approval and do not discard local runner preference.

## Final result — adopt explicit unthinned legacy presentation

After the user authorized a replacement fixture, native `wind-all-points`
**passed 10/10**, complete, in 19.156 seconds whole-process wall time (not FPS).
The dense view drew **1,488/1,488** eligible native nodes, exceeding the normal
900-symbol ceiling without thinning/capping. Shoreline filtering retained
521/521; the panned shoreline view retained 516/516. Each observation matched
node membership and the selected file/run/member, with nonblank wind content.

All → Very dense → All, explicit unthinned status, charts/navigation, opacity
zero, visibility hide/recovery and camera movement passed. Selection + CPU paint
spans were 2.5–8.5 ms for these views; accounted native arrays were 655,644 bytes.
These are limited mechanism observations, not presentation FPS/latency, RSS/VRAM
or memory-plateau acceptance. Twenty targeted tests passed; Black/scoped flake8
and whitespace checks passed. The missing-data/land synthetic test kept all
4,898 eligible nodes from 5,000 inputs, with no cap.

Decision: **adopt** this focused CPU fallback for All. Other densities retain the
viewport; the preference is not rewritten. The density control remains usable
without an endless unsupported-GPU loading loop. This intentionally uses legacy
chart/weather presentation while All is selected, not GPU all-points support.
Evidence: `profiles/rendering/2026-10-21/wind-all-points-summary.json`; raw native
artifacts in `wind-all-points-2/` beneath the approved OpenCode temp directory.
Add as a separate commit to PR #81; leave unmerged pending approval. #79 is ready
to close on merge, not #70. The earlier blocked run remains recorded below.

## Earlier result — hold for missing native fixture (resolved)

Implementation selects the explicit legacy chart/weather path for All, labels it
unthinned, and reports CPU presentation rather than endlessly preparing a rejected
GPU density. The setting and point-selection/portrayal code are unchanged.

Two targeted rounds completed: **20 passed** each; final Black and scoped flake8
clean. The new membership negative control keeps 4,898 eligible symbols out of
5,000 synthetic nodes, above the old normal-density count ceiling, without
thinning/capping; invalid wind and masked land nodes are excluded. No native
application or dense-workload performance acceptance is claimed.

The native gate was **NOT-EVALUATED**: fixture verification failed before any app
rendering. Read-only inventory found the original manifest still lists 5,693
files; only 503 remain. **5,190 removed, 0 added, 0 changed**. Removed-path samples
are under `chs-tiles/`. The expected SHA begins `1ed1bca`; actual inventory SHA
begins `da18dbc`. Do not silently ignore removed files, accept a new checksum or
repair user artifacts without authorization. The existing #81 commit is still
unchanged on GitHub; #79 work remains separate and uncommitted locally.

Decision: **hold/stop**, because the supplied native fixture is unavailable in its
recorded form. Next step: restore an identical fixture or explicitly authorize a
replacement snapshot and record its provenance, then rerun native All → thinned
→ All membership/content/lifecycle checks. Raw blocker artifact:
`wind-all-points-1/wind-all-points.result.json` under the approved OpenCode temp
directory. This is a fixture blocker, not a native rendering failure or a pass.

## Authorized fixture replacement (before resumed native run)

The user explicitly authorized a fresh snapshot after being told that the old
43 forecast files were unavailable, while current forecasts were present.
Capture current managed app data into a new private directory with the existing
capture tool; exclude credentials/connections/recordings as that tool specifies.
Keep live data and the old damaged fixture untouched. Record the new content SHA,
file count and forecast provenance. This is a changed functional test workload,
not restoration of the old baseline and not a performance comparison to it.

Use the unchanged `wind-all-points` gate on that verified replacement, with the
same Puget dense view, membership, shoreline, switching and visibility checks.
One resumed native process <=110 seconds; static checks are already recorded for
unchanged code. Stop/report if capture or gate fails; do not silently extend the
fix budget or change the tested density/source rules.

Replacement capture verified: SHA256
`410b88ada69dd5a22fc18765a0f26b016e9d15df22bcfeaa954b8b4b62da2346`,
5,654 files / 851.4 MiB; capture time 2026-10-21T09:00:00. Forecasts are
`gfs_20261020_18z_120h_51399d81.grib2` and three HRRR files from
2026-10-21 01Z (`2ad60ae9`, `da0db438`, `fee795dc`). Snapshot directory:
`wind-all-points-fixture-2026-10-21` beneath the approved OpenCode temp directory.
Capture/verification succeeded without touching live data or the damaged copy.
