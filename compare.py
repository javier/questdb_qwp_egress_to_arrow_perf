#!/usr/bin/env python3
"""Run the read benchmark across engines x variants x reader-counts and tabulate.

Invokes each ``read_bench_*.py`` as a subprocess with ``--json``, parses the single
``RESULT {...}`` line it prints, and renders a rows/s (and MB/s) matrix - rows are
engine/variant, columns are reader counts. Writes ``results.json`` and a markdown
table (``results.md``).

    python compare.py --limit 10000000 --readers 1,2,4,8
    python compare.py --limit 5000000 --readers 1,4 --engines clickhouse,timescale

Assumes the three containers are up and loaded (see README). Connection flags use
each script's localhost defaults; run a script directly to point elsewhere.
"""

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def machine_info():
    """(vCPU, RAM_bytes, platform_string) - best effort across Linux and macOS."""
    cpu = os.cpu_count()
    ram = 0
    try:  # Linux
        ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError):
        try:  # macOS
            out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                                 capture_output=True, text=True)
            ram = int(out.stdout.strip())
        except Exception:  # noqa: BLE001
            ram = 0
    return cpu, ram, platform.platform()

# (engine, script, variant, mode) - variant None means the script has no --variant flag.
#
# `mode` is how the N readers are run, and it is NOT cosmetic:
#   thread  - N threads in one interpreter. Correct for clients that decode in native code
#             and release the GIL (QuestDB QWP/Arrow, ClickHouse ArrowStream, ADBC). Measured
#             identical to processes for those (1.00-1.01x) with less overhead.
#   process - N independent processes, one timestamp slice each (via proc_compare.py).
#             REQUIRED for clickhouse/native: clickhouse-driver materialises numpy in Python
#             while holding the GIL, so threads serialise it. Measured 27.1M rows/s on 8
#             threads vs 141.9M on 8 processes - running it threaded understates it 5.2x.
TARGETS = [
    ("questdb", "read_bench_questdb.py", None, "thread"),
    ("clickhouse", "read_bench_clickhouse.py", "arrow", "thread"),
    ("clickhouse", "read_bench_clickhouse.py", "native", "process"),
    ("timescale", "read_bench_timescale.py", "adbc", "thread"),
    ("timescale", "read_bench_timescale.py", "connectorx", "thread"),
]

# Variants that STREAM the result (constant client memory). The others
# (clickhouse/native, timescale/connectorx) materialise the whole result on the
# client, so their peak RSS grows with the row count - at a few hundred million
# rows they need a big-RAM client or they swap. Use
# `--variants "$STREAMING"` to restrict a large campaign to the safe set.
STREAMING_VARIANTS = "qwp-arrow,arrow,adbc"


def variant_key(variant):
    """Filter key for --variants; questdb has no --variant flag, so name it explicitly."""
    return variant or "qwp-arrow"


def run_one(script, variant, limit, readers, run_timeout):
    cmd = [PY, os.path.join(HERE, script), "--limit", str(limit),
           "--readers", str(readers), "--json"]
    if variant:
        cmd += ["--variant", variant]
    try:
        proc = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                              timeout=(run_timeout or None))
    except subprocess.TimeoutExpired:
        # One pathological variant (e.g. a materialise-whole reader that swaps on a
        # large result) must not wedge the whole sweep. Mark it and move on.
        return None, f"TIMEOUT after {run_timeout}s"
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT "):]), None
    err = proc.stderr.strip().splitlines()
    tail = err[-1] if err else f"exit {proc.returncode}, no RESULT line"
    return None, tail


def run_cell(script, variant, limit, readers, run_timeout, warmup, repeats, log, settle=0):
    """Warm the cell `warmup` times (discarded, lets the DB/OS page-cache heat up), pause
    `settle` seconds, then measure `repeats` times and return the MEAN over the measured
    runs. Each run is a fresh client process, but server-side buffer pools and the OS file
    cache persist across them, so the warmups do their job.

    The settle pause matters: warmups leave the server busy (background merges, WAL
    flushing, dirty-page writeback), and without a gap that tail bleeds into the FIRST
    measured run - observed as a low first sample and a 10-19% spread, while the remaining
    samples agreed to <1%. Warmups fill the cache; settle lets the system go quiet.

    Fails fast: if any warmup or measured run times out / errors, the cell is marked and
    the rest is skipped."""
    for w in range(warmup):
        res, err = run_one(script, variant, limit, readers, run_timeout)
        log(f"warm {w + 1}/{warmup}: "
            + (f"{res['rows_per_s']:,.0f} rows/s" if res else f"FAILED {err}"))
        if res is None:
            return None, err
    if settle > 0:
        log(f"settle {settle}s before measuring")
        time.sleep(settle)
    samples = []
    for m in range(repeats):
        res, err = run_one(script, variant, limit, readers, run_timeout)
        log(f"meas {m + 1}/{repeats}: "
            + (f"{res['rows_per_s']:,.0f} rows/s | {res['mb_per_s']:,.1f} MB/s" if res
               else f"FAILED {err}"))
        if res is None:
            return None, err
        samples.append(res)
    if not samples:
        return None, "no measured runs"
    rps = [s["rows_per_s"] for s in samples]
    mbps = [s["mb_per_s"] for s in samples]
    base = samples[0]
    return {
        "engine": base["engine"], "variant": variant, "readers": readers,
        "rows": base["rows"], "limit": limit, "status": "ok",
        "rows_per_s": statistics.mean(rps),          # <- reported value (mean of measured)
        "mb_per_s": statistics.mean(mbps),
        "elapsed_s": statistics.mean(s["elapsed_s"] for s in samples),
        "rows_per_s_min": min(rps), "rows_per_s_max": max(rps),
        "rows_per_s_samples": rps, "warmup": warmup, "repeats": len(samples),
        "settle_s": settle,
    }, None


