# M0 / E2: retained field versus native interaction

2026-10-18; [canonical plan and contracts](rendering-performance-plan.md),
[epic #51](https://github.com/evanlivingston/gweatherrouting/issues/51).

## Review decision

**Retained numerical data is useful, but a worker-only cache is not a demonstrated
pan-fluidity fix.** Do not promote an isolated worker speedup as application
performance. The next design must separate persistent field data from current
view selection and presentation, not merely make exact-view atlas jobs faster.

The experiments below do not edit #50 or routing/shared sampling code. They use
its modules read-only and, for the native control, install hooks only inside a
disposable benchmark process. #50's approved decoded-band fix remains separate,
published as `21b40d5` / `175096d`, opt-in and unmerged.

## E2a — retain values, recompute the current portrayal

The probe stages one immutable field for the saved viewport plus 256 logical
pixels of coverage, then supplies its numerical values to the unchanged #50
native-node selection/atlas algorithm at exact requested coordinates. It does
**not** translate a previously thinned scene. Screen-cell selection, count cap,
anchors and images are regenerated for each current view by the reference code.

Reference and retained query order are interleaved across nine horizontal
forward/reverse/return offsets, repeated three times in one private worker.
The clock is the saved instant plus one exact second (interior to an interval).
The source set and derived functional fixture are those in the
[residency report](rendering-m0-residency.md), with all 33 forecast files retained.

During retained queries the sampler's `_side` and every index's `array` entrypoint
raise hard errors. A warm decoded-band cache cannot conceal numerical resampling.
Cancelled, unknown-coordinate, incompatible-interval, oversized and out-of-
coverage requests reject instead of producing blank/partial success. The field
copies its publication buffers into read-only arrays.

### E2a result

- **27/27 pairs:** exact endpoint-array/coordinate/provenance signatures and all
  six atlas-image byte hashes, shapes, byte sizes and selected symbol counts match.
  Selected counts vary from 722 to 737; the candidate does not win by drawing less.
- Numerical preparation was blocked for every retained query; outside-coverage
  rejection passed. Field preparation sampled 1,921 unique retained native nodes
  from 3,927 candidates.
- Warm reference worker median/p95: **20.1 / 42.5 ms**. Retained lookup plus current
  portrayal/packing: **2.7 / 2.9 ms**. One-time wider field preparation: **80.8 ms**
  after source/index initialization. These are isolated worker spans, not FPS.
- Coordinate and endpoint arrays occupy **61,472 bytes**; Python lookup-object
  overhead is not included in that number. Decoded-band peak **62,080 bytes**;
  process high-water RSS **182,353,920 bytes**. No total-memory/plateau claim.
- Seven tests and three subtests passed; Black passed on two files; whitespace
  passed. One implementation/check iteration and one real-data process consumed
  the declared E2a budget.

Raw result: `rendering-m0-retained-field.json` in the private OpenCode workspace.
Harness SHA-256:
`16d6ac8986633f795c68bf181f961cb4d899119307604216cbb3185faa680a06`.
The raw report retains per-pair hashes, not just a pass flag. This is a single
process with correlated warm queries, not independent full-app repetitions.

## E2b — native-app control

The native reference and retained control both run the #50 app at `175096d`,
restoring the same derived fixture and saved window. The control holds a wider
field in the worker and injects its lookup into a copy of the portrayal function's
globals; it does not patch a shared module while other workers use it. Numerical
preparation is blocked during retained selection, the initial atlas is byte-
verified against the normal path, source fingerprints remain checked, and worker
ownership/shutdown remain intact.

Both runs dispatch 24 eight-pixel wheel inputs with forward/reverse motion at
16.667 ms intended cadence. B1's non-readback probe timestamps direct render
callbacks and binds each callback to the synchronized submitted frame, retaining
all input revisions. Per-frame metadata distinguishes numerical pairs, ready
shaders, positive opacity and CPU fallback batches/raster.

The comparison checks app/harness hashes, environment, full startup/viewport and
**every input's camera/time state**. Those identities matched. Input-phase counts
end one interval after the last dispatch; two-second drain callbacks are separate.

### E2b result: mixed, not an application win

| Observation | Reference | Retained control |
| --- | ---: | ---: |
| Dispatched inputs | 24 | 24 |
| Input revisions observed at any input-phase render callback | 18 | 17 |
| Revisions with clean numerical-ready submitted state observed at a callback | 0 | 1 |
| Input-phase callbacks | 19 | 20 |
| Callbacks including drain | 21 | 22 |
| All-input dispatch lateness p50 / p95 | 7.8 / 23.9 ms | 8.3 / 17.7 ms |
| Observed-only first-any-render p50 / p95 | 13.8 / 21.9 ms | 14.7 / 33.0 ms |
| Texture uploads, including drain | 13 | 31 |
| Texture-upload bytes, including drain | 19,670,496 | 19,795,776 |
| Vertex-upload bytes, including drain | 12,496,080 | 11,122,416 |

The control built and reference-verified one field before input and completed
**24 retained selections during input with zero new field builds**. It still
submitted 24 no-pair frames, four numerical-pair frames alongside CPU batches,
and only one clean numerical frame in that window. Reference submissions included
23 no-pair, one mixed, and one clean numerical state; its clean state was not
observed within the input window.

These are **submission-state observations associated with render callbacks**,
not GPU-completion, pixel-readback or presentation proof. A mixed submission is
deliberately not counted as clean numerical rendering; the source makes both
portrayal paths eligible, but this run did not perform a temporal pixel-overdraw
test. Readiness can change after submission, so counts are conservative, not a
claim to observe the compositor's exact contents.

There are too few observations and only one fresh process per control to infer
a stable performance regression or improvement. The proxy tail is conditional
on different observed subsets; missing/coalesced inputs stay in the denominator.
**The isolated worker result did not translate into demonstrated native latency
or sustained numerical-readiness improvement.** No end-to-end speedup is claimed.

Validation: **18 tests passed** across native-control comparisons and B1 recorder/
adapter checks; Black passed on four files; whitespace passed. Two native controls
and their strict comparison exited 0. This consumed E2b's native-process budget;
no tuning/retry matrix was launched to seek a better outcome.

Private reports: `rendering-m0-native-retained-reference.json`,
`rendering-m0-native-retained-candidate.json`, and
`rendering-m0-native-retained-comparison.json`. The sanitized
[mechanism summary](profiles/rendering/2026-10-18/m0-mechanism-summary.json) preserves
the result without private paths/view coordinates.

## Architecture implication / next gate

1. Keep authoritative sampling and source selection in bounded worker-owned
   preparation. Reuse immutable numerical fields by source generation, interval
   and coverage, not exact camera position.
2. Keep those field textures resident during covered camera motion. Do not publish
   six new numerical/anchor images or rebuild the entire portrayal merely because
   the view changes. Native float transport is possible but raw weather payload
   bandwidth has not been shown to dominate this saved scene.
3. Separate `ViewState` and exact selection from field ownership. A small current-
   view index/mask/geometry payload should reference stable field data; camera/time
   uniforms should not wait for another field job. Preserve native membership,
   screen-cell thinning, cap-before-shoreline order and source provenance.
4. Define one atomic symbol presentation mode. Do not silently oscillate between
   CPU geometry and new numerical resources or treat mixed submissions as a win.
   If previews are used, explicitly bound their coverage/freshness and distinguish
   them from exact-current portrayal; do not silently relax selection invariants.
5. Address GUI assembly and chart/weather invalidation separately. Faster worker
   sampling does not remove main-thread frame assembly, background image uploads
   or exact-view acceptance churn. These are next attribution/design targets,
   not proven causes of every tail spike from this single pair.

Before a production pan change, specify immutable field/selection generation keys,
resource reservations (including lookup/index overhead and replacement overlap),
no-GDAL GUI boundaries, missing/source-event behavior and native lifecycle/fidelity
checks. The prototype's Python coordinate dictionary is not an unbudgeted
production cache design. A compact bounded index needs its own validation.

**Decision: retain the data-reuse mechanism as a design input; do not ship the
worker-only prototype as the pan fix.** The next bounded prototype should test
persistent field resources plus per-view selection/atomic presentation. It stays
outside #50's already approved narrow residency scope unless the user explicitly
authorizes another scope change. M0 and the broader renderer epic remain open.
