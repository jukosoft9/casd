from __future__ import annotations

import os
import socket
import threading
import time

import pytest

from casd.__main__ import main, parse_size
from casd.store import EMPTY_SHA256
from conftest import action_result, sha256, tree

AC_KEY = "1" * 64


def blob(tag: str, size: int = 1000) -> bytes:
    return (tag.encode() * size)[:size]


def stored_files(server, kind: str) -> list[str]:
    return sorted(p.name for p in (server.store.root / kind).glob("*/*"))


def temp_files(server) -> list[str]:
    return os.listdir(server.store.root / "tmp")


# -- basic protocol ------------------------------------------------------

def test_cas_round_trip(start):
    client, _ = start()
    data = b"hello bazel"
    key = sha256(data)
    assert client.get(f"/cas/{key}")[0] == 404
    assert client.put(f"/cas/{key}", data)[0] == 200
    status, body, headers = client.get(f"/cas/{key}")
    assert (status, body) == (200, data)
    assert headers["Content-Length"] == str(len(data))


def test_head_reports_size_without_body(start):
    client, _ = start()
    key = client.put_cas(b"x" * 123)
    status, body, headers = client.request("HEAD", f"/cas/{key}")
    assert (status, body, headers["Content-Length"]) == (200, b"", "123")
    assert client.request("HEAD", f"/cas/{'0' * 64}")[0] == 404


def test_chunked_upload(start):
    client, _ = start()
    data = blob("c", 200_000)
    key = sha256(data)
    pieces = iter([data[:70_000], data[70_000:150_000], data[150_000:]])
    assert client.put(f"/cas/{key}", pieces)[0] == 200
    assert client.get(f"/cas/{key}")[1] == data


def test_keep_alive_serves_several_requests_on_one_connection(start):
    import http.client

    client, _ = start()
    conn = http.client.HTTPConnection("127.0.0.1", client.port, timeout=10)
    for i in range(5):
        data = blob(str(i))
        conn.request("PUT", f"/cas/{sha256(data)}", body=data)
        response = conn.getresponse()
        response.read()
        assert response.status == 200
        # A rejected upload must not desynchronise the connection either.
        conn.request("PUT", f"/cas/{'f' * 64}", body=data)
        response = conn.getresponse()
        response.read()
        assert response.status == 400
    conn.close()


@pytest.mark.parametrize("path", ["/", "/cas/abc", "/cas/" + "G" * 64, "/cas/" + "A" * 64, "/other/" + "0" * 64])
def test_unknown_paths_are_404(start, path):
    client, _ = start()
    assert client.get(path)[0] == 404
    assert client.put(path, b"data")[0] == 404


