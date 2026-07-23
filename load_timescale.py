#!/usr/bin/env python3
"""Load the shared `trades` dataset into TimescaleDB via binary-fast CSV COPY.

``COPY ... FROM STDIN`` is Postgres's fastest bulk-load path; the CSV is parsed in
C. Each ``datagen`` batch is rendered to CSV by polars (native, no per-row Python
loop) and streamed straight into the COPY pipe. The session timezone is pinned to
UTC so the microsecond timestamps land as the same instants as the other engines.

Usage:
    python load_timescale.py --dsn 'host=localhost port=5432 user=bench password=bench dbname=bench' --rows 10000000
"""

import argparse
import os
import sys
import time

import polars as pl
import psycopg

import datagen

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DSN = (f"host={os.environ.get('TS_HOST', 'localhost')} "
               f"port={os.environ.get('TS_PORT', '5432')} "
               f"user={os.environ.get('TS_USER', 'bench')} "
               f"password={os.environ.get('TS_PASSWORD', 'bench')} "
               f"dbname={os.environ.get('TS_DBNAME', 'bench')}")


def main(argv):
    ap = argparse.ArgumentParser(description="Load shared trades dataset into TimescaleDB (CSV COPY)")
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--rows", type=int, default=10_000_000)
    ap.add_argument("--batch-rows", type=int, default=1_000_000)
    ap.add_argument("--table", default="trades")
    ap.add_argument("--template", default=datagen.DEFAULT_TEMPLATE)
    ap.add_argument("--recreate", action="store_true", help="DROP and recreate the hypertable first")
    args = ap.parse_args(argv)

    with open(os.path.join(HERE, "schema", "timescale.sql")) as fh:
        ddl = fh.read().replace("trades", args.table)

    print(f"[load]   timescale {args.dsn} table={args.table} rows={args.rows:,}")
    with psycopg.connect(args.dsn, autocommit=True) as conn:
        conn.execute("SET TIME ZONE 'UTC'")
        if args.recreate:
            conn.execute(f"DROP TABLE IF EXISTS {args.table}")
        conn.execute(ddl)

        copy_sql = (f"COPY {args.table} (symbol, side, price, amount, timestamp) "
                    f"FROM STDIN WITH (FORMAT csv)")
        t0 = time.monotonic()
        done = 0
        for batch, _ in datagen.iter_batches(args.rows, args.batch_rows, args.template):
            df = pl.DataFrame({
                "symbol": pl.Series(batch["symbol"]),
                "side": pl.Series(batch["side"]),
                "price": batch["price"],
                "amount": batch["amount"],
                "timestamp": pl.Series(batch["timestamp_us"]).cast(pl.Datetime("us", "UTC")),
            })
            csv_bytes = df.write_csv(include_header=False).encode("utf-8")
            with conn.cursor() as cur:
                with cur.copy(copy_sql) as copy:
                    copy.write(csv_bytes)
            done += df.height
            el = time.monotonic() - t0
            print(f"[load]   {done:,}/{args.rows:,} rows | {done/el:,.0f} rows/s", file=sys.stderr)

        # Verify against the server, not the sender's own tally. This loader COPYs in
        # autocommit batches, so an interrupted run leaves a partial table that still looks
        # healthy - exactly how a 500M load once silently became 458M.
        actual = conn.execute(f"SELECT count(*) FROM {args.table}").fetchone()[0]

    el = time.monotonic() - t0
    print(f"[done]   loaded {done:,} rows in {el:.1f}s ({done/el:,.0f} rows/s)")
    if actual != args.rows:
        print(f"[ERROR]  row count mismatch: expected {args.rows:,}, table has {actual:,} "
              f"({args.rows - actual:+,}). The load did not complete.", file=sys.stderr)
        return 1
    print(f"[verify] row count OK: {actual:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
