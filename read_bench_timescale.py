#!/usr/bin/env python3
"""TimescaleDB egress read benchmark - two "fastest path" variants, side by side.

  --variant adbc (default): the Arrow Database Connectivity PostgreSQL driver. Each
        reader opens its own connection, runs its timestamp slice, and streams the
        result as Arrow record batches (``cursor.fetch_record_batch()``) - ADBC uses
        the Postgres binary COPY protocol underneath and hands back Arrow directly.
        This is the "Arrow over Postgres" path; same manual timestamp split as the
        QuestDB and ClickHouse scripts.

  --variant connectorx: the Rust ``connectorx`` reader, which partitions the query
        itself. One ``read_sql(..., partition_on='timestamp', partition_num=readers)``
        call fans out into ``readers`` parallel connections internally and returns one
        Arrow table. Here ``--readers`` maps to connectorx partitions rather than
        threads we manage. It materialises the whole result (no incremental ticker).

Note: Postgres TEXT has no dictionary encoding, so the symbol/side columns arrive as
full string arrays - the Arrow byte tally here is legitimately larger per row than the
dictionary-encoded QuestDB/ClickHouse columns. Compare rows/s across engines; compare
MB/s only within Timescale.

    python read_bench_timescale.py --variant adbc       --limit 10000000 --readers 4
    python read_bench_timescale.py --variant connectorx --limit 10000000 --readers 4
"""

import os
import sys
import time

import benchlib

COLS = "symbol, side, price, amount, timestamp"


def build_dsn(args):
    return (f"host={args.host} port={args.port} user={args.user} "
            f"password={args.password} dbname={args.dbname}")


def build_uri(args):
    return f"postgresql://{args.user}:{args.password}@{args.host}:{args.port}/{args.dbname}"


def pg_ts(us):
    """SQL timestamptz literal (explicit +00:00 offset, session-tz independent)."""
    return "'" + benchlib.us_to_iso(us).replace("Z", "+00:00") + "'"


def predicate(args, a, b, is_last):
    op = "<=" if is_last else "<"
    ts = args.timestamp_col
    return f"{ts} >= {pg_ts(a)} AND {ts} {op} {pg_ts(b)}"


def get_bounds(args):
    """(lo_us, hi_us) of the last --limit rows. Returns None if empty."""
    import psycopg
    sql = (f"SELECT (extract(epoch from min({args.timestamp_col}))*1000000)::bigint, "
           f"       (extract(epoch from max({args.timestamp_col}))*1000000)::bigint "
           f"FROM (SELECT {args.timestamp_col} FROM {args.table} "
           f"      ORDER BY {args.timestamp_col} DESC LIMIT {args.limit}) t")
    with psycopg.connect(build_dsn(args)) as conn:
        lo, hi = conn.execute(sql).fetchone()
    if lo is None:
        return None
    return int(lo), int(hi)


def run_adbc(args, lo, hi):
    import adbc_driver_postgresql.dbapi as pg
    readers = args.readers
    slices = list(benchlib.slice_bounds(lo, hi, readers))
    counts, byts, errors = [0] * readers, [0] * readers, []
    uri = build_uri(args)

    def reader_fn(idx):
        _, a, b, is_last = slices[idx]
        sql = f"SELECT {COLS} FROM {args.table} WHERE {predicate(args, a, b, is_last)}"
        with pg.connect(uri) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                reader = cur.fetch_record_batch()
                n = bb = 0
                for batch in reader:
                    n += batch.num_rows
                    bb += batch.nbytes
                    counts[idx], byts[idx] = n, bb
                counts[idx], byts[idx] = n, bb

    rep = benchlib.Reporter(counts, byts, args.report_interval)
    rep.start()
    t0 = time.monotonic()
    benchlib.run_readers(reader_fn, readers, counts, byts, errors)
    elapsed = time.monotonic() - t0
    rep.finish()
    return counts, byts, elapsed, errors


def run_connectorx(args, lo, hi):
    import connectorx as cx
    readers = args.readers
    ts = args.timestamp_col
    where = f"{ts} >= {pg_ts(lo)} AND {ts} <= {pg_ts(hi)}"
    uri = build_uri(args)
    counts, byts, errors = [0], [0], []
    rep = benchlib.Reporter(counts, byts, args.report_interval)
    rep.start()
    t0 = time.monotonic()
    try:
        if readers <= 1:
            sql = f"SELECT {COLS} FROM {args.table} WHERE {where}"
            tbl = cx.read_sql(uri, sql, return_type="arrow")
        else:
            # connectorx only partitions on int/float, so expose the timestamp as an
            # epoch-microsecond bigint (`c_part`) for it to split on; explicit range
            # avoids a min/max probe. Drop the helper column before byte accounting.
            sql = (f"SELECT {COLS}, (extract(epoch from {ts})*1000000)::bigint AS c_part "
                   f"FROM {args.table} WHERE {where}")
            tbl = cx.read_sql(uri, sql, return_type="arrow",
                              partition_on="c_part", partition_num=readers,
                              partition_range=(lo, hi + 1))
            tbl = tbl.drop_columns(["c_part"])
        counts[0], byts[0] = tbl.num_rows, tbl.nbytes
    except Exception as e:  # noqa: BLE001
        errors.append(str(e))
    elapsed = time.monotonic() - t0
    rep.finish()
    return counts, byts, elapsed, errors


def main(argv):
    ap = benchlib.common_args("TimescaleDB egress read benchmark")
    ap.add_argument("--variant", choices=["adbc", "connectorx"], default="adbc")
    ap.add_argument("--host", default=os.environ.get("TS_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("TS_PORT", "5432")))
    ap.add_argument("--user", default=os.environ.get("TS_USER", "bench"))
    ap.add_argument("--password", default=os.environ.get("TS_PASSWORD", "bench"))
    ap.add_argument("--dbname", default=os.environ.get("TS_DBNAME", "bench"))
    args = ap.parse_args(argv)

    bounds = get_bounds(args)
    if bounds is None:
        print(f"[error] '{args.table}' returned no rows", file=sys.stderr)
        return 1
    lo, hi = bounds

    print(f"[scan]   timescale/{args.variant}: last {args.limit:,} rows of "
          f"'{args.table}', {args.readers} reader(s)", file=sys.stderr)
    if args.variant == "adbc":
        counts, byts, elapsed, errors = run_adbc(args, lo, hi)
    else:
        counts, byts, elapsed, errors = run_connectorx(args, lo, hi)

    if errors:
        for e in errors:
            print(f"[error] {e}", file=sys.stderr)
        return 1
    benchlib.emit_result("timescale", args.variant, args, args.readers,
                         sum(counts), sum(byts), elapsed, counts)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
