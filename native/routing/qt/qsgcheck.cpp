// Standalone retained-QSG check for the routing overlay.
//
// Runs the item in a real Qt Quick window with the software backend and prints
// a JSON report. It is a separate executable because the application's Qt
// runtime is PySide6's bundled Qt; alignment is achieved by launching the app
// with DYLD_FRAMEWORK_PATH pointing at the same Qt build as this item.

#include <QtCore/QCoreApplication>
#include <QtCore/QJsonArray>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QThread>
#include <QtGui/QGuiApplication>
#include <QtGui/QImage>
#include <QtGui/QMouseEvent>
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
                     qreal centerLon, qreal zoom,
                     qreal width = kWidth, qreal height = kHeight) {
  const qreal world = 256.0 * std::pow(2.0, zoom);
  qreal dx = (longitude - centerLon) * world / 360.0;
  dx -= std::round(dx / world) * world;
  const qreal y = world * mercatorY(latitude);
  const qreal centerY = world * mercatorY(centerLat);
  return {width / 2.0 + dx, height / 2.0 + y - centerY};
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
  QVariantMap polyline;
  polyline["id"] = QStringLiteral("trace-0");
  polyline["points"] = segment;
  QVariantList polylines;
  polylines.append(polyline);
  item->setProperty("polylines", polylines);
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

  // Highlight must survive a geometry rebuild (zoom recreates the quads).
  item->setProperty("hoverId", QStringLiteral("trace-0"));
  spin(app);
  item->setProperty("zoom", kZoom + 0.5);
  spin(app);
  const QImage zoomed = view.grabWindow();
  const QPointF zoomedStart =
      projectPoint(48.2, -123.7, kCenterLat, movedLon, kZoom + 0.5);
  const QPointF zoomedEnd =
      projectPoint(48.4, -123.5, kCenterLat, movedLon, kZoom + 0.5);
  const QPointF zoomedMidpoint{(zoomedStart.x() + zoomedEnd.x()) / 2.0,
                               (zoomedStart.y() + zoomedEnd.y()) / 2.0};
  bool highlightSurvivesZoom = false;
  for (int dy = -4; dy <= 4 && !highlightSurvivesZoom; ++dy) {
    for (int dx = -4; dx <= 4; ++dx) {
      const int px = static_cast<int>(zoomedMidpoint.x()) + dx;
      const int py = static_cast<int>(zoomedMidpoint.y()) + dy;
      if (px < 0 || py < 0 || px >= zoomed.width() || py >= zoomed.height()) {
        continue;
      }
      const QColor c = zoomed.pixelColor(px, py);
      if (c.red() > c.blue() + 40) {
        highlightSurvivesZoom = true;
        break;
      }
    }
  }
  const qreal strokePixels = std::fabs(movedMidpoint.x() - midpoint.x());

  // Resize: the camera translation depends on width()/height(), so the
  // overlay must follow the chart without rebuilding segment geometry.
  const int rebuildsBeforeResize = item->property("geometryRebuilds").toInt();
  const int matricesBeforeResize = item->property("matrixUpdates").toInt();
  // Resize the overlay host (the application resizes its sidecar), not the
  // window, so the scene graph survives and only the camera translation moves.
  constexpr qreal kResizedWidth = 240.0;
  constexpr qreal kResizedHeight = 180.0;
  view.rootObject()->setSize(QSizeF(kResizedWidth, kResizedHeight));
  spin(app);
  const QImage resized = view.grabWindow();
  const QPointF resizedStart = projectPoint(
      48.2, -123.7, kCenterLat, movedLon, kZoom + 0.5, kResizedWidth,
      kResizedHeight);
  const QPointF resizedEnd = projectPoint(
      48.4, -123.5, kCenterLat, movedLon, kZoom + 0.5, kResizedWidth,
      kResizedHeight);
  const QPointF resizedMidpoint{(resizedStart.x() + resizedEnd.x()) / 2.0,
                                (resizedStart.y() + resizedEnd.y()) / 2.0};
  const int rebuildsAfterResize = item->property("geometryRebuilds").toInt();
  const int matricesAfterResize = item->property("matrixUpdates").toInt();

  // Mouse: the geometry-only item must not take the press a pan needs. The
  // MouseArea below the overlay stands in for the viewport's pan handler.
  QQuickItem *underlay =
      view.rootObject()->findChild<QQuickItem *>("underlay");
  const QPointF pressPoint(kResizedWidth / 2.0, kResizedHeight / 2.0);
  const QPointF globalPoint = view.mapToGlobal(pressPoint.toPoint());
  QMouseEvent hover(QEvent::MouseMove, pressPoint, globalPoint, Qt::NoButton,
                    Qt::NoButton, Qt::NoModifier);
  QCoreApplication::sendEvent(&view, &hover);
  spin(app);
  QMouseEvent press(QEvent::MouseButtonPress, pressPoint, globalPoint,
                    Qt::LeftButton, Qt::LeftButton, Qt::NoModifier);
  QCoreApplication::sendEvent(&view, &press);
  spin(app);
  QMouseEvent release(QEvent::MouseButtonRelease, pressPoint, globalPoint,
                      Qt::LeftButton, Qt::NoButton, Qt::NoModifier);
  QCoreApplication::sendEvent(&view, &release);
  spin(app);
  const int acceptedButtons =
      static_cast<int>(item->acceptedMouseButtons());
  const bool underlayReceivedPress =
      underlay != nullptr && underlay->property("presses").toInt() > 0;
  const bool underlayReceivedHover =
      underlay != nullptr && underlay->property("hovers").toInt() > 0;

  QJsonObject report;
  report["tool"] = "routing_retained_qsg_check";
  report["endpoint"] =
      "Real Qt Quick window, retained QSG segment nodes, software backend. "
      "Fidelity only; NOT presentation FPS.";
  report["fidelity"] = QJsonObject{
      {"ink_at_oracle_midpoint", inkAtOracle},
      {"ink_after_pan", inkNear(moved, movedMidpoint)},
      {"stale_ink_at_old_midpoint", inkNear(moved, midpoint)},
      {"ink_after_resize", inkNear(resized, resizedMidpoint)},
      {"stale_ink_at_pre_resize_midpoint", inkNear(resized, zoomedMidpoint)},
      {"highlight_survives_zoom", highlightSurvivesZoom},
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
      {"geometry_rebuilds_before_resize", rebuildsBeforeResize},
      {"geometry_rebuilds_after_resize", rebuildsAfterResize},
      {"matrix_updates_before_resize", matricesBeforeResize},
      {"matrix_updates_after_resize", matricesAfterResize},
      {"accepted_mouse_buttons", acceptedButtons},
      {"underlay_received_press", underlayReceivedPress},
      {"underlay_received_hover", underlayReceivedHover},
      {"matrix_updates", item->property("matrixUpdates").toInt()},
      {"pan_pixels", strokePixels},
  };
  report["integration"] =
      "Standalone harness built against a Qt C++ SDK. The application loads the "
      "module after tools/build_routing_qsg_module.py relinks it to PySide6's "
      "frameworks; no DYLD_FRAMEWORK_PATH is required.";

  const QByteArray json = QJsonDocument(report).toJson(QJsonDocument::Indented);
  std::fwrite(json.constData(), 1, static_cast<size_t>(json.size()), stdout);
  return 0;
}
