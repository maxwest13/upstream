"""Telegram alerts for breakouts. One alert per term, ever, per reason."""
import json
import logging
import urllib.request

log = logging.getLogger("upstream.alerts")
MAX_ALERTS_PER_RUN = 5


def _send(token: str, chat: str, text: str) -> bool:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat, "text": text, "disable_web_page_preview": True}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:  # network trouble must never stop the worker
        log.warning("telegram send failed: %s", e)
        return False


def format_alert(ticker: str, sig) -> str:
    return (
        f"UPSTREAM ▲ {ticker}  {sig.term}\n"
        f"{sig.velocity * 100:+.0f}% vs same 12h yesterday · accel {sig.acceleration * 100:+.0f}%\n"
        f"{sig.m12:,} mentions · ~{sig.a12:,} authors · stage {sig.stage}"
    )


def send_breakout_alerts(conn, s, sigs) -> int:
    if not (s.telegram_token and s.telegram_chat):
        return 0
    candidates = [
        x for x in sigs
        if x.velocity >= s.breakout_velocity and x.a12 >= 2 * s.min_authors_12h
    ][:20]
    if not candidates:
        return 0
    already = {r[0] for r in conn.execute(
        "SELECT term FROM alerts_sent WHERE reason = 'breakout' AND term = ANY(%s)",
        ([x.term for x in candidates],),
    )}
    sent = 0
    for x in candidates:
        if x.term in already or sent >= MAX_ALERTS_PER_RUN:
            continue
        row = conn.execute("SELECT ticker FROM assets WHERE term = %s", (x.term,)).fetchone()
        if _send(s.telegram_token, s.telegram_chat, format_alert(row[0] if row else "NEW", x)):
            conn.execute(
                "INSERT INTO alerts_sent (term, reason) VALUES (%s, 'breakout') ON CONFLICT DO NOTHING",
                (x.term,),
            )
            conn.commit()
            sent += 1
    return sent
