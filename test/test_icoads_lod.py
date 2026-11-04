from types import SimpleNamespace

from PySide6.QtWidgets import QApplication

from sailworks.qt.icoadsquick import DISPLAY_LOD_THRESHOLD, IcoadsQuickItem


def test_display_lod_and_cache():
    app = QApplication.instance() or QApplication([])
    view = SimpleNamespace(convert_geographic_to_screen=lambda point: (50, 50))
    camera = SimpleNamespace(view=lambda: view)
    item = IcoadsQuickItem()
    item.setWidth(100)
    item.setHeight(100)
    reports = [SimpleNamespace(latitude=0, longitude=0) for _ in range(4001)]
    item.set_reports(reports[:10], camera)
    assert [entry[2] for entry in item._positions()] == reports[:10]
    item.set_reports(reports, camera)
    positions = item._positions()
    assert item._positions() is positions
    assert [entry[2] for entry in positions] == reports[::3] + [reports[-1]]
    assert len(positions) <= DISPLAY_LOD_THRESHOLD + 1
    item._selected_report = reports[1]
    selected = item._positions()
    assert selected is not positions
    assert [entry[2] is reports[1] for entry in selected].count(True) == 1
    assert selected[1][2] is reports[1]
    assert item._positions() is selected
    assert app is not None
