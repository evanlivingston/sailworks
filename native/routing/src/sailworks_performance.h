#pragma once

#include <optional>
#include <string>
#include <vector>

#include "supercpn/weather_routing/Providers.h"

namespace sailworks::native_routing {

namespace wr = supercpn::weather_routing;

// Apply Sailworks SafetyPolicy to polar performance in both search and the
// independent validator. The legacy motor fallback substitutes boat speed;
// it does not select the native engine's distinct Motor propulsion mode.
class SailworksPerformance final : public wr::VesselPerformanceModel {
 public:
  SailworksPerformance(wr::VesselConfiguration vessel,
                       std::optional<double> maximumWindKnots,
                       std::optional<double> minimumSailingKnots,
                       double motorKnots, bool windBasedWaveProxy);

  [[nodiscard]] bool valid(std::string* reason = nullptr) const override;
  [[nodiscard]] std::vector<wr::PerformanceCandidate> candidates(
      double trueWindSpeedKnots, double trueWindAngleDegrees,
      const wr::WaveSample& waves, wr::PropulsionMode previousMode,
      wr::Duration previousModeDuration) const override;
  [[nodiscard]] wr::PerformanceCandidate evaluate(
      wr::PropulsionMode mode, wr::ProfileRole role,
      const std::string& profileIdentity, double trueWindSpeedKnots,
      double trueWindAngleDegrees, const wr::WaveSample& waves) const override;

 private:
  [[nodiscard]] wr::PerformanceCandidate apply(
      wr::PerformanceCandidate candidate, double windKnots) const;

  wr::PolarPerformanceModel polar_;
  std::optional<double> maximumWindKnots_;
  std::optional<double> minimumSailingKnots_;
  double motorKnots_;
  bool windBasedWaveProxy_;
};

}  // namespace sailworks::native_routing
