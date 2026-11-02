#version 440
layout(location = 0) in float marker;
layout(location = 0) out vec4 fragColor;
layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    int firstSegment;
    int segmentCount;
    float lineWidth;
    vec4 lineColor;
    vec2 viewportSize;
    vec2 cameraCenter;
    float cameraZoom;
    int arrowMode;
};
void main() {
    if (abs(marker - round(marker)) > 0.00001) discard;
    fragColor = lineColor * qt_Opacity;
}
