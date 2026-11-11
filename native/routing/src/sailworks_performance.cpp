#include "sailworks_performance.h"

#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <utility>

namespace sailworks::native_routing {

SailworksPerformance::SailworksPerformance(
    wr::VesselConfiguration vessel, std::optional<double> maximumWindKnots,
    std::optional<double> minimumSailingKnots, double motorKnots,
    bool windBasedWaveProxy)
    : polar_(std::move(vessel)),
      maximumWindKnots_(maximumWindKnots),
      minimumSailingKnots_(minimumSailingKnots),
      motorKnots_(motorKnots),
      windBasedWaveProxy_(windBasedWaveProxy) {
  for (const auto value : {maximumWindKnots_, minimumSailingKnots_,
                           std::optional<double>{motorKnots_}}) {
    if (value && (!std::isfinite(*value) || *value <= 0.0))
      throw std::invalid_argument("Sailworks safety speeds must be positive");
  }
}

bool SailworksPerformance::valid(std::string* reason) const {
  return polar_.valid(reason);
}

wr::PerformanceCandidate SailworksPerformance::apply(
    wr::PerformanceCandidate candidate, double windKnots) const {
  if (candidate.mode != wr::PropulsionMode::Sail ||
      (maximumWindKnots_ && windKnots > *maximumWindKnots_)) {
    candidate.valid = false;
    candidate.speedThroughWaterKnots = 0.0;
    return candidate;
  }
  double speed = candidate.speedThroughWaterKnots;
  if (windBasedWaveProxy_ && windKnots > 20.0) {
    const double excess = std::clamp((windKnots - 20.0) / 20.0, 0.0, 1.0);
    speed *= 1.0 - 0.25 * excess;
  }
  if (minimumSailingKnots_ && speed < *minimumSailingKnots_)
    speed = motorKnots_;
  candidate.speedThroughWaterKnots = speed;
  candidate.valid = std::isfinite(speed) && speed > 0.0;
  return candidate;
}

std::vector<wr::PerformanceCandidate> SailworksPerformance::candidates(
    double windKnots, double windAngle, const wr::WaveSample& waves,
    wr::PropulsionMode previousMode, wr::Duration previousModeDuration) const {
  if (maximumWindKnots_ && windKnots > *maximumWindKnots_) return {};
  auto candidates = polar_.candidates(windKnots, windAngle, waves, previousMode,
                                      previousModeDuration);
  if (candidates.empty() && minimumSailingKnots_) {
    // The native polar drops zero-speed sail candidates before a policy can
    // substitute the legacy motor speed. Evaluate the selected sail row once.
    candidates.push_back(polar_.evaluate(wr::PropulsionMode::Sail,
                                         wr::ProfileRole::SailOnly, {}, windKnots,
                                         windAngle, waves));
  }
  for (auto& candidate : candidates) candidate = apply(candidate, windKnots);
  candidates.erase(std::remove_if(candidates.begin(), candidates.end(),
                                  [](const auto& value) { return !value.valid; }),
                   candidates.end());
  return candidates;
}

wr::PerformanceCandidate SailworksPerformance::evaluate(
    wr::PropulsionMode mode, wr::ProfileRole role,
    const std::string& profileIdentity, double windKnots, double windAngle,
    const wr::WaveSample& waves) const {
  return apply(polar_.evaluate(mode, role, profileIdentity, windKnots, windAngle,
                               waves),
               windKnots);
}

}  // namespace sailworks::native_routing