def run_process_variant(engine, variant, args, reader_counts):
    """Delegate a whole variant to proc_compare.py, which sweeps the reader counts using
    independent processes. Returns its result dicts (already schema-compatible)."""
    cmd = [PY, os.path.join(HERE, "proc_compare.py"), "--engine", engine,
           "--limit", str(args.limit), "--readers", ",".join(str(r) for r in reader_counts),
           "--warmup", str(args.warmup), "--settle", str(args.settle),
           "--repeats", str(args.repeats), "--run-timeout", str(args.run_timeout),
           "--warmup-scope", "run"]
    if variant:
        cmd += ["--variant", variant]
    proc = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True)
    sys.stderr.write(proc.stderr)
    for line in proc.stdout.splitlines():
        if line.startswith("RESULTS "):
            return json.loads(line[len("RESULTS "):])
    tail = (proc.stderr.strip().splitlines() or ["no RESULTS line"])[-1]
    print(f"[error] process-mode run failed for {engine}/{variant}: {tail}", file=sys.stderr)
    return [{"engine": engine, "variant": variant, "readers": r, "status": "ERR",
             "rows_per_s": None, "mb_per_s": None} for r in reader_counts]


def label_of(res):
    """questdb -> 'questdb'; clickhouse+arrow -> 'clickhouse/arrow'."""
    return res["engine"] if not res.get("variant") else f'{res["engine"]}/{res["variant"]}'


def fmt(n):
    return f"{n:,.0f}" if n else "-"


def fmt_gbytes(mb_per_s):
    """MB/s -> GB/s. Reported in GIGABYTES/s because '6,974 MB/s' reads badly next to the
    gigaBIT figures; multiply by 8 for Gb/s (so 7.0 GB/s == 55.8 Gb/s)."""
    return f"{mb_per_s / 1000:,.2f}" if mb_per_s else "-"


def build_cells(results):
    """From a flat results list, return (rps_cells, gbps_cells, ordered_labels).
    Throughput cells are GB/s. Failed cells render their status marker (TIMEOUT/ERR)."""
    rps, gbs, labels = {}, {}, []
    for res in results:
        lab = label_of(res)
        if lab not in labels:
            labels.append(lab)
        key = (lab, res["readers"])
        if res.get("status", "ok") == "ok":
            rps[key], gbs[key] = fmt(res["rows_per_s"]), fmt_gbytes(res["mb_per_s"])
        else:
            rps[key] = gbs[key] = res.get("status", "ERR")
    return rps, gbs, labels


def table(rows_hdr, cols, cells, unit):
    head = f"| {'engine/variant':<22} | " + " | ".join(f"{c:>12}" for c in cols) + " |"
    sep = "|" + "-" * 24 + "|" + "|".join("-" * 14 for _ in cols) + "|"
    lines = [f"**{unit}**", "", head, sep]
    for label in rows_hdr:
        cs = " | ".join(f"{cells.get((label, c), ''):>12}" for c in cols)
        lines.append(f"| {label:<22} | {cs} |")
    return "\n".join(lines)


