#!/usr/bin/env python3
"""Process-based reader comparison — the GIL-free counterpart to compare.py.

compare.py runs N readers as THREADS in one interpreter. That is fine for client paths
which decode in native code and release the GIL (QuestDB QWP/Arrow, ClickHouse ArrowStream,
ADBC), but it silently serialises paths that decode in Python while holding it - notably
clickhouse-driver's numpy path, which measured 26M rows/s on 8 threads while using only
~140-230% of 3200% available CPU, and ~6x that once freed from the GIL.

Here each of the N timestamp slices is read by an INDEPENDENT PROCESS (via each reader's
--slice i/N flag), so nothing is shared. Same warmup / settle / repeats discipline as
compare.py so the numbers are comparable.

Reported rate is sum(rows) / max(per-process elapsed). Each process times only its own scan
(not interpreter startup), and they are spawned together, so the slowest process's window
is the closest analogue to the threaded run's wall clock. The looser wall-clock figure
(which includes process spawn) is printed alongside for transparency.

    python proc_compare.py --engine questdb --readers 1,2,4,8 --limit 500000000
    python proc_compare.py --engine clickhouse --variant native --readers 8
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

SCRIPTS = {
    "questdb": "read_bench_questdb.py",
    "clickhouse": "read_bench_clickhouse.py",
    "timescale": "read_bench_timescale.py",
}


def resolve_bounds(engine, variant, limit):
    """Compute the row-range bounds ONCE. Without this every worker process repeats the
    same full-range min/max scan concurrently, and those scans compete with the measured
    reads - which collapsed an 8-process QuestDB cell to below its 4-process rate."""
    script = os.path.join(HERE, SCRIPTS[engine])
    cmd = [PY, script, "--limit", str(limit), "--emit-bounds"]
    if variant:
        cmd += ["--variant", variant]
    out = subprocess.run(cmd, capture_output=True, text=True)
    for line in out.stdout.splitlines():
        if line.startswith("BOUNDS "):
            _, lo, hi = line.split()
            return f"{lo},{hi}"
    raise SystemExit(f"could not resolve bounds for {engine}: {out.stderr.strip()[-300:]}")


def one_round(engine, variant, limit, n, run_timeout, bounds):
    """Spawn n single-slice reader processes concurrently.
    Returns (rows, decoded_bytes, max_elapsed, wall)."""
    script = os.path.join(HERE, SCRIPTS[engine])
    procs = []
    t0 = time.monotonic()
    for i in range(n):
        cmd = [PY, script, "--limit", str(limit), "--slice", f"{i}/{n}",
               "--bounds", bounds, "--json"]
        if variant:
            cmd += ["--variant", variant]
        procs.append(subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True))
    rows, nbytes, elapsed = 0, 0, []
    for p in procs:
        try:
            out, _ = p.communicate(timeout=run_timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            return None
        for line in out.splitlines():
            if line.startswith("RESULT "):
                r = json.loads(line[len("RESULT "):])
                rows += r["rows"]
                nbytes += r.get("bytes", 0)
                elapsed.append(r["elapsed_s"])
    wall = time.monotonic() - t0
    if len(elapsed) != n:
        return None
    return rows, nbytes, max(elapsed), wall


def main(argv):
    ap = argparse.ArgumentParser(description="Process-based (GIL-free) reader comparison")
    ap.add_argument("--engine", required=True, choices=sorted(SCRIPTS))
    ap.add_argument("--variant", default=None)
    ap.add_argument("--limit", type=int, default=500_000_000)
    ap.add_argument("--readers", default="1,2,4,8")
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--settle", type=int, default=10)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--warmup-scope", choices=["cell", "run"], default="cell",
                    help="'cell' warms before every reader count (thorough but mostly "
                         "redundant); 'run' warms ONCE up front and then relies on the "
                         "settle pause before each cell. Warming fills the server's page "
                         "cache, which depends on the dataset, not the reader count - so "
                         "'run' measures the same thing for ~40%% less work.")
    ap.add_argument("--run-timeout", type=int, default=3600)
    ap.add_argument("--out", default=None, help="write results JSON here")
    args = ap.parse_args(argv)

    label = args.engine if not args.variant else f"{args.engine}/{args.variant}"
    bounds = resolve_bounds(args.engine, args.variant, args.limit)
    print(f"[bounds] resolved once for all processes: {bounds}", file=sys.stderr)
    reader_counts = [int(x) for x in args.readers.split(",") if x.strip()]

    # Warming fills the SERVER's page cache, which depends on the dataset - not on how many
    # client connections read it. So one warmup covers every reader count (and every variant
    # of the same engine). Do it with the largest count, which scans the data fastest.
    if args.warmup_scope == "run" and args.warmup:
        wn = max(reader_counts)
        print(f"[warm] one-time warmup x{args.warmup} using {wn} process(es)", file=sys.stderr)
        for w in range(args.warmup):
            r = one_round(args.engine, args.variant, args.limit, wn, args.run_timeout, bounds)
            if r is None:
                print("       warmup FAILED", file=sys.stderr)
                break
            print(f"       warm {w + 1}/{args.warmup}: {r[0] / r[2]:,.0f} rows/s", file=sys.stderr)

    results = []
    for n in reader_counts:
        print(f"[cell] {label:22} procs={n} limit={args.limit:,} "
              f"(warmup={args.warmup if args.warmup_scope == 'cell' else 0}, "
              f"settle={args.settle}s, measure={args.repeats})", file=sys.stderr)
        ok = True
        if args.warmup_scope == "cell":
            for w in range(args.warmup):
                r = one_round(args.engine, args.variant, args.limit, n, args.run_timeout, bounds)
                if r is None:
                    ok = False
                    break
                print(f"       warm {w + 1}/{args.warmup}: {r[0] / r[1]:,.0f} rows/s",
                      file=sys.stderr)
        if not ok:
            print("       FAILED during warmup", file=sys.stderr)
            results.append({"engine": args.engine, "variant": args.variant, "readers": n,
                            "status": "ERR", "rows_per_s": None, "mb_per_s": None})
            continue
        if args.settle:
            print(f"       settle {args.settle}s before measuring", file=sys.stderr)
            time.sleep(args.settle)

        samples, walls, mbs = [], [], []
        for m in range(args.repeats):
            r = one_round(args.engine, args.variant, args.limit, n, args.run_timeout, bounds)
            if r is None:
                ok = False
                break
            rows, nbytes, max_elapsed, wall = r
            rate = rows / max_elapsed
            samples.append(rate)
            walls.append(rows / wall)
            mbs.append(nbytes / max_elapsed / 1e6)
            print(f"       meas {m + 1}/{args.repeats}: {rate:,.0f} rows/s "
                  f"(wall-based {rows / wall:,.0f})", file=sys.stderr)
        if not ok or not samples:
            results.append({"engine": args.engine, "variant": args.variant, "readers": n,
                            "status": "ERR", "rows_per_s": None, "mb_per_s": None})
            continue
        mean = statistics.mean(samples)
        spread = (max(samples) - min(samples)) / mean * 100
        print(f"       => mean {mean:,.0f} rows/s (spread {spread:.1f}%, "
              f"wall-based {statistics.mean(walls):,.0f})", file=sys.stderr)
        mb_mean = statistics.mean(mbs) if mbs else 0.0
        results.append({
            "engine": args.engine, "variant": args.variant, "readers": n,
            "status": "ok", "mode": "process", "rows": args.limit, "limit": args.limit,
            "rows_per_s": mean, "rows_per_s_samples": samples,
            "rows_per_s_wall": statistics.mean(walls),
            "mb_per_s": mb_mean, "gb_per_s": mb_mean * 8 / 1000,
            "elapsed_s": args.limit / mean if mean else None,
            "warmup": args.warmup, "settle_s": args.settle, "repeats": len(samples),
        })

    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w") as fh:
            json.dump(results, fh, indent=2)
        print(f"[saved] {args.out}", file=sys.stderr)
    print("RESULTS " + json.dumps(results))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
