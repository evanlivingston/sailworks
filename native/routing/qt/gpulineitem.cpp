#include "gpulineitem.h"

#include <QtCore/QFile>
#include <QtCore/QVariant>
#include <QtGui/QColor>
#include <QtGui/QMatrix4x4>
#include <QtMath>
#include <QtQuick/QSGGeometryNode>
#include <QtQuick/QSGMaterial>
#include <QtQuick/QQuickWindow>

#include <cmath>
#include <cstring>
#include <vector>

#include "gpulinematerial.h"

namespace {

// Mirrors the vertex layout declared in shaders/line.vert: eight attributes,
// 56 bytes, no padding. `along` is the distance from the segment start in
// normalised world units; `phase` is the same unit at the segment start, so
// `phase + along` continues one dash pattern across a polyline. The vertex
// shader scales both to screen pixels so the dash period is constant.
// `dashOn`/`dashOff` travel per vertex because a dash pattern is a per-segment
// property and this item draws one segment per quad.
struct LineVertex {
  float x;
  float y;
  float nx;
  float ny;
  float side;
  float width;
  float along;
  float r;
  float g;
  float b;
  float a;
  float dashOn;
  float dashOff;
  float phase;
};

static_assert(sizeof(LineVertex) == 56, "LineVertex must stay tightly packed");

const QSGGeometry::AttributeSet &lineAttributes() {
  static const QSGGeometry::Attribute data[] = {
      QSGGeometry::Attribute::create(0, 2, QSGGeometry::FloatType),
      QSGGeometry::Attribute::create(1, 2, QSGGeometry::FloatType, 8),
      QSGGeometry::Attribute::create(2, 1, QSGGeometry::FloatType, 16),
      QSGGeometry::Attribute::create(3, 1, QSGGeometry::FloatType, 20),
      QSGGeometry::Attribute::create(4, 1, QSGGeometry::FloatType, 24),
      QSGGeometry::Attribute::create(5, 4, QSGGeometry::FloatType, 28),
      QSGGeometry::Attribute::create(6, 2, QSGGeometry::FloatType, 44),
      QSGGeometry::Attribute::create(7, 1, QSGGeometry::FloatType, 52),
  };
  static const QSGGeometry::AttributeSet set{
      8, static_cast<int>(sizeof(LineVertex)), data};
  return set;
}

qreal mercatorY(qreal latitude) {
  const qreal clamped = qBound<qreal>(-85.05112878, latitude, 85.05112878);
  return 0.5 - std::asinh(std::tan(qDegreesToRadians(clamped))) / (2.0 * M_PI);
}

qreal mercatorX(qreal longitude) { return (longitude + 180.0) / 360.0; }

struct SegmentQuad {
  QPointF start;
  QPointF end;
  QColor color;
  qreal width;
  float dashOn;
  float dashOff;
  float phase;
};

// Packed ENC segment records. The current layout is twelve float32 values
// (endpoints, width, RGBA, dash on/off, phase). The eleven-float layout carries
// no phase and the legacy nine-float one carries no dash either; both are still
// read so a stale payload keeps drawing.
constexpr int kPackedSolidFields = 9;
constexpr int kPackedDashedFields = 11;
constexpr int kPackedPhasedFields = 12;
constexpr int kPackedSolidStride =
    kPackedSolidFields * static_cast<int>(sizeof(float));
constexpr int kPackedDashedStride =
    kPackedDashedFields * static_cast<int>(sizeof(float));
constexpr int kPackedPhasedStride =
    kPackedPhasedFields * static_cast<int>(sizeof(float));

}  // namespace

GpuLineItem::GpuLineItem(QQuickItem *parent) : QQuickItem(parent) {
  setFlag(ItemHasContents, true);
  // Geometry only: mouse, hover and wheel must reach the map beneath.
  setAcceptedMouseButtons(Qt::NoButton);
  setAcceptHoverEvents(false);
  shaderReady_ = QFile::exists(GpuLineShader::vertexShaderPath()) &&
                 QFile::exists(GpuLineShader::fragmentShaderPath());
}