def test_put_without_length_is_411(start):
    client, _ = start()
    with socket.create_connection(("127.0.0.1", client.port), timeout=10) as sock:
        sock.sendall(f"PUT /cas/{'0' * 64} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        assert sock.recv(1024).startswith(b"HTTP/1.1 411")


# -- integrity -----------------------------------------------------------

def test_cas_upload_with_wrong_hash_is_rejected_and_not_stored(start):
    client, server = start()
    wrong_key = sha256(b"what the client claims")
    assert client.put(f"/cas/{wrong_key}", b"what the client sent")[0] == 400
    assert client.get(f"/cas/{wrong_key}")[0] == 404
    assert stored_files(server, "cas") == []
    assert temp_files(server) == []
    assert 'result="rejected"' in client.metrics()


def test_upload_cut_off_mid_body_leaves_nothing_behind(start):
    client, server = start()
    data = blob("p", 100_000)
    key = sha256(data)
    with socket.create_connection(("127.0.0.1", client.port), timeout=10) as sock:
        sock.sendall(f"PUT /cas/{key} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(data)}\r\n\r\n".encode())
        sock.sendall(data[:40_000])
    deadline = time.time() + 5
    while 'result="aborted"' not in client.metrics() and time.time() < deadline:
        time.sleep(0.05)
    assert 'result="aborted"' in client.metrics()
    assert client.get(f"/cas/{key}")[0] == 404
    assert stored_files(server, "cas") == []
    assert temp_files(server) == []


def test_upload_larger_than_limit_is_413(start):
    client, server = start(max_blob_bytes=1000)
    data = blob("z", 1001)
    assert client.put(f"/cas/{sha256(data)}", data)[0] == 413
    pieces = iter([data[:600], data[600:]])
    assert client.put(f"/cas/{sha256(data)}", pieces)[0] == 413
    assert stored_files(server, "cas") == [] and temp_files(server) == []


def test_blob_limit_never_exceeds_store_size(start):
    client, _ = start(max_bytes=500)
    data = blob("z", 501)
    assert client.put(f"/cas/{sha256(data)}", data)[0] == 413


# -- action cache validation --------------------------------------------

def test_action_result_is_served_only_while_its_outputs_exist(start):
    client, server = start()
    out, log_text = blob("o"), blob("l", 50)
    result = action_result(files=[(sha256(out), len(out))], stdout=(sha256(log_text), len(log_text)))
    assert client.put(f"/ac/{AC_KEY}", result)[0] == 200

    assert client.get(f"/ac/{AC_KEY}")[0] == 404  # outputs not uploaded yet
    assert stored_files(server, "ac") == []  # and the dangling entry was dropped
    assert "casd_ac_invalidated_total 1" in client.metrics()

    client.put_cas(out)
    client.put_cas(log_text)
    client.put(f"/ac/{AC_KEY}", result)
    assert client.get(f"/ac/{AC_KEY}")[:2] == (200, result)
    assert client.request("HEAD", f"/ac/{AC_KEY}")[0] == 200


def test_action_result_checks_files_inside_output_directories(start):
    client, _ = start()
    inner = blob("i")
    tree_blob = tree(root_files=[(sha256(inner), len(inner))])
    result = action_result(trees=[(sha256(tree_blob), len(tree_blob))])

    client.put(f"/ac/{AC_KEY}", result)
    assert client.get(f"/ac/{AC_KEY}")[0] == 404  # tree blob missing

    client.put_cas(tree_blob)
    client.put(f"/ac/{AC_KEY}", result)
    assert client.get(f"/ac/{AC_KEY}")[0] == 404  # file inside the tree missing

    client.put_cas(inner)
    client.put(f"/ac/{AC_KEY}", result)
    assert client.get(f"/ac/{AC_KEY}")[0] == 200


def test_empty_outputs_do_not_need_to_be_uploaded(start):
    client, _ = start()
    result = action_result(files=[(EMPTY_SHA256, 0)])
    client.put(f"/ac/{AC_KEY}", result)
    assert client.get(f"/ac/{AC_KEY}")[0] == 200


def test_validation_can_be_turned_off_and_opaque_values_are_served(start):
    client, _ = start(validate_ac=False)
    dangling = action_result(files=[("9" * 64, 5)])
    client.put(f"/ac/{AC_KEY}", dangling)
    assert client.get(f"/ac/{AC_KEY}")[0] == 200

    client, _ = start(directory=None)
    client.put(f"/ac/{AC_KEY}", b"\xff\xff not a protobuf")
    assert client.get(f"/ac/{AC_KEY}")[0] == 200


def test_action_result_can_be_overwritten(start):
    client, _ = start()
    client.put(f"/ac/{AC_KEY}", action_result())
    newer = action_result(files=[(EMPTY_SHA256, 0)])
    client.put(f"/ac/{AC_KEY}", newer)
    assert client.get(f"/ac/{AC_KEY}")[1] == newer


# -- eviction ------------------------------------------------------------

def test_least_recently_used_blob_is_evicted_first(start):
    client, server = start(max_bytes=3000)
    a, b, c = (client.put_cas(blob(t)) for t in "abc")
    assert client.get(f"/cas/{a}")[0] == 200  # a is now more recent than b
    d = client.put_cas(blob("d"))
    assert [client.get(f"/cas/{k}")[0] for k in (a, b, c, d)] == [200, 404, 200, 200]
    assert server.store.stats() == (3000, 3)
    text = client.metrics()
    assert "casd_evictions_total 1" in text and "casd_evicted_bytes_total 1000" in text
    assert "casd_store_bytes 3000" in text


def test_serving_an_action_result_keeps_its_outputs_alive(start):
    client, _ = start(max_bytes=3200)
    out = blob("o")
    result = action_result(files=[(sha256(out), len(out))])
    out_key = client.put_cas(out)
    client.put(f"/ac/{AC_KEY}", result)
    filler = client.put_cas(blob("f"))
    assert client.get(f"/ac/{AC_KEY}")[0] == 200  # refreshes `out`, not `filler`
    client.put_cas(blob("g"))
    client.put_cas(blob("h"))
    assert client.get(f"/cas/{out_key}")[0] == 200
    assert client.get(f"/cas/{filler}")[0] == 404


def test_evicting_an_output_turns_the_action_result_into_a_miss(start):
    client, _ = start(max_bytes=2500)
    out = blob("o")
    client.put_cas(out)
    client.put(f"/ac/{AC_KEY}", action_result(files=[(sha256(out), len(out))]))
    client.put_cas(blob("x"))
    client.put_cas(blob("y"))  # pushes `out` over the limit
    assert client.get(f"/cas/{sha256(out)}")[0] == 404
    assert client.get(f"/ac/{AC_KEY}")[0] == 404


def test_restart_rebuilds_the_index_and_removes_temp_files(start, tmp_path):
    directory = tmp_path / "persist"
    client, server = start(max_bytes=10_000, directory=directory)
    keys = []
    for i, tag in enumerate("abc"):
        keys.append(client.put_cas(blob(tag)))
        # Give each file a distinct, ordered last-used time.
        os.utime(server.store._path("cas", keys[-1]), (1000 + i, 1000 + i))
    (directory / "tmp" / "crashed-upload").write_bytes(b"partial")
    server.shutdown()

    client, server = start(max_bytes=2000, directory=directory)  # smaller limit
    assert temp_files(server) == []
    assert server.store.stats() == (2000, 2)
    assert [client.get(f"/cas/{k}")[0] for k in keys] == [404, 200, 200]  # oldest went


# -- concurrency ---------------------------------------------------------

def test_concurrent_uploads_of_the_same_blob(start):
    client, server = start()
    data = blob("s", 200_000)
    key = sha256(data)
    statuses = []

    def upload():
        statuses.append(client.put(f"/cas/{key}", data)[0])

    threads = [threading.Thread(target=upload) for _ in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert statuses == [200] * 16
    assert stored_files(server, "cas") == [key]
    assert server.store.stats() == (200_000, 1)
    assert temp_files(server) == []
    assert client.get(f"/cas/{key}")[1] == data


def test_store_stays_within_limit_and_never_serves_wrong_bytes_under_load(start):
    limit = 50_000
    client, server = start(max_bytes=limit)
    blobs = [blob(f"{i:03d}", 1000 + 37 * i) for i in range(120)]
    errors = []

    def worker(offset: int):
        try:
            for i in range(offset, len(blobs), 4):
                key = client.put_cas(blobs[i])
                for j in (i, (i * 7) % len(blobs)):
                    status, body, _ = client.get(f"/cas/{sha256(blobs[j])}")
                    if status == 200 and body != blobs[j]:
                        errors.append(f"wrong bytes for blob {j}")
                    elif status not in (200, 404):
                        errors.append(f"status {status}")
                if server.store.stats()[0] > limit + max(len(b) for b in blobs) * 8:
                    errors.append("store far over limit")
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)] * 1
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    total, items = server.store.stats()
    assert total <= limit
    on_disk = sum(p.stat().st_size for p in (server.store.root / "cas").glob("*/*"))
    assert on_disk == total and items == len(stored_files(server, "cas"))


