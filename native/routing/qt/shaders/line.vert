#version 440

// Constant-screen-width line quads for the routing overlay.
//
// One quad per segment. Positions are normalised Web-Mercator world units
// relative to a per-geometry origin, so zoom is a scale uniform and pan is a
// matrix translation; neither rebuilds geometry. The sideways offset is applied
// after the world->pixel scale, which is what keeps the stroke a constant
// number of screen pixels at every zoom level.

layout(location = 0) in vec2 a_position;
layout(location = 1) in vec2 a_normal;
layout(location = 2) in float a_side;
layout(location = 3) in float a_width;
layout(location = 4) in float a_along;
layout(location = 5) in vec4 a_color;
// Per-segment dash pattern in logical screen pixels: (ink, gap). Both zero
// means a solid stroke.
layout(location = 6) in vec2 a_dash;
// Dash phase at the segment start, in normalised Web-Mercator world units: the
// distance along the polyline or ring this segment continues. Added to
// a_along before the world->pixel scale, so one pattern runs across joints.
layout(location = 7) in float a_phase;

layout(location = 0) out vec4 v_color;
layout(location = 1) out float v_half_width;
layout(location = 2) out float v_distance;
// Distance from the start of the segment's chain in logical screen pixels: the
// per-part phase plus the segment's own along distance. Scaling here rather
// than in the fragment shader keeps the dash period constant across zoom.
layout(location = 3) out float v_along;
layout(location = 4) out vec2 v_dash;

layout(std140, binding = 0) uniform buf {
    mat4 u_matrix;
    float u_scale;
    float u_dpr;
    float u_width_scale;
    float u_opacity;
};

void main() {
    float minimum_width = 1.0 / max(u_dpr, 0.0001);
    float half_width = 0.5 * max(a_width * u_width_scale, minimum_width);
    vec2 pixel_position = a_position * u_scale + a_normal * (a_side * half_width);
    gl_Position = u_matrix * vec4(pixel_position, 0.0, 1.0);
    v_color = a_color;
    v_half_width = half_width;
    v_distance = a_side * half_width;
    v_along = (a_phase + a_along) * u_scale;
    v_dash = a_dash;
}
