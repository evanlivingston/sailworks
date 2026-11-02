# A0h: resident numerical worker and clean timing windows

> **Status: HISTORICAL.** The A0h resident-numerical-worker report, written
> 2026-10-18. Superseded by [ADR-001](../adr-001-grib-rendering-architecture.md)
> D1-D3 and issues #64-#66. Evidence, not a plan.

## Outcome

**Implemented and validated an experiment-only persistent numerical data worker,
deferred fidelity checks, and matched source-residency controls.** Two matched
pairs pass identical input, resource ownership, native freshness and deferred
fidelity gates. Pair 2 reverses candidate/control launch order.

Observed new-area preparation is **1.18–1.31 s with retained sources versus
1.79–1.96 s reopening sources**, in the same no-oracle/no-explicit-readback protocol.
This supports source residency in this workload, not whole-application speedup,
presentation latency, statistical significance or performance equivalence.

The canonical contract/decision is in [the performance plan](../rendering-performance-plan.md).
See [machine-readable evidence](../profiles/rendering/2026-10-18/a0h-resident-worker-summary.json).
All forecast data, raw images, timing samples, commands and logs remain outside Git.

## Implementation

- `tools/resident_weather_worker.py`: one bounded-queue worker creates, uses and
  closes numerical source resources on the same non-GUI thread. The retained
  policy opens once; the comparison policy opens/closes per task on that same
  worker. No GDAL/sampler handle crosses into the interactive GUI. Existing
  128 MiB decoded-band policy and source-selection algorithms are reused unchanged.
- `tools/experiment_vertical_renderer.py`: numerical export can use that worker
  without running the 25-position oracle or capturing a framebuffer. Prepared
  CPU images and field lookups are constructed off the interactive GUI thread.
  They are transferred for a short GUI publication; no numerical approximation,
  field downsampling, source pruning or routing change is introduced.
- `tools/persistent_coverage.py`: retains the existing chart preparation host,
  requests frames without explicit readback in measured work, and provides a
  separate post-window validation request. Initial chart-host setup/readbacks
  complete before timing begins.
- `tools/compare_resident_runs.py`: refuses different input states, host/fixture/
  clock/asset identity, timed oracle/readback contamination, missing render
  signals, incorrect owner/open/close counts, stale publications or incomplete
  deferred numerical/scene checks.

The legacy application **still supplies charts and the independent oracle in
the preparation process**. Its ~30–31 s initialization is not eliminated. This is
a real retained numerical-worker core, not a claim that the full chart renderer
or application startup has already been replaced.

## Measurement and fidelity separation

Each case uses the same saved view, frozen exact time, source order, viewport/DPR,
two destinations and fixed 32-input partial-pan trajectory per destination. The
native A→B→A test starts old A generation 3, coalesces B generation 4, and binds
only newer A generation 5. All four cases retain the same application/harness
identity; launch order is reopen→resident, then resident→reopen.

Inside measured input windows:

- no 25-position oracle invocation;
- no explicit framebuffer/widget grab in the candidate or request export path;
- an interactive-GUI guard rejects explicit framebuffer readback attempts;
- timed exports declare zero reference checks and a pending oracle;
- raw GUI tick/callback/bind and `afterRendering` signal times are retained.

Qt-internal rendering behavior is not proved free of every driver synchronization
or internal transfer. The guard concerns explicit experiment readbacks. Render
signals are offscreen scene endpoints, not presentation, GPU-completion or
input-to-photon measurements. Fixed input states match across policies; the
longer control naturally has more idle timer ticks while waiting for work. Do
not interpret whole-window callback percentile differences as input-latency wins.

Publications remain **provisional until validation succeeds**. After all measured
input loops stop, the producer performs four separate oracle exports, 25 checks
each. Timed resources are not rewritten. Original timed metadata/provenance,
coordinate/value dtypes, numerical arrays/NaN masks, image arrays and scene pixels
are checked against the later reference. The obsolete generation is also checked,
but reconstruction outside measurement is diagnostic, not a replay publication.