# -- auth ----------------------------------------------------------------

def test_open_by_default(start):
    client, _ = start()
    client.put_cas(b"anyone")


def test_separate_read_and_write_credentials(start):
    anonymous, _ = start(read_credentials="dev:r", write_credentials="ci:w")
    reader, writer = anonymous.as_user("dev:r"), anonymous.as_user("ci:w")
    data = b"secret"
    key = sha256(data)

    assert anonymous.put(f"/cas/{key}", data)[0] == 401
    assert reader.put(f"/cas/{key}", data)[0] == 401
    assert writer.put(f"/cas/{key}", data)[0] == 200

    status, _, headers = anonymous.get(f"/cas/{key}")
    assert status == 401 and headers["WWW-Authenticate"].startswith("Basic")
    assert anonymous.as_user("dev:wrong").get(f"/cas/{key}")[0] == 401
    assert reader.get(f"/cas/{key}")[0] == 200
    assert writer.get(f"/cas/{key}")[0] == 200
    assert anonymous.request("GET", f"/cas/{key}", headers={"Authorization": "Basic !!!"})[0] == 401

    assert anonymous.get("/healthz")[0] == 200
    assert anonymous.get("/metrics")[0] == 200


def test_write_credentials_only_leaves_reads_open(start):
    anonymous, _ = start(write_credentials="ci:w")
    key = anonymous.as_user("ci:w").put_cas(b"public read")
    assert anonymous.get(f"/cas/{key}")[0] == 200
    assert anonymous.put(f"/cas/{sha256(b'x')}", b"x")[0] == 401


