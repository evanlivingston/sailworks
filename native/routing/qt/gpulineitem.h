#pragma once

#include <QtCore/QByteArray>
#include <QtCore/QPointF>
#include <QtCore/QRectF>
#include <QtCore/QVariantList>
#include <QtGui/QColor>
#include <QtQuick/QQuickItem>

class QSGGeometryNode;

// Retained routing line item rendered by a custom scene-graph material.
//
// Unlike RoutingRetainedItem, whose zoom path rebuilds one rectangle node per
// segment, this item allocates a single QSGGeometry: four vertices per segment
// carrying world position, normal, side, width, along distance, colour and the
// dash pattern.
// The vertex shader expands each quad to a constant screen-pixel width and
// passes the along distance through in screen pixels, so:
//
//   * pan       -> material matrix translation only
//   * zoom      -> scale uniform only (plus the matrix translation that
//                  depends on scale); geometry is untouched
//   * restyle   -> per-vertex colour and width attributes (geometry rebuild)
//
// Segment ends are square and independent: no joins, matching the existing
// renderer. A dash pattern is per segment: `dash_on + dash_off > 0` cuts the
// stroke into `dash_on` ink pixels and `dash_off` gap pixels measured along the
// stroke in logical screen pixels, so the pattern, like the width, is
// independent of zoom. Both zero means a solid stroke.
//
// Two payload forms upload the same geometry:
//
// `segments` accepts either a list of maps
//   {points: [[lat, lon], ...], color: "#rrggbb", width: pixels}
// or a bare list of [lat, lon] point pairs using the item defaults.  It is the
// prototype's QML path: one QVariant per point is parsed here.
//
// `segmentData` is the packed path the ENC scene publishes: one opaque
// QByteArray holding `N` records of twelve little-endian float32 values, no
// header and no padding (stride 48 bytes):
//
//   [0]  x0  longitude of the segment start, degrees
//   [1]  y0  latitude  of the segment start, degrees
//   [2]  x1  longitude of the segment end, degrees
//   [3]  y1  latitude  of the segment end, degrees
//   [4]  w   stroke width, screen pixels (the vertex shader expands it, so it
//            stays constant across zoom)
//   [5]  r   red    0..1
//   [6]  g   green  0..1
//   [7]  b   blue   0..1
//   [8]  a   alpha  0..1
//   [9]  dash_on    ink length in screen pixels, 0 with [10] for solid
//   [10] dash_off   gap length in screen pixels, 0 with [9] for solid
//   [11] phase      dash phase at the segment start, in normalised Web-Mercator
//                   world units: the cumulative length along the polyline or
//                   ring the segment belongs to. The vertex shader adds it to
//                   the segment's own along distance, so one dash pattern runs
//                   across consecutive segments instead of restarting at every
//                   joint. It is world units because `a_along` and `u_scale`
//                   are: a degree phase would put a seam back wherever the
//                   heading changes.
//
// The legacy nine-float (36-byte) record and the eleven-float (44-byte) record
// are still accepted when the payload is an exact multiple of their stride and
// of no larger one; both are drawn solid/no-phase. A trailing partial record is
// ignored, and when `segmentData` is non-empty it is the payload drawn:
// `segments` exists for the prototype and is not merged with it. 36 and 48
// share the 144-byte multiple and a payload divisible by both is read as the
// larger record, which is what the current publisher emits.
class GpuLineItem : public QQuickItem {
  Q_OBJECT
  Q_PROPERTY(QVariantList segments READ segments WRITE setSegments NOTIFY
                 segmentsChanged)
  Q_PROPERTY(
      QByteArray segmentData READ segmentData WRITE setSegmentData NOTIFY
          segmentDataChanged)
  Q_PROPERTY(int segmentCount READ segmentCount NOTIFY segmentDataChanged)
  Q_PROPERTY(QColor color READ color WRITE setColor NOTIFY styleChanged)
  Q_PROPERTY(qreal lineWidth READ lineWidth WRITE setLineWidth NOTIFY styleChanged)
  Q_PROPERTY(qreal lineWidthScale READ lineWidthScale WRITE setLineWidthScale
                 NOTIFY styleChanged)
  Q_PROPERTY(qreal centerLatitude READ centerLatitude WRITE setCenterLatitude
                 NOTIFY cameraChanged)
  Q_PROPERTY(qreal centerLongitude READ centerLongitude WRITE setCenterLongitude
                 NOTIFY cameraChanged)
  Q_PROPERTY(qreal zoom READ zoom WRITE setZoom NOTIFY zoomChanged)
  // Diagnostics for the standalone check only.
  Q_PROPERTY(int geometryRebuilds READ geometryRebuilds)
  Q_PROPERTY(int matrixUpdates READ matrixUpdates)
  Q_PROPERTY(bool shaderReady READ shaderReady)

 public:
  explicit GpuLineItem(QQuickItem *parent = nullptr);

  QVariantList segments() const { return segments_; }
  void setSegments(const QVariantList &segments);

  QByteArray segmentData() const { return segmentData_; }
  void setSegmentData(const QByteArray &data);
  int segmentCount() const { return segmentCount_; }

  QColor color() const { return color_; }
  void setColor(const QColor &color);

  qreal lineWidth() const { return lineWidth_; }
  void setLineWidth(qreal width);

  qreal lineWidthScale() const { return lineWidthScale_; }
  void setLineWidthScale(qreal scale);

  qreal centerLatitude() const { return centerLatitude_; }
  void setCenterLatitude(qreal value);

  qreal centerLongitude() const { return centerLongitude_; }
  void setCenterLongitude(qreal value);

  qreal zoom() const { return zoom_; }
  void setZoom(qreal value);

  int geometryRebuilds() const { return geometryRebuilds_; }
  int matrixUpdates() const { return matrixUpdates_; }
  bool shaderReady() const { return shaderReady_; }

 signals:
  void segmentsChanged();
  void segmentDataChanged();
  void styleChanged();
  void cameraChanged();
  void zoomChanged();

 protected:
  QSGNode *updatePaintNode(QSGNode *node, UpdatePaintNodeData *data) override;
  void geometryChange(const QRectF &newGeometry,
                      const QRectF &oldGeometry) override;

 private:
  void markGeometryDirty();
  void markStyleDirty();
  void markCameraDirty();
  void rebuildGeometry(QSGGeometryNode *node);

  QVariantList segments_;
  // Packed ENC payload: `N` records of twelve float32 values, stride 48 bytes
  // (see the class comment for the field order). A 44-byte record is read as
  // eleven floats (no phase) and a 36-byte one as nine floats (no dash, no
  // phase); both are drawn from the same vertex layout.
  QByteArray segmentData_;
  int segmentCount_ = 0;
  int recordStride_ = 48;
  bool legacyRecord_ = false;
  QColor color_{QColor(0, 0, 204, 180)};
  qreal lineWidth_ = 1.5;
  qreal lineWidthScale_ = 1.0;
  qreal centerLatitude_ = 0.0;
  qreal centerLongitude_ = 0.0;
  qreal zoom_ = 6.0;

  // Geometry-relative origin in normalised world units. Baking it keeps float
  // precision usable at high zoom while still allowing pan/zoom uniforms.
  QPointF origin_;

  bool geometryDirty_ = true;
  bool shaderReady_ = false;
  int geometryRebuilds_ = 0;
  int matrixUpdates_ = 0;
};
