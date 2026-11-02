"""Plot data helpers that do not initialize Matplotlib or its font cache."""

import numpy as np


def elapsed_offsets(points):
    if not points:
        return ()
    first = points[0].time
    return np.array([(point.time - first).total_seconds() for point in points])
