# Qt frontend work

Applies to files in this directory.

## Read only what the task needs

- `docs/qt-first-pass.md` - how the Qt frontend relates to the GTK one.
- `docs/qt-parity.md` - the GTK to Qt interface parity checklist and its status.
- `docs/qt-default-validation.md` - validation of the default Qt launcher and
  the explicit GTK entry points.
- Renderer, viewport and GPU rules: `../rendering/AGENTS.md`.

## Rules

- The new viewport (`gribviewport.py`) is the target implementation; the old
  default map (`mapcanvas.py`) is being retired under issue #86.
- Keep the application's panels, controls, routes and navigation overlays
  working through the viewport. Report missing basemap data or load failures
  rather than showing a blank map.
