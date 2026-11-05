#version 440
layout(location = 0) in vec4 qt_Vertex;
layout(location = 1) in vec2 qt_MultiTexCoord0;
layout(location = 0) out vec2 marker;
layout(location = 1) out vec2 localPoint;
layout(location = 2) out float directionDegrees;
layout(location = 3) out float waveHeight;
layout(location = 4) out float wavePeriod;
layout(location = 5) out float validWave;
layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    float blend;
    int nodeIdIndirection;
    int fieldColumns;
    int rowOffset;
    int meshRows;
    // Reference-local anchor compensation.  The selection anchors are encoded
    // for the camera that published them; the retained layer is placed in
    // shared map-content space, so a rebase shifts this offset instead of
    // re-uploading anchor textures.
    float anchorOffsetX;
    float anchorOffsetY;
    // Temporary zoom-only preview of the previous frame's retained anchors.
    // The old atlas was projected at another zoom; `previewScale` grows or
    // shrinks the anchor positions about the old frame's centre while the
    // symbol's local quad stays its fixed screen size.  Identity by default.
    float previewScale;
    float previewOffsetX;
    float previewOffsetY;
};
// One float per sampler, two texels per cell, exactly like the wind path.
// A wave arrow needs three scalars at each of the two forecast times, so six
// samplers: u0..u2 hold wave_height/wave_period/wave_direction at time A and
// u3..u5 the same three at time B.
layout(binding = 1) uniform sampler2D u0;
layout(binding = 2) uniform sampler2D u1;
layout(binding = 3) uniform sampler2D u2;
layout(binding = 4) uniform sampler2D u3;
layout(binding = 5) uniform sampler2D u4;
layout(binding = 6) uniform sampler2D u5;
layout(binding = 7) uniform sampler2D anchorX;
layout(binding = 8) uniform sampler2D anchorY;
layout(binding = 9) uniform sampler2D nodeId;

uint fieldBits(sampler2D field, ivec2 cell) {
    uvec3 low = uvec3(round(texelFetch(field, ivec2(cell.x * 2, cell.y), 0).rgb * 255.0));
    uint high = uint(round(texelFetch(field, ivec2(cell.x * 2 + 1, cell.y), 0).r * 255.0));
    return low.r | (low.g << 8) | (low.b << 16) | (high << 24);
}

float decodeField(sampler2D field, ivec2 cell) {
    return uintBitsToFloat(fieldBits(field, cell));
}

bool finiteBits(uint bits) {
    return (bits & 0x7f800000u) != 0x7f800000u;
}

// The raw bits of the interpolated value feed the NaN check and the bearing
// reconstruction, so keep the vector form alongside the angle.
vec2 bearingVector(float degreesValue) {
    float angle = radians(degreesValue);
    return vec2(cos(angle), sin(angle));
}

void main() {
    ivec2 size = textureSize(u0, 0) / ivec2(2, 1);
    // With the node-id indirection the mesh cell indexes the selection, and
    // the selection names the node of the resident field.  Without it the
    // mesh cell indexes the field directly, as the historical path does.
    ivec2 grid = nodeIdIndirection == 1 ? textureSize(nodeId, 0) / ivec2(2, 1) : size;
    // A 2N-1 mesh has two vertices per symbol axis. Every other mesh cell
    // connects neighboring quads; its fractional marker discards that gap.
    ivec2 vertex = ivec2(round(qt_MultiTexCoord0 * vec2(ivec2(grid.x, meshRows) * 2 - 1)));
    ivec2 cell = vertex / 2;
    cell.y += rowOffset;
    marker = vec2(cell);
    localPoint = (vec2(vertex % 2) * 2.0 - 1.0) * 32.0;
    // The node atlas is indexed by the field node id, so a pan that only
    // permutes mesh slots reuses one anchor texture.  Without indirection the
    // historical path still anchors each mesh cell directly.
    ivec2 field = cell;
    int id = -1;
    if (nodeIdIndirection == 1) {
        id = int(decodeField(nodeId, cell));
        field = ivec2(max(id, 0) % max(fieldColumns, 1), max(id, 0) / max(fieldColumns, 1));
    }
    vec2 anchor = vec2(decodeField(anchorX, field), decodeField(anchorY, field));
    uint heightA = fieldBits(u0, field);
    uint periodA = fieldBits(u1, field);
    uint directionA = fieldBits(u2, field);
    uint heightB = fieldBits(u3, field);
    uint periodB = fieldBits(u4, field);
    uint directionB = fieldBits(u5, field);
    if (nodeIdIndirection == 1 && id < 0) {
        // An unselected cell carries no symbol: a missing wave discards it
        // exactly like a missing datum, and never reaches the fragment stage.
        heightA = 0x7fc00000u;
        periodA = heightA;
        directionA = heightA;
        heightB = heightA;
        periodB = heightA;
        directionB = heightA;
    }
    bool valid = blend <= 0.0
               ? (finiteBits(heightA) && finiteBits(periodA) && finiteBits(directionA))
               : (blend >= 1.0
                  ? (finiteBits(heightB) && finiteBits(periodB) && finiteBits(directionB))
                  : (finiteBits(heightA) && finiteBits(periodA) && finiteBits(directionA)
                     && finiteBits(heightB) && finiteBits(periodB) && finiteBits(directionB)));
    validWave = valid ? 1.0 : 0.0;
    // Never pass NaNs through interpolation or fragment arithmetic. Some GPU
    // backends optimize floating-point NaN checks under fast-math rules.
    directionDegrees = 0.0;
    waveHeight = 0.0;
    wavePeriod = 0.0;
    if (valid) {
        float heightLo = uintBitsToFloat(heightA);
        float periodLo = uintBitsToFloat(periodA);
        float heightHi = uintBitsToFloat(heightB);
        float periodHi = uintBitsToFloat(periodB);
        if (blend <= 0.0) {
            waveHeight = heightLo;
            wavePeriod = periodLo;
        } else if (blend >= 1.0) {
            waveHeight = heightHi;
            wavePeriod = periodHi;
        } else {
            waveHeight = heightLo + (heightHi - heightLo) * blend;
            wavePeriod = periodLo + (periodHi - periodLo) * blend;
        }
        // Interpolate the bearing as a vector so a blend across the 0/360
        // seam takes the short way round instead of sweeping through 180
        // degrees. The quad is one cell, so the fragment stage receives a
        // single reconstructed bearing per symbol.
        vec2 aVector = bearingVector(uintBitsToFloat(directionA));
        vec2 bVector = bearingVector(uintBitsToFloat(directionB));
        vec2 bearing;
        if (blend <= 0.0) {
            bearing = aVector;
        } else if (blend >= 1.0) {
            bearing = bVector;
        } else {
            bearing = aVector + (bVector - aVector) * blend;
            // Exactly opposite bearings cancel; keep time A's direction
            // rather than inventing one from a zero vector.
            if (dot(bearing, bearing) < 1e-6) bearing = aVector;
        }
        directionDegrees = mod(degrees(atan(bearing.y, bearing.x)) + 360.0, 360.0);
    }
    vec2 offset = vec2(anchorOffsetX, anchorOffsetY);
    // The zoom preview scales the anchor POSITION only.  localPoint is added
    // after the transform, so a symbol keeps its 32 px screen quad whether the
    // preview zooms in or out.  Defaults (1, 0, 0) are the identity.
    vec2 position = previewScale * anchor + vec2(previewOffsetX, previewOffsetY);
    gl_Position = qt_Matrix * vec4(position + offset + localPoint, 0.0, 1.0);
}
