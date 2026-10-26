#version 440

layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;
layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    float blend;
    float outputMode;
};
layout(binding = 1) uniform sampler2D u0;
layout(binding = 2) uniform sampler2D v0;
layout(binding = 3) uniform sampler2D u1;
layout(binding = 4) uniform sampler2D v1;

float decodeField(sampler2D field, ivec2 cell) {
    // Two opaque RGB texels per float; never filter these encoded bytes.
    uvec3 low = uvec3(round(texelFetch(field, ivec2(cell.x * 2, cell.y), 0).rgb * 255.0));
    uint high = uint(round(texelFetch(field, ivec2(cell.x * 2 + 1, cell.y), 0).r * 255.0));
    return uintBitsToFloat(low.r | (low.g << 8) | (low.b << 16) | (high << 24));
}

vec2 vectorAt(ivec2 cell) {
    // Do not let a NaN in the unused endpoint erase an exact-time sample.
    if (blend <= 0.0)
        return vec2(decodeField(u0, cell), decodeField(v0, cell));
    if (blend >= 1.0)
        return vec2(decodeField(u1, cell), decodeField(v1, cell));
    vec2 a = vec2(decodeField(u0, cell), decodeField(v0, cell));
    vec2 b = vec2(decodeField(u1, cell), decodeField(v1, cell));
    return a + (b - a) * blend;
}

void main() {
    ivec2 size = textureSize(u0, 0) / ivec2(2, 1);
    vec2 point = clamp(qt_TexCoord0 * vec2(size) - 0.5, vec2(0), vec2(size - 1));
    ivec2 base = ivec2(floor(point));
    vec2 delta = fract(point);
    vec2 uv = vec2(0);
    for (int y = 0; y < 2; ++y) {
        for (int x = 0; x < 2; ++x) {
            float weight = (x == 0 ? 1.0 - delta.x : delta.x) *
                           (y == 0 ? 1.0 - delta.y : delta.y);
            // Match the sampler's strict missing-corner policy.
            if (weight <= 1e-12) continue;
            vec2 value = vectorAt(min(base + ivec2(x, y), size - 1));
            if (any(isnan(value)) || any(isinf(value))) {
                fragColor = vec4(0);
                return;
            }
            uv += value * weight;
        }
    }
    // Diagnostic modes let native GPU tests inspect interpolated U/V or speed
    // through an ordinary RGBA8 framebuffer. They are not forecast encoding.
    if (outputMode > 1.5) {
        fragColor = vec4(vec3(length(uv) / 128.0), 1) * qt_Opacity;
    } else if (outputMode > 0.5) {
        fragColor = vec4((uv + 128.0) / 256.0, 0, 1) * qt_Opacity;
    } else {
        // A continuous wind-speed wash beneath the barbs. Keep the existing
        // speed ramp translucent so chart and other map layers show through.
        float speed = clamp(length(uv) / 32.0, 0.0, 1.0);
        vec3 color = vec3(speed, 1.0 - abs(2.0 * speed - 1.0), 1.0 - speed);
        fragColor = vec4(color, 0.35) * qt_Opacity;
    }
}
