"""Observe stock Qt Quick texture identity/lifetime without replacing transport.

Texture identity is NOT an upload counter: an existing texture can be updated in
place. These observations establish native resource retention/release only.
Read render-thread-only texture providers through a direct render callback.
"""

from threading import Lock


class TextureProbe:
    def __init__(self, widget):
        from PySide6.QtCore import QObject, Qt

        self._lock = Lock()
        self._items = {}
        self._seen = {}
        self._live = {}
        self._released = 0
        self._samples = 0
        self._errors = []
        self._rhi_fields = set()
        self._field_atlas = False
        for name in (
            "fieldU0",
            "fieldV0",
            "fieldU1",
            "fieldV1",
            "selectionX",
            "selectionY",
            "selectionId",
        ):
            item = widget._barbs.findChild(QObject, name)
            if item is None:
                raise RuntimeError(f"missing texture probe item: {name}")
            self._items[name] = item
        self._window = widget.quickWindow()
        self._window.afterRendering.connect(
            self._sample, Qt.ConnectionType.DirectConnection
        )

    def _sample(self):
        from PySide6.QtCore import Qt
        from shiboken6 import getCppPointer

        try:
            for name, item in self._items.items():
                provider = item.textureProvider()
                texture = None if provider is None else provider.texture()
                if texture is None:
                    continue
                pointer = getCppPointer(texture)[0]
                with self._lock:
                    if name.startswith("field"):
                        rhi = texture.rhiTexture()
                        if rhi is not None:
                            self._rhi_fields.add(getCppPointer(rhi)[0])
                        self._field_atlas |= texture.isAtlasTexture()
                    if pointer in self._live:
                        continue
                    # Reused addresses still represent a new lifetime after
                    # destruction. Do not keep ownership of Qt's texture object.
                    token = len(self._seen)
                    self._seen[token] = name
                    self._live[pointer] = token
                texture.destroyed.connect(
                    lambda *_args, p=pointer: self._destroyed(p),
                    Qt.ConnectionType.DirectConnection,
                )
            with self._lock:
                self._samples += 1
        except Exception as exc:
            with self._lock:
                if len(self._errors) < 8:
                    self._errors.append(str(exc))

    def _destroyed(self, pointer):
        with self._lock:
            if self._live.pop(pointer, None) is not None:
                self._released += 1

    def snapshot(self):
        with self._lock:
            return {
                "samples": self._samples,
                "observed_field_textures": sum(
                    n.startswith("field") for n in self._seen.values()
                ),
                "observed_selection_textures": sum(
                    n.startswith("selection") for n in self._seen.values()
                ),
                "live_textures": len(self._live),
                "destroyed_textures": self._released,
                "errors": list(self._errors),
                "rhi_field_pointers": sorted(self._rhi_fields),
                "field_atlas_observed": self._field_atlas,
            }

    def detach(self):
        self._window.afterRendering.disconnect(self._sample)
