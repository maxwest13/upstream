//! Upstream collector #1: Bluesky Jetstream -> raw_posts.
//!
//! Always-on. Keeps one websocket open, filters to new posts in the wanted
//! languages, batches inserts, and saves the Jetstream cursor after every
//! successful flush so a restart resumes where it left off (with a few
//! seconds of overlap; duplicates are dropped by the (time, uri) key).

mod parse;

use anyhow::{Context, Result};
use futures_util::StreamExt;
use parse::{to_post, Event, Post};
use std::time::{Duration, Instant, SystemTime};
use tokio_postgres::{Client, NoTls};
use tokio_tungstenite::{connect_async, tungstenite::Message};
use tracing::{error, info, warn};

const SOURCE: &str = "bluesky";
const TIER: &str = "inner";
const FLUSH_EVERY: Duration = Duration::from_secs(2);
const FLUSH_SIZE: usize = 1000;
/// Rewind this far on reconnect so nothing is lost between flush and crash.
const REWIND_US: u64 = 5_000_000;

struct Config {
    database_url: String,
    jetstream_url: String,
    langs: Vec<String>,
    salt: String,
}

impl Config {
    fn from_env() -> Result<Self> {
        let salt = std::env::var("AUTHOR_SALT").context("AUTHOR_SALT is required")?;
        if salt.len() < 16 || salt.starts_with("change-me") {
            anyhow::bail!("AUTHOR_SALT must be set to a long random value");
        }
        Ok(Self {
            database_url: std::env::var("DATABASE_URL").context("DATABASE_URL is required")?,
            jetstream_url: std::env::var("JETSTREAM_URL")
                .unwrap_or_else(|_| "wss://jetstream2.us-east.bsky.network/subscribe".into()),
            langs: std::env::var("JETSTREAM_LANGS")
                .unwrap_or_else(|_| "en".into())
                .split(',')
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .collect(),
            salt,
        })
    }
}

async fn connect_db(url: &str) -> Result<Client> {
    let (client, conn) = tokio_postgres::connect(url, NoTls).await.context("connecting to Postgres")?;
    tokio::spawn(async move {
        if let Err(e) = conn.await {
            error!("postgres connection ended: {e}");
        }
    });
    Ok(client)
}

async fn load_cursor(db: &Client) -> Result<Option<u64>> {
    let row = db
        .query_opt("SELECT cursor FROM collector_state WHERE source = $1", &[&SOURCE])
        .await?;
    Ok(row.map(|r| r.get::<_, i64>(0) as u64))
}

async fn flush(db: &Client, batch: &mut Vec<Post>) -> Result<u64> {
    if batch.is_empty() {
        return Ok(0);
    }
    let times: Vec<SystemTime> = batch.iter().map(|p| p.time).collect();
    let uris: Vec<&str> = batch.iter().map(|p| p.uri.as_str()).collect();
    let authors: Vec<&str> = batch.iter().map(|p| p.author_hash.as_str()).collect();
    let langs: Vec<&str> = batch.iter().map(|p| p.lang.as_str()).collect();
    let texts: Vec<&str> = batch.iter().map(|p| p.text.as_str()).collect();
    let tags: Vec<&str> = batch.iter().map(|p| p.tags.as_str()).collect();
    let max_cursor = batch.iter().map(|p| p.time_us).max().unwrap_or(0) as i64;

    let inserted = db
        .execute(
            "INSERT INTO raw_posts (time, source, tier, uri, author_hash, lang, text, tags)
             SELECT t, $1, $2, u, a, l, x, g
             FROM UNNEST($3::timestamptz[], $4::text[], $5::text[], $6::text[], $7::text[], $8::text[])
                  AS r(t, u, a, l, x, g)
             ON CONFLICT (time, uri) DO NOTHING",
            &[&SOURCE, &TIER, &times, &uris, &authors, &langs, &texts, &tags],
        )
        .await
        .context("inserting batch")?;

    db.execute(
        "INSERT INTO collector_state (source, cursor, updated_at) VALUES ($1, $2, now())
         ON CONFLICT (source) DO UPDATE SET cursor = EXCLUDED.cursor, updated_at = now()",
        &[&SOURCE, &max_cursor],
    )
    .await
    .context("saving cursor")?;

    batch.clear();
    Ok(inserted)
}

/// One websocket session. Returns when the socket closes or errors.
async fn run_session(cfg: &Config, db: &Client) -> Result<()> {
    let mut url = format!("{}?wantedCollections=app.bsky.feed.post", cfg.jetstream_url);
    if let Some(c) = load_cursor(db).await? {
        url.push_str(&format!("&cursor={}", c.saturating_sub(REWIND_US)));
    }
    info!("connecting to {url}");
    let (ws, _) = connect_async(&url).await.context("websocket connect")?;
    let (_write, mut read) = ws.split();

    let mut batch: Vec<Post> = Vec::with_capacity(FLUSH_SIZE);
    let mut last_flush = Instant::now();
    let mut stats_at = Instant::now();
    let (mut seen, mut kept, mut written) = (0u64, 0u64, 0u64);
    let mut tick = tokio::time::interval(Duration::from_millis(500));
    // Docker stops containers with SIGTERM; flush before exiting.
    let mut sigterm = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;

    loop {
        tokio::select! {
            msg = read.next() => {
                let msg = match msg {
                    Some(Ok(m)) => m,
                    Some(Err(e)) => { flush(db, &mut batch).await?; return Err(e.into()); }
                    None => { flush(db, &mut batch).await?; return Ok(()); }
                };
                if let Message::Text(txt) = msg {
                    seen += 1;
                    match serde_json::from_str::<Event>(&txt) {
                        Ok(ev) => if let Some(p) = to_post(&ev, &cfg.langs, &cfg.salt) {
                            kept += 1;
                            batch.push(p);
                        },
                        Err(e) => warn!("unparseable event: {e}"),
                    }
                }
            }
            _ = tick.tick() => {}
            _ = sigterm.recv() => {
                info!("SIGTERM, flushing {} posts", batch.len());
                flush(db, &mut batch).await?;
                std::process::exit(0);
            }
            _ = tokio::signal::ctrl_c() => {
                info!("shutting down, flushing {} posts", batch.len());
                flush(db, &mut batch).await?;
                std::process::exit(0);
            }
        }

        if batch.len() >= FLUSH_SIZE || (!batch.is_empty() && last_flush.elapsed() >= FLUSH_EVERY) {
            written += flush(db, &mut batch).await?;
            last_flush = Instant::now();
        }
        if stats_at.elapsed() >= Duration::from_secs(60) {
            info!("last 60s: {seen} events, {kept} posts kept, {written} rows written");
            seen = 0;
            kept = 0;
            written = 0;
            stats_at = Instant::now();
        }
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    // rustls needs one crypto backend chosen explicitly for wss:// connections.
    let _ = rustls::crypto::ring::default_provider().install_default();
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .init();
    let cfg = Config::from_env()?;
    let mut backoff = Duration::from_secs(1);

    loop {
        let result = async {
            let db = connect_db(&cfg.database_url).await?;
            run_session(&cfg, &db).await
        }
        .await;
        match result {
            Ok(()) => {
                warn!("stream closed, reconnecting");
                backoff = Duration::from_secs(1);
            }
            Err(e) => {
                error!("session failed: {e:#}; retrying in {}s", backoff.as_secs());
                tokio::time::sleep(backoff).await;
                backoff = (backoff * 2).min(Duration::from_secs(60));
            }
        }
    }
}
