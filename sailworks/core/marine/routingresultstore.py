"""Lazy, bounded storage for content-addressed routing result artifacts."""

from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Optional, Union

from .routingrecord import ArtifactRef, ArtifactIntegrityError, SchemaError

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_TYPE = "application/json"


class RoutingResultStore:
    """Store artifacts by digest and verify them before each fresh read.

    The store never scans or loads historical artifacts at construction time.
    ``load`` verifies byte length and SHA-256 before returning uncached bytes.
    Only verified bytes enter the LRU cache, which is bounded by entry count
    and total bytes.
    """

    def __init__(
        self,
        root: Union[str, Path],
        *,
        max_entries: int = 8,
        max_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int):
            raise SchemaError("max_entries must be a positive integer")
        if max_entries < 1:
            raise SchemaError("max_entries must be a positive integer")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
            raise SchemaError("max_bytes must be a non-negative integer")
        if max_bytes < 0:
            raise SchemaError("max_bytes must be a non-negative integer")
        self.root = Path(root)
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._cache: "OrderedDict[str, bytes]" = OrderedDict()
        self._cache_bytes = 0

    @property
    def cache_entries(self) -> int:
        return len(self._cache)

    @property
    def cache_bytes(self) -> int:
        return self._cache_bytes

    @property
    def cached_ids(self) -> tuple[str, ...]:
        return tuple(self._cache)

    def clear_cache(self) -> None:
        self._cache.clear()
        self._cache_bytes = 0

    def _artifact_id(self, value: object) -> str:
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise SchemaError("artifact identifiers must be lowercase SHA-256 digests")
        return value

    def _expected(self, ref: Union[ArtifactRef, str]):
        if isinstance(ref, ArtifactRef):
            return ref.artifact_id, ref.sha256, ref.byte_length
        artifact_id = self._artifact_id(ref)
        return artifact_id, artifact_id, None

    def _path(self, artifact_id: str) -> Path:
        path = self.root / artifact_id
        if path.parent != self.root:
            raise SchemaError("artifact path escaped the store root")
        return path

    def put(self, payload: bytes, *, media_type: str = _MEDIA_TYPE) -> ArtifactRef:
        if not isinstance(payload, bytes):
            raise TypeError("artifact payload must be bytes")
        if not isinstance(media_type, str) or not media_type:
            raise SchemaError("media_type must be a non-empty string")
        digest = hashlib.sha256(payload).hexdigest()
        path = self._path(digest)
        self.root.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self.root, prefix=".routing-")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return ArtifactRef(
            artifact_version=1,
            artifact_id=digest,
            sha256=digest,
            byte_length=len(payload),
            media_type=media_type,
            uri=f"artifact://sha256/{digest}",
        )

    def load(self, ref: Union[ArtifactRef, str]) -> bytes:
        artifact_id, expected_sha, expected_length = self._expected(ref)
        cached = self._cache.get(artifact_id)
        if cached is not None:
            if expected_length is not None and len(cached) != expected_length:
                raise ArtifactIntegrityError("artifact byte length does not match")
            self._cache.move_to_end(artifact_id)
            return cached
        path = self._path(artifact_id)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ArtifactIntegrityError("artifact is not readable") from exc
        digest = hashlib.sha256(payload).hexdigest()
        if digest != artifact_id or digest != expected_sha:
            raise ArtifactIntegrityError("artifact digest does not match")
        if expected_length is not None and len(payload) != expected_length:
            raise ArtifactIntegrityError("artifact byte length does not match")
        self._remember(artifact_id, payload)
        return payload

    def load_json(self, ref: Union[ArtifactRef, str]) -> object:
        payload = self.load(ref)
        try:
            return json.loads(payload.decode("utf-8"), parse_constant=_reject)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SchemaError("artifact is not valid UTF-8 JSON") from exc

    def _remember(self, artifact_id: str, payload: bytes) -> None:
        if len(payload) > self.max_bytes:
            return
        self._cache[artifact_id] = payload
        self._cache_bytes += len(payload)
        while len(self._cache) > self.max_entries or self._cache_bytes > self.max_bytes:
            _, evicted = self._cache.popitem(last=False)
            self._cache_bytes -= len(evicted)


def _reject(value: str) -> None:
    raise SchemaError(f"non-finite JSON constant {value} is not valid")
