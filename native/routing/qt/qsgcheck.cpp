// Standalone retained-QSG check for the routing overlay.
//
// Runs the item in a real Qt Quick window with the software backend and prints
// a JSON report. It is a separate executable because the application's Qt
// runtime is PySide6's bundled Qt; alignment is achieved by launching the app
// with DYLD_FRAMEWORK_PATH pointing at the same Qt build as this item.

#include <QtCore/QByteArray>
#include <QtCore/QCoreApplication>
#include <QtCore/QDir>
#include <QtCore/QElapsedTimer>
#include <QtCore/QJsonArray>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QMutex>
#include <QtCore/QStringList>
#include <QtCore/QThread>
#include <QtCore/QtMath>
#include <QtCore/QVariantMap>
#include <QtGui/QGuiApplication>
#include <QtGui/QImage>
#include <QtGui/QMouseEvent>
#include <QtQuick/QQuickItem>
#include <QtQuick/QQuickView>
#include <QtQuick/QSGRendererInterface>
#include <QtQuick/QtQuick>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <random>

#include "gpulineitem.h"
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

// --- constant-width GPU line check ---------------------------------------

constexpr int kGpuWidth = 400;
constexpr int kGpuHeight = 300;
constexpr qreal kGpuCenterLat = 48.3;
constexpr qreal kGpuCenterLon = -123.6;

bool isStroke(const QColor &colour) {
  return colour.red() < 240 || colour.green() < 240 || colour.blue() < 240;
}

// Counts stroke pixels in a vertical column crossing a near-horizontal
// segment. The image is in device pixels; coordinates are logical.
int strokePixelsInColumn(const QImage &image, qreal logicalX, qreal logicalY,
                         qreal logicalRadius, qreal devicePixelRatio) {
  const int x = qRound(logicalX * devicePixelRatio);
  const int centre = qRound(logicalY * devicePixelRatio);
  const int radius = qMax(1, qRound(logicalRadius * devicePixelRatio));
  int count = 0;
  for (int y = centre - radius; y <= centre + radius; ++y) {
    if (x < 0 || y < 0 || x >= image.width() || y >= image.height()) continue;
    if (isStroke(image.pixelColor(x, y))) ++count;
  }
  return count;
}

bool strokeNear(const QImage &image, QPointF logicalPoint,
                qreal devicePixelRatio, int logicalRadius = 4) {
  const QPointF device = logicalPoint * devicePixelRatio;
  const int radius = qMax(1, qRound(logicalRadius * devicePixelRatio));
  for (int dy = -radius; dy <= radius; ++dy) {
    for (int dx = -radius; dx <= radius; ++dx) {
      const int px = qRound(device.x()) + dx;
      const int py = qRound(device.y()) + dy;
      if (px < 0 || py < 0 || px >= image.width() || py >= image.height()) {
        continue;
      }
      if (isStroke(image.pixelColor(px, py))) return true;
    }
  }
  return false;
}

// Walks a near-horizontal stroke one device column at a time and reports how
// its ink alternates. `radiusLogical` bounds the column scan so geometry above
// or below the stroke cannot be mistaken for ink.
struct DashStats {
  int inkRuns = 0;
  int gapRuns = 0;
  int inkColumns = 0;
  int sampledColumns = 0;
  int maxColumnPixels = 0;
  qreal inkFraction = 0.0;
};

DashStats dashStatsAlong(const QImage &image, QPointF logicalStart,
                         QPointF logicalEnd, qreal devicePixelRatio,
                         qreal radiusLogical) {
  DashStats stats;
  const int centre = qRound(logicalStart.y() * devicePixelRatio);
  const int radius = qMax(1, qRound(radiusLogical * devicePixelRatio));
  const int first =
      qMax(0, qRound(logicalStart.x() * devicePixelRatio));
  const int last = qMin(image.width() - 1,
                        qRound(logicalEnd.x() * devicePixelRatio));
  bool inInk = false;
  // Only a gap that ink resumes after is a dash gap; a gap at the sample end is
  // the segment's own cap, not a pattern boundary.
  bool gapPending = false;
  for (int x = first; x <= last; ++x) {
    int pixels = 0;
    for (int y = centre - radius; y <= centre + radius; ++y) {
      if (y < 0 || y >= image.height()) continue;
      if (isStroke(image.pixelColor(x, y))) ++pixels;
    }
    const bool ink = pixels > 0;
    if (ink) {
      ++stats.inkColumns;
      stats.maxColumnPixels = qMax(stats.maxColumnPixels, pixels);
    }
    if (ink && !inInk) {
      ++stats.inkRuns;
      if (gapPending) {
        ++stats.gapRuns;
        gapPending = false;
      }
    } else if (!ink && inInk) {
      gapPending = true;
    }
    inInk = ink;
    ++stats.sampledColumns;
  }
  if (stats.sampledColumns > 0) {
    stats.inkFraction =
        qreal(stats.inkColumns) / qreal(stats.sampledColumns);
  }
  return stats;
}

