"""Always-on worker: extract every minute, signals every 10 minutes.

Run:  python -m upstream.worker          (forever)
      python -m upstream.worker --once   (one pass of each, then exit)
"""
import logging
import sys
import time

import psycopg

from .config import Settings
from .jobs import extract_job, signals_job

EXTRACT_EVERY = 60
SIGNALS_EVERY = 600

log = logging.getLogger("upstream.worker")


def run(once: bool = False) -> None:
    s = Settings.from_env()
    last_signals = 0.0
    while True:
        try:
            # autocommit: each job opens its own explicit transaction blocks
            with psycopg.connect(s.database_url, autocommit=True) as conn:
                while True:
                    extract_job(conn, s)
                    if once or time.monotonic() - last_signals >= SIGNALS_EVERY:
                        signals_job(conn, s)
                        last_signals = time.monotonic()
                    if once:
                        return
                    time.sleep(EXTRACT_EVERY)
        except Exception:
            log.exception("worker pass failed; reconnecting in 15s")
            if once:
                raise
            time.sleep(15)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run(once="--once" in sys.argv)
