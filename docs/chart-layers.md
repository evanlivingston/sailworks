# Chart Layers panel

Open **View → Chart Layers** (also available in the Window menu). The panel
provides independent switches for Canadian online tiles (CHS), world
coastlines/fallback map, local ENCs, raster charts, CM93, OpenStreetMap
seamarks, and other vector charts.

These persistent, display-only switches affect both chart and log maps.
They do not rewrite individual chart selections in the library, alter routing
coastline checks, or bypass normal chart scale/coverage rules. New imports
respect their class switch. All classes are allowed by default; importing
other charts no longer silently disables the built-in seamarks layer.

To inspect one source, turn off the other classes. Turning every class off
leaves the map background, instruments and navigation overlays. This panel
controls source visibility; it does not redesign CHS/ENC handoff or change
their drawing priority.
