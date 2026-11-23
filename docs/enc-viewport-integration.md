# Complete faithful ENC viewport integration (#78)

Issue: https://github.com/evanlivingston/sailworks/issues/78, under #51.
This records the 2026-10-21 #78-era experimental integration decision, not
the current default. ENC subsequently shipped enabled by default; see
[ENC rollout](enc-default-rollout.md) and the
[performance plan current state](rendering-performance-plan.md#current-state).

> **Superseded 2026-11-23 by #267.** The Cairo renderer described below was
> removed. The CPU raster backend is Qt's `QPainter` (`QPainterCanvas` in
> `sailworks/rendering/canvas.py`), and the ENC fidelity gate's reference image
> is now built on that backend. `S57ChartDrawer` no longer serves as a cairo
> oracle. The Cairo statements below are kept as the record of the #78-era
> decision they were written as, not as current behaviour.

## Implementation contract (before implementation/runs)

**Question:** can the independent viewport present complete installed/enabled ENC
charts with the authoritative selection, coverage clipping and Cairo portrayal,
while its camera/weather continue independently of chart preparation?

**Scope:** worker-private ENC handles, original rank/visibility/coverage/portrayal,
correct DPR, explicit coverage/loading states, bounded latest-wins preparation,
safe cancellation/close/source replacement, and experimental viewport wiring.
Reference semantics live in `rendering/encrenderer.py`, not in the unpublished
experimental Qt Shapes pipelines. Reuse faithful composition where appropriate;
no raster-resampled zoom frame may be reported as current, and no background-only
image may count as successful ENC. Screen-space labels require exact-scale
composition after zoom; current pan-only imagery can retain translation.

**Non-goals:** default switch, routing or weather calculation/source-selection
changes, downloader/catalog policy, full S-52, custom tessellation, font/style
changes or importing `tools/` into the product. Preserve uncommitted experiments
in the sibling worktree. No commits/push/PR/merge without authorization.

**Baseline/workload:** `dev` c3a6f3a, verified private snapshot SHA256
`1ed1bca1281ad4096713ce20af9c1473e70bd4eb0766a54965849b7ace698a03`, all
enabled chart sources, detailed SF/Puget views, real multi-cell boundaries and
synthetic coverage/holes/scale-ranking negatives. Original rendering is the
pixel reference; existing weather evidence is not remeasured as an unchanged
baseline. Native readbacks outside timed inputs at 710x743 logical/DPR 2/Metal.

**Gates:** targeted tests of source selection, exact worker composition,
latest-wins cancellation and cleanup; native one-command `enc-viewport` gate for
real ENC/nonblank/identity/multi-cell masks and faithful chart/weather composition.
Use existing R0.1 MAE <=1/255, p99 <=8/255 and annotation silhouette/coverage
criteria, exact-zero-alpha checks; preserve source/feature/label semantics.
Deliberately stall chart preparation while camera/weather advance; report loading
and settling, not phantom responsiveness. Account retained payloads separately
from unmeasured RSS/VRAM. Timing is callback/wall evidence, not presentation FPS.
No invented accepted speed threshold. NOT-EVALUATED is not success.

**Delegation:** Kilo Auto Free creates ticket, implements and independently audits
fidelity/coverage. Primary owns scope/review/integration/gate selection. Only the
configured free test runner executes checks/native commands; no paid fallback.

**Budget/stop:** issue phase 1: <=4 targeted validation/fix rounds, <=3 native
processes of <=110s. At the gate explicitly choose continue/narrow/pivot/stop;
phase 2 integration acceptance is <=3 rounds/2 native processes <=120s if phase 1
supports continuing. Phase 3 is evidence/report only, no remeasurement. Stop and
report on budget exhaustion, missing fixture/API, scope expansion or any need to
relax gates. Correctness, native fidelity and whole-experience performance are
separate outcomes. Completion requires honest coverage of acceptance criteria;
do not close #78 because the integration merely draws a coastline.

