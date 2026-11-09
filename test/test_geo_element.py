# -*- coding: utf-8 -*-
# Copyright (C) 2017-2026 Davide Gessa
"""
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

For detail about GNU see <http://www.gnu.org/licenses/>.
"""

import unittest
from unittest.mock import MagicMock

from sailworks.core.geo.element import Element
from sailworks.core.geo.elementpoint import ElementPoint
from sailworks.core.geo.elementmultipoint import ElementMultiPoint
from sailworks.core.geo.rounding import ROUNDING_GATE, ROUNDING_PORT


class TestElement(unittest.TestCase):
    def test_defaults(self):
        e = Element()
        self.assertEqual(e.name, "any")
        self.assertTrue(e.visible)

    def test_custom_init(self):
        e = Element(name="test", visible=False)
        self.assertEqual(e.name, "test")
        self.assertFalse(e.visible)

    def test_to_json_from_json_roundtrip(self):
        e = Element(name="roundtrip", visible=False)
        j = e.to_json()
        e2 = Element.from_json(j)
        self.assertEqual(e2.name, "roundtrip")
        self.assertFalse(e2.visible)

    def test_to_gpx_object_raises(self):
        e = Element()
        with self.assertRaises(Exception):
            e.to_gpx_object()

    def test_len_returns_zero(self):
        e = Element()
        self.assertEqual(len(e), 0)


class TestElementPoint(unittest.TestCase):
    def test_init_with_position(self):
        ep = ElementPoint("wp1", (42.0, 13.5))
        self.assertEqual(ep.name, "wp1")
        self.assertEqual(ep.position, (42.0, 13.5))
        self.assertTrue(ep.visible)

    def test_to_json_includes_position(self):
        ep = ElementPoint("wp1", (42.0, 13.5))
        j = ep.to_json()
        self.assertIn("position", j)
        self.assertEqual(j["position"], (42.0, 13.5))
        self.assertEqual(j["name"], "wp1")

    def test_from_json_roundtrip(self):
        ep = ElementPoint("wp1", (42.0, 13.5), visible=False)
        j = ep.to_json()
        ep2 = ElementPoint.from_json(j)
        self.assertEqual(ep2.name, "wp1")
        self.assertEqual(ep2.position[0], 42.0)
        self.assertEqual(ep2.position[1], 13.5)
        self.assertFalse(ep2.visible)


