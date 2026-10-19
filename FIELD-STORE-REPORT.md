# Field store report — issue #64 (GPU-resident numerical field cache)

**Status:** implemented, feature-flagged, default OFF. The `residency` gate is
**FAIL before and FAIL after**: the gate's ratified threshold is not reachable
inside this issue's scope. The blocker is at the end of this report; nothing
was relaxed to manufacture a pass.

---

## 1. What was built, file by file

| File | Change |
| --- | --- |
| `gweatherrouting/qt/gpuweatherfields.py` **(new)** | The field store: `FieldIdentity`, `ResidentField`, `FieldStore` (byte budget, LRU eviction, protected current field, explicit release, statistics) and the RGBA8888 expansion constants. |
| `gweatherrouting/rendering/weathercache.py` | New `ByteAccountedLRU` — the single byte-accounted LRU of the renderer. `DecodedBandCache` was refactored onto it, so the decoded-band cache and the field store share one accounting implementation and one budget discipline instead of a second, differently-accounted one. Its public API, statistics keys and error messages are unchanged. |
| `gweatherrouting/rendering/weatherstaging.py` | `time_window(sampler, stamp)` extracted from `stage_wind`: the **resident time window** that serves an instant. `stage_wind` now calls it, so the window used for residency is the same window the staging uses (bit-for-bit the same event list, the same singleton rule, the same exact-time mask and new-cycle selection semantics). |
| `gweatherrouting/rendering/weatherbarbs.py` | `BarbAtlas.identity` (the field identity of the staged atlas) and `stage_barbs(..., store=, source=)`: the staged field is looked up in the store under its identity and stored after staging. Without a store the call is byte-for-byte the previous behaviour. |
| `gweatherrouting/qt/gpuweatherjobs.py` | The numerical worker owns the store (`_WeatherResources.fields`), passes it to the staging only when the flag is on, releases every resident field on a source change, on invalidation and on shutdown, and publishes the store statistics (`WeatherStager.field_store_statistics()`). `FIELD_BYTE_LIMIT` is the declared budget. |
| `gweatherrouting/qt/gpuweather.py` | `WeatherImageProvider.publish(pair, key=None)`: with a key the published URLs are derived from the bytes they serve, so an image that is already on the GPU keeps its URL and is not uploaded again. Without a key the historical generation scheme is used unchanged. **`encode_component()` is untouched** — the RGBA8888 bit-packing is byte-identical to before. |
| `gweatherrouting/qt/gpuscene.py` | The scene counts the *actual* uploads (the images whose URL changed) and separates the chart layer from the weather field: `weather_field_uploads`, `weather_field_upload_bytes`, `weather_field_reuses`, `chart_texture_uploads`, `chart_texture_upload_bytes`. The pre-existing `texture_uploads` / `texture_upload_bytes` keep their meaning and their values on the **default** path (six uploads per publication, exactly as before); on the flagged path they count the uploads that really happen. |
| `gweatherrouting/qt/gpupresenter.py`, `gweatherrouting/qt/controller.py`, `gweatherrouting/qt/__main__.py` | The flag: `--gpu-resident-fields` (CLI) and `GWR_GPU_RESIDENT_FIELDS=1` (used by the benchmark harness, which starts the app in-process). Both are **default OFF**. |
| `tools/benchmark_startup_rendering.py` | The field-store statistics are added to the report (`numerical_weather.field_store`). A measurement addition, not a threshold change. |
| `tools/gates.py` | The residency gate now runs the flagged path (`GWR_GPU_RESIDENT_FIELDS=1`) and carries three new checks: the field-store budget (ratified, sourced from the 32 MiB staging ceiling the presenter already enforced) and two **unratified** checks — the field-scoped and chart-scoped upload counts — which are reported without affecting the verdict because no recorded evidence bounds them for this workload. **No existing threshold was changed, relaxed or re-scoped.** |
| `test/test_gpuweather_fields.py` **(new)** | 14 targeted checks: identity stability, the resident time window, byte accounting, eviction and protection, release/clear, the RGBA8888 expansion, reuse on a time change and on a return, a new field on a new window, a new field on a covered pan, numerical parity against a fresh stage, the unchanged default publication and the stable named publication. |

### The field identity

`FieldIdentity(source, parameter, before, after, window, resolution)` — exactly
the ADR-001 D1 identity, and deliberately **not** a per-frame image identity:

* `source` — the signature of the selected forecast files (`source_key()` per
  file: path, name, centre, start/last forecast, bounds and the file version),
  so a changed file invalidates every field that used it;
