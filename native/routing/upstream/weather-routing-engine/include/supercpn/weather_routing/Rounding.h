#pragma once

#include <optional>
#include <string>
#include <vector>

#include "supercpn/weather_routing/Types.h"

namespace supercpn::weather_routing {

// Structural validation for the transitional radius-carrying request. The
// radius is legacy metadata and never a planning or acceptance constraint.
bool roundingConstraintValid(const RoundingConstraint& rounding);

// Retained for the preflight call site that still carries the legacy radius.
// The closest-pass predicate has no distance gate, so a well-formed spec can
// never be geometrically impossible.
bool roundingGeometricallyImpossible(const RoundingConstraint& rounding);

// Closest delivered point to the mark and the course over ground there.
struct RoundingApproach {
  GeoPoint point;
  double courseDegrees{};
};

// Shared closest-pass evaluator for engine completion and independent replay.
//
// `subTrack` is the delivered leg sub-track in order (the leg from the previous
// course point to the next, or to the delivered final endpoint). Distance and
// course are evaluated continuously over every segment: the mark is projected
// onto each segment's great circle, clamped to the segment endpoints, and the
// distance, course and bearing are all derived from that interpolated point, so
// a closest pass between two recorded points is measured on the interpolation.
//
// The mark must be passed: zero ray crossings is NotPassed. A passage is an
// ahead-to-astern crossing of the ray from the mark along the bearing from the
// mark to the closest-pass point, so a lap counts one crossing rather than one
// per delivered segment. Two or more crossings are rejected as
// CirclesMoreThanOnce. The side is judged at the closest pass from
// the signed lateral cross(course, bearingToMark) * distance: port requires a
// positive lateral, starboard a negative one, and |lateral| within 0.01 NM is
// ambiguous. `exitCourseDegrees` is the delivered outbound course (the first
// replayed point of the next leg to the second, or the terminal course for the
// destination) and must lie in the inclusive exit range when one is set.
RoundingMeasurement measureRounding(const RoundingSpec& rounding,
                                    const std::vector<GeoPoint>& subTrack,
                                    GeoPoint mark, double exitCourseDegrees);

// Transitional adapter for the radius-era engine call sites. It reconstructs
// the two-sample sub-track from the recorded closest approach and the delivered
// endpoint; the engine will pass full leg sub-tracks once completion is wired
// to the shared predicate. No radius gate is applied.
RoundingMeasurement measureRounding(
    const RoundingConstraint& rounding, GeoPoint endpoint,
    double terminalCourseDegrees, GeoPoint mark,
    const std::optional<RoundingApproach>& approach = std::nullopt);

// Deterministic, bounded terminal candidates for a radius-era direct
// connection. No candidate is exported unless it was replayed and accepted by
// the shared predicate.
std::vector<GeoPoint> roundingTargets(GeoPoint from, GeoPoint mark,
                                      const RoundingConstraint& rounding);

// Scalar progress for ranking already-generated arrival candidates. Lower is
// closer to satisfying the rounding; zero means satisfied.
double roundingProgress(
    const RoundingConstraint& rounding, GeoPoint endpoint,
    double terminalCourseDegrees, GeoPoint mark,
    const std::optional<RoundingApproach>& approach = std::nullopt);

}  // namespace supercpn::weather_routing
