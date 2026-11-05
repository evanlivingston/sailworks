#version 440
layout(location = 0) in vec2 marker;
layout(location = 1) in vec2 localPoint;
layout(location = 2) in float directionDegrees;
layout(location = 3) in float waveHeight;
layout(location = 4) in float wavePeriod;
layout(location = 5) in float validWave;
layout(location = 0) out vec4 fragColor;
layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    float blend;
};

// Wave arrow geometry and period mapping. Keep these in sync with
// windpainter.wave_arrow_length() and its constants; the test parses them.
const float WAVE_PERIOD_MIN_S = 4.0;
const float WAVE_PERIOD_MAX_S = 20.0;
const float WAVE_ARROW_SHAFT_MIN = 8.0;
const float WAVE_ARROW_SHAFT_MAX = 20.0;
const float WAVE_ARROW_HEAD_LEN = 5.0;
const float WAVE_ARROW_HEAD_SPREAD = 3.0;

float segment(vec2 p, vec2 a, vec2 b) {
    vec2 axis = b - a;
    float along = dot(p - a, axis) / dot(axis, axis);
    // Butt caps matching the CPU barb painter, with a one-pixel stroke.
    float across = abs((p.x - a.x) * axis.y - (p.y - a.y) * axis.x) / length(axis);
    return max(across - 0.5, max(-along, along - 1.0) * length(axis));
}

vec3 waveColor(float height) {
    // Same stops as sailworks.common.WAVE_BUCKET_COLORS and wave.frag, at
    // 0.5 m intervals from 0 m to 4 m and above.
    const vec3 colors[9] = vec3[9](
        vec3(230,247,255), vec3(166,228,255), vec3(77,195,255), vec3(0,150,232),
        vec3(0,87,183), vec3(0,60,143), vec3(31,42,122), vec3(75,31,143),
        vec3(110,11,94));
    return colors[clamp(int(floor(height / 0.5)), 0, 8)] / 255.0;
}

float waveArrowLength(float period) {
    // A missing, zero or non-finite period keeps the minimum length, matching
    // wave_arrow_length() on the CPU: the direction is still visible.
    if (!(period > 0.0) || isinf(period)) return WAVE_ARROW_SHAFT_MIN;
    float fraction = (period - WAVE_PERIOD_MIN_S)
                   / (WAVE_PERIOD_MAX_S - WAVE_PERIOD_MIN_S);
    fraction = clamp(fraction, 0.0, 1.0);
    return WAVE_ARROW_SHAFT_MIN
         + fraction * (WAVE_ARROW_SHAFT_MAX - WAVE_ARROW_SHAFT_MIN);
}

void main() {
    if (validWave < 0.999) discard;
    if (any(greaterThan(abs(marker - round(marker)), vec2(0.00001)))) discard;
    if (any(isnan(vec3(directionDegrees, waveHeight, wavePeriod)))
        || any(isinf(vec3(directionDegrees, waveHeight, wavePeriod)))) discard;
    float angle = radians(directionDegrees);
    // Meteorological coming-from bearing in screen space: 0 deg points up
    // (screen -y) and 90 deg points east/right, exactly like the
    // (sin, -cos) shaft in windpainter.build_wave_geometry().
    vec2 shaft = vec2(sin(angle), -cos(angle));
    vec2 perpendicular = vec2(cos(angle), sin(angle));
    // Work in (across, along) space so the glyph is the same for every
    // bearing: the shaft runs from (0, 0) to (0, shaftLength).
    vec2 p = vec2(dot(localPoint, perpendicular), dot(localPoint, shaft));
    float shaftLength = waveArrowLength(wavePeriod);
    float distanceToInk = segment(p, vec2(0), vec2(0, shaftLength));
    // One chevron head and no feathers: a filled triangle with its tip at
    // (0, shaftLength) and base corners at (+/-spread, shaftLength - HEAD_LEN).
    // q.y is the distance back from the tip, so the half-width grows linearly.
    vec2 q = vec2(p.x, shaftLength - p.y);
    float head = max(abs(q.x) - WAVE_ARROW_HEAD_SPREAD * q.y / WAVE_ARROW_HEAD_LEN,
                     max(-q.y, q.y - WAVE_ARROW_HEAD_LEN));
    distanceToInk = min(distanceToInk, head);
    float aa = max(fwidth(distanceToInk), 0.001);
    float alpha = (1.0 - smoothstep(-aa * 0.5, aa * 0.5, distanceToInk)) * qt_Opacity;
    fragColor = vec4(waveColor(waveHeight) * alpha, alpha);
}
