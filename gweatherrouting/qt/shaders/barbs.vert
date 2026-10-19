#version 440
layout(location = 0) in vec4 qt_Vertex;
layout(location = 1) in vec2 qt_MultiTexCoord0;
layout(location = 0) out vec2 marker;
layout(location = 1) out vec2 localPoint;
layout(location = 2) out vec2 wind;
layout(location = 3) out float validWind;
layout(location = 4) out float calmShaftY;
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

uint fieldBits(sampler2D field, ivec2 cell) {
    uvec3 low = uvec3(round(texelFetch(field, ivec2(cell.x * 2, cell.y), 0).rgb * 255.0));
    uint high = uint(round(texelFetch(field, ivec2(cell.x * 2 + 1, cell.y), 0).r * 255.0));
    return low.r | (low.g << 8) | (low.b << 16) | (high << 24);
}

float decodeField(sampler2D field, ivec2 cell) {
    return uintBitsToFloat(fieldBits(field, cell));
}

bool finiteVector(uvec2 bits) {
    return all(notEqual(bits & uvec2(0x7f800000u), uvec2(0x7f800000u)));
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
    uvec2 aBits = uvec2(fieldBits(u0, cell), fieldBits(v0, cell));
    uvec2 bBits = uvec2(fieldBits(u1, cell), fieldBits(v1, cell));
    bool valid = blend <= 0.0 ? finiteVector(aBits)
               : (blend >= 1.0 ? finiteVector(bBits)
                  : finiteVector(aBits) && finiteVector(bBits));
    validWind = valid ? 1.0 : 0.0;
    // Never pass NaNs through interpolation or fragment arithmetic. Some GPU
    // backends optimize floating-point NaN checks under fast-math rules.
    wind = vec2(0.0);
    calmShaftY = 1.0;
    if (valid) {
        vec2 a = uintBitsToFloat(aBits);
        vec2 b = uintBitsToFloat(bBits);
        wind = blend <= 0.0 ? a : (blend >= 1.0 ? b : a + (b - a) * blend);
        // Preserve the CPU atan2 convention for signed-zero calm endpoints.
        // Carry a finite direction, not -0.0, through raster interpolation.
        uint northBits = blend <= 0.0 ? aBits.y
                       : (blend >= 1.0 ? bBits.y : floatBitsToUint(wind.y));
        calmShaftY = (northBits & 0x80000000u) != 0u ? -1.0 : 1.0;
    }
    gl_Position = qt_Matrix * vec4(anchor + localPoint, 0.0, 1.0);
}
