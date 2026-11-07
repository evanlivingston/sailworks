# ADR-001: GRIB rendering architecture — reconciliation and decisions

- **Status:** Accepted. D2 was decided on 2026-10-19 in favour of option (a); the
  amendment is applied in §6. D6 (target interaction path) was decided on
  2026-10-19 in §4.6. D7 records the user's direction to use the viewport in the
  normal application; D8 records the decision to retire the old map without CPU
  recovery. The #70 keep-experimental evidence remains recorded, but those later
  user directions supersede its rollout recommendation.
- **Date:** 2026-10-19
- **Supersedes:** nothing. **Amends:** [`rendering-implementation-plan.md`](rendering-implementation-plan.md)
  (the R0–R5 sequence and the invariants at lines 43–70), as recorded in §6.
- **Related epic:** [#51](https://github.com/evanlivingston/sailworks/issues/51)

This ADR is **not** a new architecture. The target architecture already exists in [`rendering-implementation-plan.md`](rendering-implementation-plan.md) — see "Architecture and
ownership" (lines 71–123) and the R0–R5 implementation sequence (lines 130–266). This document reconciles that specification with the 2026-10-19 current-state analysis and the
reference-technique analysis, records the resulting decisions, and maps them to executable issues. It deliberately does **not** restate the specification; it links to it.

## Current checkpoint

**ENC application rollout direction (after #80):** the user authorized enabling the completed ENC worker in the normal application viewport, retaining legacy recovery, without a
pixel-for-pixel legacy matching requirement. The bounded implementation/acceptance contract is [enc-default-rollout.md](enc-default-rollout.md). This revises the default-off
direction for this ENC slice only, subject to its application-level functional gate. That gate subsequently passed 13/13 on the verified fixture (current pan/zoom/resize/time views
and legacy recovery), with 140 targeted passes and one known base failure; see the rollout evidence. It does not declare the #70 platform/soak or presentation-latency program
complete and does not change numerical sampling.

- **Canonical current state and invariants:**
  [`rendering-performance-plan.md`](rendering-performance-plan.md) — start at its
  "Current state". The archived decision log is in
  [`history/rendering-decision-log.md`](history/rendering-decision-log.md).
  This ADR is the decision input to that roadmap.
- **Implementation program:** epic #51, issues
  [#62–#70](https://github.com/evanlivingston/sailworks/issues/62); the
  local index is [`milestones.md`](milestones.md). One issue at a time; each
  states its own gate, evidence and validation command.
- **R0–R5 implementation detail, invariants and measurement contract:**
  [`rendering-implementation-plan.md`](rendering-implementation-plan.md), as
  amended by §6 below.
- **Experiment record:** the A0a–A0l slices in
  [`rendering-architecture-evaluation.md`](history/rendering-architecture-evaluation.md)
  and in the archived
  [`decision log`](history/rendering-decision-log.md); the M0 mechanism probes
  in the `rendering-m0-*.md` records and in
  [`history/rendering-experiment-records.md`](history/rendering-experiment-records.md);
  machine-readable evidence under `docs/profiles/rendering/`.
- **Status:** D1–D6 are settled. D7's application integration was delivered by
  PR #91; D8's legacy-map retirement is in progress under #86. §10 requires
  explicit user forecast selection in the Weather panel. Fidelity/performance
  acceptance remains incomplete. No new A0 slice is authorized.
- **Wider documentation map:** [the documentation index](README.md).

---

## 1. Context

### 1.1 What the analyses established

| Analysis | Result |
| --- | --- |
| [`grib-architecture-analysis.md`](grib-architecture-analysis.md) | Current-state architecture with file/line citations, and the structural gaps listed in §3. |

Two conclusions from the reference analysis materially shape this ADR:

1. **The reference implementation performs spatial interpolation on the CPU** (T6), not on
   the GPU. GPU spatial interpolation (D2) is therefore an advance *beyond* the reference,
   not parity work, and must never be justified as "the reference does it this way."
2. **The reference was not observed to have a unified residency budget or eviction**
   (T7). Our D1 therefore extends, rather than mirrors.

Four of the seven gaps have a direct reference analogue (T1/T4 async structure, T2 tiled storage, T3 retained scene graph, T5 animation-state time switching). Three do not (GPU
spatial interpolation, unified GPU budget, cache-key coherence) and are places where we can exceed the reference.

### 1.2 What the existing specification already settles

These are **not** re-decided here. Citing them so that no agent reopens them.

| Settled | Evidence |
| --- | --- |
| Host: mouse-transparent `QQuickWidget` for base/navigation/wind, sibling `QWidget` for route/track/AIS/dashboard | [`gpu-embedded-rendering.md`](gpu-embedded-rendering.md) line 12 and following. R0.2 is effectively discharged. |
| A resource ledger with explicit trial limits (512 MiB estimated GPU storage, 128 MiB CPU scene snapshots, 128 MiB upload payload) | [`gpu-embedded-rendering.md`](gpu-embedded-rendering.md) lines 24–26. R5 is partially discharged. |
| Layer/ownership model: `ViewState`, `ContentKey`, `LayerFrame`, UI/worker boundary, GPU boundary, publication generation rejection | [`rendering-implementation-plan.md`](rendering-implementation-plan.md) lines 77–120. |
| Non-negotiable invariants (sampling, masks, interpolation, source ordering, fallback, density, glyph geometry, CPU fallback retained) | [`rendering-implementation-plan.md`](rendering-implementation-plan.md) lines 56–70. |
| Measurement contract and required acceptance evidence per area | [`rendering-implementation-plan.md`](rendering-implementation-plan.md) lines 267–292. |
| Program-level CPU-vs-GPU matched baseline (stationary p99 51.8→11.0 ms, pan 90.7→54.2 ms, zoom out 98.3→70.3 ms) | [`gpu-embedded-rendering.md`](gpu-embedded-rendering.md) lines 44–46. **Readback timings, not presentation.** |

---

## 2. Scope of this ADR

**In scope:** layer boundaries, ownership, data representations, scheduling model, and the disposition of the seven structural gaps.

**Explicitly out of scope (unchanged non-negotiables):** routing calculations, source selection and ordering, weather value fidelity, portrayal density/thinning/geometry, the CPU
fallback path, the GTK frontend, network acquisition policy, and user-data mutation. These remain as stated in
[`rendering-implementation-plan.md`](rendering-implementation-plan.md) lines 56–70.

---

## 3. Structural gaps and disposition

The seven gaps, as named in [`grib-architecture-analysis.md`](grib-architecture-analysis.md):

| # | Gap | Disposition | Decision | Issue |
| --- | --- | --- | --- | --- |
| G1 | Decode location: CPU (GDAL → NumPy) | **Deferred** | D5 | — |
| G2 | Data residency: CPU-side caches, no GPU-resident field | **Closed** | D1 | [#64](https://github.com/evanlivingston/sailworks/issues/64) |
| G3 | Spatial interpolation: CPU Python loops | **Closed** | D2 | [#65](https://github.com/evanlivingston/sailworks/issues/65) |
| G4 | Pipeline model: strictly serial stages | **Closed** | D3 | [#66](https://github.com/evanlivingston/sailworks/issues/66) |
| G5 | Time scrubbing: two-timestep, view-specific restage | **Closed** | D4 | [#67](https://github.com/evanlivingston/sailworks/issues/67) |
| G6 | Memory management: per-worker CPU byte limits, no GPU budget | **Closed** | D1 | [#64](https://github.com/evanlivingston/sailworks/issues/64) **Amended by [#109](https://github.com/evanlivingston/sailworks/issues/109):** decoded CPU bands are exact-identity keyed and one session-scoped byte-accounted cache is shared by wind, wave and chart consumers; each consumer keeps its own GDAL handle. |
| G7 | Threading model: serialized daemon threads, no overlap | **Closed** | D3 | [#66](https://github.com/evanlivingston/sailworks/issues/66) |

The analysis's comparison table additionally lists *resource granularity* (full-frame textures). That is a consequence of G2 and is addressed by D1, not tracked separately.

---

## 4. Decisions

### 4.1 D1 — GPU-resident numerical field (`LayerFrame` extended)

**Decision.** Add a **GPU-resident numerical field** alongside `LayerFrame`.

`LayerFrame` is currently defined as "immutable pixels or geometry plus content key…" ([`rendering-implementation-plan.md`](rendering-implementation-plan.md) lines 106–109) — i.e.
a CPU-produced artifact handed to a presenter. Weather fields additionally need a form whose *numeric values* live on the GPU and are addressed by a stable identity, so that a time
change or a pan within covered area reads the resident resource rather than re-uploading.

**Field identity** is: source + parameter + time instant + spatial window + resolution. It is deliberately **not** a per-frame image identity.

**Revises:** the `LayerFrame` definition (lines 106–109) and extends the R5 resource ledger (lines 251–253) to account for GPU-resident numerical fields alongside GPU textures.

**Transport format.** Native float textures, with explicit shader arithmetic for interpolation, replacing the current RGBA8888 bit-packing. Justification:

- Hardware linear filtering on native float textures **failed** the 2e-5 gate (max error
  2.6e-3); explicit `texelFetch` + manual bilinear arithmetic **passed** (max error
  2.4e-7) — [`rendering-m0-float-textures.md`](history/rendering-m0-float-textures.md), E1/E1b.
- Missing values must not enter shader varyings as NaN; the A0k repair (integer-exponent
  validity checks, finite missing-cell outputs, explicit validity discard) is the
  validated pattern — [`rendering-a0k-shader-safety.md`](history/rendering-a0k-shader-safety.md).

**Residency budget.** The existing ledger is extended, not replaced. Eviction protects visible content first and reduces speculation before it reduces selected-source fidelity, per
lines 257–259.

**Issue:** [#64](https://github.com/evanlivingston/sailworks/issues/64)

### 4.2 D2 — GPU spatial interpolation (**DECIDED 2026-10-19: option (a)**)

**Conflict.** [`rendering-implementation-plan.md`](rendering-implementation-plan.md) line 216 states, under R3:

> Keep the routing/sampling numerical interface stable.

Moving spatial sampling onto the GPU changes that interface. This cannot be left implicit: the R3 invariant and the target architecture would then disagree.

**Options.**

| | Option (a) — amend the invariant | Option (b) — keep CPU sampling |
| --- | --- | --- |
| Mechanism | Sample the resident field on the GPU with explicit bilinear arithmetic | Resample on the CPU, upload the resampled field |
| Effect on the R3 invariant | Requires an explicit, reviewed amendment with a stated error bound | No change |
| Fidelity | Bounded by the existing 2e-5 gate, which becomes the amendment's error bound | Same values as today |
| Benefit | Removes the largest remaining CPU cost from the interaction path | Removes per-frame upload only; CPU sampling cost remains |
| Reference parity | **Beyond the reference** — the reference samples on the CPU (T6) | Matches the reference |

**Decision: option (a) — amend the invariant.** Spatial interpolation may be evaluated on the GPU against a resident numeric field, bounded by the existing 2e-5 gate. The amendment
is applied to [`rendering-implementation-plan.md`](rendering-implementation-plan.md) at R3 item 4 and recorded in §6.

This is an **advance beyond the reference**, which samples on the CPU (T6). It must not be described as parity. The CPU sampler remains the reference authority and the fallback
path, and the 2e-5 gate is unchanged.

**Issue:** [#65](https://github.com/evanlivingston/sailworks/issues/65) — now unblocked.

### 4.3 D3 — Frame N / N+1 overlap

**Decision.** The pipeline model gains explicit **double buffering**: two sets of prepared resources alternate, so preparation of frame N+1 overlaps presentation of frame N.

This extends, and does not replace, the existing scheduling work. R2 already requires that no file access or expensive portrayal happen on the interaction thread and that requests
be coalesced (lines 201–203); R4 already defines the priority order visible > predicted > nearby > adjacent zoom (lines 231–232). Neither states that two preparations may be in
flight, nor that a superseded request must not publish.

**Added requirements:**

- At most two preparations in flight; the older is cancelled or superseded.
- Every prepared resource carries a generation key (source/time/coverage/scope);
  publication is rejected on generation mismatch.
- Coverage misses, stalled preparation, and unsupported states are **explicit** and
  distinguishable from preview/current/missing/unavailable coverage (extends line 120).
- No stale or clipped content may be presented as current.

**Revises:** the pipeline diagram (lines 77–93) and adds the generation/overlap requirements to R2 and R4.

**Issue:** [#66](https://github.com/evanlivingston/sailworks/issues/66)

### 4.4 D4 — Resident time windows

**Decision.** `ContentKey`'s "forecast instant/bracket" (line 103) is extended to a **resident time window** covering multiple forecast instants over one spatial window.

Consequences:

- A time change within the window performs **zero** decodes and **zero** uploads.
- The staged `before`/`after` pair becomes a special case of a general window; the
  existing GPU temporal blend (`blend` uniform) generalizes to any instant in the
  window.
- **Time is decoupled from spatial preparation.** A time change must not invalidate
  spatial resources, and a pan must not invalidate time resources.
- Lookahead prepares the next window while the current one is displayed, so an interval
  crossing does not stall. Interval crossings, source changes, and coverage misses show
  an accurate loading state — a cached smooth scrub must not conceal poor settling.

**Revises:** the `ContentKey` definition (line 103) and extends R3's reuse requirement (lines 212–215) from spatial to temporal reuse.

**Issue:** [#67](https://github.com/evanlivingston/sailworks/issues/67)

### 4.5 D5 — GPU decode: explicitly deferred

**Decision.** GPU-side GRIB decompression is **deferred**, with no near-term issue.

Reasoning: R3 already warns "Do not assume windowed GRIB reads avoid full decoding; measure actual driver behavior before choosing that optimization" (lines 222–223). The profiled
cost that matters for interaction is not the decompression itself but the per-frame re-upload and the CPU sampling loop, which D1 and D2 address. GPU decode would additionally
require a decoder available to the GPU, which is a much larger dependency than the residency work, and would change the numerical values produced.

**Revisit when:** D1 and D2 are landed and the remaining decode cost is re-profiled against the new baseline. It is a separate decision, not a silent omission.

### 4.6 D6 — Independent viewport is the target weather interaction path

**Decision (2026-10-19).** The independent GRIB viewport (`sailworks/qt/gribviewport.py`) is the target weather interaction path. The existing weather frame path
(`TrackMap._gpu_frame` / MapFrame / MapScene / WeatherStager / ContentKey) is legacy and is retired once #69 and
#70 complete the layer migration. The viewport owns camera/time state, field demand and field/selection portrayal, and reuses the existing
decoder, sampler, source semantics and shaders.

Consequences:

- D1–D4 apply to the viewport's field store, preparation pipeline and time
  windows. Issues #64–#68 are re-pointed to that vehicle.
- The default application path stays unchanged until #70 makes an explicit
  rollout decision. Viewport work stays behind its standalone entry point and
  runtime flags.
- Charts, fill, contours and routing are not yet in the viewport. #68 and #69
  provide them before #70 can consider a default switch.
- Numerical, mask, source-order and selection gates are unchanged. Visual
  fidelity remains a separate, currently deferred gate.

**Revises:** the program target implied by §4.1–§4.4 and the issue mapping in §3. **Issues:**
[#64](https://github.com/evanlivingston/sailworks/issues/64)–[#70](https://github.com/evanlivingston/sailworks/issues/70); review vehicle
[#72](https://github.com/evanlivingston/sailworks/pull/72).

### 4.7 D7 — User-directed application use (2026-10-20; implemented in PR #91)

**User decision after reviewing the proposal:** start using the new viewport in the normal application, and add explicit forecast selection to the **Weather panel**. Prioritize
these two implementation tasks over further issue/branch cleanup. The choice of forecast belongs to the user, not an automatic multi-file fallback policy.

This authorizes the integration/default-use direction despite the recorded open checks; it does **not** claim that those checks passed. PR
#91 moved normal Charts/Logs presentation onto the viewport without silently changing forecast selection. D8 below supersedes D7's legacy
recovery requirement. Unsupported states remain explicit; routing calculations remain unchanged.

**Original #70 recommendation, retained as evidence rather than the current usage decision:** keep the independent viewport **experimental**. Do **not** switch the application's
default weather interaction path to it on the evidence recorded by [#70](https://github.com/evanlivingston/sailworks/issues/70).

D6 named the viewport as the target interaction path and reserved the usage decision for the user. That decision is now recorded above; the evidence and limitations below are
unchanged by the decision to begin using it.

**What the evidence says (measured, 2026-10-20, fixture `1ed1bca1…`).**

| Question | Answer | Where |
| --- | --- | --- |
| Does candidate availability survive run-to-run variance? | Six candidate-only repetitions, three before and three after the soak: 0 loading inputs in every phase, `continuous_weather` true in all six, same-payload value error 5.96e-08 and same-lattice placement error 4.17e-07. These are not alternating baseline/candidate pairs and do not prove comparative improvement or original-sampler fidelity. | [viewport-validation-summary.json](profiles/rendering/2026-10-20/viewport-validation-summary.json) |
| Is there an accounted-payload plateau, not a high-water mark? | Over 21,216 steps/900.0 s the store settles at 29,610,216 B of 128 MiB, 12 fields/no eviction; the memo at 15,627,216 B of 16 MiB; thirds agree within 0.0%. This is not RSS/VRAM or eviction-stress acceptance. | the same summary |
| Are the cold load, the coverage miss and the interval crossing separate? | Yes: 1,429.9 ms cold load, 265.2 ms coverage-miss recovery (22.1 ms to the honest loading state), 172.9 ms interval-crossing recovery (31.3 ms to the honest loading state). | the same summary |
| Does every layer report its own GPU / loading / fallback state? | Yes: the fill, the barbs and the chart each report the GPU path, an explicit loading state and no fallback. | the same summary |
| Does the retained CPU fallback work? | Yes: 24 of 24 frames current on the application's own CPU renderer on the same fixture. | the same summary |
| Is the platform matrix complete? | **No.** A device-pixel-ratio change and a Metal device loss cannot be exercised on this host; the OpenGL backend does not reproduce the Metal result (its framebuffer readback is empty); the Vulkan and software backends do not present the viewport at all. | the same summary |
| Is the workload matrix complete? | **No.** A marine-heavy workload and the application's two-map view are not in the vehicle at all. | the same summary |

**Why #70 recommended *keep-experimental*, not evidence-based acceptance.** The interaction contract is the strongest result this program has produced, and it is not enough to
switch a default: visual fidelity is still failed and deferred, the measurement endpoint is a render-callback proxy (presentation latency and input-to-present latency are
**unmeasured**), two workload cells and three platform conditions are reported as gaps rather than closed, and the soak's plateau criterion and the three separate limits are
**unratified** — the numbers are reported, but no recorded acceptance bound backs them.

**What the proposal does not cover.** Visual fidelity. Presentation or input-to-present latency. A device-pixel-ratio change. A Metal device loss. Marine layers. The application's
two-map layout. The default application's own start-up and draw path.

**Original proposal's consequences (superseded):** retain the default path and experimental flags, and postpone retirement of the legacy frame assembler pending user approval. The
#70 summary's `recommendation.next_concrete_steps` calls for ratifying or deliberately leaving unratified the plateau and limit bounds, measuring presentation at the compositor,
closing or recording the marine and two-map cells, and deciding the OpenGL backend.

**Issues:** [#70](https://github.com/evanlivingston/sailworks/issues/70), [#51](https://github.com/evanlivingston/sailworks/issues/51).

### 4.8 D8 — Retire the legacy map without CPU recovery (2026-10-22)

**User decision after PR #91:** continue the application migration by retiring `TrackMap`/`mapcanvas.py`. The old QWidget map and its CPU rendering/recovery path are not required
as a fallback. If viewport rendering is unavailable, report that state explicitly; do not silently present a blank or stale map.

This supersedes D7's requirement to preserve the legacy renderer as a recovery path and #86's original prerequisite to retain the old map until #70's full gate passes. It does
**not** change the #70 evidence: the viewport's presentation latency, visual fidelity, unsupported backends, marine-heavy workload and two-map workload remain recorded limitations,
not passed results. No performance claim is made by this decision.

The old base-chart painting defect in #84 will not be repaired in the legacy path; once that path is removed, #84 should be closed as superseded, with the viewport's own
missing-data/error reporting retained.

---

## 5. Relationship to the existing sequences

Three numbering schemes now coexist. This section fixes their relationship so that agents do not follow the wrong one.

| Scheme | Scope | Granularity | Authority |
| --- | --- | --- | --- |
| **R0–R5** ([`rendering-implementation-plan.md`](rendering-implementation-plan.md) lines 130–266) | Implementation-detail sequence | Milestone with exit criteria | **Canonical for implementation detail.** |
| **M0–M4** (epic #51, GitHub milestones) | Program phase | Phase | Canonical for program phase and target ratification. |
| **#62–#70** (GitHub issues) | Executable unit | Single bounded change with one gate | Canonical for what an agent works on. |

**Rule.** An agent works **one issue**. An issue's scope is a *subset* of the relevant R-slice; the R-slice exit criteria still apply and are not weakened. Where this ADR and the
R-sequence disagree, **this ADR wins** (it is the later decision), and the R-sequence is amended as recorded in §6.

---

## 6. Amendments to `rendering-implementation-plan.md`

Applied on 2026-10-19, when D2 was decided. Listed explicitly so the spec remains the single roadmap.

| Location | Amendment |
| --- | --- |
| Lines 106–109 (`LayerFrame`) | Add a GPU-resident numerical field form; field identity is source+parameter+instant+window+resolution, not image identity. (D1) |
| Lines 251–253 (R5 ledger) | Ledger additionally accounts for GPU-resident numerical fields. (D1) |
| Line 234 (R3 invariant) | **Amended (D2 = (a)):** spatial interpolation may be evaluated on the GPU against a resident numeric field, provided values match the reference sampler within 2e-5, missing/mask semantics match exactly, and global-grid wraparound behavior matches. |
| Lines 77–93 (pipeline) | Add explicit N/N+1 overlap, ≤2 in flight, and generation-key publication rejection. (D3) |
| Line 103 (`ContentKey`) | "Forecast instant/bracket" becomes a resident time window over multiple instants. (D4) |
| Lines 212–215 (R3 reuse) | Reuse extends from spatial to temporal. (D4) |
| §4.6 (program target) | The independent viewport is the target weather interaction path; issues #64–#68 are re-pointed to it and the frame assembler is retired at #69/#70. (D6) |
| §4.7 (user direction, 2026-10-20) | **Implemented in PR #91:** use the new viewport in the application with user-controlled forecast selection. The original #70 keep-experimental recommendation and open checks remain evidence, not retroactively passed gates. (D7) |
| §4.8 (user direction, 2026-10-22) | **In progress under #86:** retire the old map without requiring its CPU recovery renderer; unavailable viewport states remain explicit. The #70 evidence gaps remain documented. (D8) |

**Deferred and not amended:** R1 (host), R0.3/R0.4 (resource envelope, platform matrix), R5 (retention hardening, rollout). R0.2 is discharged by
[`gpu-embedded-rendering.md`](gpu-embedded-rendering.md).

---

## 7. Evidence index

| Artifact | Used for |
| --- | --- |
| [`grib-architecture-analysis.md`](grib-architecture-analysis.md) | Current state; the seven gaps. |
| [`rendering-implementation-plan.md`](rendering-implementation-plan.md) | The architecture being reconciled; invariants; R0–R5; measurement contract. |
| [`gpu-embedded-rendering.md`](gpu-embedded-rendering.md) | Host decision; resource ledger; matched CPU/GPU baseline. |
| [`rendering-m0-float-textures.md`](history/rendering-m0-float-textures.md) | E1/E1b: native float transport; hardware filtering fails 2e-5, explicit arithmetic passes. |
| [`rendering-a0k-shader-safety.md`](history/rendering-a0k-shader-safety.md) | Missing-data handling; zero-alpha gate; the red-flood failure and its repair. |
| [`rendering-a0j-early-display.md`](history/rendering-a0j-early-display.md) | Early weather before chart readiness; camera/clock preservation. |
| [`rendering-a0h-resident-worker.md`](history/rendering-a0h-resident-worker.md) | Single-owner retained worker; exact deferred fidelity. |
| [`rendering-architecture-evaluation.md`](history/rendering-architecture-evaluation.md) | A0a–A0k results **including the negative ones**; the "do not adopt on one pair" discipline. |
| [`profiles/rendering/2026-10-18/a0c-timeline-scrub-summary.json`](profiles/rendering/2026-10-18/a0c-timeline-scrub-summary.json) | Scrub cost: 41 uploads / 568 MB; dispatch p95 357.7 ms. The cost D1/D3/D4 remove. |
| [`profiles/rendering/2026-10-18/a0h-resident-worker-summary.json`](profiles/rendering/2026-10-18/a0h-resident-worker-summary.json) | Resident preparation 1.18–1.31 s vs 1.79–1.96 s; GUI bind 2.2–5.9 ms. |
| [`rendering-m0-measurement-audit.md`](rendering-m0-measurement-audit.md) | What the instrumentation can and cannot prove; why readback is not presentation. |
| [`profiling-priorities.md`](profiling-priorities.md) | Already-measured pan cost (0.841 s inclusive in `live_geometry_for_view`). Do not re-measure. |
| [`../research/gpu-weather-texture/`](../research/gpu-weather-texture/) | **Prior art for D4**, not evidence and not shipped code. Its idea — pre-compute every forecast hour's U and V bands as GPU textures once, so that moving the forecast-time slider becomes a texture lookup rather than a re-stage — is the first attempt at the resident time window that D4 now specifies. It was never merged and nothing imports it. Kept because it records a rejected approach and the reason it was rejected. |

---

## 8. Open questions

D7 application integration was delivered by PR #91; the status of §10 Weather-panel selection needs verification. The fidelity/acceptance gates remain open. Earlier architecture
questions are settled: Q1 (D2) was decided on 2026-10-19 in favour of option (a). Q2 (ADR naming convention) resolved: ADRs live flat in `docs/` as `adr-NNN-slug.md`. Q3 (whether
the R-sequence's "Immediate implementation order" was still authoritative) resolved: R0.1 and R0.2 are discharged, and the live sequence is now the issue program. Q4 (whether issue
#69's Part 2 needs its own issue) resolved: it does not; Part 2 stays inside
#69 and begins only after Part 1 is measured.

---

## 9. Supersedes / standing constraints

This ADR does not supersede the canonical performance roadmap ([`rendering-performance-plan.md`](rendering-performance-plan.md)) or the implementation specification. It amends them
as recorded in §6.

Standing constraints carried by every implementing issue:

1. Thresholds are never relaxed: the 2e-5 numerical gate, the A0k zero-alpha
   missing-data gate, and the existing barbs selection/anchor/count/atlas gates.
2. Correctness, lifecycle, and performance are separate gates.
3. Negative results are preserved with their reasons.
4. Render callbacks are proxies, not presentation. Readback is fidelity, not timing.
5. Every change is feature-flagged, default OFF, with the default path byte-identical.

## 10. Display forecast coherence — accepted policy; shared path opt-in

The 2026-10-20 user review of epic #51 requires one explicitly chosen file and coherent model run/member for display. Within that identity, spatial/time interpolation is allowed;
implicit cross-file components, endpoints, older-run gap filling and spatial blending across forecast ownership are not. A multi-run/member file must respect the chosen identity;
ensemble statistics are explicit products, not fallback.

Issue #101 adds an opt-in Models panel and explicit `SessionController` analysis paths. One shared region is captured from the current map view; later panning does not change it.
`ForecastAnalysisState` binds display and new routes to one active tranche/entry; running routes retain their inputs. Saved refresh intent is separate from active data. Missing or
unsupported data fails visibly, without legacy-file fallback. Explicitly choosing Models persists that profile mode; otherwise legacy selection remains the default. No legacy data
is adopted or deleted; cleanup still awaits validation.

This does not claim default cutover, real-viewport acceptance, performance acceptance or complete feature parity; #70's measured candidate is unchanged. See [forecast display
coherence](rendering-performance-plan.md#forecast-display-coherence) for the source audit, remaining acceptance gates and resampling question; the #51 checkpoint is in the
[archived decision log](history/rendering-decision-log.md).
