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

qreal mercatorY(qreal latitude) {
  const qreal clamped = qBound<qreal>(-85.05112878, latitude, 85.05112878);
  return 0.5 - std::asinh(std::tan(qDegreesToRadians(clamped))) / (2.0 * M_PI);
}

qreal mercatorX(qreal longitude) { return (longitude + 180.0) / 360.0; }

}  // namespace

RoutingRetainedItem::RoutingRetainedItem(QQuickItem *parent)
    : QQuickItem(parent) {
  setFlag(ItemHasContents, true);
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
    for (const QVariant &entry : segments_) {
      const QVariantList polyline = entry.toList();
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
            auto *rect = new QSGSimpleRectNode(
                QRectF(0.0, -stroke / 2.0, length, stroke), color_);
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
    styleDirty_ = false;
  } else if (styleDirty_) {
    for (QSGNode *segment = camera->firstChild(); segment != nullptr;
         segment = segment->nextSibling()) {
      for (QSGNode *rect = segment->firstChild(); rect != nullptr;
           rect = rect->nextSibling()) {
        if (auto *rectangle = dynamic_cast<QSGSimpleRectNode *>(rect)) {
          rectangle->setColor(color_);
          rectangle->markDirty(QSGNode::DirtyMaterial);
        }
      }
    }
    styleDirty_ = false;
  }

  if (cameraDirty_) {
    QMatrix4x4 matrix;
    matrix.translate(width() / 2.0 - mercatorX(centerLongitude_) * worldPixels,
                     height() / 2.0 - mercatorY(centerLatitude_) * worldPixels);
    camera->setMatrix(matrix);
    camera->markDirty(QSGNode::DirtyMatrix);
    ++matrixUpdates_;
    cameraDirty_ = false;
  }

  return root;
}
