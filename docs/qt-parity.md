# GTK to Qt interface parity

GTK is the migration reference. Preserve its workspace organization, panel names,
controls and shared data behavior; adapt widget mechanics to Qt where necessary.
Do not replace established workflows with dialogs or a newly designed navigation
structure. Regatta is hidden in GTK and should remain absent in Qt.

| GTK reference | Qt migration status |
| --- | --- |
| Charts, Logs, Polars in the main workspace header | All three workspaces, above the context toolbar |
| Polars table on the left and full-circle chart on the right | Same split, table row stride and colored wind headers |
| GTK polar curves, tack mirroring, speed rings and hover details | Shared production Cairo renderer used by both frontends |
| Polar selector with Filename/Sail/Name/Type | Shared-dock selector; changing a row updates the workspace |
| Performance panel with STW/SOG, wind band, sample visibility/count and Clear samples | Same controls and shared session history/filtering |
| Shared NMEA panel reachable from all three views | One panel/session; transport state survives tab changes |
| Shared, draggable/floating tool panels | Qt docks retain tabs/splits; floating panels have compact draggable chrome, edge resize/snap, double-click docking and Window → Reset Panels |
| Polar manager for imports and ORC downloads | Same shared-dock Polar Manager, reusing the migrated import/ORC component |
| Logs map above plots | Independent map above plots with a vertical splitter |
| Logs Speed/Wind (A)/Wind (T)/Depth/HDG/RW switches | Same labels, defaults, series grouping, local time labels and wind-direction formulas |
| View-specific toolbars | Charts, Logs and Polars controls follow the selected workspace |
| Saved workspace geometry, views and dock layout | Qt-specific atomic workspace file, including Polars preferences and both map views |
| Native filesystem dialogs | Retained |
| Chart navigation tabs for tracks/routings/POIs/AIS | Left Navigation panel with GTK tab order, editable names/visibility, point editing and native import/export; AIS tab follows active targets |
| Track, route, POI and AIS portrayal | Shared GTK Cairo drawing, including selection styles, route-time markers, AIS orientation/vectors and label spacing |
| Navigation visibility and layout | Charts-only by default; Tabs reopens it, dock can move/float; preference and splits survive restart |
| Marine currents/tides controls and overlays | Separate Charts panels, area/route downloads with cancel, saved-source selection, shared arrows/shoreline clipping and station heights; marine settings retain priority/fallback/provenance |
| Worldwide current account settings | Settings → Currents uses the existing system credential store |
| Map instrument dashboard | Shared GTK Cairo gauges/connection summary on Charts and Logs; unchanged wide layout, wrapped gauges on narrow maps |
| Cursor wind details | Shared asynchronous sampler; position/wind status readout, private bounded GRIB handles and cancellation on time/source/view changes |
| Forecast transport, step interval, manual time and scrubber | Migrated on Charts, with shared enabled-source bounds and independent NMEA playback |
| Smooth zoom and pan inertia | Live Display preferences apply to Charts and Logs; immediate/animated cursor-anchored wheel zoom and shared GTK drag inertia, with lifecycle cancellation and deferred workspace save |
| Connection/display/current settings | Main working settings migrated; GTK General/AIS pages are hidden. Native pinch/two-finger pan, About/app identity, packaging and final parity validation remain |
| Routing parameters, comparisons and currents | Plan exposes engine parameters; Compare has the four GTK scenario axes; Currents uses shared coverage/download preparation, warnings and current-aware calculations |
| GTK menu/header appearance | Qt native OS menus/decorations retained; exact fonts/chrome differ |

This pass corrects the main workspace structure rather than claiming full UI
parity. Further migration slices should use this checklist and inspect GTK's
actual implementation before choosing a Qt layout. Existing explicit user
changes take priority: keep automatic timestamped NMEA recording, retained plots
on Stop, full playback curves/cursors and native pickers; do not resurrect old
Save/Clear/crop/cursor controls from legacy Glade files or old screenshots.
