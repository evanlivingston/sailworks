# Panel docking

- Drag within a sidebar tab strip to reorder tabs. Drag out of the strip to
  detach that panel; the same drag continues moving the floating panel.
- Tabbed panels have no second title bar. A lone sidebar panel keeps one
  header so it can still be dragged or closed.
- Drag a floating panel by its header. The blue rectangle previews the drop:
  the middle of a sidebar panel groups it as a tab; the top or bottom stacks
  it vertically. The tab strip itself is also a tab-group target.
- Drop near an empty left/right workspace edge to create a sidebar there.
  Sidebar splitters resize vertically stacked panels.
- Near the top/bottom of another floating panel, a blue rectangle previews
  vertical alignment at the other panel's width. These remain independent
  floating windows, not a persistent floating tab/split container.
- Double-click a floating header to return to its previous dock location.
  Double-click a dock tab to float it.

Panel dragging uses application-owned movement, not native window movement.
There are intentionally no screen-edge half-screen/maximize snap targets.
Floating content stays opaque, without recreating native windows or enabling
translucent backing stores during docking transitions.

## Native acceptance checks

Automated offscreen checks exercise tab identity/reordering, tear-off/re-dock,
previews, top/bottom splits of tab groups, floating alignment, allowed areas,
hidden peers, content geometry, resizing, and Qt layout restoration. They do
not prove native macOS appearance or WindowServer behavior.

On macOS, check at Retina scale that Routing's nested tabs and scroll content
stay inside its floating frame, that dragging across screen edges does not
invoke desktop tiling, and that close/reopen, repeated detach/re-dock, sidebar
splitter resizing, and workspace restart preserve the intended layout. Repeat
with multiple monitors and with macOS window tiling enabled.
