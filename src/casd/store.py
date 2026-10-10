"""On-disk blob store with a size limit and least-recently-used eviction.

Layout under the root directory:

    ac/<first two hex chars>/<sha256>     action results
    cas/<first two hex chars>/<sha256>    content-addressed blobs
    tmp/                                  uploads in progress

A blob only appears under ac/ or cas/ through os.replace() from tmp/, so a
reader never sees a half-written file and a crash mid-upload leaves nothing
behind except a temp file, which is deleted at the next start.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import BinaryIO, Callable

KINDS = ("ac", "cas")
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

Key = tuple[str, str]


class FilesystemStore:
    def __init__(
        self,
        root: str | os.PathLike,
        max_bytes: int,
        on_evict: Callable[[int], None] | None = None,
    ) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes
        self._on_evict = on_evict or (lambda size: None)
        self._lock = threading.Lock()
        # Oldest entry first. Reads and writes move an entry to the end.
        self._index: OrderedDict[Key, int] = OrderedDict()
        self._total = 0
        self._tmp = self.root / "tmp"
        self._tmp.mkdir(parents=True, exist_ok=True)
        for leftover in self._tmp.iterdir():
            _unlink(leftover)
        self._load()

    def _load(self) -> None:
        found: list[tuple[float, Key, int]] = []
        for kind in KINDS:
            base = self.root / kind
            if not base.is_dir():
                continue
            for shard in os.scandir(base):
                if not shard.is_dir():
                    continue
                for entry in os.scandir(shard.path):
                    stat = entry.stat()
                    found.append((stat.st_mtime, (kind, entry.name), stat.st_size))
        # mtime is bumped on every read, so it is the last-used time.
        for _, key, size in sorted(found):
            self._index[key] = size
            self._total += size
        with self._lock:
            self._evict()

    def _path(self, kind: str, key: str) -> Path:
        return self.root / kind / key[:2] / key

    def stats(self) -> tuple[int, int]:
        """Return (bytes stored, number of entries)."""
        with self._lock:
            return self._total, len(self._index)

    def touch(self, kind: str, key: str) -> bool:
        """Mark an entry as recently used. Returns False if it is not stored."""
        with self._lock:
            if (kind, key) not in self._index:
                return False
            self._index.move_to_end((kind, key))
        try:
            os.utime(self._path(kind, key))
        except OSError:
            pass
        return True

    def open(self, kind: str, key: str) -> tuple[BinaryIO, int] | None:
        if not self.touch(kind, key):
            return None
        try:
            handle = open(self._path(kind, key), "rb")
        except FileNotFoundError:  # evicted between the index check and here
            return None
        return handle, os.fstat(handle.fileno()).st_size

    def read_bytes(self, kind: str, key: str) -> bytes | None:
        opened = self.open(kind, key)
        if opened is None:
            return None
        with opened[0] as handle:
            return handle.read()

    def temp_path(self) -> Path:
        return self._tmp / uuid.uuid4().hex

    def commit(self, kind: str, key: str, tmp: Path, size: int) -> bool:
        """Move a finished upload into place. Returns False if it was a duplicate."""
        dest = self._path(kind, key)
        with self._lock:
            if kind == "cas" and (kind, key) in self._index:
                # Same key means same bytes, so keep the copy readers may have open.
                self._index.move_to_end((kind, key))
                _unlink(tmp)
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            _replace(tmp, dest)
            self._total += size - self._index.get((kind, key), 0)
            self._index[(kind, key)] = size
            self._index.move_to_end((kind, key))
            self._evict()
            return True

    def delete(self, kind: str, key: str) -> None:
        with self._lock:
            size = self._index.pop((kind, key), None)
            if size is not None:
                self._total -= size
                _unlink(self._path(kind, key))

    def _evict(self) -> None:
        # Caller holds the lock. The newest entry is never evicted, so an
        # upload that was just accepted is always readable afterwards.
        attempts = len(self._index)
        while self._total > self.max_bytes and len(self._index) > 1 and attempts > 0:
            attempts -= 1
            (kind, key), size = self._index.popitem(last=False)
            try:
                self._path(kind, key).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # Windows refuses to delete a file that is being read. Keep it
                # and try the next oldest entry.
                self._index[(kind, key)] = size
                continue
            self._total -= size
            self._on_evict(size)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _replace(src: Path, dest: Path) -> None:
    # On Windows os.replace fails while another thread has dest open for
    # reading. Readers finish quickly, so retry briefly.
    for attempt in range(50):
        try:
            os.replace(src, dest)
            return
        except PermissionError:
            if attempt == 49:
                raise
            time.sleep(0.01)
