"""End-to-end against a real Postgres: raw posts -> extract -> signals -> API.

Runs only when TEST_DATABASE_URL points at an empty, disposable database.
"""
import hashlib
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from upstream.config import Settings
from upstream.jobs import extract_job, signals_job

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
SCHEMA = Path(__file__).resolve().parents[2] / "db" / "001_schema.sql"
SOURCES = Path(__file__).resolve().parents[2] / "db" / "003_sources.sql"
NOW = datetime(2026, 10, 30, 13, 0, tzinfo=timezone.utc)

FILLER = ["coffee this morning was great", "watching the game tonight with friends",
          "new music friday playlist is out", "traffic on the freeway again today"]


def settings():
    return Settings(database_url=URL, min_mentions_24h=40, min_authors_12h=15,
                    min_tier_mentions_24h={"inner": 20, "middle": 5, "outer": 3},
                    breakout_velocity=1.5, min_batch_count=1, include_words=False,
                    max_mentions_per_author=3.0, telegram_token="", telegram_chat="", api_token="test-token")


@pytest.fixture()
def conn():
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        c.execute(SCHEMA.read_text())
        c.execute(SOURCES.read_text())
        yield c


def seed(conn):
    rng = random.Random(7)
    rows = []

    def add(ts, text, author, tags=""):
        h = hashlib.sha256(f"did:plc:{author}".encode()).hexdigest()[:16]  # same shape as the collector's
        rows.append((ts, "bluesky", "inner", f"at://{author}/{len(rows)}", h, "en", text, tags))

    # Breakout: almost nothing yesterday, 400 posts from ~300 people in the last 12h, accelerating.
    for i in range(12):
        add(NOW - timedelta(hours=30, minutes=rng.randint(0, 300)), "analog weekend sounds nice", rng.randint(0, 5000))
    openers = ["trying an", "doing an", "first ever", "loving this", "planning an", "my", "full", "another"]
    closers = ["no phone", "just books", "highly recommend", "starting friday", "wish me luck", "so good"]
    for i in range(400):
        mins = int(rng.triangular(0, 720, 0))  # skewed toward now
        add(NOW - timedelta(minutes=mins + 1),
            f"{rng.choice(openers)} analog weekend, {rng.choice(closers)}",
            rng.randint(0, 300), "analogweekend" if rng.random() < 0.6 else "")
    # Fading: big yesterday, small today.
    for i in range(300):
        add(NOW - timedelta(hours=24, minutes=rng.randint(1, 719)), "no notes honestly", rng.randint(0, 3000))
    for i in range(60):
        add(NOW - timedelta(minutes=rng.randint(1, 719)), "no notes honestly", rng.randint(0, 3000))
    # Spam: one handful of accounts flooding.
    for i in range(300):
        add(NOW - timedelta(minutes=rng.randint(1, 719)), "free crypto giveaway", rng.randint(0, 4))
    # Background chatter, flat.
    for i in range(1500):
        add(NOW - timedelta(minutes=rng.randint(1, 36 * 60)), rng.choice(FILLER), rng.randint(0, 9000))

    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO raw_posts (time, source, tier, uri, author_hash, lang, text, tags) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", rows)
    return len(rows)


def test_pipeline_end_to_end(conn, monkeypatch):
    n = seed(conn)
    s = settings()
    assert extract_job(conn, s) == n
    assert extract_job(conn, s) == 0  # watermark holds; nothing reprocessed

    sigs = signals_job(conn, s, now=NOW)
    by_term = {x.term: x for x in sigs}

    top = max(sigs, key=lambda x: x.score)
    assert top.term in ("analog weekend", "#analogweekend")
    aw = by_term["analog weekend"]
    assert aw.velocity > 5 and aw.stage in ("Seeding", "Coiling") and aw.acceleration > 0
    # ~300 real people posted it; the sketch should land near that, not the 400 post count
    assert 220 <= aw.a12 <= 380, aw.a12

    assert by_term["no notes"].stage == "Fading" and by_term["no notes"].score < 0
    assert "crypto giveaway" not in by_term  # spam filter
    assert "music friday" not in by_term or abs(by_term["music friday"].velocity) < 0.5

    tickers = dict(conn.execute("SELECT term, ticker FROM assets WHERE compact = 'analogweekend'").fetchall())
    assert tickers == {"analog weekend": "ANLGWKND", "#analogweekend": "ANLGWKND"}
    names = {r[0] for r in conn.execute("SELECT display_name FROM assets WHERE compact = 'analogweekend'")}
    assert "analog weekend" in names

    # API on top of the same database
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("API_TOKEN", "test-token")
    from fastapi.testclient import TestClient
    import upstream.api as api
    api._settings = None
    client = TestClient(api.app)
    assert client.get("/v1/movers").status_code == 401
    h = {"Authorization": "Bearer test-token"}
    movers = client.get("/v1/movers?limit=5", headers=h).json()["movers"]
    assert movers[0]["term"] in ("analog weekend", "#analogweekend")
    assert [m["ticker"] for m in movers].count("ANLGWKND") == 1  # variants collapsed
    down = client.get("/v1/movers?direction=down", headers=h).json()["movers"]
    assert down[0]["term"] == "no notes"
    ex = client.get("/v1/terms/analog weekend/examples", headers=h).json()["examples"]
    assert ex and all("author" not in e for e in ex)
