#pragma once

#include <cstdint>
#include <optional>

#include "supercpn/weather_routing/Providers.h"

namespace sailworks::native_routing {

namespace wr = supercpn::weather_routing;

struct UserSettings {
  wr::GeoPoint start;
  wr::GeoPoint destination;
  std::int64_t departureEpochSeconds{};
  bool useCurrents{};
  double polarEfficiency{1.0};
  std::optional<double> maximumWindKnots;
  std::optional<double> minimumSailingKnots;
  double motorKnots{5.0};
  bool windBasedWaveProxy{};
  bool useWaves{};
  double landSafetyMarginNm{};
  bool coastlineChecks{};
};

// No fixture request, weather, polar, source or fixed six-hour limit enters
// this path. The returned request is suitable for independent replay as well
// as search; the caller must retain the performance and shoreline providers.
wr::RoutingRequest buildUserRequest(const UserSettings& settings,
                                    wr::VesselConfiguration& vessel,
                                    const wr::ParameterCoverage& windCoverage,
                                    const wr::ParameterCoverage& waveCoverage);

}  // namespace sailworks::native_routing