def test_read_credentials_only_means_nobody_can_write(start):
    anonymous, _ = start(read_credentials="dev:r")
    assert anonymous.as_user("dev:r").put(f"/cas/{sha256(b'x')}", b"x")[0] == 401


# -- metrics and CLI -----------------------------------------------------

def test_metrics_count_hits_misses_and_bytes(start):
    client, _ = start()
    key = client.put_cas(b"12345")
    client.put_cas(b"12345")
    client.get(f"/cas/{key}")
    client.get(f"/cas/{'0' * 64}")
    text = client.metrics()
    assert 'casd_requests_total{kind="cas",method="GET",result="hit"} 1' in text
    assert 'casd_requests_total{kind="cas",method="GET",result="miss"} 1' in text
    assert 'casd_requests_total{kind="cas",method="PUT",result="stored"} 1' in text
    assert 'casd_requests_total{kind="cas",method="PUT",result="duplicate"} 1' in text
    assert "casd_received_bytes_total 10" in text
    assert "casd_sent_bytes_total 5" in text
    assert 'casd_request_duration_seconds_count{method="PUT"} 2' in text
    assert "casd_store_items 1" in text


def test_scrub_removes_blobs_that_no_longer_match_their_hash(start, tmp_path, capsys):
    directory = tmp_path / "scrub"
    client, server = start(directory=directory)
    good, bad = client.put_cas(b"good"), client.put_cas(b"will be corrupted")
    server.shutdown()
    server.store._path("cas", bad).write_bytes(b"bit rot")

    assert main(["scrub", "--dir", str(directory), "--dry-run"]) == 1
    assert server.store._path("cas", bad).exists()
    assert main(["scrub", "--dir", str(directory)]) == 1
    assert not server.store._path("cas", bad).exists()
    assert server.store._path("cas", good).exists()
    assert main(["scrub", "--dir", str(directory)]) == 0
    assert "checked 1 blobs, 0 corrupt" in capsys.readouterr().out


@pytest.mark.parametrize("text,expected", [("500", 500), ("1K", 1024), ("1.5M", 1572864), ("10G", 10 << 30), ("2GiB", 2 << 30), ("3 gb", 3 << 30)])
def test_parse_size(text, expected):
    assert parse_size(text) == expected
