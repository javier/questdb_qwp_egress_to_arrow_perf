#!/usr/bin/env python3
"""Shared read-benchmark plumbing for the QuestDB / ClickHouse / TimescaleDB scripts.

Every ``read_bench_*.py`` follows the same shape as the original QuestDB
``read_bench.py``:

  1. find the ``[lo, hi]`` designated-timestamp range of the last ``--limit`` rows,
  2. split that range into ``--readers`` equal slices, one per connection,
  3. stream each slice as Arrow (or the engine's fastest columnar unit), tallying
     ``rows`` and decoded ``nbytes``,
  4. report a live rows/s + MB/s ticker and a final aggregate.

This module holds the parts that are identical across engines: timestamp<->ISO
conversion, the slice-edge math, the live reporter thread, and the summary /
``--json`` result emitter. Each engine script supplies only its own "given a
timestamp slice, stream it and count" reader.

**Metric note (same caveat as read_bench.py).** Throughput is the *decoded* Arrow
payload (``batch.nbytes``), not on-wire bytes. ``rows/s`` is the primary
cross-engine comparator; ``MB/s`` is secondary because encoding differs per engine
(QuestDB SYMBOL and ClickHouse LowCardinality arrive dictionary-encoded, so their
string columns weigh far less than TimescaleDB's plain TEXT arrays over ADBC).
Compare MB/s only within one engine/variant, not across engines.
"""

import argparse
import json
import sys
import threading
import time
from datetime import datetime, timezone


def us_to_iso(us):
    """Epoch microseconds -> ISO-8601 string with microsecond precision (UTC)."""
    sec, frac = divmod(int(us), 1_000_000)
    dt = datetime.fromtimestamp(sec, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{frac:06d}Z"


def split_edges(lo, hi, readers):
    """Return ``readers+1`` epoch-us edges splitting ``[lo, hi]`` into equal slices."""
    span = hi - lo
    edges = [lo + (span * i) // readers for i in range(readers + 1)]
    edges[-1] = hi
    return edges


def slice_bounds(lo, hi, readers):
    """Yield ``(idx, a_us, b_us, is_last)`` per reader. The last slice includes ``hi``
    (use ``<=``); earlier slices are half-open (use ``<``) so no row is double-counted."""
    edges = split_edges(lo, hi, readers)
    for i in range(readers):
        yield i, edges[i], edges[i + 1], (i == readers - 1)


def common_args(description, default_limit=10_000_000):
    """Argparse parser with the flags every read_bench_* script shares."""
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--table", default="trades", help="source table name")
    ap.add_argument("--limit", type=int, default=default_limit,
                    help=f"number of most-recent rows to read; default {default_limit:,}")
    ap.add_argument("--readers", type=int, default=1,
                    help="parallel reader connections (split by timestamp); default 1")
    ap.add_argument("--timestamp-col", default="timestamp",
                    help="designated/order timestamp column used to split readers")
    ap.add_argument("--report-interval", type=float, default=0.5,
                    help="seconds between progress lines; default 0.5")
    ap.add_argument("--json", action="store_true",
                    help="emit one machine-readable JSON result line on stdout (for compare.py)")
    return ap


class Reporter(threading.Thread):
    """Background live ticker: prints rows/s and MB/s deltas to stderr until stopped."""

    def __init__(self, counts, byts, interval):
        super().__init__(daemon=True)
        self.counts, self.byts, self.interval = counts, byts, interval
        self.stop = threading.Event()

    def run(self):
        last_r, last_b = 0, 0
        while not self.stop.wait(self.interval):
            r, b = sum(self.counts), sum(self.byts)
            rps = (r - last_r) / self.interval
            mbps = (b - last_b) / self.interval / 1e6
            print(f"[scan]   {r:>14,} rows | {rps:>13,.0f} rows/s | {mbps:>9,.1f} MB/s",
                  file=sys.stderr)
            last_r, last_b = r, b

    def finish(self):
        self.stop.set()


def run_readers(reader_fn, readers, counts, byts, errors):
    """Spawn one thread per reader slice; ``reader_fn(idx)`` does the streaming and
    updates ``counts[idx]`` / ``byts[idx]`` as it goes."""
    threads = []
    for i in range(readers):
        def target(idx=i):
            try:
                reader_fn(idx)
            except Exception as e:  # noqa: BLE001
                errors.append(f"reader {idx}: {e}")
        t = threading.Thread(target=target)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()


def emit_result(engine, variant, args, readers, total, tbytes, elapsed, counts):
    """Print the final human summary (stderr) and, with --json, a result line (stdout)."""
    rate = total / elapsed if elapsed > 0 else float("inf")
    mbps = tbytes / elapsed / 1e6 if elapsed > 0 else float("inf")
    gbps = tbytes * 8 / elapsed / 1e9 if elapsed > 0 else float("inf")
    gib = tbytes / (1024 ** 3)
    print(f"[done]   {engine}/{variant}: {total:,} rows, {gib:.2f} GiB decoded in "
          f"{elapsed:.3f}s across {readers} reader(s)", file=sys.stderr)
    print(f"[done]   {rate:,.0f} rows/s | {mbps / 1000:,.2f} GB/s ({gbps:.1f} Gb/s) "
          f"(decoded columnar payload, not wire bytes)", file=sys.stderr)
    if readers > 1:
        per = "  ".join(f"r{i}={c:,}" for i, c in enumerate(counts))
        print(f"[done]   per-reader rows: {per}", file=sys.stderr)
    result = {
        "engine": engine, "variant": variant, "readers": readers,
        "rows": total, "bytes": tbytes, "elapsed_s": elapsed,
        "rows_per_s": rate, "mb_per_s": mbps, "gb_per_s": gbps,
        "limit": args.limit, "table": args.table,
    }
    if args.json:
        print("RESULT " + json.dumps(result))
    else:
        print(f"[done]   {rate:,.0f} rows/s | {mbps / 1000:,.2f} GB/s")
    return result
