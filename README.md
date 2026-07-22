# QWP egress to Arrow — a performance test

How fast can a Python client stream rows **out** of a time-series database and into
Arrow? This exercises QuestDB's new **QWP** protocol (query egress over a WebSocket that
hands back Arrow batches directly), with **ClickHouse** and **TimescaleDB** alongside it
as reference points, each using the fastest columnar read path it offers.

**This is not a benchmark.** Nothing is tuned for peak: the engines run on sensible
defaults, on whatever box you point it at. It measures one specific thing — the *client
egress path*: how quickly rows cross the wire and materialise as Arrow in a Python
process, and how that scales as you add parallel connections. Treat the numbers as a
directional comparison of read paths, not as engine rankings.

All three databases hold a byte-identical `trades` dataset (`symbol`, `side`, `price`,
`amount`, `timestamp`), generated once by `datagen.py`, so the comparison is like for
like. Each reader splits the last-N-row timestamp range into `--readers` equal slices
(one connection each) and tallies rows plus decoded Arrow bytes.

## Fastest paths measured

| Engine | Variant | Path |
| --- | --- | --- |
| QuestDB | `qwp-arrow` | QWP WebSocket, `db.query(sql).iter_arrow()` streaming Arrow batches |
| ClickHouse | `arrow` | `clickhouse-connect` HTTP (:8123), `FORMAT Arrow` via `query_arrow_stream` |
| ClickHouse | `native` | `clickhouse-driver` native TCP (:9001), columnar numpy blocks (buffered) |
| TimescaleDB | `adbc` | ADBC PostgreSQL driver, `fetch_record_batch()` (Arrow over binary COPY) |
| TimescaleDB | `connectorx` | `connectorx` Rust reader, `partition_on` splits into N connections internally |

The non-QuestDB engines get two variants each, because the obvious "fastest path" isn't
obvious: HTTP-Arrow vs native-TCP for ClickHouse, and manual-split ADBC vs
self-partitioning connectorx for Timescale. Which one wins changes with the row count and
with whether the client is local or across a network.

## Prerequisites

