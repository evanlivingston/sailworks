#pragma once

#include <memory>
#include <optional>
#include <span>
#include <string>

#include "supercpn/weather_routing/Providers.h"

namespace sailworks::native_routing {

namespace wr = supercpn::weather_routing;

// One land/boundary provider composed from the selected shoreline adapter and
// the frozen native user avoid zones. It is constructed only when at least one
// zone is frozen into the calculation, so a no-zone run keeps the shoreline
// adapter itself and no polygon provider exists.
//
// Zones are exact hard boundaries with no stand-off: the user drew the edge, so
// the configured land safety margin never widens a zone and a leg that grazes
// or crosses the ring is forbidden while a leg that passes close without
// touching it is not.
//
// The coastal known-safe exemption belongs to the departure buffer only. A
// zone is always queried with the point test included, so a leg may not leave,
// enter or start inside a zone merely because the engine declared its start
// point known-safe.
class ComposedLandProvider final : public wr::LandAndBoundaryProvider {
 public:
  ComposedLandProvider(
      std::shared_ptr<const wr::LandAndBoundaryProvider> coastline,
      std::shared_ptr<const wr::PolygonBoundaryProvider> zones,
      std::string identity);

  [[nodiscard]] bool pointForbidden(wr::GeoPoint point) const override;
  [[nodiscard]] bool segmentForbidden(wr::GeoPoint start, wr::GeoPoint end,
                                      double safetyMarginNm) const override;
  [[nodiscard]] bool segmentFromKnownSafeForbidden(
      wr::GeoPoint start, wr::GeoPoint end,
      double safetyMarginNm) const override;
  [[nodiscard]] bool segmentForbiddenAt(wr::GeoPoint start, wr::GeoPoint end,
                                        wr::TimePoint time,
                                        double safetyMarginNm) const override;
  [[nodiscard]] bool segmentFromKnownSafeForbiddenAt(
      wr::GeoPoint start, wr::GeoPoint end, wr::TimePoint time,
      double safetyMarginNm) const override;
  [[nodiscard]] bool validationSegmentFromKnownSafeForbiddenAt(
      wr::GeoPoint start, wr::GeoPoint end, wr::TimePoint time,
      double safetyMarginNm) const override;
  void prepareValidationRoute(std::span<const wr::RouteLeg> legs,
                              double safetyMarginNm) const override;
  [[nodiscard]] bool supportsLandRejectionGuidance() const override;
  [[nodiscard]] double distanceToForbiddenNm(wr::GeoPoint point) const override;
  [[nodiscard]] std::optional<double> depthMetres(
      wr::GeoPoint point) const override;
  [[nodiscard]] std::string identity() const override { return identity_; }

  // Exposed for provenance and tests; never null.
  [[nodiscard]] const wr::PolygonBoundaryProvider& zones() const {
    return *zones_;
  }

 private:
  // The employment state carried by the provider. Zones never take a margin.
  [[nodiscard]] bool zoneForbidden(wr::GeoPoint start,
                                   wr::GeoPoint end) const {
    return zones_->segmentForbidden(start, end, 0.0);
  }

  std::shared_ptr<const wr::LandAndBoundaryProvider> coastline_;
  std::shared_ptr<const wr::PolygonBoundaryProvider> zones_;
  std::string identity_;
};

}  // namespace sailworks::native_routing
