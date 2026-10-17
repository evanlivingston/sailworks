# Chart quilting plan (the reference application-style), Qt first

Status: design plan for review; no implementation yet. Target branch for
implementation: `feature/chart-quilting` (worktree off `dev`).

## Goal

Match the the reference application model: one detail chart "owns" each screen area — the
best-scaled chart whose coverage contains it — and everything else draws only
where that coverage ends. The GSHHS/fallback world base stays underneath as
land-fill, so holes fall through to the small-scale world view. A new
`chartQuilting` setting (default **on**) restores today's stack behavior when
off.

## Current state (findings)

1. **ENC is already quilted.** `rendering/encrenderer.py:56-95` sorts ENC cells
   by `chart_scale_rank`, applies the `chart_scale_visible` display-level
   window, and does coverage union/difference with a `coverage_path` clip.
   The plan generalizes this engine; it does not replace it.
2. **Rasters have no scale and lose every selection.** `detailed_chart_at`
   ranks charts via `getattr(c, "_native_scale", 0)`; `GDALRasterChart` never
   defines it, so every raster ranks as scale-0 and never wins best-chart
   queries (`chartcollection.py:159-187`). Adding raster metadata fixes both
   paint and selection.
3. **No raster-to-ENC arbitration.** The ENC quilt runs as a separate layer
   after base charts (`chartcollection.py:253-271`), so an ENC always paints
   over a harbor KAP/BSB regardless of which is better scaled. The core of
   this plan is putting rasters and ENCs into one scale-ranked quilt.
4. **Debug rectangles on raster renders.** `gdalrasterchart.py:310-336`
   strokes a red box around every raster file bbox on every bake; the
   `continue` that would skip it is commented out. Removed on this branch
   (commit below) before any quilt work.
5. **CM93 and seamarks are left out of the quilt in v1.** CM93 selects its
   own 8 scale levels internally (a per-viewport mini-quilt); seamarks are a
   zoom-gated overlay (hidden past 300 m/px). Both keep today's path. A
   follow-up may quilt seamarks via viewport-bbox coverage so they fill holes
   only, letting us delete the "enable any chart → disable seamarks" hack
   (`chartcollection.py:152-155`).

## Architecture

### New `rendering/quiltrenderer.py` (absorbs `ENCOverlayRenderer`)

- Input: heterogeneous chart descriptors snapshotted on the GUI thread:
  ENC cells `(path, native_scale, coverage_envelopes)` and raster files
  `(path, per-file bbox, derived native scale)`.
- Worker holds private GDAL handles in an LRU (extend the ENC 8-entry pattern
  to raster datasets); raster tile loading moves out of
  `GDALRasterChart.load_raster`'s ad-hoc threads into the renderer.
- Same algorithm as today's ENC quilt: best `chart_scale_rank` first,
  `chart_scale_visible` culling, coverage difference, clip-and-draw.
- Charts with unknown coverage (GSHHS, country fallback) do not participate;
  they stay in the base layer underneath, producing the the reference application
  world-through-holes look.

### Raster scale metadata (`rendering/gdalrasterchart.py`)

- Prefer the header scale when GDAL/BSB metadata exposes it
  (`SCALE`/`CSCALE`); otherwise derive
  `native_scale ≈ ground_m_per_px × 3779.5` (the same 96 dpi convention as
  `chart_scale_visible`), using the geotransform at file center latitude.
- Per-file bbox doubles as quilt coverage; `metadata` tuples gain a scale
  field with backward-compatible index access.
- Applied at import (`on_register`) and lazily for pre-existing imports.
- **Rasters with no derivable scale render as always-visible, lowest
  priority**: they fill over the world base but never beat an ENC.

### `ChartCollection.do_draw`

Build one descriptor list; draw order becomes: base (GSHHS worker path,
unchanged) → CHS tiles → unified detail quilt (ENC + raster, scale-ranked
together). GTK keeps its synchronous fallbacks but shares the core; only the
Qt UI gets a settings checkbox in this round.

### Settings and Qt UI

- `chartQuilting` bool in `core/settings.py`, default `True`.
- Display-tab checkbox in `qt/settingsdialog.py` (the `smoothZoom` /
  `canadianCharts` pattern) with an the reference application-style tooltip: "Quilt charts —
  fill screen areas from the next scale-out chart where the best chart has no
  coverage. Off leaves gaps empty."
- Off = today's list-order stack, no coverage subtraction (slow machines,
  inspecting RNC edge detail).
- Cache/signature keys (`_base_key`, collection surface signatures) updated to
  include the flag.

### Selection consistency

`detailed_chart_at` / `chart_suggestion_at` need no changes; they improve
automatically once rasters carry `_native_scale`.

## Slices (each a reviewable PR off `dev`)

- **Q1 — Metadata + selection.** Raster native scale + per-file coverage;
  unit tests for scale derivation (synthetic geotransforms) and for
  best-chart selection preferring a harbor KAP at harbor zoom.
- **Q2 — Generic quilt renderer.** Absorb the ENC quilt; add raster
  descriptors and the worker-side raster LRU; unify ENC and raster ordering.
  Tests: synthetic ENC-vs-raster scene asserting the best-scaled chart wins
  each region and holes fall through to base; cancellation/reuse tests mirror
  the existing overlay tests.
- **Q3 — Setting.** `chartQuilting` toggle, Qt checkbox, cache keys; test that
  the off-path matches the current stack output.
- **Q4 — Validation and docs.** Bake-time benchmark against the saved startup
  snapshot (`rendering-baseline-2026-10-15.md`) with a raster+ENC scene;
  targeted tests + `black --check` per fork rules; update this doc with any
  divergences discovered during implementation.

## Decisions

- `chartQuilting` defaults **on**.
- Scale-less raster files: always visible, lowest priority (never beat an ENC).
- Red debug bbox rectangles: removed outright (no env-flag replacement).
