// SPDX-License-Identifier: GPL-3.0-or-later
#include "supercpn/weather_routing/Rounding.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <numbers>

#include "supercpn/weather_routing/Engine.h"

namespace supercpn::weather_routing {
namespace {

constexpr double kRoundingCoincidentEpsilonNm = 1e-6;
constexpr double kRoundingLateralEpsilonNm = 0.01;
constexpr double kRoundingExitBearingEpsilonDegrees = 1e-9;
constexpr double kRoundingNmPerDegree = 60.0;
constexpr std::size_t kMaximumRoundingTargets = 8;

bool validExitRange(const RoundingConstraint& rounding) {
  if (!rounding.exitBearingMinDegrees && !rounding.exitBearingMaxDegrees)
    return true;
  if (!rounding.exitBearingMinDegrees || !rounding.exitBearingMaxDegrees)
    return false;
  const double minimum = *rounding.exitBearingMinDegrees;
  const double maximum = *rounding.exitBearingMaxDegrees;
  return std::isfinite(minimum) && std::isfinite(maximum) && minimum >= 0.0 &&
         minimum <= maximum && maximum < 360.0;
}

bool validExitRange(const RoundingSpec& rounding) {
  if (!rounding.exitBearingMinDegrees && !rounding.exitBearingMaxDegrees)
    return true;
  if (!rounding.exitBearingMinDegrees || !rounding.exitBearingMaxDegrees)
    return false;
  const double minimum = *rounding.exitBearingMinDegrees;
  const double maximum = *rounding.exitBearingMaxDegrees;
  return std::isfinite(minimum) && std::isfinite(maximum) && minimum >= 0.0 &&
         minimum <= maximum && maximum < 360.0;
}

// Local equirectangular offset from `origin`, in nautical miles. Only the ray
// crossing sign test uses this planar frame; the closest-pass projection below
// is evaluated on the great-circle segment and its distance comes from the
// shared great-circle helper.
struct LocalOffset {
  double eastNm{};
  double northNm{};
};

double cosLatitude(GeoPoint origin) {
  return std::cos(origin.latitude * std::numbers::pi / 180.0);
}

LocalOffset localOffset(GeoPoint point, GeoPoint origin) {
  double longitudeDelta = point.longitude - origin.longitude;
  while (longitudeDelta > 180.0) longitudeDelta -= 360.0;
  while (longitudeDelta < -180.0) longitudeDelta += 360.0;
  return {longitudeDelta * cosLatitude(origin) * kRoundingNmPerDegree,
          (point.latitude - origin.latitude) * kRoundingNmPerDegree};
}

constexpr double kRoundingEarthRadiusNm = 3440.065;

struct SegmentProjection {
  GeoPoint point;
  double distanceNm{};
};

// Project the mark onto the great-circle segment start->end. The along-track
// distance is measured from `start` on the great circle and clamped to the
// segment, so an interior closest pass is found between the recorded points and
// an over-the-end projection lands on the endpoint. Distance and course are
// then derived from that same interpolated point.
SegmentProjection projectOnSegment(GeoPoint start, GeoPoint end,
                                   GeoPoint mark) {
  const double segmentLength = distanceNm(start, end);
  const double startToMark = distanceNm(start, mark);
  if (segmentLength <= kRoundingCoincidentEpsilonNm ||
      startToMark <= kRoundingCoincidentEpsilonNm) {
    return {start, startToMark};
  }
  const double angularStartToMark = startToMark / kRoundingEarthRadiusNm;
  const double segmentBearing = initialBearingDegrees(start, end);
  const double relative = (initialBearingDegrees(start, mark) - segmentBearing) *
                          std::numbers::pi / 180.0;
  const double crossTrackAngle =
      std::asin(std::clamp(std::sin(angularStartToMark) * std::sin(relative),
                           -1.0, 1.0));
  double along =
      std::acos(std::clamp(std::cos(angularStartToMark) /
                               std::cos(crossTrackAngle),
                           -1.0, 1.0)) *
      kRoundingEarthRadiusNm;
  if (std::cos(relative) < 0.0) along = -along;
  along = std::clamp(along, 0.0, segmentLength);
  GeoPoint point = start;
  if (along >= segmentLength - kRoundingCoincidentEpsilonNm) {
    point = end;
  } else if (along > kRoundingCoincidentEpsilonNm) {
    point = destinationPoint(start, segmentBearing, along);
  }
  return {point, distanceNm(point, mark)};
}

double signedLateralNm(double courseDegrees, double bearingToMarkDegrees,
                       double distanceNmValue) {
  const double difference =
      (courseDegrees - bearingToMarkDegrees) * std::numbers::pi / 180.0;
  return std::sin(difference) * distanceNmValue;
}

struct ClosestPass {
  GeoPoint point;
  double distanceNm{};
  double courseDegrees{};
  double bearingToMarkDegrees{};
  double lateralNm{};
};

// Continuous closest approach over the delivered segments. A closest pass
// between two recorded points is measured on the interpolation of that segment.
ClosestPass closestPass(const std::vector<GeoPoint>& track, GeoPoint mark) {
  ClosestPass best;
  best.point = track.front();
  best.distanceNm = distanceNm(track.front(), mark);
  best.courseDegrees = track.size() > 1
                           ? initialBearingDegrees(track.front(), track[1])
                           : 0.0;
  if (best.distanceNm > kRoundingCoincidentEpsilonNm) {
    best.bearingToMarkDegrees = initialBearingDegrees(track.front(), mark);
    best.lateralNm = signedLateralNm(best.courseDegrees,
                                     best.bearingToMarkDegrees,
                                     best.distanceNm);
  }

  for (std::size_t index = 0; index + 1 < track.size(); ++index) {
    const SegmentProjection projection =
        projectOnSegment(track[index], track[index + 1], mark);
    if (projection.distanceNm >= best.distanceNm) continue;
    best.distanceNm = projection.distanceNm;
    best.point = projection.point;
    // Course over ground at the interpolated point, toward the segment end.
    const bool atSegmentEnd =
        distanceNm(projection.point, track[index + 1]) <=
        kRoundingCoincidentEpsilonNm;
    best.courseDegrees =
        atSegmentEnd
            ? initialBearingDegrees(track[index], track[index + 1])
            : initialBearingDegrees(projection.point, track[index + 1]);
    if (projection.distanceNm <= kRoundingCoincidentEpsilonNm) {
      best.point = mark;
      best.bearingToMarkDegrees = 0.0;
      best.lateralNm = 0.0;
      break;
    }
    best.bearingToMarkDegrees = initialBearingDegrees(projection.point, mark);
    best.lateralNm = signedLateralNm(best.courseDegrees,
                                     best.bearingToMarkDegrees,
                                     projection.distanceNm);
  }
  return best;
}

// Ray crossings: passages are counted as crossings of the ray from the mark
// along the bearing from the mark to the closest-pass point (the abeam ray,
// perpendicular to the course over ground at that pass). The ray is fixed for
// the whole sub-track, so one lap around the mark crosses it once and two laps
// twice, rather than counting one flip per delivered segment. Mark positions
// on the ray carry the previous state rather than starting a passage.
int countPassages(const std::vector<GeoPoint>& track, GeoPoint mark,
                  double courseDegrees) {
  if (track.size() < 2) return 0;
  const double radians = courseDegrees * std::numbers::pi / 180.0;
  const double east = std::sin(radians);
  const double north = std::cos(radians);
  const auto along = [&](GeoPoint point) {
    const LocalOffset offset = localOffset(point, mark);
    return offset.eastNm * east + offset.northNm * north;
  };
  int passages = 0;
  bool ahead = along(track.front()) < 0.0;
  for (std::size_t index = 1; index < track.size(); ++index) {
    const double current = along(track[index]);
    if (current < 0.0) {
      ahead = true;
    } else if (ahead) {
      ++passages;
      ahead = false;
    }
  }
  return passages;
}

void addTarget(std::vector<GeoPoint>& targets, GeoPoint point, double radiusNm,
               GeoPoint mark) {
  if (targets.size() >= kMaximumRoundingTargets) return;
  if (distanceNm(point, mark) > radiusNm + 1.0) return;
  for (const auto& existing : targets) {
    if (distanceNm(existing, point) <= 1e-6) return;
  }
  targets.push_back(point);
}

}  // namespace

bool roundingConstraintValid(const RoundingConstraint& rounding) {
  // radiusNm is legacy metadata: a payload that omits it (or carries any
  // arbitrary value) is still a valid side/exit contract. Distance never gates
  // the rounding, so there is no radius requirement here.
  return validExitRange(rounding);
}

bool roundingGeometricallyImpossible(const RoundingConstraint&) {
  // The closest-pass predicate has no distance gate, so no well-formed spec is
  // geometrically impossible. Kept for the preflight call site until the
  // legacy radius field is removed.
  return false;
}

std::string RoundingMeasurement::failureCode() const {
  switch (failure) {
    case RoundingFailure::None:
      return "none";
    case RoundingFailure::NotPassed:
      return "mark_not_passed";
    case RoundingFailure::CirclesMoreThanOnce:
      return "route_circles_rounding_mark_more_than_once";
    case RoundingFailure::AmbiguousPass:
      return "ambiguous_pass";
    case RoundingFailure::SideAmbiguous:
      return "side_ambiguous";
    case RoundingFailure::WrongSide:
      return "wrong_side";
    case RoundingFailure::ExitBearingOutOfRange:
      return "exit_bearing_out_of_range";
    case RoundingFailure::NotEvaluated:
      return "not_evaluated";
  }
  return "not_evaluated";
}

std::string RoundingMeasurement::failureReason() const {
  switch (failure) {
    case RoundingFailure::None:
      return "rounding satisfied";
    case RoundingFailure::NotPassed:
      return "mark not passed on this leg";
    case RoundingFailure::CirclesMoreThanOnce:
      return "route circles the rounding mark more than once";
    case RoundingFailure::AmbiguousPass:
      return "ambiguous pass";
    case RoundingFailure::SideAmbiguous:
      return "side ambiguous";
    case RoundingFailure::WrongSide:
      return "wrong side";
    case RoundingFailure::ExitBearingOutOfRange:
      return "exit bearing out of range";
    case RoundingFailure::NotEvaluated:
      return "not evaluated";
  }
  return "not evaluated";
}

RoundingMeasurement measureRounding(const RoundingSpec& rounding,
                                    const std::vector<GeoPoint>& subTrack,
                                    GeoPoint mark, double exitCourseDegrees) {
  RoundingMeasurement result;
  result.terminalCourseDegrees = normalizeHeading(exitCourseDegrees);
  if (subTrack.empty()) {
    result.failure = RoundingFailure::NotPassed;
    return result;
  }
  result.terminalBearingToMarkDegrees =
      initialBearingDegrees(subTrack.back(), mark);

  const ClosestPass closest = closestPass(subTrack, mark);
  result.closestDistanceNm = closest.distanceNm;
  result.closestPoint = closest.point;
  result.courseAtClosestDegrees = normalizeHeading(closest.courseDegrees);
  result.bearingToMarkAtClosestDegrees =
      normalizeHeading(closest.bearingToMarkDegrees);
  result.signedLateralNm = closest.lateralNm;
  result.hasApproach = subTrack.size() > 1;
  result.closestApproachBearingToMarkDegrees =
      result.bearingToMarkAtClosestDegrees;
  result.closestApproachCourseDegrees = result.courseAtClosestDegrees;
  if (std::abs(closest.lateralNm) >= kRoundingLateralEpsilonNm) {
    result.achievedSide = closest.lateralNm > 0.0 ? RoundingSide::Port
                                                  : RoundingSide::Starboard;
  }
  result.passageCount =
      countPassages(subTrack, mark, result.courseAtClosestDegrees);

  if (result.passageCount == 0) {
    result.failure = RoundingFailure::NotPassed;
    return result;
  }
  if (result.passageCount >= 2) {
    result.failure = RoundingFailure::CirclesMoreThanOnce;
    return result;
  }
  if (result.closestDistanceNm <= kRoundingCoincidentEpsilonNm) {
    result.failure = RoundingFailure::AmbiguousPass;
    return result;
  }
  if (std::abs(result.signedLateralNm) < kRoundingLateralEpsilonNm) {
    result.failure = RoundingFailure::SideAmbiguous;
    return result;
  }
  const bool portSide = rounding.side == RoundingSide::Port;
  if (portSide != (result.signedLateralNm > 0.0)) {
    result.failure = RoundingFailure::WrongSide;
    return result;
  }

  if (rounding.exitBearingMinDegrees && rounding.exitBearingMaxDegrees &&
      validExitRange(rounding)) {
    const double minimum = *rounding.exitBearingMinDegrees;
    const double maximum = *rounding.exitBearingMaxDegrees;
    result.exitRangeSatisfied =
        result.terminalCourseDegrees >=
            minimum - kRoundingExitBearingEpsilonDegrees &&
        result.terminalCourseDegrees <=
            maximum + kRoundingExitBearingEpsilonDegrees;
    if (!result.exitRangeSatisfied) {
      result.failure = RoundingFailure::ExitBearingOutOfRange;
      return result;
    }
  }

  result.satisfied = true;
  result.failure = RoundingFailure::None;
  return result;
}

RoundingMeasurement measureRounding(const RoundingConstraint& rounding,
                                    GeoPoint endpoint,
                                    double terminalCourseDegrees, GeoPoint mark,
                                    const std::optional<RoundingApproach>&
                                        approach) {
  RoundingSpec spec;
  spec.side = rounding.side;
  spec.exitBearingMinDegrees = rounding.exitBearingMinDegrees;
  spec.exitBearingMaxDegrees = rounding.exitBearingMaxDegrees;

  std::vector<GeoPoint> subTrack;
  if (approach.has_value() &&
      distanceNm(approach->point, endpoint) > kRoundingCoincidentEpsilonNm)
    subTrack.push_back(approach->point);
  subTrack.push_back(endpoint);

  RoundingMeasurement result =
      measureRounding(spec, subTrack, mark, terminalCourseDegrees);
  result.terminalCourseDegrees = normalizeHeading(terminalCourseDegrees);
  result.terminalBearingToMarkDegrees = initialBearingDegrees(endpoint, mark);
  return result;
}

std::vector<GeoPoint> roundingTargets(GeoPoint from, GeoPoint mark,
                                      const RoundingConstraint& rounding) {
  std::vector<GeoPoint> targets;
  // Legacy targeting fallback: an omitted radius must not make a valid request
  // unreachable. This value only shapes the radius-era candidate set; the
  // closest-pass predicate never reads it.
  constexpr double kLegacyTargetRadiusNm = 0.5;
  const double radius =
      std::isfinite(rounding.radiusNm) && rounding.radiusNm > 0.0
          ? rounding.radiusNm
          : kLegacyTargetRadiusNm;
  const double distance = distanceNm(from, mark);
  if (distance <= 1e-9 || !validExitRange(rounding)) return targets;

  const double markToFromBearing = initialBearingDegrees(mark, from);
  const double farBearing = normalizeHeading(markToFromBearing + 180.0);

  // The two tangents from the current position.
  if (distance > radius + 1e-9) {
    const double alpha =
        std::acos(std::clamp(radius / distance, 0.0, 1.0)) * 180.0 /
        std::numbers::pi;
    addTarget(targets, destinationPoint(mark, markToFromBearing + alpha, radius),
              radius, mark);
    addTarget(targets, destinationPoint(mark, markToFromBearing - alpha, radius),
              radius, mark);
  }

  // Required-side far-arc midpoint. The offset keeps the mark unambiguously on
  // the requested side instead of exactly abeam.
  const double midpointOffset =
      rounding.side == RoundingSide::Port ? 20.0 : -20.0;
  addTarget(targets, destinationPoint(mark, farBearing + midpointOffset, radius),
            radius, mark);

  if (rounding.exitBearingMinDegrees && rounding.exitBearingMaxDegrees) {
    const double minimum = *rounding.exitBearingMinDegrees;
    const double maximum = *rounding.exitBearingMaxDegrees;
    // Port means the mark is left of the terminal course, so the mark is the
    // clockwise tangent of that course. Starboard is the opposite tangent.
    const double sideOffset = rounding.side == RoundingSide::Port ? 90.0 : -90.0;
    const std::array<double, 3> exits{
        minimum, normalizeHeading((minimum + maximum) / 2.0), maximum};
    for (const double exit : exits) {
      addTarget(targets,
                destinationPoint(mark, exit + sideOffset, radius), radius, mark);
    }
  }

  return targets;
}

double roundingProgress(
    const RoundingConstraint& rounding, GeoPoint endpoint,
    double terminalCourseDegrees, GeoPoint mark,
    const std::optional<RoundingApproach>& approach) {
  const RoundingMeasurement measured =
      measureRounding(rounding, endpoint, terminalCourseDegrees, mark,
                      approach);
  if (measured.satisfied) return 0.0;

  double progress = std::isfinite(measured.closestDistanceNm)
                        ? measured.closestDistanceNm
                        : 0.0;
  switch (measured.failure) {
    case RoundingFailure::None:
      break;
    case RoundingFailure::NotPassed:
      progress += 100.0;
      break;
    case RoundingFailure::CirclesMoreThanOnce:
      progress += 200.0;
      break;
    case RoundingFailure::AmbiguousPass:
      progress += 1000.0;
      break;
    case RoundingFailure::SideAmbiguous:
      progress += 10.0;
      break;
    case RoundingFailure::WrongSide:
      progress += 50.0;
      break;
    case RoundingFailure::ExitBearingOutOfRange:
      if (rounding.exitBearingMinDegrees && rounding.exitBearingMaxDegrees) {
        const double minimum = *rounding.exitBearingMinDegrees;
        const double maximum = *rounding.exitBearingMaxDegrees;
        const double course = measured.terminalCourseDegrees;
        const double miss = course < minimum
                                ? minimum - course
                                : (course > maximum ? course - maximum : 0.0);
        progress += miss / 360.0;
      } else {
        progress += 0.25;
      }
      break;
    case RoundingFailure::NotEvaluated:
      progress += 1000.0;
      break;
  }
  return progress;
}

}  // namespace supercpn::weather_routing
