"""Momentum math. Pure functions so they can be tested and tuned offline."""
import math
from dataclasses import dataclass

# Additive prior so a term going from 0 to 3 mentions is not "+inf%".
PRIOR = 5.0
SKETCH_K = 64
_HASH_SPACE = 16 ** 16  # author hashes are 16 hex chars


def merge_sketches(sketches, k: int = SKETCH_K) -> list[str]:
    """Union bottom-k sketches and keep the k smallest hashes."""
    return sorted(set().union(*map(set, sketches)))[:k] if sketches else []


def estimate_distinct(hashes: list[str], k: int = SKETCH_K) -> int:
    """Bottom-k distinct-count estimate. Exact below k."""
    hs = sorted(set(hashes))
    if len(hs) < k:
        return len(hs)
    kth = int(hs[k - 1], 16) + 1
    return round((k - 1) * _HASH_SPACE / kth)


def ratio_change(now: float, before: float, prior: float = PRIOR) -> float:
    return (now + prior) / (before + prior) - 1.0


@dataclass
class TermWindow:
    term: str
    kind: str
    m12: int          # last 12h
    mprev: int        # same 12h one day earlier
    m24: int          # last 24h
    a12: int          # approx distinct authors, last 12h
    m6: int           # last 6h
    m6prev: int       # the 6h before that
    tier_m24: dict    # {tier: mentions in last 24h}


@dataclass
class Signal:
    term: str
    kind: str
    m12: int
    mprev: int
    m24: int
    a12: int
    velocity: float
    acceleration: float
    breadth: int
    stage: str
    score: float


def stage_for(velocity: float, acceleration: float, breadth: int, m24: int, min24: int) -> str:
    """Provisional lifecycle stage from velocity, breadth and volume.

    'Cringe' needs brand-account detection, which arrives with the clustering
    step; until then decline is reported as 'Fading'.
    """
    if velocity >= 0.5:
        if breadth >= 2:
            return "Crossing"
        return "Seeding" if m24 < 5 * min24 else "Coiling"
    if velocity <= -0.25:
        return "Fading"
    if m24 >= 10 * min24 and acceleration <= 0.1:
        return "Peak"
    return "Steady"


def score_for(velocity: float, acceleration: float, a12: int) -> float:
    """Momentum score: rewards breadth of people (not posts), velocity, and
    still-rising acceleration. Negative for declining terms."""
    v = max(-1.0, min(velocity, 10.0))
    boost = 1.0 + 0.5 * max(acceleration, 0.0)
    return math.log1p(a12) * v * boost


def drop_subsumed(sigs: list["Signal"], ratio: float = 0.8) -> list["Signal"]:
    """Drop a longer phrase when a shorter phrase inside it carries at least
    `ratio` of its volume: "trying an analog weekend" is just "analog weekend"
    in a common context, not a separate trend."""
    by_term = {s.term: s for s in sigs if s.kind == "phrase"}
    out = []
    for s in sigs:
        if s.kind == "phrase":
            words = s.term.split()
            subs = [" ".join(words[i:j]) for i in range(len(words))
                    for j in range(i + 2, len(words) + 1) if (j - i) < len(words)]
            if any(sub in by_term and by_term[sub].m12 >= ratio * s.m12 for sub in subs):
                continue
        out.append(s)
    return out


def evaluate(w: TermWindow, min24: int, min_authors: int, min_tier: int,
             max_per_author: float) -> Signal | None:
    if w.m24 < min24 or w.a12 < min_authors:
        return None
    if w.m12 > 0 and w.m12 / max(w.a12, 1) > max_per_author:
        return None  # a few accounts posting the same thing: likely spam or bots
    velocity = ratio_change(w.m12, w.mprev)
    acceleration = ratio_change(w.m6, w.m6prev)
    def bar(tier):
        return min_tier.get(tier, 20) if isinstance(min_tier, dict) else min_tier
    breadth = sum(1 for tier, v in w.tier_m24.items() if v >= bar(tier))
    return Signal(
        term=w.term, kind=w.kind, m12=w.m12, mprev=w.mprev, m24=w.m24, a12=w.a12,
        velocity=velocity, acceleration=acceleration, breadth=breadth,
        stage=stage_for(velocity, acceleration, breadth, w.m24, min24),
        score=score_for(velocity, acceleration, w.a12),
    )
