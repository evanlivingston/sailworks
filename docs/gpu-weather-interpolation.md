# Numerical GPU weather interpolation: implementation status

## Independent GRIB viewport (review path)

From the repository, using the project virtualenv:

```sh
python -m gweatherrouting.qt.gribviewport
```

No paths means **all saved selected forecasts, in saved order**. Nothing is
downloaded or saved. To use another saved data directory or freeze the clock:

```sh
python -m gweatherrouting.qt.gribviewport --data-dir /path/to/app-data --time 2026-10-19T08:00:00
```

Explicit GRIB paths may also be supplied. Drag to pan, wheel to zoom at the
pointer, trackpad to pan. Use the UTC time editor or ±1-hour buttons to scrub.
The saved barb density and land visibility are respected. This standalone
wind-barb window has no charts, scalar fill, contours or routing overlays; it
does not replace the normal app yet. Missing selection files are errors, never
silently omitted. Loading hides stale weather rather than substituting CPU wind.

The viewport owns its camera, worker and GPU portrayal; it does not use the
application frame assembler. Covered movement retains the numerical field and
rebuilds only the current-view selection. CPU retained fields and provider images
each have 32 MiB limits; decoded-band retention is 128 MiB. These are not a unified
RSS/VRAM limit or hardware-upload measurements. Preparation/in-flight and Qt
texture lifetimes are not directly measured.

The `viewport-interaction` correctness gate runs sustained pan, anchored zoom,
time changes and pending close through the existing runner:

```sh
python tools/gate_runner.py --gate viewport-interaction --snapshot /path/to/snapshot --output /path/to/results
```

The recorded 22-source run settled all phases, but weather was loading on 49/120
pan inputs, all 24 zoom inputs and 16/18 time inputs. This is **not** smooth-weather
performance acceptance or a presentation-FPS result. The earlier native pixel
comparison failed and its investigation remains user-deferred, not passed.
See the [try-out evidence](profiles/rendering/2026-10-19/viewport-tryout-summary.json).

## Existing application path (unchanged)

The experimental map path now draws **numerically interpolated wind barbs**.
Enable it only with the existing GPU presenter:

```sh
GWR_GPU_WEATHER=1 python -m gweatherrouting.qt --offline --map-renderer gpu
```

It is not a fully GPU weather renderer: background wind fill and isobars still
use CPU rendering. Windlines retain the existing renderer. Routing calculations
are unchanged. There is no claim of instant scrubbing or measured power savings.

## Implemented

- `rendering/weatherstaging.py` stages two numerical endpoint fields on a fixed
  query grid using a worker-private `WeatherSampler`. Actual valid times,
  reference times and coverage boundaries delimit open interpolation intervals.
  Exact event timestamps get singleton snapshots. This prevents blending
  distinct model runs or interpolating across mask/source-selection changes.
- Staging reuses the sampler's split-file pairing, per-point missing-value
  fallback and native projection/vector rotation. No hourly/minute rounding.
  Cancellation produces no snapshot. Returned float32 arrays are read-only.
- `qt/gpuweather.py` transports each float32 bit pattern in two opaque RGB
  texels. There is no per-band normalization or lost scale/offset. Alpha is
  always opaque to avoid premultiplication corrupting numerical bytes.
- `qt/shaders/WeatherField.qml` uses Qt 6 `ShaderEffect` and a baked shader pack.
  Qt owns texture upload and render-thread destruction; Python never creates a
  `QSGTexture`. A locked image provider retains only the current endpoint pair,
  with a default 32 MiB CPU image budget (not a total RSS/VRAM budget).
- The shader decodes numerical bytes before spatial/time interpolation,
  rejects contributing missing corners, ignores an unused time endpoint at
  exact times, then computes vector speed. Updating the blend uniform leaves
  image URLs unchanged.
- `rendering/weatherbarbs.py` selects native forecast nodes using the existing
  longitude/latitude ordering, screen-cell thinning, count cap and post-thinning
  shoreline mask. It does not substitute samples on an approximate screen grid.
  True U/V values and exact screen anchors are packed into a bounded atlas.
- `qt/gpuweatherjobs.py` owns one persistent worker and one replaceable pending
  request per map. GDAL handles are opened, used and closed on that worker.
  Time-only changes do not cancel an active compatible staging pass. View/source
  changes cancel stale work; completed pairs are accepted only for the latest
  key/time. Source fingerprints include file identity, size and modification
  time, checked before opening and after staging. Shutdown cancels without
  blocking the GUI on a GDAL read.
- `WeatherBarbs.qml` uses a single bounded mesh, not a QML item per symbol or a
  full-screen neighborhood search per fragment. The vertex shader interpolates
  U/V; the fragment shader draws shafts, calm circles, pennants and feathers
  using the existing dimensions, knot thresholds and 2 m/s palette. Mesh gaps
  carry fractional markers and are discarded. Numerical image URLs remain
  unchanged during time interpolation.
- The map retains CPU symbols while staging/compilation is pending. Hidden wind,
  streamlines, unthinned density, unsupported providers, changed source/view keys,
  allocation limits and staging errors use the existing path. The renderer's
  explicit memory ledger includes atlas images, arrays and mesh buffers.
