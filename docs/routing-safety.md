# Routing limits and motoring

Routing Plan has optional maximum true-wind, minimum sailing-speed/motor fallback,
and wind-based speed reduction controls. All are disabled by default. Settings
persist with the workspace and are copied into each immutable route/scenario
request, so changing controls cannot alter a calculation already in progress.

The wind cap rejects candidates above the configured wind speed; it is not a
clamp on forecast wind. Wind-only routing discards capped candidates rather than
allowing zero-speed waiting. Current-assisted scalar, leg-batch, and whole-frontier
paths reject capped candidates before adding current velocity. These checks use
the engines' existing environmental sampling positions and times; they do not
introduce an additional continuous hazard search between samples.

Speed adjustments run after polar efficiency. The optional wave proxy reduces
sailing speed above 20 kn wind, reaching a maximum 25% reduction at 40 kn. The
proxy itself uses wind speed only and models no wave height, period, direction
or gust data; a separate optional wave input for native calculations is
described below.
If adjusted sailing speed falls below the enabled threshold, the configured
motoring speed replaces it; the proxy does not reduce that motoring speed. Wind
caps still apply while motoring. Fuel range and engine endurance are not modeled.

Saved routing provenance records the enabled thresholds and explicitly identifies
the wave proxy. Scalar/frontier regressions verify the same adjusted speeds and
wind-cap rejection. Invalid or non-finite policy thresholds are rejected.

## Real wave data in native routing

Native calculations can additionally select a published wave forecast in the
Plan panel. The selection is a separate input from the display-only wave layer
and from the wind-based proxy. It samples:

- significant wave height in metres;
- mean wave period in seconds;
- wave direction in degrees, using the meteorological "coming from"
  convention relative to true north.

The wave entry must publish height, period and direction together. The native
bridge carries the entry's dataset identity, model run and artifact identity,
per-leg wave values and the area/time coverage into the result. The routing
inspector reports the wave source it sampled and whether the calculation used
real wave heights, the legacy wind-based proxy, or both.

Wave coverage must include the departure, both route endpoints and the
estimated route window (the same distance/3 kn window the engine preflights).
If it does not, a wave-enabled native calculation stops with an explicit
wave-data failure. It does not fall back to the wind proxy and does not carry a
zero or interpolated height across the gap.

Selecting real wave data does not yet change the speed policy: the proxy above
remains the only wave-speed effect in this release. Replacing or combining it is
a separate semantics decision.
