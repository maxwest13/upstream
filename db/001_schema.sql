-- Upstream core schema. Works on plain Postgres 14+; 002_timescale.sql adds
-- hypertables, compression and retention when TimescaleDB is installed.

-- Raw mentions from every source. One row per post/headline/item.
CREATE TABLE IF NOT EXISTS raw_posts (
    id           BIGINT GENERATED ALWAYS AS IDENTITY,
    time         TIMESTAMPTZ NOT NULL,          -- event time from the source
    source       TEXT        NOT NULL,          -- 'bluesky', 'gdelt', ...
    tier         TEXT        NOT NULL,          -- 'inner' | 'middle' | 'outer'
    uri          TEXT        NOT NULL,          -- stable id at the source
    author_hash  TEXT,                          -- salted hash, never the raw handle
    lang         TEXT,
    text         TEXT        NOT NULL,
    tags         TEXT        NOT NULL DEFAULT '', -- space-separated lowercase hashtags
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (time, uri)
);
CREATE INDEX IF NOT EXISTS raw_posts_id_idx ON raw_posts (id);

-- Collector resume points (Jetstream cursor, last GDELT file, ...).
CREATE TABLE IF NOT EXISTS collector_state (
    source      TEXT PRIMARY KEY,
    cursor      BIGINT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Job watermarks for the Python workers.
CREATE TABLE IF NOT EXISTS job_state (
    job         TEXT PRIMARY KEY,
    last_id     BIGINT NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Term counts in 5-minute buckets, per source.
-- authors is approximate (distinct within each extraction batch).
CREATE TABLE IF NOT EXISTS term_5m (
    bucket    TIMESTAMPTZ NOT NULL,
    source    TEXT        NOT NULL,
    tier      TEXT        NOT NULL,
    term      TEXT        NOT NULL,
    kind      TEXT        NOT NULL,             -- 'hashtag' | 'phrase'
    mentions  INTEGER     NOT NULL,
    authors   INTEGER     NOT NULL,
    PRIMARY KEY (bucket, source, term)
);
CREATE INDEX IF NOT EXISTS term_5m_term_idx ON term_5m (term, bucket DESC);

-- Distinct-author sketch per term per hour: the 64 smallest author hashes
-- seen (bottom-k sketch). Sketches merge across any window, so "distinct
-- authors in the last 12h" is estimated accurately without storing every
-- author-term pair. Exact when fewer than 64 authors.
CREATE TABLE IF NOT EXISTS term_sketch_1h (
    hour    TIMESTAMPTZ NOT NULL,
    term    TEXT        NOT NULL,
    hashes  TEXT[]      NOT NULL,
    PRIMARY KEY (hour, term)
);
CREATE INDEX IF NOT EXISTS term_sketch_term_idx ON term_sketch_1h (term, hour DESC);

-- Signal snapshots written by the signal engine every run.
CREATE TABLE IF NOT EXISTS signals (
    snapshot_at    TIMESTAMPTZ NOT NULL,
    term           TEXT        NOT NULL,
    kind           TEXT        NOT NULL,
    mentions_12h   INTEGER     NOT NULL,
    mentions_prev  INTEGER     NOT NULL,        -- same 12 hours one day earlier
    mentions_24h   INTEGER     NOT NULL,
    authors_12h    INTEGER     NOT NULL,
    velocity       DOUBLE PRECISION NOT NULL,   -- 12h vs same window yesterday
    acceleration   DOUBLE PRECISION NOT NULL,   -- last 6h vs prior 6h
    breadth        INTEGER     NOT NULL,        -- number of tiers above min volume
    stage          TEXT        NOT NULL,
    score          DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (snapshot_at, term)
);
CREATE INDEX IF NOT EXISTS signals_term_idx ON signals (term, snapshot_at DESC);

-- Assets: the "tickers". Created when a term first qualifies; edited by hand
-- or by the clustering step later (merging variants into one asset).
-- Variants that compact to the same letters ("analog weekend",
-- "#analogweekend", "analog-weekend") share one ticker.
CREATE TABLE IF NOT EXISTS assets (
    term         TEXT PRIMARY KEY,
    compact      TEXT NOT NULL,                  -- lowercase letters/digits only
    ticker       TEXT NOT NULL,
    display_name TEXT,
    asset_type   TEXT,                           -- Phrase | Format | Audio | Aesthetic | Reaction
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    muted        BOOLEAN NOT NULL DEFAULT false  -- hide spam/noise from movers
);
CREATE INDEX IF NOT EXISTS assets_compact_idx ON assets (compact);
CREATE INDEX IF NOT EXISTS assets_ticker_idx ON assets (ticker);

-- Alerts already sent, so Telegram is not spammed.
CREATE TABLE IF NOT EXISTS alerts_sent (
    term     TEXT NOT NULL,
    reason   TEXT NOT NULL,
    sent_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (term, reason)
);

-- Latest snapshot per term: what the terminal and movers table read.
CREATE OR REPLACE VIEW latest_signals AS
SELECT s.*, a.ticker, a.display_name, a.asset_type
FROM signals s
LEFT JOIN assets a ON a.term = s.term
WHERE s.snapshot_at = (SELECT max(snapshot_at) FROM signals)
  AND coalesce(a.muted, false) = false;
