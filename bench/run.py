#!/usr/bin/env python3
"""Benchmark casd with real Bazel builds and write the results as JSON and Markdown.

Run on Linux (or WSL) with bazel/bazelisk and a C++ compiler on PATH:

    python3 bench/run.py

Scenarios, each starting from `bazel clean` so nothing is reused locally:

  1. no cache          every action runs locally
  2. empty cache       every action runs locally and is uploaded
  3. warm cache        nothing changed since the upload
  4. one file changed  a source file was edited since the upload
  5. after eviction    the cache was restarted at 40% of the size it needs
  6. the same trimmed cache with action-result validation switched off

  7. after blob loss    30% of the stored blobs were deleted behind the server's back
  8. the same damaged cache with action-result validation switched off

5/6 and 7/8 each start from byte-identical copies of the store, so the only
difference within a pair is whether the server checks that an action result's
outputs still exist before serving it.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROCESSES = re.compile(r"INFO: (\d+) process(?:es)?: (.*)\.")
TRACKED = {
    "ac_hit": 'casd_requests_total{kind="ac",method="GET",result="hit"}',
    "ac_miss": 'casd_requests_total{kind="ac",method="GET",result="miss"}',
    "ac_invalidated": "casd_ac_invalidated_total",
    "cas_stored": 'casd_requests_total{kind="cas",method="PUT",result="stored"}',
    "received_bytes": "casd_received_bytes_total",
    "sent_bytes": "casd_sent_bytes_total",
    "evictions": "casd_evictions_total",
    "store_bytes": "casd_store_bytes",
    "store_items": "casd_store_items",
}


class Cache:
    """A casd server running as a child process."""

    def __init__(self, directory: Path, max_size: int, *extra: str) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.process = subprocess.Popen(
            [sys.executable, "-m", "casd", "serve", "--dir", str(directory),
             "--max-size", str(max_size), "--port", str(self.port), *extra],
            env={**os.environ, "PYTHONPATH": str(REPO / "src")},
        )
        for _ in range(100):
            try:
                urllib.request.urlopen(self.url + "/healthz", timeout=1).close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("casd did not start")

    def metrics(self) -> dict[str, float]:
        text = urllib.request.urlopen(self.url + "/metrics", timeout=5).read().decode()
        values = dict(line.rsplit(" ", 1) for line in text.splitlines() if not line.startswith("#"))
        return {name: float(values.get(series, 0)) for name, series in TRACKED.items()}

    def stop(self) -> None:
        self.process.send_signal(signal.SIGTERM)
        self.process.wait(30)


def bazel(workspace: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bazel", *args], cwd=workspace, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


def scenario(name: str, workspace: Path, cache: Cache | None, results: list[dict]) -> dict:
    bazel(workspace, "clean")
    flags = [f"--remote_cache={cache.url}"] if cache else []
    before = cache.metrics() if cache else {}
    started = time.perf_counter()
    build = bazel(workspace, "build", "//...", "--color=no", "--curses=no", *flags)
    seconds = time.perf_counter() - started
    after = cache.metrics() if cache else {}

    match = PROCESSES.search(build.stdout)
    breakdown = match.group(2) if match else ""
    cache_hits = re.search(r"(\d+) remote cache hit", breakdown)
    row = {
        "scenario": name,
        "seconds": round(seconds, 1),
        "exit_code": build.returncode,
        "actions": int(match.group(1)) if match else None,
        "remote_cache_hits": int(cache_hits.group(1)) if cache_hits else 0,
        "breakdown": breakdown,
        # Lines that show Bazel reacting to blobs the cache no longer has.
        "warnings": sorted({
            line.strip()[:300] for line in build.stdout.splitlines()
            if re.search(r"evict|lost input|no longer available|cache miss|missing digest|rewind", line, re.I)
        })[:8],
    }
    for key in after:
        gauge = key.startswith("store_")
        row[key] = int(after[key] if gauge else after[key] - before[key])
    results.append(row)
    print(f"{name:<28} {seconds:7.1f}s  exit {build.returncode}  {breakdown}", flush=True)
    if build.returncode != 0:
        print("\n".join(build.stdout.splitlines()[-15:]), flush=True)
    return row


def markdown(results: list[dict], environment: dict) -> str:
    baseline = results[0]["seconds"]
    lines = [
        "| Scenario | Wall time | vs no cache | Bazel exit | Actions | Remote cache hits | AC hits / misses | Results dropped | Uploaded | Downloaded |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        cached = "ac_hit" in r
        lines.append(
            f"| {r['scenario']} | {r['seconds']} s | {r['seconds'] / baseline:.2f}x | {r['exit_code']} "
            f"| {r['actions']} | {r['remote_cache_hits']} | "
            + (f"{r['ac_hit']} / {r['ac_miss']} | {r['ac_invalidated']} "
               f"| {r['received_bytes'] / 1e6:.1f} MB | {r['sent_bytes'] / 1e6:.1f} MB |" if cached else "- | - | - | - |")
        )
    lines += ["", "Environment: " + ", ".join(f"{k} {v}" for k, v in environment.items())]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work", default=str(Path.home() / "casd-bench"), help="scratch directory")
    parser.add_argument("--out", default=str(REPO / "bench" / "results"))
    parser.add_argument("--smoke", action="store_true",
                        help="only scenarios 2 and 3, and fail unless the warm build is served from the cache")
    args = parser.parse_args()

    work, out = Path(args.work), Path(args.out)
    workspace, store = work / "ws", work / "store"
    for stale in (store, work / "store-novalidate"):
        shutil.rmtree(stale, ignore_errors=True)
    # Build from a copy so the edit in scenario 4 never touches the repository.
    shutil.copytree(REPO / "bench" / "workspace", workspace, dirs_exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)

    version = bazel(workspace, "--version").stdout.strip()
    bazel(workspace, "build", "//...", "--nobuild")  # fetch dependencies, untimed
    results: list[dict] = []

    if args.smoke:
        cache = Cache(store, 10 << 30)
        upload = scenario("2 empty cache (upload)", workspace, cache, results)
        warm = scenario("3 warm cache", workspace, cache, results)
        cache.stop()
        ok = (upload["exit_code"] == warm["exit_code"] == 0
              and upload["cas_stored"] > 0 and warm["remote_cache_hits"] > 0 and warm["ac_miss"] == 0)
        print("smoke test " + ("passed" if ok else "FAILED"))
        return 0 if ok else 1

    scenario("1 no cache", workspace, None, results)

    cache = Cache(store, 10 << 30)
    scenario("2 empty cache (upload)", workspace, cache, results)
    scenario("3 warm cache", workspace, cache, results)
    with open(workspace / "records.cc", "a") as source:
        source.write(f"\n// benchmark edit {time.time()}\n")
    scenario("4 one file changed", workspace, cache, results)
    full_size = int(cache.metrics()["store_bytes"])
    cache.stop()

    # Restarting with a smaller limit evicts the least recently used entries.
    trimmed = int(full_size * 0.4)
    Cache(store, trimmed).stop()
    shutil.copytree(store, work / "store-novalidate")

    cache = Cache(store, 10 << 30)
    scenario("5 after eviction", workspace, cache, results)
    cache.stop()
    cache = Cache(work / "store-novalidate", 10 << 30, "--no-validate-ac")
    scenario("6 after eviction, unchecked", workspace, cache, results)
    cache.stop()

    # Scenario 5 refilled `store`. Now lose blobs without telling anyone, which
    # is what a cache that evicts blobs independently of action results does.
    lost = [p for p in sorted((store / "cas").glob("*/*")) if int(p.name[:8], 16) % 10 < 3]
    for path in lost:
        path.unlink()
    shutil.rmtree(work / "store-novalidate")
    shutil.copytree(store, work / "store-novalidate")
    cache = Cache(store, 10 << 30)
    scenario("7 after blob loss", workspace, cache, results)
    cache.stop()
    cache = Cache(work / "store-novalidate", 10 << 30, "--no-validate-ac")
    scenario("8 after blob loss, unchecked", workspace, cache, results)
    cache.stop()

    environment = {
        "bazel": version.replace("bazel ", ""),
        "python": platform.python_version(),
        "cpus": os.cpu_count(),
        "kernel": platform.release(),
        "full store": f"{full_size / 1e6:.1f} MB",
        "trimmed store": f"{trimmed / 1e6:.1f} MB",
        "blobs deleted": len(lost),
    }
    (out / "results.json").write_text(json.dumps({"environment": environment, "results": results}, indent=2) + "\n")
    table = markdown(results, environment)
    (out / "results.md").write_text(table)
    print("\n" + table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
