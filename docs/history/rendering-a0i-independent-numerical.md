# A0i: independent numerical startup — fresh-baseline mechanism passed

> **Status: HISTORICAL.** The A0i independent-numerical-startup report, written
> 2026-10-19. Superseded by
> [ADR-001](../adr-001-grib-rendering-architecture.md) and the issue program.
> Evidence, not a plan.

## Current outcome: user-authorized regeneration

Follow-up: [A0j early display](rendering-a0j-early-display.md) now passes the
live weather-before-charts publication mechanism, but reveals a nonempty red
overlay artifact inherited from the GPU reference. Visual acceptance and
production integration remain blocked; the headless numerical slice below is
still valid, not proof of usable GPU portrayal.

[A0k](rendering-a0k-shader-safety.md) subsequently fixes that missing-data artifact
in the experimental renderer and passes bounded CPU-portrayal checks. Production
integration and broader visual/performance acceptance remain open.

The user authorized using a fresh baseline instead of reproducing old cache
metadata. Captured current application data into a new private fixture; old
fixtures/manifests remain unchanged. New fixture identity:
`1ed1bca1281ad4096713ce20af9c1473e70bd4eb0766a54965849b7ace698a03`,
5,693 files, frozen clock `2026-10-19T08:00:00`, 22 ordered selected forecasts.
Saved portrayal is very-dense barbs, hide wind over land, 902×735 logical viewport.
This is **not** the old 33-source workload, and timings are not compared across it.

The experiment now prepares native-node land flags on the numerical owner using
the existing shoreline policy. Selection receives a detached immutable lookup,
not a live land/GDAL object. Unknown nodes or a different shoreline scale tier
are rejected rather than silently shown. Production/routing/#50 are untouched.

One fresh reference export passes **300 numerical comparisons**: 12 times × 25
pan positions, checking native membership, endpoint vectors/masks, selected nodes,
anchors and symbol counts against the existing sampler. One concurrently launched
headless/chart startup run then matches the **first** exported field/selection,
including all prepared land decisions, with 12,381 nodes and 1,053 selected symbols.
Headless startup does not yet exercise all 12 reference times.

Observed once, relative to startup after **3.43 s fixture/sandbox preflight**:

| Event | Seconds |
| --- | ---: |
| Numerical service ready | 1.73 |
| First matching weather field | 2.52 |
| Concurrent chart host ready | 28.87 |

Numerical open/task/close share a single owner thread; the service asserts no Qt
application and no legacy window. Both workers exit 0 without timeout. Raw data
remain private in `rendering-a0i-current-reference-1*` and
`rendering-a0i-current-headless-1*`; fixture is `rendering-current-fixture-2026-10-19`.
See [fresh-baseline summary](../profiles/rendering/2026-10-19/a0i-current-startup-summary.json).

Final targeted validation: **34 tests and 32 subtests pass**, Black on four changed
Python files, changed-file whitespace checks, summary JSON and raw-evidence
cross-checks pass. The latter confirm 300 exact reference comparisons and 12,381
matching land flags. No full-suite run was needed for experiment-only changes.

**Decision: mechanism passed; narrow next work to independent live display
publication under a new bounded scope.** The application does not yet display
these early weather fields while charts load. No full-map startup, presentation,
statistical speedup or architecture-adoption claim. One export/startup attempt
each used; stop this slice without consuming unused retries by default.

## Historical recovery checkpoint (superseded by authorized new fixture)

### Authorized partial restoration

Recovered **4,006 original files**, each checked against the original size/SHA-256,
from the live application-data directory. The manifest and 533 existing files
remain unchanged; 4,539 present files now match their original records. The
forecasts, chart tiles/ENCs and other recovered inputs are back. No live input was
modified, downloaded or hardlinked into the snapshot.

