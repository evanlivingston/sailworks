# Sailworks GRIB Data Loading and Visualization Architecture Analysis

**Read with:** [`adr-001-grib-rendering-architecture.md`](adr-001-grib-rendering-architecture.md)
for the decisions and gap disposition, and
[`reference-architecture-analysis.md`](reference-architecture-analysis.md) for the
reference implementation's techniques.

**What this document is:** a description of the code as it exists today, with file and
line citations, and the structural gaps that follow from it. It is evidence, not a plan.

## CURRENT IMPLEMENTATION

### 1. GRIB LOADING
**Location:** `/sailworks/core/grib.py`, `/sailworks/core/gribmanager.py`

- **Backend:** Uses GDAL (`from osgeo import gdal`) to open GRIB files via `gdal.Open(path)`
- **Threading Model:** 
  - Each worker thread opens its own GDAL dataset handle (private per worker)
  - GDAL handles are NOT shared between threads (each `Grib` instance has private `self.dataset`)
  - Worker threads managed via Python `threading.Thread` (daemon=True) in `WeatherStager._run()` and `OverlayRenderer._start_next()`
  - Qt Quick render thread (GUI) is separate from worker threads
- **Streaming/Partial Decode:** 
  - **NO streaming decode** - entire bands are read into memory via `band.ReadAsArray()`
  - In `Grib.get_rindex_data(t)` (line 147-165): reads full U and V bands then converts to lat/lon/value tuples
  - In `DisplayGrib._get_wind_data()` (line 167-188): crops after full band read using numpy slicing
  - No support for windowed/partial GDAL reads (would require `gdal.Dataset.ReadRaster()` or similar)
- **Caching Strategy:**
  - **L1 Cache:** `functools.lru_cache` on `Grib.get_rindex_data()` (maxsize=2048) and `get_pressure_rindex_data()` (maxsize=512) - caches decoded lat/lon/value tuples
  - **L2 Cache:** `WeatherSampler` in `gribsampling.py` maintains:
    - `_grid_cache`: LRU cache of sampled grids (limited by entry count=64 and byte limit=64MB)
    - Per-source `GridIndex.arrays`: LRU cache of decoded bands (max 6 bands per source, FIFO eviction)
  - **L3 Cache:** `WindOverlayRenderer._field_cache`: LRU cache of prepared wind/pressure columns (max 64 entries, 32MB byte limit)
  - **L4 Cache:** `gpuweatherjobs.DecodedBandCache`: Worker-private band cache (128MB limit) used by `DisplayWeatherSource`

### 2. DATA STRUCTURES
**Location:** `/sailworks/core/gribsampling.py`, `/sailworks/rendering/windrenderer.py`

- **Decoded Field Representation:**
  - **CPU-side:** NumPy float64 arrays (from `band.ReadAsArray()` → `np.asarray(..., dtype=np.float64)`)
  - **Storage:** Full 2D grids stored as NumPy arrays in `GridIndex.arrays` (per-source LRU cache)
  - **Processing:** 
    - Wind data: Stored as separate U/V components, converted to lat/lon/value tuples for sampling
    - Pressure data: Stored as single component, converted to lat/lon/value tuples
  - **No native GPU textures** during decode/decompression phase
  - **No pyramids/mipmaps:** Single resolution only; no multi-resolution storage
  - **Representation:** 
    - Raw data: `(height, width)` NumPy float64 arrays
    - Intermediate: `List[(lat, lon, value)]` tuples (from `get_rindex_data`)
    - Final: Grouped by longitude into "columns" for rendering: `(lon, lats_array, dirs_array, speeds_array)`

### 3. GPU VISUALIZATION
**Location:** `/sailworks/qt/gpuweather.py`, `/sailworks/qt/gpuscene.py`, `/sailworks/qt/shaders/`, `/sailworks/rendering/windrenderer.py`

- **Pipeline:** Qt Quick Scene Graph (QSG) via custom `QQuickItem` subclasses
  - `WindItem`: Renders wind barbs as `QSGGeometryNode` with `DrawTriangles`
  - `ImageItem`: Renders numerical weather textures as `QSGSimpleTextureNode`
- **Texture Formats:**
  - **Numerical Weather:** Custom RGBA8888 encoding where each float32 occupies 2 opaque RGB texels (see `gpuweather.py:encode_component()`)
    - U0, V0 (before timestep) and U1, V1 (after timestep) textures
    - 2x width expansion to store float32 bit pattern across 2 texels
    - Nearest filtering only (no interpolation) - decoding happens in shader
  - **Wind Barbs:** Not texture-based - rendered as geometry (triangles) with per-vertex attributes