void GpuLineItem::setSegments(const QVariantList &segments) {
  if (segments_ == segments) return;
  segments_ = segments;
  markGeometryDirty();
  emit segmentsChanged();
}

void GpuLineItem::setSegmentData(const QByteArray &data) {
  if (segmentData_ == data) return;
  segmentData_ = data;
  // Prefer the 48-byte phased record, then the 44-byte dashed one, then the
  // legacy 36-byte one. A payload divisible by several strides is read with the
  // largest, which is the only layout the current publisher emits; a partial
  // trailing record is never a segment, so every count floors.
  if (data.size() % kPackedPhasedStride == 0) {
    recordStride_ = kPackedPhasedStride;
    legacyRecord_ = false;
  } else if (data.size() % kPackedDashedStride == 0) {
    recordStride_ = kPackedDashedStride;
    legacyRecord_ = false;
  } else {
    recordStride_ = kPackedSolidStride;
    legacyRecord_ = true;
  }
  segmentCount_ = data.size() / recordStride_;
  markGeometryDirty();
  emit segmentDataChanged();
}

void GpuLineItem::setColor(const QColor &color) {
  if (color_ == color) return;
  color_ = color;
  // Colours are baked into the vertex attributes, so restyling rebuilds.
  markGeometryDirty();
  emit styleChanged();
}

void GpuLineItem::setLineWidth(qreal width) {
  if (qFuzzyCompare(lineWidth_, width)) return;
  lineWidth_ = width;
  markGeometryDirty();
  emit styleChanged();
}

void GpuLineItem::setLineWidthScale(qreal scale) {
  if (qFuzzyCompare(lineWidthScale_, scale)) return;
  lineWidthScale_ = scale;
  // Uniform only: no geometry rebuild and no vertex upload.
  markStyleDirty();
  emit styleChanged();
}

void GpuLineItem::setCenterLatitude(qreal value) {
  if (qFuzzyCompare(centerLatitude_, value)) return;
  centerLatitude_ = value;
  markCameraDirty();
  emit cameraChanged();
}

void GpuLineItem::setCenterLongitude(qreal value) {
  if (qFuzzyCompare(centerLongitude_, value)) return;
  centerLongitude_ = value;
  markCameraDirty();
  emit cameraChanged();
}

void GpuLineItem::setZoom(qreal value) {
  if (qFuzzyCompare(zoom_, value)) return;
  zoom_ = value;
  // Camera only: the quads are already world-space and the stroke is expanded
  // in screen pixels by the vertex shader.
  markCameraDirty();
  emit zoomChanged();
}

void GpuLineItem::markGeometryDirty() {
  geometryDirty_ = true;
  update();
}

void GpuLineItem::markStyleDirty() { update(); }

void GpuLineItem::markCameraDirty() {
  // Only the material matrix is rebuilt; the vertex data is untouched.
  update();
}

void GpuLineItem::geometryChange(const QRectF &newGeometry,
                                 const QRectF &oldGeometry) {
  QQuickItem::geometryChange(newGeometry, oldGeometry);
  if (newGeometry.size() != oldGeometry.size()) {
    // Only the camera matrix centres the world on width()/height(); segment
    // vertices are untouched.
    markCameraDirty();
  }
}

