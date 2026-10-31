"""Culture-press headlines via RSS (outer tier). No key needed.

Each outlet counts as one 'author', so distinct-author counts measure how many
outlets picked a phrase up, not how many articles one outlet ran."""
import calendar
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import feedparser

from .base import Row, clean_text, hash_author

log = logging.getLogger("upstream.collectors.news")
SOURCE, TIER = "news", "outer"


def load_feeds(path: str) -> list[str]:
    with open(path) as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]


def parse_feed(content: bytes, feed_url: str, salt: str) -> list[Row]:
    parsed = feedparser.parse(content)
    outlet = urlparse(feed_url).netloc.removeprefix("www.").removeprefix("feeds.").removeprefix("rss.")
    rows = []
    now = datetime.now(timezone.utc)
    for e in parsed.entries:
        title = clean_text(e.get("title", ""), 300)
        link = e.get("link") or e.get("id")
        if not title or not link:
            continue
        stamp = e.get("published_parsed") or e.get("updated_parsed")
        ts = datetime.fromtimestamp(calendar.timegm(stamp), tz=timezone.utc) if stamp else now
        if ts > now:
            ts = now  # some feeds stamp the future
        summary = clean_text(e.get("summary", ""), 400)
        text = f"{title}. {summary}" if summary and not summary.startswith(title) else title
        rows.append(Row(ts, SOURCE, TIER, link, hash_author(outlet, salt), "en", text))
    return rows


def collect(cfg, http) -> list[Row]:
    rows, ok, failed = [], 0, []
    for url in load_feeds(cfg.feeds_file):
        try:
            r = http.get(url, follow_redirects=True)
            r.raise_for_status()
            rows += parse_feed(r.content, url, cfg.salt)
            ok += 1
        except Exception as e:  # one dead feed never stops the rest
            failed.append(f"{url} ({type(e).__name__})")
    if failed:
        log.warning("news: %d feeds failed: %s", len(failed), "; ".join(failed[:5]))
    log.info("news: %d/%d feeds ok, %d items", ok, ok + len(failed), len(rows))
    return rows
