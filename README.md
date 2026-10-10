# casd

A remote build cache for Bazel, written from scratch in about 700 lines of
dependency-free Python, with the failure cases tested and the speed-up measured.

I built it to understand build caching from the inside: what a cache has to
guarantee, where it can corrupt a build, and what happens when it runs out of
space.

## Results

A C++ workspace (my own library, CLI and test on top of Abseil and GoogleTest,
435 actions) built with Bazel 8.8.1. Every scenario starts from `bazel clean`,
so nothing is reused locally. Reproduce with `python3 bench/run.py`.

| Scenario | Wall time | vs no cache | Bazel exit | Remote cache hits | Action results dropped by casd |
|---|---:|---:|---:|---:|---:|
| 1. No cache | 88.7 s | 1.00x | 0 | 0 | - |
| 2. Empty cache (build and upload) | 85.3 s | 0.96x | 0 | 0 | 0 |
| 3. Warm cache | 6.4 s | 0.07x | 0 | 379 of 379 | 0 |
| 4. Warm cache, one source file edited | 9.3 s | 0.10x | 0 | 378 of 379 | 0 |
| 5. Cache restarted at 40% of the size it needs | 69.9 s | 0.79x | 0 | 77 | 1 |
| 6. Same as 5, validation off | 77.6 s | 0.87x | 0 | 77 | 0 |
| 7. 30% of stored blobs deleted behind the server's back | 46.7 s | 0.53x | 0 | 230 | 149 |
| 8. Same as 7, validation off | 14.8 s | - | **1 (build failed)** | 97 | 0 |

Measured once on a 4-core / 8-thread laptop CPU under WSL2, with the cache on
the same machine, so there is no network latency in these numbers. Full output
is in [bench/results](bench/results).

What the numbers say:

- **A warm cache makes a clean build 14 times faster** (88.7 s to 6.4 s). The
  remaining 6.4 s is Bazel's own analysis plus 379 small requests.
- **Uploading costs nothing measurable here.** Scenario 2 was slightly faster
  than scenario 1, which is run-to-run noise; an earlier run had it 10% slower.
- **Validation is the difference between a slower build and a broken one.**
  In scenario 8 the cache served action results whose outputs were gone, and
  Bazel failed with `Missing digest: b8df166a...` on a file it had been told
  was cached. In scenario 7 casd dropped those 149 results instead, Bazel
  rebuilt them, and the build passed.
- **Ordinary LRU eviction rarely leaves dangling results.** Outputs and the
  result that points at them are written and read together, so they age out
  together: after trimming the store to 40%, only one result was dangling, and
  Bazel recovered from that one on its own (scenario 6).

## How it works

Bazel's HTTP cache protocol is two namespaces and three verbs:

```
GET | HEAD | PUT   /ac/<sha256 of the action>     a serialised ActionResult
GET | HEAD | PUT   /cas/<sha256 of the content>   the blob itself
```

An action result does not contain outputs. It lists the digests of blobs in the
CAS. Almost every design decision follows from that.

| Decision | Why |
|---|---|
| Hash CAS uploads while they stream in; reject on mismatch | A corrupt upload must never be stored under a good key. The check costs no extra read. |
| Write to `tmp/`, then `os.replace` into place | Readers never see a partial file, and a crash mid-upload leaves nothing behind. |
| Serve an action result only if every blob it references is present | Otherwise the client gets a hit it cannot use (scenario 8). |
| Decode the ActionResult protobuf by hand ([reapi.py](src/casd/reapi.py)) | Only four message types are needed. It avoids a protobuf dependency and generated code. |
| Serving an action result refreshes its blobs in the LRU | Keeps outputs from being evicted before the result that points at them. |
| Never evict the newest entry | An upload that returned 200 is always readable afterwards. |
| A duplicate CAS upload keeps the existing file | Same key means same bytes, and readers may have the file open. |
| Recency is persisted as file mtime | The LRU order survives a restart without a separate index file. |

Other features: Basic auth with separate read and write credentials, Prometheus
metrics at `/metrics`, `/healthz`, graceful shutdown on SIGTERM, and
`casd scrub` to re-hash every stored blob and delete any that no longer match.

## Failure cases covered by tests

`pytest` runs 49 tests in about 18 seconds, on Linux and Windows.

| Failure | Expected behaviour |
|---|---|
| Upload whose content does not match its key | 400, nothing stored |
| Client disconnects mid-upload | Nothing stored, temp file removed |
| Upload larger than the limit (fixed length or chunked) | 413, nothing stored |
| Action result whose outputs are missing or were evicted | 404, entry dropped |
| Action result with a missing file inside an output directory | 404 |
| 16 clients upload the same blob at once | All get 200, one copy stored |
| Mixed concurrent load on a store that is too small | Never serves wrong bytes; size accounting matches the disk |
| Restart with a smaller size limit and a leftover temp file | Oldest entries evicted, temp file removed |
| Bit rot on disk | `casd scrub` finds and removes the blob |
| Truncated or malformed protobuf | `ValueError`, never a crash or a hang |

## Run it

```
pip install -e ".[dev]"
pytest
casd serve --dir /var/cache/casd --max-size 20G
```

```
bazel build //... --remote_cache=http://127.0.0.1:8080
```

With credentials (set `CASD_WRITE_CREDENTIALS=ci:secret` on the server):

```
bazel build //... --remote_cache=http://127.0.0.1:8080 \
    --remote_header="Authorization=Basic $(printf 'ci:secret' | base64)"
```

## Deployment

- [Dockerfile](Dockerfile): non-root image.
- [.github/workflows/ci.yml](.github/workflows/ci.yml): unit tests on Linux and
  Windows, a job that builds the Bazel workspace twice through casd and fails
  unless the second build is fully cached, Terraform validation, and an image
  pushed to GHCR with a build provenance attestation.
- [infra/](infra): Terraform for Azure Container Apps with the blob directory
  on an Azure Files share, generated credentials stored as Container Apps
  secrets, optional source IP allow-list, and the image pinned by digest.

Status: the Terraform passes `terraform validate` against azurerm 4.x but has
not been applied to a subscription, and the image has not been built outside CI.

## Limits

- **One replica.** The eviction index lives in memory, so two servers sharing a
  directory would each enforce the limit separately. Scaling out needs the
  index in shared storage, or a consistent-hash front end.
- **HTTP protocol only.** No gRPC, so no remote execution, no `FindMissingBlobs`
  batching, and no compressed blobs. Bazel makes one request per blob.
- **Thread per connection, in Python.** Fine for a team; not built for
  thousands of concurrent clients.
- **Action results are not authenticated.** Anyone with write credentials can
  upload a false result for an action. That is true of every cache that speaks
  this protocol, which is why CI should be the only writer.
- **The benchmark is small and local.** 435 actions and 37 MB of outputs, with
  no network between Bazel and the cache.
