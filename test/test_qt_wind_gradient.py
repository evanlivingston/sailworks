import re
from pathlib import Path

from PySide6.QtGui import QShader

from sailworks.common import WAVE_BUCKET_COLORS, wave_color

SHADERS = Path(__file__).parents[1] / "sailworks" / "qt" / "shaders"


def test_background_gradient_is_a_separate_layer_for_each_weather_scene():
    for name in (
        "WeatherBarbs.qml",
        "WeatherBufferedBarbs.qml",
        "WeatherScene.qml",
        "ViewportChartScene.qml",
    ):
        source = (SHADERS / name).read_text()
        assert 'objectName: "weatherFill"' in source, name
        if name in ("WeatherBarbs.qml", "WeatherBufferedBarbs.qml"):
            # Buffer owners keep their fill hidden until publication.
            assert 'objectName: "weatherFillBack"' in source, name
            assert "visible: false" in source, name
        else:
            assert "visible: root.fillEnabled" in source, name


def test_buffered_weather_scene_has_a_fill_back_buffer():
    source = (SHADERS / "WeatherBufferedBarbs.qml").read_text()
    assert 'objectName: "weatherFillBack"' in source
    assert 'objectName: "weatherBarbsBack"' in source


def test_weather_fill_uses_the_same_wind_speed_palette_as_barbs():
    weather = (SHADERS / "weather.frag").read_text()
    barbs = (SHADERS / "barbs.frag").read_text()

    assert "windColor(length(uv))" in weather
    assert "colors[clamp(int(floor(speed / 2.0)), 0, 14)] / 255.0" in barbs
    assert "mix(colors[lower], colors[upper], fract(palettePosition))" in weather
    assert "if (gradientSmooth != 0)" in weather
    assert "int levels = clamp(gradientLevels, 2, 15);" in weather
    assert (
        "int paletteIndex = int(round(float(level) * 14.0 / float(levels - 1)));"
        in weather
    )
    assert "vec4(windColor(length(uv)), 1.0) * qt_Opacity" in weather
    fill_qml = (SHADERS / "WeatherFill.qml").read_text()
    assert "property int gradientLevels: 15" in fill_qml
    assert "property bool gradientSmooth: true" in fill_qml
    packed = QShader.fromSerialized((SHADERS / "weather.frag.qsb").read_bytes())
    assert packed.isValid()


def test_gradient_has_separate_opacity_and_refreshes_the_active_presenter():
    panel = (SHADERS.parent / "weatherpanel.py").read_text()
    presenter = (SHADERS.parent / "weatherpresenter.py").read_text()
    viewport = (SHADERS.parent / "gribviewport.py").read_text()

    assert '"gribWindGradientOpacity", "Gradient opacity"' in panel
    assert "form.addRow(self.wind_gradient_opacity)" in panel
    assert 'self.wind_gradient_style.addItem("Smooth", True)' in panel
    assert 'self.wind_gradient_style.addItem("Hard transitions", False)' in panel
    assert '"gribWindGradientLevels", "Gradient levels", 2, 15' in panel
    assert "form.addRow(self.wind_gradient_levels)" in panel
    assert "presenter.request()" in panel
    assert "set_wind_gradient_opacity(" in presenter
    assert "def set_wind_gradient_opacity(self, opacity):" in viewport


def test_wave_palette_matches_the_cpu_buckets_and_shader_stops():
    shader = (SHADERS / "wave.frag").read_text()
    stops = [
        tuple(int(channel) for channel in match)
        for match in re.findall(r"vec3\((\d+),\s*(\d+),\s*(\d+)\)", shader)
    ]
    expected = [
        tuple(int(color[index : index + 2], 16) for index in (0, 2, 4))
        for color in WAVE_BUCKET_COLORS
    ]
    # One source of truth: the shader stops, the Python palette and the CPU
    # bucket function agree channel for channel.
    assert stops == expected
    for index, color in enumerate(expected):
        assert tuple(round(c * 255) for c in wave_color(index * 0.5)) == color
    assert "waveColor(length(uv))" in shader
    packed = QShader.fromSerialized((SHADERS / "wave.frag.qsb").read_bytes())
    assert packed.isValid()
    # The wave layer is separate: the wind fill's own lookup is untouched.
    weather = (SHADERS / "weather.frag").read_text()
    assert "windColor(length(uv))" in weather


def test_wave_legend_visibility_follows_wave_visible():
    """The legend is shown exactly while the wave layer is asked for."""
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import Mock

    from sailworks.qt.weatherpresenter import WeatherPresenter

    settings = SimpleNamespace(
        chartPalette="dark",
        gribArrowOpacity=0.8,
        gribWindStyle="barbs",
        gribWindGradient=False,
        gribWindGradientOpacity=0.12,
        gribWindGradientSmooth=True,
        gribWindGradientLevels=15,
        gribWaveOpacity=0.85,
        gribWaveSmooth=True,
        gribWaveLevels=9,
    )
    marine = Mock()
    marine.sources = ()
    marine.visible = False
    marine.time_control.time = datetime(2026, 10, 19, tzinfo=timezone.utc)
    observations = Mock()
    observations.reports = ()
    observations.visible = False
    legend = Mock()
    canvas = SimpleNamespace(
        controller=SimpleNamespace(settings=settings),
        requested_viewport_camera=object,
        wave_visible=False,
        wind_visible=True,
        wave_legend=legend,
        marine=marine,
        ship_observations=observations,
        timeline=None,
        _desktop_viewport=True,
        rect=Mock(),
        _layout_wave_legend=Mock(),
    )
    widget = Mock()
    widget._chart_palette = "dark"
    widget._environment = None
    widget._state = "ready"
    presenter = SimpleNamespace(
        widget=widget,
        foreground=Mock(),
        canvas=canvas,
        _source=object(),
        _rendered=True,
        deadline=Mock(),
    )

    WeatherPresenter._sync_active(presenter, presenter._source)
    legend.setVisible.assert_called_once_with(False)
    canvas._layout_wave_legend.assert_not_called()
    legend.raise_.assert_not_called()

    legend.reset_mock()
    canvas.wave_visible = True
    WeatherPresenter._sync_active(presenter, presenter._source)
    legend.setVisible.assert_called_once_with(True)
    canvas._layout_wave_legend.assert_called_once_with()
    legend.raise_.assert_called_once_with()
