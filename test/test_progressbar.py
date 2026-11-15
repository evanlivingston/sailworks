"""The reusable progress bar: counts inside, tweened fill, idle sweep."""

from __future__ import annotations

from sailworks.qt.progressbar import BAR_HEIGHT, TweenedProgressBar


def test_counts_are_drawn_inside_the_bar(qtbot):
    bar = TweenedProgressBar()
    qtbot.addWidget(bar)

    bar.set_count(3, 9)

    assert bar.maximum() == 9
    assert bar.value() == 3
    assert bar.format() == "%v / %m"
    assert bar.isTextVisible()
    assert bar.height() == BAR_HEIGHT >= 16


def test_an_unknown_total_spins_instead_of_lying(qtbot):
    bar = TweenedProgressBar()
    qtbot.addWidget(bar)

    bar.set_count(0, 0)

    assert bar.minimum() == bar.maximum() == 0


def test_a_visible_bar_tweens_to_the_new_value(qtbot):
    bar = TweenedProgressBar()
    qtbot.addWidget(bar)
    bar.show()
    bar.set_count(1, 100)
    qtbot.wait(10)

    bar.set_count(80, 100)
    # Immediately after the call the fill is still travelling.
    assert bar.value() < 80

    qtbot.wait(400)
    assert bar.value() == 80


def test_an_idle_bar_is_marked_active_while_working(qtbot):
    bar = TweenedProgressBar()
    qtbot.addWidget(bar)

    assert not bar.is_active()
    bar.set_active(True)
    assert bar.is_active()
    bar.set_active(False)
    assert not bar.is_active()


def test_the_sweep_paints_without_error(qtbot):
    bar = TweenedProgressBar()
    qtbot.addWidget(bar)
    bar.resize(240, BAR_HEIGHT)
    bar.show()
    bar.set_count(5, 10)
    bar.set_active(True)
    qtbot.wait(120)

    pixmap = bar.grab()

    assert not pixmap.isNull()
    assert pixmap.width() == bar.width()


def test_every_bar_in_the_historical_widget_has_the_same_width(qtbot):
    from sailworks.qt.historicalroutingprogress import HistoricalProgress

    widget = HistoricalProgress()
    qtbot.addWidget(widget)
    widget.resize(420, 200)
    widget.show()
    widget.set_weather_phase(2, 9, "")
    widget.set_weather_detail(
        {
            "span_done": 2,
            "span_total": 9,
            "chunk_done": 2,
            "chunk_total": 17,
            "hour_done": 3,
            "hour_total": 24,
        }
    )
    qtbot.wait(100)

    widths = {
        widget.overall_bar.width(),
        widget.request_bar.width(),
        widget.hour_bar.width(),
    }
    assert len(widths) == 1


def test_captions_are_not_bold(qtbot):
    from sailworks.qt.historicalroutingprogress import HistoricalProgress

    widget = HistoricalProgress()
    qtbot.addWidget(widget)

    for caption in (
        widget.overall_caption,
        widget.request_caption,
        widget.hour_caption,
    ):
        assert not caption.font().bold()
