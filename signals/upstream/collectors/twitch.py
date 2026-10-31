"""Top live English Twitch streams (middle tier). Needs TWITCH_CLIENT_ID and
TWITCH_CLIENT_SECRET from a free app registration.

Stream titles plus the game or category name; each stream session is counted
once, at its start time."""
import logging
import time
from datetime import datetime, timezone

from .base import (Row, Snapshot, clean_text, compact_tag, hash_author, parse_time, store_rows,
                   store_snapshots, tags_field)

log = logging.getLogger("upstream.collectors.twitch")
SOURCE, TIER = "twitch", "middle"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
STREAMS_URL = "https://api.twitch.tv/helix/streams"
_token = {"value": None, "expires": 0.0}


def parse_streams(payload: dict, salt: str) -> list[Row]:
    rows = []
    for s in payload.get("data", []):
        sid, title = s.get("id"), clean_text(s.get("title", ""), 300)
        if not sid or not title:
            continue
        game = s.get("game_name") or ""
        tags = list(s.get("tags") or [])
        if game:
            tags.append(game)
        tags = [t for t in tags if compact_tag(t) not in ("english",)]
        rows.append(Row(
            time=parse_time(s.get("started_at")),
            source=SOURCE, tier=TIER,
            uri=f"https://www.twitch.tv/{s.get('user_login') or s.get('user_id')}#{sid}",
            author_hash=hash_author(s.get("user_id") or sid, salt),
            lang=(s.get("language") or "en").lower(),
            text=f"{title} · {game}" if game else title,
            tags=tags_field(tags),
        ))
    return rows


def _app_token(cfg, http) -> str:
    if _token["value"] and time.time() < _token["expires"] - 300:
        return _token["value"]
    r = http.post(TOKEN_URL, params={"client_id": cfg.twitch_id, "client_secret": cfg.twitch_secret,
                                     "grant_type": "client_credentials"})
    r.raise_for_status()
    body = r.json()
    _token.update(value=body["access_token"], expires=time.time() + body.get("expires_in", 3600))
    return _token["value"]


def parse_viewer_snapshots(payload: dict, at: datetime) -> list[Snapshot]:
    """Live viewers per stream: a small stream whose audience is climbing fast
    is the Twitch equivalent of a video taking off."""
    out = []
    for s in payload.get("data", []):
        if s.get("id") and isinstance(s.get("viewer_count"), int):
            out.append(Snapshot(at, SOURCE, s["id"],
                                f"https://www.twitch.tv/{s.get('user_login') or s.get('user_id')}",
                                clean_text(f"{s.get('title', '')} · {s.get('game_name', '')}", 200),
                                s["viewer_count"]))
    return out


def fetch(cfg, http) -> tuple[list[Row], list[Snapshot]]:
    headers = {"Client-ID": cfg.twitch_id, "Authorization": f"Bearer {_app_token(cfg, http)}"}
    rows, snaps, cursor, at = [], [], None, datetime.now(timezone.utc)
    for _ in range(2):  # top 200 live English streams
        params = {"first": 100, "language": "en"}
        if cursor:
            params["after"] = cursor
        r = http.get(STREAMS_URL, params=params, headers=headers)
        if r.status_code == 401:
            _token["value"] = None
            log.warning("twitch: 401, token refreshed next run")
            return rows, snaps
        r.raise_for_status()
        data = r.json()
        rows += parse_streams(data, cfg.salt)
        snaps += parse_viewer_snapshots(data, at)
        cursor = (data.get("pagination") or {}).get("cursor")
        if not cursor:
            break
    log.info("twitch: %d live streams", len(snaps))
    return rows, snaps


def collect_into(conn, cfg, http) -> int:
    rows, snaps = fetch(cfg, http)
    store_snapshots(conn, snaps)
    return store_rows(conn, rows)
