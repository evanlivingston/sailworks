"""Persisted, versioned per-cell ENC scene store.

A :class:`~sailworks.rendering.vectordrawer.encscene.CellScene` is immutable
frozen data, so it can be written once and read back on the next launch
without re-reading the cell through GDAL.  The measured budget drives every
choice here: a cell is ~12.7 MB and takes ~309 ms to build, so a load must stay
well inside that to be worth a file on disk.

Encoding, deliberately not pickle.  A pickle is executable input, cannot be
versioned cleanly and ties the file to the interpreter.  Instead the scene is a
header of JSON descriptors (identity, string tables, section table) followed by
little-endian numpy arrays and two raw blobs:

* record columns (layer, sequence, fid, class index) as integer arrays;
* one ``float64`` envelope array for every record;
* the WKB geometries concatenated once, with a ``uint64`` offset array;
* attributes as per-record offsets into parallel field/kind/value arrays, so
  the tuple order the builder produced is preserved exactly;
* the per-layer grid as cell counts plus one concatenated offset array.

Nothing is recomputed on load that would need a pass over the geometry.  Two
values are derived rather than stored: ``LayerCandidates.start`` is stored (it
is 8 bytes and the invariant "records are grouped by layer" is not worth
leaning on for correctness), while ``CellScene.nbytes`` is stored because it is
cache accounting only and recomputing it walks every record.

A schema hash covers the format version, the dataclass fields, the drawer's
layer/attribute rules and the grid cap, so a code change that alters what a
scene means invalidates the files instead of loading stale binary.  A file that
is truncated, unreadable or does not decode is a miss and is deleted; the cell
is rebuilt.  A cache is never a failure: every store method tolerates IO errors
and returns "not cached".
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
import time
from dataclasses import fields
from pathlib import Path

import numpy as np

from sailworks.rendering.vectordrawer import encscene
from sailworks.rendering.vectordrawer.encscene import CellScene, FeatureRecord

MAGIC = b"ENCSCENE"
FORMAT_VERSION = 1
SUFFIX = ".encscene"

#: Cells resident across a restart.  A cell is ~12.7 MB at this camera and a
#: coastal region is a few dozen cells; 256 MiB holds about twenty, matches the
#: in-process mesh budget, and stays a bounded, evictable amount of disk.
DEFAULT_MAX_BYTES = 256 * 1024 * 1024

_NONE, _INT, _FLOAT, _STR, _BOOL, _BYTES, _TUPLE = range(7)


class SceneFormatError(ValueError):
    """The bytes are not a scene this build can read."""


def _stage(timings, name, started):
    """Accumulate one opt-in diagnostic duration; a no-op when ``None``."""
    if timings is not None:
        timings[name] = timings.get(name, 0.0) + (time.perf_counter() - started)


def _schema_hash() -> str:
    """A hash of everything that decides what a stored scene means."""
    tokens = (
        FORMAT_VERSION,
        [field.name for field in fields(FeatureRecord)],
        [field.name for field in fields(encscene.LayerCandidates)],
        sorted(encscene.SKIP_LAYERS),
        sorted((key, value) for key, value in encscene.ATTRIBUTE_FIELDS.items()),
        encscene._GRID_MAX_SIDE,
    )
    return hashlib.sha256(repr(tokens).encode("utf-8")).hexdigest()[:16]


SCHEMA_HASH = _schema_hash()


def scene_key(path, version, schema=SCHEMA_HASH) -> str:
    """The file name identity: the cell, its version and the schema."""
    payload = "\0".join(
        (os.path.abspath(os.fspath(path)), repr(tuple(version)), str(schema))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]


def _value_kind(value) -> int:
    """The encoding kind of one attribute value, or refuse the scene.

    Only the scalar types OGR returns for the drawer's attribute fields and
    the tuples :func:`encscene._freeze_value` makes of its list fields are
    encodable.  Anything else is refused rather than serialised lossily, so a
    future attribute is never cached as a value it is not.
    """
    if value is None:
        return _NONE
    if isinstance(value, bool):
        return _BOOL
    if isinstance(value, int):
        return _INT
    if isinstance(value, float):
        return _FLOAT
    if isinstance(value, str):
        return _STR
    if isinstance(value, (bytes, bytearray)):
        return _BYTES
    if isinstance(value, tuple):
        return _TUPLE
    raise SceneFormatError(f"cannot encode attribute value {value!r}")


def _encode_value(value, target) -> None:
    """Append one value and, for a tuple, its items' pairs in order."""
    kind = _value_kind(value)
    position = len(target["kind"])
    target["field"].append(None)
    target["kind"].append(kind)
    target["int"].append(0)
    target["float"].append(0.0)
    target["text_ends"].append(len(target["text"]))
    if kind in (_INT, _BOOL):
        target["int"][position] = value
    elif kind == _FLOAT:
        target["float"][position] = value
    elif kind in (_STR, _BYTES):
        text = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        target["text"].extend(text)
        target["text_ends"][position] = len(target["text"])
    elif kind == _TUPLE:
        target["int"][position] = len(value)
        for item in value:
            _encode_value(item, target)