void GpuLineItem::rebuildGeometry(QSGGeometryNode *node) {
  std::vector<SegmentQuad> quads;
  QPointF firstWorld;
  bool haveOrigin = false;

  const auto appendPolyline = [&](const QVariantList &points,
                                  const QColor &colour, qreal width) {
    QPointF previousWorld;
    bool havePrevious = false;
    for (const QVariant &pointValue : points) {
      const QVariantList point = pointValue.toList();
      if (point.size() < 2) {
        havePrevious = false;
        continue;
      }
      const QPointF world(mercatorX(point[1].toDouble()),
                          mercatorY(point[0].toDouble()));
      if (!haveOrigin) {
        firstWorld = world;
        haveOrigin = true;
      }
      if (havePrevious) {
        const QPointF delta = world - previousWorld;
        if (!qFuzzyIsNull(delta.x()) || !qFuzzyIsNull(delta.y())) {
          quads.push_back(
              {previousWorld, world, colour, width, 0.0f, 0.0f, 0.0f});
        }
      }
      previousWorld = world;
      havePrevious = true;
    }
  };

  if (!segmentData_.isEmpty()) {
    // Packed ENC payload.  Records are (lon0, lat0, lon1, lat1, width, r, g,
    // b, a[, dash_on, dash_off[, phase]]); the colour is already 0..1, the
    // width and the dash pattern already screen pixels, and the phase already
    // normalised world units, so each record costs one Mercator transform per
    // end and nothing else.  This is the same geometry the `segments` path
    // builds: one quad per drawable edge, square ends, no joins.
    const char *bytes = segmentData_.constData();
    const int fields = recordStride_ / static_cast<int>(sizeof(float));
    float values[kPackedPhasedFields] = {};
    for (int index = 0; index < segmentCount_; ++index) {
      std::memcpy(values,
                  bytes + static_cast<qsizetype>(index) * recordStride_,
                  static_cast<size_t>(recordStride_));
      const QPointF start(mercatorX(values[0]), mercatorY(values[1]));
      const QPointF end(mercatorX(values[2]), mercatorY(values[3]));
      // The publisher drops float32-degenerate edges; a zero-length quad would
      // divide by zero below, so it is not admitted either way.
      if (start.x() == end.x() && start.y() == end.y()) continue;
      if (!haveOrigin) {
        firstWorld = start;
        haveOrigin = true;
      }
      const QColor colour = QColor::fromRgbF(
          qBound(0.0f, values[5], 1.0f), qBound(0.0f, values[6], 1.0f),
          qBound(0.0f, values[7], 1.0f), qBound(0.0f, values[8], 1.0f));
      // Shorter records leave the trailing slots zero-filled, so a legacy
      // payload stays solid and starts every segment's dash at zero.
      const float dashOn = qMax(0.0f, values[9]);
      const float dashOff = qMax(0.0f, values[10]);
      const float phase = fields > 11 ? qMax(0.0f, values[11]) : 0.0f;
      quads.push_back({start, end, colour, static_cast<qreal>(values[4]),
                       dashOn, dashOff, phase});
    }
  } else {
    // Prototype QML path: a list of point lists or segment maps.
    for (const QVariant &entry : segments_) {
      if (entry.userType() == QMetaType::QVariantMap) {
        const QVariantMap map = entry.toMap();
        QColor colour = color_;
        if (map.contains("color")) {
          const QVariant value = map.value("color");
          colour = value.value<QColor>();
          if (!colour.isValid()) colour = QColor(value.toString());
          if (!colour.isValid()) colour = color_;
        }
        const qreal width =
            map.contains("width") ? map.value("width").toDouble() : lineWidth_;
        appendPolyline(map.value("points").toList(), colour, width);
      } else {
        appendPolyline(entry.toList(), color_, lineWidth_);
      }
    }
  }

  origin_ = haveOrigin ? firstWorld : QPointF();

  QSGGeometry *geometry = node->geometry();
  geometry->setDrawingMode(QSGGeometry::DrawTriangles);
  const int quadCount = static_cast<int>(quads.size());
  geometry->allocate(quadCount * 4, quadCount * 6);

  auto *vertices = reinterpret_cast<LineVertex *>(geometry->vertexData());
  void *indexData = geometry->indexData();
  const bool wideIndices = geometry->indexType() == QSGGeometry::UnsignedIntType;
  auto *indices16 = static_cast<quint16 *>(indexData);
  auto *indices32 = static_cast<quint32 *>(indexData);

  for (int index = 0; index < quadCount; ++index) {
    const SegmentQuad &quad = quads[static_cast<size_t>(index)];
    const QPointF start = quad.start - origin_;
    const QPointF end = quad.end - origin_;
    const qreal dx = end.x() - start.x();
    const qreal dy = end.y() - start.y();
    const qreal length = std::hypot(dx, dy);
    const qreal nx = -dy / length;
    const qreal ny = dx / length;
    const float r = static_cast<float>(quad.color.redF());
    const float g = static_cast<float>(quad.color.greenF());
    const float b = static_cast<float>(quad.color.blueF());
    const float a = static_cast<float>(quad.color.alphaF());
    const float width = static_cast<float>(quad.width);

    // Corners in the order (start,+) (start,-) (end,+) (end,-); `side` flips
    // the normal so the two vertices of an edge share one offset.
    LineVertex *vertex = vertices + index * 4;
    const auto writeVertex = [&](int slot, float px, float py, float side,
                                float along) {
      LineVertex &out = vertex[slot];
      out = LineVertex{};
      out.x = px;
      out.y = py;
      out.nx = static_cast<float>(nx);
      out.ny = static_cast<float>(ny);
      out.side = side;
      out.width = width;
      out.along = along;
      out.r = r;
      out.g = g;
      out.b = b;
      out.a = a;
      out.dashOn = quad.dashOn;
      out.dashOff = quad.dashOff;
      out.phase = quad.phase;
    };
    writeVertex(0, static_cast<float>(start.x()), static_cast<float>(start.y()), 1.0f,
         0.0f);
    writeVertex(1, static_cast<float>(start.x()), static_cast<float>(start.y()), -1.0f,
         0.0f);
    writeVertex(2, static_cast<float>(end.x()), static_cast<float>(end.y()), 1.0f,
         static_cast<float>(length));
    writeVertex(3, static_cast<float>(end.x()), static_cast<float>(end.y()), -1.0f,
         static_cast<float>(length));

    const int base = index * 4;
    const quint32 order[6] = {
        static_cast<quint32>(base + 0), static_cast<quint32>(base + 1),
        static_cast<quint32>(base + 2), static_cast<quint32>(base + 2),
        static_cast<quint32>(base + 1), static_cast<quint32>(base + 3)};
    for (int i = 0; i < 6; ++i) {
      if (wideIndices) {
        indices32[index * 6 + i] = order[i];
      } else {
        indices16[index * 6 + i] = static_cast<quint16>(order[i]);
      }
    }
  }

  node->markDirty(QSGNode::DirtyGeometry);
  ++geometryRebuilds_;
}

