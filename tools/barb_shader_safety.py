"""Experiment-only finite varyings/explicit validity for missing native wind."""


def safe_sources(vertex, fragment):
    if "int id =" not in vertex or "validWind" in vertex:
        raise ValueError("Expected unmodified native-indirection barb vertex")
    vertex = vertex.replace(
        "layout(location = 2) out vec2 wind;",
        "layout(location = 2) out vec2 wind;\nlayout(location = 3) out float validWind;",
    )
    vertex = vertex.replace(
        "void main() {",
        """bool finiteVector(vec2 v) {
    // Integer exponent checks survive Metal fast-math; do not rely on isnan
    // after interpolating missing-data NaNs through a fragment varying.
    return all(notEqual(floatBitsToUint(v) & uvec2(0x7f800000u), uvec2(0x7f800000u)));
}

void main() {""",
    )
    vertex = vertex.replace(
        "    if (id < 0) { a = vec2(uintBitsToFloat(0x7fc00000u)); b = a; }", ""
    )
    old = """    wind = blend <= 0.0 ? a : (blend >= 1.0 ? b : a + (b - a) * blend);
    gl_Position = qt_Matrix * vec4(anchor + localPoint, 0.0, 1.0);"""
    new = """    bool valid = id >= 0 && finiteVector(anchor)
        && (blend <= 0.0 ? finiteVector(a) : (blend >= 1.0 ? finiteVector(b) : (finiteVector(a) && finiteVector(b))));
    validWind = valid ? 1.0 : 0.0;
    wind = vec2(0.0);
    if (valid) wind = blend <= 0.0 ? a : (blend >= 1.0 ? b : a + (b - a) * blend);
    // Every vertex and varying is finite, including padded/missing cells.
    if (!valid) anchor = vec2(0.0);
    gl_Position = qt_Matrix * vec4(anchor + localPoint, 0.0, 1.0);"""
    if (
        old not in vertex
        or "if (any(isnan(wind)) || any(isinf(wind))) discard;" not in fragment
    ):
        raise ValueError("Barb shader contract changed")
    vertex = vertex.replace(old, new)
    fragment = fragment.replace(
        "layout(location = 2) in vec2 wind;",
        "layout(location = 2) in vec2 wind;\nlayout(location = 3) in float validWind;",
    ).replace(
        "    if (any(isnan(wind)) || any(isinf(wind))) discard;",
        "    if (validWind < 0.99999) discard;",
    )
    return vertex, fragment
