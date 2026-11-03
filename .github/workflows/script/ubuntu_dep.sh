#!/usr/bin/env bash
set -euo pipefail

sudo add-apt-repository -y ppa:ubuntugis/ubuntugis-unstable
sudo apt-get -o Acquire::Retries=3 update
# libgdal-dev selects the matching runtime ABI on each Ubuntu release.
# gdal-plugins is split out of the GDAL library package only from 24.04 on; on
# 22.04 the drivers ship inside libgdal, so ask apt instead of hardcoding it.
packages=(libgdal-dev gdal-data)
if apt-cache show gdal-plugins >/dev/null 2>&1; then
    packages+=(gdal-plugins)
fi
sudo apt-get -o Acquire::Retries=3 install -y "${packages[@]}"
sudo apt-get install -y libgtk-3-dev libgirepository1.0-dev gir1.2-gtk-3.0