def encode_scene(scene: CellScene, schema: str = SCHEMA_HASH) -> bytes:
    """Encode a whole-cell scene as bytes; deterministic for equal scenes."""
    records = scene.records
    count = len(records)

    classes: list[str] = []
    class_index: dict[str, int] = {}
    keys: list[str] = []
    key_index: dict[str, int] = {}
    layers = np.empty(count, dtype="<u4")
    sequences = np.empty(count, dtype="<u8")
    fids = np.empty(count, dtype="<i8")
    class_codes = np.empty(count, dtype="<u2")
    envelopes = np.empty((count, 4), dtype="<f8")

    attr_starts = np.empty(count + 1, dtype="<u4")
    target = {
        "field": [],
        "kind": [],
        "int": [],
        "float": [],
        "text_ends": [],
        "text": bytearray(),
    }

    for index, record in enumerate(records):
        layers[index] = record.layer_index
        sequences[index] = record.sequence
        fids[index] = record.fid
        code = class_index.get(record.source_class)
        if code is None:
            code = len(classes)
            class_index[record.source_class] = code
            classes.append(record.source_class)
        class_codes[index] = code
        envelopes[index] = record.envelope
        attr_starts[index] = len(target["kind"])
        for key, value in record.attributes:
            field = key_index.get(key)
            if field is None:
                field = len(keys)
                key_index[key] = field
                keys.append(key)
            first = len(target["kind"])
            _encode_value(value, target)
            target["field"][first:] = [field] * (len(target["kind"]) - first)
    attr_starts[count] = len(target["kind"])
    attr_field = target["field"]
    attr_kind = target["kind"]
    attr_int = target["int"]
    attr_float = target["float"]
    attr_text_starts = [0] + target["text_ends"]
    text_blob = target["text"]

    wkb_offsets = np.zeros(count + 1, dtype="<u8")
    for index, record in enumerate(records):
        wkb_offsets[index + 1] = wkb_offsets[index] + len(record.wkb)
    wkb_blob = b"".join(record.wkb for record in records)

    layer_count = len(scene.layers)
    layer_index = np.empty(layer_count, dtype="<u4")
    layer_class = np.empty(layer_count, dtype="<u2")
    layer_start = np.empty(layer_count, dtype="<u8")
    layer_grid = np.empty((layer_count, 4), dtype="<f8")
    layer_side = np.empty(layer_count, dtype="<u8")
    layer_records = np.empty(layer_count, dtype="<u4")
    cell_counts = np.empty(0, dtype="<u4")
    cell_data = np.empty(0, dtype="<u4")
    count_chunks = []
    data_chunks = []
    for position, layer in enumerate(scene.layers):
        layer_index[position] = layer.layer_index
        code = class_index.get(layer.source_class)
        if code is None:
            code = len(classes)
            class_index[layer.source_class] = code
            classes.append(layer.source_class)
        layer_class[position] = code
        layer_start[position] = layer.start
        grid = layer.grid
        layer_grid[position] = (grid[0], grid[1], grid[2], grid[3])
        layer_side[position] = int(grid[4])
        layer_records[position] = len(layer.envelopes)
        sizes = [len(cell) for cell in layer.cells]
        count_chunks.append(np.asarray(sizes, dtype="<u4"))
        flattened = [offset for cell in layer.cells for offset in cell]
        data_chunks.append(np.asarray(flattened, dtype="<u4"))
    if count_chunks:
        cell_counts = np.concatenate(count_chunks)
        cell_data = np.concatenate(data_chunks)

    sections = [
        ("record_layer", layers),
        ("record_sequence", sequences),
        ("record_fid", fids),
        ("record_class", class_codes),
        ("envelopes", envelopes),
        ("wkb_offsets", wkb_offsets),
        ("attr_starts", attr_starts),
        ("attr_field", np.asarray(attr_field, dtype="<u2")),
        ("attr_kind", np.asarray(attr_kind, dtype="<u1")),
        ("attr_int", np.asarray(attr_int, dtype="<i8")),
        ("attr_float", np.asarray(attr_float, dtype="<f8")),
        ("attr_text_offsets", np.asarray(attr_text_starts, dtype="<u8")),
        ("layer_index", layer_index),
        ("layer_class", layer_class),
        ("layer_start", layer_start),
        ("layer_grid", layer_grid),
        ("layer_side", layer_side),
        ("layer_records", layer_records),
        ("cell_counts", cell_counts),
        ("cell_data", cell_data),
    ]
    header = {
        "format": FORMAT_VERSION,
        "schema": schema,
        "path": scene.path,
        "version": list(scene.version),
        "nbytes": int(scene.nbytes),
        "count": count,
        "layer_count": layer_count,
        "classes": classes,
        "attribute_keys": keys,
        "sections": [],
    }
    blobs = [wkb_blob, bytes(text_blob)]
    body = bytearray(b"".join(array.tobytes() for _, array in sections))
    body.extend(blobs[0])
    body.extend(blobs[1])
    offset = 0
    for name, array in sections:
        nbytes = array.nbytes
        header["sections"].append([name, offset, nbytes])
        offset += nbytes
    header["sections"].append(["wkb", offset, len(blobs[0])])
    offset += len(blobs[0])
    header["sections"].append(["text", offset, len(blobs[1])])
    raw_header = json.dumps(header, separators=(",", ":"), sort_keys=True).encode()
    return b"".join(
        (MAGIC, struct.pack("<Q", len(raw_header)), raw_header, bytes(body))
    )


