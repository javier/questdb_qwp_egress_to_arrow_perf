#!/usr/bin/env python3
"""Time-to-first-batch probe: what does a caller actually wait for before it can start work?

The streaming paths (QuestDB QWP/Arrow, ClickHouse ArrowStream) hand back the first Arrow
batch while the rest is still in flight. The buffered path (clickhouse-driver's
execute(columnar=True)) cannot return anything until the ENTIRE result is materialised, so
its time-to-first-row is by construction its total time.

Prints one JSON line: first-batch seconds, total seconds, rows, decoded bytes.

    python ttfb_probe.py --engine questdb
    python ttfb_probe.py --engine clickhouse --variant arrow|native
"""

import argparse
import json
import os
import sys
import time

COLS = "symbol, side, price, amount, timestamp"


def probe_questdb(table):
    import questdb
    addr = os.environ.get("QDB_ADDR", "localhost:9000")
    sql = f"select {COLS} from {table}"
    with questdb.connect(f"ws::addr={addr};") as db:
        t0 = time.monotonic()
        result = db.query(sql)
        first = None
        rows = nbytes = 0
        for batch in result.iter_arrow():
            if first is None:
                first = time.monotonic() - t0
            rows += batch.num_rows
            nbytes += batch.nbytes
        return first, time.monotonic() - t0, rows, nbytes


def probe_clickhouse_arrow(table):
    import clickhouse_connect
    client = clickhouse_connect.get_client(
        host=os.environ.get("CH_HOST", "localhost"),
        port=int(os.environ.get("CH_HTTP_PORT", "8123")),
        username="default", password=os.environ.get("CH_PASSWORD", "bench"))
    sql = f"SELECT {COLS} FROM {table}"
    t0 = time.monotonic()
    first = None
    rows = nbytes = 0
    with client.query_arrow_stream(sql) as reader:
        for chunk in reader:
            if first is None:
                first = time.monotonic() - t0
            rows += chunk.num_rows
            nbytes += chunk.nbytes
    return first, time.monotonic() - t0, rows, nbytes


def probe_clickhouse_native(table):
    from clickhouse_driver import Client
    client = Client(host=os.environ.get("CH_HOST", "localhost"),
                    port=int(os.environ.get("CH_NATIVE_PORT", "9001")),
                    user="default", password=os.environ.get("CH_PASSWORD", "bench"),
                    settings={"use_numpy": True})
    sql = f"SELECT {COLS} FROM {table}"
    t0 = time.monotonic()
    cols = client.execute(sql, columnar=True)   # nothing is available until this returns
    first = time.monotonic() - t0               # ... so first row == whole result
    rows = len(cols[0]) if cols else 0
    nbytes = sum(int(getattr(c, "nbytes", 0)) for c in cols)
    return first, time.monotonic() - t0, rows, nbytes


def probe_timescale_adbc(table):
    import adbc_driver_postgresql.dbapi as pg
    uri = (f"postgresql://{os.environ.get('TS_USER', 'bench')}:"
           f"{os.environ.get('TS_PASSWORD', 'bench')}@"
           f"{os.environ.get('TS_HOST', 'localhost')}:"
           f"{os.environ.get('TS_PORT', '5432')}/{os.environ.get('TS_DBNAME', 'bench')}")
    sql = f"SELECT {COLS} FROM {table}"
    with pg.connect(uri) as conn:
        with conn.cursor() as cur:
            t0 = time.monotonic()
            cur.execute(sql)
            reader = cur.fetch_record_batch()
            first = None
            rows = nbytes = 0
            for batch in reader:
                if first is None:
                    first = time.monotonic() - t0
                rows += batch.num_rows
                nbytes += batch.nbytes
            return first, time.monotonic() - t0, rows, nbytes


def main(argv):
    ap = argparse.ArgumentParser(description="time-to-first-batch probe")
    ap.add_argument("--engine", required=True,
                    choices=["questdb", "clickhouse", "timescale"])
    ap.add_argument("--variant", default="arrow", choices=["arrow", "native"])
    ap.add_argument("--table", default="trades")
    args = ap.parse_args(argv)

    if args.engine == "questdb":
        label, fn = "questdb/qwp-arrow", probe_questdb
    elif args.engine == "timescale":
        label, fn = "timescale/adbc", probe_timescale_adbc
    elif args.variant == "arrow":
        label, fn = "clickhouse/arrow", probe_clickhouse_arrow
    else:
        label, fn = "clickhouse/native", probe_clickhouse_native

    streaming = args.engine in ("questdb", "timescale") or args.variant == "arrow"
    first, total, rows, nbytes = fn(args.table)
    print("TTFB " + json.dumps({
        "path": label, "streaming": streaming,
        "first_batch_s": first, "total_s": total, "rows": rows,
        "decoded_bytes": nbytes,
        "first_batch_pct_of_total": (first / total * 100) if total else None,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