// One packed line record: endpoints, width, RGBA, the dash on/off lengths and
// the dash phase. `fields` selects the layout: 12 is current (48 bytes), 11 is
// the phase-less 44-byte form and 9 the legacy 36-byte solid form.
QByteArray packLineRecord(qreal lon0, qreal lat0, qreal lon1, qreal lat1,
                          qreal width, const QColor &colour, qreal dashOn,
                          qreal dashOff, qreal phase, int fields = 12) {
  QByteArray data(fields * static_cast<int>(sizeof(float)), Qt::Uninitialized);
  auto *values = reinterpret_cast<float *>(data.data());
  values[0] = static_cast<float>(lon0);
  values[1] = static_cast<float>(lat0);
  values[2] = static_cast<float>(lon1);
  values[3] = static_cast<float>(lat1);
  values[4] = static_cast<float>(width);
  values[5] = static_cast<float>(colour.redF());
  values[6] = static_cast<float>(colour.greenF());
  values[7] = static_cast<float>(colour.blueF());
  values[8] = static_cast<float>(colour.alphaF());
  if (fields > 9) {
    values[9] = static_cast<float>(dashOn);
    values[10] = static_cast<float>(dashOff);
  }
  if (fields > 11) {
    values[11] = static_cast<float>(phase);
  }
  return data;
}

// Normalised Web-Mercator world position, the unit `phase` is published in and
// the item's along distance is measured in. Mirrors mercatorX/mercatorY in
// gpulineitem.cpp.
QPointF worldPoint(qreal latitude, qreal longitude) {
  const qreal clamped = qBound<qreal>(-85.05112878, latitude, 85.05112878);
  return QPointF((longitude + 180.0) / 360.0,
                 0.5 - std::asinh(std::tan(qDegreesToRadians(clamped))) /
                           (2.0 * M_PI));
}

QString graphicsApiName(QSGRendererInterface::GraphicsApi api) {
  switch (api) {
    case QSGRendererInterface::Metal:
      return QStringLiteral("metal");
    case QSGRendererInterface::OpenGL:
      return QStringLiteral("opengl");
    case QSGRendererInterface::Vulkan:
      return QStringLiteral("vulkan");
    case QSGRendererInterface::Direct3D11:
      return QStringLiteral("d3d11");
    case QSGRendererInterface::Software:
      return QStringLiteral("software");
    default:
      return QStringLiteral("unknown");
  }
}

// --- bounded GPU line benchmark -----------------------------------------
//
// Generates N deterministic world segments once, builds the single geometry
// once, then drives pan/zoom frames and times them from the window's
// afterRendering signal. Nothing here touches production code paths: the
// segment list goes through the same `segments` property the item already
// exposes, and geometryRebuilds is an existing diagnostic.

// Collects afterRendering stamps. Under the threaded render loop the signal is
// delivered on the render thread, so the connection is direct and the recorder
// is mutex-guarded: the stamp is taken when the frame completes rather than
// when the GUI thread next runs.
struct FrameRecorder {
  void reset() {
    QMutexLocker locker(&mutex);
    stampsNs.clear();
    clock.restart();
  }
  void record() {
    QMutexLocker locker(&mutex);
    stampsNs.append(clock.nsecsElapsed());
  }
  int count() {
    QMutexLocker locker(&mutex);
    return static_cast<int>(stampsNs.size());
  }
  QVector<qint64> take() {
    QMutexLocker locker(&mutex);
    return stampsNs;
  }

  QMutex mutex;
  QVector<qint64> stampsNs;
  QElapsedTimer clock;
};

// Fixed seed and fixed per-index style, so every N renders the same prefix and
// the three runs are comparable.
QVariantList generateBenchSegments(int count) {
  static const QColor kColors[4] = {
      QColor(0, 60, 200, 220), QColor(200, 40, 40, 220),
      QColor(20, 140, 80, 220), QColor(230, 170, 30, 220)};
  static const qreal kWidths[4] = {1.0, 2.0, 3.5, 6.0};

  std::mt19937 rng(0x5A11u);
  std::uniform_real_distribution<double> offset(-4.5, 4.5);
  std::uniform_real_distribution<double> length(0.02, 0.45);
  std::uniform_real_distribution<double> heading(0.0, 6.283185307179586);

  QVariantList segments;
  segments.reserve(count);
  for (int index = 0; index < count; ++index) {
    const int style = index % 4;
    const qreal lat = kGpuCenterLat + offset(rng);
    const qreal lon = kGpuCenterLon + offset(rng);
    const qreal span = length(rng);
    const qreal angle = heading(rng);
    QVariantMap segment;
    segment["points"] = QVariantList{
        QVariantList{lat, lon},
        QVariantList{lat + span * std::sin(angle),
                     lon + span * std::cos(angle)}};
    segment["color"] = kColors[style];
    segment["width"] = kWidths[style];
    segments.append(segment);
  }
  return segments;
}