### Refused ENC cells (2026-11-21, decided)

A planned cell the assembly refuses (``unavailable``/``oversized``/``error``)
is not painted by any Cairo overlay: the ground the viewport already has
beneath the mesh -- the raster chart or the background -- shows through that
cell's clip. Selection and portrayal of drawn cells are unchanged. The refusal
stays observable in the publication's own metadata: ``cell_states`` names each
cell and its reason, and with tracing on the ``scene_publish`` event reports
``unavailable`` as ``(path, reason)`` pairs. The former trimmed-clip metadata
and the opt-in log-only fallback are removed; no other recovery path changes.

## Phase 1 amendment (recorded before any run)

The user directed complete integration and the rejected alternate representations
(DPR-2 resampled raster, per-label outlines, constant cosmetic strokes) already
supply selection context. Phase 1 therefore no longer searches representations or
forbids product integration. It composes the **reference Cairo portrayal and
coverage kernels** at the **exact current scale and DPR**, inside the viewport's
own scheduler, behind a **disabled-by-default** flag:

- the reference renderer and the coverage/ranking/styles/draw-order/label rules
  are unchanged and remain the oracle;
- a retained image is valid only for the current camera — **a fractional zoom
  never publishes a previous scale's image as current**, and re-prepares the
  faithful current-scale output asynchronously (latest-wins), with explicit
  loading/stale/coverage-miss states meanwhile;
- no experiment tooling is imported into `sailworks/`, no legacy worker
  scheduling is reused, and no custom vector/tessellator/stroker is introduced.

Current behavior distinguishes a marked, throttled, half-resolution chart
preview during motion (which can include ENC) from the final settled composition,
which must be current and exact-scale at full DPR. Old-scale/time ENC is
withdrawn immediately rather than displayed as a retained scaled preview.

Acceptance A/B/C, the fixed image bounds, the experimental status, the budget
(4 validation/fix rounds, 3 native processes <=110 s) and the
`enc-viewport` one-command gate are unchanged. No run had executed under the
earlier draft wording, so nothing was discarded or re-measured by this amendment.

## Historical #78 state (2026-10-21)

### Combined-experience acceptance authorized (before runs)

The user explicitly reviewed the functional result and requested the remaining
zoom/resize/coverage/lifecycle and combined-weather acceptance, without requiring
pixel-for-pixel matching to the old viewport. This is Phase 2 entry, with that
visual requirement revised by the user. Scope: native chart+barbs+weather-fill
display through fractional zoom, resize, rapid reversals, coverage miss/recovery,
fixed screen-space sounding size, blocked chart preparation, and close. Preserve
source/coverage semantics, no missing detail, no stale content; do not enforce
legacy pixel values. Default rollout, full catalogue/platform matrix, RSS/VRAM,
presentation latency and weather/routing changes remain non-goals.

Budget: two focused validation/fix rounds, two native processes <=110 seconds.
Use the existing fixture, Metal/DPR 2, Puget weather-covered camera and an ocean
coverage-miss camera; original six static views are recorded evidence and are not
remeasured. New one-command gate: `enc-viewport-experience`. Report preparation
and accounted bytes as diagnostics, never FPS or RSS. Stop and report if the
second round still fails, rather than widening scope. PR #80 scope expands only
by this user-authorized acceptance slice; merge remains pending these results.

### Combined-experience outcome — PASS; adopt experimental integration

First round: `enc-viewport-experience` **14/14 PASS**, complete, 37.9 seconds
process wall time (not FPS). No legacy pixel comparison was performed.
Charts, fill and barbs stayed visible together; fractional zoom and resize to
850x620 logical / 1700x1240 physical recovered; 49 shared sounding strings kept
the same physical extents across zoom. Rapid reversals settled. The ocean view
reported coverage miss without previous ENC, then returned successfully to the
covered Puget view. Blocking chart preparation did not stop camera/weather;
stale hiding, supersession and cleanup passed.

