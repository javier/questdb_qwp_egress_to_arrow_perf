#!/usr/bin/env python3
"""QuestDB egress read benchmark - the QWP Arrow-streaming path.

Derived from the original ``read_bench.py``: consume the QWP query cursor's Arrow
batches directly (``db.query(sql).iter_arrow()``) and tally rows + decoded bytes.
No pandas/polars materialisation - the native client already decodes the wire into
Arrow, so the Python loop only adds counters. ``--readers N`` splits the last-N-row
timestamp range into N slices, one WebSocket connection each.

    python read_bench_questdb.py --addr localhost:9000 --limit 10000000 --readers 4
"""

import os
import sys
import time

import questdb

import benchlib

VARIANT = "qwp-arrow"


def build_conf(args):
    scheme = "wss" if (args.tls or args.token or args.username) else "ws"
    parts = [f"{scheme}::addr={args.addr};"]
    if args.token:
        parts.append(f"token={args.token};")
    elif args.username and args.password:
        parts.append(f"username={args.username};password={args.password};")
    if scheme == "wss" and args.tls_verify == "unsafe_off":
        parts.append("tls_verify=unsafe_off;")
    return "".join(parts)


def main(argv):
    ap = benchlib.common_args("QuestDB QWP Arrow egress read benchmark")
    ap.add_argument("--addr", default=os.environ.get("QDB_ADDR", "localhost:9000"))
    ap.add_argument("--token", default=None)
    ap.add_argument("--username", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--tls", action="store_true")
    ap.add_argument("--tls-verify", choices=["on", "unsafe_off"], default="on")
    args = ap.parse_args(argv)

    conf = build_conf(args)
    ts, table, limit, readers = args.timestamp_col, args.table, args.limit, args.readers

    with questdb.connect(conf) as db:
        mm = db.query(f"select min({ts}) lo, max({ts}) hi "
                      f"from (select {ts} from {table} limit -{limit})").to_polars()
    if mm.height == 0 or mm["lo"][0] is None:
        print(f"[error] '{table}' returned no rows", file=sys.stderr)
        return 1
    lo = mm["lo"].dt.epoch("us")[0]
    hi = mm["hi"].dt.epoch("us")[0]

    slices = list(benchlib.slice_bounds(lo, hi, readers))
    counts, byts, errors = [0] * readers, [0] * readers, []

    def reader_fn(idx):
        _, a, b, is_last = slices[idx]
        op = "<=" if is_last else "<"
        sql = (f"select symbol, side, price, amount, {ts} from {table} "
               f"where {ts} >= '{benchlib.us_to_iso(a)}' and {ts} {op} '{benchlib.us_to_iso(b)}'")
        with questdb.connect(conf) as db:
            n = b_ = 0
            for batch in db.query(sql).iter_arrow():
                n += batch.num_rows
                b_ += batch.nbytes
                counts[idx], byts[idx] = n, b_
            counts[idx], byts[idx] = n, b_

    print(f"[scan]   questdb/{VARIANT}: last {limit:,} rows of '{table}', {readers} reader(s)",
          file=sys.stderr)
    rep = benchlib.Reporter(counts, byts, args.report_interval)
    rep.start()
    t0 = time.monotonic()
    benchlib.run_readers(reader_fn, readers, counts, byts, errors)
    elapsed = time.monotonic() - t0
    rep.finish()

    if errors:
        for e in errors:
            print(f"[error] {e}", file=sys.stderr)
        return 1
    benchlib.emit_result("questdb", VARIANT, args, readers,
                         sum(counts), sum(byts), elapsed, counts)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
