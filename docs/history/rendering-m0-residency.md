# M0 / E3a: actual decoded-band residency

> **Status: HISTORICAL.** The E3a/E3b decoded-band residency record, written
> 2026-10-17/18. Superseded by
> [ADR-001](../adr-001-grib-rendering-architecture.md) D1 and issue #64. Evidence,
> not a plan.

2026-10-17; [plan experiment index](rendering-experiment-records.md#experiment-contracts-and-results-b1-e1-e3),
[epic #51](https://github.com/evanlivingston/gweatherrouting/issues/51).

## Finding

PR #50's potential-cache estimate and actual display demand are radically
different on this saved workload. This is evidence for on-demand residency, not
permission to raise/remove the production safety guard.

See the [sanitized mechanism summary](../profiles/rendering/2026-10-18/m0-mechanism-summary.json)
for a durable machine-readable record of the key measurements.

| Measurement | Reference | Global-budget candidate |
| --- | --- | --- |
| Selected sources, same saved order | 33 | 33 |
| Potential six-band-per-source estimate | 1,369,235,952 bytes | Same |
| Retention limit / safety stop | 512 MiB, fail instead of global eviction | 128 MiB, global LRU |
| Peak retained decoded float64 arrays | 62,080 bytes | 62,080 bytes |
| Decodes / cache hits | 12 / 168 | 12 / 168 |
| Global evictions | 0 | 0 |
| Process high-water RSS | 142,639,104 bytes | 142,524,416 bytes |
| End-of-run GDAL cache use | 105,024 bytes | 105,024 bytes |

**Nine query pairs passed exact value/missing-mask/provenance hash comparison.**
Each query had 1,089/1,089 finite U/V points. Queries cover a viewport-derived
33×33 lattice, a 0.1-degree longitude shift and return, at the saved instant,
+1 second and +15 minutes. No sources were removed or reprioritized.

This is **not** PR #50's native-node union, endpoint staging, barb atlas, or GPU
draw path. The region/instants need only a small part of the available source
set. The experiment did not exercise global eviction on the real workload;
unit tests cover adapter eviction, reload identity and rejection. Broader travel,
new forecast brackets, source overlaps and long-run plateau remain unproven.

## Mechanism and safety

`tools/experiment_weather_residency.py` uses the existing sampler unchanged in a
separate offline process with a verified private snapshot copy. Its experiment-
only mapping implements the per-source OrderedDict interface while maintaining
a global byte-accounted LRU. Existing per-source six-band behavior is retained.

It reserves retained-array space before decoding, rejecting oversized bands
before reads. Budget failure uses a distinct exception deliberately outside the
sampler's recoverable error classes: it must not turn resource rejection into
missing values or fallback to a different model.

Full-band decoding and mask/float64 temporary allocations remain unchanged.
The 128 MiB limit bounds retained arrays, not live caller references, GDAL caches,
decode temporaries, GPU resources, or total RSS. No claim of a total-memory bound,
windowed decode, speedup, or memory plateau is made.

## Reproduction and evidence identity

Run the reference and candidate serially with the project Python, an external
temporary directory, and the verified snapshot/view report from B1:

```sh
PYTHONPATH="$PWD" python tools/experiment_weather_residency.py \
  --snapshot <private-snapshot> --view-report <native-smoke-2.json> \
  --mode reference --output <reference.json>
PYTHONPATH="$PWD" python tools/experiment_weather_residency.py \
  --snapshot <private-snapshot> --view-report <native-smoke-2.json> \
  --mode bounded --output <bounded.json>
python tools/experiment_weather_residency.py \
  --compare <reference.json> <bounded.json> --output <comparison.json>
```

Each workload had a 110-second child timeout and 120-second runner limit. Both
exited 0; comparison exited 0 with `matched_queries: 9`. Do not pool the sequential
timings as a speedup experiment: initialization and OS/driver caching differ.

Base commit `1f14649` with local changes. Harness SHA-256:
`7c51ddbc15b59e22bee299bfd068f736fb8df2ed5362bb893fb090bdf5c276ce`.
Same snapshot content hash as B1 and frozen clock `2026-10-16T08:00:00` local;
710×735 logical-pixel viewport. Private raw files in the local OpenCode workspace:
`rendering-m0-residency-reference.json`, `rendering-m0-residency-bounded.json`, and
`rendering-m0-residency-comparison.json`. Keep private view/data details outside Git.

Validation iteration 1: **21 checks passed, nine subtests passed** across residency
and B1 recorder/adapter tests. Black passed on two new Python files; whitespace
check passed. No shared sampler/routing/application code changed, so no full suite
or production renderer validation is claimed.

## Review

**Continue narrowly to E3b**, the actual PR #50 native-node/endpoint workload.
Do not interpret a 62 KB lattice cache as proof that staging or GPU rendering
works. Keep E3 open and retain the production guard until bounded ownership and
real staging have their own evidence. E2's pure whole-scene pan shortcut remains
deferred because it can change screen thinning or expose clipped content.

## E3b follow-up — actual PR staging (2026-10-18)

A separate probe, `tools/experiment_staging_residency.py`, loaded only the PR's
staging/transport modules read-only from its existing worktree, identified by
hashes in the private report. It reused the current branch's sampler and global
cache adapter in a separate private/offline process. No PR file or application
guard was changed.

- **33 sources; 2,898 native candidates; 1,406 unique native nodes.**
- At the saved time, +1 second, and +15 minutes: **734 visible symbols**, each
  atlas **41,760 image bytes**, decoded-array retention peak **60,512 bytes**.
- All three full-native-node differentials matched masks and per-point
  provenance. Maximum U/V component error from the PR's float32 staging was
  **4.3733e-7 m/s**, below the experiment's predeclared 2e-5 m/s threshold.
- Eight decodes, 136 cache hits, zero global evictions. Process high-water RSS
  197,361,664 bytes. This remains a retained-array limit, not total RSS/GPU memory.
- Combined field staging, differential sampling and atlas encoding took about
  56–70 ms per query; that inclusive span is not GPU draw or frame latency.
- One of at most two workload processes used; exit 0. Targeted staging/residency
  checks: **17 passed, nine subtests passed**; Black passed on the two new files;
  whitespace passed. No full-suite pass is claimed.

Validation incident: a runner initially failed its own zsh logging wrapper before
pytest executed; a subsequent invocation accidentally included a standalone `.`
and selected the wider suite, timing out after 120 seconds with four unclassified
failures and two skips visible in partial output. That run is invalid as targeted
evidence and was not repeated. The corrected two-file invocation produced the
17-pass result above. Do not reinterpret the accidental run as a suite pass or
attribute its failures to these changed files without diagnosis.

Private result: `rendering-m0-staging-residency.json`; harness SHA-256
`d7f1f7272e79a57e3bd2f7d7b80bbe51a93258d8e121591d45d3a57ff530c306`.
The E3a helper hash is unchanged from the report above. Tested source-module hashes:

```text
qt/gpuweather: a01ecef6dffd153605f5d2f9a376cb6869ad1538d83cc6bbec7212dcf2b2dca4
rendering/weatherstaging: 4484e0df81b2a32ee98aa4f841762fffb175981b02d630ca3a4e091f129919a3
rendering/weatherbarbs: aa5df17789d94be7851b64f470eb002bf3a0b3cbde12581059fe9b68cec27e7a
```

**Decision:** bounded on-demand decoded-band ownership is justified for this
saved-view staging workload. Do not increase the preflight limit; propose an
explicit cache/ownership replacement with rejection behavior and expanded
coverage tests. The next M0 uncertainty is E1 native texture integration. No GPU
draw, broad spatial/time stress, field-fill equivalence, or production eligibility
claim follows from this staging-only result.
