import math
import re
from pathlib import Path

import pytest
from PySide6.QtGui import QShader

from sailworks.common import WAVE_BUCKET_COLORS, wave_color
from sailworks.rendering.windpainter import (
    WAVE_ARROW_HEAD_LEN,
    WAVE_ARROW_HEAD_SPREAD,
    WAVE_ARROW_SHAFT_MAX,
    WAVE_ARROW_SHAFT_MIN,
    WAVE_PERIOD_MAX_S,
    WAVE_PERIOD_MIN_S,
    wave_arrow_length,
)

SHADERS = Path(__file__).parents[1] / "sailworks" / "qt" / "shaders"
FRAG = SHADERS / "wavebarbs.frag"
VERT = SHADERS / "wavebarbs.vert"
QML = SHADERS / "WaveBarbs.qml"


def shader_float(source, name):
    match = re.search(rf"const\s+float\s+{name}\s*=\s*([0-9.]+)\s*;", source)
    assert match, f"{name} is not declared as a shader constant"
    return float(match.group(1))


def test_wave_arrow_palette_matches_the_cpu_bucket_palette():
    source = FRAG.read_text()
    stops = [
        tuple(int(channel) for channel in match)
        for match in re.findall(r"vec3\((\d+),\s*(\d+),\s*(\d+)\)", source)
    ]
    expected = [
        tuple(int(color[index : index + 2], 16) for index in (0, 2, 4))
        for color in WAVE_BUCKET_COLORS
    ]
    # One source of truth: the arrow stops, the legend palette and the CPU
    # bucket function agree channel for channel.
    assert stops == expected
    for index, color in enumerate(expected):
        assert tuple(round(c * 255) for c in wave_color(index * 0.5)) == color
    assert "waveColor(waveHeight)" in source


def test_wave_arrow_length_matches_the_cpu_period_mapping():
    source = FRAG.read_text()
    assert shader_float(source, "WAVE_PERIOD_MIN_S") == WAVE_PERIOD_MIN_S
    assert shader_float(source, "WAVE_PERIOD_MAX_S") == WAVE_PERIOD_MAX_S
    minimum = shader_float(source, "WAVE_ARROW_SHAFT_MIN")
    maximum = shader_float(source, "WAVE_ARROW_SHAFT_MAX")
    assert minimum == WAVE_ARROW_SHAFT_MIN
    assert maximum == WAVE_ARROW_SHAFT_MAX
    assert shader_float(source, "WAVE_ARROW_HEAD_LEN") == WAVE_ARROW_HEAD_LEN
    assert shader_float(source, "WAVE_ARROW_HEAD_SPREAD") == WAVE_ARROW_HEAD_SPREAD
    # The shader computes the same linear, clamped fraction the CPU helper does.
    assert "clamp(fraction, 0.0, 1.0)" in source

    periods = (
        None,
        0.0,
        -2.0,
        float("nan"),
        float("inf"),
        1.0,
        WAVE_PERIOD_MIN_S,
        7.0,
        12.5,
        WAVE_PERIOD_MAX_S,
        40.0,
    )
    for period in periods:
        if period is None or not math.isfinite(period) or period <= 0:
            shader_length = minimum
        else:
            fraction = (period - WAVE_PERIOD_MIN_S) / (
                WAVE_PERIOD_MAX_S - WAVE_PERIOD_MIN_S
            )
            fraction = min(1.0, max(0.0, fraction))
            shader_length = minimum + fraction * (maximum - minimum)
        assert shader_length == pytest.approx(wave_arrow_length(period))
    assert wave_arrow_length(WAVE_PERIOD_MIN_S) == 8.0
    assert wave_arrow_length(WAVE_PERIOD_MAX_S) == 20.0


def test_wave_arrow_direction_is_the_coming_from_convention():
    source = FRAG.read_text()
    # build_wave_geometry()'s shaft: 0 deg points up (screen -y) and 90 deg
    # points right, so the fragment stage must use the same (sin, -cos) form.
    assert "vec2 shaft = vec2(sin(angle), -cos(angle));" in source
    assert "vec2 perpendicular = vec2(cos(angle), sin(angle));" in source
    assert "radians(directionDegrees)" in source


def test_wave_direction_interpolates_as_a_vector_across_the_seam():
    source = VERT.read_text()
    # Interpolating the bearing in degrees would sweep 350 -> 10 through 180.
    assert "vec2 bearingVector(float degreesValue)" in source
    assert "vec2(cos(angle), sin(angle))" in source
    assert "atan(bearing.y, bearing.x)" in source
    assert "mod(degrees(atan(bearing.y, bearing.x)) + 360.0, 360.0)" in source
    assert "bearing = aVector + (bVector - aVector) * blend;" in source


def test_wave_vertex_shader_binds_six_scalar_samplers():
    source = VERT.read_text()
    for index in range(6):
        assert re.search(rf"uniform sampler2D u{index};", source), f"u{index}"
    # Every scalar is read through fieldBits on its own sampler.
    assert source.count("fieldBits(u") == 6
    assert re.search(r"uniform sampler2D anchorX;", source)
    assert re.search(r"uniform sampler2D anchorY;", source)
    assert re.search(r"uniform sampler2D nodeId;", source)
    # Height/period/direction at A (u0..u2) and B (u3..u5).
    assert "fieldBits(u0, field)" in source
    assert "fieldBits(u1, field)" in source
    assert "fieldBits(u2, field)" in source
    assert "fieldBits(u3, field)" in source
    assert "fieldBits(u4, field)" in source
    assert "fieldBits(u5, field)" in source


def test_wave_glyph_scene_supplies_six_images_and_the_indirection():
    source = QML.read_text()
    for index in range(6):
        assert f"property alias u{index}Source:" in source
        assert f"property variant u{index}:" in source
    for name in (
        "waveHeight0",
        "wavePeriod0",
        "waveDirection0",
        "waveHeight1",
        "wavePeriod1",
        "waveDirection1",
    ):
        assert f'objectName: "{name}"' in source, name
    assert 'vertexShader: "wavebarbs.vert.qsb"' in source
    assert 'fragmentShader: "wavebarbs.frag.qsb"' in source
    for name in (
        "nodeIdIndirection",
        "fieldColumns",
        "anchorOffsetX",
        "anchorOffsetY",
        "previewScale",
        "previewOffsetX",
        "previewOffsetY",
        "property real blend: 0",
    ):
        assert name in source, name


def test_compiled_wave_shaders_are_present_and_valid():
    for name in ("wavebarbs.vert.qsb", "wavebarbs.frag.qsb"):
        path = SHADERS / name
        assert path.is_file(), name
        assert path.stat().st_size > 1024, name
        packed = QShader.fromSerialized(path.read_bytes())
        assert packed.isValid(), name


def test_existing_wind_glyph_is_untouched():
    """The wave layer adds a scene; it must not edit the wind glyph."""
    for name in ("barbs.vert", "barbs.frag", "WeatherBarbs.qml"):
        source = (SHADERS / name).read_text()
        assert "wavebarbs" not in source, name
        assert "waveHeight" not in source, name
    qml = (SHADERS / "WeatherBarbs.qml").read_text()
    assert 'vertexShader: "barbs.vert.qsb"' in qml
    assert 'fragmentShader: "barbs.frag.qsb"' in qml
    # The six-sampler layout belongs only to the new stages.
    for name in ("barbs.vert", "barbs.frag"):
        assert "uniform sampler2D u5;" not in (SHADERS / name).read_text()
