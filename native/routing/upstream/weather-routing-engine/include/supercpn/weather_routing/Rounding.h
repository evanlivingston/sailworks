#pragma once

#include <optional>
#include <string>
#include <vector>

#include "supercpn/weather_routing/Types.h"

namespace supercpn::weather_routing {

// Structural validation for the transitional radius-carrying request. The
// radius is legacy metadata and never a planning or acceptance constraint.
bool roundingConstraintValid(const RoundingConstraint& rounding);

// Cheap, deterministic preflight run before any search starts. Rejects an exit
// range that no feasible pass can satisfy, and never rejects a feasible range.
//
// The only provable impossibility is an interior mark whose required outbound
// course cannot make progress toward the next course point. The departing leg
// leaves the mark and must reach that point, so a range lying entirely more
// than a right angle away from the bearing to it can never be delivered: the
// boat would have to sail away from the point and then reverse, which the
// shared predicate counts as a second passage. A terminal mark has no such
// restriction, because its passing point can be placed at any bearing around
// the mark, so nothing is rejected there.
bool roundingGeometricallyImpossible(GeoPoint mark,
                                     const RoundingConstraint& rounding,
                                     std::optional<GeoPoint> nextPoint);

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

// Convert the legacy radius-carrying request into the side/exit contract. The
// radius is dropped: it is never a planning or acceptance constraint.
RoundingSpec roundingSpec(const RoundingConstraint& rounding);

// Concatenated sub-track overload for the legacy request type. The engine and
// the synthetic chain pass the full delivered track (the leg from the previous
// course point through the next, or to the delivered final endpoint), not just
// the arriving sub-run.
RoundingMeasurement measureRounding(const RoundingConstraint& rounding,
                                    const std::vector<GeoPoint>& subTrack,
                                    GeoPoint mark, double exitCourseDegrees);

// Ending-mark verdict. The sub-track must reach the mark exactly; no side or
// exit constraint applies. The first and last points of a concatenated
// sub-track are course points (ending marks), not rounding marks.
RoundingMeasurement measureEndingMark(const std::vector<GeoPoint>& subTrack,
                                      GeoPoint mark);

// Deterministic, bounded passing-point candidates on the required side. The
// distances are a solver decision (0.1, 0.5, 2 and 10 NM) plus the two
// tangents from the current position; no user radius shapes the set. With an
// exit range the set also carries the points whose outbound tangent equals the
// range minimum, midpoint and maximum: the abeam point on the required side for
// an interior mark, and for a terminal mark the foot of the perpendicular from
// the mark onto the ray from the current position at that course, so the
// departing leg actually leaves inside the range. No candidate is exported
// unless it was replayed and accepted by the shared predicate.
std::vector<GeoPoint> roundingTargets(
    GeoPoint from, GeoPoint mark, const RoundingConstraint& rounding,
    std::optional<GeoPoint> nextPoint = std::nullopt);

// Scalar progress for an already-measured candidate. Lower is closer to
// satisfying the rounding; zero means satisfied.
double roundingProgress(const RoundingMeasurement& measured);



}  // namespace supercpn::weather_routing
