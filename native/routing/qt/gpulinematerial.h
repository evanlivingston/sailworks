#pragma once

#include <QMatrix4x4>
#include <QSGMaterial>
#include <QSGMaterialShader>
#include <QSGRendererInterface>

// Scene-graph material for the constant-screen-width line item.
//
// The vertex shader receives one quad per segment, already in normalised
// Web-Mercator world units, and offsets each vertex sideways by half the stroke
// width in screen pixels. That keeps the stroke a constant pixel width at every
// zoom without rebuilding geometry: zoom is a scale uniform, pan is a matrix
// translation.
//
// Uniform block layout (std140, binding 0):
//   mat4  u_matrix        local pixel space -> clip space
//   float u_scale         world units -> logical pixels (256 * 2^zoom)
//   float u_dpr           device pixel ratio, used for the 1-device-pixel
//                         minimum stroke and the antialias fringe
//   float u_width_scale   user line-width multiplier, no geometry rebuild
//   float u_pad           std140 tail padding
class GpuLineShader : public QSGMaterialShader {
 public:
  GpuLineShader();

  bool updateUniformData(RenderState &state, QSGMaterial *newMaterial,
                         QSGMaterial *oldMaterial) override;

  static QString vertexShaderPath();
  static QString fragmentShaderPath();
};

class GpuLineMaterial : public QSGMaterial {
 public:
  GpuLineMaterial();

  QSGMaterialType *type() const override;
  QSGMaterialShader *createShader(QSGRendererInterface::RenderMode renderMode) const override;
  int compare(const QSGMaterial *other) const override;

  void setMatrix(const QMatrix4x4 &matrix) { matrix_ = matrix; }
  void setScale(float scale) { scale_ = scale; }
  void setDevicePixelRatio(float ratio) { devicePixelRatio_ = ratio; }
  void setWidthScale(float scale) { widthScale_ = scale; }

  const QMatrix4x4 &matrix() const { return matrix_; }
  float scale() const { return scale_; }
  float devicePixelRatio() const { return devicePixelRatio_; }
  float widthScale() const { return widthScale_; }

 private:
  QMatrix4x4 matrix_;
  float scale_ = 256.0f;
  float devicePixelRatio_ = 1.0f;
  float widthScale_ = 1.0f;
};
