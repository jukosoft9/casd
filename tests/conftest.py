from __future__ import annotations

import base64
import hashlib
import http.client
import threading

import pytest

from casd.server import build_server


class Client:
    """Thin wrapper over http.client, one connection per request."""

    def __init__(self, port: int, credentials: str | None = None) -> None:
        self.port = port
        self.credentials = credentials

    def request(self, method: str, path: str, body: bytes | None = None, headers=None):
        headers = dict(headers or {})
        if self.credentials:
            headers["Authorization"] = "Basic " + base64.b64encode(self.credentials.encode()).decode()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.read(), response.headers
        finally:
            conn.close()

    def get(self, path: str):
        return self.request("GET", path)

    def put(self, path: str, body: bytes, **kwargs):
        return self.request("PUT", path, body, **kwargs)

    def put_cas(self, data: bytes) -> str:
        key = sha256(data)
        status, _, _ = self.put(f"/cas/{key}", data)
        assert status == 200
        return key

    def metrics(self) -> str:
        return self.get("/metrics")[1].decode()

    def as_user(self, credentials: str | None) -> "Client":
        return Client(self.port, credentials)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def start(tmp_path):
    """Factory that starts a server and returns (client, server)."""
    running = []

    def _start(max_bytes: int = 1 << 20, directory=None, **options):
        server = build_server(str(directory or tmp_path / "store"), max_bytes, port=0, **options)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        running.append((server, thread))
        return Client(server.server_address[1]), server

    yield _start
    for server, thread in running:
        server.shutdown()
        server.server_close()
        thread.join(5)


# -- hand-rolled protobuf encoding, the mirror image of casd.reapi ----------

def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def message(number: int, payload: bytes) -> bytes:
    return varint(number << 3 | 2) + varint(len(payload)) + payload


def integer(number: int, value: int) -> bytes:
    return varint(number << 3) + varint(value)


def digest(blob_hash: str, size: int) -> bytes:
    return message(1, blob_hash.encode()) + integer(2, size)


def action_result(files=(), trees=(), stdout=None, stderr=None) -> bytes:
    out = b""
    for i, (blob_hash, size) in enumerate(files):
        out += message(2, message(1, f"out/{i}.o".encode()) + message(2, digest(blob_hash, size)) + integer(4, 1))
    for i, (blob_hash, size) in enumerate(trees):
        out += message(3, message(1, f"out/dir{i}".encode()) + message(3, digest(blob_hash, size)))
    out += integer(4, 1)  # exit_code
    out += message(5, b"inline stdout")
    if stdout:
        out += message(6, digest(*stdout))
    if stderr:
        out += message(8, digest(*stderr))
    return out


def tree(root_files=(), child_files=()) -> bytes:
    def directory(files):
        return b"".join(
            message(1, message(1, f"f{i}".encode()) + message(2, digest(h, s)))
            for i, (h, s) in enumerate(files)
        )

    return message(1, directory(root_files)) + message(2, directory(child_files))
