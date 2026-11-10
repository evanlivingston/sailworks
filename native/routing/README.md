# Native routing bridge

Pinned C++20 + ZLIB [pybind11](https://pybind11.readthedocs.io/) bridge over
the vendored `weather-routing-engine` (Apache-2.0) and
`original-routing-engine` (GPL-3.0-or-later) sources. It implements the
deterministic synthetic fixture calculation, independent replay validation,
structured statuses, cancellation and explicit capability gaps. It is not the
production routing backend and does not select itself as a default.

## Build

```sh
cmake -S native/routing -B build/native-routing \
  -DCMAKE_BUILD_TYPE=Release \
  -Dpybind11_DIR="$(python -m pybind11 --cmakedir)"
cmake --build build/native-routing --parallel
ctest --test-dir build/native-routing --output-on-failure
```

`pybind11` and Python development headers are required only for the Python
module. Without them the selftest still builds. The module is written to
`build/native-routing/python/sailworks_native_routing*.so` (or `.pyd` on
Windows). Tests find it through `SAILWORKS_NATIVE_ROUTING_BUILD`, defaulting
to `build/native-routing/python`.

## API

- `identity()` — pinned commit and versions read from the vendored
  `VERSION`, `weather-routing-engine.version` and CMake `project()`, tree
  hash, documented integration patch set, licences, capabilities, and
  explicit absent geometry/time/forecast data.
- `synthetic_observables()` — native samples of the frozen analytic fixture.
- `synthetic_route(engine="main"|"original"|"professional")` — deterministic
  synthetic route with engine validation and independent `RouteValidator`
  replay. `accepted` is true only for a complete status with both checks.
  `validation_pass_flags_equal` compares only the two validators' pass flags;
  the full validation dictionaries are exposed separately and may differ in
  failure reason or sample count. Each leg reports
  wind/current availability, per-leg safety margins, warnings, tack,
  estimated fuel and `wind_gust_knots` (`None` when the engine has no gust).
  `layers` carries the engine's bounded inspection geometry: each layer has a
  stable `index`/`id`, `time` as exact UTC epoch seconds, the engine `reverse`
  flag, `contours` as engine-ordered `{points, point_count, closed}` records
  and `traces` as `{endpoint, route, point_count, times}`. Points are
  `{latitude, longitude}`; the bridge never joins contours, closes an open
  contour or invents points. `times` is `None` when the producer emitted no
  predecessor instants and otherwise an epoch-seconds array aligned 1:1 with
  `route`; absent times mean unknown, not zero. `inspection_geometry` on the
  run records the producer and this bounded/sampled caveat. A run may report
  `layers: []` (for example the original/professional entry points); that
  absence is explicit, not an error.
- `synthetic_cancelled_route(engine=...)` — pre-cancelled request; never an
  accepted completion. Cancellation is limited to a request cancelled before
  it is submitted: there is no mid-route or live cancellation.
- `synthetic_invalid_destination()` — structured failure outside coverage.
- `synthetic_validation_failure()` — tampered leg replay for the failure path.
- `route_with_weather(wind, current, coverage, provider_identity=..., source="grib", engine="main")`
  — runs the same request and polar with weather sampled through Python
  callables, so established Sailworks sampling can drive the native solver.
  `wind(latitude, longitude, epoch_s)` returns
  `(from_degrees, speed_ms, available)` — exactly the sampling representation
  used by `WeatherSampler.wind_at`. `current(...)` returns
  `(east_ms, north_ms, available)` with components pointing toward water
  motion. The bridge converts to engine knots once at this boundary. The
  solver runs with the GIL held on the calling thread and no native
  concurrency; a Sailworks vessel-performance adapter is not wired yet, so the
  fixture polar and constraints are still used. Missing samples must be
  reported with `available=False` rather than as calm water. An optional
  `polar` argument replaces the fixture vessel model: rows are
  `{true_wind_speed_knots, points: [(twa_degrees, speed_kn), ...]}` and must be
  strictly increasing in both axes with finite non-negative speeds.
  `polar_rows_from_polar()` converts a Sailworks/weatherrouting polar (radians,
  m/s) without changing interpolation or efficiency.

The synthetic polar uses the frozen contract angles 30, 60, 90, 120, 150 and
180 degrees. There is no 0-degree row; the engine clamps lower true wind
angles to the 30-degree row, which the identity records explicitly.

Synthetic routes use `OpenWaterProvider`. There is no bundled shoreline,
GSHHG dataset, depth provider, chart-hazard provider, real GRIB acquisition or
host GUI orchestration. The Professional entry point accepts the Original
engine result as an incumbent; this slice does not claim full Professional or
cross-platform parity.
