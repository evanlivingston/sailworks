"""Custom scene-graph shader for interpolating between GRIB forecast hours.

Samples two U/V time-layer textures, interpolates U/V by fractional hour,
computes wind direction/speed, and maps to a color ramp. Runs entirely on the
GPU, so scrubbing the forecast time is just changing a uniform float — no CPU
resampling, no texture upload per frame.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray
from PySide6.QtQuick import QSGMaterial, QSGMaterialShader

# Vertex shader: pass through texture coordinates
_VERT_SHADER = b"""
#version 100
attribute highp vec2 vertex;
attribute highp vec2 texCoord;
uniform highp mat4 matrix;
varying highp vec2 coord;
void main() {
    coord = texCoord;
    gl_Position = matrix * vec4(vertex, 0.0, 1.0);
}
"""

# Fragment shader: interpolate between two time layers, compute wind vector,
# map speed to color ramp: blue=calm, green=moderate, red=strong.
_FRAG_SHADER = b"""
#version 100
uniform lowp sampler2D u_tex_u0;
uniform lowp sampler2D u_tex_v0;
uniform lowp sampler2D u_tex_u1;
uniform lowp sampler2D u_tex_v1;
uniform lowp float u_blend;
varying highp vec2 coord;
void main() {
    lowp vec4 u0 = texture2D(u_tex_u0, coord);
    lowp vec4 v0 = texture2D(u_tex_v0, coord);
    lowp vec4 u1 = texture2D(u_tex_u1, coord);
    lowp vec4 v1 = texture2D(u_tex_v1, coord);

    lowp float u = mix(u0.r, u1.r, u_blend) * 2.0 - 1.0;
    lowp float v = mix(v0.r, v1.r, u_blend) * 2.0 - 1.0;

    lowp float speed = sqrt(u * u + v * v);
    speed = clamp(speed, 0.0, 1.0);

    lowp vec3 color;
    if (speed < 0.5) {
        color = vec3(0.2, 0.5, 1.0);
    } else if (speed < 0.8) {
        color = mix(vec3(0.2, 0.5, 1.0), vec3(0.0, 1.0, 0.5), (speed - 0.5) / 0.3);
    } else {
        color = mix(vec3(0.0, 1.0, 0.5), vec3(1.0, 0.0, 0.0), (speed - 0.8) / 0.2);
    }

    lowp float alpha = mix(u0.a, v1.a, u_blend);
    gl_FragColor = vec4(color, alpha);
}
"""


class GPUMaterialShader(QSGMaterialShader):
    """Shader that blends two U/V time layers and renders wind speed as color."""

    def __init__(self):
        super().__init__()

    def vertexShader(self) -> QByteArray:  # noqa: N802 - Qt virtual name
        return QByteArray(_VERT_SHADER)

    def fragmentShader(self) -> QByteArray:  # noqa: N802 - Qt virtual name
        return QByteArray(_FRAG_SHADER)

    def updateState(  # noqa: N802 - Qt virtual name
        self,
        state,
        newMaterial: QSGMaterial,  # noqa: N803 - Qt virtual name
        oldMaterial: QSGMaterial,  # noqa: N803 - Qt virtual name
    ) -> None:
        program = self.program()
        material = newMaterial  # type: GPUMaterial

        # Bind textures to specific texture units
        if material.tex_u0 is not None:
            program.setUniformValue("u_tex_u0", 0)
            state.bindTexture(0, material.tex_u0.texture)
        if material.tex_v0 is not None:
            program.setUniformValue("u_tex_v0", 1)
            state.bindTexture(1, material.tex_v0.texture)
        if material.tex_u1 is not None:
            program.setUniformValue("u_tex_u1", 2)
            state.bindTexture(2, material.tex_u1.texture)
        if material.tex_v1 is not None:
            program.setUniformValue("u_tex_v1", 3)
            state.bindTexture(3, material.tex_v1.texture)

        program.setUniformValue(state, "u_blend", material.blend)

    def uniformCount(self) -> int:  # noqa: N802 - Qt virtual name
        return 1

    def uniformName(self, index: int) -> QByteArray:  # noqa: N802 - Qt virtual
        if index == 0:
            return QByteArray(b"u_blend")
        return QByteArray()


class GPUMaterial(QSGMaterial):
    """Material that carries four U/V textures (two time layers) and a blend factor."""

    def __init__(self):
        super().__init__()
        self.tex_u0 = None
        self.tex_v0 = None
        self.tex_u1 = None
        self.tex_v1 = None
        self.blend: float = 0.0

    def type(self) -> int:
        return QSGMaterial.registerType()

    def compare(self, other: QSGMaterial) -> int:
        # Always return 1 to force state re-evaluation when textures change
        return 1

    def createShader(self) -> QSGMaterialShader:  # noqa: N802 - Qt virtual
        return GPUMaterialShader()

    def set_textures(self, tex_u0, tex_v0, tex_u1, tex_v1, blend: float) -> None:
        self.tex_u0 = tex_u0
        self.tex_v0 = tex_v0
        self.tex_u1 = tex_u1
        self.tex_v1 = tex_v1
        self.blend = blend
