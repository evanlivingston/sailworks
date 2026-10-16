# Experimental embedded GPU rendering

The Qt app now has a persistent **GPU rendering (experimental)** setting,
default off, applied on restart. `--map-renderer cpu` overrides it without changing
the saved choice. Unsupported initialization, scene-graph errors, and explicit
resource-budget failures return the affected map to CPU rendering. The settings
UI shows only the toggle, a pending restart note, or a fallback message.

## Implementation boundary

`TrackMap` still owns input, selected data, asynchronous renderers, cache coverage,
and camera state. A mouse-transparent `QQuickWidget` retains base/navigation
textures and per-color wind meshes. A sibling QWidget retains route/track/AIS,
dashboard and caption painting; coverage controls remain above both. Chart and
log maps use the same host. Frame requests are coalesced separately from parent
paint events. Immutable image/vertex snapshots contain no dataset handles.

At DPR below 2, wind uses the existing CPU path painter and a retained GPU
texture. Native 1x mesh rendering failed the existing visual tolerance (alpha
coverage ratio 1.088; foreground MAE 15.09). This is an explicit presentation-path
fallback, not a relaxed gate: samples, density, opacity and glyph dimensions are
unchanged. At 2x the mesh path passes. DPR changes switch paths automatically.

Trial limits are 512 MiB estimated GPU storage, 128 MiB CPU scene snapshots, and
128 MiB potential upload payload. The ledger includes old/new image and vertex
storage, per-bucket resolved/4x MSAA surfaces, widget target, and estimated window
buffers. These are **explicit-resource estimates**, not measured total VRAM,
driver allocations, or a completed memory-budget gate.

## Matched startup replay — 2026-10-16

The October 15 private snapshot failed inventory validation because marine field
files were missing. It was not modified or compared against new results. A fresh
snapshot (4,939 files, 1,591.5 MiB) was captured and verified for both candidates.
The new workload has 32 enabled charts, 33 forecasts, dense barbs, 1 hPa isobars,
and marine layers enabled, but **zero marine fields**. It does not replace the
earlier marine-heavy baseline or demonstrate marine-heavy performance.

Native Cocoa / Metal, Qt 6.12.0, arm64 macOS, DPR 2. Three isolated runs per
renderer/scenario, 24 replay frames each, same snapshot and frozen forecast clock:

| Scenario | CPU p99, ms | GPU p99, ms |
|---|---:|---:|
| Stationary | 51.8 | 11.0 |
| Pan | 90.7 | 54.2 |
| Zoom out | 98.3 | 70.3 |

Entries are medians of the three per-run p99 measurements. These are synchronous
**grab/readback benchmark timings**, not display swap intervals or an FPS claim.
The GPU probe forces frame preparation and framebuffer readback before the parent
capture; raw reports separately retain preparation and readback times. Startup
worker idle remained about 12–14 seconds; pipeline stalls are not fixed.
The stationary GPU explicit-resource estimate was about 301 MiB. Process peak
RSS varied substantially (roughly 1.7–2.0 GiB); no total-memory saving is claimed.

Raw matched reports remain private in the approved OpenCode temporary directory:
`cpu-embedded-startup.json` and `gpu-embedded-startup.json`. Reproduce with
`tools/benchmark_startup_rendering.py --snapshot <private-snapshot>
--map-renderer cpu` (then `gpu`), using `--scenarios stationary,pan,zoom_out
--frames 24 --repeats 3`. The earlier timed-out CPU run is not included above.

## Validation and remaining gates

Tests cover preference persistence, save-error rollback, CLI-equivalent controller
override, unsupported-platform fallback, embedded hardware selection, coalescing,
gesture forwarding, control stacking, parent captures with dynamic overlays,
wind parity, retained uploads, both maps, resize/hide/show/close, resource failure,
and simulated scene-graph errors. Native runs cover 1x and 2x on Metal only.

Other GPU backends, real device/context-loss recovery, physical screen-to-screen
DPR changes, long-session memory/driver behavior, and marine-heavy embedded replay
remain unverified. CPU remains default. Prefetch, cancellation churn, and worker
pipeline restructuring are separate unimplemented milestones.
