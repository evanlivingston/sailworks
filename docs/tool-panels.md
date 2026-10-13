# Tool panels

`gtk/widgets/tooldock.py` supplies a tool-agnostic `ToolDock`: a horizontal
splitter with the chart workspace on the left and a notebook on the right.
Tools register a stable key, a title and a GTK content widget. The dock owns the
tab strip, show/hide state, tab reordering, native GTK tab dragging, floating
windows and cleanup. Content survives reparenting and hiding.

```python
dock = ToolDock(chart_workspace, main_window)
dock.register("currents", "Currents", current_controls)
dock.register("tides", "Tides", tide_controls)
dock.show_group(("currents", "tides"))
dock.hide_group(("currents", "tides"))
```

Floating windows are undecorated. The notebook supplies a close control and a
drag grip alongside its tabs; no repeated content heading or placement button
is required. Dragging the grip moves the group, and releasing near the right
workspace edge docks it. Dragging individual tabs uses GTK notebook transfers,
including an edge-drop destination when the main dock is hidden.

Additional tools register with the same dock. Floating notebooks share a group
with the main dock so tabs can be dragged between them. `panel-visibility-changed`
provides the tool key and visible state for synchronizing toolbar actions. Panel
visibility is independent of what its tool renders. Mode is retained during the
session; layout is not yet persisted across application restarts.

`MarineToolPanel` supplies current-model controls and coverage; `TideToolPanel`
supplies station water-level controls and automatic downloads for the view.
`SpatialMarinePanel(compact=True)` reuses the existing provider, source-selection
and download controls in a narrow layout. `MarineDownloadJobs` shares the worker,
progress, cancellation and destruction lifecycle with the advanced marine
manager window. Hiding a panel does not cancel its download; destroying its
content does. Progress stays outside the tool's scrollable controls.

Each marine tab has a cog beside its visibility control. Both open the same
`MarineSettingsWindow`, selecting the matching Currents or Tides tab. The
application Settings window has a Currents tab for the worldwide Copernicus
account. It shares the marine manager's coverage planner with both the palette
and source settings. Credentials are saved in the operating system credential
store (Keychain on macOS) and restored when the application starts. The password
entry clears after submission. “Forget account” removes the saved login.
Station predictions, source inspection and
priority and fallback controls appear directly in the marine settings tabs.
Download area and interval controls live in the chart palettes; both use
“Start (UTC)”. There is no secondary manager window. Both palettes use shared content margins and spacing,
and use the global chart time without repeating it in their contents. Download
forms retain their interval controls.

Both palettes use `CoverageTarget` for the chart-area/route selector. Tide route
downloads select CHS water-level stations inside the same padded waypoint bounds
used for current coverage, including routes crossing the date line. Bounds are
captured before the worker starts. The shared `DownloadStartEntry` follows the
global chart time until the user edits it or selects Now using the playback-style
icon inside the entry. Now uses the current UTC minute and preserves the chart
time, coverage selection and Hours. There is no separate chart-area/time button.

Both marine palettes use “Download” for the section and “Download data” for the
button. The current palette omits the automatic-provider/login paragraph;
provider provenance remains available in saved-source details and download progress.

Current downloads are grouped by provider, model and resolution in an expandable
tree. Model rows summarize download counts and merged UTC intervals; separate
intervals retain gaps. Expanding a model shows each download's interval, forecast
run and short identifier, with a circled information icon revealed on row hover. Hovering the icon shows
a human-readable model description, provider, resolution and download details;
the Source details dropdown has been removed. Keyboard tooltip requests show
information for the selected row.
Model checkboxes enable/disable all their downloads; mixed selections display an
indeterminate checkbox. Child checkboxes and removal operate on one download.
Expansion and selection survive refreshes.

Current coverage distinguishes absent downloaded coverage from a location with
no configured provider. Outside the regional CIOPS domains, a worldwide account
must be configured; otherwise the panel reports “No provider for current area”.
Saved offline data remains usable regardless of account configuration.
