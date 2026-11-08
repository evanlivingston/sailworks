"""Build the optional PyO3 routing geometry kernel with setuptools.

The extension is optional so a source checkout without a Rust toolchain still
installs the Python implementation. Environments that build without isolation
and do not provide setuptools-rust (the Windows Qt workflow) also fall back to
the Python implementation.
"""

from setuptools import setup

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

setup(rust_extensions=rust_extensions)
