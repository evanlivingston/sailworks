#version 440
struct Segment {
    vec4 endpoints;
    vec4 metadata;
};
layout(std430, binding = 0) readonly buffer TraceOutput {
    Segment segments[];
};
layout(std140, binding = 1) uniform RenderParameters {
    float viewWidth;
    float viewHeight;
    float opacity;
    float lineWidth;
};
layout(location = 0) flat out float speed;
layout(location = 1) flat out float alpha;
void main() {
    int vertex = gl_VertexIndex;
    Segment segment = segments[vertex / 6];
    int part = vertex % 6;
    bool arrowVertex = part >= 2;
    if (segment.metadata.z < 0.5
        || (arrowVertex && segment.metadata.y < 0.5)) {
        gl_Position = vec4(2.0, 2.0, 0.0, 1.0);
        return;
    }
    vec2 a = segment.endpoints.xy;
    vec2 b = segment.endpoints.zw;
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
    alpha = opacity;
}
