#!/usr/bin/env bash
set -euo pipefail

# The archive GDAL is too old for this project (24.04 ships 3.8, which cannot
# build its numpy array extension against numpy 2), so the ubuntugis PPA is
# still required. launchpad is occasionally unreachable from hosted runners;
# retry before failing so a transient outage does not kill the job, and say
# plainly what failed when it is real.
for attempt in 1 2 3; do
    if sudo add-apt-repository -y ppa:ubuntugis/ubuntugis-unstable; then
        break
    fi
    if [ "$attempt" = 3 ]; then
        echo "Unable to add ppa:ubuntugis/ubuntugis-unstable after 3 attempts." >&2
        echo "The archive GDAL is not a substitute: Ubuntu 24.04 ships 3.8, which cannot build its numpy extension against numpy 2." >&2
        exit 1
    fi
    echo "launchpad unreachable; retrying in ${PPA_RETRY_SLEEP:-15}s (attempt $attempt/3)" >&2
    sleep "${PPA_RETRY_SLEEP:-15}"
done
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
