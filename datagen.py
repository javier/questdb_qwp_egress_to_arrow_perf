#!/usr/bin/env python3
"""Deterministic `trades` generator with uniform synthetic timestamps.

One generator feeds all three loaders (QuestDB, ClickHouse, TimescaleDB) so the
databases hold *byte-identical* data and the read benchmark compares like for
like. Two properties matter and both are deliberate:

1. **Uniform timestamps.** The read benchmarks split the row range into N equal
   timestamp slices, one per reader connection. That split is only balanced if
   the rows are spread uniformly in time. Row ``i`` is stamped ``T0 + i*delta``
   over a fixed ``SPAN_DAYS`` window, so every slice holds ~the same row count.
   (Contrast ``csv_columnar_sender.py``'s live-``now()`` stamping, which clusters
   rows into dense 1 ns runs with wall-clock gaps - fine for an ingest demo, but
   it would make the reader split lopsided and the cross-DB comparison unfair.)

2. **Realistic values.** ``symbol/side/price/amount`` are cycled (with wraparound)
   from a real trades CSV, so column cardinality and widths match production data
   (27 symbols, 2 sides). Only the timestamp is synthetic.

Timestamps are microsecond resolution - the common precision floor across the
three engines (PostgreSQL/TimescaleDB is microsecond; QuestDB TIMESTAMP and
ClickHouse DateTime64(6) both hold micros). ``floor(i*delta)`` is non-decreasing,
so the stream is monotonic (no out-of-order rows for QuestDB's designated ts).

Emits batches as dicts of numpy arrays::

    {"symbol": <U>, "side": <U>, "price": f8, "amount": f8, "timestamp_us": i8}

where ``timestamp_us`` is epoch microseconds. Each loader adapts that to its
engine's fastest ingest shape.
"""

import gzip
import io
import os

import numpy as np
import polars as pl

# Real trades CSV used purely as a value template (symbol/side/price/amount).
# Vendored into the repo so the benchmark is self-contained and portable.
DEFAULT_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "trades_template.csv.gz")

# Epoch-microsecond anchor for row 0: 2025-01-01T00:00:00Z.
T0_US = 1_735_689_600_000_000
# Rows are spread uniformly across this many days regardless of row count, so the
# timestamp distribution (hence the reader split) is scale-invariant.
SPAN_DAYS = 30


def load_template(path=DEFAULT_TEMPLATE):
    """Load the value template CSV once as numpy columns (symbol/side/price/amount)."""
    if path.endswith(".gz"):
        with gzip.open(path, "rb") as fh:
            df = pl.read_csv(io.BytesIO(fh.read()))
    else:
        df = pl.read_csv(path)
    for col in ("symbol", "side", "price", "amount"):
        if col not in df.columns:
            raise ValueError(f"template CSV missing required column: {col}")
    return (
        df["symbol"].cast(pl.Utf8).to_numpy(),
        df["side"].cast(pl.Utf8).to_numpy(),
        df["price"].cast(pl.Float64).to_numpy(),
        df["amount"].cast(pl.Float64).to_numpy(),
    )


def delta_us(total_rows, span_days=SPAN_DAYS):
    """Microseconds between consecutive rows for a uniform span."""
    span = span_days * 86_400 * 1_000_000
    return span / total_rows


def iter_batches(total_rows, batch_rows=1_000_000, template_path=DEFAULT_TEMPLATE,
                 t0_us=T0_US, span_days=SPAN_DAYS):
    """Yield ``(batch_dict, produced_before)`` covering ``total_rows`` rows.

    ``batch_dict`` has numpy arrays keyed symbol/side/price/amount/timestamp_us.
    ``produced_before`` is the global index of the batch's first row (for progress).
    """
    sym, side, price, amount = load_template(template_path)
    L = len(sym)
    d = delta_us(total_rows, span_days)
    produced = 0
    while produced < total_rows:
        n = min(batch_rows, total_rows - produced)
        gidx = np.arange(produced, produced + n)
        tidx = gidx % L
        ts_us = t0_us + np.floor(gidx * d).astype(np.int64)
        yield {
            "symbol": sym[tidx],
            "side": side[tidx],
            "price": price[tidx],
            "amount": amount[tidx],
            "timestamp_us": ts_us,
        }, produced
        produced += n


def time_bounds(total_rows, t0_us=T0_US, span_days=SPAN_DAYS):
    """(lo_us, hi_us) of the full generated range - handy for pre-creating chunks."""
    d = delta_us(total_rows, span_days)
    lo = t0_us
    hi = t0_us + int(np.floor((total_rows - 1) * d))
    return lo, hi
