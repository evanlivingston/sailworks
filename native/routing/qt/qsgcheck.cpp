// Standalone retained-QSG check for the routing overlay.
//
// Runs the item in a real Qt Quick window with the software backend and prints
// a JSON report. It is a separate executable because the application's Qt
// runtime is PySide6's bundled Qt; alignment is achieved by launching the app
// with DYLD_FRAMEWORK_PATH pointing at the same Qt build as this item.

#include <QtCore/QJsonArray>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QThread>
#include <QtGui/QGuiApplication>
#include <QtGui/QImage>
#include <QtQuick/QQuickItem>
#include <QtQuick/QQuickView>
#include <QtQuick/QtQuick>

#include <cmath>
#include <cstdio>

#include "routingretaineditem.h"

namespace {

constexpr int kWidth = 320;
constexpr int kHeight = 240;
constexpr qreal kCenterLat = 48.3;
constexpr qreal kCenterLon = -123.6;
constexpr qreal kZoom = 6.0;

qreal mercatorY(qreal latitude) {
  const qreal clamped = qBound<qreal>(-85.05112878, latitude, 85.05112878);
  return 0.5 - std::asinh(std::tan(qDegreesToRadians(clamped))) / (2.0 * M_PI);
}

qreal mercatorX(qreal longitude) { return (longitude + 180.0) / 360.0; }

QPointF projectPoint(qreal latitude, qreal longitude, qreal centerLat,
                     qreal centerLon, qreal zoom) {
  const qreal world = 256.0 * std::pow(2.0, zoom);
  qreal dx = (longitude - centerLon) * world / 360.0;
  dx -= std::round(dx / world) * world;
  const qreal y = world * mercatorY(latitude);
  const qreal centerY = world * mercatorY(centerLat);
  return {kWidth / 2.0 + dx, kHeight / 2.0 + y - centerY};
}

bool isInk(const QColor &colour) {
  return colour.red() != 255 || colour.green() != 255 || colour.blue() != 255;
}

bool inkNear(const QImage &image, QPointF point, int radius = 4) {
  for (int dy = -radius; dy <= radius; ++dy) {
    for (int dx = -radius; dx <= radius; ++dx) {
      const int px = static_cast<int>(point.x()) + dx;
      const int py = static_cast<int>(point.y()) + dy;
      if (px < 0 || py < 0 || px >= image.width() || py >= image.height()) {
        continue;
      }
      if (isInk(image.pixelColor(px, py))) return true;
    }
  }
  return false;
}

void spin(QGuiApplication &app, int rounds = 12) {
  for (int index = 0; index < rounds; ++index) {
    app.processEvents();
    QThread::msleep(20);
  }
}

}  // namespace

int main(int argc, char **argv) {
  QGuiApplication app(argc, argv);
  QQuickView view;
  view.setResizeMode(QQuickView::SizeRootObjectToView);
  qmlRegisterType<RoutingRetainedItem>("SailworksRouting", 1, 0,
                                       "RoutingRetainedItem");
  view.setSource(QUrl::fromLocalFile(QStringLiteral(SAILWORKS_RETAINED_TEST_QML)));
  view.resize(kWidth, kHeight);
  view.show();
  spin(app);
  if (view.status() != QQuickView::Ready) {
    std::fprintf(stderr, "scene failed to load\n");
    return 2;
  }
  QQuickItem *item = view.rootObject()->findChild<QQuickItem *>("retained");
  if (item == nullptr) {
    std::fprintf(stderr, "retained item not found\n");
    return 2;
  }

  QVariantList segment;
  segment.append(QVariant(QVariantList{48.2, -123.7}));
  segment.append(QVariant(QVariantList{48.4, -123.5}));
  QVariantList segments;
  segments.append(QVariant(segment));
  item->setProperty("segments", segments);
  item->setProperty("color", QColor(0, 0, 204, 200));
  item->setProperty("lineWidth", 3.0);
  item->setProperty("zoom", kZoom);
  item->setProperty("centerLatitude", kCenterLat);
  item->setProperty("centerLongitude", kCenterLon);
  spin(app);
  const QImage first = view.grabWindow();

  const QPointF start = projectPoint(48.2, -123.7, kCenterLat, kCenterLon, kZoom);
  const QPointF end = projectPoint(48.4, -123.5, kCenterLat, kCenterLon, kZoom);
  const QPointF midpoint{(start.x() + end.x()) / 2.0, (start.y() + end.y()) / 2.0};
  const bool inkAtOracle = inkNear(first, midpoint);
  const int rebuildsAfterFirst = item->property("geometryRebuilds").toInt();

  item->setProperty("color", QColor(220, 40, 40, 220));
  spin(app);
  const int rebuildsAfterStyle = item->property("geometryRebuilds").toInt();

  const qreal movedLon = kCenterLon + 0.5;
  item->setProperty("centerLongitude", movedLon);
  spin(app);
  const QImage moved = view.grabWindow();
  const QPointF startMoved = projectPoint(48.2, -123.7, kCenterLat, movedLon, kZoom);
  const QPointF endMoved = projectPoint(48.4, -123.5, kCenterLat, movedLon, kZoom);
  const QPointF movedMidpoint{(startMoved.x() + endMoved.x()) / 2.0,
                              (startMoved.y() + endMoved.y()) / 2.0};
  const int rebuildsAfterPan = item->property("geometryRebuilds").toInt();
  const qreal strokePixels = std::fabs(movedMidpoint.x() - midpoint.x());

  QJsonObject report;
  report["tool"] = "routing_retained_qsg_check";
  report["endpoint"] =
      "Real Qt Quick window, retained QSG segment nodes, software backend. "
      "Fidelity only; NOT presentation FPS.";
  report["fidelity"] = QJsonObject{
      {"ink_at_oracle_midpoint", inkAtOracle},
      {"ink_after_pan", inkNear(moved, movedMidpoint)},
      {"stale_ink_at_old_midpoint", inkNear(moved, midpoint)},
      {"sampled_blue_minus_red",
       first.pixelColor(static_cast<int>(midpoint.x()),
                        static_cast<int>(midpoint.y()))
               .blue() -
           first.pixelColor(static_cast<int>(midpoint.x()),
                            static_cast<int>(midpoint.y()))
               .red()},
  };
  QPoint firstInk(-1, -1);
  for (int y = 0; y < moved.height(); ++y) {
    for (int x = 0; x < moved.width(); ++x) {
      if (isInk(moved.pixelColor(x, y))) {
        if (firstInk.x() < 0) firstInk = QPoint(x, y);
      }
    }
  }
  report["retained"] = QJsonObject{
      {"moved_first_ink", QJsonArray{firstInk.x(), firstInk.y()}},
      {"moved_midpoint", QJsonArray{movedMidpoint.x(), movedMidpoint.y()}},
      {"geometry_rebuilds_after_first_frame", rebuildsAfterFirst},
      {"geometry_rebuilds_after_style", rebuildsAfterStyle},
      {"geometry_rebuilds_after_pan", rebuildsAfterPan},
      {"matrix_updates", item->property("matrixUpdates").toInt()},
      {"pan_pixels", strokePixels},
  };
  report["integration"] =
      "Standalone harness built against Homebrew Qt; the PySide6 app must run "
      "with DYLD_FRAMEWORK_PATH pointing at the same Qt build.";

  const QByteArray json = QJsonDocument(report).toJson(QJsonDocument::Indented);
  std::fwrite(json.constData(), 1, static_cast<size_t>(json.size()), stdout);
  return 0;
}
