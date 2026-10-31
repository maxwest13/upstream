"""Tumblr posts under seed tags (inner tier). Needs TUMBLR_API_KEY
(the OAuth consumer key from a free app registration).

Tumblr has no public firehose, so we sample a set of aesthetic, fashion and
fandom tags. New tags that keep showing up alongside them are what we watch."""
import logging

from .base import Row, clean_text, hash_author, parse_time, tags_field

log = logging.getLogger("upstream.collectors.tumblr")
SOURCE, TIER = "tumblr", "inner"
URL = "https://api.tumblr.com/v2/tagged"


def parse_tagged(payload: dict, salt: str) -> list[Row]:
    rows = []
    for p in payload.get("response", []) or []:
        pid = p.get("id_string") or str(p.get("id", ""))
        blog = p.get("blog_name") or (p.get("blog") or {}).get("name") or pid
        text = clean_text(p.get("summary") or " ".join(
            filter(None, [p.get("title"), p.get("body"), p.get("caption"), p.get("description")])), 600)
        tags = p.get("tags") or []
        if not pid or not (text or tags):
            continue
        rows.append(Row(
            time=parse_time(p.get("timestamp")),
            source=SOURCE, tier=TIER,
            uri=p.get("post_url") or f"tumblr:{pid}",
            author_hash=hash_author(blog, salt),
            lang="en",
            text=text or " ".join(tags)[:600],
            tags=tags_field(tags[:20]),
        ))
    return rows


def collect(cfg, http) -> list[Row]:
    rows = []
    for tag in cfg.tumblr_tags:
        r = http.get(URL, params={"tag": tag, "api_key": cfg.tumblr_key, "filter": "text", "limit": 20})
        if r.status_code in (401, 403):
            log.warning("tumblr: %s, check TUMBLR_API_KEY", r.status_code)
            return rows
        r.raise_for_status()
        rows += parse_tagged(r.json(), cfg.salt)
    log.info("tumblr: %d posts across %d tags", len(rows), len(cfg.tumblr_tags))
    return rows
