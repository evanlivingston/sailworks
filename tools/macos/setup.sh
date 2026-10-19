#!/usr/bin/env bash
set -euo pipefail

if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew is required. Install it from https://brew.sh/ and run this script again." >&2
    exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

brew install gdal cairo pkg-config python@3.11

BREW_PREFIX="$(brew --prefix)"
export PATH="$(brew --prefix python@3.11)/bin:$BREW_PREFIX/bin:$PATH"
export PKG_CONFIG_PATH="$BREW_PREFIX/lib/pkgconfig:$BREW_PREFIX/share/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"

if [[ ! -d .venv ]]; then
    python3.11 -m venv .venv
fi

source .venv/bin/activate
python -m pip install --upgrade pip wheel setuptools

# Build Python GDAL against the GDAL library installed by Homebrew.
GDAL_VERSION="$(gdal-config --version)"
python -m pip install numpy
python -m pip install --no-build-isolation "GDAL[numpy]==${GDAL_VERSION}"
# pycairo has no macOS wheel, so pip builds it from the source distribution.
# Since 1.29 that source distribution uses the meson build backend, which
# --no-build-isolation cannot install on its own. Install the backend and
# its build tool first so the editable install below can resolve it.
python -m pip install meson-python ninja
python -m pip install --no-build-isolation --editable .

echo "Setup complete. Activate the environment with: source .venv/bin/activate"
echo "Then start the app with: gweatherrouting"