- **Retained vs Immediate:**
  - **Retained Resources:** 
    - Numerical weather textures retained in Qt texture cache (managed by `QQuickImageProvider`)
    - Barb vertex buffers retained in `WindItem.vertices` (numpy float32 array)
  - **Immediate Mode:** None - all rendering goes through QSG retained mode
  - **Texture Uploads:** Happens on GUI thread via `QQuickWindow.createTextureFromImage()` in `ImageItem.updatePaintNode()`

### 4. INTERPOLATION
**Location:** `/sailworks/core/gribsampling.py`, `/sailworks/qt/shaders/weather.frag`

- **Primary Location:** CPU (in worker threads)
  - **Method:** Bilinear interpolation in `GridIndex.sample()` (lines 198-240)
    - Uses 4-sample bilinear weighting with bounds checking
    - Handles periodic grids (global wraparound)
    - Applies missing data handling (NaN for invalid/masked values)
  - **Wind-specific:** Combines U/V components after interpolation
- **GPU Interpolation:** Limited to temporal blending only
  - **Shader (`weather.frag`):** Linear blend between two timesteps (`blend` uniform)
    - `vectorAt()` performs `a + (b - a) * blend` in fragment shader
    - Spatial interpolation (bilinear) remains CPU-side
  - **Barb Interpolation:** None in shader - barbs use nearest-neighbor sampling via texelFetch

### 5. MEMORY MANAGEMENT
**Location:** Multiple caching layers throughout

- **Residency:** 
  - Worker-private: Each thread maintains its own caches
  - No global GPU residency tracking
  - CPU caches bounded by byte/entry limits
- **Budget & Eviction:**
  - **Decoded Band Cache** (`gpuweatherjobs.DecodedBandCache`): 128MB hard limit per worker
  - **Grid Index Arrays** (`gribsampling.GridIndex`): Max 6 bands per source (FIFO)
  - **Sampled Grid Cache** (`gribsampling.WeatherSampler`): 64 entries, 64MB byte limit
  - **Field Cache** (`windrenderer.WindOverlayRenderer`): 32MB byte limit, 64 entries
  - **Prefetch Store** (`overlayrenderer.OverlayRenderer`): 2 frames, 96MB byte limit
  - **Numerical Textures** (`gpuweather.WeatherImageProvider`): 32MB byte limit
- **Eviction Policies:** 
  - LRU (Least Recently Used) for most caches
  - FIFO for `GridIndex.arrays` (oldest first)
  - Byte-based eviction when limits exceeded

### 6. TIME SCRUBBING (FORECAST-TIME CHANGES)
**Location:** `/sailworks/qt/gpuweatherjobs.py`, `/sailworks/rendering/weatherstaging.py`

- **Architecture:** 
  - **Temporal Staging:** Worker prepares pairs of timesteps (`before`/`after`)
  - **Interpolation:** Linear blend in GPU shader (`weather.frag:vectorAt()`)
  - **Cache Key:** Based on timestep pair (`StagedWind.before`, `StagedWind.after`)
  - **Seeking:** 
    - If time within current staged range: use GPU blend (cheap)
    - If time outside range: worker must stage new pair (expensive)
- **Mechanism:**
  1. Worker computes native grid query points (lat/lon)
  2. Samples U/V components at `before` and `after` timesteps
  3. Encodes results as RGBA8888 textures (2 texels per float32)
  4. Uploads to Qt texture cache
  5. GUI blends textures in fragment shader using `blend` uniform
- **Limitations:** 
  - Only supports linear interpolation between exactly two staged timesteps
  - No arbitrary time interpolation without worker restage
  - Stage preparation blocks worker thread (CPU-bound)

### 7. THREADING
**Location:** `/sailworks/rendering/overlayrenderer.py`, `/sailworks/qt/gpuweatherjobs.py`

- **Worker Threads:**
  - **Overlay Renderer:** Dedicated thread per `OverlayRenderer` instance (daemon=True)
  - **Weather Stager:** Dedicated thread per `WeatherStager` instance (daemon=True)
  - Each worker thread maintains private resources:
    - Private GDAL handles (`Grib` instances)
    - Private `WeatherSampler`
    - Private `DecodedBandCache`
    - Private `WindLandMask`
