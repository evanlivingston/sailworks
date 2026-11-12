#include "routingretaineditem.h"

#include <QtCore/QVariant>
#include <QtGui/QColor>
#include <QtGui/QMatrix4x4>
#include <QtMath>
#include <QtQuick/QSGNode>
#include <QtQuick/QSGSimpleRectNode>
#include <QtQuick/QSGTransformNode>

#include <cmath>

namespace {

// Rectangle node that remembers which routing polyline it belongs to so a
// hover/selection change can recolour without rebuilding geometry.
class SegmentRect : public QSGSimpleRectNode {
 public:
  explicit SegmentRect(const QRectF &rect, const QColor &colour,
                       const QString &polylineId)
      : QSGSimpleRectNode(rect, colour), polylineId_(polylineId) {}
  QString polylineId() const { return polylineId_; }

 private:
  QString polylineId_;
};

}  // namespace

namespace {

qreal mercatorY(qreal latitude) {
  const qreal clamped = qBound<qreal>(-85.05112878, latitude, 85.05112878);
  return 0.5 - std::asinh(std::tan(qDegreesToRadians(clamped))) / (2.0 * M_PI);
}

qreal mercatorX(qreal longitude) { return (longitude + 180.0) / 360.0; }

}  // namespace

RoutingRetainedItem::RoutingRetainedItem(QQuickItem *parent)
    : QQuickItem(parent) {
  setFlag(ItemHasContents, true);
  // Geometry only: mouse, hover and wheel must reach the map beneath.
  setAcceptedMouseButtons(Qt::NoButton);
  setAcceptHoverEvents(false);
}

void RoutingRetainedItem::setPolylines(const QVariantList &polylines) {
  if (polylines_ == polylines) return;
  polylines_ = polylines;
  markGeometryDirty();
  emit segmentsChanged();
}

void RoutingRetainedItem::setHoverId(const QString &value) {
  if (hoverId_ == value) return;
  hoverId_ = value;
  markStyleDirty();
  emit highlightChanged();
}

void RoutingRetainedItem::setSelectedId(const QString &value) {
  if (selectedId_ == value) return;
  selectedId_ = value;
  markStyleDirty();
  emit highlightChanged();
}

void RoutingRetainedItem::setHoverColor(const QColor &value) {
  if (hoverColor_ == value) return;
  hoverColor_ = value;
  markStyleDirty();
  emit styleChanged();
}

void RoutingRetainedItem::setSelectedColor(const QColor &value) {
  if (selectedColor_ == value) return;
  selectedColor_ = value;
  markStyleDirty();
  emit styleChanged();
}

void RoutingRetainedItem::setSegments(const QVariantList &segments) {
  if (segments_ == segments) return;
  segments_ = segments;
  markGeometryDirty();
  emit segmentsChanged();
}

void RoutingRetainedItem::setColor(const QColor &color) {
  if (color_ == color) return;
  color_ = color;
  markStyleDirty();
  emit styleChanged();
}

void RoutingRetainedItem::setLineWidth(qreal width) {
  if (qFuzzyCompare(lineWidth_, width)) return;
  lineWidth_ = width;
  markGeometryDirty();
  emit styleChanged();
}

void RoutingRetainedItem::setCenterLatitude(qreal value) {
  if (qFuzzyCompare(centerLatitude_, value)) return;
  centerLatitude_ = value;
  markCameraDirty();
  emit cameraChanged();
}

void RoutingRetainedItem::setCenterLongitude(qreal value) {
  if (qFuzzyCompare(centerLongitude_, value)) return;
  centerLongitude_ = value;
  markCameraDirty();
  emit cameraChanged();
}

void RoutingRetainedItem::setZoom(qreal value) {
  if (qFuzzyCompare(zoom_, value)) return;
  zoom_ = value;
  markGeometryDirty();
  markCameraDirty();
  emit zoomChanged();
}

void RoutingRetainedItem::markGeometryDirty() {
  geometryDirty_ = true;
  update();
}

void RoutingRetainedItem::markStyleDirty() {
  styleDirty_ = true;
  update();
}

void RoutingRetainedItem::markCameraDirty() {
  cameraDirty_ = true;
  update();
}

void RoutingRetainedItem::geometryChange(const QRectF &newGeometry,
                                         const QRectF &oldGeometry) {
  QQuickItem::geometryChange(newGeometry, oldGeometry);
  if (newGeometry.size() != oldGeometry.size()) {
    // The camera matrix centres the world on width()/height(); refresh only
    // the translation, leaving the retained segment nodes untouched.
    markCameraDirty();
  }
}

