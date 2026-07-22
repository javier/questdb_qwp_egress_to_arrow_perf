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

`c8gn.2xlarge` has 16 GB RAM — fine for 50M in the split layout (DB and client are on
separate boxes). For much larger runs, `connectorx`/`clickhouse-native` buffer the whole
result on the **client**, so bump the client's RAM (`EGB_INSTANCE_TYPE=r8g.2xlarge`, 64 GB)
if you push far past 50M.

## Lifecycle

```sh
cd aws

# 1. Provision: key pair, SG (SSH from your IP + all intra-group), cluster placement
#    group, 2 tagged instances, gp3 maxed. Aborts if the rig already exists.
./provision.sh

# 2. Bootstrap: push the repo, install Docker on both, pre-pull DB images on the
#    server, build the client image on the client (compiles the QuestDB 5.0 client).
./bootstrap.sh

# 3. Run: per engine -> up-alone on server -> load from client over the net ->
#    measure (warmup+repeat-average) -> client builds the report -> scp back here.
./run.sh 50000000 1,2,4,8         # ROWS  READERS   (env: WARMUP=2 REPEATS=3)
#    -> results/aws/RESULTS.md on your laptop

# 4. Tear everything down (instances, SG, placement group, key pair).
./teardown.sh
```

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
| `run.sh` | laptop | drive the full isolated split run, scp `RESULTS.md` back |
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
