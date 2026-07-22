# Running the egress test on AWS (SSH-driven, same-AZ split)

Automates the split run end to end from your laptop: two EC2 boxes in **one AZ / cluster
placement group** (lowest RTT), the **DB host** running the databases and the **client
host** running ingestion + the read test and generating the report. Modeled on
`c-questdb-client/doc/net_bench`, but over plain **SSH/scp** instead of SSM.

Why bother with two boxes: on a single machine the client and server share a loopback with
no round-trip latency, so extra reader connections buy nothing. Across a real network a
single connection is round-trip-bound, and parallel readers actually multiply egress
throughput — that only shows up on a rig like this.

Only the **client** box builds the QuestDB 5.0 client image (the heavy step); the **DB**
box just pulls prebuilt images. Ingestion runs *from the client over the network*, so the
DB host needs no Python client at all. The **root EBS is gp3 maxed to 16000 IOPS /
1000 MiB/s** so disk is never the bottleneck (especially the server's cold reads).

## Laptop prerequisites

- `aws` CLI v2, **already logged in** to the target account (`aws sso login` for your
  profile, or `aws configure`). The scripts use your ambient AWS credentials and abort
  with a hint if `aws sts get-caller-identity` fails — they never log you in for you.
  Pin a named profile with `EGB_AWS_PROFILE` if you don't want the default chain.
- `ssh`, `rsync`, `curl`
- An SSH key pair. By default `provision.sh` creates a rig-owned key `egress-bench-key`
  and saves the `.pem` here in `aws/`. To reuse an existing key, set
  `EGB_KEY_NAME` and `EGB_KEY_FILE` (see below).

## Config

All knobs live in [`env.sh`](env.sh), overridable via `EGB_*` env vars:

| Variable | Default | Purpose |
| --- | --- | --- |
| `EGB_AWS_PROFILE` | *(ambient)* | pin a named AWS profile (else default chain) |
| `EGB_AWS_REGION` | `eu-west-1` | region |
| `EGB_INSTANCE_TYPE` | `c8gn.2xlarge` | box type (8 vCPU / 16 GB, Graviton4, network-optimized) |
| `EGB_ARCH` | `arm64` | `x86_64` or `arm64` (match the instance family) |
| `EGB_KEY_NAME` / `EGB_KEY_FILE` | `egress-bench-key` / `aws/egress-bench-key.pem` | reuse an existing key by pointing both at it |
| `EGB_ROOT_GB` | `120` | root gp3 size (Timescale is uncompressed, ~6 GB/50M rows) |
| `EGB_EBS_IOPS` / `EGB_EBS_THROUGHPUT` | `16000` / `1000` | gp3 maxed |

## Sizing the rig — read this before picking a row count

This measures the **egress path**, but only while the data is served from the DB host's
**page cache**. Once the dataset exceeds the server's RAM, reads come off EBS and you are
measuring the *disk* instead — throughput collapses to the volume's ceiling and the engine
ranking changes for reasons that have nothing to do with the protocol.

Measured on-disk footprint per row (from real `du` on a loaded rig):

| engine | B/row on disk | 500M on disk |
| --- | --: | --: |
| clickhouse | ~22 B | ~11 GB |
| questdb | ~30 B | ~15 GB |
| **timescale** | **~124 B** | **~62 GB** ← Postgres tuple overhead; sets the ceiling |

Because engines are tested **in isolation**, only the *active* engine must be resident, not
the sum. So the rule of thumb is:

> **usable page cache ÷ 124 B ≈ max rows** (Timescale binds it; ~4× more if you skip it)

On a 16 GB server that is only ~100M rows. Sizing for cache residency:

| server RAM | max rows (all three, cache-resident) |
| --- | --: |
| 16 GB | ~100M |
| 64 GB | ~450M |
| 128 GB | ~900M |

**Network matters as much as RAM.** Once data is cached, egress is bounded by the NIC — a
cached run has been observed near 39 Gb/s, so a "big memory" instance with a small pipe is a
trap. Check both before choosing:

| type | vCPU | RAM | network | verdict |
| --- | --: | --: | --- | --- |
| `c8gn.2xlarge` | 8 | 16 GB | 50 Gbps | fine up to ~100M rows |
| `r8g.4xlarge` | 16 | 128 GB | *up to 15 Gbps* | ❌ big RAM, starved NIC |
| `m8gn.4xlarge` | 16 | 64 GB | 50 Gbps | good mid-size |
| **`m8gn.8xlarge`** | 32 | 128 GB | 100 Gbps | ✅ best balance for ~500M |
| `c8gn.16xlarge` | 64 | 128 GB | 200 Gbps | fastest, priciest |

Both boxes count: the **client's** NIC caps the run too, so pairing a 100 Gbps server with a
50 Gbps client tops out at ~50 Gbps. `provision.sh` currently uses one type for both boxes;
set `EGB_INSTANCE_TYPE` accordingly, or resize the server alone afterwards (stop → change
type → start, which preserves data — see `SKIP_LOAD` above).

Client RAM is a separate constraint: `connectorx` and `clickhouse/native` buffer the whole
result client-side (~43 B/row), so 500M needs ~21 GB there. The streaming variants
(`qwp-arrow`, `arrow`, `adbc`) hold constant memory — use `VARIANTS` to stick to them.

## Lifecycle

```sh
cd aws

# 1. Provision: key pair, SG (SSH from your IP + all intra-group), cluster placement
#    group, 2 tagged instances, gp3 maxed. Aborts if the rig already exists.
./provision.sh

# 2. Bootstrap: push the repo, install Docker on both, pre-pull DB images on the
#    server, build the client image on the client (compiles the QuestDB 5.0 client).
./bootstrap.sh

# 3. Run. Two drivers, same work:
#
#    a) UNATTENDED (recommended) - launches the campaign DETACHED on the client box, so it
#       survives your laptop, your ssh session and this script. Launch it and walk away.
nohup ./campaign.sh 500000000 1,2,4,8 > /tmp/campaign_driver.log 2>&1 &
#
#    b) ATTENDED - drives every step over ssh from your laptop; if the laptop drops, the
#       run stops with it. Fine for short runs.
./run.sh 50000000 1,2,4,8         # ROWS  READERS   (env: WARMUP REPEATS VARIANTS RUN_TIMEOUT)
#
#    -> results/aws/RESULTS.md on your laptop

# 4. Tear everything down (instances, SG, placement group, rig-created key).
./teardown.sh
```

### `campaign.sh` env

| Var | Default | Purpose |
| --- | --- | --- |
| `VARIANTS` | all | restrict read paths, e.g. `qwp-arrow,arrow,adbc` (streaming only) |
| `WARMUP` / `REPEATS` | `1` / `3` | unmeasured warmups, then measured runs (mean reported) |
| `RUN_TIMEOUT` | `3600` | per-run cap; **raise it for big row counts** or cells get marked `TIMEOUT` |
| `SKIP_LOAD` | `0` | measure only — data is already loaded (e.g. after an instance resize) |
| `ENGINES` | all three | subset of engines |
| `DEADMAN_MIN` | `720` | runaway-cost backstop: OS shutdown after N min (`0` disables) |
| `DEADMAN_TERMINATE` | `0` | make that shutdown terminate — **destroys all data**; off by default |
| `TEARDOWN` | `0` | tear the rig down when finished |
| `SERVER_PUB`/`CLIENT_PUB`/`SERVER_PRIV` | — | skip AWS lookups; the campaign itself needs no AWS credentials |

It is idempotent: if a campaign is already in flight it waits and fetches rather than
launching a second one.

**Stop/start preserves everything.** Resizing an instance (stop → change type → start) keeps
the EBS root volume, so the loaded data, Docker, the images and the built client all survive
— only the public IP changes and containers need `serve.sh up` again. That is why
`SKIP_LOAD=1` exists: after a resize you re-measure without repeating a multi-hour load.
Only *terminate* destroys data, which is why the dead-man defaults to **stop**.

## Doing it by hand (no run.sh)

`run.sh` is just this loop; you can drive it piecewise for ad-hoc checks. Get the server's
private IP once (`aws ec2 describe-instances ...`, or read it from `provision.sh` output),
then for each engine:

```sh
K=aws/egress-bench-key.pem      # or your own EGB_KEY_FILE

# on the DB host — bring up ONLY this engine (isolation):
ssh -i $K ubuntu@<server-public> 'cd /opt/egress_perf && ./serve.sh up questdb'

# on the client host — load, then measure, against the server's PRIVATE ip:
ssh -i $K ubuntu@<client-public> \
    'cd /opt/egress_perf && DB_HOST=<server-private> ./client.sh load    questdb 50000000'
ssh -i $K ubuntu@<client-public> \
    'cd /opt/egress_perf && DB_HOST=<server-private> ./client.sh measure questdb 50000000 1,2,4,8'

# ...repeat for clickhouse and timescale, then on the client:
ssh -i $K ubuntu@<client-public> 'cd /opt/egress_perf && ISOLATED=1 ./client.sh report 50000000'
scp -i $K ubuntu@<client-public>:/opt/egress_perf/RESULTS.md results/aws/
```

`ssh <box> 'cd /opt/egress_perf && ...'` also lets you poke around interactively (e.g.
`docker compose ps`, `docker stats`, `./serve.sh down`).

## Scripts

| Script | Runs on | What it does |
| --- | --- | --- |
| `provision.sh` | laptop | create key pair, SG, placement group, 2 instances (gp3 maxed) |
| `bootstrap.sh` | laptop | push repo, install Docker, pull DB images (server), build client (client) |
| `campaign.sh` | laptop | **unattended** driver: launch detached on the client, wait, fetch results |
| `box_campaign.sh` | client box | the campaign loop itself (up → load → measure → report) |
| `run.sh` | laptop | attended driver: same work, but dies with your laptop |
| `teardown.sh` | laptop | terminate + delete everything by tag |
| `box_bootstrap.sh` | either box | install Docker Engine + compose plugin |
| `env.sh` / `lib.sh` | laptop | shared config + ssh/rsync helpers |

## Notes

- **Cost.** Two `c8gn.2xlarge` + gp3 run a few $/hr; `teardown.sh` removes the instances,
  security group, and placement group (tag `Project=egress-bench` scopes every resource).
- **Keys on teardown.** A key `provision.sh` auto-created is deleted (AWS pair + local `.pem`,
  tracked by a marker file it wrote). A **reused/pre-existing key is never deleted** — there
  is no override, so pointing the rig at your own key can't lose it.
- **Same-AZ, real RTT.** This is the regime where `--readers > 1` finally scales: a single
  connection is round-trip-bound, so parallel readers multiply egress throughput (unlike
  single-box localhost, where the curves stay flat).
- **Untested against live AWS here** — authored to mirror the proven `net_bench` rig; run
  `provision.sh` behind your own credentials. Read each script before running; it creates
  billable resources.
