# Normal startup rendering baseline — 2026-10-15

**This is the reference workload:** the app as it opens with the saved local
configuration, not a constructed heavy-data scene. No application renderer,
numerical sampling, model selection, or display-density setting was changed.

## Captured configuration

The full Qt window was restored from a private snapshot of installed application
data. Capture included 4,939 files (1,591.5 MiB); settings/data remained untouched
in their original locations. Each replay used a fresh disposable copy, not
hardlinks. Connections, recordings, partial downloads, and credentials were
excluded. The system credential loader was disabled in the benchmark worker.

* Native macOS Qt (`cocoa`), actual DPR **2**, saved maximized layout; active map
  **710 x 743 logical pixels** on the 1512 x 948 available screen.
* Saved Charts view near Monterey Bay, zoom 10.236; dark palette; dashboard off.
* **30 imported vector charts plus two base layers**, all enabled. Enabled does
  not imply every chart intersects the current view.
* **33 enabled forecast files**, dense wind barbs, visible isobars at **1 hPa**.
* Marine layer visible: six tide datasets; current arrows off.
* Five routes (505 route points), one track (three points); 2,126 retained wind
  samples in the initial completed viewport frame.
* Forecast clock frozen at capture time, **2026-10-15 21:00 local**, rather than
  advancing between repetitions. Snapshot SHA-256 and timezone are in the report.

Online chart rendering remained enabled but **cache-only**. No remote chart
requests or instrument connections were initiated. This Monterey-area replay
recorded zero CHS tile cache misses; it does not benchmark cold Canadian tiles.

## Results

[Raw native report](profiles/rendering/2026-10-15/startup-native.json): four
scenarios, three fresh processes each, 180 measured grabs each (**2,160 total**),
five warmup grabs, camera steps paced toward 16.667 ms when work permits. Results
below pool 540 samples per scenario. These are synchronous QWidget rendering
probes, **not compositor-presented FPS or input-to-photon measurements**.

| Scenario | Median / p95 / p99 / max grab (ms) | Grabs >50 ms | Post-replay worker-idle median |
| --- | --- | --- | --- |
| Stationary repaint | 24.3 / 28.1 / 31.3 / 36.7 | 0 | 0.28 s |
| Pan | 10.0 / 46.7 / 76.5 / 86.3 | 16 | 3.25 s |
| Reverse pan | 25.1 / 56.4 / 79.5 / 97.9 | 30 | 0.96 s |
| Zoom out | 22.8 / 30.8 / 36.6 / 89.3 | 3 | 4.85 s |

The post-replay probe includes a deliberate 200 ms quiet window, rendering,
event-loop service, and worker completion. It is not a direct measure of full
spatial coverage. Import/Qt initialization and snapshot copying are outside the
startup timer. From controller construction to initial worker idle took a median
**13.21 s** across the 12 runs. No overlay errors or logged warnings occurred in
the completed baseline, and all observed overlay jobs completed their callbacks.

### What is actually expensive

1. **Foreground wind rendering dominates steady repaint.** In the stationary
   scene, the wind-barb paint span had a median **21.43 ms**, versus **24.30 ms**
   for the whole grab. Every stationary grab exceeded 16.7 ms despite zero base
   rebuilds. Navigation rebuilds were approximately a millisecond, not the large
   navigation stalls seen with the synthetic 10,000-point stress fixture.
2. **Base image rebuilds cause pan spikes.** Pan/reverse-pan rebuilt the base
   image 30 times each across their three repetitions. Median base rebuild was
   **45.14 / 44.20 ms**, with Qt painting, chart portrayal, compositing, and image
   copying still included in that span. These need finer attribution before
   selecting the replacement architecture.
3. **Weather preparation is repeatedly discarded.** During pan, 24 of 27 wind
   jobs started in the replay were canceled; reverse-pan canceled all 17, and
   zoom-out canceled all three. Only three pan jobs published a frame. Completed
   initial weather frames took roughly 3.5 s, while zoom settlement required
   another multi-second weather preparation. Overscan alone cannot hide this.
4. **Image and total process memory must be distinguished.** Known retained
   surfaces reached **186.6 MiB**; maximum process high-water RSS was
   **1,455.9 MiB** across these scenarios. RSS includes source objects, decoded
   arrays, libraries, and transient startup allocations. This is not a current-RSS
   trace or evidence of a memory leak, and no long-run plateau was measured.

**Do not interpret pan's lower median as better rendering.** Existing wind
vectors move outside the viewport during a long pan, reducing foreground work
before a replacement field arrives. Future comparisons need content/coverage
checks so that missing weather cannot masquerade as a speed improvement.

### Independent CPU diagnostic

A separate 60-grab stationary cProfile replay (not pooled into the timing
baseline) spent **1.438 of 1.512 s** in Qt `drawPath` relative to the wind-barb
paint span. Cached geometry lookup consumed about **0.004 s**, and path
move/line construction about **0.025 s**. Thus merely caching QPainterPath
construction or projecting fewer samples is unlikely to fix this saved scene.

See [the CPU excerpt](profiles/rendering/2026-10-15/startup-cpu-summary.txt).
cProfile perturbs Python timings and this trace profiles the main thread, not
the weather worker's internal computation. A separate startup observation in
that trace showed repeated collection serialization; `Collection.load_json()`
calls `clear()`/`append()`, which save as they load. Treat that as a focused
startup follow-up, not as an explanation of pan rendering costs.

## Updated experiment plan

These findings supersede the synthetic fixture's optimization ordering:

1. **Wind drawing experiment first.** Compare native foreground rendering
   alternatives such as batched primitives and bounded cached glyph/raster
   representations. Preserve density, exact directions/speeds, opacity, crisp
   fixed screen size, and sample selection. Measure drawing separately from
   geometry; validate stationary and moving views at fractional zoom/DPR.
2. **Remove synchronous base rebuild stalls.** Split base attribution into chart
   drawing, raster composition, allocation, and ownership-copy spans. Evaluate
   independent worker-owned static frames or spatial chunks; retain the last
   valid frame while preparing replacements. Preserve chart scale selection,
   masks, seams, labels, and attribution. Do not simply enlarge every surface.
3. **Separate weather data retention from viewport rendering.** Measure handle
   opening, source indexing, band decode, sampling, fill, and contours on the
   worker. Evaluate view-independent geographic/time chunks and a byte-accounted
   working set. Keep current multi-model/newest-run/fallback semantics; do not
   gain speed by silently disabling older sources or changing interpolation.
4. **Then add predictive prefetch with quality observability.** Prioritize
   visible gaps, likely pan destination, nearby regions, then adjacent zoom
   levels. Base lookahead on measured velocity/latency, not fixed image padding.
   Record per-layer usable coverage, retained/displayed glyph counts, useful
   prefetch hit rate, canceled/wasted work, reverse-pan reuse, and memory plateau.
   Local preparation and automatic network downloads need separate policies.
5. **Keep startup persistence separate.** Verify load/save behavior and profile
   allocations before changing collection persistence. Preserve saved data and
   do not combine an unrelated storage refactor with a rendering optimization.

Acceptance remains the same: compare the **same snapshot and camera replay**,
track p99/max and UI service delays, bound memory, and reject gains caused by
less rendered coverage or altered data. The synthetic scenarios remain useful
scaling tests, not the primary workload.
