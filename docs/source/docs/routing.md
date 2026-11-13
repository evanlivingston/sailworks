Routing and navigation
======================

Tracks, routes and points of interest live in the **Navigation** panel in the left
sidebar, which has **Tracks**, **Routings**, **POIs** and **AIS** tabs. Routing turns a
loaded weather forecast and a boat polar into a suggested fastest route.

```{image} ../_static/images/manual/panel-navigation.png
:alt: Navigation panel, Tracks tab
:width: 300px
```

Tracks and points of interest
-----------------------------

1. In the **Tracks** tab press **New** to create a track.
2. Press **Add Point** to enter a coordinate, or choose **Edit** from the track's context
   menu and click the chart to append points.
3. Right-click a track to fit, export or remove it, and right-click a point to move,
   reorder, duplicate or remove it.

Tracks and navigation state are imported and exported with **File → Import GPX…** and
**File → Export navigation…**. Points of interest are managed from the **POIs** tab and
appear on the chart like other navigation objects. The **AIS** tab holds received AIS
targets when an AIS input is connected.

Planning a route
----------------

Routing needs a loaded forecast and a polar file.

1. Select the polar, the start mode and the departure time in the **Routing** panel.
2. Tune the routing limits and advanced options for the passage.
3. Press **Calculate route**.

Sailworks calculates the isochrones and draws the fastest route on the chart. The
**Routing inspector** lists the resulting legs, and the time controls replay the
computation so the boat position can be followed over time. Calculated routes appear in
the **Routings** tab of the Navigation panel, where they can be fitted, exported or
removed.

```{image} ../_static/images/manual/panel-routing.png
:alt: Routing panel with polar, start mode and departure time controls
:width: 300px
```

```{image} ../_static/images/manual/panel-navigation-routings.png
:alt: Navigation panel, Routings tab listing calculated routes
:width: 300px
```

Choosing a polar for routing is covered in
[Polars and performance](polars-performance.md).

Before you rely on a route
--------------------------

::::{warning}
Sailworks is a planning and analysis aid, not a primary navigation system. A calculated
route is a suggestion based on forecast weather and a polar file.

- Depth-aware routing is not implemented. The router has no depth or chart-hazard
  awareness: it does not check shoals, rocks, under-keel clearance or tide height.
  The coastline check keeps a route off land, but it is not a safety check.
- Currents can be supplied as an optional routing input, but they do not add tide,
  depth or hazard awareness.
- A route does not guarantee safe water. Check every leg against official charts and
  your own navigation before sailing it.
- Forecast weather changes. Re-check the forecast and the route before departure and
  while underway.
- Results depend on the selected polar. An inaccurate polar produces an inaccurate
  route.
::::

Next steps
----------

- [Set up charts and forecasts](charts-weather.md).
- [Import or select a polar](polars-performance.md).
- [Record the passage and review it afterwards](instruments-logs.md).
