#version 440
layout(location = 0) in vec4 qt_Vertex;
layout(location = 1) in vec2 qt_MultiTexCoord0;
layout(location = 0) out vec2 marker;
layout(location = 1) out vec2 localPoint;
layout(location = 2) out vec2 wind;
layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    float blend;
};
layout(binding = 1) uniform sampler2D u0;
layout(binding = 2) uniform sampler2D v0;
layout(binding = 3) uniform sampler2D u1;
layout(binding = 4) uniform sampler2D v1;
layout(binding = 5) uniform sampler2D anchorX;
layout(binding = 6) uniform sampler2D anchorY;

float decodeField(sampler2D field, ivec2 cell) {
    uvec3 low = uvec3(round(texelFetch(field, ivec2(cell.x * 2, cell.y), 0).rgb * 255.0));
    uint high = uint(round(texelFetch(field, ivec2(cell.x * 2 + 1, cell.y), 0).r * 255.0));
    return uintBitsToFloat(low.r | (low.g << 8) | (low.b << 16) | (high << 24));
}

void main() {
    ivec2 size = textureSize(u0, 0) / ivec2(2, 1);
    // A 2N-1 mesh has two vertices per symbol axis. Every other mesh cell
    // connects neighboring quads; its fractional marker discards that gap.
    ivec2 vertex = ivec2(round(qt_MultiTexCoord0 * vec2(size * 2 - 1)));
    ivec2 cell = vertex / 2;
    marker = vec2(cell);
    localPoint = (vec2(vertex % 2) * 2.0 - 1.0) * 32.0;
    vec2 anchor = vec2(decodeField(anchorX, cell), decodeField(anchorY, cell));
    vec2 a = vec2(decodeField(u0, cell), decodeField(v0, cell));
    vec2 b = vec2(decodeField(u1, cell), decodeField(v1, cell));
    wind = blend <= 0.0 ? a : (blend >= 1.0 ? b : a + (b - a) * blend);
    gl_Position = qt_Matrix * vec4(anchor + localPoint, 0.0, 1.0);
}
