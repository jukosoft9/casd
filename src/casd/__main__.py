"""Command line entry point: `casd serve` and `casd scrub`."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import signal
import sys
import threading
from pathlib import Path

from .server import build_server

_SIZE = re.compile(r"^(\d+(?:\.\d+)?)\s*([KMGT]?)(?:i?B)?$", re.IGNORECASE)
_UNITS = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}


def parse_size(text: str) -> int:
    match = _SIZE.match(text.strip())
    if match is None:
        raise argparse.ArgumentTypeError(f"not a size: {text!r} (try 500M or 10G)")
    return int(float(match.group(1)) * _UNITS[match.group(2).upper()])


def serve(args: argparse.Namespace) -> int:
    server = build_server(
        args.dir,
        args.max_size,
        args.host,
        args.port,
        max_blob_bytes=args.max_blob_size,
        validate_ac=not args.no_validate_ac,
        read_credentials=os.environ.get("CASD_READ_CREDENTIALS") or None,
        write_credentials=os.environ.get("CASD_WRITE_CREDENTIALS") or None,
    )
    used, items = server.store.stats()
    logging.info(
        "serving %s on %s:%d (%d entries, %d of %d bytes used)",
        args.dir, args.host, server.server_address[1], items, used, args.max_size,
    )

    def stop(*_: object) -> None:
        # shutdown() blocks until serve_forever() returns, so it cannot run
        # on the main thread, which is the one inside serve_forever().
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    server.serve_forever()
    server.server_close()
    logging.info("stopped")
    return 0


def scrub(args: argparse.Namespace) -> int:
    """Re-hash every CAS blob and delete any whose content no longer matches."""
    checked = corrupt = 0
    for path in sorted(Path(args.dir, "cas").glob("*/*")):
        hasher = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                hasher.update(chunk)
        checked += 1
        if hasher.hexdigest() != path.name:
            corrupt += 1
            print(f"corrupt: {path}")
            if not args.dry_run:
                path.unlink()
    print(f"checked {checked} blobs, {corrupt} corrupt" + (" (dry run)" if args.dry_run else ""))
    return 1 if corrupt else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="casd", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="log every request")
    commands = parser.add_subparsers(dest="command", required=True)

    s = commands.add_parser("serve", help="run the cache server")
    s.add_argument("--dir", required=True, help="directory to store blobs in")
    s.add_argument("--max-size", type=parse_size, default="10G", help="evict above this (default 10G)")
    s.add_argument("--max-blob-size", type=parse_size, default="2G", help="largest single upload (default 2G)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--no-validate-ac", action="store_true",
                   help="serve action results without checking their blobs exist")
    s.set_defaults(run=serve)

    c = commands.add_parser("scrub", help="verify stored blobs against their hashes (run while stopped)")
    c.add_argument("--dir", required=True)
    c.add_argument("--dry-run", action="store_true", help="report corrupt blobs without deleting them")
    c.set_defaults(run=scrub)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
