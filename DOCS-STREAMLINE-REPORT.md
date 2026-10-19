# Documentation streamline report

Documentation-only change. No code, test, tooling or `docs/profiles/` file was
touched. Nothing was committed, pushed, or merged; all changes are uncommitted in
the worktree.

- **23 files modified, 1 file created** (`docs/README.md`).
- **`docs/rendering-performance-plan.md`: 1,297 → 1,142 lines** (−155).
- **All 57 pre-existing `docs/*.md` files now have an inbound link.** The 13 that
  previously had none (listed in the task) each gained exactly one, from
  `docs/README.md`.

---

## 1. Files changed, and why

| File | What changed | Why |
| --- | --- | --- |
| `docs/README.md` | **Created.** Documentation index: a "Which document do I read for X?" table, a status-label legend, a per-topic catalogue of all 57 documents with `CANONICAL` / `REFERENCE` / `HISTORICAL` / `EXPERIMENT` labels, a machine-readable-evidence and research-code section, and a "Superseded documents" table. | Task 3. The 13 listed documents had no inbound link from anywhere and were effectively invisible. |
| `docs/rendering-performance-plan.md` | Task 1 live-status corrections; new "How to read this document" map; the 57-line A0 narrative blob in "Current checkpoint" replaced by a pointer; a new `## Experiment contracts and results (B1, E1-E3)` group; the 9 E/B1 contract bodies replaced by pointer stubs; `## B1 …` demoted from `##` to `###` and moved into that group; an intro added to `## Decision log`; a pointer added to `## Architecture recommendation after mechanism probes`. | Task 2. The file was doing three jobs; it now keeps the roadmap and the decision log, and points at the canonical record for everything else. |
| `docs/adr-001-grib-rendering-architecture.md` | Added a `## Current checkpoint` block (roadmap, issue program, R0–R5 detail, experiment record, status, index link). Corrected the stale §6 lead-in "To be applied when D2 is decided" → "Applied on 2026-10-19, when D2 was decided." | Task 2 (checkpoint pointer). ADR-001 is the "read this first" document and had no onward pointer. The §6 sentence contradicted the ADR's own status header and §4.2. |
| `docs/milestones.md` | Added a "Start here" line pointing at ADR-001, the issue table, and the documentation index. | Makes ADR-001 → issues #62–#70 → index navigable in both directions. |
| `docs/gpu-weather-residency.md` | **Live** status claim rewritten: "leave #50 opt-in and unmerged" → "leave #50 opt-in" plus the merge commit/date. Added a `HISTORICAL` status header. | Task 1 (live class) + Task 4. |
| `docs/early-weather-startup-integration.md` | Added a dated status note after the "unmerged #50 modules" bullet; added a `HISTORICAL` status header. | Task 1 (dated class) + Task 4. |
| `docs/rendering-architecture-evaluation.md` | Added dated status notes at the two places that say "#50 untouched and unmerged" (A0a contract, A0e follow-up decision). Replaced the existing bold "**Status:**" preamble with the standard `HISTORICAL` header. Body untouched. | Task 1 (dated class) + Task 4. |
| `docs/rendering-m0-app-persistent-field.md` | Added a dated status note after "#50 is untouched, opt-in and unmerged."; added a `HISTORICAL` header; link label "[canonical contract]" → "[plan entry]". | Task 1 (dated class) + Task 4. The label change removes a now-false claim that this plan holds the canonical contract text (see §2.4). |
| `docs/rendering-m0-persistent-field.md` | Added a dated status note after "#50 stays unchanged and unmerged."; added a `HISTORICAL` header; link label "[Canonical contract]" → "[Plan entry]". | Task 1 (dated class) + Task 4. |
| `docs/rendering-m0-retained-field.md` | Added a dated status note after "opt-in and unmerged"; added a `HISTORICAL` header; link label "[canonical plan and contracts]" → "[plan experiment index]". | Task 1 (dated class, **not** in the task's list of 9 — same class of stale claim) + Task 4. |
| `docs/rendering-a0h-resident-worker.md` | Added a dated status note after "PR #50 remains untouched/unmerged" (indented into its list item so the list is not split); added a `HISTORICAL` header. | Task 1 (dated class, **not** in the task's list of 9) + Task 4. |
| `docs/early-weather-startup.md` | Added a dated status note after "PR #50 and the retained numerical/GPU prototype remain separate and unmerged." | Task 1 (dated class, **not** in the task's list of 9). |
| `docs/rendering-m0-float-textures.md` | Added a `HISTORICAL` header; link label "[canonical plan and contracts]" → "[plan experiment index]". | Task 4 + §2.4. |
| `docs/rendering-m0-residency.md` | Added a `HISTORICAL` header; link label "[canonical plan]" → "[plan experiment index]". | Task 4 + §2.4. |
| `docs/rendering-m0-native-probe.md` | Added a `HISTORICAL` header; link label "[canonical plan and experiment contract]" → "[plan experiment index]". | Task 4 + §2.4. |
| `docs/gpu-rendering-experiment.md` | Added a `HISTORICAL` header. | Task 4. |
| `docs/gpu-texture-parity-r0.1.md` | Added a `HISTORICAL` header. | Task 4. |
| `docs/rendering-a0g-progress.md` | Added a `HISTORICAL` header. | Task 4. |
| `docs/rendering-a0i-independent-numerical.md` | Added a `HISTORICAL` header. | Task 4. |
| `docs/rendering-a0j-early-display.md` | Added a `HISTORICAL` header. | Task 4. |
| `docs/rendering-a0k-shader-safety.md` | Added a `HISTORICAL` header. | Task 4. |
| `docs/rendering-baseline-2026-10-15.md` | Added a `HISTORICAL` header naming the startup baseline as the reference workload. | Task 4. |
| `docs/rendering-performance.md` | Added a `HISTORICAL` header. | Task 4. |
| `docs/routing-profile-2026-10-12.md` | Added a `HISTORICAL` header. | Task 4. |

**Not modified:** the root `README.md`; `gweatherrouting/`, `test/`, `tools/`;
anything under `docs/profiles/`; `research/gpu-weather-texture/`;
`AGENTS.md`; every non-markdown file in `docs/`; every `docs/` document not
listed above.

---

## 2. Task 1 — the stale "#50 is unmerged" status

### 2.1 Live status claims — rewritten to the current fact

All four now state the merge commit `b9e2471` and PR #61 `1a90515` (2026-10-19):

| Location | Old claim | New claim |
| --- | --- | --- |
| `docs/rendering-performance-plan.md:79` (now in the "A0 experiment record" bullet) | "Keep the completed #50 residency slice separate and unmerged." | The #50 slice is merged to `dev` (`b9e2471` / PR #61 `1a90515`); it stays opt-in and separate from the ADR-001 work program. |
| `docs/rendering-performance-plan.md:87` ("Starting code") | "PR #50 is a separate, unmerged experimental candidate, not this baseline." | "PR #50 **was** a separate experimental candidate, not this baseline. It has since been merged to `dev` (merge commit `b9e2471`, via PR #61 `1a90515`)." |
| `docs/rendering-performance-plan.md:1274` (in the dated `2026-10-18 — CI-review corrections delivered` entry) | "#50 remains opt-in, open and unmerged." | "#50 remains opt-in; it was subsequently merged to `dev` (merge commit `b9e2471`, via PR #61 `1a90515`)." |
| `docs/gpu-weather-residency.md:121` ("Review / remaining gates") | "retain this narrow residency fix for human review, leave #50 opt-in and unmerged." | "…leave #50 opt-in." plus a sentence recording the 2026-10-19 merge and stating that the opt-in default and every gate below are unchanged. |

### 2.2 Dated historical records — original sentence preserved, dated note added

No original sentence was rewritten. A blockquote of the form

> **Status update (2026-10-19):** PR #50 was merged to `dev` via `b9e2471` / PR #61
> `1a90515`. … The … below was recorded when it was still unmerged; the
> measurements are unchanged.

was added at each of these locations:

| Location | Note says the record was written when #50 was unmerged |
| --- | --- |
| `docs/rendering-m0-app-persistent-field.md:16` | "The experiment below was recorded when it was still unmerged; the measurements are unchanged." |
| `docs/rendering-m0-persistent-field.md:4` | same |
| `docs/rendering-architecture-evaluation.md:341` | "The A0e follow-up record below was written when it was still unmerged; the measurements are unchanged." |
| `docs/early-weather-startup-integration.md:22` | "The integration contract above was recorded when it was still unmerged; the measurements are unchanged." |
| `docs/rendering-performance-plan.md:1233` (end of the `2026-10-18 — Approved residency slice delivered` entry) | "The entry above was recorded when it was still unmerged; the measurements are unchanged." |

### 2.3 Four further stale claims of the same class, found and annotated

The task listed nine locations. A repository-wide grep for `unmerged` found four
more places making the same claim. They were left as written and given the same
dated status note, so that no stale "#50 is unmerged" claim survives anywhere:

- `docs/rendering-m0-retained-field.md:16` — "…opt-in and unmerged."
- `docs/early-weather-startup.md:15` — "PR #50 and the retained numerical/GPU prototype remain separate and unmerged."
- `docs/rendering-a0h-resident-worker.md:125` — "PR #50 remains untouched/unmerged…"
- `docs/rendering-architecture-evaluation.md:149` — A0a scope protection: "leave #50 untouched and unmerged".

(`docs/rendering-performance-plan.md:1218`, inside the dated `2026-10-18` entry, is
covered by the note already added at the end of that entry.
`docs/rendering-implementation-plan.md:333` and `AGENTS.md:59` refer to the
general "leave PRs unmerged until approved" workflow policy, not to PR #50, and
were left alone.)

### 2.4 Two more live-status corrections found while reading

Neither was in the task's list; both were the same class of stale live claim and
both directly affected "what the current status actually is":

1. **`docs/rendering-performance-plan.md`, "Current checkpoint"** said
   *"**Open decision:** D2 (GPU spatial interpolation vs. the R3 sampling
   invariant) needs a user decision before issue #65 may start."* ADR-001 §4.2
   records D2 as **decided on 2026-10-19 in favour of option (a)**, and the
   amendment is already applied to `rendering-implementation-plan.md` R3 item 4.
   Rewritten to: *"**D2 decided 2026-10-19:** GPU spatial interpolation, bounded
   by the existing 2e-5 gate. The R3 sampling invariant is amended, not relaxed —
   see ADR-001 §4.2 and §6. Issue #65 is unblocked; the CPU sampler remains the
   reference authority and the fallback path."* The 2e-5 gate is unchanged.
2. **`docs/adr-001-grib-rendering-architecture.md` §6** said "To be applied when D2
   is decided", contradicting the ADR's own status header and §4.2. Changed to
   "Applied on 2026-10-19, when D2 was decided."

### 2.5 What was *not* changed

No measurement, number, threshold or result was altered anywhere. The 2e-5
numerical gate, the A0k zero-alpha missing-data gate, the 512 MiB / 128 MiB
retention limits, the 16 MiB texture-payload envelope, and the barbs
selection/anchor/count/atlas gates all appear with their original values, and
every stub added to the plan restates them verbatim (2e-5, 0.002604166666666824,
2.384185791015625e-7, 512 MiB, 128 MiB, 16 MiB).

---

## 3. Task 2 — reconciling `docs/rendering-performance-plan.md` with ADR-001

The file remains the canonical roadmap. It was not renamed, moved or gutted: the
hypothesis ledger, reference workload and acceptance contract, milestones table,
evidence and comparison rules, working/review rules, architecture recommendation
and the **entire A0 decision log** are all still here.

### 3.1 What was added

1. **`## How to read this document`** (new, directly under the title) — a
   five-row table naming the single home for each of the file's jobs, plus the
   rule that "every decision, threshold, negative result and budget stays either
   in this file or behind a link to the document that records it".
2. **`## Experiment contracts and results (B1, E1-E3)`** (new H2) — groups the
   mechanism-probe entries and states that the linked per-experiment document is
   the canonical record.
3. **`## Decision log` intro** — states that this log is the canonical record of
   the A0 program, points at the narrative home, and says not to read any single
   entry as a current work item.
4. **`## Architecture recommendation after mechanism probes` preamble** — states
   that these five recommendations were the input to ADR-001, that **ADR-001
   wins where they differ**, and lists D1–D5.
5. **`## Current checkpoint` refresh** — the "Open decision: D2" bullet replaced
   (§2.4); the 57-line A0 narrative blob replaced by a pointer (see §3.2); the
   "#50 unmerged" bullets replaced (§2.1); the D2 decision, the issue-program
   pointer and the ADR-001 pointer retained and made current.

### 3.2 Sections trimmed, and where their content now lives

**Rule applied:** a body was replaced by a pointer *only* after confirming that
every decision, threshold, negative result and budget in it is recorded in the
linked document. Each stub keeps the roadmap question, the fixed thresholds and
the decision, and names the canonical record.

| # | Section trimmed | Body | Where the content now lives | Nothing lost because… |
| --- | --- | --- | --- | --- |
| 1 | "Current checkpoint" → the old **"Next decision: A0 …"** bullet (57 lines of A0a–A0l narrative) | A0a–A0l results: A0a inconclusive; A0b complete; A0c 12-step scrub, dispatch p95 2.0 vs 357.7 ms, 24/24 vs 12/24 revisions, 0 vs 41 texture uploads; A0d/A0e; A0g empty-layer oracle correction; A0h 1.18–1.31 s vs 1.79–1.96 s, GUI bind 2.2–5.9 ms, heartbeat 38–49 ms, ~31 s startup; A0i 4,036 missing / 4,006 restored / 30 sidecars, 22 sources, 300 comparisons, 2.52 s vs 28.87 s (3.43 s preflight); A0j 2.45 s vs 28.54 s; A0k 2.73 s vs 28.39 s (3.53 s preflight) | `rendering-architecture-evaluation.md` (A0a/A0b/A0c in full, plus the A0d–A0i checkpoints), the per-slice reports `rendering-a0g-progress.md`, `rendering-a0h-resident-worker.md`, `rendering-a0i-independent-numerical.md`, `rendering-a0j-early-display.md`, `rendering-a0k-shader-safety.md`, `early-weather-startup-integration.md`, and `docs/profiles/rendering/2026-10-18/a0{b,c,g,h}-*-summary.json`, `2026-10-19/a0{i,j,k}-*-summary.json` | The A0a/A0b/A0c diagnostic tables and every A0g–A0l number are reproduced verbatim in the evaluation doc and the per-slice reports; the A0i timings are in `rendering-a0i-independent-numerical.md` and `a0i-current-startup-summary.json`. The decision ("do not adopt the whole architecture yet") is kept in the replacement bullet. |
| 2 | `### E2 design review: avoid a misleading frozen-scene win` | *Kept in full* (the deferral decision and its source-review reasoning). Two pointer lines added naming the canonical records. | — | The decision is unique to the plan; only the corroborating records are linked. |
| 3 | `### E2a contract: retained field, current selection (not a frozen scene)` | Question, Mechanism, Reference, Preparation stall, Workload, Budget, Success, Stop, Outcome | `rendering-m0-retained-field.md` §E2a — "E2a — retain values, recompute the current portrayal" and "### E2a result" | The report contains the mechanism (immutable staged field + blocked sampler `_side`/`array`), the interleaved reference/candidate order, the exact-atlas/count/anchor/provenance success gate, the one-iteration/one-process budget, the 27/27 result and the decision. The stub keeps the question, the budget and the decision. |
| 4 | `### E2b contract: native retained-field control, not production migration` | Question, Scope, Workload, Gates, Budget, Decision, Outcome | `rendering-m0-retained-field.md` §E2b and the "### E2b result: mixed, not an application win" table | The report contains the two-process scope, the 24-input/16.667 ms workload, the identity and all-input-counting gates, the two-process budget, the mixed result and the decision. |
| 5 | `### E2c contract: persistent GPU field / exact selection indirection` | Hypothesis, Scope, Reference/fidelity, Ownership/measurement, Budget, Gate/stop, Outcome | `rendering-m0-persistent-field.md` | The report contains the 2e-5 m/s vector gate, the 16 MiB envelope, the generation-key/ownership rules, the one-iteration/one-process/one-native budget, the 9/9 + 3/3 results table and the next gate. The stub restates the 2e-5 gate and the 16 MiB envelope verbatim. |
| 6 | `### E2d contract: disposable full-app persistent-field / atomic-symbol control` | Hypothesis, Scope, Fidelity, Boundary, Measurement, Budget, Decision gate, Outcome | `rendering-m0-app-persistent-field.md` | The report contains the scope, the mixed-mode rejection rule, the measurement rules, the two-iteration/three-native-attempt budget, the failed lifecycle record and the stop/narrow decision. |
| 7 | `### E3a experiment contract: real-forecast decoded-band working set` | Question, Scope, Non-goals, Budget, Evidence, Fidelity gate, Success, Stop/review, Outcome | `rendering-m0-residency.md` | The report contains the 512 MiB / 128 MiB retention limits, the exact-value/mask/provenance fidelity gate, the budget, the measurement table and the "continue narrowly to E3b" decision. The stub restates both retention limits. |
| 8 | `### E3b contract: actual PR #50 staging, without changing the PR` | Question, Scope, Checks, Budget, Non-goals, Success, Stop/review, Outcome | `rendering-m0-residency.md` §"E3b follow-up" | The report contains the 128 MiB retention, the 2e-5 m/s differential threshold, the "never skip a source to get a pass" rule, the budget, the results and the decision. The stub restates 128 MiB and 2e-5. |
| 9 | `### E1 experiment contract: native float textures and sampling` | Question, Scope, Budget, Fidelity, Evidence, Decision gate, Outcome | `rendering-m0-float-textures.md` | The report contains the generated-field scope, the two-iteration/two-process budget, the "do not loosen the threshold" budget review, the binding limits and the decision. The stub restates 2e-5, 0.002604166666666824 and 2.384185791015625e-7 verbatim. |
| 10 | `### E1b contract: explicit interpolation, not a looser sampling gate` | Question, Change, Budget, Success, Stop, Outcome | `rendering-m0-float-textures.md` §"E1b result" and "## Experiment history and budgets" | The report contains the manual-sampling change, the retained hardware-linear mode, the unchanged 2e-5 threshold, the one-iteration/one-process budget, the 20-second QSB and 15-second readback bounds, the result and the decision. |
| 11 | `## B1 experiment contract and outcome` → demoted to `### B1 experiment contract and outcome` and moved into the new group | Question, Scope, Non-goals, Budget, Mechanism checks, Timing endpoint, Correctness, Success, Stop, Outcome | `rendering-m0-native-probe.md` | The report contains the native-render measurement mode, the render-signal endpoint semantics, the correctness checks, the two-iteration/two-native-attempt 120-second budget, the attempt results and the decision. |

**Net effect:** 11 bodies replaced by pointers; 10 `###`/`##` headings kept (so the
`#e2c-…` and `#e2d-…` anchors used by `rendering-m0-persistent-field.md` and
`rendering-m0-app-persistent-field.md` still resolve); the plan is 155 lines
shorter and now has one job per section.

### 3.3 What was deliberately **not** trimmed, and why

- **The A0 decision log body (A0l, A0k, A0j, A0i ×3, A0h, A0g, A0f, A0e ×2, A0d,
  and the four dated `2026-10-17`/`2026-10-18` entries) was kept in full.** The
  task says "Keep the decision log", and the evaluation document itself defers to
  the plan for these contracts — `rendering-architecture-evaluation.md` says
  *"The bounded restart contract and decision are in the canonical plan"* and
  *"see the canonical plan's A0e contract/outcome"*. The plan's A0 entries are
  therefore the canonical **contract** record (question, scope, gates, budget),
  not a duplicate of the evaluation doc's narrative. Trimming them would either
  lose the contracts or require rewriting them. I verified the decision log
  differs from `HEAD` only by: the new intro paragraph, the two PR #50 status
  corrections, and the dated status note.
- **The `## Architecture recommendation after mechanism probes` body was kept.**
  Its five numbered recommendations are the reasoning that produced ADR-001 D1–D5
  and are not reproduced anywhere else; ADR-001 now points back to it explicitly.
- **The `## Hypothesis ledger` and `## Milestones` tables were kept.** They are
  the plan's live status surface and are not duplicated elsewhere.

### 3.4 Reciprocal "canonical" links

The six `rendering-m0-*.md` records said "canonical plan and contracts" /
"[canonical contract]", pointing into the plan. After the trim the plan defers to
those records for the contract text, so the labels were changed to "plan
experiment index" / "plan entry" and the two anchor links still land on the plan
entry, which names the record as canonical. No body text, measurement or
threshold was touched. Files: `rendering-m0-float-textures.md`,
`rendering-m0-residency.md`, `rendering-m0-retained-field.md`,
`rendering-m0-native-probe.md`, `rendering-m0-persistent-field.md`,
`rendering-m0-app-persistent-field.md`.

### 3.5 ADR-001 ↔ issue program ↔ R0–R5 relationship

Unchanged. No fourth numbering scheme was introduced. The plan still states the
relationship, and ADR-001 §5 still fixes it:

| Scheme | Scope | Authority (ADR-001 §5) |
| --- | --- | --- |
| R0–R5 | Implementation-detail sequence | Canonical for implementation detail |
| M0–M4 | Program phase | Canonical for program phase and target ratification |
| #62–#70 | Executable unit | Canonical for what an agent works on |

Both the plan's "Current checkpoint" and the new ADR-001 "Current checkpoint"
point at the same two things: ADR-001 first, then issues #62–#70 via
`docs/milestones.md`.

---

## 4. Task 3 — the structure of `docs/README.md`

```
# Documentation index
   Prominent pointer: architecture = ADR-001, work program = issues #62-#70
   "Start here" (ADR-001 + milestones.md)

## Which document do I read for X?
   ~18 rows: start-up, architecture, roadmap, next issue, routing, routing safety,
   sampling, chart controls, chart quilting, tides/currents, marine, tool panels,
   panel docking, Qt frontend + parity, wind style, GPU renderer host

## Status labels
   CANONICAL / REFERENCE / HISTORICAL / EXPERIMENT, one line each

## Architecture, roadmap and program          (5 docs)
## Start-up and application lifecycle          (2 docs)
## Rendering evidence — historical experiment records   (19 docs)
## Rendering features and shipped slices      (14 docs)
## Routing                                    (2 docs)
## Weather data and sampling                  (1 doc)
## Charts and base data                       (2 docs)
## Tides, currents and marine data            (5 docs)
## Tool panels, docking and frontends         (5 docs)
## Machine-readable evidence and research code (4 rows: profiles/, research/, source/)

## Superseded documents
   Table: superseded document -> what supersedes it
   Closing note: nothing here has been deleted; they are the evidence base
```

Every document carries **one line on what it is** plus a **status label**:

- `CANONICAL` (5) — `adr-001-…`, `rendering-performance-plan.md`,
  `rendering-implementation-plan.md`, `milestones.md`, `current-routing.md`.
- `HISTORICAL` (19) — the A0a–A0k record and per-slice reports, the six
  `rendering-m0-*` mechanism-probe records, the two GPU experiment records, the
  PR #50 record, the early-weather integration checkpoint, the two dated
  baselines/profiles and the dated routing profile.
- `REFERENCE` (33) — feature and slice documentation, the analyses, the frontends,
  the charts, tides/currents/marine, tool panels and docking. (Three further
  `REFERENCE` rows cover `docs/profiles/`, `docs/profiles/2026-10-12/` and
  `docs/source/`.)
- `EXPERIMENT` (1) — `research/gpu-weather-texture/` (see §6).

5 + 19 + 33 + 1 = 58 rows; 57 of them are documents, the last is a directory.
Every one of the 57 documents in `docs/` appears in exactly one table.

`docs/profiles/` is covered by two rows rather than per-file rows, because
`docs/profiles/` is machine-readable evidence that must not be edited, moved or
re-indexed file-by-file.

---

## 5. Task 4 — status headers

A 4-line `HISTORICAL` blockquote was added directly under the title of each of
the 19 `HISTORICAL` documents, stating what the document is, when it was
written, and what supersedes it. No body of any of those documents was rewritten.
The one exception is `rendering-architecture-evaluation.md`, whose existing
`**Status:**` preamble was converted to the same blockquote form and
date-annotated; the A0a–A0k body below it is untouched.

`research/gpu-weather-texture/README.md` already carries an equivalent header
("archived experiment", "not part of the application", "Nothing in this directory
should be treated as a validated implementation"). Per the task's instruction not
to act on that directory, it was left byte-identical.

---

## 6. Task 5 — deletions

### 6.1 Deleted

**Nothing was deleted.** No document in `docs/` satisfies all three conditions
(a) fully superseded by a document that still contains its content, (b) nothing
links to it, (c) no unique measurement, decision, threshold or negative result.
Condition (b) now fails for every document in `docs/`: after `docs/README.md`
was created, all 57 documents have at least one inbound link. Condition (c) also
fails for every document — each contains at least one measurement, decision,
threshold or negative result that is recorded nowhere else.

### 6.2 Proposed but **not** deleted

Each is a genuine candidate, and each fails the three-part test. I propose rather
than act.

1. **`docs/rendering-baseline-2026-10-15.md`**
   - (a) ✗ Not fully superseded. The document is the *synthetic scaling* baseline
     and says so; `rendering-startup-baseline-2026-10-15.md` is the *startup*
     baseline. The plan's reference-workload section points at the startup one
     "not a current baseline", so the two are complementary, not redundant.
   - (b) ✗ Now linked from `docs/README.md`, `rendering-performance.md`,
     `rendering-performance-plan.md` and the implementation plan.
   - (c) ✗ Unique synthetic scaling protocol and its own measurements.
2. **`docs/rendering-performance.md`**
   - (a) ✗ Not fully superseded. The plan is the roadmap; this document holds the
     benchmark harness invocations, the snapshot-capture rules, the scenario
     defaults and the acceptance-gate values. Those are not in the plan.
   - (b) ✗ Linked from `docs/README.md` and `rendering-implementation-plan.md`.
   - (c) ✗ Unique protocol content (capture/verify/compare commands, 180-step ×
     3-repetition defaults, "keep snapshots outside the repository" privacy
     rules).
3. **`docs/gpu-rendering-experiment.md`**
   - (a) ✗ Not fully superseded. `gpu-texture-parity-r0.1.md` closes the parity
     failures but does not contain the original retained-scene measurements.
   - (b) ✗ Linked from `docs/README.md` and `rendering-performance.md`.
   - (c) ✗ Unique negative result (the reproduced 2× Metal transformed-texture
     composite failures) and its measurements.
4. **`docs/rendering-m0-measurement-audit.md`**
   - (a) ✗ Not fully superseded. The plan's "Evidence and comparison rules" is a
     summary; the audit's per-finding code-evidence table is not reproduced.
   - (b) ✗ Linked from `docs/README.md` and `adr-001-…` §7.
   - (c) ✗ Unique measurement-blocker table (readback ≠ presentation, event-loop
     delay ≠ presentation, coverage-gate incompleteness) with code citations.
5. **`research/gpu-weather-texture/`** — see §6.3.

If a maintainer later wants one of 1–4 removed, the correct action is to fold
its unique content into the superseding document first, not to delete it. The
repository's rules ("preserve rejected approaches and reasons", "historical
reports must not be overwritten") and the "do not delete the evidence base"
instruction in this task both point the same way.

### 6.3 Recommendation on `research/gpu-weather-texture/`

**Recommendation: keep it as prior art, and cross-reference it from ADR-001 —
explicitly labelled as prior art, not as evidence. Do not remove it.**

Reasoning:

- **It is the prior art for ADR-001 D4.** Its core idea is to pre-compute every
  forecast hour's U and V bands as GPU textures once, so that moving the
  forecast-time slider becomes a texture lookup plus a two-texture blend instead
  of a CPU resampling pass. D4 decides essentially the same thing from the other
  direction — a *resident time window* over multiple forecast instants, where
  "a time change within the window performs **zero** decodes and **zero**
  uploads" and the staged before/after pair generalises to any instant in the
  window. ADR-001's evidence index does not mention this experiment, so a reader
  asking "has this been tried?" finds nothing. That is a real gap.
- **The repository's rules require preserving it.** "Preserve rejected approaches
  and reasons" applies directly: this is a rejected alternative transport
  (direct `QSGTexture` + custom `QSGMaterialShader`) to PR #50's image-provider
  transport, and it is the only surviving record of that comparison.
- **It must not be cited as evidence.** The README itself says there is no
  measurement, no fidelity comparison and no claim about scrubbing latency, and
  that the texture-array path was abandoned and the scene-graph integration was
  never ported. A cross-reference must therefore say "prior art / an approach
  that was explored", never "a result".
- **Removing it would lose the only record of the alternative transport.** The
  two remaining modules (`gpuweather.py`, `gpuweather_shader.py`) are the only
  place that code exists; it is not a copy of anything in the application.

**Not acted on, per the task.** Concretely, the recommendation is that ADR-001
§7 (Evidence index) gain one row, worded along these lines, and that the
directory itself stay byte-identical:

> `research/gpu-weather-texture/README.md` | Prior art for D4 (resident time
> windows): pre-computing each forecast hour's U/V bands as GPU textures so a
> time change is a texture lookup. Archived experiment — unmeasured, not
> imported by the application, and **not** evidence.

`docs/README.md` already lists the directory as `EXPERIMENT` and states that it
is prior art for D4.

---

## 7. Genuine contradictions found

### 7.1 Resolved

| Contradiction | Resolution |
| --- | --- |
| The plan's "Current checkpoint" said D2 "needs a user decision", while ADR-001 §4.2 recorded D2 as decided on 2026-10-19 in favour of option (a) and the amendment was already applied to the implementation plan's R3 item 4. | Plan bullet rewritten (§2.4). |
| ADR-001 §6's lead-in "To be applied when D2 is decided" contradicted the ADR's own status header and §4.2. | Changed to "Applied on 2026-10-19, when D2 was decided." |
| The six `rendering-m0-*.md` records called the plan "canonical" for their contracts, while the plan's A0/evaluation split made the evaluation doc the canonical narrative. | Link labels relabelled; each record now names itself as the canonical record for its experiment (§3.4). |

### 7.2 Found, **not** resolved

1. **`docs/rendering-implementation-plan.md` labels itself "Historical
   specification"**, while `AGENTS.md` says it "holds the detailed R0–R5
   sequence" and ADR-001 §5 lists R0–R5 as "Canonical for implementation
   detail". Not resolved because the two statements are not strictly
   contradictory — the implementation plan defers *roadmap and decision gates* to
   the performance plan while keeping the R0–R5 *detail* — but the wording reads
   as a conflict. Re-labelling it "CANONICAL" in the index risks implying that
   R0–R5 is again a live task list, which ADR-001 §8 (Q3) explicitly resolves
   the other way: "the live sequence is now the issue program". Left as a
   `CANONICAL` entry whose one-liner says "canonical R0–R5 implementation detail;
   the roadmap is the performance plan". **A maintainer should confirm the
   intended label.**
2. **`docs/gpu-weather-interpolation.md` and the M0 residency/float records
   describe the same GPU weather path from different angles.** The feature
   document says the experimental map path "draws numerically interpolated wind
   barbs" and is enabled with `GWR_GPU_WEATHER=1 … --map-renderer gpu`, with no
   pointer to the residency work; `rendering-m0-residency.md` and
   `rendering-m0-float-textures.md` record that production integration and
   map integration are *not* proven. Both are true, but a reader who starts at
   the feature document can easily take the GPU weather path as production-adopted.
   Not resolved: the feature document's body is the user-facing description of
   an opt-in flag and rewriting it was outside this task's scope. **Suggested
   follow-up:** add one line to `gpu-weather-interpolation.md` pointing at
   `gpu-weather-residency.md` and ADR-001 D1.
3. **`docs/early-weather-startup.md` vs `docs/early-weather-startup-integration.md`.**
   The first describes `--early-weather` as integrated into the real
   `SessionController`/`PreviewWindow` on `dev`; the second describes the same
   feature as the M0/A0l integration checkpoint on a `dev`-based worktree with
   its own baseline ("Actual validation on this new `dev`-based worktree is
   separate from old-branch timings/array evidence"). These may describe
   different code states on different branches, and I could not tell which is
   current from the documents alone. Not resolved: deciding which branch the
   integrated code lives on is a code question, not a documentation question.
   **A maintainer should confirm and then reconcile the two documents.**
4. **Two documents named "baseline" for 2026-10-15** (`rendering-baseline-…` =
   synthetic scaling; `rendering-startup-baseline-…` = the reference workload).
   Not a content contradiction, but the names invite the wrong one. Mitigated
   rather than resolved: both are now disambiguated in `docs/README.md` and both
   carry status headers, but the filenames were not renamed (renaming would break
   existing links in the plan, the implementation plan and the evidence
   documents).

---

## 8. Validation performed

Documentation only, so no test suite was run (the task states there is nothing to
test). Checks performed:

- Every relative link in all 58 `docs/*.md` files resolves to an existing file —
  **0 broken**.
- Every in-document anchor (`file.md#anchor`) resolves to a heading in the
  target file, using GitHub's slug rules — **0 broken** (this includes the
  `#e2c-…` and `#e2d-…` anchors used by `rendering-m0-persistent-field.md` and
  `rendering-m0-app-persistent-field.md`, and the new
  `#experiment-contracts-and-results-b1-e1-e3` and `#current-checkpoint`
  anchors).
- The 13 previously invisible documents each gained exactly one inbound link;
  the repository-wide inbound-link count was recomputed before and after.
- `docs/rendering-performance-plan.md`'s `## Decision log` was diffed against
  `HEAD` to confirm the A0 entries are otherwise byte-identical.
- `git status` confirms only `docs/*.md` changed, plus the new
  `docs/README.md`. `docs/profiles/`, `gweatherrouting/`, `test/`, `tools/`,
  `research/`, the root `README.md` and `AGENTS.md` are untouched.
- No measurement, number, threshold or result was altered: verified by
  diffing every changed line that contains a threshold value.

## 9. Not done (and why)

- **No commit, push, PR or merge.** The task requires the changes to be left
  uncommitted in the worktree.
- **No code, test or tooling change.** `tools/gate_runner.py`, `tools/gates.py`
  and `tools/check_doc_links.py` were not created or modified.
- **`docs/profiles/` was not touched**, and no file under it was deleted, moved
  or renamed.
- **`AGENTS.md` was not modified.** It would be reasonable to add a line there
  pointing at `docs/README.md` as the documentation entry point, but `AGENTS.md`
  is the repository's working rules and a maintainer session is reviewing in
  parallel; changing it was not requested and would be the maintainer's call.
- **`research/gpu-weather-texture/` was not touched**, per the task.
- **The A0 decision log bodies were not trimmed**, per the task's "Keep the
  decision log" and for the reason given in §3.3.
