"""Focused GPU streamline item scheduling checks."""

from pathlib import Path
from types import SimpleNamespace
import unittest

import numpy as np
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QShader

from gweatherrouting.qt.gpustreamlines import (
    GpuStreamlineItem,
    GpuTraceInput,
    jittered_seed_positions,
)
from gweatherrouting.rendering.streamlines import density_parameters


class TestGpuStreamlineItemScheduling(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_trace_input(self):
        return GpuTraceInput(
            wind=np.zeros((2, 2), dtype=np.float32),
            x_coordinates=np.zeros(2, dtype=np.float32),
            y_coordinates=np.zeros(2, dtype=np.float32),
            valid_x=np.ones(2, dtype=np.uint32),
            valid_y=np.ones(2, dtype=np.uint32),
            seeds=np.zeros((1, 2), dtype=np.float32),
            width=20,
            height=10,
        )

    def test_opacity_and_phase_do_not_advance_trace_generation(self):
        item = GpuStreamlineItem()
        try:
            item.set_trace_input(self.make_trace_input())
            generation = item._input_generation
            item.set_opacity(0.5)
            item.set_view_transform(40, 30, 2.0, -3.0, 4.0, 360.0)
            item._advance_flow()
            self.assertEqual(item._input_generation, generation)
            self.assertEqual(item.width(), 40)
            self.assertEqual(item.height(), 30)
        finally:
            item.set_trace_input(None)

    def test_animation_timer_tracks_trace_input_and_visibility(self):
        item = GpuStreamlineItem()
        try:
            item.set_trace_input(self.make_trace_input())
            self.assertTrue(item._animation_timer.isActive())
            item.setVisible(False)
            self.assertFalse(item._animation_timer.isActive())
            # A windowless QQuickItem may remain effectively hidden after
            # setVisible(True); its effective visibility drives the timer.
            item.set_trace_input(None)
            self.assertFalse(item._animation_timer.isActive())
        finally:
            item.set_trace_input(None)

    def test_trace_shader_uses_previous_endpoint_buffer_for_transition(self):
        shader = (
            Path(__file__).parents[1]
            / "gweatherrouting/qt/shaders/streamline_trace.vert"
        ).read_text()
        self.assertIn("layout(std430, binding = 2)", shader)
        self.assertIn("mix(previous.endpoints, segment.endpoints, transition)", shader)
        self.assertIn("segment.endpoints.xy * viewScale", shader)
        self.assertIn("worldWidth > 0.0", shader)

    def test_gpu_filter_keeps_crossings_and_binds_visibility_by_generation(self):
        shaders = Path(__file__).parents[1] / "gweatherrouting/qt/shaders"
        filter_source = (shaders / "streamline_filter.comp").read_text()
        vertex = (shaders / "streamline_trace.vert").read_text()
        item = (shaders.parent / "gpustreamlines.py").read_text()
        self.assertIn("usable >= 12 && matches * 100 >= usable * 80", filter_source)
        self.assertIn("if (!rejected)", filter_source)
        self.assertIn("owner[uint(y) * cellsX + uint(x)] == NO_OWNER", filter_source)
        self.assertIn(
            "layout(std430, binding = 3) readonly buffer CurrentVisibility", vertex
        )
        self.assertIn(
            "layout(std430, binding = 4) readonly buffer PreviousVisibility", vertex
        )
        self.assertIn('self._state["previous_visibility"]', item)
        self.assertRegex(item, r"bufferLoadStore\(\s*1,")
        self.assertIn(
            "updates.uploadStaticBuffer(filter_params, filter_params_data)", item
        )
        self.assertTrue(
            QShader.fromSerialized(
                (shaders / "streamline_filter.comp.qsb").read_bytes()
            ).isValid()
        )


class TestGpuSeedDensity(unittest.TestCase):
    def test_quarter_of_under_cap_and_capped_seeds_at_each_density(self):
        for density in ("sparse", "standard", "dense", "very_dense", "all"):
            spacing, cap, _ = density_parameters(density)
            for cells, expected in ((8, 16), (100, (cap + 3) // 4)):
                size = cells * spacing
                view = SimpleNamespace(
                    width=size,
                    height=size,
                    _center_lon=0,
                    _center_lat=0,
                    _zoom=2,
                    _lon_to_x=lambda _lon: size / 2,
                    _lat_to_y=lambda _lat: size / 2,
                )
                with self.subTest(density=density, cells=cells):
                    selected = jittered_seed_positions(view, density)
                    self.assertEqual(len(selected), expected)
                    if cells == 8:
                        indices = {
                            (int(x // spacing), int(y // spacing)) for x, y in selected
                        }
                        self.assertEqual(len(indices), 16)
                        self.assertTrue(
                            all(ix % 2 == 0 and iy % 2 == 0 for ix, iy in indices)
                        )
