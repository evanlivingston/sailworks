import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(
    os.environ.get("GWR_NATIVE_GPU_TEST") != "1", reason="Native GPU opt-in"
)
def test_isolated_numerical_map():
    environment = dict(os.environ)
    environment.pop("QT_QPA_PLATFORM", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "unittest",
            "test.qt_gpu_weather_checks.NumericalMapChecks",
            "-q",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout)
