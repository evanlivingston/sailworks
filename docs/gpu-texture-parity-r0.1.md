# R0.1 — GPU texture parity implementation

> **Status: HISTORICAL.** The R0.1 texture-parity implementation record for the
> reproduced 2× Metal failures. Superseded by
> [ADR-001](adr-001-grib-rendering-architecture.md) and the issue program.
> Evidence, not a plan.

Status: the reproduced 2x Metal fractional-pan failures are fixed. This completes
the first implementation slice of the [rendering plan](rendering-implementation-plan.md),
not the production GPU integration or the full platform/DPR matrix.

## Cause and change

The prototype always selected linear texture filtering. The raster QPainter
reference does **not** interpolate a 1:1 image blit, even when
`SmoothPixmapTransform` is enabled and the image has a fractional translation.
On the controlled grid, the reference retained two full-intensity texels while
the GPU spread them over neighboring pixels. This was a sampling-policy
mismatch, not missing glyphs, altered weather values, or an incorrect projection.

`tools/gpu_rendering_scene.py` now detects **unit device-pixel scale on both
axes** and selects nearest texture sampling only in that case. Actual scaling,
including fractional zoom, continues to use linear filtering. It does not round
the camera, wind geometry, directions, or image rectangles, and does not impose
a new convention for negative half-pixel ties. Texture and vertex retention are
unchanged. No application renderer or numerical/data pipeline changed.

The experiment report records the sampling-policy version, and diagnostic image
capture now preserves failed noninitial camera positions instead of saving only
the starting position. Images remain in the private artifacts directory.

## Regression contract

* Mathematical classification tests cover device scales 1, 1.5, and 2 and reject
  genuine horizontal/vertical scaling. These are not claims of complete native
  fractional-DPR rendering validation.
* Raster reference tests use positive/negative quarter-, half-, and three-quarter-
  pixel translations on **both axes**, at 1x and 2x. They establish that unit-scale
  QPainter blits preserve texel values rather than softening them. They do not
  assert that every possible native GPU half-pixel tie has been validated.
* Native Metal tests now require **both glyph and composite gates for every
  checked position in every mode**. The former exception allowing composite
  failures was removed. No quality threshold was relaxed.

The wider native 1x/2x and fractional-DPR/platform matrix, unsupported backend
handling, integration hosting, resource accounting, and lifecycle remain R0 gates
before production rollout.

## Candidate evidence

Historical reports are unchanged. Candidate reports use the same fixture data
and camera trajectories, with the changed renderer/code hashes recorded:

* [Controlled candidate](profiles/rendering/2026-10-15/gpu-r0.1-controlled-2x.json):
  all three modes, three scenarios, eight steps, one repetition — nine replays,
  36 image comparisons. **All complete visual gates pass.**
* [Saved-startup GPU candidate](profiles/rendering/2026-10-15/gpu-r0.1-startup.json):
  three scenarios, 180 steps, three repetitions — nine replays, 36 image
  comparisons. **All complete visual gates pass.** Actual Metal/M1 Pro, native DPR 2.

The previously failing controlled pan positions changed as follows. The limits
remain mean RGBA error <=1/255 and p99 channel error <=8/255:

| Position | Historical mean / p99 error | Candidate mean / p99 error |
| --- | --- | --- |
| Frame 2 | 0.3549 / 12 (fail) | 0.0669 / 1 (pass) |
| Frame 4 | 0.5197 / 19 (fail) | 0.0889 / 2 (pass) |

Saved-startup candidate geometry hashes, selected sample counts, and primitive
counts were checked frame-for-frame against the historical **CPU reference**:
identical in all nine runs. No QImages were supplied during timed GPU replay.

The correctness change retains the presentation benefit:

| Scenario | CPU-side preparation median | Qt swap-interval p99 |
| --- | --- | --- |
| Stationary | 0.054 ms | 17.39 ms |
| Pan | 2.029 ms | 21.38 ms |
| Zoom | 2.003 ms | 17.96 ms |

These are pooled prototype measurements, not full-application FPS or GPU
execution times. Differences from the earlier timing run can be measurement
variation; this fix is not claimed as another performance improvement. Normal
startup sampling/loading, progressive updates, and real input are still excluded
from the retained-scene experiment.

Validation: seven GPU math/reference/native tests passed; 18 candidate replays
completed serially. Historical failed evidence is preserved separately.

Next implementation slice: R0.2, comparing the candidate hosts in the real
workspace before exposing a feature-flagged production presenter.
