"""Quality timings must survive without becoming source identity."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from sailworks.qt.basecharts import describe_sources
from sailworks.rendering.gshhs import GSHHSVectorChart


def test_timing_does_not_invalidate_source_identity():
    chart = GSHHSVectorChart.__new__(GSHHSVectorChart)
    chart.enabled = True
    chart.path = "coast"
    chart.force_downscale = False
    chart.countries = ()
    chart.last_time = {"f": 0.0}
    first = describe_sources([chart])[0]
    chart.last_time["f"] = 0.4
    second = describe_sources([chart])[0]
    assert first == second
    assert hash(first) == hash(second)
    assert dict(second.initial_times)["f"] == 0.4


def test_full_quality_timing_triggers_downscale():
    chart = GSHHSVectorChart.__new__(GSHHSVectorChart)
    chart.last_time = {q: 0.0 for q in "cli hf" if q != " "}
    chart.force_downscale = False
    chart.vector_files = {"f1": "full", "h1": "high"}
    chart.drawer = SimpleNamespace(draw=Mock())
    chart.countries = ()
    chart.get_bounding_geometry = Mock(return_value=None)
    viewport = SimpleNamespace(get_scale=lambda: 1)
    with patch(
        "sailworks.rendering.gshhs.time.perf_counter", side_effect=[1, 1.3, 2, 2.1]
    ):
        chart.do_draw(viewport, None)
        chart.do_draw(viewport, None)
    assert chart.drawer.draw.call_args_list[0].args[2] == "full"
    assert chart.drawer.draw.call_args_list[1].args[2] == "high"
    assert chart.last_time["f"] > 0.25
