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
sailing speed above 20 kn wind, reaching a maximum 25% reduction at 40 kn. This
uses wind speed only: no wave height, period, direction, or gust data is modeled.
If adjusted sailing speed falls below the enabled threshold, the configured
motoring speed replaces it; the proxy does not reduce that motoring speed. Wind
caps still apply while motoring. Fuel range and engine endurance are not modeled.

Saved routing provenance records the enabled thresholds and explicitly identifies
the wave proxy. Scalar/frontier regressions verify the same adjusted speeds and
wind-cap rejection. Invalid or non-finite policy thresholds are rejected.
