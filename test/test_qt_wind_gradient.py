from pathlib import Path

from PySide6.QtGui import QShader

SHADERS = Path(__file__).parents[1] / "gweatherrouting" / "qt" / "shaders"


def test_background_gradient_is_a_separate_layer_for_each_weather_scene():
    for name in (
        "WeatherBarbs.qml",
        "WeatherBufferedBarbs.qml",
        "WeatherScene.qml",
        "ViewportChartScene.qml",
    ):
        source = (SHADERS / name).read_text()
        assert 'objectName: "weatherFill"' in source, name
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

    assert 'percent("gribWindGradientOpacity")' in panel
    assert 'form.addRow("Gradient opacity", self.wind_gradient_opacity)' in panel
    assert 'self.wind_gradient_style.addItem("Smooth", True)' in panel
    assert 'self.wind_gradient_style.addItem("Hard transitions", False)' in panel
    assert 'form.addRow("Gradient levels", self.wind_gradient_levels)' in panel
    assert "presenter.request()" in panel
    assert "set_wind_gradient_opacity(" in presenter
    assert "def set_wind_gradient_opacity(self, opacity):" in viewport
