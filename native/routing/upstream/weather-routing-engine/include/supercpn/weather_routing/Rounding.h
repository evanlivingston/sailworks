#pragma once

#include <vector>

#include "supercpn/weather_routing/Types.h"

namespace supercpn::weather_routing {

// Structural validation shared by the engine and the bridge. A valid spec has
// a finite positive radius, a known side and either both or neither exit
// bounds, ordered without wrap-around.
bool roundingConstraintValid(const RoundingConstraint& rounding);

// True when the radius is too small for the shared predicate's unambiguous-side
// lateral epsilon. The spec is well-formed but can never be satisfied.
bool roundingGeometricallyImpossible(const RoundingConstraint& rounding);

// One shared definition for engine completion and independent replay.
// `terminalCourseDegrees` is the course over ground of the last replayed
// sample, not the requested through-water heading.
RoundingMeasurement measureRounding(const RoundingConstraint& rounding,
                                    GeoPoint endpoint,
                                    double terminalCourseDegrees,
                                    GeoPoint mark);

inline bool roundingSatisfied(const RoundingConstraint& rounding,
                              GeoPoint endpoint, double terminalCourseDegrees,
                              GeoPoint mark) {
  return measureRounding(rounding, endpoint, terminalCourseDegrees, mark)
      .satisfied;
}

// Deterministic, bounded terminal candidates for a rounding direct connection.
// No candidate is exported unless it was replayed and accepted by the shared
// predicate.
std::vector<GeoPoint> roundingTargets(GeoPoint from, GeoPoint mark,
                                      const RoundingConstraint& rounding);

// Scalar progress for ranking already-generated arrival candidates. Lower is
// closer to satisfying the rounding; zero means satisfied.
double roundingProgress(const RoundingConstraint& rounding, GeoPoint endpoint,
                        double terminalCourseDegrees, GeoPoint mark);

}  // namespace supercpn::weather_routing
