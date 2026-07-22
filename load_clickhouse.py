#!/usr/bin/env python3
"""Load the shared `trades` dataset into ClickHouse via Arrow inserts.

``clickhouse-connect``'s ``insert_arrow`` streams whole Arrow columns over HTTP -
the fast columnar ingest path, the mirror image of the Arrow egress the read
benchmark measures. Rows come from ``datagen`` so they match the QuestDB and
Timescale loads byte for byte.

Usage:
    python load_clickhouse.py --host localhost --port 8123 --rows 10000000
"""

import argparse
import os
import sys
import time

import clickhouse_connect
import pyarrow as pa

import datagen

HERE = os.path.dirname(os.path.abspath(__file__))


def batch_to_arrow(batch):
    return pa.table({
        "symbol": pa.array(batch["symbol"]),
        "side": pa.array(batch["side"]),
        "price": pa.array(batch["price"]),
        "amount": pa.array(batch["amount"]),
        "timestamp": pa.array(batch["timestamp_us"]).cast(pa.timestamp("us", "UTC")),
    })


def main(argv):
    ap = argparse.ArgumentParser(description="Load shared trades dataset into ClickHouse (Arrow insert)")
    ap.add_argument("--host", default=os.environ.get("CH_HOST", "localhost"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("CH_HTTP_PORT", "8123")), help="HTTP port")
    ap.add_argument("--user", default="default")
    ap.add_argument("--password", default=os.environ.get("CH_PASSWORD", "bench"))
    ap.add_argument("--database", default="default")
    ap.add_argument("--rows", type=int, default=10_000_000)
    ap.add_argument("--batch-rows", type=int, default=1_000_000)
    ap.add_argument("--table", default="trades")
    ap.add_argument("--template", default=datagen.DEFAULT_TEMPLATE)
    ap.add_argument("--recreate", action="store_true", help="DROP and recreate the table first")
    args = ap.parse_args(argv)

    with open(os.path.join(HERE, "schema", "clickhouse.sql")) as fh:
        ddl = fh.read().replace("trades", args.table)

    client = clickhouse_connect.get_client(
        host=args.host, port=args.port, username=args.user,
        password=args.password, database=args.database)
    print(f"[load]   clickhouse {args.host}:{args.port} table={args.table} rows={args.rows:,}")
    if args.recreate:
        client.command(f"DROP TABLE IF EXISTS {args.table}")
    client.command(ddl)

    t0 = time.monotonic()
    done = 0
    for batch, _ in datagen.iter_batches(args.rows, args.batch_rows, args.template):
        client.insert_arrow(args.table, batch_to_arrow(batch))
        done += len(batch["price"])
        el = time.monotonic() - t0
        print(f"[load]   {done:,}/{args.rows:,} rows | {done/el:,.0f} rows/s", file=sys.stderr)

    el = time.monotonic() - t0
    print(f"[done]   loaded {done:,} rows in {el:.1f}s ({done/el:,.0f} rows/s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
