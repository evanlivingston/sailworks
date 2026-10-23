#version 440
layout(location = 0) in vec4 qt_Vertex;
layout(location = 1) in vec2 qt_MultiTexCoord0;
layout(location = 0) out float marker;
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
layout(binding = 1) uniform sampler2D segmentTexture;
float byteAt(int index, int row) {
    vec3 texel = texelFetch(segmentTexture, ivec2(index / 3, row), 0).rgb;
    return floor(texel[index % 3] * 255.0 + 0.5);
}
float coordAt(int index, int row) { return (byteAt(index,row)*256.0+byteAt(index+1,row))/65535.0; }
void main() {
    int perSegment = arrowMode == 0 ? 2 : 4;
    int vertexY = int(round(qt_MultiTexCoord0.y * float(segmentCount * perSegment - 1)));
    int localSegment = vertexY / perSegment;
    int part = arrowMode == 0 ? 0 : (vertexY % perSegment) / 2;
    int endpoint = vertexY % 2;
    int row = firstSegment + localSegment;
    vec2 start = vec2(coordAt(0,row),coordAt(2,row));
    vec2 finish = vec2(coordAt(4,row),coordAt(6,row));
    float dx = finish.x-start.x;
    dx -= round(dx);
    start.x = cameraCenter.x + (start.x-cameraCenter.x-round(start.x-cameraCenter.x));
    finish.x = start.x + dx;
    float worldPixels = 256.0 * exp2(cameraZoom);
    vec2 a = (start-cameraCenter)*worldPixels + viewportSize*0.5;
    vec2 b = (finish-cameraCenter)*worldPixels + viewportSize*0.5;
    vec2 delta = b-a;
    float lengthPx = length(delta);
    vec2 direction = lengthPx > 1e-6 ? delta/lengthPx : vec2(1.0,0.0);
    vec2 position;
    vec2 normal;
    if (arrowMode == 0) {
        position = endpoint == 0 ? a : b;
        normal = lengthPx > 1e-6 ? vec2(-direction.y,direction.x) : vec2(0.0,1.0);
    } else {
        vec2 branchNormal = vec2(-direction.y,direction.x);
        vec2 branchEnd = b - 5.0*direction + (part == 0 ? 2.5 : -2.5)*branchNormal;
        position = endpoint == 0 ? b : branchEnd;
        vec2 branchDirection = lengthPx > 1e-6 ? normalize(branchEnd-b) : vec2(1.0,0.0);
        normal = vec2(-branchDirection.y,branchDirection.x);
    }
    float side = qt_MultiTexCoord0.x < 0.5 ? -1.0 : 1.0;
    marker = float(vertexY / 2);
    gl_Position = qt_Matrix * vec4(position + normal*side*lineWidth*0.5,0.0,1.0);
}
