# Upstream backend · v0.1

Always-on pipeline that watches culture in real time and turns it into
tradeable-style signals for the Upstream terminal.

```
Bluesky Jetstream ──► collector-bluesky (Rust) ──► raw_posts
                                                      │  every minute
                                                      ▼
                                          worker: extract ──► term_5m + author sketches
                                                      │  every 10 minutes
                                                      ▼
                                          worker: signals ──► signals, assets (tickers), Telegram alerts
                                                      │
                                                      ▼
                                          api (FastAPI) ──► terminal, share cards, weekly
```

## What v0.1 does

- **Collects** every new English Bluesky post live (the inner tier), stores
  text and hashtags, and hashes author ids. Raw handles are never stored.
  Resumes from its saved cursor after any restart or crash, with no gaps and no duplicates.
- **Extracts** hashtags, two- and three-word phrases and words into
  5-minute counts. Phrases never cross punctuation.
- **Counts distinct people, not posts.** It uses a bottom-k sketch per term
  per hour, so one account spamming a phrase can't fake a trend.
- **Scores momentum** every 10 minutes:
  - velocity: last 12h vs the same 12h yesterday
  - acceleration: last 6h vs the 6h before
  - breadth, a lifecycle stage and a momentum score
- **Mints tickers** for rising terms automatically (`analog weekend` →
  `ANLGWKND`). Spelling variants like `#analogweekend` share one ticker.
- **Alerts** you on Telegram when something breaks out, once per term.
- **Serves** the data to the terminal over a token-protected API.

## Sources (v0.2)

| Source | Tier | Key needed | Every | What it adds |
|---|---|---|---|---|
| Bluesky (Rust collector) | inner | none | live | every new English post |
| Culture-press RSS (`signals/upstream/collectors/feeds.txt`) | outer | none | 15 min | when a trend reaches the mainstream |
| Wikipedia top articles | outer | none (set `BOT_CONTACT_EMAIL`) | 3 h | public attention spikes, used as breadth evidence |
| YouTube trending (4 regions) | middle | `YOUTUBE_API_KEY` | 30 min | trending videos + view snapshots for acceleration |
| Tumblr seed tags | inner | `TUMBLR_API_KEY` | 15 min | aesthetics and fandom |
| GIPHY trending | middle | `GIPHY_API_KEY` | 30 min | trending reactions |
| Twitch top streams | middle | `TWITCH_CLIENT_ID` + `TWITCH_CLIENT_SECRET` | 15 min | stream titles + viewer snapshots |

Sources without a key stay off; `docker compose exec collectors python -m upstream.collectors --list` shows which are on.
`GET /v1/sources` shows each one's latest item and volume; `GET /v1/accelerating?source=youtube` ranks videos by
view growth relative to their size; `GET /v1/attention` lists Wikipedia spikes.

## Deploy on the Hetzner box

```bash
# 1. Docker, if it isn't there already
curl -fsSL https://get.docker.com | sh

# 2. Code
scp upstream-backend.zip root@YOUR_SERVER:~ && ssh root@YOUR_SERVER
unzip upstream-backend.zip && cd upstream

# 3. Secrets
cp .env.example .env
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env
PW=$(grep ^POSTGRES_PASSWORD .env | cut -d= -f2)
sed -i "s#^DATABASE_URL=.*#DATABASE_URL=postgres://upstream:$PW@db:5432/upstream#" .env
sed -i "s/^AUTHOR_SALT=.*/AUTHOR_SALT=$(openssl rand -hex 24)/" .env
sed -i "s/^API_TOKEN=.*/API_TOKEN=$(openssl rand -hex 24)/" .env
# optional: add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID

# 4. Start everything (restarts automatically on crash or reboot)
docker compose up -d --build
```

The `db` container runs `db/001_schema.sql` and `db/002_timescale.sql` on
first start only.

## Check it's alive

```bash
docker compose logs -f collector-bluesky   # "last 60s: N events, N posts kept, N rows written"
docker compose logs -f worker              # extract every minute, signals every 10
curl -s localhost:8080/health
curl -s -H "Authorization: Bearer $(grep ^API_TOKEN .env | cut -d= -f2)" \
     "localhost:8080/v1/movers?limit=20" | python3 -m json.tool
```

The first real signals appear after about **36 hours**. Velocity compares
today with the same hours yesterday, so it needs a day and a half of history.
Before that, movers will be empty or noisy.

## Expose the API (for the terminal)

Put Caddy in front for automatic HTTPS. In `/etc/caddy/Caddyfile`:

```
api.yourdomain.com {
    reverse_proxy 127.0.0.1:8080
}
```

## Endpoints

| Endpoint | Returns |
|---|---|
| `GET /health` | last post time, last signal snapshot (no token needed) |
| `GET /v1/movers?direction=up\|down\|all&limit=50` | ranked movers, one row per ticker |
| `GET /v1/terms/{term}/series?hours=48` | hourly mentions per tier (candles, sparklines) |
| `GET /v1/terms/{term}/examples?limit=10` | recent post text, no author data |

## Tuning

All thresholds are environment variables in `.env`. Restart the worker after
changing them: `docker compose up -d worker`.

| Variable | Default | What it does |
|---|---|---|
| `MIN_MENTIONS_24H` | 40 | Ignore terms below this daily volume |
| `MIN_AUTHORS_12H` | 15 | Ignore terms posted by fewer distinct people |
| `MAX_MENTIONS_PER_AUTHOR` | 3 | Drop terms where a few accounts post a lot (spam) |
| `BREAKOUT_VELOCITY` | 1.5 | +150% vs yesterday triggers a Telegram alert |
| `INCLUDE_WORDS` | true | Track single words as well as phrases |

To hide noise from the movers list:

```sql
UPDATE assets SET muted = true WHERE ticker = 'XXXXXXXX';
```

Check disk use daily for the first week:

```sql
SELECT hypertable_name, pg_size_pretty(hypertable_size(format('%I', hypertable_name)::regclass))
FROM timescaledb_information.hypertables;
```

Raw posts compress after a day and are dropped after 10 days. Term counts
are kept for a year for cohort and comparables analysis. Both settings live
in `db/002_timescale.sql`.

## Tests

```bash
cd collector-bluesky && cargo test                  # event parsing, filtering, author hashing
cd signals && pip install -e ".[test]" && pytest     # extraction, scoring, sketches
TEST_DATABASE_URL=postgres://... pytest             # + full pipeline and API on a scratch database
python tools/fake_jetstream.py 8765 conn.log 20     # fake Jetstream for testing the collector locally
```

The end-to-end test seeds a breakout, a fading phrase, a spam flood and
background chatter. It checks that:
- the breakout ranks first, with a sensible distinct-author count
- the fade scores negative
- the spam is filtered out
- variants share one ticker
- the API enforces its token

## Next builds

1. **GDELT collector (outer tier).** News headlines every 15 minutes. This
   turns on breadth and the real "Crossing" stage.
2. **Clustering.** Embeddings plus an LLM pass to merge related phrases into
   one asset and name it; brand-account detection for the "Cringe" stage.
3. **Content machine.** Server-rendered share-card PNGs and the Monday
   Weekly job, posting a draft to Telegram for approval.
4. **Thread agent.** On breakout, trace the origin, follow branches, draft
   the trend report.
5. **Terminal wiring.** Point the Upstream pages at this API.
6. **Paid tiers.** A Reddit and short-video data vendor, through licensed
   access only.
