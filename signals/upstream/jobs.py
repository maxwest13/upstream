"""The two recurring jobs: extract (raw posts -> 5-minute term counts) and
signals (term counts -> momentum snapshot, assets and alerts)."""
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import psycopg

from .alerts import send_breakout_alerts
from .config import Settings
from .extract import make_ticker, terms_for_post
from .scoring import (SKETCH_K, Signal, TermWindow, drop_subsumed, estimate_distinct, evaluate,
                      merge_sketches)

log = logging.getLogger("upstream.jobs")

EXTRACT_BATCH = 20_000
MAX_SIGNALS_PER_RUN = 1000


def _bucket(ts: datetime) -> datetime:
    return ts.replace(minute=ts.minute - ts.minute % 5, second=0, microsecond=0)


def extract_job(conn: psycopg.Connection, s: Settings) -> int:
    """Process raw_posts newer than the watermark. Returns posts processed."""
    total = 0
    while True:
        with conn.transaction():
            row = conn.execute("SELECT last_id FROM job_state WHERE job = 'extract' FOR UPDATE").fetchone()
            last_id = row[0] if row else 0
            posts = conn.execute(
                "SELECT id, time, source, tier, author_hash, text, tags FROM raw_posts "
                "WHERE id > %s ORDER BY id LIMIT %s",
                (last_id, EXTRACT_BATCH),
            ).fetchall()
            if not posts:
                if row is None:
                    conn.execute("INSERT INTO job_state (job, last_id) VALUES ('extract', 0) ON CONFLICT DO NOTHING")
                return total

            counts: dict[tuple, int] = defaultdict(int)
            authors: dict[tuple, set] = defaultdict(set)
            hour_authors: dict[tuple, set] = defaultdict(set)
            term_total: dict[str, int] = defaultdict(int)
            kinds: dict[str, str] = {}
            for _id, ts, source, tier, author, text, tags in posts:
                b = _bucket(ts)
                hour = ts.replace(minute=0, second=0, microsecond=0)
                for term, kind in terms_for_post(text, tags or "", s.include_words).items():
                    key = (b, source, tier, term)
                    counts[key] += 1
                    term_total[term] += 1
                    if author:
                        authors[key].add(author)
                        hour_authors[(hour, term)].add(author)
                    kinds[term] = kind

            rows = [
                (b, source, tier, term, kinds[term], n, len(authors[(b, source, tier, term)]))
                for (b, source, tier, term), n in counts.items()
                if n >= s.min_batch_count
            ]
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS term_stage "
                    "(bucket timestamptz, source text, tier text, term text, kind text, mentions int, authors int) "
                    "ON COMMIT DELETE ROWS"
                )
                with cur.copy("COPY term_stage FROM STDIN") as cp:
                    for r in rows:
                        cp.write_row(r)
                cur.execute(
                    "INSERT INTO term_5m (bucket, source, tier, term, kind, mentions, authors) "
                    "SELECT bucket, source, tier, term, kind, mentions, authors FROM term_stage "
                    "ON CONFLICT (bucket, source, term) DO UPDATE SET "
                    "mentions = term_5m.mentions + EXCLUDED.mentions, "
                    "authors = term_5m.authors + EXCLUDED.authors"
                )
                sketches = [
                    (hour, term, sorted(hs)[:SKETCH_K])
                    for (hour, term), hs in hour_authors.items()
                    if term_total[term] >= s.min_batch_count
                ]
                cur.executemany(
                    "INSERT INTO term_sketch_1h (hour, term, hashes) VALUES (%s, %s, %s) "
                    "ON CONFLICT (hour, term) DO UPDATE SET hashes = ARRAY("
                    "  SELECT DISTINCT h FROM unnest(term_sketch_1h.hashes || EXCLUDED.hashes) AS h "
                    f"  ORDER BY h LIMIT {SKETCH_K})",
                    sketches,
                )
            new_last = posts[-1][0]
            conn.execute(
                "INSERT INTO job_state (job, last_id, updated_at) VALUES ('extract', %s, now()) "
                "ON CONFLICT (job) DO UPDATE SET last_id = EXCLUDED.last_id, updated_at = now()",
                (new_last,),
            )
        total += len(posts)
        log.info("extract: %d posts -> %d term rows (watermark %d)", len(posts), len(rows), new_last)
        if len(posts) < EXTRACT_BATCH:
            return total