def _section(body, name, sections) -> memoryview:
    try:
        offset, nbytes = sections[name]
    except KeyError:
        raise SceneFormatError(f"missing section {name}") from None
    if offset < 0 or nbytes < 0 or offset + nbytes > len(body):
        raise SceneFormatError(f"section {name} runs past the end of the file")
    return body[offset : offset + nbytes]


def _array(view, dtype, count):
    if count < 0 or len(view) != np.dtype(dtype).itemsize * count:
        raise SceneFormatError("section length does not match its count")
    return np.frombuffer(view, dtype=dtype, count=count)


def decode_scene(data, schema: str = SCHEMA_HASH, timings=None) -> CellScene:
    """Decode stored bytes into a scene, optionally timing the decode.

    ``timings`` is an opt-in diagnostic mapping that receives ``"decode_scene"``
    seconds; ``None`` (the default) records nothing.  The decoded value and
    every error are exactly what :func:`_decode_scene` produces.
    """
    started = time.perf_counter()
    scene = _decode_scene(data, schema)
    _stage(timings, "decode_scene", started)
    return scene


def _decode_scene(data, schema: str = SCHEMA_HASH) -> CellScene:
    """Decode bytes written by :func:`encode_scene`.

    Raises :class:`SceneFormatError` for anything that is not a complete scene
    of this schema; never returns a partially populated one.
    """
    if len(data) < 16 or data[: len(MAGIC)] != MAGIC:
        raise SceneFormatError("not an ENC scene file")
    (header_size,) = struct.unpack("<Q", data[len(MAGIC) : len(MAGIC) + 8])
    start = len(MAGIC) + 8 + header_size
    if start > len(data):
        raise SceneFormatError("truncated scene header")
    try:
        header = json.loads(data[len(MAGIC) + 8 : start])
    except ValueError as error:
        raise SceneFormatError(f"unreadable scene header: {error}") from None
    if not isinstance(header, dict) or header.get("format") != FORMAT_VERSION:
        raise SceneFormatError("scene format is not this build's")
    if header.get("schema") != schema:
        raise SceneFormatError(f"scene schema {header.get('schema')} is not {schema}")
    sections = {
        str(name): (int(offset), int(nbytes))
        for name, offset, nbytes in header.get("sections") or ()
    }
    body = memoryview(data)[start:]
    count = int(header.get("count", -1))
    layer_count = int(header.get("layer_count", -1))
    if count < 0 or layer_count < 0:
        raise SceneFormatError("scene header has no counts")

    def part(name, dtype, length=count):
        return _array(_section(body, name, sections), dtype, length)

    layers = part("record_layer", "<u4")
    sequences = part("record_sequence", "<u8")
    fids = part("record_fid", "<i8")
    class_codes = part("record_class", "<u2")
    envelopes = part("envelopes", "<f8", count * 4).reshape(count, 4)
    wkb_offsets = part("wkb_offsets", "<u8", count + 1)
    attr_starts = part("attr_starts", "<u4", count + 1)
    pairs = int(attr_starts[count]) if count else 0
    attr_field = part("attr_field", "<u2", pairs)
    attr_kind = part("attr_kind", "<u1", pairs)
    attr_int = part("attr_int", "<i8", pairs)
    attr_float = part("attr_float", "<f8", pairs)
    attr_text = part("attr_text_offsets", "<u8", pairs + 1)
    layer_index = part("layer_index", "<u4", layer_count)
    layer_class = part("layer_class", "<u2", layer_count)
    layer_start = part("layer_start", "<u8", layer_count)
    layer_grid = part("layer_grid", "<f8", layer_count * 4)
    layer_side = part("layer_side", "<u8", layer_count)
    layer_records = part("layer_records", "<u4", layer_count)
    counts = _array(
        _section(body, "cell_counts", sections),
        "<u4",
        len(_section(body, "cell_counts", sections)) // 4,
    )
    cell_data = _array(
        _section(body, "cell_data", sections),
        "<u4",
        len(_section(body, "cell_data", sections)) // 4,
    )

    wkb_blob = _section(body, "wkb", sections)
    text_blob = _section(body, "text", sections)
    classes = [str(name) for name in header.get("classes") or ()]
    keys = [str(name) for name in header.get("attribute_keys") or ()]
    if class_codes.size and int(class_codes.max()) >= len(classes):
        raise SceneFormatError("a record names a class the header does not hold")
    if attr_field.size and int(attr_field.max()) >= len(keys):
        raise SceneFormatError("a record names an attribute the header does not hold")
    if int(wkb_offsets[count]) != len(wkb_blob):
        raise SceneFormatError("WKB offsets do not cover the WKB blob")
    if int(attr_text[pairs]) != len(text_blob):
        raise SceneFormatError("attribute text offsets do not cover the text blob")

    def text_of(index) -> bytes:
        return bytes(text_blob[int(attr_text[index]) : int(attr_text[index + 1])])

    def read_value(index, last):
        """One attribute value and the next pair; a tuple reads its items."""
        if index >= last:
            raise SceneFormatError("attribute value runs past its record")
        kind = int(attr_kind[index])
        if kind == _NONE:
            return None, index + 1
        if kind == _INT:
            return int(attr_int[index]), index + 1
        if kind == _FLOAT:
            return float(attr_float[index]), index + 1
        if kind == _BOOL:
            return bool(attr_int[index]), index + 1
        if kind == _STR:
            return text_of(index).decode("utf-8"), index + 1
        if kind == _BYTES:
            return text_of(index), index + 1
        if kind == _TUPLE:
            items = []
            cursor = index + 1
            for _ in range(int(attr_int[index])):
                item, cursor = read_value(cursor, last)
                items.append(item)
            return tuple(items), cursor
        raise SceneFormatError(f"unknown attribute kind {kind}")

    records = []
    for position in range(count):
        first = int(attr_starts[position])
        last = int(attr_starts[position + 1])
        if last < first or last > pairs:
            raise SceneFormatError("record attribute offsets are not ordered")
        attributes = []
        cursor = first
        while cursor < last:
            key = keys[int(attr_field[cursor])]
            value, cursor = read_value(cursor, last)
            attributes.append((key, value))
        low, high = int(wkb_offsets[position]), int(wkb_offsets[position + 1])
        records.append(
            FeatureRecord(
                layer_index=int(layers[position]),
                sequence=int(sequences[position]),
                fid=int(fids[position]),
                source_class=classes[int(class_codes[position])],
                attributes=tuple(attributes),
                envelope=tuple(envelopes[position].tolist()),
                wkb=bytes(wkb_blob[low:high]),
            )
        )

    layer_rows = []
    cursor = 0
    data_cursor = 0
    for position in range(layer_count):
        side = int(layer_side[position])
        size = side * side
        if cursor + size > counts.size:
            raise SceneFormatError("grid cell counts run past the section")
        chunk = counts[cursor : cursor + size]
        cursor += size
        cells = []
        for cell_size in chunk:
            cell_size = int(cell_size)
            if data_cursor + cell_size > cell_data.size:
                raise SceneFormatError("grid offsets run past the section")
            cells.append(
                tuple(
                    int(value)
                    for value in cell_data[data_cursor : data_cursor + cell_size]
                )
            )
            data_cursor += cell_size
        layer_first = int(layer_start[position])
        layer_size = int(layer_records[position])
        if layer_first + layer_size > count:
            raise SceneFormatError("a layer's records run past the record columns")
        grid = layer_grid[position * 4 : position * 4 + 4]
        layer_rows.append(
            encscene.LayerCandidates(
                layer_index=int(layer_index[position]),
                source_class=classes[int(layer_class[position])],
                start=layer_first,
                envelopes=tuple(
                    tuple(envelopes[row].tolist())
                    for row in range(layer_first, layer_first + layer_size)
                ),
                grid=tuple(float(value) for value in grid) + (side,),
                cells=tuple(cells),
            )
        )
    if cursor != counts.size:
        raise SceneFormatError("grid cell counts do not cover the section")
    if data_cursor != cell_data.size:
        raise SceneFormatError("grid offsets do not cover the section")
    return CellScene(
        path=str(header.get("path") or ""),
        version=tuple(int(value) for value in header.get("version") or ()),
        records=tuple(records),
        layers=tuple(layer_rows),
        nbytes=int(header.get("nbytes", 0)),
    )