double percentileMs(QVector<double> samples, double fraction) {
  if (samples.isEmpty()) return 0.0;
  std::sort(samples.begin(), samples.end());
  const int last = static_cast<int>(samples.size()) - 1;
  const int index = qBound(0, static_cast<int>(std::lround(fraction * last)),
                           last);
  return samples[index];
}

}  // namespace

int runRetainedCheck(QGuiApplication &app) {
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

int runGpuLineCheck(QGuiApplication &app) {
  QQuickView view;
  view.setResizeMode(QQuickView::SizeRootObjectToView);
  qmlRegisterType<GpuLineItem>("SailworksRouting", 1, 0, "GpuLineItem");
  view.setSource(QUrl::fromLocalFile(QStringLiteral(SAILWORKS_GPU_LINE_TEST_QML)));
  view.resize(kGpuWidth, kGpuHeight);
  view.show();
  spin(app);
  if (view.status() != QQuickView::Ready || view.rootObject() == nullptr) {
    std::fprintf(stderr, "gpu line scene failed to load\n");
    return 2;
  }
  QQuickItem *item = view.rootObject()->findChild<QQuickItem *>("gpuLine");
  if (item == nullptr) {
    std::fprintf(stderr, "gpu line item not found\n");
    return 2;
  }

  const auto makeSegment = [](qreal lat0, qreal lon0, qreal lat1, qreal lon1,
                              const QColor &colour, qreal width) {
    QVariantMap map;
    map["points"] =
        QVariantList{QVariantList{lat0, lon0}, QVariantList{lat1, lon1}};
    map["color"] = colour;
    map["width"] = width;
    return QVariant::fromValue(map);
  };

  QVariantList segments;
  segments.append(
      makeSegment(48.3, -124.2, 48.3, -123.0, QColor(0, 0, 204), 4.0));
  segments.append(
      makeSegment(47.7, -124.1, 48.0, -123.3, QColor(204, 34, 34), 6.0));
  segments.append(
      makeSegment(48.35, -124.15, 48.15, -124.15, QColor(17, 136, 68), 2.0));

  item->setProperty("segments", segments);
  item->setProperty("zoom", 6.0);
  item->setProperty("centerLatitude", kGpuCenterLat);
  item->setProperty("centerLongitude", kGpuCenterLon);
  spin(app);
  const QImage first = view.grabWindow();
  const qreal dpr = first.width() > 0 ? first.width() / qreal(kGpuWidth) : 1.0;

  // The blue horizontal segment crosses the column at the view centre.
  const QPointF centrePoint = projectPoint(48.3, -123.6, kGpuCenterLat,
                                           kGpuCenterLon, 6.0, kGpuWidth,
                                           kGpuHeight);
  const int widthAt6 =
      strokePixelsInColumn(first, centrePoint.x(), centrePoint.y(), 8.0, dpr);
  const int rebuildsAfterFirst = item->property("geometryRebuilds").toInt();

  item->setProperty("zoom", 9.0);
  spin(app);
  const QImage zoomed = view.grabWindow();
  const QPointF zoomedCentre = projectPoint(48.3, -123.6, kGpuCenterLat,
                                            kGpuCenterLon, 9.0, kGpuWidth,
                                            kGpuHeight);
  const int widthAt9 =
      strokePixelsInColumn(zoomed, zoomedCentre.x(), zoomedCentre.y(), 8.0, dpr);
  const int rebuildsAfterZoom = item->property("geometryRebuilds").toInt();

  // lineWidthScale is a uniform: the stroke doubles with no vertex upload.
  item->setProperty("lineWidthScale", 2.0);
  spin(app);
  const QImage scaled = view.grabWindow();
  const int widthScaled = strokePixelsInColumn(
      scaled, zoomedCentre.x(), zoomedCentre.y(), 8.0, dpr);
  const int rebuildsAfterScale = item->property("geometryRebuilds").toInt();
  item->setProperty("lineWidthScale", 1.0);

  // Pan at the original zoom: the stroke must move and keep its width.
  const qreal pannedLon = kGpuCenterLon + 1.0;
  item->setProperty("zoom", 6.0);
  item->setProperty("centerLongitude", pannedLon);
  spin(app);
  const QImage panned = view.grabWindow();
  const QPointF pannedCentre = projectPoint(48.3, -123.6, kGpuCenterLat,
                                            pannedLon, 6.0, kGpuWidth,
                                            kGpuHeight);
  const QPointF staleCentre = projectPoint(48.3, -123.6, kGpuCenterLat,
                                           kGpuCenterLon, 6.0, kGpuWidth,
                                           kGpuHeight);
  const int widthAfterPan = strokePixelsInColumn(
      panned, pannedCentre.x(), pannedCentre.y(), 8.0, dpr);
  const int rebuildsAfterPan = item->property("geometryRebuilds").toInt();

  // --- dashes -------------------------------------------------------------
  // A single horizontal packed segment exercises the 44-byte record: 6px ink,
  // 4px gap, 5px wide. The pattern is measured along the stroke in logical
  // screen pixels, so both zooms must alternate and keep the same width.
  const qreal dashLat = kGpuCenterLat;
  const qreal dashLon0 = -124.2;
  const qreal dashLon1 = -123.0;
  const qreal dashOn = 6.0;
  const qreal dashOff = 4.0;
  const qreal dashWidth = 5.0;
  // Radius in logical pixels: half the stroke plus slack for the antialias
  // fringe, narrow enough that nothing else in the scene can register.
  const qreal dashRadius = dashWidth / 2.0 + 3.0;
  const auto dashSample = [&](qreal zoom) {
    const QImage image = view.grabWindow();
    return dashStatsAlong(
        image, projectPoint(dashLat, dashLon0, kGpuCenterLat, kGpuCenterLon,
                            zoom, kGpuWidth, kGpuHeight),
        projectPoint(dashLat, dashLon1, kGpuCenterLat, kGpuCenterLon, zoom,
                     kGpuWidth, kGpuHeight),
        dpr, dashRadius);
  };

  item->setProperty("zoom", 6.0);
  item->setProperty("centerLongitude", kGpuCenterLon);
  item->setProperty("lineWidthScale", 1.0);
  item->setProperty("segmentData",
                    packLineRecord(dashLon0, dashLat, dashLon1, dashLat,
                                   dashWidth, QColor(0, 0, 0), dashOn, dashOff,
                                   0.0));
  spin(app);
  const DashStats dashAt6 = dashSample(6.0);

  item->setProperty("zoom", 9.0);
  spin(app);
  const DashStats dashAt9 = dashSample(9.0);

  // Both dash lengths zero must stay a solid stroke, and the legacy 36-byte
  // payload must still render solid with the new vertex layout.
  item->setProperty("zoom", 6.0);
  item->setProperty("segmentData",
                    packLineRecord(dashLon0, dashLat, dashLon1, dashLat,
                                   dashWidth, QColor(0, 0, 0), 0.0, 0.0, 0.0));
  spin(app);
  const DashStats solidAt6 = dashSample(6.0);

  item->setProperty("segmentData",
                    packLineRecord(dashLon0, dashLat, dashLon1, dashLat,
                                   dashWidth, QColor(0, 0, 0), 0.0, 0.0, 0.0,
                                   9));
  spin(app);
  const DashStats legacyAt6 = dashSample(6.0);
  item->setProperty("segmentData", QByteArray());

  // --- continuous dash phase across a chained polyline ---------------------
  // Six short edges joined end to end, each ~4.5 logical px at zoom 6 and so
  // shorter than the 6px ink run. If the pattern restarted at every joint every
  // edge would be pure ink and the chain would carry no gap at all; a phase
  // that accumulates along the chain must still produce the 6/4 pattern. The
  // presence of a gap is therefore the discriminator, and the same payload is
  // measured at two zooms to keep the constant-width property covered on the
  // chained geometry too.
  const qreal chainLat = kGpuCenterLat;
  const qreal chainLon0 = -124.4;
  const qreal chainStep = 0.1;
  const int chainEdges = 6;
  const qreal chainDashOn = 6.0;
  const qreal chainDashOff = 4.0;
  const qreal chainWidth = 5.0;
  const qreal chainRadius = chainWidth / 2.0 + 3.0;
  // A 0.004 degree latitude stair keeps the edges distinct (real joints with
  // different headings) while the chain stays inside one column-scan band.
  const qreal chainStair = 0.004;

  const auto chainPayload = [&](bool continuous) {
    QByteArray data;
    qreal phase = 0.0;
    for (int edge = 0; edge < chainEdges; ++edge) {
      const qreal lon0 = chainLon0 + edge * chainStep;
      const qreal lon1 = lon0 + chainStep;
      const qreal lat0 = chainLat + (edge % 2 == 0 ? 0.0 : chainStair);
      const qreal lat1 = chainLat + (edge % 2 == 0 ? chainStair : 0.0);
      data.append(packLineRecord(lon0, lat0, lon1, lat1, chainWidth,
                                 QColor(0, 0, 0), chainDashOn, chainDashOff,
                                 continuous ? phase : 0.0));
      const QPointF start = worldPoint(lat0, lon0);
      const QPointF end = worldPoint(lat1, lon1);
      phase += std::hypot(end.x() - start.x(), end.y() - start.y());
    }
    return data;
  };

  const auto chainSample = [&](qreal zoom) {
    const QImage image = view.grabWindow();
    return dashStatsAlong(
        image,
        projectPoint(chainLat, chainLon0, kGpuCenterLat, kGpuCenterLon, zoom,
                     kGpuWidth, kGpuHeight),
        projectPoint(chainLat, chainLon0 + chainEdges * chainStep, kGpuCenterLat,
                     kGpuCenterLon, zoom, kGpuWidth, kGpuHeight),
        dpr, chainRadius);
  };

  item->setProperty("zoom", 6.0);
  item->setProperty("centerLongitude", kGpuCenterLon);
  item->setProperty("lineWidthScale", 1.0);
  item->setProperty("segmentData", chainPayload(false));
  spin(app);
  const DashStats chainRestartAt6 = chainSample(6.0);
  const int chainRestartBytes = item->property("segmentData").toByteArray().size();

  item->setProperty("segmentData", chainPayload(true));
  spin(app);
  const DashStats chainAt6 = chainSample(6.0);

  item->setProperty("zoom", 9.0);
  spin(app);
  const DashStats chainAt9 = chainSample(9.0);
  item->setProperty("segmentData", QByteArray());
  item->setProperty("zoom", 6.0);

  // A restarting pattern cannot leave a gap on edges shorter than the ink run;
  // a continuous one must alternate exactly as the single-segment case does.
  const bool chainContinues = chainAt6.gapRuns >= 1 && chainAt6.inkRuns >= 2 &&
                              chainAt9.gapRuns >= 1 && chainAt9.inkRuns >= 2;
  const bool chainRestartLooksSolid =
      chainRestartAt6.gapRuns == 0 && chainRestartAt6.inkColumns > 0;
  const bool chainWidthConstant =
      qAbs(chainAt9.maxColumnPixels - chainAt6.maxColumnPixels) <=
      qMax(2, qRound(2.0 * dpr));

  const qreal dashExpectedFraction = dashOn / (dashOn + dashOff);
  const bool dashesAlternate =
      dashAt6.inkRuns >= 2 && dashAt6.gapRuns >= 1 && dashAt9.inkRuns >= 2 &&
      dashAt9.gapRuns >= 1;
  const bool dashWidthConstant =
      qAbs(dashAt9.maxColumnPixels - dashAt6.maxColumnPixels) <=
      qMax(2, qRound(2.0 * dpr));

  QJsonObject report;
  report["tool"] = "routing_gpu_line_qsg_check";
  report["endpoint"] =
      "Real Qt Quick window, one QSGGeometryNode with a custom QSGMaterial. "
      "Per-segment quads are expanded to a constant screen-pixel width in the "
      "vertex shader. Fidelity only; NOT presentation FPS.";
  report["backend"] =
      view.rendererInterface() != nullptr
          ? graphicsApiName(view.rendererInterface()->graphicsApi())
          : QStringLiteral("unknown");
  report["fidelity"] = QJsonObject{
      {"device_pixel_ratio", dpr},
      {"stroke_device_pixels_zoom6", widthAt6},
      {"stroke_device_pixels_zoom9", widthAt9},
      {"stroke_logical_pixels_zoom6", widthAt6 / dpr},
      {"stroke_logical_pixels_zoom9", widthAt9 / dpr},
      {"constant_width_across_zoom", qAbs(widthAt9 - widthAt6) <= 1},
      {"stroke_device_pixels_width_scale_2", widthScaled},
      {"width_scale_is_uniform_only", widthScaled > widthAt9},
      {"ink_at_zoom6_midpoint", strokeNear(first, centrePoint, dpr)},
      {"ink_at_zoom9_midpoint", strokeNear(zoomed, zoomedCentre, dpr)},
      {"ink_at_panned_midpoint", strokeNear(panned, pannedCentre, dpr)},
      {"stale_ink_at_pre_pan_midpoint", strokeNear(panned, staleCentre, dpr)},
  };
  report["retained"] = QJsonObject{
      {"segment_count", static_cast<int>(segments.size())},
      {"shader_ready", item->property("shaderReady").toBool()},
      {"geometry_rebuilds_after_first_frame", rebuildsAfterFirst},
      {"geometry_rebuilds_after_zoom", rebuildsAfterZoom},
      {"geometry_rebuilds_after_width_scale", rebuildsAfterScale},
      {"geometry_rebuilds_after_pan", rebuildsAfterPan},
      {"matrix_updates", item->property("matrixUpdates").toInt()},
      {"stroke_device_pixels_after_pan", widthAfterPan},
  };
  report["dashes"] = QJsonObject{
      {"record_bytes", 48},
      {"record_fields", 12},
      {"pattern_pixels", QJsonArray{dashOn, dashOff}},
      {"stroke_width_pixels", dashWidth},
      {"stroke_device_pixels_zoom6", dashAt6.maxColumnPixels},
      {"stroke_device_pixels_zoom9", dashAt9.maxColumnPixels},
      {"constant_width_across_zoom", dashWidthConstant},
      {"ink_runs_zoom6", dashAt6.inkRuns},
      {"gap_runs_zoom6", dashAt6.gapRuns},
      {"ink_runs_zoom9", dashAt9.inkRuns},
      {"gap_runs_zoom9", dashAt9.gapRuns},
      {"alternating_ink", dashesAlternate},
      {"ink_fraction_zoom6", dashAt6.inkFraction},
      {"ink_fraction_zoom9", dashAt9.inkFraction},
      {"expected_ink_fraction", dashExpectedFraction},
      {"ink_fraction_matches_pattern",
       qAbs(dashAt6.inkFraction - dashExpectedFraction) < 0.2 &&
           qAbs(dashAt9.inkFraction - dashExpectedFraction) < 0.2},
      {"solid_when_zero_dash",
       solidAt6.gapRuns == 0 && solidAt6.inkColumns > 0},
      {"solid_ink_runs", solidAt6.inkRuns},
      {"solid_gap_runs", solidAt6.gapRuns},
      {"solid_ink_columns", solidAt6.inkColumns},
      {"solid_sampled_columns", solidAt6.sampledColumns},
      {"legacy_36_byte_solid",
       legacyAt6.gapRuns == 0 && legacyAt6.inkColumns > 0},
      {"legacy_ink_runs", legacyAt6.inkRuns},
      {"legacy_gap_runs", legacyAt6.gapRuns},
      {"legacy_ink_columns", legacyAt6.inkColumns},
      {"legacy_sampled_columns", legacyAt6.sampledColumns},
  };
  report["phases"] = QJsonObject{
      {"chained_edges", chainEdges},
      {"payload_bytes", chainRestartBytes},
      {"payload_is_48_byte_records",
       chainRestartBytes == chainEdges * 48},
      {"pattern_pixels", QJsonArray{chainDashOn, chainDashOff}},
      {"stroke_width_pixels", chainWidth},
      // Restarting the phase at every joint cannot produce a gap on an edge
      // shorter than the ink run, so this row is the proof the phase travels.
      {"restart_ink_runs_zoom6", chainRestartAt6.inkRuns},
      {"restart_gap_runs_zoom6", chainRestartAt6.gapRuns},
      {"restart_looks_solid", chainRestartLooksSolid},
      {"continuous_ink_runs_zoom6", chainAt6.inkRuns},
      {"continuous_gap_runs_zoom6", chainAt6.gapRuns},
      {"continuous_ink_runs_zoom9", chainAt9.inkRuns},
      {"continuous_gap_runs_zoom9", chainAt9.gapRuns},
      {"continuous_across_joints", chainContinues},
      {"stroke_device_pixels_zoom6", chainAt6.maxColumnPixels},
      {"stroke_device_pixels_zoom9", chainAt9.maxColumnPixels},
      {"constant_width_across_zoom", chainWidthConstant},
      {"ink_fraction_zoom6", chainAt6.inkFraction},
      {"expected_ink_fraction", chainDashOn / (chainDashOn + chainDashOff)},
  };
  report["integration"] =
      "Standalone harness built against a Qt C++ SDK. The shaders are embedded "
      "as .qsb resources (Metal plus GLSL) in both the module and this "
      "executable, so tools/build_routing_qsg_module.py can copy the single "
      "plugin library into the PySide6 runtime.";

  const QByteArray json = QJsonDocument(report).toJson(QJsonDocument::Indented);
  std::fwrite(json.constData(), 1, static_cast<size_t>(json.size()), stdout);
  return 0;
}

int runGpuLineBench(QGuiApplication &app, int segmentCount, int frameCount,
                    const QString &shotDir) {
  QQuickView view;
  view.setResizeMode(QQuickView::SizeRootObjectToView);
  qmlRegisterType<GpuLineItem>("SailworksRouting", 1, 0, "GpuLineItem");
  view.setSource(QUrl::fromLocalFile(QStringLiteral(SAILWORKS_GPU_LINE_TEST_QML)));
  view.resize(kGpuWidth, kGpuHeight);
  view.show();
  spin(app);
  if (view.status() != QQuickView::Ready || view.rootObject() == nullptr) {
    std::fprintf(stderr, "gpu line bench scene failed to load\n");
    return 2;
  }
  QQuickItem *item = view.rootObject()->findChild<QQuickItem *>("gpuLine");
  if (item == nullptr) {
    std::fprintf(stderr, "gpu line item not found\n");
    return 2;
  }
  item->setProperty("zoom", 6.0);
  item->setProperty("centerLatitude", kGpuCenterLat);
  item->setProperty("centerLongitude", kGpuCenterLon);
  spin(app);

  QStringList sceneErrors;
  QObject::connect(
      &view, &QQuickWindow::sceneGraphError, &view,
      [&sceneErrors](QQuickWindow::SceneGraphError, const QString &message) {
        sceneErrors.append(message);
      });

  FrameRecorder recorder;
  QObject::connect(
      &view, &QQuickWindow::afterRendering, &view,
      [&recorder]() { recorder.record(); }, Qt::DirectConnection);

  QElapsedTimer generateClock;
  generateClock.start();
  const QVariantList segments = generateBenchSegments(segmentCount);
  const double generateMs = generateClock.nsecsElapsed() / 1e6;

  // Build the geometry exactly once. The rebuild happens inside the synced
  // frame, so the stamp covers build plus one present; the steady-state median
  // measured below is subtracted to report an estimate of the build alone.
  recorder.reset();
  const int rebuildsBefore = item->property("geometryRebuilds").toInt();
  int rebuildsAfterBuild = rebuildsBefore;
  item->setProperty("segments", segments);
  while (recorder.count() < 1 || rebuildsAfterBuild <= rebuildsBefore) {
    app.processEvents();
    QThread::msleep(0);
    rebuildsAfterBuild = item->property("geometryRebuilds").toInt();
  }
  const QVector<qint64> buildStamps = recorder.take();
  const double buildPlusFrameMs =
      buildStamps.isEmpty() ? 0.0 : buildStamps.last() / 1e6;

  // Driven frames: pan every frame and toggle zoom periodically. Neither path
  // touches the vertex data, so geometryRebuilds must stay at one.
  constexpr int kWarmupFrames = 8;
  const int drivenFrames = frameCount + kWarmupFrames;
  recorder.reset();
  for (int frame = 0; frame < drivenFrames; ++frame) {
    const qreal phase = frame * 0.12;
    item->setProperty("centerLatitude", kGpuCenterLat + 0.35 * std::sin(phase));
    item->setProperty("centerLongitude",
                      kGpuCenterLon + 0.55 * std::cos(phase));
    if (frame % 20 == 19) {
      item->setProperty("zoom", ((frame / 20) % 2 == 0) ? 7.0 : 6.0);
    }
    const int before = recorder.count();
    while (recorder.count() <= before) {
      app.processEvents();
      QThread::msleep(0);
    }
  }
  const QVector<qint64> stamps = recorder.take();

  QVector<double> frameMs;
  for (int index = kWarmupFrames + 1; index < stamps.size(); ++index) {
    frameMs.append((stamps[index] - stamps[index - 1]) / 1e6);
  }
  double frameTotalMs = 0.0;
  for (double sample : frameMs) frameTotalMs += sample;

  const double medianFrameMs = percentileMs(frameMs, 0.5);
  const double meanFrameMs =
      frameMs.isEmpty() ? 0.0 : frameTotalMs / frameMs.size();
  const int rebuildsFinal = item->property("geometryRebuilds").toInt();

  // Two-zoom visual comparison. Grabs are for looking at the picture only; the
  // timing above never reads pixels back.
  QJsonArray screenshotArray;
  if (!shotDir.isEmpty() && QDir().mkpath(shotDir)) {
    const qreal zooms[2] = {6.0, 8.0};
    const char *labels[2] = {"zoom6", "zoom8"};
    for (int index = 0; index < 2; ++index) {
      item->setProperty("zoom", zooms[index]);
      item->setProperty("centerLatitude", kGpuCenterLat);
      item->setProperty("centerLongitude", kGpuCenterLon);
      spin(app);
      const QString path =
          QDir(shotDir).filePath(QStringLiteral("gpuline-%1-%2.png")
                                     .arg(segmentCount)
                                     .arg(QLatin1String(labels[index])));
      if (view.grabWindow().save(path)) screenshotArray.append(path);
    }
  }

  QJsonArray errorArray;
  for (const QString &message : sceneErrors) errorArray.append(message);

  QJsonObject report;
  report["tool"] = "routing_gpu_line_bench";
  report["endpoint"] =
      "Real Qt Quick window, one GpuLineItem holding a single QSGGeometryNode. "
      "N deterministic segments are uploaded once; frames are timed from the "
      "window's afterRendering signal while panning and zooming, so geometry is "
      "never rebuilt. Wall time includes the present/vsync wait.";
  report["backend"] = view.rendererInterface() != nullptr
                          ? graphicsApiName(view.rendererInterface()->graphicsApi())
                          : QStringLiteral("unknown");
  report["bench"] = QJsonObject{
      {"segment_count", segmentCount},
      {"vertex_count", segmentCount * 4},
      {"index_count", segmentCount * 6},
      {"vertex_bytes", segmentCount * 4 * 56},
      {"colors", 4},
      {"widths", QJsonArray{1.0, 2.0, 3.5, 6.0}},
      {"generate_ms", generateMs},
      {"geometry_build_frame_ms", buildPlusFrameMs},
      {"steady_frame_ms_median", medianFrameMs},
      // Exactly one rebuild for the upload; the earlier count is the empty
      // geometry built on the first frame of the view.
      {"geometry_rebuilds_for_upload", rebuildsAfterBuild - rebuildsBefore},
      {"geometry_rebuilds_after_build", rebuildsAfterBuild},
      {"geometry_rebuilds_during_frames", rebuildsFinal - rebuildsAfterBuild},
      {"geometry_rebuilds_final", rebuildsFinal},
      {"matrix_updates", item->property("matrixUpdates").toInt()},
      {"shader_ready", item->property("shaderReady").toBool()},
      {"frames_driven", drivenFrames},
      {"frames_warmup", kWarmupFrames},
      {"frames_measured", static_cast<int>(frameMs.size())},
      {"frame_ms_median", medianFrameMs},
      {"frame_ms_mean", meanFrameMs},
      {"frame_ms_p95", percentileMs(frameMs, 0.95)},
      {"frame_ms_min", percentileMs(frameMs, 0.0)},
      {"frame_ms_max", percentileMs(frameMs, 1.0)},
      {"frame_wall_total_ms", frameTotalMs},
      {"likely_vsync_capped", medianFrameMs < 17.5},
      {"zoom_toggles", drivenFrames / 20},
      {"scene_graph_errors", errorArray},
      {"screenshots", screenshotArray},
  };
  report["integration"] =
      "Standalone harness built against a Qt C++ SDK by "
      "tools/build_routing_qsg_module.py. Timing only; no ENC integration.";

  std::fprintf(stderr,
               "bench N=%d backend=%s build_frame=%.1fms steady=%.1fms "
               "rebuilds=%d(+%d during frames) errors=%d\n", segmentCount,
               qPrintable(report["backend"].toString()), buildPlusFrameMs,
               medianFrameMs, rebuildsAfterBuild,
               rebuildsFinal - rebuildsAfterBuild,
               static_cast<int>(sceneErrors.size()));

  const QByteArray json = QJsonDocument(report).toJson(QJsonDocument::Indented);
  std::fwrite(json.constData(), 1, static_cast<size_t>(json.size()), stdout);
  return 0;
}

int main(int argc, char **argv) {
  const QStringList arguments = [&argc, &argv]() {
    QStringList values;
    for (int index = 1; index < argc; ++index) {
      values.append(QString::fromLocal8Bit(argv[index]));
    }
    return values;
  }();

  bool gpuLineMode = false;
  int benchCount = -1;
  int benchFrames = 60;
  QString shotDir = QStringLiteral("build/routing-gpu-bench");
  for (int index = 0; index < arguments.size(); ++index) {
    const QString &argument = arguments[index];
    if (argument == QLatin1String("--gpu-line")) {
      gpuLineMode = true;
    } else if (argument == QLatin1String("--gpu-line-bench") &&
               index + 1 < arguments.size()) {
      benchCount = arguments[++index].toInt();
    } else if (argument.startsWith(QLatin1String("--gpu-line-bench="))) {
      benchCount = argument.mid(16).toInt();
    } else if (argument == QLatin1String("--frames") &&
               index + 1 < arguments.size()) {
      benchFrames = qMax(1, arguments[++index].toInt());
    } else if (argument == QLatin1String("--shots") &&
               index + 1 < arguments.size()) {
      shotDir = arguments[++index];
    } else if (argument == QLatin1String("--no-shots")) {
      shotDir.clear();
    }
  }
  if (benchCount == 0) benchCount = -1;

  if ((gpuLineMode || benchCount > 0) &&
      qEnvironmentVariableIsEmpty("QSG_RHI_BACKEND")) {
    qputenv("QSG_RHI_BACKEND", "metal");
  }
  QGuiApplication app(argc, argv);
  if (benchCount > 0) return runGpuLineBench(app, benchCount, benchFrames, shotDir);
  if (gpuLineMode) return runGpuLineCheck(app);
  return runRetainedCheck(app);
}
