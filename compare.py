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

# (engine, script, variant) - variant None means the script has no --variant flag.
TARGETS = [
    ("questdb", "read_bench_questdb.py", None),
    ("clickhouse", "read_bench_clickhouse.py", "arrow"),
    ("clickhouse", "read_bench_clickhouse.py", "native"),
    ("timescale", "read_bench_timescale.py", "adbc"),
    ("timescale", "read_bench_timescale.py", "connectorx"),
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


def run_cell(script, variant, limit, readers, run_timeout, warmup, repeats, log):
    """Warm the cell `warmup` times (discarded, lets the DB/OS page-cache heat up),
    then measure `repeats` times and return the MEAN over the measured runs. Each run
    is a fresh client process, but server-side buffer pools and the OS file cache
    persist across them, so the warmups do their job. Fails fast: if any warmup or
    measured run times out / errors, the cell is marked and the rest is skipped."""
    for w in range(warmup):
        res, err = run_one(script, variant, limit, readers, run_timeout)
        log(f"warm {w + 1}/{warmup}: "
            + (f"{res['rows_per_s']:,.0f} rows/s" if res else f"FAILED {err}"))
        if res is None:
            return None, err
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
    }, None


def label_of(res):
    """questdb -> 'questdb'; clickhouse+arrow -> 'clickhouse/arrow'."""
    return res["engine"] if not res.get("variant") else f'{res["engine"]}/{res["variant"]}'


def fmt(n):
    return f"{n:,.0f}" if n else "-"


def build_cells(results):
    """From a flat results list, return (rps_cells, mbps_cells, ordered_labels).
    Failed cells (status != ok) render their status marker (TIMEOUT/ERR)."""
    rps, mbps, labels = {}, {}, []
    for res in results:
        lab = label_of(res)
        if lab not in labels:
            labels.append(lab)
        key = (lab, res["readers"])
        if res.get("status", "ok") == "ok":
            rps[key], mbps[key] = fmt(res["rows_per_s"]), fmt(res["mb_per_s"])
        else:
            rps[key] = mbps[key] = res.get("status", "ERR")
    return rps, mbps, labels


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

    results = []
    for engine, script, variant in targets:
        label = engine if variant is None else f"{engine}/{variant}"
        for r in reader_counts:
            print(f"[cell] {label:<22} readers={r} limit={args.limit:,} "
                  f"(warmup={args.warmup}, measure={args.repeats}) ...", file=sys.stderr)
            res, err = run_cell(script, variant, args.limit, r, args.run_timeout,
                                args.warmup, args.repeats,
                                lambda m: print(f"       {m}", file=sys.stderr))
            if res is None:
                print(f"       FAILED: {err}", file=sys.stderr)
                status = "TIMEOUT" if err and err.startswith("TIMEOUT") else "ERR"
                results.append({"engine": engine, "variant": variant, "readers": r,
                                "status": status, "rows_per_s": None, "mb_per_s": None})
                continue
            results.append(res)
            spread = (res["rows_per_s_max"] - res["rows_per_s_min"]) / res["rows_per_s"] * 100 \
                if res["rows_per_s"] else 0
            print(f"       => mean {res['rows_per_s']:,.0f} rows/s | {res['mb_per_s']:,.1f} MB/s "
                  f"(spread {spread:.1f}% over {res['repeats']} runs)", file=sys.stderr)

    method = f"mean of {args.repeats} measured run(s) after {args.warmup} warmup(s) per cell"
    rps_cells, mbps_cells, labels = build_cells(results)
    rps_tbl = table(labels, reader_counts, rps_cells, "rows/s by reader count")
    mbps_tbl = table(labels, reader_counts, mbps_cells, "MB/s (decoded payload; compare within-engine only)")
    md = (f"# Egress read benchmark\n\nlimit = {args.limit:,} rows per run — {method}\n\n"
          f"{rps_tbl}\n\n{mbps_tbl}\n")
    print("\n" + rps_tbl + "\n\n" + mbps_tbl + "\n")

    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out + ".json", "w") as fh:
        json.dump(results, fh, indent=2)
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