The close check exposed and fixed retained-image/base-handle cleanup: close now
withdraws the payload and the worker releases base-chart handles as well as ENC
handles. Initial/resized payloads were 24,537,760 / 24,668,544 bytes, bounded by
one margin-inclusive ARGB32 image at the current dimensions/DPR, with at most one
provider image. These are accounted bytes, not measured RSS/VRAM.

Targeted checks: 104 passed and the same known base failure; changed-file Black,
tool/helper flake8 and scoped whitespace checks passed. Evidence:
`profiles/rendering/2026-10-20/enc-viewport-experience-summary.json`; raw artifacts
in `enc-viewport-experience-1/` under the approved OpenCode temporary directory.
Decision: **adopt**, and stop measurement after the first passing round. Together
with the earlier real-cell identity/coverage/detail checks this completes #78's
user-revised acceptance, without pixel parity. At that decision it was ready for
review, not a default switch. Catalogue/platform breadth, presentation endpoint,
RSS/VRAM and rollout were separate #70 work; no new campaign was implied.

### Functional scope revision authorized by the user

The user deferred exact visual matching in favor of charts displaying reliably
and independent weather/lifecycle behavior. The original `enc-viewport` fidelity
gate is unchanged. New `enc-viewport-functional` enforces its runtime checks and
complete view coverage without claiming the deferred pixel bounds passed.
Hypothesis: a weather miss hides the shared QML scene root, inadvertently hiding
ENC; keep the ENC scene visible and hide only weather children. Permit initial
chart preparation after a terminal weather miss, still yielding to active weather
work. Defaults and portrayal kernels remain unchanged.
Budget: two focused rounds, at most two native processes <=110 s each; stop on
remaining functional failures. Use the same fixture/SF/Puget/multi-cell cameras,
reversed repetitions and blocked-worker lifecycle probe. No new baseline timing,
rollout, full suite or performance claim.

### Functional outcome — PASS; decision: stop and offer review

The first revised round passed `enc-viewport-functional` (13/13 checks,
39.98 seconds process wall time, **not FPS**). Evidence is summarized in
`profiles/rendering/2026-10-20/enc-viewport-functional-summary.json`. Native raw
artifacts are under the approved OpenCode temporary directory in
`enc-viewport-functional-1/`.

- SF, Puget and the real multi-cell view displayed at Metal/DPR 2 in both
  forward and reverse orders. Selected cell/region identities matched.
- The weather-miss bug was the shared scene root being hidden; ENC now keeps
  that root visible and withdraws only weather children. A terminal weather
  miss no longer prevents an initial ENC request. Active weather still takes
  precedence over chart preparation.
- The deliberately blocked chart remained loading while camera and weather
  advanced. Old imagery was withdrawn, superseded work rejected, and resources
  released. All requested views completed; no forced readiness was used.
- Although exact matching was deferred by the user, all six recorded image
  comparisons happened to have MAE/p99 zero and annotation matching fraction
  1.0. This is measured evidence for these views, not broad catalogue/platform
  acceptance or a separate rerun of the strict gate.
- Targeted tests: 102 passed; one unchanged, previously recorded base failure
  (`test_the_application_never_imports_the_viewport`). Black (four changed
  Python files), tool/helper flake8 and scoped whitespace checks passed.

This completes the narrowed functional display/lifecycle task. Default rollout,
combined-experience/performance acceptance, full catalogue/platform testing and
Phase 2 remain separate. No commit, push, PR or merge is authorized or performed.

### Earlier user-authorized check completion

After three native attempts (the first two aborted in the harness), the user
authorized completing the outstanding checks. Scope: repair the oracle/readback
alignment and selection probes, exercise real multi-cell boundaries, and verify
stale imagery is withdrawn during a blocked exact-scale replacement. Budget:
two targeted validation rounds, at most two native processes of 110 seconds.
Use the same fixture and unchanged fidelity limits; no rollout or performance
claims. Stop after the second round if any acceptance criterion remains open.
The earlier MAE result is not accepted chart evidence: the harness could read
the previous camera before its queued GUI publication completed.

