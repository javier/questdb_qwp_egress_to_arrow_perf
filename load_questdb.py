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
import re
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
    ap.add_argument("--parquet", action="store_true",
                    help="create the table with FORMAT PARQUET so partitions are stored as "
                         "compressed Parquet instead of QuestDB's native uncompressed columns")
    ap.add_argument("--token", default=None)
    ap.add_argument("--username", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--tls", action="store_true")
    ap.add_argument("--tls-verify", choices=["on", "unsafe_off"], default="on")
    args = ap.parse_args(argv)

    with open(os.path.join(HERE, "schema", "questdb.sql")) as fh:
        ddl = fh.read().replace("trades", args.table)
    if args.parquet:
        # Same schema, only the storage format changes, so the Parquet and native runs stay
        # comparable row for row.
        ddl, n = re.subn(r"PARTITION BY DAY WAL", "PARTITION BY DAY FORMAT PARQUET WAL", ddl)
        if n != 1:
            print(f"[ERROR]  could not inject FORMAT PARQUET into the DDL:\n{ddl}", file=sys.stderr)
            return 1

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

    # Verify what actually landed. Never trust the sender's own count: a load killed midway
    # still reports the rows it sent, and every later measurement silently runs on a short
    # table. Reconnect first so the sender has flushed, then poll - QuestDB's WAL applies
    # asynchronously, so the count can lag a moment behind ingestion.
    actual = -1
    with questdb.connect(conf) as db:
        for _ in range(60):
            actual = int(db.query(f"select count() c from {args.table}").to_polars()["c"][0])
            if actual >= args.rows:
                break
            time.sleep(1)
    if actual != args.rows:
        print(f"[ERROR]  row count mismatch: expected {args.rows:,}, table has {actual:,} "
              f"({args.rows - actual:+,}). The load did not complete.", file=sys.stderr)
        return 1
    print(f"[verify] row count OK: {actual:,}")

    # Partition storage. Worth printing for every run, not just Parquet ones: it is the
    # only place the native/Parquet footprint comparison actually comes from.
    with questdb.connect(conf) as db:
        def partitions():
            return db.query(
                "select count() n, sum(diskSize) disk, sum(numRows) rows_, "
                f"sum(case when isParquet then 0 else 1 end) pending "
                f"from table_partitions('{args.table}')").to_polars()

        if args.parquet:
            # FORMAT PARQUET writes Parquet partitions directly out of the WAL, so there is
            # nothing to wait for: by the time the row count is right, the partitions are
            # already Parquet. This is only a guard that the DDL actually took effect.
            pending = int(partitions()["pending"][0] or 0)
            if pending:
                print(f"[ERROR]  {pending} partition(s) are not Parquet - FORMAT PARQUET "
                      f"did not take effect.", file=sys.stderr)
                return 1
            print("[verify] all partitions are Parquet")

        p = partitions()
        nparts, disk, prows = int(p["n"][0]), int(p["disk"][0] or 0), int(p["rows_"][0] or 0)
        fmt = "parquet" if args.parquet else "native"
        print(f"[size]   format={fmt} partitions={nparts} rows={prows:,} "
              f"disk={disk:,} bytes ({disk / 1e9:.2f} GB, {disk / max(prows, 1):.2f} B/row)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