- **Qt Thread Affinity:**
  - **GUI Thread:** Qt Quick scene graph updates, texture uploads, rendering
  - **Worker Threads:** Data loading, decoding, sampling, texture encoding
  - **No direct GPU access from worker threads** - all GPU resources managed by GUI thread
- **Ownership Model:**
  - **GUI owns:** Qt textures, scene graph nodes
  - **Workers own:** CPU-side data (NumPy arrays, GDAL handles)
  - **Transfer:** 
    - Workers produce `QImage` objects (CPU-side) via `gpuweather.encode_component()`
    - GUI thread uploads `QImage` to GPU via `createTextureFromImage()`
    - Zero-copy not attempted - full CPU→GPU upload each frame

### 8. IDENTIFIED BOTTLENECKS
**From:** `/docs/rendering-performance-plan.md` + architecture evaluation

- **Primary Bottlenecks Identified:**
  1. **CPU-bound data preparation:** Worker threads spend significant time in:
     - GDAL `ReadAsArray()` calls (decompression)
     - NumPy array operations (cropping, conversion to lat/lon/value tuples)
     - Bilinear sampling in `GridIndex.sample()` (Python loops over stencil points)
     - Texture encoding (`encode_component()` - bit manipulation loops)
  2. **Serializing dependencies:** 
     - Each weather request requires full worker processing (no pipelining)
     - Texture uploads happen on GUI thread causing stalls
  3. **Memory bandwidth:** 
     - Repeated CPU→GPU uploads of same data during time scrubbing
     - No retained GPU numerical textures (must re-upload each timestep change)
  4. **Latency amplification:** 
     - Weather staging (CPU) → texture upload (GUI blocked) → render → present
     - Each step adds latency; no overlap between stages
  5. **Cache inefficiency:** 
     - Caches keyed by exact coordinates/timesteps → poor reuse during panning
     - No spatial/temporal coherence exploitation in cache keys
  6. **Shader limitations:** 
     - Only temporal interpolation in GPU; spatial interpolation remains CPU
     - No opportunity for GPU-based spatial filtering or aggregation

## COMPARATIVE ANALYSIS

The reference implementation's techniques are analysed separately in
[`reference-architecture-analysis.md`](reference-architecture-analysis.md). Read that
alongside this document.

**The "Modern GPU-Optimal" column below is aspirational, not a parity target.** The
reference implementation performs *spatial* interpolation on the CPU (technique T6), so
GPU spatial interpolation is an advance beyond the reference rather than a target it
already meets. The load-bearing differences from the reference are architectural shape:
async task groups, retained resources, a dirty-flag retained scene graph, and
main-thread isolation.

Decisions and gap disposition are recorded in
[`adr-001-grib-rendering-architecture.md`](adr-001-grib-rendering-architecture.md).
Two items in the list below were changed by that ADR: GPU spatial interpolation was
approved on 2026-10-19 (D2), bounded by the existing 2e-5 gate, and GPU-side decode is
explicitly deferred (D5).

### VS MODERN GPU-OPTIMAL ARCHITECTURE

| Aspect | Sailworks | Modern GPU-Optimal | Gap Analysis |
|--------|----------------|-------------------|--------------|
| **Data Decoding** | CPU (GDAL → NumPy) | GPU-accelerated (compute shaders) | **Structural:** No offload of GRIB decoding to GPU; missing opportunity for parallel decompression |
| **Data Residency** | CPU caches + periodic GPU uploads | Persistent GPU numerical textures | **Structural:** No retained GPU-resident weather fields; every timestep change requires full re-upload |
| **Spatial Interpolation** | CPU (bilinear in Python) | GPU (hardware texture sampling or compute shader) | **Structural:** Spatial interpolation bottleneck remains on CPU; missing GPU sampling hierarchy |
| **Temporal Interpolation** | GPU (linear blend in shader) | GPU (linear blend in shader) | **Aligned:** Temporal blending correctly on GPU |
| **Threading Model** | Worker threads → GUI thread (serialized) | Async compute queues + GPU overlap | **Structural:** No overlap between compute and render; strict handoff causes pipeline stalls |
| **Memory Management** | Multi-layer CPU caches + GPU texture cache | Unified GPU memory budget with eviction | **Structural:** No GPU memory residency tracking; CPU caches don't reflect actual GPU usage |
| **Time Scrubbing** | Worker restage required for new timestep pairs | Async prefetch + double/triple buffering | **Structural:** No lookahead or pipelining; time changes block on worker availability |
| **Resource Granularity** | Full-frame textures/vertex buffers | Tile-based or clip-scoped rendering | **Structural:** No partial updates; entire frame reprocessed for small viewport changes |