### Earlier check-completion results — decision: stop, Phase 1 not accepted

The two authorized validation rounds/native processes are exhausted. Native
artifacts are in `enc-viewport-run-4/` and `enc-viewport-run-5/` under the approved
OpenCode temporary directory. The fixture matched the recorded SHA in both.

- Targeted checks: 99 passed, one known base failure
  (`test_the_application_never_imports_the_viewport`, unchanged
  `weatherpresenter.py` import). Changed-file Black, tool flake8 and whitespace
  checks passed in both rounds.
- Fixed harness camera/publication waiting, premultiplied readback format,
  independent clipped-region selection, annotation identity and pixel comparison,
  real multi-cell checking, and the previously ignored reversed repetition.
- Fixed product stale-image withdrawal: exact-scale/time replacement clears the
  displayed source immediately. Run 4 observed stale hiding, latest-wins and
  resource release passing. Run 4's weather-stall probe failed to force a new
  composition during retained pan; run 5 explicitly forces it.
- Run 4 (27.8 s): Puget and real multi-cell view matched pixel-for-pixel; SF
  readback was transparent while the weather viewport was hidden. The latter is
  invalid visual evidence, not an accepted portrayal mismatch or speedup.
- Run 5 (45.9 s): Puget and real multi-cell views matched pixel-for-pixel in both
  reversed repetitions (MAE/p99 zero, silhouette/ink/annotation coverage 1.0).
  Selected sources and clipped-region identities matched. SF failed the added
  visible-weather readiness condition in both repetitions, so SF visual evidence
  remains **not evaluated**. The gate remained FAIL.
- Because SF was incomplete, run 5 did not enter its behavioral stage. Its four
  false behavioral defaults are **not evaluated**, not four demonstrated product
  regressions. The forced-stall correction therefore remains unverified.

At this earlier stop, remaining blockers were: diagnose SF readiness without forcing or bypassing it;
obtain complete matched SF readbacks; run the corrected forced-stall probe and
repeat lifecycle checks in the same complete run. No further run is authorized
by this budget. No Phase 2 entry, rollout, performance acceptance, commit or issue
closure is claimed. Normal app defaults remain unchanged. Sibling uncommitted
experiment work is preserved.

## Cairo → GPU ENC mark inventory (step 1, 2026-11-21)

Scope of this section: the inventory only. It records what the reference Cairo
drawer actually emits and what the GPU scene has to reach to match it. It makes
no fidelity or performance claim, and it does not re-measure a recorded view.

Selector/reproduction note: this inventory was taken while the application
could still choose the raster ENC source. Since step 3 the application always
presents ENC as the GPU scene (`WeatherPresenter._create` passes
`enc_scene_layer=True`); there is no `chartENCScene` setting or panel toggle
for a raster app run. `--map-renderer` was never the ENC selector and is now
inert.

### Verified inheritance

`S57ChartDrawer(CM93ChartDrawer)` (`sailworks/rendering/vectordrawer/s57chartdrawer.py:19`).
It overrides `draw`, the feature collection/cache chain
(`_collect_layers`, `_ensure_feature_cache`, `_prepare_feature`,
`_feature_cache_*`, `_compute_feature`, `_collect`), `_render_area`, the
DEPARE coverage-boundary filter (`_coverage_edge_filter`,
`_stroke_exposed_edges`, `_nearby_boundaries`, `_on_coverage_boundary`),
`_render_soundings` and `_render_point`. Every remaining mark is inherited from
`CM93ChartDrawer` unchanged; there is no other override layer between them.

### Symbol-atlas finding (corrects a prior assumption)

