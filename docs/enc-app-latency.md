# Historical ENC delay investigation — PR #81 user feedback (2026-10-21)

This is not a current ENC latency baseline. The zero-composition run below was
inconclusive; subsequent isolated GSHHS-only measurements supersede it as
chart-latency evidence, not as a measurement of ENC. See the
[performance plan](rendering-performance-plan.md#current-state).

The user reported seconds of chart delay both at startup and after pan/zoom in
the actual app. Functional acceptance was not performance acceptance. The
contemporaneous hold pending diagnosis did not establish a renderer failure.

## Before profiling

Scope: separate deliberate settle/scheduling delay, weather readiness, ENC worker
composition/collection/replay and GUI publication at the user's saved wide-area
view (48.45003186114848, -124.28875762043288, zoom 9.380182039882325), plus a pan and
fractional zoom. Worktree base fe5e5c4. Use the verified fresh private fixture
410b88ada69dd5a22fc18765a0f26b016e9d15df22bcfeaa954b8b4b62da2346, HRRR selection,
710x743 logical/DPR 2/Metal. The app's cached backgrounds remain enabled; no
network or credential access. Profile chart worker calls without changing their
inputs or output. Run a single app session per process.

Non-goals: new renderer representation, geometry thinning, style/source-selection
changes, old-viewport pixel matching, default switch/merge, FPS/input-to-present,
RSS/VRAM or a fresh platform/catalogue campaign. The earlier static/native passes
remain correctness evidence, not latency baselines for this wider view.

Budget: one profiling implementation pass, one native process <=110 seconds,
targeted static checks only. Gate `enc-app-latency` checks completed/nonblank
current views, but timing is reported only: no invented speed threshold. Worker
cProfile data includes profiling overhead; stage spans include scheduling and
readiness waits, never presentation FPS. Stop at the gate and record
continue/narrow/pivot/stop plus the measured bottleneck before a performance fix.

## Result — diagnostic inconclusive (budget used)

One native run of `tools/profile_enc_app_latency.py` (67.6 s wall, fresh snapshot
410b88ad…) completed without an exception, but no stage became ready: startup,
small pan and fractional zoom each waited the full 20 s and recorded zero chart
compositions, with an empty selection. This does not measure the user's several-
second delay and does not explain it. The historical possible causes were: the wide view may lie outside the selected HRRR coverage; weather
readiness gates the chart; or the 20 s harness window is shorter than the real
startup. No rendering or speed change is made by this result. The recommendation at the time was to stop and report; separating those causes
would have required an additional approved native process.
