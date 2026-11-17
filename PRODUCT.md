# Product

<!-- impeccable:product-schema 1 -->

## Platform

adaptive

## Users

Primary users are sailing data analysts and tinkerers, coastal racers and
performance sailors, and delivery skippers or professional navigators. They
bring their own boat data — polars, charts, forecasts and instrument logs — and
use Sailworks to decide when to leave, which way to go, and what actually
happened afterwards.

Cruising sailors were considered and not selected as a primary audience.

## Product Purpose

Sailworks plans sailing routes through real weather on the user's own desktop,
using the user's own charts, polars and instrument feed. The user picks a boat,
loads a forecast, sets where they want to go, and Sailworks works out when to
leave and which way to go. The same application then carries the plan onto a
live chart beside the instruments and into the recorded log for review.

Success means four things at once: Sailworks is a credible open-source
alternative to a serious performance-analysis and routing workbench; it is
trusted for real race and passage planning; it becomes a daily-use analysis
tool rather than an occasional experiment; and other sailors and developers
adopt, contribute to and extend it.

## Positioning

An open-source alternative to a serious performance analysis and routing
workbench, joining routing, forecast, live instruments and log analysis in one
desktop application.

## Operating Context

- **Planning at a desk:** comparing routes through a forecast, scrubbing the
  forecast timeline, checking currents and chart detail, and keeping results in
  a session.
- **On the water:** NMEA 0183 and NMEA 2000 instruments, AIS targets, a live
  boat dashboard and the chart.
- **After sailing:** replaying NMEA and GPX logs, reviewing tracks, and
  comparing recorded sailing data with the boat's polars.
- **Data sources:** GRIB forecasts, raster and vector charts, boat polars, and
  instrument or GPX logs. Core planning works with local files; some forecast
  and current sources require free accounts or credentials.
- **Runs on:** macOS, Linux and Windows, from a Python 3.12+ source install with
  native GDAL and Cairo.

## Capabilities and Constraints

- Multi-point weather routing with selectable algorithms and configurable
  parameters, including current-aware routing.
- GRIB1 and GRIB2 weather, multiple forecast sources, and a forecast timeline
  that keeps each model's time range separate.
- A chart stack spanning GSHHS and GeoJSON basemaps, KAP, GeoTIFF and other
  GDAL rasters, CM93, MBTiles, NOAA ENC, OpenSeaMap and CHS imagery, with
  offline caches.
- A boat polar library and editor, a session manager for POIs, tracks, routings
  and races, and GPX import and export.
- NMEA 0183 and NMEA 2000 input, AIS rendering, a realtime dashboard, NMEA 2000
  recording, and NMEA and GPX log analysis and replay.
- The current shipping surface is a native Qt 6 desktop application. Mobile
  surfaces are planned later and are not built.
- Sailworks is a planning and analysis aid, not a primary navigation system.
- Depth-aware routing and automatic chart updates are not implemented.
- NMEA 2000 connections are receive-only.
- NOAA ENC downloading covers the U.S. catalog and produces snapshots; CHS
  imagery is a viewing service and does not supply depths to the router.
- Licensed GPL-3.0-or-later. Sailworks is a fork of GWeatherRouting; upstream
  copyright notices and provenance are retained, and the GWeatherRouting name
  and logo remain with the upstream project.

## Brand Commitments

- The product name is **Sailworks**.
- The voice is calm, technical and direct, like a serious instrument.
- No other brand identity, logo system or personality was confirmed.

## Evidence on Hand

- `README.md`: feature inventory, limitations, installation and platform notes.
- `docs/source/`: user documentation, quickstart and manual pages.
- `docs/source/_static/images/readme-routing.png`,
  `readme-logs.png` and `readme-polars.png`: real application screenshots.
- `test/`: the automated test suite.
- No customer testimonials, commercial benchmarks, pricing, partnerships or
  third-party endorsements are on hand; future work must not fabricate them.

## Product Principles

1. **One workbench from plan to review.** Routing, live instruments and log
   analysis are one continuous workflow, not separate tools.
2. **Be trustworthy for real planning.** The route and the analysis must be
   accurate and inspectable enough to trust for a real race or passage.
3. **Stay open.** Open source is part of the product's identity: the code can be
   inspected, modified and extended.
4. **Behave like a serious instrument.** Calm, precise and direct; the interface
   serves the data and the decision, never decoration.