QSGNode *RoutingRetainedItem::updatePaintNode(QSGNode *node,
                                              UpdatePaintNodeData *) {
  QSGNode *root = node;
  QSGTransformNode *camera = nullptr;
  if (root == nullptr) {
    root = new QSGNode();
    camera = new QSGTransformNode();
    root->appendChildNode(camera);
    geometryDirty_ = true;
    styleDirty_ = true;
    cameraDirty_ = true;
  } else {
    camera = static_cast<QSGTransformNode *>(root->firstChild());
  }

  const qreal worldPixels = 256.0 * std::pow(2.0, zoom_);
  const qreal stroke = lineWidth_;

  if (geometryDirty_) {
    while (camera->firstChild() != nullptr) {
      QSGNode *child = camera->firstChild();
      camera->removeChildNode(child);
      delete child;
    }
    QVariantList effective = polylines_;
    if (effective.isEmpty()) {
      for (const QVariant &entry : segments_) {
        QVariantMap map;
        map["id"] = QString();
        map["points"] = entry;
        effective.append(map);
      }
    }
    for (const QVariant &entryValue : effective) {
      const QVariantMap entry = entryValue.toMap();
      const QVariantList polyline = entry.value("points").toList();
      const QString polylineId = entry.value("id").toString();
      if (polyline.size() < 2) continue;
      QPointF previous;
      bool havePrevious = false;
      for (const QVariant &pointValue : polyline) {
        const QVariantList point = pointValue.toList();
        if (point.size() < 2) {
          havePrevious = false;
          continue;
        }
        const QPointF current(mercatorX(point[1].toDouble()) * worldPixels,
                              mercatorY(point[0].toDouble()) * worldPixels);
        if (havePrevious) {
          const qreal dx = current.x() - previous.x();
          const qreal dy = current.y() - previous.y();
          const qreal length = std::hypot(dx, dy);
          if (length > 0.0) {
            auto *segmentTransform = new QSGTransformNode();
            QMatrix4x4 matrix;
            matrix.translate(previous.x(), previous.y());
            matrix.rotate(qRadiansToDegrees(std::atan2(dy, dx)), 0, 0, 1);
            segmentTransform->setMatrix(matrix);
            QColor segmentColour = color_;
            if (!polylineId.isEmpty()) {
              if (polylineId == hoverId_) {
                segmentColour = hoverColor_;
              } else if (polylineId == selectedId_) {
                segmentColour = selectedColor_;
              }
            }
            auto *rect = new SegmentRect(
                QRectF(0.0, -stroke / 2.0, length, stroke), segmentColour,
                polylineId);
            segmentTransform->appendChildNode(rect);
            camera->appendChildNode(segmentTransform);
          }
        }
        previous = current;
        havePrevious = true;
      }
    }
    ++geometryRebuilds_;
    geometryDirty_ = false;
    // Keep one style pass pending so a later highlight change is not lost.
    styleDirty_ = true;
  } else if (styleDirty_) {
    for (QSGNode *segment = camera->firstChild(); segment != nullptr;
         segment = segment->nextSibling()) {
      for (QSGNode *rect = segment->firstChild(); rect != nullptr;
           rect = rect->nextSibling()) {
        if (auto *rectangle = dynamic_cast<SegmentRect *>(rect)) {
          QColor colour = color_;
          if (!rectangle->polylineId().isEmpty()) {
            if (rectangle->polylineId() == hoverId_) {
              colour = hoverColor_;
            } else if (rectangle->polylineId() == selectedId_) {
              colour = selectedColor_;
            }
          }
          rectangle->setColor(colour);
          rectangle->markDirty(QSGNode::DirtyMaterial);
        }
      }
    }
    styleDirty_ = false;
  }

  if (cameraDirty_ || cameraWidth_ != width() || cameraHeight_ != height()) {
    QMatrix4x4 matrix;
    matrix.translate(width() / 2.0 - mercatorX(centerLongitude_) * worldPixels,
                     height() / 2.0 - mercatorY(centerLatitude_) * worldPixels);
    camera->setMatrix(matrix);
    camera->markDirty(QSGNode::DirtyMatrix);
    cameraWidth_ = width();
    cameraHeight_ = height();
    ++matrixUpdates_;
    cameraDirty_ = false;
  }

  return root;
}
