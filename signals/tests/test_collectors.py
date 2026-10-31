"""Collector parsers against realistic payloads, plus storage and signal
integration on a real Postgres when TEST_DATABASE_URL is set."""
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from upstream.collectors import giphy, news, tumblr, twitch, wikipedia, youtube
from upstream.collectors.__main__ import due, plan
from upstream.collectors.base import CollectorConfig, Row, hash_author, parse_time, store_rows, tags_field

SALT = "test-salt-0123456789abcdef"

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Culture</title>
<item><title>Why a generation is logging off for an analog weekend</title>
<link>https://www.example.com/culture/analog-weekend</link>
<description>&lt;p&gt;Phones go in the drawer &amp;amp; the &lt;b&gt;board games&lt;/b&gt; come out.&lt;/p&gt;</description>
<pubDate>Thu, 29 Oct 2026 14:00:00 GMT</pubDate></item>
<item><title></title><link>https://www.example.com/empty</link></item>
<item><title>Gas-station chic hits the runway</title><link>https://www.example.com/style/gas</link></item>
</channel></rss>"""

YT = {"items": [
    {"id": "vid1", "snippet": {"publishedAt": "2026-10-29T18:00:00Z", "channelId": "UCabc",
                               "title": "I tried an ANALOG WEEKEND (no phone for 48 hours)",
                               "description": "Film camera, flip phone, farmers market.\nSubscribe!",
                               "tags": ["analog weekend", "digital detox", "Film Camera"],
                               "defaultAudioLanguage": "en-US"},
     "statistics": {"viewCount": "45000", "likeCount": "3100", "commentCount": "210"}},
    {"id": "vid2", "snippet": {"publishedAt": "2026-10-29T10:00:00Z", "channelId": "UCxyz",
                               "title": "Video en español", "defaultAudioLanguage": "es"},
     "statistics": {"viewCount": "1000"}},
], "nextPageToken": None}

TUMBLR = {"meta": {"status": 200}, "response": [
    {"id": 7301, "id_string": "7301", "blog_name": "fluorescentfeelings", "timestamp": 1793300000,
     "post_url": "https://fluorescentfeelings.tumblr.com/post/7301", "type": "photo",
     "summary": "roller grills at 2am, the most honest light there is",
     "tags": ["gas station chic", "aesthetic", "moodboard", "2026"]},
    {"id": 7302, "blog": {"name": "noblogname"}, "timestamp": 1793300100, "tags": [], "summary": ""},
]}

GIPHY = {"data": [
    {"id": "g1", "title": "Happy Friday GIF by Netflix", "username": "netflix",
     "trending_datetime": "2026-10-30 09:15:02", "url": "https://giphy.com/gifs/g1"},
    {"id": "g2", "title": "Slow Blink Cat Sticker", "username": "", "trending_datetime": "0000-00-00 00:00:00",
     "import_datetime": "2026-10-28 11:00:00", "url": "https://giphy.com/gifs/g2"},
]}

TWITCH = {"data": [
    {"id": "s1", "user_id": "u1", "user_login": "streamer1", "game_name": "Just Chatting",
     "title": "analog weekend speedrun (no phone challenge)", "started_at": "2026-10-30T15:00:00Z",
     "viewer_count": 4200, "language": "en", "tags": ["English", "IRL"]},
], "pagination": {"cursor": None}}

WIKI = {"items": [{"project": "en.wikipedia", "access": "all-access", "year": "2026", "month": "10", "day": "29",
                   "articles": [{"article": "Main_Page", "views": 5000000, "rank": 1},
                                {"article": "Special:Search", "views": 900000, "rank": 2},
                                {"article": "Analog_weekend", "views": 48000, "rank": 3},
                                {"article": "Taylor_Swift", "views": 120000, "rank": 4}]}]}


def test_hash_matches_rust_collector_scheme():
    # Same input as the Rust unit test style: salted sha256, 16 hex chars
    h = hash_author("did:plc:abc123", SALT)
    assert len(h) == 16 and all(c in "0123456789abcdef" for c in h)


def test_tags_compacted_like_hashtags():
    assert tags_field(["Analog Weekend", "film-camera", "2026", "x"]) == "analogweekend filmcamera"


def test_parse_time_variants():
    assert parse_time("2026-10-29T18:00:00Z") == datetime(2026, 10, 29, 18, tzinfo=timezone.utc)
    assert parse_time("2026-10-30 09:15:02").tzinfo is not None
    assert parse_time(1793300000).year == 2026
    fb = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert parse_time("0000-00-00 00:00:00", fb) == fb
    assert parse_time("garbage", fb) == fb


def test_news_feed():
    rows = news.parse_feed(RSS, "https://www.theguardian.com/culture/rss", SALT)
    assert len(rows) == 2  # empty-title item skipped
    r = rows[0]
    assert r.source == "news" and r.tier == "outer"
    assert r.text.startswith("Why a generation is logging off for an analog weekend.")
    assert "<" not in r.text and "&amp;" not in r.text and "board games" in r.text
    assert r.time == datetime(2026, 10, 29, 14, tzinfo=timezone.utc)
    # same outlet = same "author", so distinct authors counts outlets
    assert rows[0].author_hash == rows[1].author_hash == hash_author("theguardian.com", SALT)


def test_feeds_file_is_valid():
    feeds = news.load_feeds(str(Path(news.__file__).with_name("feeds.txt")))
    assert len(feeds) >= 20 and all(f.startswith("https://") for f in feeds)


def test_youtube():
    rows = youtube.parse_videos(YT, SALT)
    assert len(rows) == 1  # Spanish video skipped
    r = rows[0]
    assert r.tier == "middle" and r.uri == "https://www.youtube.com/watch?v=vid1"
    assert "analogweekend" in r.tags.split() and "digitaldetox" in r.tags.split()
    snaps = youtube.parse_snapshots(YT, datetime(2026, 10, 30, tzinfo=timezone.utc))
    assert [(s.item_id, s.views, s.likes) for s in snaps] == [("vid1", 45000, 3100), ("vid2", 1000, None)]


def test_tumblr():
    rows = tumblr.parse_tagged(TUMBLR, SALT)
    assert len(rows) == 1
    assert rows[0].tier == "inner" and "gasstationchic" in rows[0].tags.split()
    assert "2026" not in rows[0].tags.split()


def test_giphy_strips_gif_by_tail():
    rows = giphy.parse_trending(GIPHY, SALT)
    assert [r.text for r in rows] == ["Happy Friday", "Slow Blink Cat"]
    assert rows[1].time == datetime(2026, 10, 28, 11, tzinfo=timezone.utc)  # fell back to import time


def test_twitch():
    rows = twitch.parse_streams(TWITCH, SALT)
    assert rows[0].text == "analog weekend speedrun (no phone challenge) · Just Chatting"
    assert rows[0].tags.split() == ["irl", "justchatting"]  # 'English' dropped
    snaps = twitch.parse_viewer_snapshots(TWITCH, datetime.now(timezone.utc))
    assert snaps[0].views == 4200


def test_wikipedia_filters_noise():
    rows = wikipedia.parse_top(WIKI)
    terms = [t for _, t, _, _ in rows]
    assert terms == ["analog weekend", "taylor swift"]
    assert rows[0][2] == "analogweekend" and rows[0][0] == date(2026, 10, 29)


def test_plan_switches_sources_on_by_key(monkeypatch):
    monkeypatch.setenv("AUTHOR_SALT", SALT)
    for k in ("YOUTUBE_API_KEY", "TUMBLR_API_KEY", "GIPHY_API_KEY", "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    on = {n: o for n, _, o, _, _ in plan(CollectorConfig.from_env())}
    assert on == {"news": True, "wikipedia": True, "youtube": False, "tumblr": False, "giphy": False, "twitch": False}
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    assert {n: o for n, _, o, _, _ in plan(CollectorConfig.from_env())}["youtube"] is True


def test_redact_hides_keys_in_error_messages():
    from upstream.collectors.__main__ import redact
    msg = ("Client error '400 Bad Request' for url 'https://www.googleapis.com/youtube/v3/videos"
           "?part=snippet&chart=mostPopular&key=AIzaSECRET123&regionCode=US'")
    out = redact(msg)
    assert "AIzaSECRET123" not in out and "key=***" in out and "regionCode=US" in out
    assert "s3cr3t" not in redact("https://id.twitch.tv/oauth2/token?client_id=abc&client_secret=s3cr3t")


def test_due():
    assert due({}, "news", 900, 0.0)
    assert not due({"news": 100.0}, "news", 900, 500.0)
    assert due({"news": 100.0}, "news", 900, 1000.0)


# ---------- database tests ----------

URL = os.environ.get("TEST_DATABASE_URL")
db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
ROOT = Path(__file__).resolve().parents[2] / "db"


@pytest.fixture()
def conn():
    import psycopg
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        c.execute((ROOT / "001_schema.sql").read_text())
        c.execute((ROOT / "003_sources.sql").read_text())
        c.execute((ROOT / "003_sources.sql").read_text())  # idempotent
        yield c


@db
def test_store_rows_dedupes(conn):
    rows = news.parse_feed(RSS, "https://www.theguardian.com/culture/rss", SALT)
    assert store_rows(conn, rows) == 2
    assert store_rows(conn, rows + rows) == 0  # replays and in-batch duplicates both skipped


@db
def test_news_plus_wikipedia_make_a_trend_cross(conn):
    """A phrase rising on Bluesky that also lands in culture press and spikes on
    Wikipedia should reach breadth 2+ and the Crossing stage."""
    from upstream.config import Settings
    from upstream.jobs import extract_job, signals_job
    import random
    rng = random.Random(3)
    now = datetime(2026, 10, 30, 13, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(300):  # inner: Bluesky chatter, mostly in the last 12h
        rows.append(Row(now - timedelta(minutes=rng.randint(1, 700)), "bluesky", "inner", f"at://x/{i}",
                        hash_author(f"did:{rng.randint(0, 250)}", SALT), "en",
                        f"{rng.choice(['doing', 'loving', 'planning'])} an analog weekend, {rng.choice(['no phone', 'books', 'hikes'])}"))
    for i, outlet in enumerate(["theguardian.com", "vulture.com", "thecut.com", "npr.org"]):  # outer: press
        rows.append(Row(now - timedelta(hours=2 + i), "news", "outer", f"https://{outlet}/a{i}",
                        hash_author(outlet, SALT), "en", "Inside the analog weekend movement"))
    assert store_rows(conn, rows) == len(rows)
    wikipedia.store(conn, [(date(2026, 10, d), "analog weekend", "analogweekend", v)
                           for d, v in [(22, 900), (23, 1100), (24, 1000), (25, 1300), (26, 1200),
                                        (27, 1500), (28, 2600), (29, 48000)]])
    s = Settings(database_url=URL, min_mentions_24h=40, min_authors_12h=15,
                 min_tier_mentions_24h={"inner": 20, "middle": 5, "outer": 3}, breakout_velocity=1.5,
                 min_batch_count=1, include_words=False, max_mentions_per_author=3.0,
                 telegram_token="", telegram_chat="", api_token="t")
    extract_job(conn, s)
    sig = {x.term: x for x in signals_job(conn, s, now=now)}["analog weekend"]
    assert sig.breadth >= 2 and sig.stage == "Crossing"


@db
def test_accelerating_endpoint(conn, monkeypatch):
    from upstream.collectors.base import Snapshot, store_snapshots
    t = datetime.now(timezone.utc)
    snaps = []
    for k, (big, small) in enumerate([(2_380_000, 12_000), (2_400_000, 45_000)]):
        at = t - timedelta(minutes=30 * (1 - k))
        snaps += [Snapshot(at, "youtube", "A", "u/A", "Huge video", big),
                  Snapshot(at, "youtube", "B", "u/B", "Small video taking off", small)]
    store_snapshots(conn, snaps)
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("API_TOKEN", "tok")
    from fastapi.testclient import TestClient
    import upstream.api as api
    api._settings = None
    c = TestClient(api.app)
    items = c.get("/v1/accelerating?source=youtube", headers={"Authorization": "Bearer tok"}).json()["items"]
    assert [i["item_id"] for i in items] == ["B", "A"]
    assert items[0]["growth_pct"] == 275.0
    src = c.get("/v1/sources", headers={"Authorization": "Bearer tok"}).json()["sources"]
    assert any(x["source"] == "wikipedia" for x in src)