- **Docker** — the three databases run as containers; `bench.sh` pulls any missing images.
- **A client** — either the Docker image `docker compose` builds for you, or a local venv.
  See [The client](#the-client) below; nothing else to install.

The trades value template is vendored at `trades_template.csv.gz` (1M rows, 27 symbols), so
the repo is self-contained. `datagen.py` only synthesizes the timestamps.

## The client

The read scripts import the source-built **QuestDB 5.0** client (not on PyPI). You get it
one of two ways, and every script (`bench.sh`, `serve.sh`, `client.sh`) picks between them
the same way — **`PYTHON` set → host interpreter; `PYTHON` unset → the Docker image that
`docker compose` builds from `Dockerfile.client`** (the reproducible default; the first build
compiles the client's Rust FFI, a few minutes, then it's cached and hard-pinned to a commit):

```bash
# Option A (default): let docker compose build the client image. Nothing to install.
#   -> just run the scripts below with PYTHON unset.

# Option B: a host venv (handy for iterating, or where you don't want Docker for the client).
#   setup.sh builds the pinned QuestDB 5.0 client from source into ./.venv and installs
#   requirements.txt. Needs Python 3.12, a Rust toolchain, a C compiler and git.
./setup.sh && export PYTHON=./.venv/bin/python
#   (any interpreter that already has the 5.0 client works too: export PYTHON=/path/to/python)
```

## Quick start — single box, one command

```bash
cd egress_perf

# For each engine IN ISOLATION (other two containers stopped): drop -> load N rows ->
# warmup + measured reads (mean) -> merge into a machine-tagged section of RESULTS.md.
./bench.sh 50000000                      # ROWS; optional 2nd arg = reader sweep (default 1,2,4,8)
WARMUP=2 REPEATS=3 ./bench.sh 50000000 1 # tune warmup/measured runs; single connection

# Reclaim everything when done.
./teardown.sh                            # stop containers + delete volumes (all data)
./teardown.sh --images                   # also remove the DB images + built client image
```

Each `./bench.sh` run appends one dated section to `RESULTS.md` (row count + vCPU/RAM +
method), so re-running at different row counts builds a comparison log. Per-run JSON lands in
`results/`. **Isolation:** only the engine under test has its container running, so nothing
else competes for RAM/CPU/disk; **warmup:** each cell runs `WARMUP` unmeasured passes (heats
the DB/OS page cache) then `REPEATS` measured passes, and reports their mean with a spread%.

## Split setup — client on a separate instance

Databases + ingestion on one machine, queries from another. **You coordinate which engine is
up on the DB host by hand** (bring one up, measure it from the client, switch, repeat); the
client only reads and it generates the report.

On the **DB host** — bring up one engine at a time (`up` only needs Docker, no client):

```bash
./serve.sh up questdb        # isolate engine 1 (stops the other two)
#   ... client loads + measures questdb (below) ...
./serve.sh up clickhouse     # switch to engine 2
#   ... and so on ...
./serve.sh down              # when finished
```

On the **client instance** (`DB_HOST` points at the DB host; needs the client via Option A or B):

```bash
DB_HOST=10.0.0.5 ./client.sh load    questdb 50000000               # ingest over the network
DB_HOST=10.0.0.5 ./client.sh measure questdb 50000000 1,2,4,8       # then measure it
#   ... repeat load+measure for clickhouse and timescale as you switch them on the DB host ...
ISOLATED=1 ./client.sh report 50000000                              # client stitches the combined report
```

Ingesting from the client keeps the DB host free of any Python client — it only runs
prebuilt images. (If you'd rather load locally on the DB host, `./serve.sh load <engine>
<rows>` does that instead, but then the DB host needs the client too.)

`measure` saves `results/run_<rows>_<engine>.json`; `report` merges all of them into one
combined, machine-tagged section of `RESULTS.md`. This is the regime where **parallel readers
actually scale** — a single connection is network-round-trip-bound, so `--readers` past 1 buys
real throughput (unlike single-box localhost, where the curves stay flat).

Run any single read path directly for live per-tick output:

```bash
PY=${PYTHON:-./.venv/bin/python}
$PY read_bench_questdb.py               --limit 50000000 --readers 4
$PY read_bench_clickhouse.py --variant arrow  --limit 50000000 --readers 4
$PY read_bench_clickhouse.py --variant native --limit 50000000 --readers 4
$PY read_bench_timescale.py  --variant adbc       --limit 50000000 --readers 4
$PY read_bench_timescale.py  --variant connectorx --limit 50000000 --readers 4
```

## Running on AWS — fully automated

[`aws/`](aws/) is an SSH-driven rig (modeled on `c-questdb-client/doc/net_bench`) that does
the whole split run from your laptop: two same-AZ EC2 boxes (DB host + client host),
**gp3 maxed to 16000 IOPS / 1000 MiB/s** so disk isn't the bottleneck, ingestion + queries
from the client over the real network, results scp'd back:

```bash
cd aws
./provision.sh                 # key pair, SG, cluster placement group, 2 gp3-maxed boxes
./bootstrap.sh                 # push repo, install Docker, build the client image on the client
./run.sh 50000000 1,2,4,8      # per-engine isolated: up -> load -> measure -> report -> scp back
./teardown.sh                  # terminate + delete everything by tag
```

Results land in `results/aws/RESULTS.md`. See [`aws/README.md`](aws/README.md) for config
(`EGB_*` — instance type, arch, disk), a by-hand walkthrough, and cost notes. **This is the
regime where `--readers > 1` scales** — over a real network a single connection is
round-trip-bound, so parallel readers multiply egress throughput (single-box localhost has
no RTT, so its curves stay flat).

For a single big box instead of the split rig, `./setup.sh` + `./bench.sh 50000000` works on
any Docker host (`bench.sh` records the instance's vCPU/RAM in `RESULTS.md`); mind the RAM
(`connectorx`/`clickhouse/native` buffer whole results) and disk (Timescale uncompressed).

## Data model and why timestamps are uniform

`datagen.py` stamps row `i` at `T0 + i*delta` over a fixed 30-day window (microsecond
resolution, the common precision floor - Postgres is microsecond). Two reasons:

- **Balanced reader split.** The readers divide the min→max timestamp range into N
  equal slices. That is only fair if rows are spread uniformly in time; then each slice
  holds ~the same row count (you'll see `r0=... r1=...` come out even).
- **Identical data everywhere.** One generator feeds all three loaders, so byte counts
  and cardinality match across engines. Only the timestamp is synthetic; `symbol`/`side`/
  `price`/`amount` are cycled from the real CSV.

Tables are daily-partitioned in every engine (QuestDB `PARTITION BY DAY`, ClickHouse
`PARTITION BY toYYYYMMDD`, Timescale 1-day chunks), so a 30-day span gives ~30 partitions
and the timestamp slices prune cleanly. Schemas: `schema/{questdb,clickhouse,timescale}.sql`.

## Reading the numbers

- **`rows/s` is the primary cross-engine metric.** It is the honest comparator.
- **`MB/s` is the decoded columnar payload** (`batch.nbytes`), not on-wire bytes, and is
  only comparable *within* one engine. QuestDB SYMBOL and ClickHouse `LowCardinality`
  arrive dictionary-encoded, so their string columns weigh far less than Timescale's plain
  TEXT arrays - Timescale's MB/s looks higher per row for the same data. Don't rank engines
  by MB/s.
- **`clickhouse/native` and `timescale/connectorx` buffer** the whole result (numpy blocks /
  one Arrow table) rather than streaming, so their live ticker jumps at the end; the final
  aggregate is still valid. `connectorx`'s `--readers` maps to internal partitions, not
  threads we manage.
- **The buffering variants are memory-bound; the streaming ones are not.** `connectorx`
  materialises the entire result into one Arrow table and `clickhouse/native` into numpy
  columns, so their peak client RSS grows with the row count (~43 B/row for connectorx:
  ~2 GB at 50M, ~21 GB at 500M). Past a few hundred million rows they need a big-RAM client
  or they swap. The streaming paths (`qwp-arrow`, `arrow`, `adbc`) hold constant memory at
  any size, so prefer ADBC for large Timescale egress.

  Two escape hatches: `--variants` restricts a large campaign to the safe set, and
  `--run-timeout` (default 180s) marks an over-running cell `TIMEOUT` and continues instead
  of wedging the sweep.

  ```bash
  VARIANTS=qwp-arrow,arrow,adbc ./bench.sh 500000000        # streaming only, constant RAM
  VARIANTS=qwp-arrow,arrow,adbc ./run.sh   500000000        # same, via the AWS rig
  python compare.py --variants qwp-arrow,arrow,adbc --limit 500000000
  ```

## Scaling and caveats

- **Disk.** Measured on-disk footprint per engine, and what it implies (all three coexist,
  since each engine keeps its volume between passes):

  | rows | questdb | clickhouse | timescale | total (+OS/images) |
  | --- | --- | --- | --- | --- |
  | 50M | 1.6 GB | 0.7 GB | 6.2 GB | ~14 GB |
  | 500M (×10) | ~16 GB | ~7 GB | ~62 GB | ~90 GB |

  Timescale dominates — Postgres stores rows uncompressed. Size the volume with headroom for
  its WAL during a large COPY (~200 GB for a 500M campaign). `./teardown.sh` wipes all data.
- **Parallelism needs either volume or network RTT to pay off.** On localhost with a few
  million rows, per-connection setup dominates and more readers barely help (or slightly
  hurt). The multi-reader win is real when a single connection is round-trip bound - i.e.
  over a real network - exactly the regime the QuestDB `read_bench.py` notes describe. To
  see it, run against remote servers (point the scripts at `--addr`/`--host`/`--dsn`) or
  push the row count well up.
- **QuestDB nightly + QWP egress.** `read_bench_questdb.py` talks the QWP WebSocket query
  protocol on `:9000`; the compose file pins `questdb/questdb:nightly`, which carries it.
- **Not tuned for absolute peak.** Container configs are reasonable defaults (Timescale
  gets a bigger `shared_buffers` and parallel workers). This measures *client-path* egress
  speed, not each engine's theoretical ceiling.

## Teardown

```bash
./teardown.sh               # stop containers + delete volumes (all loaded data)
./teardown.sh --images      # also remove the pulled images
```

## Files

```
bench.sh                    SINGLE BOX: per-engine isolation -> load -> warmup+measure -> RESULTS.md
serve.sh                    SPLIT / DB host: up <engine|all> | load <engine> <rows> | down
client.sh                   SPLIT / query instance: measure <engine> against DB_HOST | report
teardown.sh                 stop containers + delete volumes (+ --images)
setup.sh                    one-time host venv + source QuestDB 5.0 client (Option B / no-Docker client)
Dockerfile.client           builds the pinned QuestDB 5.0 client + read clients (Option A, compose-built)
docker-compose.yml          questdb(nightly) + clickhouse + timescaledb + bench (client, profile)
schema/*.sql                trades DDL per engine
trades_template.csv.gz      vendored value template (symbol/side/price/amount)
datagen.py                  deterministic uniform-timestamp row generator (shared)
load_questdb.py             columnar QWP load (db.dataframe)
load_clickhouse.py          Arrow insert (clickhouse-connect)
load_timescale.py           CSV COPY (psycopg3)
benchlib.py                 shared read plumbing (split math, reporter, result emit)
read_bench_questdb.py       QWP Arrow egress
read_bench_clickhouse.py    --variant arrow | native
read_bench_timescale.py     --variant adbc | connectorx
compare.py                  measure engines x variants x readers (warmup+repeats) -> results/
merge_results.py            stitch per-engine result JSONs -> combined RESULTS.md section
aws/                        SSH-driven AWS rig: provision | bootstrap | run | teardown (see aws/README.md)
```

Measurement output (`RESULTS.md`, `results/`) is generated at run time and gitignored — this
repo ships the harness, not numbers.

All endpoints are env-configurable (`QDB_ADDR`, `CH_HOST`/`CH_HTTP_PORT`/`CH_NATIVE_PORT`,
`TS_HOST`/`TS_PORT`, …) with localhost defaults, which is what lets the same scripts run
on one box, in the compose-built client container (service-name endpoints), or against a
remote DB host (`DB_HOST=…`).
