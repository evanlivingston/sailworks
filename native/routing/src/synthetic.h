#pragma once

#include <array>
#include <cstdint>
#include <memory>
#include <string>

#include "supercpn/weather_routing/Engine.h"
#include "supercpn/weather_routing/Providers.h"

namespace sailworks::native_routing {

namespace wr = supercpn::weather_routing;

// Frozen synthetic fixture identity (test/native_routing_fixtures.py).
inline constexpr const char* kFixtureId = "native-routing-synthetic-v1";
inline constexpr int kFixtureRevision = 1;
inline constexpr double kKnotsPerMetreSecond = 3600.0 / 1852.0;
inline constexpr double kGridLon0 = -124.0;
inline constexpr double kGridLat0 = 48.0;
inline constexpr double kGridDx = 0.5;
inline constexpr double kGridDy = 0.5;
inline constexpr int kGridNx = 5;
inline constexpr int kGridNy = 5;
inline constexpr std::int64_t kReferenceEpochSeconds = 1767225600; // 2026-01-01Z
inline constexpr int kLastValidHour = 6;
// Frozen contract polar angles (test/native_routing_fixtures.py POLAR_TWA).
// There is deliberately no 0-degree row; the engine clamps lower angles to
// the 30-degree row.
inline constexpr std::array<double, 6> kPolarTwaDegrees{
    30.0, 60.0, 90.0, 120.0, 150.0, 180.0};

// Engine-independent synthetic field used by the bridge and its selftest.
// It implements the frozen analytic formulas, not a reimplementation of the
// production sampler.
class FixtureWeatherProvider final : public wr::WeatherProvider {
 public:
  [[nodiscard]] wr::ParameterCoverage windCoverage() const override;
  [[nodiscard]] wr::ParameterCoverage currentCoverage() const override;
  [[nodiscard]] wr::ParameterCoverage waveCoverage() const override;
  [[nodiscard]] wr::WindSample wind(wr::GeoPoint position,
                                    wr::TimePoint time) const override;
  [[nodiscard]] wr::CurrentSample current(wr::GeoPoint position,
                                           wr::TimePoint time) const override;
  [[nodiscard]] wr::WaveSample waves(wr::GeoPoint position,
                                     wr::TimePoint time) const override;
  [[nodiscard]] std::string identity() const override {
    return "native-routing-fixture-weather";
  }

  struct Observables {
    double windU{};
    double windV{};
    double pressurePa{};
    double currentEast{};
    double currentNorth{};
  };

  [[nodiscard]] bool covered(wr::GeoPoint position,
                             wr::TimePoint time) const;
  [[nodiscard]] Observables sample(wr::GeoPoint position,
                                   wr::TimePoint time) const;
  [[nodiscard]] wr::ParameterCoverage coverage(const char* identity) const;
};

struct SyntheticRun {
  std::string engine;
  wr::RoutingRequest request;
  wr::RoutingEnvironment environment;
  wr::RoutingResult result;
  wr::RouteValidationResult independentValidation;
  bool accepted{};
};

wr::VesselConfiguration fixtureVesselConfiguration();
wr::RoutingRequest fixtureRequest(bool cancelled = false,
                                  bool invalidDestination = false);
wr::RoutingEnvironment fixtureEnvironment();

SyntheticRun runSynthetic(const std::string& engine);
SyntheticRun runCancelled(const std::string& engine);
SyntheticRun runInvalidDestination();

[[nodiscard]] bool isCompleteStatus(wr::RoutingStatus status);
[[nodiscard]] std::string statusName(wr::RoutingStatus status);
[[nodiscard]] std::string solverPathName(wr::SolverPath path);
[[nodiscard]] std::string routeDigest(const wr::RoutingResult& result);

// Fixture observables reported exactly as the C++ provider samples them.
struct FixtureObservables {
  wr::WindSample windSouthWestHour0;
  wr::WindSample windNorthEastHour0;
  wr::WindSample windSouthWestHour1;
  double pressureSouthWestHour0Pa{};
  double pressureNorthEastHour0Pa{};
  wr::CurrentSample currentSouthWestHour0;
  wr::CurrentSample currentNorthEastHour0;
  wr::CurrentSample currentNorthEastHour3;
  wr::CurrentSample currentNorthEastHour6;
  double polarSpeed60At8{};
  double polarSpeed90At12{};
  double polarSpeed180At16{};
};
FixtureObservables fixtureObservables();

}  // namespace sailworks::native_routing
