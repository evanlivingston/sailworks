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
| Shared, draggable/floating tool panels | Native Qt docks support tabs/splits, visibility and floating windows |
| Polar manager for imports and ORC downloads | Same shared-dock Polar Manager, reusing the migrated import/ORC component |
| Logs map above plots | Independent map above plots with a vertical splitter |
| Logs Speed/Wind (A)/Wind (T)/Depth/HDG/RW switches | Same labels, defaults, series grouping, local time labels and wind-direction formulas |
| View-specific toolbars | Charts, Logs and Polars controls follow the selected workspace |
| Saved workspace geometry, views and dock layout | Qt-specific atomic workspace file, including Polars preferences and both map views |
| Native filesystem dialogs | Retained |
| Chart navigation tabs for POI/track/route/AIS tools | Routing migrated; other navigation tools remain pending |
| Marine currents/tides controls and overlays | Pending |
| Map instrument dashboard and wind-cursor details | Pending |
| Forecast navigation/timeline shortcuts and broader settings | Partially migrated; GTK control parity remains pending |
| Routing efficiency and algorithm parameter controls | GTK routing wizard has additional controls; Qt still uses the existing defaults |
| GTK menu/header appearance | Qt native OS menus/decorations retained; exact fonts/chrome differ |

This pass corrects the main workspace structure rather than claiming full UI
parity. Further migration slices should use this checklist and inspect GTK's
actual implementation before choosing a Qt layout. Existing explicit user
changes take priority: keep automatic timestamped NMEA recording, retained plots
on Stop, full playback curves/cursors and native pickers; do not resurrect old
Save/Clear/crop/cursor controls from legacy Glade files or old screenshots.
