# A0j: early weather display — mechanism passed, visual acceptance blocked

> **Status: HISTORICAL.** The A0j early-display report, written 2026-10-19; the
> visual blocker it records was repaired by [A0k](rendering-a0k-shader-safety.md).
> Superseded by [ADR-001](adr-001-grib-rendering-architecture.md) and the issue
> program. Evidence, not a plan.

**Follow-up:** [A0k](rendering-a0k-shader-safety.md) fixes the missing-data overlay
in an opt-in experimental shader and revalidates early/late display against an
independent CPU oracle. The artifact and rejected visual gate below are historical,
not the corrected renderer's current status. Main-app integration remains open.

## Outcome

`tools/early_weather_display.py` now starts an independent Qt viewport alongside
the headless numerical service and chart host. It displays NEW weather output
before charts are ready, with explicit loading status. Charts are subsequently
composed into that same viewport without replacing its camera or weather field.
This remains an experiment, **not a change to the main application's startup**.

One diagnostic rejected camera round-trip drift; the second run passes mechanism
and exact numerical/pixel gates. Both children exit 0 on the passing run. However,
inspection of preserved readbacks reveals red GPU flooding in the nonempty
reference, early wind view and final composition. Thus reference pixel equality
does NOT establish visual fidelity or usability. **Do not ship this portrayal.**

## Scope and implementation

- New field and land/selection arrays come from the real A0i owner-thread service,
  not cached reference outputs. The old reference supplies matched scalar
  viewport/style/clock/provenance only; its arrays are used for deferred checks.
- The Qt viewport has no legacy core/window. GDAL opens are explicitly blocked in
  its process during live display. All forecast/shoreline ownership stays in the
  numerical producer, and charts are prepared in their separate host.
- A single preparation thread decodes/checks immutable payloads and encodes field
  images, with 64 MiB per-payload limits. Chart images are decoded off GUI thread.
  This does not establish a global RSS/GPU memory plateau.
- Very-dense barbs, hide-over-land, source order and forecast clock are preserved.
  Empty selections have an explicit status; out-of-retained coverage hides old
  layers rather than pretending absent content is valid wind. New-area loading,
  zoom, scrub and full-app integration are not included.
- Covered horizontal drag and an automated four-step pan/return exercise the live
  selection path. The automated driver calls the viewport directly, not native
  event delivery. Absolute screen displacement avoids accumulating projection
  round-trip error; exact return follows zero displacement, not chart publication.

## Matched current workload

New approved fixture SHA-256:
`1ed1bca1281ad4096713ce20af9c1473e70bd4eb0766a54965849b7ace698a03`.
22 selected forecasts, frozen `2026-10-19T08:00:00`, 902×735 logical pixels,
Cocoa/Metal/DPR 2. No comparison with old-fixture timings is valid.

Private run directories: `rendering-a0j-early-display-{1,2}` and sibling JSON
reports in the approved OpenCode temporary directory. PNGs and private inputs
remain outside Git. Original inputs/manifests and PR #50 are untouched.

## Attempt ledger and observations

1. **Rejected:** wind appears while charts load, but repeated camera projection
   round trips drift ~1e-14 degrees. Numerical exit 0; chart startup cancelled on
   rejection, exit -15. Neither process remains running. Readbacks also expose
   inherited red flooding in the nonempty GPU wind output.
2. **Mechanism passed:** revised camera uses cumulative screen displacement from
   the original center. Four steps (+8,+8,-8,-8 px) run before chart readiness,
   change selected counts (1,048/1,055/1,048/1,053) and return the camera exactly.

Passing-run milestones, relative to concurrent launch **after 2.498 s preflight**:

| Event | Seconds |
| --- | ---: |
| Fresh numerical field available | 2.392 |
| Wind bound in live Qt viewport | 2.429 |
| Early wind readback completes | 2.449 |
| Chart host ready | 28.538 |
| Charts bound into existing viewport | 32.037 |
| Final scene readback completes | 32.312 |

The early readback checks chart-ready markers before and after the grab. Disk
save/comparison finish at 2.590 s; do not call that the render endpoint. The chart
export takes 3.281 s, including explicit readbacks and 25 numerical oracle checks
(1.658 s selection-oracle time). It is not optimized production chart publication.

Deferred headless/reference arrays, masks, nodes and selected anchors match
exactly: 12,381 native nodes and 1,053 initially selected symbols. Weather-only
readbacks before/after chart preparation are byte-equal after the camera return.
Late chart publication preserves the exact field object and camera; the composed
scene equals the concurrent chart oracle pixels. Both child processes exit 0,
without cancellation or timeout; numerical open/task/close share one owner thread
with no Qt application or legacy window.

Heartbeat p95 is 17.12 ms and maximum 301.42 ms, **including** explicit live
readback/save/comparison overhead. These are GUI timer gaps, not presentation FPS,
and not clean interaction-performance acceptance. The red flooding means a
nonblank readback alone also cannot certify correct glyph portrayal.

## Gate decision

Final targeted validation: **37 tests and 38 subtests pass**, Black on two changed
Python files, whitespace/JSON checks, 39 local links and raw-evidence consistency
checks pass. A read-only image check confirms red-dominant pixels occupy more
than half of both the early view and original nonempty reference. No full suite
or third native attempt was run. Reporting-only visual-acceptance annotations
were added to the harness after attempt 2; no rendering behavior changed.

**Stop/pivot at visual fidelity.** Provisionally retain independent publication as
a demonstrated mechanism. Reject visual acceptance and production adoption.
The red artifact is present in the original GPU oracle too; it is not acceptable
to hide it by claiming exact reference pixels. Diagnosis/repair needs a separate
bounded scope and an independent CPU or transparent-background portrayal oracle.
Root cause has not been established; no proprietary code, weather approximation,
source-selection or routing change is involved.

Two of three native attempts used; unused retry is not consumed. Production
startup, complete layers, broader interaction, memory plateau and presentation
goals remain open. No commits, pushes, PR changes or merges performed.

See the [canonical decision ledger](rendering-performance-plan.md) and
[machine-readable evidence](profiles/rendering/2026-10-19/a0j-early-display-summary.json).
