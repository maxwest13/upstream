-- TimescaleDB setup: time partitioning, automatic cleanup, compression.
-- Runs once, on the first start of the docker-compose db container.
-- Compression is best-effort: if this TimescaleDB version rejects a setting,
-- it logs a warning and the database still starts. Retention (auto-delete of
-- old data) always applies.

CREATE EXTENSION IF NOT EXISTS timescaledb;

SELECT create_hypertable('raw_posts', 'time', migrate_data => true, if_not_exists => true,
                         chunk_time_interval => INTERVAL '6 hours');
SELECT create_hypertable('term_5m', 'bucket', migrate_data => true, if_not_exists => true,
                         chunk_time_interval => INTERVAL '1 day');
SELECT create_hypertable('term_sketch_1h', 'hour', migrate_data => true, if_not_exists => true,
                         chunk_time_interval => INTERVAL '1 day');

-- Retention: raw text is bulky and only needed for recent examples;
-- term counts are small and kept a year for cohort and comparables work.
SELECT add_retention_policy('raw_posts', INTERVAL '10 days', if_not_exists => true);
SELECT add_retention_policy('term_5m', INTERVAL '365 days', if_not_exists => true);
SELECT add_retention_policy('term_sketch_1h', INTERVAL '30 days', if_not_exists => true);

-- Engagement snapshots arrive with 003_sources.sql (runs after this file on a
-- fresh install); make it a hypertable there if TimescaleDB is present.

-- Compression. Unique-key columns must appear in segmentby/orderby.
DO $$
BEGIN
    ALTER TABLE raw_posts SET (timescaledb.compress,
        timescaledb.compress_segmentby = 'source',
        timescaledb.compress_orderby = 'time DESC, uri');
    PERFORM add_compression_policy('raw_posts', INTERVAL '1 day', if_not_exists => true);
EXCEPTION WHEN others THEN
    RAISE WARNING 'raw_posts compression not enabled: %', SQLERRM;
END $$;

DO $$
BEGIN
    ALTER TABLE term_5m SET (timescaledb.compress,
        timescaledb.compress_segmentby = 'term',
        timescaledb.compress_orderby = 'bucket DESC, source');
    PERFORM add_compression_policy('term_5m', INTERVAL '3 days', if_not_exists => true);
EXCEPTION WHEN others THEN
    RAISE WARNING 'term_5m compression not enabled: %', SQLERRM;
END $$;
