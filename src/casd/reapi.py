"""Just enough protobuf decoding to find the blobs an ActionResult points at.

Bazel stores a serialised `build.bazel.remote.execution.v2.ActionResult` under
/ac/<action hash>. The result does not contain the outputs themselves, only
digests of blobs that live under /cas/. If the cache evicts one of those blobs
and still serves the ActionResult, the client gets a "hit" it cannot use. To
prevent that the server needs the list of referenced digests, which means
reading four message types. That is small enough to decode by hand rather than
pull in protobuf and generated code.

Field numbers come from remote_execution.proto (REAPI v2):

    ActionResult     output_files=2  output_directories=3
                     stdout_digest=6 stderr_digest=8
    OutputFile       digest=2
    OutputDirectory  tree_digest=3
    Tree             root=1  children=2          (both are Directory)
    Directory        files=1                     (FileNode)
    FileNode         digest=2
    Digest           hash=1  size_bytes=2
"""

from __future__ import annotations

from typing import Iterator

Digest = tuple[str, int]

_VARINT, _FIXED64, _BYTES, _FIXED32 = 0, 1, 2, 5


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(buf) or shift > 63:
            raise ValueError("truncated or oversized varint")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def _fields(buf: bytes) -> Iterator[tuple[int, int, int | bytes]]:
    """Yield (field number, wire type, value) for every field in a message."""
    pos = 0
    while pos < len(buf):
        tag, pos = _varint(buf, pos)
        number, wire_type = tag >> 3, tag & 7
        if number == 0:
            raise ValueError("field number 0")
        if wire_type == _VARINT:
            value, pos = _varint(buf, pos)
            yield number, wire_type, value
            continue
        if wire_type == _BYTES:
            length, pos = _varint(buf, pos)
        elif wire_type == _FIXED64:
            length = 8
        elif wire_type == _FIXED32:
            length = 4
        else:
            raise ValueError(f"unsupported wire type {wire_type}")
        end = pos + length
        if end > len(buf):
            raise ValueError("field runs past end of message")
        yield number, wire_type, buf[pos:end]
        pos = end


def _submessages(buf: bytes, number: int) -> Iterator[bytes]:
    for field, wire_type, value in _fields(buf):
        if field == number and wire_type == _BYTES:
            yield value  # type: ignore[misc]


def _digest(buf: bytes) -> Digest:
    digest_hash, size = "", 0
    for field, wire_type, value in _fields(buf):
        if field == 1 and wire_type == _BYTES:
            digest_hash = value.decode("ascii")  # type: ignore[union-attr]
        elif field == 2 and wire_type == _VARINT:
            size = value  # type: ignore[assignment]
    if len(digest_hash) != 64 or any(c not in "0123456789abcdef" for c in digest_hash):
        raise ValueError("digest hash is not lowercase sha256 hex")
    return digest_hash, size


def action_result_refs(buf: bytes) -> tuple[list[Digest], list[Digest]]:
    """Return (blob digests, tree digests) referenced by an ActionResult."""
    blobs: list[Digest] = []
    trees: list[Digest] = []
    try:
        for field, wire_type, value in _fields(buf):
            if wire_type != _BYTES:
                continue
            if field == 2:  # OutputFile
                blobs.extend(_digest(d) for d in _submessages(value, 2))  # type: ignore[arg-type]
            elif field == 3:  # OutputDirectory
                trees.extend(_digest(d) for d in _submessages(value, 3))  # type: ignore[arg-type]
            elif field in (6, 8):  # stdout_digest, stderr_digest
                blobs.append(_digest(value))  # type: ignore[arg-type]
    except UnicodeDecodeError as exc:
        raise ValueError("digest hash is not ascii") from exc
    return blobs, trees


def tree_file_digests(buf: bytes) -> list[Digest]:
    """Return the digest of every file in a Tree (root and all children)."""
    digests: list[Digest] = []
    try:
        for field, wire_type, directory in _fields(buf):
            if field not in (1, 2) or wire_type != _BYTES:
                continue
            for file_node in _submessages(directory, 1):  # type: ignore[arg-type]
                digests.extend(_digest(d) for d in _submessages(file_node, 2))
    except UnicodeDecodeError as exc:
        raise ValueError("digest hash is not ascii") from exc
    return digests
