"""English Wikipedia's 1,000 most-viewed articles per day (outer tier,
attention). No key needed; Wikimedia asks for a contact in the User-Agent,
set with BOT_CONTACT_EMAIL.

This is attention, not posts, so it goes in its own table. The signal engine
uses a spike here as evidence that a rising phrase has reached the mainstream."""
import logging
import re
from datetime import date, datetime, timedelta, timezone

import psycopg

log = logging.getLogger("upstream.collectors.wikipedia")
SOURCE = "wikipedia"
URL = "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/{y}/{m:02d}/{d:02d}"
_SKIP = {"main_page", "-", "undefined"}


def compact(s: str) -> str:
    return re.sub(r"[^0-9a-z]", "", s.lower())


def parse_top(payload: dict) -> list[tuple[date, str, str, int]]:
    out = []
    for item in payload.get("items", []):
        day = date(int(item["year"]), int(item["month"]), int(item["day"]))
        for a in item.get("articles", []):
            title = a.get("article", "")
            if not title or title.lower() in _SKIP or ":" in title:  # Special:, File:, Wikipedia: ...
                continue
            term = title.replace("_", " ").lower()
            out.append((day, term, compact(term), int(a.get("views", 0))))
    return out


def store(conn: psycopg.Connection, rows) -> int:
    if not rows:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO attention_daily (day, source, term, compact, views) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (day, source, term) DO UPDATE SET views = EXCLUDED.views",
            [(d, SOURCE, t, c, v) for d, t, c, v in rows],
        )
    return len(rows)


def collect_into(conn, cfg, http) -> int:
    """Fetch the last two complete days (a day publishes a few hours after it ends)."""
    stored = 0
    today = datetime.now(timezone.utc).date()
    for back in (1, 2):
        d = today - timedelta(days=back)
        r = http.get(URL.format(y=d.year, m=d.month, d=d.day), headers={"User-Agent": cfg.user_agent})
        if r.status_code == 404:
            continue  # not published yet
        r.raise_for_status()
        stored += store(conn, parse_top(r.json()))
    log.info("wikipedia: %d article-days stored", stored)
    return stored
