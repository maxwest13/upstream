"""Always-on scheduler for the polling collectors.

    python -m upstream.collectors          run forever
    python -m upstream.collectors --once   run every enabled collector once, then exit
    python -m upstream.collectors --list   show which collectors are on and why
"""
import logging
import re
import sys
import time

import httpx
import psycopg

from . import giphy, news, tumblr, twitch, wikipedia, youtube
from .base import CollectorConfig, store_rows

log = logging.getLogger("upstream.collectors")


def plan(cfg: CollectorConfig):
    """(name, every_seconds, enabled, reason_if_off, run(conn, http) -> count)"""
    def rows_job(mod):
        return lambda conn, http: store_rows(conn, mod.collect(cfg, http))

    return [
        ("news", 15 * 60, True, "", rows_job(news)),
        ("wikipedia", 3 * 3600, True, "", lambda conn, http: wikipedia.collect_into(conn, cfg, http)),
        ("youtube", 30 * 60, bool(cfg.youtube_key), "set YOUTUBE_API_KEY",
         lambda conn, http: youtube.collect_into(conn, cfg, http)),
        ("tumblr", 15 * 60, bool(cfg.tumblr_key), "set TUMBLR_API_KEY", rows_job(tumblr)),
        ("giphy", 30 * 60, bool(cfg.giphy_key), "set GIPHY_API_KEY", rows_job(giphy)),
        ("twitch", 15 * 60, bool(cfg.twitch_id and cfg.twitch_secret),
         "set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET",
         lambda conn, http: twitch.collect_into(conn, cfg, http)),
    ]


_SECRET_RE = re.compile(r"((?:key|api_key|client_secret|client_id|access_token)=)[^&\s'\"]+", re.I)


def redact(msg: str) -> str:
    """Error messages can include the request URL, and our URLs carry API keys."""
    return _SECRET_RE.sub(r"\1***", msg)


def due(last_run: dict, name: str, every: int, now: float) -> bool:
    return now - last_run.get(name, float("-inf")) >= every


def run(once: bool = False) -> None:
    import os
    cfg = CollectorConfig.from_env()
    jobs = plan(cfg)
    for name, every, on, why, _ in jobs:
        log.info("%-9s %s", name, f"ON, every {every // 60} min" if on else f"off ({why})")
    last: dict[str, float] = {}
    http = httpx.Client(timeout=20, headers={"User-Agent": cfg.user_agent})
    while True:
        try:
            with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
                while True:
                    now = time.monotonic()
                    for name, every, on, _, job in jobs:
                        if on and due(last, name, every, now):
                            last[name] = now  # set first: a failing source waits its interval, no hammering
                            try:
                                added = job(conn, http)
                                log.info("%s: %d new rows", name, added)
                            except psycopg.Error:
                                raise
                            except Exception as e:
                                log.warning("%s failed: %s: %s", name, type(e).__name__, redact(str(e))[:300])
                    if once:
                        return
                    time.sleep(30)
        except psycopg.Error:
            log.exception("database error; reconnecting in 15s")
            if once:
                raise
            time.sleep(15)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # never log request URLs: they carry API keys
    if "--list" in sys.argv:
        for name, every, on, why, _ in plan(CollectorConfig.from_env()):
            print(f"{name:10} {'ON' if on else 'off'}  {'' if on else why}")
    else:
        run(once="--once" in sys.argv)
