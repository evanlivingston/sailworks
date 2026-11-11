"""Build optional native extensions with setuptools.

Two extensions are optional and independent:

* the PyO3 routing geometry kernel from ``native/geometry`` (built when
  ``setuptools-rust`` is available); and
* the pinned C++ pybind11 routing bridge from ``native/routing`` (built only
  when ``SAILWORKS_BUILD_NATIVE_ROUTING=1``, because it needs CMake, a C++20
  compiler, ZLIB and pybind11). Enabling the bridge composes with the Rust
  build step when ``setuptools-rust`` is available; neither extension becomes
  required.

A source checkout installs without either toolchain and uses the Python
implementations. Environments that build without isolation and do not provide
setuptools-rust (the Windows Qt workflow) also fall back to Python.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext

try:
    from setuptools_rust import Binding, RustExtension
except ImportError:
    rust_extensions = []
else:
    rust_extensions = [
        RustExtension(
            "sailworks._geometry_native",
            path="native/geometry/Cargo.toml",
            binding=Binding.PyO3,
            optional=True,
            cargo_manifest_args=["--locked"],
        )
    ]


def _rust_build_ext():
    """Return setuptools-rust's build_ext class when it is installed."""
    try:
        from setuptools_rust import setuptools_ext
    except ImportError:
        return None
    candidate = getattr(setuptools_ext, "build_ext", None)
    return candidate if isinstance(candidate, type) else None


RUST_BUILD_EXT = _rust_build_ext()


class CMakeNativeRoutingBuildExt(RUST_BUILD_EXT or build_ext):
    """Build ``sailworks_native_routing`` through its standalone CMake project.

    When setuptools-rust is available the Rust geometry extension is built
    first by the inherited command, so enabling native routing does not drop
    ``sailworks._geometry_native``. The Rust extension stays optional.
    """

    def _build_rust_extensions(self) -> None:
        if RUST_BUILD_EXT is None:
            print(
                "sailworks: setuptools-rust build command is not available; "
                "sailworks._geometry_native stays Python-only",
                file=sys.stderr,
            )
            return
        original = list(self.extensions)
        # The placeholder extension is copied from the CMake build below, so
        # the inherited command must not try to compile it.
        self.extensions = [
            extension
            for extension in original
            if extension.name != "sailworks_native_routing"
        ]
        try:
            super().run()
        finally:
            self.extensions = original

    def run(self):
        self._build_rust_extensions()
        root = Path(__file__).resolve().parent
        try:
            import pybind11
        except ImportError as error:
            raise RuntimeError(
                "SAILWORKS_BUILD_NATIVE_ROUTING=1 requires pybind11; "
                "install it in the build environment"
            ) from error

        build_temp = Path(self.build_temp) / "native-routing"
        cmake_args = [
            "cmake",
            "-S",
            str(root / "native" / "routing"),
            "-B",
            str(build_temp),
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DPython3_EXECUTABLE={sys.executable}",
            f"-Dpybind11_DIR={pybind11.get_cmake_dir()}",
        ]
        subprocess.check_call(cmake_args)
        subprocess.check_call(["cmake", "--build", str(build_temp), "--parallel"])

        produced = sorted((build_temp / "python").glob("sailworks_native_routing*"))
        if not produced:
            raise RuntimeError(
                "CMake did not produce sailworks_native_routing; "
                "check the pybind11/Python development configuration"
            )
        for extension in self.extensions:
            if extension.name != "sailworks_native_routing":
                continue
            target = Path(self.get_ext_fullpath(extension.name))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(produced[0], target)


native_routing_extensions = []
cmdclass = {}
if os.environ.get("SAILWORKS_BUILD_NATIVE_ROUTING") == "1":
    native_routing_extensions = [Extension("sailworks_native_routing", sources=[])]
    cmdclass = {"build_ext": CMakeNativeRoutingBuildExt}

setup(
    rust_extensions=rust_extensions,
    ext_modules=native_routing_extensions,
    cmdclass=cmdclass,
)
