#include "user_request.h"

#include <chrono>
#include <cmath>
#include <stdexcept>

namespace sailworks::native_routing {
namespace {

void checkPoint(wr::GeoPoint point) {
  if (!std::isfinite(point.latitude) || !std::isfinite(point.longitude) ||
      std::abs(point.latitude) > 90.0 || std::abs(point.longitude) > 180.0)
    throw std::invalid_argument("user routing coordinate is invalid");
}

}  // namespace

wr::RoutingRequest buildUserRequest(const UserSettings& settings,
                                    wr::VesselConfiguration& vessel,
                                    const wr::ParameterCoverage& windCoverage,
                                    const wr::ParameterCoverage& waveCoverage) {
  checkPoint(settings.start);
  checkPoint(settings.destination);
  if (settings.start.latitude == settings.destination.latitude &&
      settings.start.longitude == settings.destination.longitude)
    throw std::invalid_argument("user routing start equals destination");
  if (!std::isfinite(settings.polarEfficiency) ||
      settings.polarEfficiency <= 0.0)
    throw std::invalid_argument("polar efficiency must be positive");
  if (!windCoverage.available || !windCoverage.begins || !windCoverage.ends)
    throw std::invalid_argument("selected wind coverage is unavailable");

  wr::RoutingRequest request;
  request.start = settings.start;
  request.destination = settings.destination;
  request.departure = wr::TimePoint{wr::Duration{settings.departureEpochSeconds}};
  if (request.departure < *windCoverage.begins ||
      request.departure >= *windCoverage.ends)
    throw std::invalid_argument("selected wind does not cover departure");
  request.vessel = vessel;
  for (auto& profile : request.vessel.profiles)
    profile.efficiency = settings.polarEfficiency;
  vessel = request.vessel;
  request.environment.useWind = true;
  request.environment.useCurrent = settings.useCurrents;
  request.environment.useWaves = settings.useWaves;
  if (settings.useWaves) {
    if (!waveCoverage.available || !waveCoverage.begins || !waveCoverage.ends)
      throw std::invalid_argument("selected wave coverage is unavailable");
    // A calculation that asks for real wave data must fail when coverage is
    // missing. It never falls back to the wind-based proxy silently.
    request.environment.missingWaves =
        wr::MissingWavePolicy::RequireExplicitAcknowledgement;
    request.environment.missingWavesAcknowledged = false;
  }
  request.environment.missingCurrent = wr::MissingCurrentPolicy::Disallow;
  request.objective.kind = wr::ObjectiveKind::FastestUnderSafetyLimits;
  if (!std::isfinite(settings.landSafetyMarginNm) ||
      settings.landSafetyMarginNm < 0.0)
    throw std::invalid_argument("land safety margin must be non-negative");
  if (!settings.coastlineChecks && settings.landSafetyMarginNm > 0.0)
    throw std::invalid_argument(
        "land safety margin requires enabled coastline checks");
  request.constraints.landSafetyMarginNm =
      settings.coastlineChecks ? settings.landSafetyMarginNm : 0.0;
  // The wind-based wave proxy acts on performance, not real wave constraints.
  request.limits.maximumRouteDuration =
      *windCoverage.ends - request.departure;
  return request;
}

}  // namespace sailworks::native_routing
