#include "gpulinematerial.h"

#include <QtCore/QByteArray>
#include <cstddef>
#include <cstring>

namespace {

struct LineUniforms {
  float matrix[16];
  float scale;
  float devicePixelRatio;
  float widthScale;
  float opacity;
  float pad[4];  // std140 rounds the block up to 96 bytes
};

static_assert(offsetof(LineUniforms, pad) == 80,
              "std140 header before padding is 80 bytes");

}  // namespace

QString GpuLineShader::vertexShaderPath() {
  return QStringLiteral(":/sailworks/shaders/line.vert.qsb");
}

QString GpuLineShader::fragmentShaderPath() {
  return QStringLiteral(":/sailworks/shaders/line.frag.qsb");
}

GpuLineShader::GpuLineShader() {
  setShaderFileName(VertexStage, vertexShaderPath());
  setShaderFileName(FragmentStage, fragmentShaderPath());
}

bool GpuLineShader::updateUniformData(RenderState &state,
                                      QSGMaterial *newMaterial,
                                      QSGMaterial *oldMaterial) {
  Q_UNUSED(oldMaterial);
  auto *material = static_cast<GpuLineMaterial *>(newMaterial);
  if (material == nullptr) return false;

  QByteArray *data = state.uniformData();
  constexpr int kUsed = static_cast<int>(offsetof(LineUniforms, pad));
  if (data->size() < kUsed) return false;

  LineUniforms uniforms{};
  std::memcpy(uniforms.matrix, material->matrix().constData(),
              sizeof(uniforms.matrix));
  uniforms.scale = material->scale();
  uniforms.devicePixelRatio = material->devicePixelRatio();
  uniforms.widthScale = material->widthScale();
  uniforms.opacity = state.opacity();
  std::memcpy(data->data(), &uniforms, sizeof(uniforms));
  return true;
}

GpuLineMaterial::GpuLineMaterial() {
  // Separate quads never join, so alpha blending is the only pipeline state
  // the material needs; the matrix is a translation plus projection. Batching
  // is opted out of because the quads are already in one geometry and the
  // vertex shader has no rewriter-inserted batch attribute.
  setFlag(Blending | RequiresFullMatrixExceptTranslate | NoBatching);
}

QSGMaterialType *GpuLineMaterial::type() const {
  static QSGMaterialType type;
  return &type;
}

QSGMaterialShader *GpuLineMaterial::createShader(
    QSGRendererInterface::RenderMode renderMode) const {
  Q_UNUSED(renderMode);
  return new GpuLineShader();
}

int GpuLineMaterial::compare(const QSGMaterial *other) const {
  // A single material instance is shared by every node in the item, so any two
  // nodes of this type are equivalent after the uniform upload above.
  return other == this ? 0 : 1;
}
