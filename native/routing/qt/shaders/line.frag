#version 440

// Stroke colour with a one-pixel antialias fringe across the width. Segment
// ends stay square because each quad is independent. A per-segment dash pattern
// cuts the stroke into ink and gap runs measured along the segment's chain in
// screen pixels; when both dash lengths are zero the stroke is solid.

layout(location = 0) in vec4 v_color;
layout(location = 1) in float v_half_width;
layout(location = 2) in float v_distance;
layout(location = 3) in float v_along;
layout(location = 4) in vec2 v_dash;

layout(location = 0) out vec4 fragColor;

layout(std140, binding = 0) uniform buf {
    mat4 u_matrix;
    float u_scale;
    float u_dpr;
    float u_width_scale;
    float u_opacity;
};

void main() {
    float fade = min(1.0, v_half_width);
    float alpha = 1.0 - smoothstep(v_half_width - fade, v_half_width,
                                   abs(v_distance));
    float dash_total = v_dash.x + v_dash.y;
    if (dash_total > 0.0) {
        // One-device-pixel fade at the ink -> gap boundary only; the gap -> ink
        // boundary sits on the period wrap and stays hard.
        float edge = 1.0 / max(u_dpr, 0.0001);
        float phase = mod(v_along, dash_total);
        alpha *= 1.0 - smoothstep(v_dash.x - edge, v_dash.x, phase);
    }
    alpha *= v_color.a * u_opacity;
    // The Qt Quick scene graph expects premultiplied alpha.
    fragColor = vec4(v_color.rgb * alpha, alpha);
}
