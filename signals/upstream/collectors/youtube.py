"""YouTube trending videos per region (middle tier). Needs YOUTUBE_API_KEY.

videos.list with chart=mostPopular costs 1 quota unit per page, so 4 regions
x 2 pages every 30 minutes is about 380 of the 10,000 free daily units.
Each video is stored once, at its publish time; a phrase's count therefore
means 'how many trending videos use it'."""
import logging
from datetime import datetime, timezone

from .base import (Row, Snapshot, _int_or_none, clean_text, hash_author, parse_time, store_rows,
                   store_snapshots, tags_field)

log = logging.getLogger("upstream.collectors.youtube")
SOURCE, TIER = "youtube", "middle"
URL = "https://www.googleapis.com/youtube/v3/videos"


def parse_videos(payload: dict, salt: str) -> list[Row]:
    rows = []
    for it in payload.get("items", []):
        sn = it.get("snippet", {})
        vid = it.get("id")
        title = clean_text(sn.get("title", ""), 300)
        if not vid or not title:
            continue
        lang = (sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "en").lower()
        if not lang.startswith("en"):
            continue
        desc = clean_text(sn.get("description", ""), 280)
        rows.append(Row(
            time=parse_time(sn.get("publishedAt")),
            source=SOURCE, tier=TIER,
            uri=f"https://www.youtube.com/watch?v={vid}",
            author_hash=hash_author(sn.get("channelId", vid), salt),
            lang="en",
            text=f"{title}. {desc}" if desc else title,
            tags=tags_field(sn.get("tags", [])[:15]),  # creators stuff dozens of tags; keep the first few
        ))
    return rows


def parse_snapshots(payload: dict, at: datetime) -> list[Snapshot]:
    out = []
    for it in payload.get("items", []):
        st, sn, vid = it.get("statistics") or {}, it.get("snippet") or {}, it.get("id")
        views = _int_or_none(st.get("viewCount"))
        if not vid or views is None:
            continue
        out.append(Snapshot(at, SOURCE, vid, f"https://www.youtube.com/watch?v={vid}",
                            clean_text(sn.get("title", ""), 200), views,
                            _int_or_none(st.get("likeCount")), _int_or_none(st.get("commentCount"))))
    return out


def fetch(cfg, http) -> tuple[list[Row], list[Snapshot]]:
    rows, snaps, at = [], [], datetime.now(timezone.utc)
    for region in cfg.youtube_regions:
        token = None
        for _ in range(cfg.youtube_pages):
            # snippet + statistics in one call: still 1 quota unit per page
            params = {"part": "snippet,statistics", "chart": "mostPopular", "regionCode": region,
                      "maxResults": 50, "key": cfg.youtube_key}
            if token:
                params["pageToken"] = token
            r = http.get(URL, params=params)
            if r.status_code == 403:
                log.warning("youtube: 403 for %s (quota or key problem)", region)
                return rows, snaps
            r.raise_for_status()
            data = r.json()
            rows += parse_videos(data, cfg.salt)
            snaps += parse_snapshots(data, at)
            token = data.get("nextPageToken")
            if not token:
                break
    # a video trending in several regions is one video
    snaps = list({s.item_id: s for s in snaps}.values())
    log.info("youtube: %d trending videos across %s", len(snaps), ",".join(cfg.youtube_regions))
    return rows, snaps


def collect_into(conn, cfg, http) -> int:
    rows, snaps = fetch(cfg, http)
    store_snapshots(conn, snaps)
    return store_rows(conn, rows)
