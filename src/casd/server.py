"""HTTP front end: Bazel's remote cache protocol over a FilesystemStore.

The protocol is two namespaces and three verbs:

    GET|HEAD|PUT /ac/<sha256 of the action>     -> serialised ActionResult
    GET|HEAD|PUT /cas/<sha256 of the content>   -> the blob itself

What the server adds on top of plain file serving:

  * a CAS upload is hashed while it streams in and rejected if the hash does
    not match the key, so a corrupt upload can never poison the cache;
  * an action result is only served if every blob it references is still in
    the store (see reapi.py), otherwise it is dropped and reported as a miss;
  * optional Basic auth with separate read and write credentials.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import shutil
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, reapi
from .metrics import Metrics
from .store import EMPTY_SHA256, FilesystemStore

log = logging.getLogger("casd")

_BLOB_PATH = re.compile(r"^/(ac|cas)/([0-9a-f]{64})$")
_CHUNK = 1 << 16
# Action results are a few hundred bytes to a few KB. Anything larger than this
# is served without checking its references.
_MAX_AC_VALIDATE = 4 << 20


class _TooLarge(Exception):
    pass


class _BadRequest(Exception):
    pass


class CacheServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(
        self,
        address: tuple[str, int],
        store: FilesystemStore,
        metrics: Metrics,
        *,
        max_blob_bytes: int = 2 << 30,
        validate_ac: bool = True,
        read_credentials: str | None = None,
        write_credentials: str | None = None,
    ) -> None:
        self.store = store
        self.metrics = metrics
        # A blob bigger than the whole store would evict everything else.
        self.max_blob_bytes = min(max_blob_bytes, store.max_bytes)
        self.validate_ac = validate_ac
        self.read_credentials = read_credentials
        self.write_credentials = write_credentials
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive; Bazel reuses connections
    server_version = f"casd/{__version__}"
    timeout = 120  # seconds of socket inactivity before a connection is dropped
    server: CacheServer

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        log.debug("%s %s", self.address_string(), format % args)

    # -- helpers ---------------------------------------------------------

    def _reply(self, status: int, body: bytes = b"", content_type: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if status == 401:
            self.send_header("WWW-Authenticate", 'Basic realm="casd"')
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _count(self, kind: str, result: str) -> None:
        self.server.metrics.inc(
            "casd_requests_total", method=self.command, kind=kind, result=result
        )

    def _authorised(self, write: bool) -> bool:
        read, write_creds = self.server.read_credentials, self.server.write_credentials
        accepted = [write_creds] if write else [read, write_creds]
        if write and write_creds is None:
            # No write credentials configured: writes are open only if reads are too.
            return read is None
        if not write and read is None:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            supplied = base64.b64decode(header[6:], validate=True)
        except ValueError:
            return False
        return any(
            c is not None and hmac.compare_digest(supplied, c.encode()) for c in accepted
        )

    def _route(self, write: bool = False) -> tuple[str, str] | None:
        """Parse the path and check auth. Replies and returns None on failure."""
        match = _BLOB_PATH.match(self.path)
        if match is None:
            self.close_connection = write  # an unread request body may follow
            self._reply(404, b"expected /ac/<sha256> or /cas/<sha256>\n")
            return None
        kind, key = match.groups()
        if not self._authorised(write):
            self.close_connection = write
            self._count(kind, "unauthorised")
            self._reply(401, b"unauthorised\n")
            return None
        return kind, key

    # -- GET / HEAD ------------------------------------------------------

    def do_GET(self) -> None:
        started = time.perf_counter()
        try:
            if self.path == "/healthz":
                self._reply(200, b"ok\n")
            elif self.path == "/metrics":
                body = self.server.metrics.render().encode()
                self._reply(200, body, "text/plain; version=0.0.4")
            else:
                self._get_blob()
        except OSError:
            self.close_connection = True  # client went away mid-response
        finally:
            self.server.metrics.observe(self.command, time.perf_counter() - started)

    do_HEAD = do_GET

    def _get_blob(self) -> None:
        route = self._route()
        if route is None:
            return
        kind, key = route
        store = self.server.store
        if kind == "ac" and self.server.validate_ac:
            body = store.read_bytes("ac", key)
            if body is not None and not self._references_present(body):
                store.delete("ac", key)
                self.server.metrics.inc("casd_ac_invalidated_total")
                body = None
            if body is None:
                self._count(kind, "miss")
                self._reply(404)
                return
            self._count(kind, "hit")
            self._reply(200, body, "application/octet-stream")
            if self.command == "GET":
                self.server.metrics.inc("casd_sent_bytes_total", len(body))
            return

        opened = store.open(kind, key)
        if opened is None:
            self._count(kind, "miss")
            self._reply(404)
            return
        handle, size = opened
        with handle:
            self._count(kind, "hit")
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            if self.command == "GET":
                shutil.copyfileobj(handle, self.wfile, _CHUNK)
                self.server.metrics.inc("casd_sent_bytes_total", size)

    def _references_present(self, action_result: bytes) -> bool:
        if len(action_result) > _MAX_AC_VALIDATE:
            return True
        store = self.server.store
        try:
            blobs, trees = reapi.action_result_refs(action_result)
        except ValueError:
            return True  # not an ActionResult; serve it as an opaque value
        for tree_hash, _ in trees:
            tree = store.read_bytes("cas", tree_hash)
            if tree is None:
                return False
            try:
                blobs.extend(reapi.tree_file_digests(tree))
            except ValueError:
                return False
        for blob_hash, size in blobs:
            # Bazel never uploads the empty blob, so it is always "present".
            if size == 0 or blob_hash == EMPTY_SHA256:
                continue
            # touch() also refreshes the blob, so outputs of a live action
            # are not evicted before the action result that points at them.
            if not store.touch("cas", blob_hash):
                return False
        return True

    # -- PUT -------------------------------------------------------------

    def do_PUT(self) -> None:
        started = time.perf_counter()
        try:
            self._put_blob()
        except OSError:
            self.close_connection = True
        finally:
            self.server.metrics.observe("PUT", time.perf_counter() - started)

    def _put_blob(self) -> None:
        route = self._route(write=True)
        if route is None:
            return
        kind, key = route
        store = self.server.store
        tmp = store.temp_path()
        hasher = hashlib.sha256()
        try:
            with open(tmp, "wb") as out:
                size = self._receive_body(out, hasher)
            if kind == "cas" and hasher.hexdigest() != key:
                self._count(kind, "rejected")
                self._reply(400, b"content does not match the sha256 in the path\n")
                return
            created = store.commit(kind, key, tmp, size)
        except _TooLarge:
            self.close_connection = True
            self._count(kind, "too_large")
            self._reply(413, b"blob exceeds the size limit\n")
            return
        except _BadRequest as exc:
            self.close_connection = True
            self._count(kind, "bad_request")
            self._reply(exc.args[0], exc.args[1])
            return
        except OSError:
            # Client went away or timed out mid-upload. Nothing was committed.
            self.close_connection = True
            self._count(kind, "aborted")
            return
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        self.server.metrics.inc("casd_received_bytes_total", size)
        self._count(kind, "stored" if created else "duplicate")
        self._reply(200)

    def _receive_body(self, out, hasher) -> int:
        limit = self.server.max_blob_bytes
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            total = 0
            while True:
                line = self.rfile.readline(1024)
                try:
                    chunk_size = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError:
                    raise _BadRequest(400, b"malformed chunk size\n") from None
                if chunk_size == 0:
                    while self.rfile.readline(8192) not in (b"\r\n", b"\n", b""):
                        pass  # trailers
                    return total
                total += chunk_size
                if total > limit:
                    raise _TooLarge
                self._copy(chunk_size, out, hasher)
                self.rfile.readline(8)  # CRLF after the chunk data
        try:
            length = int(self.headers["Content-Length"])
        except (TypeError, ValueError):
            raise _BadRequest(411, b"Content-Length or chunked encoding required\n") from None
        if length < 0:
            raise _BadRequest(400, b"negative Content-Length\n")
        if length > limit:
            raise _TooLarge
        self._copy(length, out, hasher)
        return length

    def _copy(self, remaining: int, out, hasher) -> None:
        while remaining:
            chunk = self.rfile.read(min(remaining, _CHUNK))
            if not chunk:
                raise ConnectionError("client closed the connection mid-body")
            out.write(chunk)
            hasher.update(chunk)
            remaining -= len(chunk)


def build_server(
    directory: str,
    max_bytes: int,
    host: str = "127.0.0.1",
    port: int = 8080,
    **options,
) -> CacheServer:
    metrics = Metrics()

    def evicted(size: int) -> None:
        metrics.inc("casd_evictions_total")
        metrics.inc("casd_evicted_bytes_total", size)

    store = FilesystemStore(directory, max_bytes, on_evict=evicted)
    metrics.gauge("casd_store_bytes", "Bytes currently stored.", lambda: store.stats()[0])
    metrics.gauge("casd_store_items", "Entries currently stored.", lambda: store.stats()[1])
    metrics.gauge("casd_store_max_bytes", "Configured size limit.", lambda: max_bytes)
    return CacheServer((host, port), store, metrics, **options)
