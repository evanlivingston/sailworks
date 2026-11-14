#include "boundary_provider.h"

#include <algorithm>
#include <utility>

namespace sailworks::native_routing {

ComposedLandProvider::ComposedLandProvider(
    std::shared_ptr<const wr::LandAndBoundaryProvider> coastline,
    std::shared_ptr<const wr::PolygonBoundaryProvider> zones,
    std::string identity)
    : coastline_(std::move(coastline)),
      zones_(std::move(zones)),
      identity_(std::move(identity)) {}

bool ComposedLandProvider::pointForbidden(wr::GeoPoint point) const {
  // A point is forbidden when either half forbids it. Forbidding is the union,
  // so a zone never relaxes a shoreline rejection and vice versa.
  if (zones_->pointForbidden(point)) return true;
  return coastline_ && coastline_->pointForbidden(point);
}

bool ComposedLandProvider::segmentForbidden(wr::GeoPoint start, wr::GeoPoint end,
                                            double safetyMarginNm) const {
  if (zones_->segmentForbidden(start, end, 0.0)) return true;
  return coastline_ && coastline_->segmentForbidden(start, end, safetyMarginNm);
}

bool ComposedLandProvider::segmentFromKnownSafeForbidden(
    wr::GeoPoint start, wr::GeoPoint end, double safetyMarginNm) const {
  // Zones keep the full point test: the exemption belongs to the shoreline
  // departure buffer, not to drawn geometry.
  return zoneForbidden(start, end) ||
         (coastline_ &&
          coastline_->segmentFromKnownSafeForbidden(start, end,
                                                    safetyMarginNm));
}

bool ComposedLandProvider::segmentForbiddenAt(wr::GeoPoint start,
                                              wr::GeoPoint end,
                                              wr::TimePoint time,
                                              double safetyMarginNm) const {
  if (zones_->segmentForbidden(start, end, 0.0)) return true;
  return coastline_ &&
         coastline_->segmentForbiddenAt(start, end, time, safetyMarginNm);
}

bool ComposedLandProvider::segmentFromKnownSafeForbiddenAt(
    wr::GeoPoint start, wr::GeoPoint end, wr::TimePoint time,
    double safetyMarginNm) const {
  return zoneForbidden(start, end) ||
         (coastline_ &&
          coastline_->segmentFromKnownSafeForbiddenAt(start, end, time,
                                                      safetyMarginNm));
}

bool ComposedLandProvider::validationSegmentFromKnownSafeForbiddenAt(
    wr::GeoPoint start, wr::GeoPoint end, wr::TimePoint time,
    double safetyMarginNm) const {
  // Independent replay must judge the same zone geometry. Only the shoreline
  // half may use a host's stricter authoritative chart.
  return zoneForbidden(start, end) ||
         (coastline_ && coastline_->validationSegmentFromKnownSafeForbiddenAt(
                            start, end, time, safetyMarginNm));
}

void ComposedLandProvider::prepareValidationRoute(
    std::span<const wr::RouteLeg> legs, double safetyMarginNm) const {
  // Scheduling hint only; the zone half needs no preparation.
  if (coastline_)
    coastline_->prepareValidationRoute(legs, safetyMarginNm);
}

bool ComposedLandProvider::supportsLandRejectionGuidance() const {
  // Zones are hard polygons: guidance may steer around them exactly like a
  // coastline, so the composite keeps the shoreline's answer (and the engine
  // default when there is no shoreline).
  return !coastline_ || coastline_->supportsLandRejectionGuidance();
}

double ComposedLandProvider::distanceToForbiddenNm(wr::GeoPoint point) const {
  // Worse of the two clearances, reported once.
  const double zoneDistance = zones_->distanceToForbiddenNm(point);
  if (!coastline_) return zoneDistance;
  return std::min(zoneDistance,
                  coastline_->distanceToForbiddenNm(point));
}

std::optional<double> ComposedLandProvider::depthMetres(
    wr::GeoPoint point) const {
  // A polygon zone carries no depth information; a depth constraint keeps the
  // shoreline's answer.
  if (!coastline_) return std::nullopt;
  return coastline_->depthMetres(point);
}

}  // namespace sailworks::native_routing
