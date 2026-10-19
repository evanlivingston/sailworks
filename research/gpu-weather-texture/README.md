# GPU weather texture interpolation (archived experiment)

This directory archives an experiment that was never merged and is **not**
part of the application. Nothing here is imported by `gweatherrouting/`, and
the application does not read it.

## What the experiment was

A second approach to GPU weather rendering, alongside the image-provider
approach in PR #50. The idea was to pre-compute every forecast hour's U and V
GRIB bands as GPU textures once at startup, so that moving the forecast-time
slider becomes a texture lookup plus a two-texture blend instead of a CPU
resampling pass.

- `gpuweather.py` uploads per-hour U/V band textures from GRIB and holds the
  mapping from forecast hour to texture, and it can return the two endpoint
  textures plus a fractional blend for a requested time.
- `gpuweather_shader.py` is a `QSGMaterialShader` that binds those four
  textures, interpolates U/V by fractional hour, computes wind speed and
  direction and maps the result to a colour ramp. The interpolation happens
  entirely on the GPU, so changing the time is a uniform change rather than a
  resample.

## How it relates to PR #50

The two approaches differ in how the numerical bytes reach the GPU:

- PR #50 transports each float32 in two opaque RGB texels through a
  `QQuickImageProvider`, and decodes the numerical bytes before any spatial
  or temporal interpolation.
- This experiment used per-hour `QSGTexture` objects directly, and left the
  interpolation to a custom shader.

PR #50 is the approach that was carried forward. This branch exists so the
alternative is not lost, and so the differences can be compared deliberately
if the image-provider path later needs a different transport.

## State of the experiment

Incomplete, and deliberately not finished:

- The texture-array path was abandoned. `_build_texture_arrays` reports that it
  is skipped; a real 3D texture array would need a `QSGTextureProvider`.
- The scene-graph integration (the material, the presenter and the map
  wiring) was explored on an older base and was not ported. Only the two
  self-contained modules are kept here.
- There is no measurement, no fidelity comparison against the CPU renderer,
  and no claim about scrubbing latency, power use or frame rate.

Nothing in this directory should be treated as a validated implementation. It
is a record of an approach that was explored.
