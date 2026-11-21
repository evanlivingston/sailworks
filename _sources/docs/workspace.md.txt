Workspace and panels
====================

Sailworks remembers how you arranged the app between launches, and it lets you stack,
split and float the sidebar components while you work.

What is restored between launches
---------------------------------

The app automatically remembers its active tab, Charts and Logs map positions and zoom,
chart display toggles, Logs graph selections, selected polar and performance filters, and
live-stream and playback-speed preferences. It also restores sidebar components, selected
component tabs, groups, divider positions, sidebar width, floating-window geometry, and
the main window's normal size and maximized state. Polar Manager's selected tab and ORC
search text are remembered.

View changes are saved after a short idle period and again on normal exit. Saving waits
while a map is being panned or zoomed. Workspace preferences use a separate versioned
`workspace.json` in the app's data directory (on macOS,
`~/Library/Application Support/sailworks/`). Files are replaced atomically; missing or
unreadable workspace files fall back to the default view. Saved windows are fitted onto
the available monitors when the display arrangement changes.

What is session-only
--------------------

Sample history, loaded NMEA files, recording state, and playback position are
session-only. Launching starts with live input selected and the saved streaming
preference; it does not reopen or automatically play a log. Existing settings,
connections, tracks, routes, and downloaded data retain their own storage.

Arranging components
--------------------

### Stack components vertically

Drag a component's named tab to the upper or lower part of another component below its
tab strip in the sidebar. A blue horizontal line marks where it will be placed; release
to create a vertical stack. Drag the divider between groups to adjust their heights. Each
group can still contain multiple tabs.

### Combine or float components

Drop on a group's tab strip or middle to combine components as tabs. The dotted grip
moves a whole tab group: drag it out to float, or back over the sidebar to place that
group above, below, or inside another group. Hiding a group keeps its component state;
reopening a component restores its place in the stack.

NMEA, Performance and Polar Manager all share this sidebar and floating-window behavior,
and keep their state when hidden and reopened. Open **NMEA** from the toolbar in
**Charts**, **Logs**, or **Polars** and all three tabs share one sidebar and one NMEA
component. Open components stay visible when switching tabs, including components opened
from another tab's toolbar. Closing a component hides it everywhere; any NMEA toolbar
button reopens that same component. See
{ref}`Instruments and logs <shared-nmea-streaming-and-playback>` for what
the shared NMEA component does.

Next steps
----------

- [Charts and weather](charts-weather.md)
- [Routing and navigation](routing.md)
- [Polars and performance](polars-performance.md)
- [Instruments and logs](instruments-logs.md)