The S-57 symbol provider is **not** part of the S57 Cairo portrayal. The only
atlas blit in the tree is `SymbolsProvider.draw_light`, which ends in
`self.draw(cr, "LIGHTS13", xx, yy)`
(`sailworks/rendering/vectordrawer/s57symbolprovider.py:176`), and its only
caller is `OSMChartDrawer.draw_light` (`osmchartdrawer.py:111`). `S57ChartDrawer`
and `CM93ChartDrawer` never construct or call a symbols provider, so **zero**
atlas sprites are drawn on the ENC path and no LIGHTS13 blit exists there.

Therefore the atlas inventory is not evidence of drawn-but-missing symbols, and
an inventory diff must not be reported as lost detail. Within the atlas itself,
one key (`LIGHTS13`) is blitted and the rest are unused — that is an unused-asset
fact, not a lost-mark fact.

### Cairo marks actually emitted

Areas (`_render_area`, one even/odd path per feature, `fill_preserve` then
`stroke`): `LNDARE`, `DEPARE`, `DRGARE`, `BUAARE`, `BUISGL`, `LAKARE`, `RIVERS`,
`ACHARE`, `ANCBRT`, `RESARE`, `CTNARE`, in `AREA_ORDER` depth→dredged→land→other.
Only `DEPARE` re-lays its stroke against the coverage boundary; fills and
interior contours are never clipped.

Conditional style to preserve: `DEPARE` picks its fill from `DRVAL1 < 5`
(shallow sea), else `DRVAL2 < 20` (medium), else deep; every area's outline is
`CONTOUR_STYLE` unless `land_stroke`. This is a data-dependent style, not a
constant cosmetic stroke, and a GPU pass that fills a flat depth colour diverges
from Cairo on exactly the shallow bands that matter for navigation.

Lines (`_render_line`): `COALNE` 1.5 px land stroke; `DEPCNT` contour;
`NAVLNE`/`FERYRT`/`TSELNE` dashed 4.0; `SLCONS` 1.0; everything else a 0.5 px
`DEFAULT_LINE`. Dash is reset after each stroke.

Point marks, in `_render_point` precedence order:

| Cairo mark | Feature classes | Emission |
|---|---|---|
| light | `LIGHTS` | yellow circle r=4 plus eight rays, 0.5 px stroke (`_draw_light`) |
| beacon | `BCNCAR`/`BCNLAT`/`BCNISD`/`BCNSAW`/`BCNSPP` | stroked triangle, 1.5 px |
| buoy | `BOYCAR`/`BOYISD`/`BOYSAW`/`BOYSPP` | filled generic dot r=3, `DEFAULT_POINT` |
| lateral buoy | `BOYLAT` | filled dot r=3, red when `CATLAM == 1`, else green |
| wreck | `WRECKS`/`UWTROC`/`OBSTRN` | stroked X |
| landmark | `LNDMRK`/`SILTNK`/`CHIMNY`/`TOWERS` | 4×4 stroked rectangle, 1.0 px |
| generic dot | any other point, scale ≤ 100 | `DEFAULT_POINT` dot r=1.5 |

Two distinctions this inventory keeps explicit:

- The generic dot is a **drawn** mark, not an absent one. "Point class not in a
  specific branch" means `DEFAULT_POINT` at r=1.5, never "nothing drawn", and it
  is suppressed entirely at scale > 100.
- `BOYLAT` is **not** keyed on `CATLAM` in the source data. The override reads
  `COLOUR` (`ATTRIBUTE_FIELDS = {"BOYLAT": ("COLOUR",)}`) and synthesizes
  `CATLAM = 1` for `COLOUR == "3"`, `2` for `"4"`, and otherwise rewrites the
  feature to `BOYSPP` so it falls to the generic dot. A GPU comparison that
  selects on a raw `CATLAM` attribute will disagree with Cairo on both the
  colour and the class.

