-- v0.2: attention data (Wikipedia page views) for the multi-source collectors.
-- Safe to run more than once.
--   Existing install:  docker compose exec -T db psql -U upstream -d upstream < db/003_sources.sql
--   Fresh install:     runs automatically on first start.

CREATE TABLE IF NOT EXISTS attention_daily (
    day      DATE   NOT NULL,
    source   TEXT   NOT NULL,     -- 'wikipedia'
    term     TEXT   NOT NULL,     -- article title, lowercase, spaces
    compact  TEXT   NOT NULL,     -- letters/digits only, to match phrases and hashtags
    views    BIGINT NOT NULL,
    PRIMARY KEY (day, source, term)
);
CREATE INDEX IF NOT EXISTS attention_daily_compact_idx ON attention_daily (compact, day DESC);

-- Repeated engagement readings per video or stream, every poll. Acceleration
-- (views now vs 30 minutes ago) finds a small video taking off long before
-- it tops any chart. Any future TikTok/Instagram vendor feed lands here too.
CREATE TABLE IF NOT EXISTS engagement_snapshots (
    time      TIMESTAMPTZ NOT NULL,
    source    TEXT        NOT NULL,
    item_id   TEXT        NOT NULL,
    uri       TEXT        NOT NULL,
    title     TEXT        NOT NULL,
    views     BIGINT      NOT NULL,
    likes     BIGINT,
    comments  BIGINT,
    PRIMARY KEY (time, source, item_id)
);
CREATE INDEX IF NOT EXISTS engagement_item_idx ON engagement_snapshots (source, item_id, time DESC);

-- Fast per-source health checks.
CREATE INDEX IF NOT EXISTS raw_posts_source_ingested_idx ON raw_posts (source, ingested_at DESC);

-- With TimescaleDB: partition snapshots by day and keep 30 days. Best effort.
DO $$
BEGIN
    PERFORM create_hypertable('engagement_snapshots', 'time', migrate_data => true, if_not_exists => true,
                              chunk_time_interval => INTERVAL '1 day');
    PERFORM add_retention_policy('engagement_snapshots', INTERVAL '30 days', if_not_exists => true);
EXCEPTION WHEN others THEN
    RAISE NOTICE 'engagement_snapshots stays a plain table: %', SQLERRM;
END $$;
