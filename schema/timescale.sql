-- TimescaleDB `trades`, mirroring the QuestDB table.
--   symbol/side  -> text          (Postgres has no low-cardinality type; plain TEXT)
--   price/amount -> double precision
--   timestamp    -> timestamptz   (microsecond precision, like the others)
-- A hypertable partitioned into 1-day chunks mirrors QuestDB PARTITION BY DAY and
-- ClickHouse daily parts, and lets the planner prune chunks to each reader's slice.
CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS trades (
    symbol    text        NOT NULL,
    side      text        NOT NULL,
    price     double precision,
    amount    double precision,
    timestamp timestamptz NOT NULL
);

SELECT create_hypertable(
    'trades', 'timestamp',
    chunk_time_interval => interval '1 day',
    if_not_exists => TRUE
);
