#!/usr/bin/env python3
"""Load the shared `trades` dataset into QuestDB over the columnar QWP path.

Same fast path as ``csv_columnar_sender.py`` (``db.dataframe`` ships whole Arrow
columns to the native client), but driven by ``datagen`` so the rows - and their
uniform timestamps - are identical to what the ClickHouse and Timescale loaders
write. Pre-creates the table from ``schema/questdb.sql`` so ``timestamp`` is
microsecond TIMESTAMP (matching the other two engines) rather than the
nanosecond TIMESTAMP_NS a fresh columnar insert would auto-create.

Usage:
    python load_questdb.py --addr localhost:9000 --rows 10000000
"""

import argparse
import os
import sys
import time

import polars as pl
import questdb

import datagen

HERE = os.path.dirname(os.path.abspath(__file__))


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
    ap = argparse.ArgumentParser(description="Load shared trades dataset into QuestDB (columnar QWP)")
    ap.add_argument("--addr", default=os.environ.get("QDB_ADDR", "localhost:9000"))
    ap.add_argument("--rows", type=int, default=10_000_000)
    ap.add_argument("--batch-rows", type=int, default=1_000_000)
    ap.add_argument("--table", default="trades")
    ap.add_argument("--template", default=datagen.DEFAULT_TEMPLATE)
    ap.add_argument("--recreate", action="store_true", help="DROP and recreate the table first")
    ap.add_argument("--token", default=None)
    ap.add_argument("--username", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--tls", action="store_true")
    ap.add_argument("--tls-verify", choices=["on", "unsafe_off"], default="on")
    args = ap.parse_args(argv)

    with open(os.path.join(HERE, "schema", "questdb.sql")) as fh:
        ddl = fh.read().replace("trades", args.table)

    conf = build_conf(args)
    print(f"[load]   questdb {args.addr} table={args.table} rows={args.rows:,}")
    with questdb.connect(conf) as db:
        if args.recreate:
            db.execute(f"DROP TABLE IF EXISTS {args.table}")
        db.execute(ddl)

        t0 = time.monotonic()
        done = 0
        for batch, _ in datagen.iter_batches(args.rows, args.batch_rows, args.template):
            df = pl.DataFrame({
                "symbol": pl.Series(batch["symbol"]).cast(pl.Categorical),
                "side": pl.Series(batch["side"]).cast(pl.Categorical),
                "price": batch["price"],
                "amount": batch["amount"],
                "timestamp": pl.Series(batch["timestamp_us"]).cast(pl.Datetime("us", "UTC")),
            })
            db.dataframe(df, table_name=args.table, symbols=["symbol", "side"], at="timestamp")
            done += df.height
            el = time.monotonic() - t0
            print(f"[load]   {done:,}/{args.rows:,} rows | {done/el:,.0f} rows/s", file=sys.stderr)

    el = time.monotonic() - t0
    print(f"[done]   loaded {done:,} rows in {el:.1f}s ({done/el:,.0f} rows/s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
