import hashlib
import html
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import psycopg

USER_AGENT_DEFAULT = "UpstreamCultureBot/0.2 (cultural trend research)"


@dataclass(frozen=True)
class Row:
    time: datetime
    source: str
    tier: str
    uri: str
    author_hash: str
    lang: str
    text: str
    tags: str = ""


@dataclass(frozen=True)
class CollectorConfig:
    salt: str
    contact: str
    youtube_key: str
    youtube_regions: tuple
    youtube_pages: int
    tumblr_key: str
    tumblr_tags: tuple
    giphy_key: str
    twitch_id: str
    twitch_secret: str
    feeds_file: str
    extra: dict = field(default_factory=dict)

    @property
    def user_agent(self) -> str:
        # Wikimedia and several publishers ask bots to identify themselves with a contact.
        return f"{USER_AGENT_DEFAULT} contact: {self.contact}" if self.contact else USER_AGENT_DEFAULT

    @classmethod
    def from_env(cls) -> "CollectorConfig":
        def lst(name, default):
            return tuple(x.strip() for x in os.environ.get(name, default).split(",") if x.strip())

        return cls(
            salt=os.environ["AUTHOR_SALT"],
            contact=os.environ.get("BOT_CONTACT_EMAIL", ""),
            youtube_key=os.environ.get("YOUTUBE_API_KEY", ""),
            youtube_regions=lst("YOUTUBE_REGIONS", "US,GB,CA,AU"),
            youtube_pages=int(os.environ.get("YOUTUBE_PAGES", "2")),
            tumblr_key=os.environ.get("TUMBLR_API_KEY", ""),
            tumblr_tags=lst("TUMBLR_TAGS", "aesthetic,moodboard,outfit,fashion,interior design,home decor,"
                                           "new music,meme,trend,vibes,style,fandom"),
            giphy_key=os.environ.get("GIPHY_API_KEY", ""),
            twitch_id=os.environ.get("TWITCH_CLIENT_ID", ""),
            twitch_secret=os.environ.get("TWITCH_CLIENT_SECRET", ""),
            feeds_file=os.environ.get("FEEDS_FILE", os.path.join(os.path.dirname(__file__), "feeds.txt")),
        )


def hash_author(author_id: str, salt: str) -> str:
    """Same scheme as the Rust collector: first 8 bytes of sha256(salt:id), hex."""
    return hashlib.sha256(f"{salt}:{author_id}".encode()).hexdigest()[:16]


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(s: str, limit: int = 1000) -> str:
    s = html.unescape(_TAG_RE.sub(" ", s or ""))
    return _WS_RE.sub(" ", s).strip()[:limit]


def compact_tag(t: str) -> str:
    """'Analog Weekend' -> 'analogweekend', matching how hashtags are stored."""
    return re.sub(r"[^0-9a-z]", "", (t or "").lower())


def tags_field(tags) -> str:
    out = sorted({c for c in (compact_tag(t) for t in tags or []) if 2 <= len(c) <= 64 and not c.isdigit()})
    return " ".join(out)


def parse_time(value, fallback: datetime | None = None) -> datetime:
    """ISO-8601 strings, 'YYYY-MM-DD HH:MM:SS' (UTC) or epoch seconds -> aware UTC datetime."""
    fallback = fallback or datetime.now(timezone.utc)
    if value in (None, "", 0) or str(value).startswith("0000"):
        return fallback
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        v = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return fallback


@dataclass(frozen=True)
class Snapshot:
    time: datetime
    source: str
    item_id: str
    uri: str
    title: str
    views: int
    likes: int | None = None
    comments: int | None = None


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def store_snapshots(conn: psycopg.Connection, snaps: list[Snapshot]) -> int:
    if not snaps:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO engagement_snapshots (time, source, item_id, uri, title, views, likes, comments) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            [(s.time, s.source, s.item_id, s.uri, s.title, s.views, s.likes, s.comments) for s in snaps],
        )
    return len(snaps)


def store_rows(conn: psycopg.Connection, rows: list[Row]) -> int:
    """Insert, skipping anything already stored. Returns rows actually added."""
    if not rows:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE IF NOT EXISTS raw_stage "
            "(time timestamptz, source text, tier text, uri text, author_hash text, lang text, text text, tags text) "
            "ON COMMIT DELETE ROWS"
        )
        with cur.copy("COPY raw_stage FROM STDIN") as cp:
            for r in rows:
                cp.write_row((r.time, r.source, r.tier, r.uri, r.author_hash, r.lang, r.text, r.tags))
        cur.execute(
            "INSERT INTO raw_posts (time, source, tier, uri, author_hash, lang, text, tags) "
            "SELECT DISTINCT ON (time, uri) time, source, tier, uri, author_hash, lang, text, tags FROM raw_stage "
            "ON CONFLICT (time, uri) DO NOTHING"
        )
        return cur.rowcount
