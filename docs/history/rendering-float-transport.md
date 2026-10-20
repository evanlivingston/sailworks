# Native float texture transport through the Qt Quick scene graph (issue #71)

> **Status: HISTORICAL.** The #71 QSG float-transport spike, written 2026-10-19.
> Answer: a Python-only path works through an `ImageType.Texture` provider, but
> RGBA32F is 16 bytes per float, so D1's 4-byte payload still needs the R32F
> upload. Evidence, not a plan.

2026-10-19; [issue #71](https://github.com/evanlivingston/gweatherrouting/issues/71),
[gate `float-transport`](../../tools/gates.py).

## The answer

**Yes — the application's render path can carry a native float texture, from Python, with no
C++ bridge — but not through the hook the application uses today.** The `QQuickImageProvider`
in `ImageType.Image` mode silently loses a float image: the QML `Image` samples 0.0/1.0 where
the field should be. The working route is the same provider in **`ImageType.Texture`** mode,
returning a Python `QQuickTextureFactory` that hands the scene graph a `QSGTexture` built by
`QQuickWindow.createTextureFromImage()` from a `QImage` of `Format_RGBA32FPx4`. On that route
the float32 values arrive **bit-exactly** (−1000.0, 1000.0, −3.25, 12.75 all survive), the
application's own `texelFetch` bilinear arithmetic reproduces the CPU sampler **exactly
(0.0 error)**, and an all-missing field contributes **exactly zero alpha pixels** — the
unchanged 2e-5 gate and the A0k zero-alpha gate both hold.

The answer is therefore **(a) a Python-only path works**, with one caveat that step 2 must
settle: the reachable float format is **RGBA32F, 16 bytes per float**, which is *twice* the
current byte-packed transport's 8 bytes per float. The 4-bytes-per-float form that ADR-001 D1
justifies needs a single-channel R32F texture, and the probe could not fill one from Python.
The transport change is viable; **the memory saving that motivates D1 is not yet demonstrated**.

## What was run, and where the evidence lives

| Artifact | Identity |
| --- | --- |
| Probe | `tools/experiment_float_transport_qsg.py` (new, experiment-only) |
| Recorded result | `docs/profiles/rendering/2026-10-19/float-transport-summary.json` |
| Harness SHA-256 | `1b0ae7c3d0b0f3f0920b1b6447aafc90f586749434d72c645df11afb81732e48` |
| Protocol | `qsg-float-transport-e2x-v1` |
| Host | macOS 26.6.2 arm64, PySide6 6.12.0, Python 3.11.17, **Metal**, DPR 2.0 |
| Thresholds cited | `docs/history/rendering-m0-float-textures.md` (E1/E1b), `docs/profiles/rendering/2026-10-19/a0k-shader-safety-summary.json` (A0k) |

```sh
env -u QT_QPA_PLATFORM PYTHONHASHSEED=0 QT_SCALE_FACTOR=1 \
  python tools/experiment_float_transport_qsg.py \
  --output docs/profiles/rendering/2026-10-19/float-transport-summary.json

python tools/gate_runner.py --gate float-transport --output <dir>
```

### The code path exercised

The probe reproduces the application's own path, not a raw RHI one:

* host: `QQuickWidget` (the application's `gpuscene.MapScene` host) + `QQuickWindow`;
* scene: a probe QML file with the structure of
  `gweatherrouting/qt/shaders/WeatherField.qml` — QML `Image` elements feeding a
  `ShaderEffect` through `property variant` samplers `u0/v0/u1/v1`;
* provider: a `QQuickImageProvider` subclass, in `ImageType.Image` and in
  `ImageType.Texture` mode;
* shader: **the application's own `gweatherrouting/qt/shaders/weather.frag`**, compiled with
  only three changes, each asserted by the probe: the `decodeField` accessor (float variant
  or the application's byte-packed accessor, unchanged), the field size (halved for the
  packed layout, not for a float texture), and probe-only readback modes. The spatial
  interpolation block — `textureSize`, `clamp`, `floor`/`fract`, the 2×2 weight loop, the
  `isnan`/`isinf` rejection and the `fragColor = vec4(0)` missing-cell branch — is used
  **verbatim**; the probe raises if any of those lines changes.

The readback is a fidelity measurement, never a timing measurement: the probe writes the
IEEE-754 bits of the interpolated value into the 8-bit framebuffer in two byte-split renders
and reconstructs the exact float32 on the CPU. The probe first verifies its own instrument
(the QQuickWidget framebuffer transfer is exactly linear: 0→0, 32→32, 64→64, 128→128,
192→192, 255→255) before trusting it.

## The four test results

### 1. Does a float `QImage` uploaded through `createTextureFromImage()` arrive as a float texture?

**Yes — as `RGBA32F`, not `R32F`, and only if the provider hands over the finished
`QSGTexture`.**

* **`QImage.Format_R32F` does not exist in Qt 6.** The installed Qt 6.12 offers exactly these
  float image formats: `Format_RGBA32FPx4`, `Format_RGBA32FPx4_Premultiplied`,
  `Format_RGBX32FPx4`, `Format_RGBA16FPx4`, `Format_RGBA16FPx4_Premultiplied`,
  `Format_RGBX16FPx4`. The issue's premise names a format this platform cannot express.
* `QQuickWindow.createTextureFromImage(QImage(Format_RGBA32FPx4))` → a `QSGTexture` whose
  native format is **RGBA32F** (8×6 for an 8×6 field, 768 bytes).
* `QQuickWindow.createTextureFromRhiTexture(QRhiTexture(R32F))` → a `QSGTexture` whose native
  format is **R32F** (2×2 in the probe). The single-channel form *is* reachable as a texture.
* `QQuickTextureFactory.textureFactoryForImage(float_image).image().format()` is still
  `Format_RGBA32FPx4` — the factory preserves the float format.
* **But the ordinary `ImageType.Image` provider loses it.** The QML `Image` reports `Ready`
  and the provider is called four times, yet the shader samples **0.0 and 1.0** instead of
  −1000.0/1000.0: the float image does not survive the QML `Image`'s own texture creation.
  Measured whole-frame error 1000.0 (the field's own range).
* On the `ImageType.Texture` route the same image arrives intact: sampled texels
  `{0,0: -1000.0, 3,1: 1000.0, 5,4: -3.25, 7,5: 12.75}`, **bit-exact**
  (`sampled_cells_exact: true`).

### 2. What does `QSGTexture` report for its format and renderability?

`QSGTexture` in PySide6 6.12 **exposes neither `format()` nor `isRenderable()`**. The native
format is reachable only through `QSGTexture.rhiTexture()`, and the texture must have been
created by the render thread first (`rhiTexture()` is null until then). Observed, with the
texture actually used in a `QSGSimpleTextureNode` exactly as `gpuscene.ImageItem` does:

| Texture | `rhiTexture().format()` | flags | size | upload bytes |
| --- | --- | --- | --- | ---: |
| float `Format_RGBA32FPx4` | **RGBA32F** | `[]` | 8×6 | 768 |
| the application's byte-packed RGBA8888 | RGBA8 | `[]` | 16×6 | 384 |

Flags are empty in both cases: **no `RenderTarget`**, so the texture is *sampleable* but not
renderable. `QSGTexture.hasAlphaChannel()` is true for the float texture.
`QSGDynamicTexture` is abstract in PySide6 ("represents a C++ abstract class and cannot be
instantiated"), so a raw `QRhiTexture` cannot be wrapped that way; `createTextureFromRhiTexture`
is the exposed route.

### 3. Does the existing `weather.frag` `texelFetch` decode approach work unchanged against a float texture?

**The approach works; the byte decoder does not.**

* The application's byte-unpacking `decodeField`, used **unchanged** against a float texture,
  produces garbage: the decoded range is −2.3e24 … 3.6e25 against a field whose values span
  −1000…879, because it reads two texels per cell from a texture that holds one float per
  texel and reinterprets float bits as bytes.
* With the accessor changed to `texelFetch(field, cell, 0).r` and the field size no longer
  halved, **the rest of the shader is used verbatim** and reproduces the CPU sampler
  **exactly**: at probe-chosen sample positions the maximum absolute error is **0.0**
  (limit 2e-5), for the float transport *and* for the application's byte-packed control.
* Over the *whole frame*, against an oracle built on the modelled fragment centres, both
  transports measure **1.220703125e-4** — identically. That residual is the rasteriser's
  fragment-position quantisation, not the transport: it is the same number for the
  application’s existing path, so the float transport introduces no additional error. The
  probe therefore reports both numbers, and the gate uses the position-controlled one.

### 4. Do missing/masked values survive without becoming NaN in shader varyings?

**Yes, and the A0k zero-alpha gate holds on the float transport.**

| Case (float transport) | alpha pixels | significant alpha ( > 25/255 ) | max interpolation error |
| --- | ---: | ---: | ---: |
| all-missing (every cell NaN) | **0** of 3072 | **0** | n/a (no finite pixel) |
| partially missing (2 NaN cells) | 2560 of 3072 | 2560 | 1.2207e-4 (= the byte-packed control) |
| finite control | 3072 of 3072 | 3072 | 1.2207e-4 (= the byte-packed control) |

The byte-packed path gives the identical pixel counts (0 / 2560 / 3072), so the transport
change does not move the missing-data behaviour.

Two things are worth stating precisely:

* The **field** path interpolates in the fragment stage; no varying carries a field value, so
  the A0k "missing-data NaN crossing a vertex varying" failure mode does not apply to it. The
  equivalent risk — Metal optimising away the fragment `isnan`/`isinf` rejection, which is
  what produced the 65,536-painted-pixel red flood in the barb path — is exactly what the
  zero-alpha measurement rules out. The A0k failure signature (65,536 painted pixels for an
  all-missing layer) is absent: the layer paints **zero** pixels.
* The partially missing case paints 2560 of 3072 pixels: the 512 fragments whose 2×2
  neighbourhood contains a NaN are *exactly* transparent, not approximately.

## Recommendation: (a) a Python-only path works

The transport is implementable in Python, with no native bridge and no change to the
repository's "pure Python, no build step" shape. The minimum interface is:

1. `WeatherImageProvider` becomes an `ImageType.Texture` provider. Its `requestTexture`
   returns a Python `QQuickTextureFactory` subclass whose `createTexture(window)` returns a
   `QSGTexture`.
2. That `QSGTexture` is built once per published field with
   `QQuickWindow.createTextureFromImage(QImage(Format_RGBA32FPx4, …))` — one texel per cell,
   one float32 per texel, alpha opaque.
3. `encode_component` is replaced by a float image builder, and `weather.frag`'s
   `decodeField` is replaced by the float accessor. The interpolation arithmetic, the
   `texelFetch` sampling, the NaN/Inf rejection and the missing-cell branch are unchanged.

### The work this leaves for step 2

1. **Change the provider type and the decode accessor** in `gweatherrouting/qt/gpuweather.py`
   and `gweatherrouting/qt/shaders/weather.frag`; keep the arithmetic.
2. **Own the texture explicitly.** The probe *aborted the process* (SIGSEGV) when the
   provider's texture was replaced inside one process. The application republishes the
   field's source on every new forecast pair, so this is not optional: whoever creates the
   `QSGTexture` must own it, exactly as `gpuscene.ImageItem` already does for its
   `QSGSimpleTextureNode` texture (`setOwnsTexture(True)` plus explicit deletion of the
   previous texture).
3. **Build the texture and the factory on the GUI thread.** `requestTexture` runs on a Qt
   loader thread; creating GPU resources there aborts the process (observed).
4. **Convert the production shader too.** The running application uses
   `WeatherBarbs.qml` with `barbs.vert`/`barbs.frag`, which carry their own byte-unpacking
   decoder and which read the field in the *vertex* stage — the A0k NaN-through-a-varying
   concern applies there, not to the field shader. `WeatherField.qml`/`weather.frag` is the
   diagnostic field component (`docs/gpu-weather-interpolation.md`); both use the same
   provider → Image → ShaderEffect structure, so this spike's transport answer covers both.
5. **Decide the payload format — this is the open transport question.** RGBA32F is 16 bytes
   per float; the application's byte packing is 8 bytes per float (two RGBA8888 texels per
   float). The transport change alone therefore *doubles* the upload payload. The 4-bytes-
   per-float form needs a single-channel R32F texture: `createTextureFromRhiTexture` does
   produce an R32F `QSGTexture` (192 bytes for 48 floats, confirmed), but the probe could
   **not fill it** — the resource-update batch prepared with
   `QRhiResourceUpdateBatch.uploadTexture` is not submitted to the RHI from the GUI thread,
   and the shader read 0.0. Closing that is the remaining transport work, and it decides
   whether D1's memory reduction is realised or whether the transport is merely a fidelity
   change. It is the one place a thin C++ bridge might still be needed.
6. **Re-derive the memory ledger.** The D1 justification (halving the texture bytes) depends
   on (5); until then the ledger in `docs/gpu-embedded-rendering.md` must be recomputed for
   RGBA32F.
7. **Ratify the unratified gate check** (below) with a recorded byte-packed control error.

## The `float-transport` gate

Registered in `tools/gates.py`:

```python
"float-transport": {
    "title": "Native float texture transport in the Qt Quick scene graph",
    "milestone": "#71",
    "kind": "correctness",
    "tool": "tools/experiment_float_transport_qsg.py",
    "endpoint": ENDPOINT_FIDELITY,
    "timeout_s": 120,
    "env": {"QT_SCALE_FACTOR": "1"},
    ...
}
```

It needs no snapshot and no application state: it renders a generated field and reads the
framebuffer back. Ten checks, nine ratified, one unratified:

| Check | Threshold | Provenance |
| --- | --- | --- |
| maximum interpolation error at a probe-chosen position | `<= 2e-5` | `docs/history/rendering-m0-float-textures.md` — the unchanged 2e-5 gate; E1b measured 2.384185791015625e-7 with explicit `texelFetch` arithmetic, E1 hardware filtering measured 0.002604166666666824 and failed |
| the probe's own error limit is still 2e-5 | `== 2e-5` | the same unchanged program gate; it is not relaxed here |
| float32 transport preserved every sampled texel exactly | `is_true` | `docs/history/rendering-m0-float-textures.md` — E1: R32F upload/readback preserved all eight test bit patterns, including signed zero, a NaN payload and infinities |
| an all-missing field contributes exactly zero alpha pixels | `== 0` | `docs/profiles/rendering/2026-10-19/a0k-shader-safety-summary.json#missing_only_layers_exactly_transparent=true` and `docs/history/rendering-a0k-shader-safety.md` — missing-only controls require *exactly* zero alpha pixels, not a low fraction of errors |
| an all-missing field contributes no significant ink | `== 0` | the same artifact's `#significant_alpha_threshold=25` |
| the finite control field is painted everywhere | `== total_pixels` | the same artifact's `#finite_control_pixels_unchanged=true` |
| the float transport's end-to-end error is no worse than the byte-packed transport | `<=` the control measured in the same run | **UNRATIFIED** — the comparison is against a number measured in the same run, not against a recorded artifact; no recorded evidence holds the byte-packed transport's error through the QSG path. The placeholder bound is never enforced; step 2 must record it and ratify it |
| the float texture reached the scene graph in a float format | `== "RGBA32F"` | ADR-001 §4.1 (D1 transport format: native float textures) and the E1/E1b artifact's native R32F/RGBA32F textures |
| the probe ran every stage to completion | `== "completed"` | the probe's own contract |
| the gate does not claim presentation latency | `is_false` | ADR-001 §9 standing constraint 4 |

Observed results on this host:

```
PASS-WITH-OPEN-CHECKS  float-transport  #71  [correctness]  maximum interpolation error at a
probe-chosen position=0 <= 2e-05; the probe's own error limit is still 2e-5=2e-05 == 2e-05;
float32 transport preserved every sampled texel exactly=True; an all-missing field contributes
exactly zero alpha pixels=0 == 0; an all-missing field contributes no significant ink=0 == 0;
the finite control field is painted everywhere=3072 == 3072; the float transport's end-to-end
error is no worse than the application's own byte-packed transport=unratified; the float texture
reached the scene graph in a float format=RGBA32F == RGBA32F; the probe ran every stage to
completion=completed == completed; the gate does not claim presentation latency=False
(1 unratified)  24.1s
```

The three outcomes are distinct, as the registry requires:

* **PASS** — exit 0 (the run above);
* **FAIL** — exit 1, a ratified threshold violated. Proved by the runner's own self test:
  `python tools/gate_runner.py --gate float-transport --self-test` →
  *"violated 'maximum interpolation error at a probe-chosen position' -> runner exit 1"*;
* **NOT-EVALUATED** — exit 2, the gate could not run. Demonstrated by running the runner
  under an interpreter that cannot import PySide6: *"NOT-EVALUATED float-transport #71 …
  the interpreter … cannot import PySide6"*, exit 2.

## What surprised me

1. **`QImage.Format_R32F` does not exist in Qt 6.** The issue asks about "a `QImage` of
   `Format_R32F`"; that format was removed in Qt 6. The nearest float image is
   `Format_RGBA32FPx4` — four float channels per texel, i.e. **16 bytes per float**.
2. **The float image is lost on the path the application actually uses.** The texture
   factory preserves `Format_RGBA32FPx4`, and the provider is called, and the `Image` reports
   `Ready` — and the shader still sees 0.0/1.0. Only handing over a finished `QSGTexture`
   works. The provider *type*, not the image, is the lever.
3. **`QSGTextureProvider` is not a ShaderEffect source in Qt 6.12.** The class is exposed
   and subclassable from Python, but the ShaderEffect logs *"Texture t1 is not assigned a
   valid texture provider (std::nullptr_t)"* and binds nothing. Named sampler properties
   resolve `QQuickItem` sources only.
4. **`QSGTexture` exposes neither `format()` nor `isRenderable()`** in PySide6 6.12, and
   `rhiTexture()` is null until the render thread has created the texture — so the format is
   only observable after the texture has been used in the scene.
5. **The whole-frame comparison is limited to 1.22e-4 by the rasteriser**, not by the
   transport: the identical number appears for the application's existing byte-packed path.
   Choosing the sample position removes it and the arithmetic error is exactly 0. The E1b
   evidence measured 2.4e-7 through the raw RHI path, where the position is not rasterised.
6. **Replacing the provider's texture aborts the process** (SIGSEGV). Step 2 must own the
   texture deliberately.
7. **`QCoreApplication.processEvents()` can block forever** on the Cocoa event dispatcher;
   the probe uses a bounded `QEventLoop` instead.
8. **`weather.frag` is not the production field shader.** The running application uses
   `WeatherBarbs.qml`; `WeatherField.qml`/`weather.frag` is the diagnostic field component.
   Both share the provider → Image → ShaderEffect structure, so the transport answer holds
   for both, but the production shader has its own decoder and its own missing-data risk
   (vertex stage, varyings).

## Budget and honesty

The issue allowed one implementation/validation iteration and at most two native attempts.
**That budget was exceeded.** The transport question itself was answered within the first two
successful native runs; the additional iterations were probe plumbing, not new experiments:
a wrong `.qsb` output name, a QML truthiness bug that unbound every sampler, a byte-split
payload that divided the alpha channel by 255 as well, a stale texture provider shadowing
the QML `Image`, and a segfault during teardown that needed an explicit `os._exit(0)`. The
recorded artifact is the final probe run, and every number quoted above comes from it.

## What this means for ADR-001 D2

D2 (GPU spatial interpolation, decided as option (a) on 2026-10-19) is **unblocked**: the
unchanged 2e-5 gate is met through the application's own scene-graph path with explicit
`texelFetch` arithmetic — exactly the method E1b recorded — and the arithmetic reproduces
the CPU sampler exactly. The open question D2 does *not* settle is the resident field's
memory footprint: the transport as measured is RGBA32F (16 bytes per float) and therefore
larger than today's packing until the single-channel R32F upload is closed. Issue #65 can
proceed on the fidelity question; the ledger in D1's resource budget needs the payload
decision first.
