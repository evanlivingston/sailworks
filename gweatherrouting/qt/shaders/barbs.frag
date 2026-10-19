#version 440
layout(location = 0) in vec2 marker;
layout(location = 1) in vec2 localPoint;
layout(location = 2) in vec2 wind;
layout(location = 3) in float validWind;
layout(location = 4) in float calmShaftY;
layout(location = 0) out vec4 fragColor;
layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    float blend;
};

float segment(vec2 p, vec2 a, vec2 b) {
    vec2 axis = b - a;
    float along = dot(p - a, axis) / dot(axis, axis);
    // Butt caps matching the CPU barb painter, with a one-pixel stroke.
    float across = abs((p.x - a.x) * axis.y - (p.y - a.y) * axis.x) / length(axis);
    return max(across - 0.5, max(-along, along - 1.0) * length(axis));
}

vec3 bucketColor(float speed) {
    const vec3 colors[15] = vec3[15](
        vec3(0,0,204), vec3(0,102,255), vec3(0,255,255), vec3(0,255,102),
        vec3(0,204,0), vec3(102,255,51), vec3(204,255,51), vec3(255,255,102),
        vec3(255,204,0), vec3(255,153,0), vec3(255,102,0), vec3(255,51,0),
        vec3(255,0,0), vec3(204,102,0), vec3(204,0,0));
    return colors[clamp(int(floor(speed / 2.0)), 0, 14)] / 255.0;
}

void main() {
    if (validWind < 0.999) discard;
    if (any(greaterThan(abs(marker - round(marker)), vec2(0.00001)))) discard;
    if (any(isnan(wind)) || any(isinf(wind))) discard;
    float speed = length(wind);
    // Same true-east/north -> meteorological screen orientation as CPU.
    vec2 shaft = speed > 0.0 ? vec2(-wind.x, wind.y) / speed : vec2(0,calmShaftY);
    vec2 perpendicular = vec2(-shaft.y, shaft.x);
    vec2 p = vec2(dot(localPoint, perpendicular), dot(localPoint, shaft));
    float distanceToInk = segment(p, vec2(0), vec2(0,20));
    float knots = speed * 1.94384;
    if (knots < 2.5) {
        distanceToInk = min(distanceToInk, abs(length(p) - 2.0) - 0.5);
    } else {
        float tip = 20.0;
        // Bound pathological input; ordinary weather never approaches 500 kt.
        for (int i = 0; i < 10 && knots >= 47.5; ++i) {
            vec2 q = p - vec2(0, tip);
            // Filled triangle: (0,0), (8,0), (0,-3.5).
            float triangle = max(-q.x, max(q.y, (3.5*q.x - 8.0*q.y - 28.0) / sqrt(76.25)));
            distanceToInk = min(distanceToInk, triangle);
            tip -= 3.5;
            knots -= 50.0;
        }
        for (int i = 0; i < 5 && knots >= 7.5; ++i) {
            distanceToInk = min(distanceToInk, segment(p, vec2(0,tip), vec2(8,tip)));
            tip -= 3.5;
            knots -= 10.0;
        }
        if (knots >= 2.5)
            distanceToInk = min(distanceToInk, segment(p, vec2(0,tip), vec2(4,tip)));
    }
    float aa = max(fwidth(distanceToInk), 0.001);
    float alpha = (1.0 - smoothstep(-aa * 0.5, aa * 0.5, distanceToInk)) * qt_Opacity;
    fragColor = vec4(bucketColor(speed) * alpha, alpha);
}