Non-sounding text: the ENC Cairo drawer emits **no** text other than sounding
values. `_render_soundings` only `show_text(f"{p[2]:.1f}")`
(`s57chartdrawer.py:370`, base at `cm93chartdrawer.py:258`), gated at
scale ≤ 100, font 11 (base 8), skipping non-finite depths and de-duplicating on
32×16 px boxes. Light-character text exists only in the OSM `draw_light` path.
This document therefore makes no claim about place names, buoy labels or any
other annotation: none is proven drawn by the ENC Cairo drawer.

### GPU-side state, and what the counts mean

`encmesh.py:102` declares `NOT_RENDERED = ("symbols", "text", "soundings")`,
documented as "present in a cell but deliberately left to the retained
CPU/symbol path". `encgpu.py` does bake point symbols as flat marker triangles
(`encmarkers.triangles(kind)`), soundings as textured glyph quads from
`GlyphAtlas`, and labels through `glyph_quads`/`apply_glyph_labels`, with marker
and glyph geometry adopting the current camera so symbols keep a logical size.

So the `NOT_RENDERED` tuple is a division-of-labour declaration, not a list of
visual omissions. Tallying entries in it — or counting features whose class
falls in it — is **not** symptom proof: it does not establish that a mark is
missing on screen, because the retained path is expected to draw those marks.
An inventory count can size a migration; only a rendered comparison can show a
gap.

### Migration targets

- `sailworks/rendering/vectordrawer/s57chartdrawer.py` — the override list above
  is the reference behaviour for selection, area order, DEPARE banding and the
  `BOYLAT` `COLOUR`→`CATLAM` rule.
- `sailworks/rendering/vectordrawer/cm93chartdrawer.py` — the inherited point,
  line and sounding styles that the ENC path does not restyle.
- `sailworks/rendering/vectordrawer/encmesh.py` — `NOT_RENDERED`, `AREA_ORDER`/
  `AREA_LAYERS`/line/area style tables that already mirror Cairo.
- `sailworks/qt/encgpu.py` — marker, glyph and sounding node construction.
- `sailworks/rendering/vectordrawer/encmarkers.py`,
  `sailworks/rendering/vectordrawer/encsounding.py` — procedural mark shapes and
  sounding placement.
- `sailworks/rendering/vectordrawer/encscene.py`, `sailworks/qt/viewportenc.py` —
  scene assembly and which drawer is actually used
  (`ViewportS57ChartDrawer`).
- `sailworks/qt/weatherpresenter.py` — the application's `enc_scene_layer=True`
  construction, now the only production ENC entry point.

### Validation commands

Documentation-only step, so no product test is claimed to pass:

```
python tools/check_doc_links.py
python tools/check_doc_anchors.py
git diff --check -- docs/enc-viewport-integration.md
```

For a later code step, select the changed modules' tests rather than the suite:

```
ls test | grep -i -E 'enc|s57|vectordrawer|encgpu|encscene'
python -m pytest -q <matched modules>
python tools/gate_runner.py     # one-command rendering gate
```

## Step 3 decision (2026-11-21): GPU ENC mandatory in the application

The user authorized removing the selectable application Cairo ENC path. The
normal application constructs the viewport with `enc_scene_layer=True` and
never requests `enc_chart_layer`; `resolve_enc_modes` therefore turns the
raster source off for every app host, and the scene is the only ENC
presentation. The `chartENCScene` setting and its panel checkbox are gone; a
legacy saved value has no effect. The standalone viewport keeps
`--enc-chart-layer` as an independent raster reference, and
`ViewportChartLayer`'s Cairo ENC source remains for it and for unrelated
Cairo work (basemap, MBTiles, marine, overlays).

Preserved: ENC display still follows `chartENCVisible`, chart selection and
scale limits are unchanged, the above-ENC environment/background composition
is unchanged, and one host draws a cell exactly once (the raster composition
is constructed with its ENC source disabled whenever the scene is on). This
decision makes no new fidelity or performance claim; validation belongs to the
issue/PR that carries the change.