QSGNode *GpuLineItem::updatePaintNode(QSGNode *node,
                                      UpdatePaintNodeData *) {
  auto *geometryNode = static_cast<QSGGeometryNode *>(node);
  if (geometryNode == nullptr) {
    geometryNode = new QSGGeometryNode;
    auto *material = new GpuLineMaterial;
    auto *geometry = new QSGGeometry(lineAttributes(), 0);
    geometry->setDrawingMode(QSGGeometry::DrawTriangles);
    geometryNode->setGeometry(geometry);
    geometryNode->setFlag(QSGNode::OwnsGeometry);
    geometryNode->setMaterial(material);
    geometryNode->setFlag(QSGNode::OwnsMaterial);
    geometryDirty_ = true;
  }

  if (geometryDirty_) {
    rebuildGeometry(geometryNode);
    geometryDirty_ = false;
  }

  const qreal scale = 256.0 * std::pow(2.0, zoom_);
  const qreal devicePixelRatio =
      window() != nullptr ? window()->effectiveDevicePixelRatio() : 1.0;

  // Local pixel space is world pixels relative to origin_; the matrix places
  // that space on the item and projects it. Panning changes only the
  // translation, so no vertex data is touched.
  QMatrix4x4 matrix;
  matrix.ortho(0.0f, static_cast<float>(width()), static_cast<float>(height()),
               0.0f, -1.0f, 1.0f);
  matrix.translate(
      static_cast<float>(width() / 2.0 +
                         (origin_.x() - mercatorX(centerLongitude_)) * scale),
      static_cast<float>(height() / 2.0 +
                         (origin_.y() - mercatorY(centerLatitude_)) * scale));

  auto *material = static_cast<GpuLineMaterial *>(geometryNode->material());
  material->setMatrix(matrix);
  material->setScale(static_cast<float>(scale));
  material->setDevicePixelRatio(static_cast<float>(devicePixelRatio));
  material->setWidthScale(static_cast<float>(lineWidthScale_));
  geometryNode->markDirty(QSGNode::DirtyMaterial);

  ++matrixUpdates_;

  return geometryNode;
}