- When no time-dependent marine layer is visible, exact-time background jobs
  are requested through a Cairo recording surface. Cached chart/fill pixels are
  not rebaked merely because the numerical time uniform changed; completed
  background jobs still invalidate them. Time-sensitive route/navigation
  overlays continue updating. Active marine layers keep their original path.

Staging limits are 262,144 candidate native nodes, 8,192 atlas cells, a potential
128 MiB decoded-band cache and 32 MiB atlas images per map. Wind above 250 m/s
falls back rather than silently clipping the shader's bounded symbol geometry.
These are explicit resource envelopes, not measurements of total RSS or VRAM.

### Known real-forecast limitation

The manual application launch with the user's selected forecasts on October 17,
2026 repeatedly logged `Selected forecasts exceed GPU worker band budget`.
That session used CPU fallback for wind symbols; it did **not** demonstrate the
new numerical shader on those forecasts. The current guard conservatively sums
the potential six-band cache across all selected sources, rather than budgeting
only bands resident for the active interval. Repeated requests can retry and
repeat the warning. A bounded on-demand band-residency design and retry
suppression remain follow-up work. Do not raise/remove the memory guard merely
to label this workload GPU-accelerated.

## Validation

Targeted tests cover bit-exact float32 transport (including NaNs and signed
zero), exact-second weights over three-hour intervals, budgets/cancellation,
split files, missing values, new-cycle boundaries and rotated projected wind.

`test/test_gpu_weather_native.py` renders the actual shader on the native
graphics backend and reads pixels back. It checks signed U/V interpolation,
speed-after-vector-blending, spatial interpolation, missing-corner behavior,
resource recreation and no image-provider reload when only time changes.
Readback uses an RGBA8 diagnostic mode, so assertions allow framebuffer
quantization; the transport unit test separately checks all float32 bits.

`test/test_gpu_barbs_native.py` compares shader silhouettes and colors with the
existing Cairo geometry. Worker tests exercise coalescing, stale-work rejection
and shutdown. `test/test_gpu_weather_map.py` runs an isolated native map through
time changes, background-cache reuse, panning, style fallback, source removal
and worker shutdown. It verifies unchanged numerical image-provider requests
through a 120-frame time scrub.

The native test has passed on Metal with Qt 6.12. Other backends and the minimum
supported Qt 6.8 runtime have not been executed. The baked pack uses Qt 6.5's
serialization format for compatibility with the declared Qt 6.8 minimum.

```sh
python -m pytest -q test/test_gpu_weather.py test/test_weather_staging.py
env -u QT_QPA_PLATFORM GWR_NATIVE_GPU_TEST=1 python -m pytest -q -s test/test_gpu_weather_native.py
env -u QT_QPA_PLATFORM GWR_NATIVE_GPU_TEST=1 python -m pytest -q -s test/test_gpu_barbs_native.py test/test_gpu_weather_map.py
```

Rebuild the checked-in shader pack with PySide6's `qsb` executable:

```sh
qsb --qsbversion 65 --glsl '300 es,330,440' --hlsl 50 --msl 12 \
  -o gweatherrouting/qt/shaders/weather.frag.qsb \
  gweatherrouting/qt/shaders/weather.frag
qsb --qsbversion 65 --glsl '300 es,330,440' --hlsl 50 --msl 12 \
  -o gweatherrouting/qt/shaders/barbs.vert.qsb \
  gweatherrouting/qt/shaders/barbs.vert
qsb --qsbversion 65 --glsl '300 es,330,440' --hlsl 50 --msl 12 \
  -o gweatherrouting/qt/shaders/barbs.frag.qsb \
  gweatherrouting/qt/shaders/barbs.frag
```

## Measurements and remaining work

On this Mac/Metal with Qt 6.12, synthetic native map runs used 25,921 forecast
nodes, 90 selected barbs and 120 exact-second time changes, including framebuffer
readback after each change. With background-cache reuse enabled it measured
26.2 ms median / 51.9 ms p95 elapsed and 6.6 ms median GUI-thread CPU in one run;
the latest expanded validation run measured 35.3 / 60.9 ms and 14.7 ms GUI CPU.
Both had zero numerical image reloads. An earlier run without background reuse
measured 37.5 / 62.2 ms and 18.1 ms GUI CPU. These are individual sequential
development runs with substantial variation, **not an interleaved benchmark or
a CPU-renderer speedup measurement**. Readback
synchronization and remaining CPU backgrounds/navigation contribute to latency.

Still required: GPU background fill with native spatial/mask fidelity, GPU
windlines if feasible without degrading their portrayal, real-forecast visual
and performance comparisons, power measurements, and broader dock/context-loss
and backend testing. `WeatherField.qml` remains a diagnostic field component;
its extra bilinear interpolation between staged query points is not used for
map barbs and must not be presented as exact native-grid spatial sampling.

Float32 staging is a deliberate display-only precision change from the sampler's
float64 arithmetic. Tiny threshold/color differences are possible near exact
bucket or knot boundaries. Routing remains float64 and unchanged.