The A0g reference-empty safeguards remain: equal zero counts, no drawable finite
endpoint vectors, explicit host status and preserved original red empty-GPU image.
Only a proven-empty reference layer is hidden for the corrected scene oracle.
This is not unchanged-GPU pixel equivalence for that erroneous empty case.
Nonempty reference scene comparisons remain unchanged and exact.

Across four controller cases: **400 numerical comparisons**, exact post-window
scene checks, unchanged prepared assets, correct source/thread release and native
A→B→A checks pass. All controllers and producers exit 0.

## Results

| Case | New-area prepare/export, destinations 1 / 2 | GUI publication, destinations 1 / 2 | Source opens over full case |
| --- | ---: | ---: | ---: |
| Pair 1 reopen | 1.963 / 1.795 s | 6.03 / 2.96 ms | 8 |
| Pair 1 resident | 1.254 / 1.184 s | 3.58 / 3.24 ms | 1 |
| Pair 2 resident (run first) | 1.310 / 1.189 s | 5.90 / 2.20 ms | 1 |
| Pair 2 reopen | 1.942 / 1.828 s | 8.63 / 2.72 ms | 8 |

Eight opens in each reopening case are four measured preparation tasks plus four
post-window oracle tasks, not eight opens per load. The resident resource is
opened once before requests and closed once on its owning thread. Each case has
eight worker tasks; no sampler object is passed to the interactive GUI.
These counters measure source-resource-group construction, not the count of
individual GDAL open/read calls across all 33 files.

Resident heartbeat p95 is 17.58–18.34 ms and maximum gap 38.49–49.29 ms across the
four destination windows. The ~295 ms tail from A0g does not recur here, but this
changed protocol simultaneously removes correctness readbacks/oracle work from
measurement and moves CPU image/lookup construction off GUI. The precise old-tail
cause is not isolated, and short samples cannot exclude future stalls. **These
intervals are not FPS or presentation acceptance.**

Post-validation takes ~11.1 s in each resident case and ~13.4–13.6 s in reopening
cases, entirely outside measured interaction. It is retained as correctness work,
not quietly dropped to manufacture a faster result. Initial chart-host startup is
30.24–30.88 s for reopening and 31.10–31.24 s for residency (which includes opening
the retained resources up front). No production cold-start claim is made.

## Validation and decision

- 39 targeted tests / 34 subtests pass, including owner-thread lifecycle,
  per-task control equivalence, strict deferred dtype/pixel/provenance checks,
  timed-path oracle/readback exclusion, native-pair rejection cases and stale
  generation handling. Eight changed Python files pass Black.
- Four native controller attempts and four producer processes used; all exit 0.
  Both pairs pass strengthened final gates. Close this successful slice without
  spending unused diagnostic slots. No full-suite/CI pass is claimed; shared
  sampling and routing remain unchanged.
- No production edits, commits, pushes, PR changes or merges. PR #50 remains
  untouched/unmerged; its recorded empty-GPU-layer issue still needs separate
  focused review. Global RSS/GPU memory plateau is not established by the worker's
  decoded-band budget or queue bounds.

  > **Status update (2026-10-19):** PR #50 was merged to `dev` via `b9e2471` / PR #61
  > `1a90515`. The A0h record above was written when it was still unmerged; the
  > measurements are unchanged.

**Decision: support retained numerical-worker ownership and this clean measurement
protocol for further experiments; do not adopt the whole architecture yet.**
The remaining preparation cost still includes legacy chart/CPU weather work and
settling, and startup still boots that application. Next justified design work
is independent chart/numerical publication and removal of legacy-host startup
dependency, followed by matched scrub/layer/zoom, memory and presentation gates.
Do not infer that this fixed-source/fixed-time pan/loading test validates those
later workloads or automatically start later milestones.