* `parameter` — `"wind"` (the U/V parameter);
* `before` / `after` — the **resident time window**: the two forecast instants
  that bracket every instant the field serves (equal for an exact-instant
  field, so an event instant keeps its own singleton window and no
  interpolation crosses a model-run switch);
* `window` — the geographic bounds the field covers (the view bounds plus the
  32 px symbol margin);
* `resolution` — the published mesh shape (rows, columns) at the barb density
  step.

The identity is what the store is keyed on and what the release, eviction and
budget rules are expressed in. The *published URLs* are derived from the bytes
they serve, which is a superset of the field-id behaviour: the same field
always publishes the same URLs, and an image that is already resident is never
uploaded a second time.

---

## 2. How the residency budget is accounted for

* **One accounting implementation.** `ByteAccountedLRU` in
  `gweatherrouting/rendering/weathercache.py` is now the byte-accounted LRU
  used by both the decoded-band cache and the field store: one declared
  `byte_limit`, one `current_bytes` / `peak_bytes` accounting, one
  least-recently-used eviction, one statistics shape. The field store does not
  invent a second budget.
* **The declared budget** is `FIELD_BYTE_LIMIT = 32 MiB`
  (`gweatherrouting/qt/gpuweatherjobs.py`) — the same ceiling the CPU staging
  path already enforced for one published pair
  (`WeatherImageProvider.byte_limit`). Residency therefore does not raise the
  presenter's memory ceiling. The 128 MiB decoded-band budget is separate and
  unchanged (the gate still checks it: 104,851,200 B peak ≤ 134,217,728 B).
* **The 2× RGBA8888 expansion is part of the cost.** The transport puts one
  float32 into two opaque RGBA8888 texels, so **every float32 costs 8 bytes of
  texture** (`TEXTURE_BYTES_PER_FLOAT32 = 8`). A published field carries six
  float32 per mesh cell (u0, v0, u1, v1, anchorX, anchorY), i.e.
  **48 bytes of texture per cell**, plus the 16 bytes of CPU-side float32
  values (the two endpoint pairs) that the resident field also holds —
  64 bytes per mesh cell in total. The store accounts the texture bytes *and*
  the numerical value bytes of every resident field.
* **Eviction** is least-recently-used, and the field that was stored last (the
  one that is current) is **protected**: it is never the eviction victim, so
  speculation is reduced before selected content is. A field that does not fit
  the budget at all is published but not retained (it is not an error).
* **Release** is explicit: every resident field is dropped on a source change
  (`_WeatherResources.release()`), on invalidation (`WeatherStager.invalidate()`
  → the worker's release path) and on worker shutdown (`_WeatherResources.close()`).
  The field identity also contains the file version, so a changed forecast file
  can never resolve to a stale field.
* Measured on the real workload: **404,736 bytes resident of the 33,554,432
  byte budget**, 2 resident fields, 0 evictions, 1 acquire hit, 2 misses
  (`numerical_weather.field_store` in the post-change gate report).

---

## 3. The `residency` gate: baseline and post-change

Command (both runs, same fixture `1ed1bca1…`, 22 sources, 24 scrub inputs,
`scrub_return`, `--measurement-mode native-render`):

```
python tools/gate_runner.py --gate residency --snapshot <fixture> --output <dir>
```

### Baseline (unmodified app, pristine `HEAD`)

The baseline was re-measured on a **detached worktree of pristine `HEAD`**
(`46f423a`, no changes), with the unmodified registry, the same tool, the same
fixture and the same workload:

```
FAIL  residency  #64  [correctness]
 !texture uploads during the scrub input phase=41 <= 0
 !texture upload bytes during the scrub input phase=668015328 <= 0
  numerical weather path is active (no silent CPU fallback)=True
  resident decoded-band budget is respected=104851200 <= 134217728
  the gate does not claim presentation latency=False
  (fixture differs from the recorded evidence)
```

Exit code 1 (`FAIL`), 33.4 s, 13 of 24 inputs observed at a render callback,
forecast time 2026-10-19T08:00:00, 25 base-chart rebuilds and 26
navigation-overlay rebuilds during the scrub. This is **byte-identical** to the
recorded run of the same gate on the same fixture
(`…/gate-runner/results/residency.result.json`: 41 uploads / 668,015,328
bytes), which confirms it is the same workload.

Two earlier baseline attempts in this session could not be evaluated: the
composed tool exceeded the gate's 110 s budget twice (`NOT-EVALUATED`), and a
third exceeded the worker's own 600 s budget, because the host was running the
maintainer's parallel benchmark and a float-transport experiment (load 6–8 on
8 cores). A fourth attempt on the pristine worktree started the scrub at the
*saved workspace* time (2026-10-14T15:09) instead of the snapshot's forecast
time — a startup race in the harness that was reported separately and not used.

