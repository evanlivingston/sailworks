# M0 / E2d: disposable app persistent field / atomic symbol mode

> **Status: HISTORICAL.** The E2d disposable full-app persistent-field report,
> written 2026-10-18. Superseded by
> [ADR-001](../adr-001-grib-rendering-architecture.md) D1-D3 and issues #64-#66.
> Evidence, not a plan.

2026-10-18; [plan entry](rendering-experiment-records.md#e2d-contract-disposable-full-app-persistent-field--atomic-symbol-control).

## Decision: promising interaction evidence, lifecycle gate failed

The full-app prototype produced substantially better **numerical-readiness
observations** than the worker-only E2b control. However, both candidate commands
reported exit status 1 around teardown. Completed replay artifacts are diagnostic
evidence, **not successful native integration runs**. The declared two-iteration,
three-native-attempt budget is consumed. Stop here rather than silently retry.

Next narrow task: isolate the prototype's scene/provider/readiness-signal lifetime,
require clean process exit and explicit nonblank pixel/transition controls, then
repeat the matched interaction test. Do not widen into chart optimization or ship
this experiment as a production pan fix. #50 is untouched, opt-in and unmerged.

> **Status update (2026-10-19):** PR #50 was merged to `dev` via `b9e2471` / PR #61
> `1a90515`. The experiment below was recorded when it was still unmerged; the
> measurements are unchanged.

## What changed in the disposable process

`tools/experiment_app_persistent_weather.py` installs process-local hooks into
the supplied #50 app at `175096d`. No production checkout files were changed.
The shared research startup harness gains an optional pre-native-replay fidelity
hook; normal runs do not install it.

- The worker stages a wider immutable numerical field and passes **arrays and
  metadata only** to the GUI, never a sampler/index/GDAL handle.
- Per-view GUI selection clips retained geographic nodes, projects, thins and
  caps in the same order as the reference. It samples no weather and makes no
  GDAL calls. Source generation, interval, coverage and supported-style checks
  guard reuse. Metadata-only membership is verified against reference index nodes
  at eight interior pan/reversal offsets before publication.
- Qt Quick retains four lossless packed-float endpoint images. Three selection
  images (node ID and x/y anchors) change per view. This intentionally keeps the
  **existing packed transport**, rather than claiming E2c's native RGBA32F host
  has already been integrated into QQuickWidget.
- The barb vertex shader uses node-ID indirection into the persistent field;
  the original barb fragment shader is unchanged. Current selection/time keys
  prevent republishing the same images for redundant requests.
- Before returning a frame, preload the candidate resources and choose numerical
  symbols only when ready, otherwise CPU-only. Numerical frames contain no CPU
  symbol batches or raster. A mixed-mode assertion rejects the frame.

This is a narrowly supported experimental path (saved dense-barb, over-land,
non-seam, fixed-interval scene), not a production state machine. Escape/source/
interval/style handling rejects the retained path; comprehensive recovery,
replacement-overlap accounting and transitions are still open.

## Static and interaction evidence

Same derived functional fixture as E2c:
`97cfda3c62a494c3d462ee8cde449d4d80ed1c62f387d70edcd9192dfb65d360`.
The 33 forecasts and saved scene are unchanged. Its omitted marine download-cache
files and distinction from an identical historical timing baseline still apply.

The first reference and candidate reports share app commit, harness hashes,
environment, full startup/viewport and **every input camera/time state**. The
strict CPU comparison of their completed artifacts passed. It does not override
the candidate command's nonzero exit status.

Before timed motion the candidate and ordinary reference barb items produced
byte-identical **1420 × 1470** widget readbacks with other map items hidden. The
readbacks are fidelity work outside replay. This check is currently equality-only:
it lacks a separate nonblank/reference-content guard and temporal transition
pixel checks. Treat it as initial pixel-equality evidence, **not final glyph
fidelity acceptance**. E2c's exact endpoint/mask/anchor checks remain independent.

The native replay dispatches 24 eight-pixel forward/reverse inputs at a 16.667 ms
intended interval, without readback during replay. Input-phase counts end one
interval after the last dispatch; drain callbacks are reported separately.

| Diagnostic observation | Reference | Candidate, first attempt |
| --- | ---: | ---: |
| All input revisions | 24 | 24 |
| Revisions observed at an input-phase render callback | 15 | 17 |
| Revisions with clean numerical-ready submitted state observed at callback | 0 | 17 |
| Input-phase / total callbacks including drain | 22 / 23 | 17 / 17 |
| Dispatch lateness p50 / p95, all inputs | 6.3 / 10.0 ms | 3.4 / 10.6 ms |
| First-any-render p50 / p95, observed subset only | 14.0 / 23.0 ms | 10.8 / 20.2 ms |
| Texture-upload bytes including drain | 19,628,736 | 20,088,096 |
| Vertex-upload bytes including drain | 10,453,392 | 0 |

Candidate details during input:

- **25/25 submissions** report numerical-only symbols; **zero mixed** submissions.
  Not all submissions are rendered: observed revisions are **17/24**, not 25 frames
  of presentation or 100% input coverage.
- Four persistent field-image provider requests throughout input; no new field
  build, field-image publication or retained fast-path rejection during input.
- **24 GUI selections**, median **1.00 ms**, p95 **1.11 ms**; 24 three-image
  selection publications. Selection images are **20,880 bytes per view** in this
  packed transport, not E2c's 13,920-byte raw-float texture. The field's four packed
  images are **63,488 bytes**, versus E2c's 31,744-byte raw field.
- No worker warnings in the saved replay report. This does not imply error-free
  shutdown or correct source/coverage transition behavior.

These are conservative **submitted-state observations tied to render callbacks**,
not compositor presentation, pixel readback during motion, GPU completion or
scanout. Missing/coalesced revisions remain in the denominator; the conditional
latency subsets differ. One serial process pair and a failed lifecycle gate do
not establish an end-to-end speedup or regression.

## Failed lifecycle gate and validation record

1. Native reference command: exit 0; completed report.
2. Initial native candidate: reported exit 1 with cascading Qt event-filter
   teardown errors and `NoneType.clear`. A complete report had nevertheless been
   written, with static equality and timed replay observations.
3. Focused correction: provide MapScene's expected `weather_provider.clear()`
   contract through a group of field/selection providers; add a regression control
   and preserve static-hook exceptions before host teardown can obscure them.
4. Final diagnostic candidate: again reported exit 1; runner capture contained
   startup/loading and polling-stopped output, **no initiating exception**. A
   complete fresh report again contains initial pixel equality, 24 numerical-only
   input-window submissions, and 24 GUI selections. Its changed harness hash makes
   it **not a strict matched pair** with the earlier reference. Root cause of the
   remaining process failure is unclassified; do not assert cleanup is fixed.

Iteration 1: **22 checks passed** across experiment controls, startup snapshot
checks and native recorder tests. Iteration 2: **5 experiment checks passed**,
including provider cleanup contract; unchanged startup/recorder checks were not
repeated. Black passed on three changed Python files and whitespace passed. These
are targeted correctness results, separate from failed native lifecycle commands.

Private raw files: `rendering-m0-app-persistent-reference.json`,
`rendering-m0-app-persistent-candidate.json`,
`rendering-m0-app-persistent-candidate-diagnostic.json`, and
`rendering-m0-app-persistent-comparison-diagnostic.json` in the OpenCode workspace.
No private fixture, glyph coordinates or source paths are committed. The
[sanitized summary](../profiles/rendering/2026-10-18/m0-app-persistent-summary.json)
records failed command statuses separately from replay evidence.

## Next gate

- Preserve the initiating exception and subprocess/Qt exit status as explicit
  report metadata; file existence must never imply success.
- Close/disconnect readiness and scene update callbacks before provider clearing
  and widget destruction. Verify render/loader ownership and release order rather
  than hiding errors, forcing process exit or suppressing event-filter exceptions.
- Require a successful nonblank initial pixel check, temporal CPU/numerical
  transition checks, covered camera motion under a deliberately stalled field
  worker, and honest interval/source/coverage escape behavior.
- Only after clean lifecycle and fidelity gates, repeat matched reference and
  candidate controls with all revisions and background/upload/GUI costs visible.

**Conclusion:** the persistent-field plus synchronous exact-selection boundary is
more promising than worker caching alone for numerical readiness. Full-app
correctness, lifecycle and performance acceptance remain open. Keep this local
research separate from #50; do not merge or start default-on rollout.
