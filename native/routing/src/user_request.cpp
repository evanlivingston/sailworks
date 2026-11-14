#include "user_request.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <set>
#include <stdexcept>
#include <utility>

#include "boundary_provider.h"
#include "supercpn/weather_routing/Engine.h"
#include "supercpn/weather_routing/Rounding.h"

namespace sailworks::native_routing {
namespace {

void checkPoint(wr::GeoPoint point) {
  if (!std::isfinite(point.latitude) || !std::isfinite(point.longitude) ||
      std::abs(point.latitude) > 90.0 || std::abs(point.longitude) > 180.0)
    throw std::invalid_argument("user routing coordinate is invalid");
}

constexpr double kVertexEpsilon = 1e-9;  // ~0.1 mm of latitude
constexpr double kCollinearEpsilon = 1e-12;

std::string zoneLabel(const AvoidZone& zone) {
  if (zone.name.empty()) return "zone '" + zone.id + "'";
  return "zone '" + zone.id + "' (" + zone.name + ")";
}

bool sameVertex(wr::GeoPoint a, wr::GeoPoint b) {
  return std::abs(a.latitude - b.latitude) <= kVertexEpsilon &&
         std::abs(a.longitude - b.longitude) <= kVertexEpsilon;
}

// Planar orientation in degrees. Validation only needs topology; the engine
// provider does the geographic work.
double orientation(wr::GeoPoint a, wr::GeoPoint b, wr::GeoPoint c) {
  return (b.longitude - a.longitude) * (c.latitude - a.latitude) -
         (b.latitude - a.latitude) * (c.longitude - a.longitude);
}

bool onSegment(wr::GeoPoint a, wr::GeoPoint b, wr::GeoPoint point) {
  return point.latitude >= std::min(a.latitude, b.latitude) - kVertexEpsilon &&
         point.latitude <= std::max(a.latitude, b.latitude) + kVertexEpsilon &&
         point.longitude >= std::min(a.longitude, b.longitude) - kVertexEpsilon &&
         point.longitude <= std::max(a.longitude, b.longitude) + kVertexEpsilon;
}

bool segmentsIntersect(wr::GeoPoint a, wr::GeoPoint b, wr::GeoPoint c,
                       wr::GeoPoint d) {
  const double d1 = orientation(c, d, a);
  const double d2 = orientation(c, d, b);
  const double d3 = orientation(a, b, c);
  const double d4 = orientation(a, b, d);
  const bool crosses = ((d1 > kCollinearEpsilon && d2 < -kCollinearEpsilon) ||
                        (d1 < -kCollinearEpsilon && d2 > kCollinearEpsilon)) &&
                       ((d3 > kCollinearEpsilon && d4 < -kCollinearEpsilon) ||
                        (d3 < -kCollinearEpsilon && d4 > kCollinearEpsilon));
  if (crosses) return true;
  // Touching counts: a ring that merely grazes or repeats a vertex is not a
  // simple ring, so it cannot define an unambiguous exclusion.
  return (std::abs(d1) <= kCollinearEpsilon && onSegment(c, d, a)) ||
         (std::abs(d2) <= kCollinearEpsilon && onSegment(c, d, b)) ||
         (std::abs(d3) <= kCollinearEpsilon && onSegment(a, b, c)) ||
         (std::abs(d4) <= kCollinearEpsilon && onSegment(a, b, d));
}

// Drops an explicit closing vertex. The engine closes every ring implicitly.
std::vector<wr::GeoPoint> closedRing(const AvoidZone& zone) {
  std::vector<wr::GeoPoint> ring = zone.vertices;
  if (ring.size() >= 2 && sameVertex(ring.front(), ring.back()))
    ring.pop_back();
  return ring;
}

void checkRing(const AvoidZone& zone) {
  if (zone.id.empty())
    throw std::invalid_argument("avoid zone id must be non-empty");
  for (const wr::GeoPoint vertex : zone.vertices) {
    if (!std::isfinite(vertex.latitude) ||
        !std::isfinite(vertex.longitude) ||
        std::abs(vertex.latitude) > 90.0 ||
        std::abs(vertex.longitude) > 180.0)
      throw std::invalid_argument(zoneLabel(zone) +
                                  " has a non-finite or out-of-range vertex");
  }
  const std::vector<wr::GeoPoint> ring = closedRing(zone);
  if (ring.size() < 3)
    throw std::invalid_argument(zoneLabel(zone) +
                                " needs at least three distinct vertices");
  double west = ring.front().longitude;
  double east = west;
  for (std::size_t index = 0; index < ring.size(); ++index) {
    const wr::GeoPoint vertex = ring[index];
    const wr::GeoPoint next = ring[(index + 1) % ring.size()];
    if (sameVertex(vertex, next))
      throw std::invalid_argument(zoneLabel(zone) +
                                  " repeats a vertex or has a zero-length edge");
    west = std::min(west, vertex.longitude);
    east = std::max(east, vertex.longitude);
  }
  // A ring crossing the antimeridian would need a split; refuse rather than
  // guess a wrap direction.
  if (east - west > 180.0)
    throw std::invalid_argument(zoneLabel(zone) +
                                " spans more than 180 degrees of longitude; "
                                "split an antimeridian ring into two zones");
  for (std::size_t first = 0; first < ring.size(); ++first) {
    const wr::GeoPoint a = ring[first];
    const wr::GeoPoint b = ring[(first + 1) % ring.size()];
    for (std::size_t second = first + 1; second < ring.size(); ++second) {
      const bool adjacent =
          second == first + 1 || (first == 0 && second + 1 == ring.size());
      if (adjacent) continue;
      if (segmentsIntersect(a, b, ring[second],
                            ring[(second + 1) % ring.size()]))
        throw std::invalid_argument(zoneLabel(zone) + " is self-intersecting");
    }
  }
}

}  // namespace

void validateAvoidZones(const std::vector<AvoidZone>& zones) {
  std::set<std::string> ids;
  for (const AvoidZone& zone : zones) {
    checkRing(zone);
    if (!ids.insert(zone.id).second)
      throw std::invalid_argument("duplicate avoid zone id '" + zone.id + "'");
  }
}

void validateAvoidZoneDigest(const std::string& digest) {
  if (digest.size() != 64)
    throw std::invalid_argument(
        "avoid_zones_digest must be 64 lowercase hex characters");
  for (const char digit : digest)
    if (!((digit >= '0' && digit <= '9') || (digit >= 'a' && digit <= 'f')))
      throw std::invalid_argument(
          "avoid_zones_digest must be 64 lowercase hex characters");
}

std::shared_ptr<const wr::PolygonBoundaryProvider> buildAvoidZoneProvider(
    const std::vector<AvoidZone>& zones, const std::string& digest) {
  if (zones.empty())
    throw std::invalid_argument("avoid zone provider needs at least one zone");
  validateAvoidZones(zones);
  validateAvoidZoneDigest(digest);
  std::vector<std::vector<wr::GeoPoint>> polygons;
  polygons.reserve(zones.size());
  for (const AvoidZone& zone : zones) polygons.push_back(closedRing(zone));
  return std::make_shared<wr::PolygonBoundaryProvider>(
      std::move(polygons), "user-avoid-zones:" + digest);
}

std::shared_ptr<const wr::LandAndBoundaryProvider> composeAvoidZones(
    std::shared_ptr<const wr::LandAndBoundaryProvider> coastline,
    std::shared_ptr<const wr::PolygonBoundaryProvider> zones,
    const std::string& digest, const std::string& baseIdentity) {
  if (!zones)
    throw std::invalid_argument("avoid zone composition needs a zone provider");
  validateAvoidZoneDigest(digest);
  const std::string landIdentity =
      coastline ? coastline->identity() : baseIdentity;
  return std::make_shared<ComposedLandProvider>(
      std::move(coastline), std::move(zones),
      landIdentity + "+avoid-zones:" + digest);
}

std::optional<std::string> containingAvoidZoneId(
    const std::vector<AvoidZone>& zones, wr::GeoPoint point) {
  for (const AvoidZone& zone : zones) {
    const std::vector<wr::GeoPoint> ring = closedRing(zone);
    if (ring.size() < 3) continue;
    bool inside = false;
    for (std::size_t index = 0; index < ring.size(); ++index) {
      const wr::GeoPoint edgeStart = ring[index];
      const wr::GeoPoint edgeEnd = ring[(index + 1) % ring.size()];
      if ((edgeStart.latitude > point.latitude) ==
          (edgeEnd.latitude > point.latitude))
        continue;
      // Same parity rule as PolygonBoundaryProvider::pointForbidden.
      const double startLongitude =
          wr::normalizeLongitude(edgeStart.longitude - point.longitude);
      const double endLongitude =
          wr::normalizeLongitude(edgeEnd.longitude - point.longitude);
      if (0.0 < (endLongitude - startLongitude) *
                        (point.latitude - edgeStart.latitude) /
                        (edgeEnd.latitude - edgeStart.latitude) +
                    startLongitude)
        inside = !inside;
    }
    if (inside) return zone.id;
  }
  return std::nullopt;
}

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
  // A single-leg request carries exactly one rounding spec. A multi-leg chain
  // receives its specs per leg in runUserChain instead.
  if (settings.roundings.size() == 1 && settings.roundings.front()) {
    if (!wr::roundingConstraintValid(*settings.roundings.front()))
      throw std::invalid_argument("rounding spec is invalid");
    request.rounding = *settings.roundings.front();
  }
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