class TestElementMultiPoint(unittest.TestCase):
    def _make(self, points=None):
        """Create an ElementMultiPoint with a mock collection."""
        collection = MagicMock()
        if points is None:
            points = []
        emp = ElementMultiPoint(
            "route", list(points), visible=True, collection=collection
        )
        return emp, collection

    def test_len(self):
        emp, _ = self._make([(1.0, 2.0, None), (3.0, 4.0, None)])
        self.assertEqual(len(emp), 2)

    def test_len_empty(self):
        emp, _ = self._make()
        self.assertEqual(len(emp), 0)

    def test_getitem(self):
        emp, _ = self._make([(1.0, 2.0, None), (3.0, 4.0, None)])
        self.assertEqual(emp[0], (1.0, 2.0, None))
        self.assertEqual(emp[1], (3.0, 4.0, None))

    def test_setitem_calls_save(self):
        emp, col = self._make([(1.0, 2.0, None)])
        emp[0] = (5.0, 6.0, None)
        self.assertEqual(emp[0], (5.0, 6.0, None))
        col.save.assert_called()

    def test_delitem_calls_save(self):
        emp, col = self._make([(1.0, 2.0, None), (3.0, 4.0, None)])
        del emp[0]
        self.assertEqual(len(emp), 1)
        col.save.assert_called()

    def test_iter(self):
        pts = [(1.0, 2.0, None), (3.0, 4.0, None)]
        emp, _ = self._make(pts)
        self.assertEqual(list(emp), pts)

    def test_add(self):
        emp, col = self._make()
        emp.add(10.0, 20.0, None)
        self.assertEqual(len(emp), 1)
        self.assertEqual(emp[0], (10.0, 20.0, None))
        col.save.assert_called()

    def test_add_with_time(self):
        emp, col = self._make()
        emp.add(10.0, 20.0, "12:00")
        self.assertEqual(emp[0], (10.0, 20.0, "12:00"))

    def test_rename_point_keeps_coordinates_and_saves(self):
        emp, col = self._make([(1.0, 2.0, None), (3.0, 4.0, None)])
        self.assertTrue(emp.rename_point(1, " Finish "))
        self.assertEqual(emp.point_name(1), "Finish")
        self.assertEqual(emp[1], (3.0, 4.0, None))
        self.assertEqual(emp.point_name(0), "")
        col.save.assert_called()

    def test_rename_point_ignores_unchanged_and_invalid_index(self):
        emp, col = self._make([(1.0, 2.0, None)])
        self.assertFalse(emp.rename_point(0, ""))
        self.assertFalse(emp.rename_point(5, "Nope"))
        col.save.assert_not_called()

    def test_point_names_survive_json_roundtrip(self):
        emp, _ = self._make([(1.0, 2.0, None), (3.0, 4.0, None)])
        emp.rename_point(0, "Start")
        payload = emp.to_json()
        self.assertEqual(payload["point_names"], ["Start", ""])
        loaded = ElementMultiPoint.from_json(payload)
        self.assertEqual(loaded.point_name(0), "Start")
        self.assertEqual(loaded.point_name(1), "")
        self.assertEqual(loaded[1], (3.0, 4.0, None))

    def test_unnamed_points_keep_legacy_json_payload(self):
        emp, _ = self._make([(1.0, 2.0, None)])
        self.assertNotIn("point_names", emp.to_json())

    def test_move_and_duplicate_preserve_point_names(self):
        emp, _ = self._make([(1.0, 2.0, None), (3.0, 4.0, None)])
        emp.rename_point(0, "Start")
        emp.move(0, 5.0, 6.0)
        self.assertEqual(emp.point_name(0), "Start")
        emp.duplicate(0)
        self.assertEqual(emp.point_name(2), "Start")
        self.assertEqual(emp[2], (5.0, 6.0, None))

    def test_point_rounding_survives_json_roundtrip(self):
        emp, _ = self._make([(1.0, 2.0, None), (3.0, 4.0, None)])
        emp.set_point_rounding(1, ROUNDING_GATE)
        payload = emp.to_json()

        self.assertEqual(payload["point_roundings"], [2, ROUNDING_GATE])
        loaded = ElementMultiPoint.from_json(payload)
        self.assertEqual(loaded.point_rounding(0), 2)
        self.assertEqual(loaded.point_rounding(1), ROUNDING_GATE)

    def test_default_rounding_keeps_legacy_json_payload(self):
        emp, _ = self._make([(1.0, 2.0, None)])
        emp.set_point_rounding(0, 2)
        self.assertNotIn("point_roundings", emp.to_json())

    def test_rounding_preserved_by_rename_move_and_duplicate(self):
        emp, _ = self._make([(1.0, 2.0, None)])
        emp.set_point_rounding(0, ROUNDING_PORT)
        emp.rename_point(0, "Mark")
        emp.move(0, 5.0, 6.0)
        self.assertEqual(emp.point_rounding(0), ROUNDING_PORT)
        emp.duplicate(0)
        self.assertEqual(emp.point_rounding(1), ROUNDING_PORT)
        self.assertEqual(emp.point_name(1), "Mark")

    def test_move(self):
        emp, col = self._make([(1.0, 2.0, None)])
        emp.move(0, 5.0, 6.0)
        self.assertEqual(emp[0], (5.0, 6.0, None))
        col.save.assert_called()

    def test_remove(self):
        emp, col = self._make([(1.0, 2.0, None), (3.0, 4.0, None)])
        emp.remove(0)
        self.assertEqual(len(emp), 1)
        self.assertEqual(emp[0], (3.0, 4.0, None))
        col.save.assert_called()

    def test_move_up(self):
        emp, col = self._make([(1.0, 2.0, None), (3.0, 4.0, None)])
        emp.move_up(1)
        self.assertEqual(emp[0], (3.0, 4.0, None))
        self.assertEqual(emp[1], (1.0, 2.0, None))
        col.save.assert_called()

    def test_move_up_at_zero_is_noop(self):
        emp, col = self._make([(1.0, 2.0, None), (3.0, 4.0, None)])
        emp.move_up(0)
        self.assertEqual(emp[0], (1.0, 2.0, None))
        self.assertEqual(emp[1], (3.0, 4.0, None))
        col.save.assert_not_called()

    def test_move_down(self):
        emp, col = self._make([(1.0, 2.0, None), (3.0, 4.0, None)])
        emp.move_down(0)
        self.assertEqual(emp[0], (3.0, 4.0, None))
        self.assertEqual(emp[1], (1.0, 2.0, None))
        col.save.assert_called()

    def test_move_down_at_last_is_noop(self):
        emp, col = self._make([(1.0, 2.0, None), (3.0, 4.0, None)])
        emp.move_down(1)
        self.assertEqual(emp[0], (1.0, 2.0, None))
        self.assertEqual(emp[1], (3.0, 4.0, None))
        col.save.assert_not_called()

    def test_length_zero_points(self):
        emp, _ = self._make()
        self.assertEqual(emp.length(), 0.0)

    def test_length_one_point(self):
        emp, _ = self._make([(1.0, 2.0, None)])
        self.assertEqual(emp.length(), 0.0)

    def test_length_two_or_more_points(self):
        emp, _ = self._make([(0.0, 0.0, None), (1.0, 0.0, None)])
        d = emp.length()
        self.assertGreater(d, 0.0)


if __name__ == "__main__":
    unittest.main()
