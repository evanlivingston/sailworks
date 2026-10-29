#version 440
layout(location = 0) out vec4 fragColor;
layout(location = 0) flat in float speed;
layout(location = 1) flat in float alpha;
layout(location = 2) flat in float flow;
vec3 bucketColor(float value) {
    const vec3 colors[15] = vec3[15](
        vec3(0,0,204), vec3(0,102,255), vec3(0,255,255), vec3(0,255,102),
        vec3(0,204,0), vec3(102,255,51), vec3(204,255,51), vec3(255,255,102),
        vec3(255,204,0), vec3(255,153,0), vec3(255,102,0), vec3(255,51,0),
        vec3(255,0,0), vec3(204,102,0), vec3(204,0,0));
    return colors[clamp(int(floor(value / 2.0)), 0, 14)] / 255.0;
}
void main() {
    float arrow = 1.0 - smoothstep(0.015, 0.06, abs(flow - 0.5));
    vec3 color = mix(bucketColor(speed), vec3(1.0), arrow * 0.75);
    fragColor = vec4(color * alpha, alpha);
}
