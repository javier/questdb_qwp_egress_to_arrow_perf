#!/usr/bin/env python3
"""ClickHouse egress read benchmark - two "fastest path" variants, side by side.

  --variant arrow  (default): clickhouse-connect over HTTP (:8123), FORMAT Arrow via
        ``query_arrow_stream`` -> a streaming pyarrow reader. Direct analogue of
        QuestDB's ``iter_arrow`` and TimescaleDB's ADBC path; clean nbytes accounting.

  --variant native: clickhouse-driver over the native TCP protocol (:9001), columnar
        numpy blocks (``execute(columnar=True)``, use_numpy). Lowest-level access, but
        it buffers each reader's slice and materialises numpy columns rather than
        streaming Arrow, so its byte tally is approximate (object arrays for the
        LowCardinality string columns count 8 B/elem, not the decoded string bytes).

Both split the last-N-row timestamp range into ``--readers`` slices, one connection
each - the same split as the other engines.

    python read_bench_clickhouse.py --variant arrow  --limit 10000000 --readers 4
    python read_bench_clickhouse.py --variant native --limit 10000000 --readers 4
"""

import os
import sys
import time

import benchlib

COLS = "symbol, side, price, amount, timestamp"


def get_bounds(args):
    """(lo_us, hi_us) of the last --limit rows, via HTTP. Returns None if empty."""
    import clickhouse_connect
    client = clickhouse_connect.get_client(
        host=args.host, port=args.http_port, username=args.user,
        password=args.password, database=args.database)
    sql = (f"SELECT toUnixTimestamp64Micro(min({args.timestamp_col})), "
           f"toUnixTimestamp64Micro(max({args.timestamp_col})) "
           f"FROM (SELECT {args.timestamp_col} FROM {args.table} "
           f"ORDER BY {args.timestamp_col} DESC LIMIT {args.limit})")
    row = client.query(sql).result_rows[0]
    client.close()
    if row[0] is None:
        return None
    return int(row[0]), int(row[1])


def predicate(args, a, b, is_last):
    ts = args.timestamp_col
    op = "<=" if is_last else "<"
    return (f"{ts} >= fromUnixTimestamp64Micro(toInt64({a}), 'UTC') "
            f"AND {ts} {op} fromUnixTimestamp64Micro(toInt64({b}), 'UTC')")


def make_arrow_reader(args, slices, counts, byts):
    import clickhouse_connect

    def reader_fn(idx):
        _, a, b, is_last = slices[idx]
        sql = f"SELECT {COLS} FROM {args.table} WHERE {predicate(args, a, b, is_last)}"
        client = clickhouse_connect.get_client(
            host=args.host, port=args.http_port, username=args.user,
            password=args.password, database=args.database)
        try:
            n = bb = 0
            with client.query_arrow_stream(sql) as reader:
                for chunk in reader:            # pyarrow Table/RecordBatch per block
                    n += chunk.num_rows
                    bb += chunk.nbytes
                    counts[idx], byts[idx] = n, bb
            counts[idx], byts[idx] = n, bb
        finally:
            client.close()

    return reader_fn


def make_native_reader(args, slices, counts, byts):
    from clickhouse_driver import Client

    def reader_fn(idx):
        _, a, b, is_last = slices[idx]
        sql = f"SELECT {COLS} FROM {args.table} WHERE {predicate(args, a, b, is_last)}"
        client = Client(host=args.host, port=args.native_port, user=args.user,
                        password=args.password, database=args.database,
                        settings={"use_numpy": True})
        try:
            cols = client.execute(sql, columnar=True)   # list of numpy column arrays
            n = len(cols[0]) if cols else 0
            bb = sum(int(getattr(c, "nbytes", 0)) for c in cols)
            counts[idx], byts[idx] = n, bb
        finally:
            client.disconnect()

    return reader_fn


def main(argv):
    ap = benchlib.common_args("ClickHouse egress read benchmark")
    ap.add_argument("--variant", choices=["arrow", "native"], default="arrow")
    ap.add_argument("--host", default=os.environ.get("CH_HOST", "localhost"))
    ap.add_argument("--http-port", type=int, default=int(os.environ.get("CH_HTTP_PORT", "8123")))
    ap.add_argument("--native-port", type=int, default=int(os.environ.get("CH_NATIVE_PORT", "9001")))
    ap.add_argument("--user", default="default")
    ap.add_argument("--password", default=os.environ.get("CH_PASSWORD", "bench"))
    ap.add_argument("--database", default="default")
    args = ap.parse_args(argv)

    bounds = get_bounds(args)
    if bounds is None:
        print(f"[error] '{args.table}' returned no rows", file=sys.stderr)
        return 1
    lo, hi = bounds
    readers = args.readers
    slices = list(benchlib.slice_bounds(lo, hi, readers))
    counts, byts, errors = [0] * readers, [0] * readers, []

    if args.variant == "arrow":
        reader_fn = make_arrow_reader(args, slices, counts, byts)
    else:
        reader_fn = make_native_reader(args, slices, counts, byts)

    print(f"[scan]   clickhouse/{args.variant}: last {args.limit:,} rows of "
          f"'{args.table}', {readers} reader(s)", file=sys.stderr)
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
    benchlib.emit_result("clickhouse", args.variant, args, readers,
                         sum(counts), sum(byts), elapsed, counts)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