**30 marine cache metadata JSON sidecars remain unavailable** in the live data,
October 16 snapshot, older local snapshot and checked legacy data locations. No
replacement metadata was fabricated. Current partial inventory hash is
`14507922caa9769601a173b96f6772b18ee1449af2931055f5cecbbc13c9e64e`;
the original expected hash is unchanged. The restore explicitly exits incomplete,
and the full benchmark verification gate still rejects it. A0i workload validation
therefore remains blocked pending exact sidecar recovery or explicit approval for
a new, separately versioned baseline.

Utility validation: five tests/five subtests and Black on two Python files pass.
The private `fixture-restore-result-1.json` lists all restored and still-missing
paths. See [restoration summary](../profiles/rendering/2026-10-19/fixture-restoration-summary.json).
The failed preflight and missing counts below describe the earlier pre-restoration
state, not the current contents.

## Outcome

Implemented an experiment-only headless numerical-service draft, but **real-data
startup, field equivalence and chart-readiness independence are not validated**.
The first workload attempt rejected the original fixture before launching any
numerical or chart worker. Do not infer performance from this attempt.

The saved fixture expects content hash
`97cfda3c62a494c3d462ee8cde449d4d80ed1c62f387d70edcd9192dfb65d360`.
Current inventory hashes to
`20d8f92cf7d47b5d01a7339652d554c961035f8da5c4c98de82e273751ad4307`.
Read-only comparison finds 4,036 missing entries, zero changed and zero added:

| Directory | Missing entries |
| --- | ---: |
| chs-tiles | 2,711 |
| enc | 261 |
| grib | 34 |
| gshhs | 120 |
| marine | 900 |
| polars | 9 |
| seamarks.pbf | 1 |

The GRIB directory was empty at that preflight. Missing files are not source-selection or
sampler failures to fall back around. The cause of their absence is not established.
No original fixture was overwritten, repaired, resnapshotted or substituted.
Previously saved numerical/image output artifacts still exist, but cannot replace
the absent original forecasts in a new startup/decoding experiment.

## Draft implementation

`tools/independent_numerical_service.py`:

- verifies a fixture before launching workers and copies inputs into an isolated
  sandbox;
- reads only the saved managed-forecast list in its original order; rejects empty,
  escaped, duplicate or missing sources rather than silently pruning them;
- parses source metadata and opens/uses/closes the existing 128 MiB display
  resource group on the retained owner thread;
- stages immutable native-node fields/selection arrays without creating a Qt
  application, legacy window, chart host or routing manager in that service;
- is designed to launch a separate real chart host concurrently and record whether
  the first numerical result precedes chart readiness;
- checks field generations/provenance, dtypes, endpoint arrays, NaN masks, selected
  node IDs and anchors against previously validated A0h assets.

This implementation does **not** yet bind early numerical results into a live
map whose charts are still loading. Even if the startup experiment succeeds,
independent chart/numerical **display publication** remains a later integration
gate, not an automatic performance claim. Runtime assertions intended to enforce
no Qt application/window have not yet been exercised on real forecast data.

## Validation and next action

Focused tests cover source-order preservation, rejected unsafe lists, the existing
retained owner/protocol tests, and rejection before worker launch on changed
fixture identity: 42 tests and 39 subtests pass. Black on two changed Python files,
changed-file whitespace, JSON and 42 local links pass. No full suite,
native render result, startup timing or whole-map latency improvement is claimed.

**Decision: stop at the unavailable-artifact gate.** Restore the original fixture
byte-for-byte, or explicitly authorize a separately versioned replacement fixture
and new oracle capture. A replacement is not an identical comparison with A0h.
Once an approved verified fixture is available, run the headless/concurrent-chart
experiment before making any readiness or scene-integration claims.

See [the canonical contract](../rendering-performance-plan.md) and
[machine-readable checkpoint](../profiles/rendering/2026-10-19/a0i-independent-startup-summary.json).
Production, routing and PR #50 remain unchanged. No commits, pushes, PR changes or
merges were performed; no workload process was left running.
