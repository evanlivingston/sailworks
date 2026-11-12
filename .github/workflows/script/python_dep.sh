#!/usr/bin/env bash
set -euo pipefail

# Fail before pip runs if native GDAL installation did not succeed.
gdal_version="$(gdal-config --version)"
: "${gdal_version:?gdal-config returned an empty version}"

# NOTE: this is necessary for building gdal using deprecated bdist enforced by pep 517
python -m pip install "pip<25.3"

python -m pip install 'numpy>1.0.0' wheel 'setuptools>=67'
# Build against the env's numpy: with build isolation GDAL's setup.py cannot
# see numpy and silently omits the _gdal_array extension that gdal_array needs.
python -m pip install --no-cache --force-reinstall --no-build-isolation "gdal[numpy]==${gdal_version}.*"

echo "Checking GDAL installation..."
python -c 'from osgeo import gdal_array'