### Post-change (this worktree, `--gpu-resident-fields` on)

```
FAIL  residency  #64  [correctness]
 !texture uploads during the scrub input phase=39 <= 0
 !texture upload bytes during the scrub input phase=621966240 <= 0
  numerical weather path is active (no silent CPU fallback)=True
  resident decoded-band budget is respected=104851200 <= 134217728
  the gate does not claim presentation latency=False
  resident field texture uploads during the scrub input phase=unratified
  the resident field store stays inside its declared budget=404736 <= 33554432
  chart-layer texture uploads during the scrub input phase=unratified
  (2 unratified; fixture differs from the recorded evidence)
```

Exit code 1 (`FAIL`), 35.0 s, fixture `1ed1bca1…`, commit `46f423a` (dirty),
13 of 24 inputs observed at a render callback, forecast time 2026-10-19T08:00.

### The observed counters, split by layer

| Counter (probe delta over the 24 scrub inputs) | Baseline (pristine HEAD) | Default path (flag OFF, this worktree) | Flag ON (this worktree) |
| --- | ---: | ---: | ---: |
| `texture_uploads` | 41 | 40 | 39 |
| `texture_upload_bytes` | 668,015,328 | 644,990,784 | 621,966,240 |
| `chart_texture_uploads` (base chart + navigation overlay) | — (not instrumented) | 28 | 27 |
| `weather_field_uploads` (the numerical field) | 12 (derived, see below) | **12** | **12** |
| `weather_field_upload_bytes` | — | 303,552 | 303,552 |
| `field_store.retained_entries` | — | 0 | 2 |
| `field_store.current_bytes` / budget | — | 0 / 33,554,432 | 404,736 / 33,554,432 |
| `field_store.acquire_hits` / `acquire_misses` | — | 0 / 0 | 1 / 2 |
| `field_store.evictions` | — | 0 | 0 |
| `image_provider_requests` (whole run) | 18 | 18 | 18 |
| `numerical_weather.symbols` | 1053 | 1053 | 1053 |
| `numerical_weather.atlas_bytes` | 151,776 | 151,776 | 151,776 |
| `band_cache.peak_bytes` | 104,851,200 | 104,851,200 | 104,851,200 |
| `cpu_symbol_batches` / warnings | 0 / 0 | 0 / 0 | 0 / 0 |

**Reading of the counters.**

* The **weather field's** upload count is **12 in every run**. The baseline
  does not have the field-scoped counter, but it has the same
  `image_provider_requests = 18`: three publications of six images over the
  whole run, of which two fall inside the scrub input phase — 12 uploads, in
  the baseline and after the change. The 22 inputs *inside* the resident time
  window cost **zero** uploads and **zero** re-decodes; that is the acceptance
  criterion of this issue, and it holds.
* The **chart layer's** upload count (27–28) is the residual: the base chart
  holds the forecast-time dependent isobars and the navigation overlay holds
  the clock, so both are re-rendered and re-uploaded on every forecast-time
  change (25 and 26 rebuilds in the baseline). That count varies between
  identical runs with the number of inputs that were observed at a render
  callback, which is why the *total* moves by ±2 between identical runs.
* The two layers together are the 39–41 uploads the gate measures.
* The **default path is unchanged**: the flag-off run of this worktree
  reproduces the pristine baseline on every stable metric (1053 symbols,
  151,776 atlas bytes, 18 provider requests, 12 weather-field uploads, the
  same 104,851,200 byte decoded-band peak, no warnings, no CPU symbol
  fallback) and differs from it only in the chart layer's render-timing
  variance (28 against 29 chart uploads).
* The field store did its job on the workload: two fields are resident, one
  acquire hit (the return to the exact-time field at the end of the scrub was
  served from the store instead of being re-sampled and re-encoded), no
  eviction, and the store holds 404,736 bytes of its 33,554,432 byte budget.

---

## 4. How eviction and release are exercised

* **Eviction** is exercised by the unit tests in
  `test/test_gpuweather_fields.py`: a 2048-byte store holding two 1024-byte
  fields evicts the least recently used one when a third arrives
  (`evictions == 1`), and the *protected* (current) field survives that
  eviction while the older one does not. A field larger than the whole budget
  is published but not retained, and the store's byte accounting returns to
  zero.
