#!/usr/bin/env python3
"""Merge per-engine result JSONs into one combined section appended to RESULTS.md.

Used by bench.sh's isolated mode: each engine is measured alone (the other DB
containers stopped), so compare.py runs once per engine and writes
``results/run_<rows>_<engine>.json``. This stitches those back into a single
apples-to-apples comparison table, tagged with row count + machine specs, so the
run log still reads as one run even though the engines were measured separately.
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

from compare import build_cells, machine_info, table

# Canonical row order for the combined table.
CANON = ["questdb", "clickhouse/arrow", "clickhouse/native",
         "timescale/adbc", "timescale/connectorx"]


def main(argv):
    ap = argparse.ArgumentParser(description="Merge per-engine result JSONs into RESULTS.md")
    ap.add_argument("--inputs", nargs="+", required=True, help="result JSON files (globs ok)")
    ap.add_argument("--rows", type=int, required=True)
    ap.add_argument("--append", required=True, help="markdown file to append the section to")
    ap.add_argument("--warmup", type=int, default=None)
    ap.add_argument("--repeats", type=int, default=None)
    ap.add_argument("--isolated", action="store_true")
    args = ap.parse_args(argv)

    paths = []
    for pat in args.inputs:
        paths.extend(sorted(glob.glob(pat)))
    results = []
    for p in paths:
        with open(p) as fh:
            results.extend(json.load(fh))
    if not results:
        print("[merge] no results found in inputs", file=sys.stderr)
        return 1

    reader_counts = sorted({res["readers"] for res in results})
    rps_cells, mbps_cells, labels = build_cells(results)
    ordered = [x for x in CANON if x in labels] + [x for x in labels if x not in CANON]

    rps_tbl = table(ordered, reader_counts, rps_cells, "rows/s by reader count")
    mbps_tbl = table(ordered, reader_counts, mbps_cells,
                     "MB/s (decoded payload; compare within-engine only)")

    cpu, ram, plat = machine_info()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    bits = []
    if args.repeats is not None:
        bits.append(f"mean of {args.repeats} measured after {args.warmup} warmup(s)/cell")
    if args.isolated:
        bits.append("per-engine isolation (only the tested engine's container running)")
    method = "; ".join(bits)

    section = (f"## {stamp} — {args.rows:,} rows\n\n"
               f"- host: {plat}\n"
               f"- vCPU: {cpu} | RAM: {ram / 1e9:.1f} GB\n"
               + (f"- method: {method}\n" if method else "")
               + f"- readers swept: {','.join(str(r) for r in reader_counts)}\n\n"
               f"{rps_tbl}\n\n{mbps_tbl}\n\n---\n\n")

    new = not os.path.exists(args.append)
    with open(args.append, "a") as fh:
        if new:
            fh.write("# Egress read benchmark — run log\n\n"
                     "`rows/s` is the cross-engine metric; `MB/s` (decoded payload) "
                     "compares only within one engine.\n\n---\n\n")
        fh.write(section)
    print(f"[merge] appended combined section for {args.rows:,} rows to {args.append}",
          file=sys.stderr)
    print("\n" + rps_tbl + "\n\n" + mbps_tbl + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
