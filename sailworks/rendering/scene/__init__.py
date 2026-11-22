"""Backend-neutral scene data and portrayal policy for the ENC pipeline.

Phase 0 of #267 holds only :mod:`~sailworks.rendering.scene.policy`: the
portrayal rules the Cairo drawer and the triangle-mesh path already duplicated.
Scene IR and shared primitives arrive in a later phase, when a second backend
consumes them.
"""

from . import policy  # noqa: F401