* **Release** is exercised on the real workload by the source-change and
  invalidation paths in `_WeatherResources.release()` (which clears the store
  whenever the selected sources change or the worker is asked to release) and
  by `WeatherStager.invalidate()`. The release path is unit-tested
  (`test_release_and_clear_drop_resident_fields`) and the store's
  `released` counter is published in the gate report.
* The identity contains the file version, so a forecast file that changes on
  disk cannot resolve to a stale resident field even before the explicit
  release runs.

## 5. A covered pan outside the resident window

A pan that leaves the field's spatial window produces a **different identity**
(a different `window` and `resolution`), so the worker stages a **new field**
and publishes it; the scene never presents the previous field as current,
because the published atlas is always the one the worker produced for the
current request, and the identity is part of that atlas. The previous field
stays resident until it is evicted, so panning **back** into a window that is
still resident is free: the store returns the resident field and the worker
does not re-sample, re-encode or re-decode.

This is covered by `test_a_covered_pan_outside_the_resident_window_stages_a_new_field`:
the panned atlas is a different object, its `identity.window` differs, and
panning back returns the *original* atlas object.

**Limit, stated honestly:** with the RGBA8888 transport the published field is
a *screen-space* mesh — the anchor textures and the cell assignment depend on
the view — so a pan cannot be upload-free. What the store removes on a pan is
the re-decode, the re-sample and the re-encode; the mesh is rebuilt and
re-uploaded. Making a covered pan upload-free needs the geographic field
transport (node-ID indirection) that #71/#65/#68 own, and the shader path is
explicitly out of scope for this issue.

---

## 6. Numerical parity and the A0k missing-data gate

* **Numerical parity.** The resident field's numbers are bit-identical to a
  fresh stage: `test_the_resident_field_is_numerically_identical_to_a_fresh_stage`
  compares `staged.first` and `staged.second` as raw `uint64` views, the
  symbol count, and the **encoded transport bytes** of all six images
  (`constBits()`), and they are equal. The existing sampler parity tests
  (`test/test_weather_staging.py`, `test/test_weather_barbs.py`) still pass:
  the staging semantics, the exact-time mask and the new-cycle selection
  semantics are untouched, because `time_window()` is the same event list and
  the same singleton rule that `stage_wind()` used before.
* **A0k missing-data gate.** The transport is unchanged: `encode_component()`
  is byte-for-byte the same, and the A0k inputs (NaN in the packed float32
  bits) are the same bytes. The A0k owner tests pass unchanged
  (`test/test_barb_shader_safety.py`: 6 passed) and the recorded A0k result —
  zero alpha for all-missing layers, finite control pixels byte-identical — is
  preserved *by construction*, because a resident field carries the identical
  bytes to a fresh stage
  (`test_the_resident_field_is_numerically_identical_to_a_fresh_stage` compares
  the encoded bytes of all six images). The shader path was not touched:
  `gweatherrouting/qt/shaders/` is unchanged.
* **The A0k readback could not be re-run, for a pre-existing reason.**
  `tools/diagnose_barb_overlay.py` is the A0k readback owner, and it aborts
  with `ValueError: Reference barb vertex shader changed` against the current
  tree: it expects the *pre-repair* native-indirection vertex shader, while the
  merged A0k repair already lives in `gweatherrouting/qt/shaders/barbs.vert`.
  That staleness is pre-existing and unrelated to this change (neither the tool
  nor the shaders are touched here); it is exactly why the A0k zero-alpha
  readback is recorded as an **open requirement** of the `fill` gate
  (`tools/gates.py`: "the only existing owner is …"). The evidence used here is
  therefore the A0k owner tests plus the byte-identity of the transport.
* **Barbs gates unchanged.** The selection, the anchors, the count and the
  atlas bytes are identical: `numerical_weather.symbols = 1053` and
  `atlas_bytes = 151,776` in every run, matching the recorded baseline and
  the A0k record (`initial_selected_symbols = 1053`).

## 7. Validation run

* `python -m pytest test/test_gpuweather_fields.py test/test_weather_band_cache.py
  test/test_weather_staging.py test/test_weather_barbs.py test/test_gpu_weather.py
  test/test_gpu_weather_jobs.py test/test_barb_shader_safety.py test/test_gpu_weather_map.py
  test/test_gpu_map.py -q` → **63 passed, 2 skipped** (the two skips are the
  native-GPU opt-ins `test_gpu_map.py::test_native_gpu_workspace` and
  `test_gpu_weather_map.py::test_isolated_numerical_map`).