WINDOW_SQL = """
SELECT term, tier, min(kind) AS kind,
  coalesce(sum(mentions) FILTER (WHERE bucket >= %(t)s - interval '12 hours'), 0)                                  AS m12,
  coalesce(sum(mentions) FILTER (WHERE bucket >= %(t)s - interval '36 hours' AND bucket < %(t)s - interval '24 hours'), 0) AS mprev,
  coalesce(sum(mentions) FILTER (WHERE bucket >= %(t)s - interval '24 hours'), 0)                                  AS m24,
  coalesce(sum(authors)  FILTER (WHERE bucket >= %(t)s - interval '12 hours'), 0)                                  AS a12,
  coalesce(sum(mentions) FILTER (WHERE bucket >= %(t)s - interval '6 hours'), 0)                                   AS m6,
  coalesce(sum(mentions) FILTER (WHERE bucket >= %(t)s - interval '12 hours' AND bucket < %(t)s - interval '6 hours'), 0) AS m6prev
FROM term_5m
WHERE bucket >= %(t)s - interval '36 hours' AND bucket < %(t)s
  AND term IN (
    SELECT term FROM term_5m
    WHERE bucket >= %(t)s - interval '24 hours' AND bucket < %(t)s
    GROUP BY term HAVING sum(mentions) >= %(min24)s
  )
GROUP BY term, tier
"""


def load_windows(conn: psycopg.Connection, now: datetime, min24: int) -> list[TermWindow]:
    acc: dict[str, TermWindow] = {}
    for term, tier, kind, m12, mprev, m24, a12, m6, m6prev in conn.execute(
        WINDOW_SQL, {"t": now, "min24": min24}
    ):
        w = acc.get(term)
        if w is None:
            w = acc[term] = TermWindow(term, kind, 0, 0, 0, 0, 0, 0, {})
        w.m12 += m12; w.mprev += mprev; w.m24 += m24; w.a12 += a12; w.m6 += m6; w.m6prev += m6prev
        w.tier_m24[tier] = w.tier_m24.get(tier, 0) + m24

    # Replace summed per-bucket author counts (which overcount) with the
    # merged distinct-author sketch for the last 12 hours.
    if acc:
        sk: dict[str, list] = defaultdict(list)
        for term, hashes in conn.execute(
            "SELECT term, hashes FROM term_sketch_1h "
            "WHERE hour >= %s - interval '12 hours' AND hour < %s AND term = ANY(%s)",
            (now, now, list(acc)),
        ):
            sk[term].append(hashes)
        for term, w in acc.items():
            # can't have more distinct authors than posts
            w.a12 = min(estimate_distinct(merge_sketches(sk.get(term, []))), w.m12)
        _apply_attention_spikes(conn, acc, now)
    return list(acc.values())


WIKI_MIN_VIEWS = 5000
WIKI_SPIKE_RATIO = 2.0


