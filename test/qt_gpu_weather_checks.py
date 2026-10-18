"""Isolated native map integration for the opt-in numerical barb shader."""

from datetime import timedelta
import os
import json
import time
import unittest
from unittest.mock import patch

import numpy as np

from test.qt_gpu_checks import Fixture
from test.test_grib_sampling import TestGribSampling as GribFixtures, START


@unittest.skipUnless(os.environ.get("GWR_NATIVE_GPU_TEST") == "1", "Native GPU opt-in")
class NumericalMapChecks(Fixture):
    renderer = "gpu"

    def setUp(self):
        guard = patch.dict(os.environ, GWR_GPU_WEATHER="1")
        guard.start()
        self.addCleanup(guard.stop)
        super().setUp()
        self.gribs = GribFixtures()
        self.gribs.setUp()
        self.addCleanup(self.gribs.doCleanups)
        self.addCleanup(self.window.close)

    def test_map_scrub_sources_styles_and_shutdown(self):
        canvas = self.window.map
        canvas._center_lat = canvas._center_lon = 0.5
        canvas._zoom = 9
        self.controller.settings.gribArrowOnGround = True
        yy, xx = np.mgrid[:161, :161]
        records = []
        for hour in (0, 3):
            records.extend(
                [
                    ("UGRD", hour, 2 + xx / 16 + yy / 16 + hour * 2),
                    ("VGRD", hour, 1 + xx / 32 - yy / 32 + hour),
                    ("PRMSL", hour, 100000 + xx / 2 + yy + hour * 100),
                ]
            )
        source = self.gribs.file(
            records,
            transform=(-0.003125, 0.00625, 0, -0.003125, 0, 0.00625),
            end=3,
        )
        self.controller.core.grib_manager.gribs = [source]
        canvas.time_control.set_time(START + timedelta(minutes=1))
        canvas._invalidate_base()
        canvas.update()
        gpu = canvas._gpu
        self.wait(
            lambda: gpu.scene is not None
            and gpu.scene.frame is not None
            and gpu.scene.frame.numerical_wind is not None
            and gpu.scene.weather.property("ready")
        )
        self.wait(
            lambda: not gpu.scene.frame.wind and gpu.scene.frame.raster_wind is None
        )
        atlas = gpu.scene.frame.numerical_wind[0]
        requests = gpu.scene.weather_provider.requests
        for seconds in (61, 900, 3599, 5400, 10799):
            canvas.time_control.set_time(START + timedelta(seconds=seconds))
            canvas.update()
            self.wait(
                lambda: gpu.scene.frame.numerical_wind is not None
                and abs(gpu.scene.frame.numerical_wind[1] - seconds / 10800) < 1e-7
            )
            self.assertIs(gpu.scene.frame.numerical_wind[0], atlas)
        self.assertEqual(gpu.scene.weather_provider.requests, requests)
        self.assertGreater(atlas.count, 0)
        self.assertGreater(gpu.estimate["upload_bytes"], atlas.nbytes)
        self.wait(lambda: not canvas.wind._running)
        # With no new background publication, a time-uniform change must not
        # rebuild the chart raster. Navigation remains time-sensitive.
        with patch.object(canvas, "_draw_weather_background", return_value=None):
            canvas.update()
            self.drain()
            base_image = gpu.scene.frame.base.image.cacheKey()
            nav_image = gpu.scene.frame.navigation.image.cacheKey()
            with patch.object(canvas, "_base_image", wraps=canvas._base_image) as bake:
                for seconds in (2000, 2001, 2002):
                    canvas.time_control.set_time(START + timedelta(seconds=seconds))
                    canvas.update()
                    self.wait(
                        lambda: gpu.scene.frame.numerical_wind is not None
                        and abs(gpu.scene.frame.numerical_wind[1] - seconds / 10800)
                        < 1e-7
                    )
                    self.assertEqual(gpu.scene.frame.base.image.cacheKey(), base_image)
                    self.assertNotEqual(
                        gpu.scene.frame.navigation.image.cacheKey(), nav_image
                    )
                    nav_image = gpu.scene.frame.navigation.image.cacheKey()
                self.assertEqual(bake.call_count, 0)
        # Synthetic 25,921-node native forecast; this measures the integrated
        # map while background fill/isobar work remains on the CPU. Readback
        # waits for actual GPU output, rather than timing only uniform writes.
        elapsed, cpu = [], []
        for seconds in range(3600, 3720):
            started, thread_started = time.perf_counter(), time.thread_time()
            canvas.time_control.set_time(START + timedelta(seconds=seconds))
            canvas.update()
            self.wait(
                lambda: gpu.scene.frame.numerical_wind is not None
                and abs(gpu.scene.frame.numerical_wind[1] - seconds / 10800) < 1e-7
            )
            self.assertFalse(gpu.widget.grabFramebuffer().isNull())
            elapsed.append((time.perf_counter() - started) * 1000)
            cpu.append((time.thread_time() - thread_started) * 1000)
        self.assertEqual(gpu.scene.weather_provider.requests, requests)
        print(
            json.dumps(
                {
                    "benchmark": "synthetic_native_numerical_barbs",
                    "nodes": 161 * 161,
                    "symbols": atlas.count,
                    "frames": len(elapsed),
                    "elapsed_ms_median": float(np.median(elapsed)),
                    "elapsed_ms_p95": float(np.percentile(elapsed, 95)),
                    "gui_thread_cpu_ms_median": float(np.median(cpu)),
                    "numerical_image_reloads": gpu.scene.weather_provider.requests
                    - requests,
                }
            ),
            flush=True,
        )
        canvas._center_lon += 0.01
        canvas.update()
        self.wait(
            lambda: gpu.scene.frame.numerical_wind is not None
            and gpu.scene.frame.numerical_wind[0] is not atlas
        )
        self.assertGreater(gpu.scene.weather_provider.requests, requests)
        # Unsupported symbol style must immediately return to the existing path.
        self.controller.settings.gribWindStyle = "streamlines"
        canvas.update()
        self.wait(lambda: gpu.scene.frame.numerical_wind is None)
        self.assertFalse(gpu.scene.weather.isVisible())
        self.controller.settings.gribWindStyle = "barbs"
        canvas.update()
        self.wait(lambda: gpu.scene.frame.numerical_wind is not None)
        # Removing selected sources cannot retain numerical symbols.
        self.controller.core.grib_manager.gribs = []
        canvas.update()
        self.wait(lambda: gpu.scene.frame.numerical_wind is None)
        self.assertIsNone(gpu.weather.frame)
        thread = gpu.weather._thread
        gpu.close()
        thread.join(5)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