def main(argv):
    ap = argparse.ArgumentParser(description="Compare read throughput across engines")
    ap.add_argument("--limit", type=int, default=10_000_000)
    ap.add_argument("--readers", default="1,2,4,8",
                    help="comma-separated reader counts to sweep; default 1,2,4,8")
    ap.add_argument("--engines", default="questdb,clickhouse,timescale",
                    help="which engines to run; default all three")
    ap.add_argument("--variants", default=None,
                    help="comma-separated variants to run (qwp-arrow, arrow, native, adbc, "
                         f"connectorx); default all. Use '{STREAMING_VARIANTS}' to keep only "
                         "the streaming paths, which hold constant client memory - the "
                         "buffering ones need a big-RAM client at high row counts.")
    ap.add_argument("--out", default=os.path.join(HERE, "results", "latest"),
                    help="output path prefix (writes .json and .md); default ./results/latest")
    ap.add_argument("--run-timeout", type=int, default=180,
                    help="per-run wall-clock cap in seconds; a run that exceeds it is "
                         "marked TIMEOUT and the sweep continues (0 = no cap). Guards "
                         "against materialise-whole variants swapping on large results.")
    ap.add_argument("--warmup", type=int, default=2,
                    help="unmeasured warmup runs per cell (heats the DB/OS page cache); default 2")
    ap.add_argument("--repeats", type=int, default=3,
                    help="measured runs per cell; the reported value is their mean; default 3")
    ap.add_argument("--settle", type=int, default=10,
                    help="seconds to pause between the warmups and the measured runs, so "
                         "warmup-induced background work (merges, WAL flush, writeback) "
                         "does not bleed into the first measured run; default 10")
    ap.add_argument("--append", default=None,
                    help="append this run's tables (with row count + machine info) as a "
                         "new dated section to this markdown file, e.g. RESULTS.md")
    args = ap.parse_args(argv)

    reader_counts = [int(x) for x in args.readers.split(",") if x.strip()]
    engines = {e.strip() for e in args.engines.split(",") if e.strip()}
    targets = [t for t in TARGETS if t[0] in engines]
    if args.variants:
        keep = {v.strip() for v in args.variants.split(",") if v.strip()}
        unknown = keep - {variant_key(t[2]) for t in TARGETS}
        if unknown:
            print(f"[error] unknown variant(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        targets = [t for t in targets if variant_key(t[2]) in keep]
    if not targets:
        print("[error] no targets selected (check --engines / --variants)", file=sys.stderr)
        return 2

    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    results = []

    def flush():
        """Persist after every cell. compare.py used to write only at the very end, so an
        interrupted run threw away every completed cell - which cost us a whole engine's
        results more than once."""
        with open(args.out + ".json", "w") as fh:
            json.dump(results, fh, indent=2)

    for engine, script, variant, mode in targets:
        label = engine if variant is None else f"{engine}/{variant}"

        if mode == "process":
            print(f"[cell] {label:<22} readers={args.readers} limit={args.limit:,} "
                  f"-> PROCESS mode (this client holds the GIL; threads would understate it)",
                  file=sys.stderr)
            results.extend(run_process_variant(engine, variant, args, reader_counts))
            flush()
            continue

        for r in reader_counts:
            print(f"[cell] {label:<22} readers={r} limit={args.limit:,} "
                  f"(warmup={args.warmup}, measure={args.repeats}) ...", file=sys.stderr)
            res, err = run_cell(script, variant, args.limit, r, args.run_timeout,
                                args.warmup, args.repeats,
                                lambda m: print(f"       {m}", file=sys.stderr),
                                settle=args.settle)
            if res is None:
                print(f"       FAILED: {err}", file=sys.stderr)
                status = "TIMEOUT" if err and err.startswith("TIMEOUT") else "ERR"
                results.append({"engine": engine, "variant": variant, "readers": r,
                                "status": status, "rows_per_s": None, "mb_per_s": None})
                flush()
                continue
            res["mode"] = "thread"
            results.append(res)
            flush()
            spread = (res["rows_per_s_max"] - res["rows_per_s_min"]) / res["rows_per_s"] * 100 \
                if res["rows_per_s"] else 0
            print(f"       => mean {res['rows_per_s']:,.0f} rows/s | "
                  f"{res['mb_per_s'] / 1000:,.2f} GB/s ({res['gb_per_s']:.1f} Gb/s) "
                  f"(spread {spread:.1f}% over {res['repeats']} runs)", file=sys.stderr)

    method = (f"mean of {args.repeats} measured run(s) after {args.warmup} warmup(s) "
              f"+ {args.settle}s settle, per cell")
    rps_cells, gbs_cells, labels = build_cells(results)
    rps_tbl = table(labels, reader_counts, rps_cells, "rows/s by reader count")
    mbps_tbl = table(labels, reader_counts, gbs_cells,
                     "GB/s decoded payload (x8 for Gb/s); compare within-engine only")
    md = (f"# Egress read benchmark\n\nlimit = {args.limit:,} rows per run — {method}\n\n"
          f"{rps_tbl}\n\n{mbps_tbl}\n")
    print("\n" + rps_tbl + "\n\n" + mbps_tbl + "\n")

    flush()   # final state (incremental flushes already ran after every cell)
    with open(args.out + ".md", "w") as fh:
        fh.write(md)
    saved = f"{args.out}.json  {args.out}.md"

    if args.append:
        cpu, ram, plat = machine_info()
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        section = (f"## {stamp} — {args.limit:,} rows\n\n"
                   f"- host: {plat}\n"
                   f"- vCPU: {cpu} | RAM: {ram / 1e9:.1f} GB\n"
                   f"- readers swept: {args.readers}\n\n"
                   f"{rps_tbl}\n\n{mbps_tbl}\n\n---\n\n")
        new = not os.path.exists(args.append)
        with open(args.append, "a") as fh:
            if new:
                fh.write("# Egress read benchmark — run log\n\n"
                         "`rows/s` is the cross-engine metric; `MB/s` (decoded payload) "
                         "compares only within one engine.\n\n---\n\n")
            fh.write(section)
        saved += f"  {args.append}"

    print(f"[saved] {saved}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