def _apply_attention_spikes(conn, acc: dict, now: datetime) -> None:
    """A Wikipedia article whose views at least doubled against its own
    previous week counts as outer-tier (mainstream) evidence for the matching
    phrase or hashtag. Matching is on letters/digits only, so 'analog weekend',
    '#analogweekend' and the article 'Analog_weekend' all line up."""
    by_compact: dict[str, list] = defaultdict(list)
    for term, w in acc.items():
        c = compact(term)
        if len(c) >= 4:
            by_compact[c].append(w)
    if not by_compact:
        return
    try:
        rows = conn.execute(
            "WITH recent AS ("
            "  SELECT compact, day, sum(views) AS views FROM attention_daily "
            "  WHERE compact = ANY(%s) AND day >= %s::date - 9 AND day <= %s::date "
            "  GROUP BY compact, day), "
            "last AS (SELECT compact, max(day) AS d FROM recent GROUP BY compact) "
            "SELECT r.compact, "
            "  max(r.views) FILTER (WHERE r.day = l.d) AS latest, "
            "  avg(r.views) FILTER (WHERE r.day < l.d) AS base "
            "FROM recent r JOIN last l USING (compact) GROUP BY r.compact",
            (list(by_compact), now, now),
        ).fetchall()
    except psycopg.errors.UndefinedTable:
        return  # attention table not created yet (migration 003 not applied)
    for c, latest, base in rows:
        spiking = latest >= WIKI_MIN_VIEWS and (base is None or latest >= WIKI_SPIKE_RATIO * float(base))
        if spiking:
            for w in by_compact[c]:
                w.tier_m24["outer"] = max(w.tier_m24.get("outer", 0), 10 ** 6)  # clears any outer bar


def signals_job(conn: psycopg.Connection, s: Settings, now: datetime | None = None) -> list[Signal]:
    now = now or datetime.now(timezone.utc)
    now = _bucket(now)
    windows = load_windows(conn, now, s.min_mentions_24h)
    sigs = [
        sig for w in windows
        if (sig := evaluate(w, s.min_mentions_24h, s.min_authors_12h, s.min_tier_mentions_24h,
                            s.max_mentions_per_author)) is not None
    ]
    sigs = drop_subsumed(sigs)
    sigs.sort(key=lambda x: abs(x.score), reverse=True)
    sigs = sigs[:MAX_SIGNALS_PER_RUN]

    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO signals (snapshot_at, term, kind, mentions_12h, mentions_prev, mentions_24h, "
                "authors_12h, velocity, acceleration, breadth, stage, score) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                [(now, x.term, x.kind, x.m12, x.mprev, x.m24, x.a12, x.velocity, x.acceleration,
                  x.breadth, x.stage, x.score) for x in sigs],
            )
        _register_assets(conn, [x for x in sigs if x.stage in ("Seeding", "Coiling", "Crossing")])
    log.info("signals: %d terms evaluated, %d kept at %s", len(windows), len(sigs), now.isoformat())

    send_breakout_alerts(conn, s, sigs)
    return sigs


def compact(term: str) -> str:
    return re.sub(r"[^a-z0-9]", "", term.lower())


def _register_assets(conn: psycopg.Connection, rising: list[Signal]) -> None:
    """Give every newly rising term a ticker. Spelling variants that compact to
    the same letters share the ticker of whichever variant arrived first, and
    the readable phrase form becomes the display name when there is one."""
    if not rising:
        return
    by_compact = {c: t for c, t in conn.execute("SELECT DISTINCT ON (compact) compact, ticker FROM assets")}
    taken = set(by_compact.values())
    known = {r[0] for r in conn.execute("SELECT term FROM assets WHERE term = ANY(%s)", ([x.term for x in rising],))}
    # phrases first, so "analog weekend" names the asset rather than "#analogweekend"
    for x in sorted(rising, key=lambda x: (x.kind == "hashtag", -x.score)):
        if x.term in known:
            continue
        c = compact(x.term)
        if not c:
            continue
        ticker = by_compact.get(c)
        if ticker is None:
            readable = x.term.lstrip("#")
            ticker = make_ticker(readable, taken)
            taken.add(ticker)
            by_compact[c] = ticker
        conn.execute(
            "INSERT INTO assets (term, compact, ticker, display_name, asset_type) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (term) DO NOTHING",
            (x.term, c, ticker, x.term, "Hashtag" if x.kind == "hashtag" else "Phrase"),
        )
        conn.execute(
            "UPDATE assets SET display_name = %s WHERE compact = %s AND display_name LIKE '#%%'",
            (x.term, c),
        ) if x.kind != "hashtag" else None