### THE SEVEN STRUCTURAL GAPS

This is the list used by ADR-001 and by issues #62-#70. It matches the comparison
table above; the narrative items that follow elaborate on these and are not a
second, competing list.

| # | Gap | Closing decision | Issue |
| --- | --- | --- | --- |
| G1 | Decode location: CPU (GDAL -> NumPy) | D5 -- deferred, no near-term issue | — |
| G2 | Data residency: CPU-side caches, no GPU-resident field | D1 | #64 |
| G3 | Spatial interpolation: CPU Python loops | D2 -- decided 2026-10-19, GPU, bounded at 2e-5 | #65 |
| G4 | Pipeline model: strictly serial stages | D3 | #66 |
| G5 | Time scrubbing: two-timestep, view-specific restage | D4 | #67 |
| G6 | Memory management: per-worker CPU byte limits, no GPU budget | D1 | #64 |
| G7 | Threading model: serialized daemon threads, no overlap | D3 | #66 |

The comparison table's "Resource Granularity" row (full-frame textures) is a
consequence of G2 and is addressed by D1 rather than tracked separately. "Temporal
Interpolation" is already aligned.

### SPECIFIC ARCHITECTURAL GAPS (NOT JUST "SLOW")

1. **Missing GPU-First Data Pipeline:**
   - No mechanism to keep decoded weather data resident in GPU memory
   - Every frame requires CPU→GDAL→NumPy→encode→upload roundtrip
   - **Gap:** No path for GPU-resident numerical weather textures with compute-shader decompression

2. **No Compute Shader Utilization for Spatial Operations:**
   - Spatial interpolation (bilinear) done in CPU Python loops
   - No opportunity to leverage GPU texture hardware or compute shaders for filtering
   - **Gap:** Spatial sampling bottleneck never offloaded to GPU despite being embarrassingly parallel

3. **Strict Stage-Bound Rendering:**
   - Time scrubbing limited to exactly two pre-staged timesteps
   - No support for arbitrary time interpolation without worker stall
   - **Gap:** No temporal supersampling or asynchronous time warping capabilities

4. **CPU-GPU Ownership Friction:**
   - Clear separation: workers own CPU data, GUI owns GPU resources
   - No shared memory or zero-copy interfaces between worker and GPU
   - **Gap:** Mandatory serialization through CPU-side QImage objects creates bandwidth and latency barriers

5. **Cache Key Mismatch with Access Patterns:**
   - Caches keyed by exact (time, coordinates) but access patterns show temporal/spatial coherence
   - No exploitation of frame-to-frame coherence in cache invalidation
   - **Gap:** Poor cache utilization during interactive panning/scrubbing due to overly specific keys

6. **No Overlap Between Stages:**
   - Pipeline: [Worker Prepare] → [GUI Upload] → [Render] → [Present]
   - Each stage must complete before next begins (no double-buffering)
   - **Gap:** No opportunity to overlap weather preparation for next frame with rendering of current frame

### HIGHEST-LEVERAGE ARCHITECTURAL CHANGES

Ordered by leverage. The decision for each is recorded in
[`adr-001-grib-rendering-architecture.md`](adr-001-grib-rendering-architecture.md);
the issue that owns the work is listed alongside.

1. **GPU-Resident Numerical Weather Fields (Highest Impact)** — D1, issue #64.
   Eliminate the per-frame CPU->GPU upload during time scrubbing and panning.
2. **GPU Spatial Sampling (High Impact)** — D2, issue #65. **Decided 2026-10-19: do it
   on the GPU**, bounded by the existing 2e-5 accuracy gate. See the note above: the
   reference does this on the CPU, so this is an advance, not parity.
3. **Asynchronous Double-Buffered Pipeline (High Impact)** — D3, issue #66.
   Preparation of frame N+1 overlaps presentation of frame N.
4. **Unified GPU Memory Budget with Eviction (Medium Impact)** — D1, issue #64.
   Extends the existing resource ledger to cover GPU-resident numerical fields.
5. **Tile-Based or Clip-Scoped Rendering (Medium Impact)** — not yet decided.
   Revisit after D1-D3 are landed and the remaining cost is re-profiled.
