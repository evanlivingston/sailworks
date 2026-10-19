#!/usr/bin/env bash
set -euo pipefail

sudo add-apt-repository -y ppa:ubuntugis/ubuntugis-unstable
sudo apt-get -o Acquire::Retries=3 update
# libgdal-dev selects the matching runtime ABI on each Ubuntu release.
sudo apt-get -o Acquire::Retries=3 install -y libgdal-dev gdal-data gdal-plugins
sudo apt-get install -y libgtk-3-dev libgirepository1.0-dev gir1.2-gtk-3.0
