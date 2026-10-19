#!/usr/bin/env bash
set -euo pipefail

# Fail before pip runs if native GDAL installation did not succeed.
gdal_version="$(gdal-config --version)"
: "${gdal_version:?gdal-config returned an empty version}"

# NOTE: this is necessary for building gdal using deprecated bdist enforced by pep 517
python -m pip install "pip<25.3"

python -m pip install 'numpy>1.0.0' wheel 'setuptools>=67'
python -m pip install --no-cache --force-reinstall "gdal[numpy]==${gdal_version}.*"

echo "Checking GDAL installation..."
python -c 'from osgeo import gdal_array'
