# PR #50: bounded worker-private decoded-band residency

Scope extension approved by the user on 2026-10-18, following the M0 probes in
[epic #51](https://github.com/evanlivingston/gweatherrouting/issues/51). The M0
plan, probes and private results remain in their separate worktree; this change
does not import the broader renderer migration into #50.

## Implementation contract

- Replace the potential six-bands-per-selected-source rejection with one
  **128 MiB retained decoded-band budget per GPU weather worker**. Keep all
  selected sources and current sampler ordering, fallback, interpolation and
  vector rotation. Keep existing atlas/node/symbol limits and CPU fallback.
- Use dedicated worker-private source/index subclasses. Do not modify shared
  sampler/routing code or monkeypatch GUI-owned forecast objects.
- Reserve space before full-band decoding; evict least-recently-used decoded
  arrays across sources. Reject an individually oversized band before its read.
  Resource-limit exceptions must escape the sampler's recoverable-error handling
  rather than silently changing source selection or publishing missing values.
- Close/deselect/source-change/shutdown must release retained arrays through
  existing worker-owned source lifetimes. Closing the GUI must not join GDAL.
- Add targeted differential, eviction/reload, oversized rejection, resource
  release, coalescing and native-map checks. Verify the saved real forecast set
  reaches the numerical shader, not merely a hardware scene-graph status label.
- Budget: at most three implementation/targeted-validation iterations and four
  bounded native/real-workload attempts before an explicit review. Native/real
  attempts are capped at 120 seconds each; no larger memory limits to get a pass.
- No native texture/host migration, wind fill, streamlines, default-on change,
  unrelated cleanup, merge or propagation to other branches in this slice.

The 128 MiB budget is **retained NumPy decoded arrays**, not total RSS, GDAL
cache, decoder/mask temporaries, active caller references, atlas arrays/images,
Qt textures, or VRAM. Existing allocation envelopes and fallback remain needed.

## Evidence motivating the change

On the saved 33-source view, the potential estimate was 1,369,235,952 bytes while
actual #50 staging retained 60,512 decoded bytes, generated 734 symbols and
41,760 atlas-image bytes. Full native-node staged values matched finite masks
and provenance at three instants; maximum display float32 error was 4.38e-7 m/s.
Those probes did not prove production GPU activation or a full-memory plateau.

## Results

### Implementation

`rendering/weathercache.py` provides dedicated `DisplayWeatherSource` and
`DisplayGridIndex` subclasses for the numerical worker. They preserve the shared
index's decoding, masks, float64 arithmetic, per-source six-band behavior and
selection machinery, while a worker-wide LRU accounts for retained arrays.
`WeatherBandLimitError` deliberately does not inherit the sampler's recoverable
exception classes. Unsupported resources therefore reject the staging request
and retain CPU rendering instead of changing source provenance.

`WeatherStager` now releases private sources/arrays on invalidation as well as
source changes and final shutdown. A completed-request statistics snapshot crosses
to the GUI; no GDAL handles or mutable decoded arrays do. New benchmark telemetry
distinguishes a ready/visible numerical shader with no CPU symbols from the mere
`GPU: Metal` backend label.

### Validation

- **40 targeted worker/cache/staging/transport checks passed**, one GDAL warning.
  Tests include global eviction and exact reload, source/missing-data parity,
  budget rejection before reads, fail rather than select another model, private
  ownership, source release on invalidation, and shutdown during blocked staging.
- **11 benchmark/snapshot/telemetry checks passed.** Hardware-backend status alone
  is explicitly insufficient to report active numerical symbols.
- **One native numerical-map integration check passed** on Cocoa/Metal, Qt 6.12.
  It covers 120 exact-second synthetic scrubs with zero numerical image reloads,
  pan/source/style transitions and release after worker shutdown. Its synchronous
  readback diagnostic was 37.5 ms median / 56.6 ms p95 and 14.3 ms GUI CPU median;
  this is synthetic, not an interleaved comparison or presentation FPS.
- Black passed on all **seven changed Python files** in their targeted slices;
  whitespace checks passed. No shared sampler/routing code changed and no full
  suite was run for this slice.

### Real-data functional fixture and results

The original snapshot was found incomplete before the real-workload launch:
3,608 manifest-listed files were missing, with no added/changed surviving files.
The checksum guard stopped the run before application launch. It was **not**
disabled and the damaged fixture was not rewritten.

A new functional fixture was constructed from checksum-matching copies of the
original files available in other snapshots/installed data. Every retained file
matches its original checksum; all 33 forecasts, settings, saved scene and frozen
clock are retained. The only unrecoverable files were 370 marine `source-cache`
download-cache entries. They are explicitly omitted from the new manifest:

- Original content: `bd9464bf39629676d34fc570c2f8b6d647a8e6ff897176fd94e0a17200f51dac`.
- Derived functional content: `97cfda3c62a494c3d462ee8cde449d4d80ed1c62f387d70edcd9192dfb65d360`.
- Derived manifest: 4,569 files; clock `2026-10-16T08:00:00` local.
- Both successful app runs restored 33 forecasts, 32 enabled charts, dense barbs,
  1 hPa isobars, six tide datasets, zero marine fields and the saved navigation.

This is **not** an identical performance baseline or a reason to reinterpret
historical timings. Private data and raw reports remain outside Git.

| Functional replay | Numerical symbols during replay | Final numerical state | Retained decoded peak | Warnings |
| --- | --- | --- | --- | --- |
| Scrub/return, 24 frames | 23/24; initial pair transition pending | 734 symbols; shader ready; no CPU symbols | 60,512 bytes | 0 |
| Pixel-delta pan, 24 frames | 0/24; existing exact-view restaging fallback | 522 symbols after settling; shader ready; no CPU symbols | 61,296 bytes | 0 |

The scrub loaded six startup images and one six-image replacement endpoint pair;
the pair was reused for later scrubs. No resource-budget fallback was observed.
The numerical path is now genuinely active on the selected forecast workload,
but **continuous pan is not solved**. CPU fill/isobars, navigation, exact-view
atlas regeneration and other pending renderer work remain.

Four native/real-workload attempts were used: one synthetic integration pass,
one blocked original-fixture verification, and two derived functional passes.
Each initial command was bounded to 120 seconds. The separate CI-review budget
below was recorded before its follow-up; no memory limit was increased. The
[sanitized summary](profiles/rendering/2026-10-18/pr50-residency-functional-summary.json)
preserves the key evidence without private paths/coordinates.

### Review / remaining gates

**Decision: retain this narrow residency fix for human review, leave #50 opt-in
and unmerged.** The saved-view eligibility defect has functional evidence; there
is no claim of end-to-end CPU-renderer speedup, instant scrubbing, fluid pan,
power savings, total-memory bound, long-session plateau, minimum-Qt execution
or non-Metal validation. Changed-time retries for an unchanged unsupported
resource workload remain a follow-up. The broader renderer epic stays separate.

### CI review follow-up budget

CI on `21b40d5` identified a new exception-name convention failure, an incompatible
cache-interface type, and a provider-list annotation in the changed code. It also
flags complexity in the worker method extended by this change. Fix those issues
in place, without suppressions, shared-sampler edits, or unrelated CI/dependency
repairs. Preserve real cache behavior rather than satisfying typing with a cast.

Budget: one additional focused implementation/validation iteration and one real-
data native activation attempt (120 seconds). Refactor only worker resource
ownership and request handoff needed to keep the changed methods reviewable;
rerun the worker/cache/staging checks and a native functional replay. Existing
`nmeatoolkit` collection failures and diagnostics elsewhere remain separate; no
full-suite pass is claimed.

Results: renamed the exception to `WeatherBandLimitError`, retained GridIndex's
concrete OrderedDict cache contract with local/global references to the same
arrays, and extracted worker-only resource lifetime/request handoff helpers.
No type ignores/casts or shared-sampler edits were used. The global eviction
removes both references; tests exercise hits, eviction, reload and close.

- **40 targeted checks passed** after these corrections, one warning.
- Targeted Flake8 and mypy (`--follow-imports=silent`) passed on **both changed
  production modules**; Black passed on four newly changed Python files;
  whitespace passed. Unchanged benchmark/telemetry checks were not repeated.
- **One fresh derived real-data native scrub replay passed**: numerical shader
  active, 734 symbols, 23/24 replay frames numerical, 60,512 retained decoded
  bytes, 12 image requests including the initial pair replacement, no warnings.
- The pan numbers above belong to `21b40d5`; pan was not rerun under this
  follow-up budget. No pan improvement or full CI pass is claimed.

The recorded follow-up budget is consumed. Leave unrelated dependency/type/lint
failures to their own work rather than turning this residency fix into CI cleanup.
