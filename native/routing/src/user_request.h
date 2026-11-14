#pragma once

#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include "boundary_provider.h"
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
  // Aligned one-to-one with the chain marks after the start. Empty means no
  // rounding anywhere. Null entries keep exact-point behaviour for that mark.
  std::vector<std::optional<wr::RoundingConstraint>> roundings;
};

// One frozen user no-go zone: a stable id, a human name and an ordered ring of
// latitude/longitude vertices. The ring is closed implicitly; a repeated first
// vertex at the end is accepted and dropped. The frozen set is ordered and its
// order is part of the request.
struct AvoidZone {
  std::string id;
  std::string name;
  std::vector<wr::GeoPoint> vertices;
};

// Throws std::invalid_argument naming the offending zone when a ring is not a
// simple closed ring of at least three distinct vertices, repeats a vertex,
// contains a non-finite or out-of-range coordinate, or spans more than 180
// degrees of longitude. Duplicate zone ids are rejected.
void validateAvoidZones(const std::vector<AvoidZone>& zones);

// Throws std::invalid_argument unless digest is exactly 64 lowercase hex
// characters. The digest is the canonical content identity of the frozen zone
// set and is carried into the land adapter identity so a zoned calculation
// cannot be confused with a shoreline-only one.
void validateAvoidZoneDigest(const std::string& digest);

// Builds the native engine polygon provider for an already validated,
// non-empty frozen zone set. Throws std::invalid_argument for an empty set or a
// malformed digest.
std::shared_ptr<const wr::PolygonBoundaryProvider> buildAvoidZoneProvider(
    const std::vector<AvoidZone>& zones, const std::string& digest);

// Composes the frozen zone provider with the selected shoreline adapter. The
// zone set is never relaxed: all predicates forbid when either half forbids,
// clearance is the worse of the two, and the known-safe departure exemption
// never applies to a zone. zones must be non-null and come from
// buildAvoidZoneProvider. baseIdentity is the land identity used when there is
// no shoreline adapter.
std::shared_ptr<const wr::LandAndBoundaryProvider> composeAvoidZones(
    std::shared_ptr<const wr::LandAndBoundaryProvider> coastline,
    std::shared_ptr<const wr::PolygonBoundaryProvider> zones,
    const std::string& digest, const std::string& baseIdentity);

// Returns the id of the frozen zone containing point, or nullopt. Uses the
// same planar parity rule as the engine polygon provider so the reported zone
// is the one that actually rejected the point.
std::optional<std::string> containingAvoidZoneId(
    const std::vector<AvoidZone>& zones, wr::GeoPoint point);

// No fixture request, weather, polar, source or fixed six-hour limit enters
// this path. The returned request is suitable for independent replay as well
// as search; the caller must retain the performance and shoreline providers.
wr::RoutingRequest buildUserRequest(const UserSettings& settings,
                                    wr::VesselConfiguration& vessel,
                                    const wr::ParameterCoverage& windCoverage,
                                    const wr::ParameterCoverage& waveCoverage);

}  // namespace sailworks::native_routing
