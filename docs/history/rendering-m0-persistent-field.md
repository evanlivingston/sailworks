# M0 / E2c: persistent numerical field and exact selection indirection

> **Status: HISTORICAL.** The E2c persistent-field report, written 2026-10-18.
> Superseded by [ADR-001](../adr-001-grib-rendering-architecture.md) D1-D3 and issues
> #64-#66. Evidence, not a plan.

2026-10-18. [Plan entry](../rendering-performance-plan.md#e2c-contract-persistent-gpu-field--exact-selection-indirection).
This is separate, uncommitted M0 research; #50 stays unchanged and unmerged.

> **Status update (2026-10-19):** PR #50 was merged to `dev` via `b9e2471` / PR #61
> `1a90515`. The experiment below was recorded when it was still unmerged; the
> measurements are unchanged.

## Decision

**Continue with this field/selection boundary as the next integration candidate.**
One native Qt/Metal resource can hold numerical endpoints across camera changes,
while a separate current-view selection preserves the existing portrayal inputs.
The mechanism passed; adoption into the full renderer is **not yet justified**.

This advances beyond E2b's worker cache: the field is actually retained on the GPU
and selected through node-ID indirection. It does not translate already thinned
symbols, and it does not publish four replacement endpoint images for each view.
No inference about the reference application internals is needed for this result.

## Model and ownership

- Prepare one immutable wider native-node field with authoritative U/V endpoint
  values and source provenance, using the existing bounded display-worker sources.
  Keep float32 payloads, without normalization or hardware linear filtering.
- Pack `(u0, v0, u1, v1)` into an RGBA32F texture, 64 nodes per row. Immutable
  generation identity includes coordinates/endpoints, provenance, interval and
  the versioned snapshot identity.
- Produce a separate RGBA32F per-view selection: `(node ID, anchor x, anchor y,
  exact-time blend)`. Empty slots use ID -1 and the reference's local anchors.
  This prototype repeats the blend per slot; moving it to a uniform remains a
  possible refinement, not a measured result.
- Preserve current native membership, longitude-major ordering, screen-cell
  thinning, count cap **before** shoreline exclusion, anchors and missing masks.
  Candidate selection has no sampler/GDAL dependency. Metadata-only native-node
  enumeration supplies the current candidates, checked against the reference.
- Bind only matching field/selection generations. Validate integral IDs, actual
  node-count bounds (not padded texture capacity), finite anchors, blend bounds
  and an explicit reservation before GPU allocation.
- The reserved texture-payload envelope is 16 MiB including two generations of
  field, selection and validation output. This workload reserves **119,168 bytes**.
  This does **not** bound Python dictionaries, source metadata, GDAL, total RSS,
  driver allocation granularity or VRAM. The readback output is a probe resource,
  not a proposed extra production rendering pass.

Qt/RHI owns native resources in the standalone QRhiWidget. Validated field and
selection uploads precede one shader draw in the same command-buffer transaction.
The shader fetches the selected endpoints and returns interpolated U/V and exact
anchors. Exact endpoint branches do not consume a NaN in the unused endpoint.
There is no CPU symbol layer in this standalone test.

**Atomic resource binding here is not atomic full-app presentation.** This shader
reconstructs portrayal inputs; it does not draw the existing barb glyph mesh,
display the real chart scene, exercise loading transitions, or establish pixel
equivalence of final glyphs. Those remain integration gates.

## Workload and reference

Use the explicitly derived functional fixture from #50's residency verification:
`97cfda3c62a494c3d462ee8cde449d4d80ed1c62f387d70edcd9192dfb65d360`.
All 33 forecasts and saved settings match the historical fixture; 370 unavailable
marine download-cache files are omitted. This is not a matched historical timing
baseline. No snapshot or user data was rewritten.

The unchanged #50 modules at `175096d` serve as the oracle. At the saved clock
plus one exact second, compare nine small horizontal pan/reversal/return views
within 256 pixels of retained coverage. For each view:

1. Run ordinary `stage_barbs` and decode all six reference images.
2. Verify candidate native membership equals the reference's sampled coordinates.
3. With sampler `_side` and every decoded-band `array` entrypoint hard-blocked,
   produce the candidate selection from immutable retained endpoint data.
4. Reconstruct both endpoint atlases and anchors from node IDs on CPU, and require
   exact equality (including NaNs) and identical selected counts.
5. On the native GPU, upload the field once and replace only the selection between
   views. Read back interpolated U/V and anchors; require identical masks/anchors
   and vector error no greater than **2e-5 m/s**.

Per-selected-node source hashes follow the immutable original staged provenance;
the candidate does not reselect another forecast. Broad source-event/interval
crossing behavior was not exercised by this fixed-interval workload.

## Results

| Gate | Result |
| --- | --- |
| Real CPU reference comparisons | **9/9 exact** endpoints, masks, anchors and selected counts |
| Selected symbols | 722–737, matching reference at each view |
| Retained native nodes | 1,921 |
| Field upload per forecast generation | **1**, 31,744 bytes |
| Per-view selection upload | **13,920 bytes**, versus 41,760 bytes for the reference six-image atlas |
| Native forecast-view readbacks | **9/9 passed**, zero measured vector error |
| Generated missing/exact/interior endpoint controls | **3/3 passed**, zero measured vector error |
| Native textures created | **3** (field, selection, validation output), reused across cases |
| Total field uploads | **2**: one forecast generation and one generated-control replacement |
| Total selection uploads | **12**, 167,040 bytes |
| Native resource cleanup | All eight tracked resources destroyed |
| Targeted correctness controls | **10 checks + 5 subtests passed** |
| Formatting / whitespace | Black passed on four new Python files; whitespace passed |

The generated control replaces field contents in the reserved allocation and
exercises missing endpoints at weights 0, 0.25 and 1, plus empty slots. One fixed
framebuffer Y orientation is established from anchors and enforced thereafter;
it is not independently chosen per view to hide an error.

Tests reject stale generations, padding/out-of-range/fractional IDs, nonfinite
anchors, invalid blend, reservation overflow and coverage escape. They verify
publication buffers are independent of mutable preparation arrays and that a
shoreline exclusion after a cap does not select an extra water node.

Native environment: Qt **6.12.0**, Cocoa, Metal. Readback is used **only for
correctness**, with no frame-rate or interaction-latency inference. No temporal
sampling, routing, source priority or portrayal approximation was changed.

The budget used one implementation/targeted-validation iteration, one offline
real-data process and one native process, each command bounded to 120 seconds.
The second permitted implementation/native attempt was not needed. No soak,
context-loss/recreation, minimum-Qt or alternate-backend validation is claimed.

## Reproducibility and next gate

Harnesses: `tools/experiment_persistent_weather.py` and
`tools/experiment_persistent_weather_gpu.py`. The former reads supplied #50 code
without editing it, verifies fixture/view identity, works in a private sandbox,
and disables networking. Its prepared `.npz` contains private numerical/anchor
data and remains outside Git.

Private evidence files in the OpenCode workspace:

- `rendering-m0-persistent-prepared.json` and `.npz` (reference and candidate inputs).
- `rendering-m0-persistent-gpu.json` (native fidelity, upload and cleanup evidence).

The [sanitized summary](../profiles/rendering/2026-10-18/m0-persistent-field-summary.json)
records their hashes and key results without source paths or saved coordinates.

**Next experiment, not automatic production work:** integrate persistent field
and exact selection into a disposable app renderer. Keep one authoritative symbol
mode per submitted frame; prevent simultaneous CPU batches and numerical barbs.
Gate camera/time/source generations without accepting stale clipped previews.
Measure both selection readiness and CPU/GPU mode at render synchronization,
all-input dispatch lateness, missing/coalesced revisions, upload traffic and
worker/GUI costs with matched trajectories. Explicitly test preparation stalls,
coverage escape and source/interval changes. A camera-independent field is not
enough if current selection still queues behind exact-view jobs or GUI assembly.

Do not call this M0 completion, fluid pan, a native-rewrite requirement, a
presentation-latency result, or a full-app performance win. #50 remains the narrow
residency fix; any production adoption belongs to a separately reviewed change.
