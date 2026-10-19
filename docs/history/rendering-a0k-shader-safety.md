# A0k: missing wind shader safety — focused repair passed

> **Status: HISTORICAL.** The A0k missing-data shader-safety report, written
> 2026-10-19. Its zero-alpha gate is the fixed reference for missing-data
> portrayal; the work itself is superseded by
> [ADR-001](../adr-001-grib-rendering-architecture.md) and the issue program.

## Outcome

Fixed the red overlay in the **experimental renderer**, without changing wind
values, source ordering, selection, routing, production shaders or PR #50.
`--safe-barbs` enables the fix in `tools/early_weather_display.py`. Old behavior
is still the default for historical experiment reproduction; no production
rollout or main-application startup integration is implied.

The original Metal shader path paints invalid/unselected native cells despite
fragment `isnan`/`isinf` rejection. Its missing-cell branch sends NaN wind through
vertex varyings. Isolated all-missing and mixed controls reproduce the flood;
changing to finite outputs plus explicit validity eliminates it. This supports
the missing-data-path diagnosis, **not** a proven compiler/driver root cause.

`tools/barb_shader_safety.py` applies a focused opt-in transformation:

- check endpoint/anchor finiteness using integer IEEE exponent bits;
- require only the relevant endpoint at blend 0/1, and both for interior blends;
- use finite wind/anchor outputs for invalid cells and a validity varying/discard;
- retain finite interpolation, glyph math, palette and selection unchanged.

No missing value is converted into a visible calm-wind symbol. Invalid outputs
are finite solely to make the GPU transport safe; explicit validity discards them.

## Independent oracle and gates

`tools/diagnose_barb_overlay.py` renders transparent synthetic and real-data layers
with original and corrected shaders. CPU wind-barb geometry/painting comes from
the existing Cairo `WindPainter`, not the GPU shader or its readback. Numeric
membership and endpoint fields are separately checked against the reference.

Declared raster tolerance: significant alpha >25/255 in each image must have
reciprocal support within **3 physical pixels (1.5 logical at DPR 2)**. Opaque
GPU color (alpha >127) must match nearby CPU ink (alpha >64) within that radius
and **8/255 per RGB channel**. Missing-only controls require **exactly zero alpha
pixels**, not a low fraction of errors. These are bounded rasterization allowances,
not weather approximations or a claim of exact Cairo/GPU pixels.

| Case | Original GPU significant ink | Corrected GPU significant ink | CPU significant ink |
| --- | ---: | ---: | ---: |
| Finite four-glyph control | 563 | 563 | 557 |
| All unselected | 65,536 | 0 | 0 |
| Mixed unselected | 33,024 | 256 | 252 |
| All NaN endpoints | 65,536 | 0 | 0 |
| Valid first / missing second, blend 0 | 563 | 563 | 557 |
| Missing second, interior blend | 65,536 | 0 | 0 |
| Current real-data field | 2,651,880 | 152,000 | 153,935 |

All corrected cases have zero out-of-tolerance support pixels in either direction
and zero palette mismatches. Finite-control original/corrected readbacks are
byte-identical. Missing-only cases have zero alpha even below the significance
threshold. Original real-data output has **2,126,929** pixels outside CPU support;
corrected real-data output has **zero**. The real field has 12,381 nodes and 1,053
selected symbols; it is not an empty-content performance win.

## Fresh early-display validation

The full corrected run uses NEW output from the headless service, not the cached
diagnostic field. It preserves the verified current fixture
`1ed1bca1281ad4096713ce20af9c1473e70bd4eb0766a54965849b7ace698a03`,
22 ordered forecasts, October 19 08:00 clock, very-dense/hide-over-land portrayal,
902×735 logical pixels and Cocoa/Metal/DPR 2.

Observed once, after **3.530 s preflight**:

| Event | Seconds |
| --- | ---: |
| Fresh field available | 2.668 |
| Early wind readback ends | 2.730 |
| Chart host ready | 28.387 |
| Charts bind | 31.882 |
| Final composition readback ends | 32.154 |

Four covered pan inputs run while charts load and return the camera exactly.
Early weather-only pixels match the late pre-chart-bind readback. Chart bind
preserves the field object/camera. Independent CPU support checks on early and
final images have zero violations in either direction. The old bad GPU final
oracle is deliberately **not** a pixel-equality requirement in safe mode; it is
preserved unchanged. Chart-only readback separates chart pixels from wind ink.

Headless fields/NaN masks/native nodes/selected anchors match the original numeric
reference exactly. Numerical resources open/use/close on one owner thread, without
a Qt application/window. Both producer processes exit 0 without timeout or
cancellation. Diagnostic readbacks/save/CPU checks add substantial overhead; GUI
heartbeat maximum is 198.25 ms including those operations. This is not presentation
FPS, clean interaction acceptance or an optimized chart-publication measurement.

## Attempts, evidence and decision

Final targeted validation: **43 tests and 38 subtests pass**; Black on five changed
Python files, whitespace/JSON checks, 44 local links and raw-evidence cross-checks
pass. Seven corrected transparent cases independently pass both support and
palette checks; the fresh-display gate and both clean child exits are rechecked
from preserved reports. No full suite or additional native workload was run.

- Native diagnostics 1/2 stop after their finite control because the next window's
  cached ShaderEffect `Compiled` status is not re-emitted. The revised diagnostic
  waits for image readiness, forces native readback and checks positive CPU/ink
  controls rather than trusting that status flag.
- Diagnostic 3 completes all 14 original/corrected renders and persists an empty
  failure list. The runner's shell wrapper then errors on zsh's read-only `status`
  variable and incorrectly reports that the command did not run. A requested
  relaunch refuses the already-existing directory before any workload. The
  diagnostic's **own exit code was not preserved**: rely on preserved results
  and independent read-only image checks, not a claimed clean process exit.
- Corrected full-display attempt 1 exits 0, as do both children, with all current
  numerical/publication/CPU-support gates passing. No fourth diagnostic or second
  full-display run was performed. Three diagnostic workload attempts and one of
  two full-display slots used; further attempts are not started by default.

Private raw evidence: `rendering-a0k-shader-{1,2,3}` and
`rendering-a0k-early-safe-1{,.json}` in the approved OpenCode temporary directory.
PNGs and private forecast/chart inputs remain outside Git. See
[machine-readable summary](../profiles/rendering/2026-10-19/a0k-shader-safety-summary.json).

**Decision: provisionally adopt the focused experimental shader repair and stop
this slice.** The measured red-overlay blocker is resolved within the declared
CPU geometry/color gates, not broad visual/performance acceptance. Next bottleneck
is still real application/chart-startup integration, not numerical transport.
No architecture rollout, commits, pushes, PR edits or merges were performed.
