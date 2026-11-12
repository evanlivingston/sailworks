// SPDX-License-Identifier: GPL-3.0-or-later
#include "supercpn/weather_routing/Rounding.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <limits>
#include <numbers>

#include "supercpn/weather_routing/Engine.h"

namespace supercpn::weather_routing {
namespace {

constexpr double kRoundingDistanceEpsilonNm = 1e-3;
constexpr double kRoundingCoincidentEpsilonNm = 1e-6;
constexpr double kRoundingLateralEpsilonNm = 0.01;
// An exactly abeam mark is not ahead. Great-circle bearing at a short radius
// can leave a sub-centimetre positive along-track component, so compare the
// along-track distance against the same fixed lateral epsilon.
constexpr double kRoundingAbeamEpsilonNm = 0.01;
constexpr double kRoundingBearingEpsilonDegrees = 1e-9;
constexpr std::size_t kMaximumRoundingTargets = 8;

Vector2 unitVector(double bearingDegrees) {
  return speedDirectionToVector(1.0, bearingDegrees);
}

double cross(const Vector2& a, const Vector2& b) {
  return a.eastKnots * b.northKnots - a.northKnots * b.eastKnots;
}

double dot(const Vector2& a, const Vector2& b) {
  return a.eastKnots * b.eastKnots + a.northKnots * b.northKnots;
}

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

double bearingFromMarkForExit(RoundingSide side, double exitBearingDegrees) {
  // Port means the mark is left of the terminal course and the course is the
  // clockwise tangent. Starboard is the counter-clockwise tangent.
  return normalizeHeading(exitBearingDegrees +
                          (side == RoundingSide::Port ? 90.0 : -90.0));
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
  if (!std::isfinite(rounding.radiusNm) || rounding.radiusNm <= 0.0)
    return false;
  return validExitRange(rounding);
}

bool roundingGeometricallyImpossible(const RoundingConstraint& rounding) {
  // The predicate accepts an endpoint up to radius + distance epsilon from the
  // mark, so the largest possible lateral separation is that distance. Below
  // the lateral epsilon no non-ambiguous side exists.
  return rounding.radiusNm + kRoundingDistanceEpsilonNm <
         kRoundingLateralEpsilonNm;
}

std::string RoundingMeasurement::failureCode() const {
  switch (failure) {
    case RoundingFailure::None:
      return "none";
    case RoundingFailure::CoincidentEndpoint:
      return "coincident_endpoint";
    case RoundingFailure::DistanceOutOfRange:
      return "distance_out_of_range";
    case RoundingFailure::MarkAhead:
      return "side_unreachable";
    case RoundingFailure::SideAmbiguous:
      return "side_ambiguous";
    case RoundingFailure::WrongSide:
      return "side_unreachable";
    case RoundingFailure::ExitBearingOutOfRange:
      return "exit_bearing_unreachable";
    case RoundingFailure::NotEvaluated:
      return "not_evaluated";
  }
  return "not_evaluated";
}

RoundingMeasurement measureRounding(const RoundingConstraint& rounding,
                                    GeoPoint endpoint,
                                    double terminalCourseDegrees,
                                    GeoPoint mark) {
  RoundingMeasurement result;
  result.terminalCourseDegrees = normalizeHeading(terminalCourseDegrees);
  result.terminalBearingToMarkDegrees = initialBearingDegrees(endpoint, mark);
  const double distance = distanceNm(endpoint, mark);
  result.closestDistanceNm = distance;

  if (!(rounding.radiusNm > 0.0) || !std::isfinite(rounding.radiusNm)) {
    result.failure = RoundingFailure::DistanceOutOfRange;
    return result;
  }
  if (distance <= kRoundingCoincidentEpsilonNm) {
    result.failure = RoundingFailure::CoincidentEndpoint;
    return result;
  }
  if (distance > rounding.radiusNm + kRoundingDistanceEpsilonNm) {
    result.failure = RoundingFailure::DistanceOutOfRange;
    return result;
  }

  const Vector2 course = unitVector(result.terminalCourseDegrees);
  const Vector2 toMark = unitVector(result.terminalBearingToMarkDegrees);
  if (dot(course, toMark) * distance > kRoundingAbeamEpsilonNm) {
    result.failure = RoundingFailure::MarkAhead;
    return result;
  }

  const double signedLateralNm = cross(course, toMark) * distance;
  const bool port = rounding.side == RoundingSide::Port;
  if ((port && signedLateralNm < 0.0) ||
      (!port && signedLateralNm > 0.0)) {
    result.failure = RoundingFailure::WrongSide;
    return result;
  }
  if (std::abs(signedLateralNm) < kRoundingLateralEpsilonNm) {
    result.failure = RoundingFailure::SideAmbiguous;
    return result;
  }

  if (rounding.exitBearingMinDegrees && rounding.exitBearingMaxDegrees) {
    const double minimum = *rounding.exitBearingMinDegrees;
    const double maximum = *rounding.exitBearingMaxDegrees;
    if (result.terminalCourseDegrees + kRoundingBearingEpsilonDegrees <
            minimum ||
        result.terminalCourseDegrees - kRoundingBearingEpsilonDegrees >
            maximum) {
      result.failure = RoundingFailure::ExitBearingOutOfRange;
      return result;
    }
  }

  result.satisfied = true;
  result.failure = RoundingFailure::None;
  return result;
}

std::vector<GeoPoint> roundingTargets(GeoPoint from, GeoPoint mark,
                                      const RoundingConstraint& rounding) {
  std::vector<GeoPoint> targets;
  const double radius = rounding.radiusNm;
  const double distance = distanceNm(from, mark);
  if (!(radius > 0.0) || !std::isfinite(radius) || distance <= 1e-9 ||
      !validExitRange(rounding))
    return targets;

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
    const std::array<double, 3> exits{
        minimum, normalizeHeading((minimum + maximum) / 2.0), maximum};
    for (const double exit : exits) {
      addTarget(
          targets,
          destinationPoint(mark, bearingFromMarkForExit(rounding.side, exit),
                           radius),
          radius, mark);
    }
  }

  return targets;
}

double roundingProgress(const RoundingConstraint& rounding, GeoPoint endpoint,
                        double terminalCourseDegrees, GeoPoint mark) {
  const RoundingMeasurement measured =
      measureRounding(rounding, endpoint, terminalCourseDegrees, mark);
  if (measured.satisfied) return 0.0;

  const double radius = std::max(1e-6, rounding.radiusNm);
  double progress = 0.0;
  const double distance = distanceNm(endpoint, mark);
  if (distance > radius) progress += distance - radius;
  switch (measured.failure) {
    case RoundingFailure::None:
      break;
    case RoundingFailure::CoincidentEndpoint:
      progress += radius;
      break;
    case RoundingFailure::DistanceOutOfRange:
      break;
    case RoundingFailure::MarkAhead:
      progress += radius;
      break;
    case RoundingFailure::SideAmbiguous:
      progress += radius * 0.5;
      break;
    case RoundingFailure::WrongSide:
      progress += radius;
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
      progress += radius;
      break;
  }
  return progress;
}

}  // namespace supercpn::weather_routing
