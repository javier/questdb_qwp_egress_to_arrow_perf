-- ClickHouse `trades`, mirroring the QuestDB table.
--   symbol/side  -> LowCardinality(String)  (QuestDB SYMBOL analogue; dictionary-encoded)
--   price/amount -> Float64
--   timestamp    -> DateTime64(6, 'UTC')     (microsecond, matches the common precision floor)
-- MergeTree ordered by timestamp so the reader's range slices prune to contiguous
-- granules; daily partitions mirror QuestDB PARTITION BY DAY / a Timescale 1-day chunk.
CREATE TABLE IF NOT EXISTS trades
(
    symbol    LowCardinality(String),
    side      LowCardinality(String),
    price     Float64,
    amount    Float64,
    timestamp DateTime64(6, 'UTC')
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY timestamp;
