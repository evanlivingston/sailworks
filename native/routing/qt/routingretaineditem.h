#pragma once

#include <QtCore/QVariantList>
#include <QtQuick/QQuickItem>

// Retained routing geometry for the Qt Quick scene graph.
//
// Each geographic segment becomes one rotated rectangle node in normalised
// Web-Mercator world coordinates, parented under a single camera transform.
// Panning updates only the camera matrix and restyling updates only node
// colours, so neither rebuilds geometry. Zoom changes resize the stroke, which
// is the one camera action that refreshes the segment quads.
class RoutingRetainedItem : public QQuickItem {
  Q_OBJECT
  Q_PROPERTY(QVariantList segments READ segments WRITE setSegments NOTIFY
                 segmentsChanged)
  Q_PROPERTY(QColor color READ color WRITE setColor NOTIFY styleChanged)
  Q_PROPERTY(qreal lineWidth READ lineWidth WRITE setLineWidth NOTIFY styleChanged)
  Q_PROPERTY(qreal centerLatitude READ centerLatitude WRITE setCenterLatitude
                 NOTIFY cameraChanged)
  Q_PROPERTY(qreal centerLongitude READ centerLongitude WRITE setCenterLongitude
                 NOTIFY cameraChanged)
  Q_PROPERTY(qreal zoom READ zoom WRITE setZoom NOTIFY zoomChanged)
  Q_PROPERTY(int geometryRebuilds READ geometryRebuilds)
  Q_PROPERTY(int matrixUpdates READ matrixUpdates)

 public:
  explicit RoutingRetainedItem(QQuickItem *parent = nullptr);

  QVariantList segments() const { return segments_; }
  void setSegments(const QVariantList &segments);

  QColor color() const { return color_; }
  void setColor(const QColor &color);
  qreal lineWidth() const { return lineWidth_; }
  void setLineWidth(qreal width);

  qreal centerLatitude() const { return centerLatitude_; }
  void setCenterLatitude(qreal value);
  qreal centerLongitude() const { return centerLongitude_; }
  void setCenterLongitude(qreal value);
  qreal zoom() const { return zoom_; }
  void setZoom(qreal value);

  int geometryRebuilds() const { return geometryRebuilds_; }
  int matrixUpdates() const { return matrixUpdates_; }

 signals:
  void segmentsChanged();
  void styleChanged();
  void cameraChanged();
  void zoomChanged();

 protected:
  QSGNode *updatePaintNode(QSGNode *node, UpdatePaintNodeData *data) override;

 private:
  void markGeometryDirty();
  void markStyleDirty();
  void markCameraDirty();

  QVariantList segments_;
  QColor color_{QColor(0, 0, 204, 160)};
  qreal lineWidth_ = 1.5;
  qreal centerLatitude_ = 0.0;
  qreal centerLongitude_ = 0.0;
  qreal zoom_ = 6.0;

  bool geometryDirty_ = true;
  bool styleDirty_ = true;
  bool cameraDirty_ = true;
  int geometryRebuilds_ = 0;
  int matrixUpdates_ = 0;
};
