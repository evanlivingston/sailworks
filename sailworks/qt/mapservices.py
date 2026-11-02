"""Weather service references shared by Qt map hosts."""

from .basecharts import BaseChartRenderer


class MapWeatherServices:
    """Own the weather-related services attached to a map host."""

    def __init__(self):
        self.base_chart_renderer = None
        self.wind = None
        self.wind_cursor = None
        self.weather_coverage = None
        self.weather_presenter = None

    def initialize_base_chart_renderer(self, controller):
        self.base_chart_renderer = BaseChartRenderer(controller.scheduler.submit)
        return self.base_chart_renderer

    def initialize_wind(self, wind_painter, controller):
        # Qt Quick owns viewport wind presentation; retain the legacy slot for
        # non-viewport hosts without constructing the retired recovery renderer.
        self.wind = None
        return None

    def initialize_wind_cursor(self, host, controller):
        from .windcursor import WindCursorSampler

        self.wind_cursor = WindCursorSampler(host, controller.scheduler.submit)
        return self.wind_cursor

    def initialize_weather_coverage(self, host, show_coverage):
        from .weathercoverage import WeatherCoverage

        self.weather_coverage = WeatherCoverage(host, show_coverage)
        return self.weather_coverage

    def initialize_weather_presenter(self, host):
        from .weatherpresenter import WeatherPresenter

        self.weather_presenter = WeatherPresenter(host)
        return self.weather_presenter


class MapWeatherServicesMixin:
    """Expose weather service members through legacy map attributes."""

    @property
    def _base_chart_renderer(self):
        return self._weather_services.base_chart_renderer

    @_base_chart_renderer.setter
    def _base_chart_renderer(self, value):
        self._weather_services.base_chart_renderer = value

    @property
    def wind(self):
        return self._weather_services.wind

    @wind.setter
    def wind(self, value):
        self._weather_services.wind = value

    @property
    def _wind_cursor(self):
        return self._weather_services.wind_cursor

    @_wind_cursor.setter
    def _wind_cursor(self, value):
        self._weather_services.wind_cursor = value

    @property
    def weather_coverage(self):
        return self._weather_services.weather_coverage

    @weather_coverage.setter
    def weather_coverage(self, value):
        self._weather_services.weather_coverage = value

    @property
    def _weather_presenter(self):
        return self._weather_services.weather_presenter

    @_weather_presenter.setter
    def _weather_presenter(self, value):
        self._weather_services.weather_presenter = value
