#version 440
layout(location = 0) out vec4 fragColor;
layout(location = 0) flat in float speed;
layout(location = 1) flat in float alpha;
vec3 bucketColor(float value) {
    const vec3 colors[15] = vec3[15](
        vec3(0,0,204), vec3(0,102,255), vec3(0,255,255), vec3(0,255,102),
        vec3(0,204,0), vec3(102,255,51), vec3(204,255,51), vec3(255,255,102),
        vec3(255,204,0), vec3(255,153,0), vec3(255,102,0), vec3(255,51,0),
        vec3(255,0,0), vec3(204,102,0), vec3(204,0,0));
    return colors[clamp(int(floor(value / 2.0)), 0, 14)] / 255.0;
}
void main() {
    fragColor = vec4(bucketColor(speed) * alpha, alpha);
}
