"""Read API the Upstream terminal and report pages call.

GET /health
GET /v1/movers?direction=up|down|all&limit=50
GET /v1/terms/{term}/series?hours=48      hourly mentions, for candles/sparklines
GET /v1/terms/{term}/examples?limit=10    recent post text, no author data
"""
import hmac
from datetime import datetime, timedelta, timezone

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .config import Settings

app = FastAPI(title="Upstream API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["Authorization"])

_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def auth(authorization: str = Header(default=""), s: Settings = Depends(settings)) -> None:
    expected = f"Bearer {s.api_token}"
    if not s.api_token or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


def db(s: Settings = Depends(settings)):
    with psycopg.connect(s.database_url, autocommit=True) as conn:
        yield conn


@app.get("/health")
def health(conn=Depends(db)):
    row = conn.execute(
        "SELECT (SELECT max(time) FROM raw_posts), (SELECT max(snapshot_at) FROM signals)"
    ).fetchone()
    return {"ok": True, "last_post": row[0], "last_snapshot": row[1]}


@app.get("/v1/sources", dependencies=[Depends(auth)])
def sources(conn=Depends(db)):
    """Is every collector alive? Last item seen and volume, per source."""
    rows = conn.execute(
        "SELECT source, tier, max(ingested_at) AS last_ingested, "
        "count(*) FILTER (WHERE ingested_at > now() - interval '1 hour') AS last_hour, "
        "count(*) AS last_24h "
        "FROM raw_posts WHERE ingested_at > now() - interval '24 hours' AND time > now() - interval '30 days' "
        "GROUP BY source, tier ORDER BY last_24h DESC"
    ).fetchall()
    out = [{"source": s, "tier": t, "last_ingested": li, "items_last_hour": h, "items_24h": d}
           for s, t, li, h, d in rows]
    try:
        w = conn.execute("SELECT max(day), count(*) FROM attention_daily WHERE source = 'wikipedia'").fetchone()
        out.append({"source": "wikipedia", "tier": "outer", "latest_day": w[0], "article_days": w[1]})
    except psycopg.errors.UndefinedTable:
        pass
    return {"sources": out}


@app.get("/v1/attention", dependencies=[Depends(auth)])
def attention(limit: int = Query(50, ge=1, le=500), min_views: int = Query(5000, ge=0), conn=Depends(db)):
    """Wikipedia articles whose views jumped most against their previous week."""
    rows = conn.execute(
        "WITH latest AS (SELECT max(day) AS d FROM attention_daily WHERE source = 'wikipedia'), "
        "cur AS (SELECT term, compact, views FROM attention_daily, latest WHERE source = 'wikipedia' AND day = latest.d), "
        "base AS (SELECT term, avg(views) AS avg_views, count(*) AS days FROM attention_daily, latest "
        "         WHERE source = 'wikipedia' AND day >= latest.d - 7 AND day < latest.d GROUP BY term) "
        "SELECT cur.term, cur.views, base.avg_views, (SELECT d FROM latest) "
        "FROM cur LEFT JOIN base USING (term) WHERE cur.views >= %s "
        "ORDER BY cur.views / greatest(coalesce(base.avg_views, 0), 1000) DESC LIMIT %s",
        (min_views, limit),
    ).fetchall()
    return {"attention": [
        {"term": t, "views": v, "prev_week_avg": round(float(b)) if b is not None else None,
         "ratio": round(v / float(b), 2) if b else None, "day": d}
        for t, v, b, d in rows
    ]}


@app.get("/v1/accelerating", dependencies=[Depends(auth)])
def accelerating(source: str = Query("youtube", pattern="^[a-z]+$"),
                 hours: int = Query(6, ge=1, le=72), min_views: int = Query(10000, ge=0),
                 limit: int = Query(25, ge=1, le=200), conn=Depends(db)):
    """Videos/streams whose views grew fastest over the window, relative to
    their size: a 45k video tripling beats a 2.4M video adding 1%."""
    rows = conn.execute(
        "WITH w AS ("
        "  SELECT item_id, uri, title, time, views, "
        "         first_value(views) OVER (PARTITION BY item_id ORDER BY time) AS v0, "
        "         first_value(time)  OVER (PARTITION BY item_id ORDER BY time) AS t0, "
        "         row_number() OVER (PARTITION BY item_id ORDER BY time DESC) AS rn "
        "  FROM engagement_snapshots WHERE source = %s AND time > now() - make_interval(hours => %s)) "
        "SELECT item_id, uri, title, views, v0, extract(epoch FROM time - t0) / 3600.0 AS hrs "
        "FROM w WHERE rn = 1 AND time > t0 AND views >= %s AND v0 > 0",
        (source, hours, min_views),
    ).fetchall()
    items = []
    for item_id, uri, title, v, v0, hrs in rows:
        hrs = max(float(hrs), 0.25)
        growth = (v - v0) / v0
        items.append({"item_id": item_id, "uri": uri, "title": title, "views": v, "views_at_start": v0,
                      "hours": round(hrs, 2), "growth_pct": round(growth * 100, 1),
                      "growth_pct_per_hour": round(growth * 100 / hrs, 2),
                      "views_per_hour": round((v - v0) / hrs)})
    items.sort(key=lambda x: x["growth_pct_per_hour"], reverse=True)
    return {"source": source, "hours": hours, "items": items[:limit]}


@app.get("/v1/movers", dependencies=[Depends(auth)])
def movers(direction: str = Query("up", pattern="^(up|down|all)$"),
           limit: int = Query(50, ge=1, le=500), conn=Depends(db)):
    order = {"up": "score DESC", "down": "score ASC", "all": "abs(score) DESC"}[direction]
    where = {"up": "velocity > 0", "down": "velocity < 0", "all": "true"}[direction]
    # One row per asset: when variants share a ticker, keep the strongest.
    rows = conn.execute(
        f"SELECT * FROM ("
        f"  SELECT DISTINCT ON (coalesce(ticker, term)) term, ticker, display_name, asset_type, kind, stage, "
        f"         velocity, acceleration, breadth, mentions_12h, mentions_24h, authors_12h, score, snapshot_at "
        f"  FROM latest_signals WHERE {where} "
        f"  ORDER BY coalesce(ticker, term), abs(score) DESC"
        f") a ORDER BY {order} LIMIT %s",
        (limit,),
    ).fetchall()
    keys = ["term", "ticker", "name", "type", "kind", "stage", "velocity", "acceleration", "breadth",
            "mentions_12h", "mentions_24h", "authors_12h", "score", "snapshot_at"]
    return {"movers": [dict(zip(keys, r)) for r in rows]}


@app.get("/v1/terms/{term}/series", dependencies=[Depends(auth)])
def series(term: str, hours: int = Query(48, ge=1, le=24 * 30), conn=Depends(db)):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = conn.execute(
        "SELECT date_trunc('hour', bucket) AS hour, tier, sum(mentions), sum(authors) FROM term_5m "
        "WHERE term = %s AND bucket >= %s GROUP BY 1, 2 ORDER BY 1",
        (term, since),
    ).fetchall()
    return {"term": term, "points": [
        {"hour": h, "tier": t, "mentions": m, "authors": a} for h, t, m, a in rows
    ]}


@app.get("/v1/terms/{term}/examples", dependencies=[Depends(auth)])
def examples(term: str, limit: int = Query(10, ge=1, le=50), conn=Depends(db)):
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    needle = term.lstrip("#")
    rows = conn.execute(
        "SELECT time, source, tier, uri, text FROM raw_posts "
        "WHERE time >= %s AND (text ILIKE %s OR (' ' || tags || ' ') LIKE %s) "
        "ORDER BY time DESC LIMIT %s",
        (since, f"%{needle}%", f"% {needle.lower()} %", limit),
    ).fetchall()
    return {"term": term, "examples": [
        {"time": t, "source": s, "tier": tier, "uri": u, "text": x} for t, s, tier, u, x in rows
    ]}
