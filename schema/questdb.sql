-- QuestDB `trades`. Pre-created so `timestamp` is microsecond TIMESTAMP (not the
-- nanosecond TIMESTAMP_NS the columnar loader would auto-create), matching the
-- microsecond precision used in ClickHouse and TimescaleDB. WAL + daily partitions.
CREATE TABLE IF NOT EXISTS trades (
    symbol    symbol,
    side      symbol,
    price     double,
    amount    double,
    timestamp timestamp
) TIMESTAMP(timestamp) PARTITION BY DAY WAL;
