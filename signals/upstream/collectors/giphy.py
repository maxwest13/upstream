"""GIPHY trending GIFs (middle tier). Needs GIPHY_API_KEY.

Titles look like 'Happy Friday GIF by Netflix'; the ' GIF by …' tail is
dropped so only the reaction itself is counted."""
import logging
import re

from .base import Row, clean_text, hash_author, parse_time

log = logging.getLogger("upstream.collectors.giphy")
SOURCE, TIER = "giphy", "middle"
URL = "https://api.giphy.com/v1/gifs/trending"
_TAIL = re.compile(r"\s+(?:GIF|Sticker)(?:\s+by\s+.*)?$", re.I)


def parse_trending(payload: dict, salt: str) -> list[Row]:
    rows = []
    for g in payload.get("data", []):
        gid = g.get("id")
        title = _TAIL.sub("", clean_text(g.get("title", ""), 200)).strip()
        if not gid or not title:
            continue
        ts = g.get("trending_datetime")
        if not ts or str(ts).startswith("0000") or str(ts).startswith("1970"):
            ts = g.get("import_datetime")
        rows.append(Row(
            time=parse_time(ts),
            source=SOURCE, tier=TIER,
            uri=g.get("url") or f"giphy:{gid}",
            author_hash=hash_author(g.get("username") or gid, salt),
            lang="en", text=title,
        ))
    return rows


def collect(cfg, http) -> list[Row]:
    r = http.get(URL, params={"api_key": cfg.giphy_key, "limit": 50, "rating": "pg-13"})
    if r.status_code in (401, 403):
        log.warning("giphy: %s, check GIPHY_API_KEY", r.status_code)
        return []
    r.raise_for_status()
    rows = parse_trending(r.json(), cfg.salt)
    log.info("giphy: %d trending GIFs", len(rows))
    return rows