* `black --check` on the 13 changed/added Python files → clean.
* `git diff --check` → clean.
* `tools/diagnose_barb_overlay.py` (the A0k readback owner) → **cannot run**:
  `ValueError: Reference barb vertex shader changed` (pre-existing, see §6).
* The full test suite was **not** run (out of scope for this issue).
* The gate runner was **not** re-run for the other gates: they protect other
  issues and this change does not touch their behaviour.

## 8. What surprised me, and the limits I hit

1. **The gate's threshold was derived from a different program.** The recorded
   `0` uploads
   (`docs/profiles/rendering/2026-10-18/a0c-timeline-scrub-summary.json`)
   was measured on the *independent vertical viewport* — a program that renders
   no chart layer and no resident weather window. Applied to the app's
   chart-inclusive scene counter it cannot be met, because the chart's
   isobars and clock are forecast-time dependent by construction. The two new
   unratified checks in `tools/gates.py` make the split visible instead of
   hiding it.
2. **The weather field was already reused inside a time window.** The app's
   staged before/after pair *is* a resident time window, and a time change
   inside it already cost no upload and no re-decode before this change. What
   was missing was the explicit identity, the budget, the eviction and the
   release — and the re-decode on a *return* to a window that is still
   resident, which the store now removes (1 acquire hit on the workload).
3. **The residual 12 field uploads are genuine content changes, not waste.**
   The scrub crosses the exact-time event at 08:00:00 twice. At that instant
   the app deliberately keeps the exact-time mask and new-cycle selection
   semantics, so the field at the event is *not* the field of the window that
   follows it, and the two differ in their values and in their anchors. With
   the screen-space mesh transport the field therefore has to be published
   twice. The recorded candidate avoided this with node-ID indirection, which
   is the transport of #71/#65/#68.
4. **The host was contended.** The maintainer's parallel benchmark and a
   float-transport experiment kept the load at 6–8 on 8 cores, so the first
   baseline attempts could not finish inside the gate's 110 s budget
   (NOT-EVALUATED, twice) and two further attempts exceeded the worker's own
   600 s budget. The baseline that is quoted was re-measured on a pristine
   `HEAD` worktree once the host was quiet, and it reproduces the recorded
   numbers exactly (41 uploads / 668,015,328 bytes).
5. **The harness has a startup race.** One pristine-worktree run began the
   scrub at the *saved workspace* time (2026-10-14T15:09) instead of the
   snapshot's forecast time (2026-10-19T08:00), which changes the data set
   entirely (25 retained decoded bands instead of 4) and makes its counters
   incomparable. It was detected and discarded; it is worth knowing that the
   benchmark's forecast time can lose a race with the workspace restore.
6. **The total upload count is not a stable measurement.** The chart part
   varies with the number of inputs observed at a render callback (12–15 of
   24), so the total moves by ±2 between identical runs (31–41 observed over
   five runs). The weather-field part (12), the provider request count (18) and
   the atlas bytes (151,776) are stable. A gate that has to compare a total
   like this needs a stable metric, which is one more reason the field-scoped
   metric is the one that should carry a bound.
7. **The A0k readback tool is stale.** `tools/diagnose_barb_overlay.py` cannot
   run against the current tree ("Reference barb vertex shader changed"): it
   expects the pre-repair shader while the merged A0k repair is already in
   `gweatherrouting/qt/shaders/barbs.vert`. Pre-existing and out of scope
   here, but it means the A0k *readback* has no runnable owner — which the
   gate registry already records as the `fill` gate's open requirement.

---

## 9. Blocker (≤150 words)

**Expected.** The `residency` gate's ratified threshold — zero texture uploads
during the scrub input phase — should be reachable by a GPU-resident field
store; the field-scoped upload count should fall to zero.

**Observed.** 39–40 total uploads after the change, identical to the default
path: 27–28 chart-layer uploads (base chart isobars and the navigation clock,
re-rendered on every forecast-time change) and 12 weather-field uploads (two
publications of six images at the two crossings of the exact-time event at
08:00:00, whose content genuinely differs).

**Root cause.** The RGBA8888 screen-space mesh transport publishes one field
per resident time window; the recorded zero-upload candidate measured
node-ID indirection, i.e. the geographic field transport of #71/#65/#68,
which this issue must not touch.

**Next step.** Land #71/#65/#68's geographic field transport, then ratify the
field-scoped zero-upload bound on fixture `1ed1bca1` in `tools/gates.py`.
