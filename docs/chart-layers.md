# Chart Layers panel

Open **View → Chart Layers** (also available in the Window menu). The panel
provides independent switches for Canadian online tiles (CHS), world
coastlines/fallback map, local ENCs, raster charts, CM93, OpenStreetMap
seamarks, offline MBTiles, and other vector charts.

These persistent, display-only switches affect both chart and log maps.
They do not rewrite individual chart selections in the library, alter routing
coastline checks, or bypass normal chart scale/coverage rules. New imports
respect their class switch. All classes are allowed by default; importing
other charts no longer silently disables the built-in seamarks layer.

To inspect one source, turn off the other classes. Turning every class off
leaves the map background, instruments and navigation overlays. This panel
controls source visibility; it does not redesign CHS/ENC handoff or change
their drawing priority.

## Offline MBTiles

In **Data Library → Charts**, choose **MBTiles…** and select a `.mbtiles`
archive. The app references the original file; it does not convert or copy it.
Use **Offline MBTiles charts** in Chart Layers to hide/show the whole class,
or use individual library checkboxes to choose archives. Removing an entry
does not delete its archive.

This first version supports Web Mercator **PNG raster** MBTiles, both TMS
(default) and XYZ row schemes. JPEG and vector/PBF archives are rejected with
an explanation. An indexed tile table/view is required so zoom discovery does
not scan a whole large archive; the source database is never modified to add
an index. Tile zoom levels are discovered from the database; sparse
areas fall back to available parent tiles, then show underlying charts where
no imagery exists. Beyond the stored zoom range imagery is rescaled rather
than invented. Low-zoom views requiring more than 512 tiles even at the lowest
stored level are suppressed to bound work.

SQLite reads and PNG decoding run in a background worker. The decoded cache
retains at most 256 entries / 64 MiB, excluding in-flight rendering surfaces
and SQLite's own cache. Connections are read-only and closed after each job;
no tile downloads occur. Enabled archives paint above CHS and local ENCs, with
later imports on top. This explicit overlay order is separate from Q2's quilt.

These are images, not ENC objects or routing/depth data. Labels and soundings
are baked into the source image; currency and navigation suitability depend
on the archive. Metadata attribution (or the archive name) appears on the map.

### Local validation

224 targeted tests and 43 subtests passed, including import/persistence/removal,
TMS/XYZ orientation, fractional zoom/retina output, parent fallback, corrupt
tiles, exact coverage clipping, antimeridian wrap, cancellation, worker-only
SQLite access, and bounded cache/work. Changed Python files pass Black and
the diff whitespace check.

Read-only smoke test on the supplied 2.1 GiB Vancouver Island archive: a
900×600 view near Tofino (49.15, -125.9), zoom 12, DPR 1 rendered correctly.
One run measured 4.5 ms metadata inspection, 27.9 ms initial rendering, and
1.6 ms median of five cached repeats, retaining 20 tiles / 5 MiB. OS disk-cache
state was not controlled; these are single-view smoke measurements, not a
general speedup benchmark. Source size and modification time were unchanged.