class SceneStore:
    """A bounded on-disk LRU of whole-cell scenes, keyed by identity and schema.

    Single-worker ownership like the in-memory cache; no lock.  A file that is
    missing, stale, truncated or unreadable is a miss, never a failure.
    """

    def __init__(self, directory, max_bytes: int = DEFAULT_MAX_BYTES, schema=None):
        max_bytes = int(max_bytes)
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.directory = Path(directory)
        self.max_bytes = max_bytes
        self.schema = SCHEMA_HASH if schema is None else str(schema)
        self.hits = self.misses = self.writes = self.evictions = self.errors = 0

    def path_for(self, path, version) -> Path:
        return self.directory / (scene_key(path, version, self.schema) + SUFFIX)

    def get(self, path, version, timings=None):
        """The stored scene for this exact cell identity, or ``None``.

        ``timings`` is an opt-in diagnostic mapping for the decode.
        """
        target = self.path_for(path, version)
        try:
            raw = target.read_bytes()
        except FileNotFoundError:
            self.misses += 1
            return None
        except OSError:
            self.errors += 1
            self.misses += 1
            return None
        try:
            scene = decode_scene(raw, self.schema, timings)
            if os.path.abspath(scene.path) != os.path.abspath(os.fspath(path)) or tuple(
                scene.version
            ) != tuple(version):
                raise SceneFormatError("scene identity does not match the request")
        except Exception:  # a corrupt file is a miss, never a crash
            self.errors += 1
            self.misses += 1
            self._discard(target)
            return None
        try:
            os.utime(target, None)
        except OSError:
            pass
        self.hits += 1
        return scene

    def put(self, scene: CellScene) -> bool:
        """Write one scene, evicting the least recently used files over the cap."""
        try:
            raw = encode_scene(scene, self.schema)
        except (SceneFormatError, TypeError, ValueError):
            self.errors += 1
            return False
        if len(raw) > self.max_bytes:
            self.errors += 1
            return False
        target = self.path_for(scene.path, scene.version)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(
                dir=self.directory, prefix=".tmp-", suffix=SUFFIX
            )
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        except OSError:
            self.errors += 1
            return False
        self.writes += 1
        self.evict()
        return True

    def load_or_build(self, path, version, loader, timings=None):
        """The stored scene, or ``loader()``'s scene stored for the next launch.

        ``timings`` is an opt-in diagnostic mapping: ``"store_get"`` is the
        persisted read plus its decode (a store hit) and ``"store_build"`` is
        ``loader()``, the fresh OGR read that only happens on a store miss.
        ``None`` (the default) records nothing.
        """
        started = time.perf_counter()
        scene = self.get(path, version, timings)
        _stage(timings, "store_get", started)
        if scene is not None:
            return scene
        started = time.perf_counter()
        built = loader()
        _stage(timings, "store_build", started)
        if built is not None:
            self.put(built)
        return built

    def evict(self) -> None:
        """Delete least recently used files until the directory is under the cap."""
        try:
            entries = sorted(
                (
                    (entry.stat().st_mtime_ns, entry.stat().st_size, entry)
                    for entry in self.directory.iterdir()
                    if entry.suffix == SUFFIX
                ),
                key=lambda item: item[0],
            )
        except OSError:
            return
        total = sum(size for _, size, _ in entries)
        for _, size, entry in entries:
            if total <= self.max_bytes:
                break
            try:
                entry.unlink()
            except OSError:
                continue
            total -= size
            self.evictions += 1

    def size_bytes(self) -> int:
        try:
            return sum(
                entry.stat().st_size
                for entry in self.directory.iterdir()
                if entry.suffix == SUFFIX
            )
        except OSError:
            return 0

    @staticmethod
    def _discard(target) -> None:
        try:
            target.unlink()
        except OSError:
            pass
