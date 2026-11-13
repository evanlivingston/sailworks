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
side and exit-bearing range. Completion, terminal direct connection and
independent replay share one pass-and-exit predicate; a route is accepted only
when the replayed track satisfies it. No rounding means the existing
exact-point behaviour and no extra geometry. The additive side-of-track
evaluator in `Rounding.h/.cpp` measures the continuous closest approach over
every delivered segment, counts ahead-to-astern ray crossings of the mark,
judges the side from the signed lateral at that closest pass and applies the
inclusive exit range; two or more crossings are rejected with the multi-lap
`UnsatisfiableRounding` reason. Distance is reported, never gated: a wide pass
on the requested side is compliant. The radius-carrying predicate stays
compiled unchanged for the radius-era engine call sites in this stage, but the
closest-pass evaluator applies no radius acceptance path and validation no
longer requires a radius: a payload that omits it, or carries any arbitrary
legacy value, is accepted. The Python/native
payload no longer sends a radius as a constraint: it remains legacy point
metadata, parsed and preserved but never planned against.

`../weather-routing-engine.version` identifies the imported upstream baseline.
The integration repository also carries subsequent fixes; it is not a claim
that this directory is byte-for-byte identical to that upstream commit.

Weather Routing 1.17.5 adds bounded worker progress publication (including effort
identity), diagnostic graph-frontier summaries, and an optional wider forward
retry for long land-aware routes obstructed inside a narrow corridor. The
OpenCPN adapter enables this retry only for land-aware, non-scout calculations.
Existing resource ceilings and independent route validation remain authoritative.
The regression fixture is `ModernNativeEngine.WidensCollapsedContinentalForwardCorridorWithinBudget`.
