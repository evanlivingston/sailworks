#version 440
struct Segment {
    vec4 endpoints;
    vec4 metadata;
};
layout(std430, binding = 0) readonly buffer TraceOutput {
    Segment segments[];
};
layout(std430, binding = 2) readonly buffer PreviousTrace {
    Segment previousSegments[];
};
layout(std430, binding = 3) readonly buffer CurrentVisibility { uint currentVisible[]; };
layout(std430, binding = 4) readonly buffer PreviousVisibility { uint previousVisible[]; };
layout(std140, binding = 1) uniform RenderParameters {
    float viewWidth;
    float viewHeight;
    float opacity;
    float lineWidth;
    float phase;
    float transition;
    float viewScale;
    float offsetX;
    float offsetY;
    float worldWidth;
};
layout(location = 0) flat out float speed;
layout(location = 1) flat out float alpha;
layout(location = 2) flat out float flow;
void main() {
    int vertex = gl_VertexIndex;
    int segmentIndex = vertex / 6;
    Segment segment = segments[segmentIndex];
    Segment previous = previousSegments[segmentIndex];
    int part = vertex % 6;
    bool arrowVertex = part >= 2;
    bool currentValid = segment.metadata.z >= 0.5;
    bool previousValid = previous.metadata.z >= 0.5;
    currentValid = currentValid && currentVisible[segmentIndex / 320] != 0u;
    previousValid = previousValid && previousVisible[segmentIndex / 320] != 0u;
    if (!currentValid && !previousValid) {
        gl_Position = vec4(2.0, 2.0, 0.0, 1.0);
        return;
    }
    float visibility = 1.0;
    if (!currentValid) {
        segment = previous;
        visibility = 1.0 - transition;
    } else if (!previousValid) {
        visibility = transition;
    } else {
        segment.endpoints = mix(previous.endpoints, segment.endpoints, transition);
        segment.metadata.x = mix(previous.metadata.x, segment.metadata.x, transition);
    }
    if (arrowVertex && segment.metadata.y < 0.5) {
        gl_Position = vec4(2.0, 2.0, 0.0, 1.0);
        return;
    }
    vec2 a = segment.endpoints.xy * viewScale + vec2(offsetX, offsetY);
    vec2 b = segment.endpoints.zw * viewScale + vec2(offsetX, offsetY);
    if (worldWidth > 0.0) {
        float shift = round((viewWidth * 0.5 - (a.x + b.x) * 0.5) / worldWidth) * worldWidth;
        a.x += shift;
        b.x += shift;
    }
    vec2 point;
    if (part < 2) {
        point = part == 0 ? a : b;
    } else {
        vec2 axis = normalize(b - a);
        vec2 normal = vec2(-axis.y, axis.x);
        vec2 base = b - axis * 5.0;
        if (part < 4)
            point = part == 2 ? b : base + normal * 2.5;
        else
            point = part == 4 ? b : base - normal * 2.5;
    }
    gl_Position = vec4(point.x / viewWidth * 2.0 - 1.0,
                       1.0 - point.y / viewHeight * 2.0, 0.0, 1.0);
    speed = segment.metadata.x;
    float along = float(segmentIndex % 320) * 0.25;
    float pulse = fract((along - phase) / 20.0);
    flow = pulse;
    alpha = opacity * visibility * (0.18 + 0.82 * smoothstep(0.0, 0.12, 1.0 - abs(pulse - 0.5) * 2.0));
}
