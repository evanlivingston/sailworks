# M0 / E1 and E1b: native float texture feasibility

> **Status: HISTORICAL.** The E1/E1b native-float-transport record, written
> 2026-10-18; its 2e-5 gate result is the evidence ADR-001 D1 relies on. The work
> is superseded by [ADR-001](adr-001-grib-rendering-architecture.md) D1 and issue
> #64. Evidence, not a plan.

2026-10-18; [plan experiment index](rendering-performance-plan.md#experiment-contracts-and-results-b1-e1-e3),
[epic #51](https://github.com/evanlivingston/gweatherrouting/issues/51).

## Decision

**Native float transport works through installed PySide/Qt RHI bindings.**
Explicit shader interpolation passed the unchanged accuracy gate; hardware
linear filtering did not. Keep both findings. A broad Metal/C++ rewrite is not
justified, and a production native-texture map integration is not proven.

The [sanitized mechanism summary](profiles/rendering/2026-10-18/m0-mechanism-summary.json)
retains key evidence without publishing private workload coordinates or paths.

## Experiment history and budgets

E1 allowed two implementation/validation iterations and two native processes:

1. The first process stopped before measurement: shader-baker discovery assumed
   `Qt/libexec/qsb`, but this installed PySide wheel exposes `PySide6/qsb`.
   No dependency was installed. Corrected discovery supports the known installed
   layouts and fails explicitly if none exists.
2. The second process created native Metal R32F/RGBA32F textures and preserved
   raw transport bits, but hardware linear sampling failed the predeclared
   **2e-5** maximum-error threshold: measured error **0.002604166666666824**.
   The process exited 1. This initial probe raised before writing a result file;
   the failure is retained in runner output, not misrepresented as a JSON result.

**Budget review:** stop hardware-filter trials; do not loosen the threshold or
claim a proven cause (filter weight precision versus shader compilation was not
isolated). Opened E1b as a different, narrowly specified method: four `texelFetch`
samples and explicit float32 interpolation, same oracle and threshold, one code
iteration and one native process. No extra hardware-filter run was made.

## E1b result

Native Cocoa/Metal, Qt 6.12.0, Python 3.11.17, generated data only; no forecast,
account, instrument, settings or application-state access.

- R32F upload/readback preserved all eight test bit patterns: positive/negative
  zero, positive/negative finite values, a NaN payload, infinities, and a fractional
  float32 value. This is raw texture transport, not filtering missing samples.
- A finite 3×2 RGBA32F field was sampled into a 9×6 RGBA32F render target.
  **Maximum component error: 2.384185791015625e-7**, against the original 2e-5 gate.
- Readback was vertically flipped relative to the CPU oracle; the report records
  this explicitly rather than silently imposing a coordinate convention.
- Field transport was **96 bytes**, versus **192 bytes** for the equivalent PR #50
  float byte encoding. This is arithmetic payload size, not a measured speedup,
  driver allocation, or end-to-end memory reduction.
- Three textures were created; two initial uploads totaled 128 bytes including
  the separate raw transport test; three widget render callbacks occurred.
  This does not establish context-loss handling or long-session resource stability.
- The native process exited 0. Six CPU oracle/path checks passed; Black passed on
  two Python files; whitespace check passed.

The generated-data raw result is `rendering-m0-float-textures-manual.json` in the
local OpenCode temporary workspace. It includes all sampled values and transport
bits. Harness SHA-256:
`39afdc1e519337999cc1b73faed72da4ce47e4cf58cea1ea7dacbe4a5cf15428`.

## Reproduction

Using the project's installed Python/Qt environment, with native GUI available:

```sh
env -u QT_QPA_PLATFORM PYTHONPATH="$PWD" \
  python tools/experiment_float_textures.py --sampling manual \
  --output <external-generated-data-result.json>
```

`--sampling linear` retains the hardware-filter experiment; it is not the adopted
method. The updated probe writes numerical failure evidence before returning
nonzero. QSB comes from the installed package, compiles only local generated
shaders, and is bounded to 20 seconds per invocation. Native readback has a
15-second event-loop deadline; the runner bounded each full process to 120 seconds.
Readback is part of correctness validation here, not an interaction benchmark.

## Binding and integration limits

Installed bindings expose QRhiWidget's initialize/render command-buffer callbacks,
float upload/readback, graphics pipelines and sampled texture bindings. They also
expose QQuickWindow.createTextureFromRhiTexture, but do not expose the C++ virtual
QSGMaterialShader.updateSampledImage hook. QQuickWidget integration via a custom
texture provider or thin native bridge needs its own proof. This standalone widget
is not a proposal to replace the application's host unconditionally.

The probe does not test production projection, source fallback at texture edges,
strict missing-corner interpolation, mask transport, palettes/blending, temporal
interpolation, visible symbols, both-map operation, non-Metal backends, minimum
Qt runtime, native scene invalidation, or real-forecast speedup. Those gates remain
open; hardware filtering must not be substituted for the authoritative sampling
policy merely because it is convenient.
