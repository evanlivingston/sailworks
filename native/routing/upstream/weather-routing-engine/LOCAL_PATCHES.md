# Integration patches

xWeatherRouting 1.29.2 records the leading coastal-departure legs established
by independent chronological validation. The contour engine preserves that
metadata when Professional selects its incumbent, keeping the final host and
plotting checks consistent with the validated bounded endpoint policy.
`OriginalEngine.ValidatedCoastalDepartureSurvivesDeliveredChordChecks` covers
the handoff; offshore departures retain the full stand-off policy.

xWeatherRouting 1.29 permits motor-only headings outside the sailing-angle
limits when the best usable sailing STW is below the route's configured
motoring threshold. Performance providers explicitly distinguish usable zero
speed from unknown data. Search integration and independent chronological
validation use the same rule; sailing, motor-sailing and other safety limits
are unchanged. `LowWindMotor_tests.cpp` exercises Quick, Standard and
Professional with calm, stronger wind, currents and changing thresholds.

Mark rounding constraints (`rounding-constraints`) add an optional per-mark
side, radius and exit-bearing range. Completion, terminal direct connection and
independent replay share one pass-and-exit predicate; a route is accepted only
when the replayed terminal course satisfies it. No rounding means the existing
exact-point behaviour and no extra geometry. The additive side-of-track
evaluator in `Winding.h/.cpp` applies the issue's taut-string reduction, closes
the reduced sub-track with a chord back to its first point, and applies the
signed closed-loop winding decision table, including the multi-lap
`UnsatisfiableRounding` reason; the radius predicate above stays compiled
unchanged in this stage. The
Python/native payload no longer sends a radius as a constraint: it remains
legacy point metadata, parsed and preserved but never planned against.

`../weather-routing-engine.version` identifies the imported upstream baseline.
The integration repository also carries subsequent fixes; it is not a claim
that this directory is byte-for-byte identical to that upstream commit.

Weather Routing 1.17.5 adds bounded worker progress publication (including effort
identity), diagnostic graph-frontier summaries, and an optional wider forward
retry for long land-aware routes obstructed inside a narrow corridor. The
OpenCPN adapter enables this retry only for land-aware, non-scout calculations.
Existing resource ceilings and independent route validation remain authoritative.
The regression fixture is `ModernNativeEngine.WidensCollapsedContinentalForwardCorridorWithinBudget`.
