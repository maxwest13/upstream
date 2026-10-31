import hashlib
import random

from upstream.scoring import TermWindow, estimate_distinct, evaluate, merge_sketches, ratio_change, stage_for


def _h(i):
    return hashlib.sha256(f"author-{i}".encode()).hexdigest()[:16]


def test_sketch_exact_below_k():
    assert estimate_distinct([_h(i) for i in range(40)] * 3) == 40


def test_sketch_estimate_within_25pct():
    for n in (500, 5000, 50000):
        est = estimate_distinct(sorted(_h(i) for i in range(n))[:64])
        assert abs(est - n) / n < 0.25, (n, est)


def test_sketches_merge_without_double_counting():
    rng = random.Random(1)
    people = [_h(i) for i in range(3000)]
    hours = [rng.sample(people, 800) for _ in range(12)]  # same people across hours
    merged = merge_sketches([sorted(h)[:64] for h in hours])
    est = estimate_distinct(merged)
    truth = len(set().union(*map(set, hours)))
    assert abs(est - truth) / truth < 0.25


def w(**kw):
    base = dict(term="t", kind="phrase", m12=0, mprev=0, m24=0, a12=0, m6=0, m6prev=0, tier_m24={"inner": 0})
    base.update(kw)
    return TermWindow(**base)


def test_drop_subsumed_keeps_core_phrase():
    from upstream.scoring import Signal, drop_subsumed
    mk = lambda term, m12: Signal(term, "phrase", m12, 0, m12, 50, 2.0, 0.5, 1, "Seeding", 1.0)
    out = {s.term for s in drop_subsumed([mk("analog weekend", 400), mk("doing an analog weekend", 60),
                                          mk("analog weekend vibes", 380), mk("weekend plans", 90)])}
    # both longer phrases are "analog weekend" in context; "weekend plans" is its own thing
    assert out == {"analog weekend", "weekend plans"}


def test_ratio_change_prior_damps_tiny_bases():
    assert abs(ratio_change(0, 0)) < 1e-9
    assert ratio_change(3, 0) < 1.0
    assert ratio_change(200, 20) > 7


def test_breakout_is_seeding_then_coiling_then_crossing():
    assert stage_for(2.0, 0.5, 1, 100, 40) == "Seeding"
    assert stage_for(2.0, 0.5, 1, 1000, 40) == "Coiling"
    assert stage_for(2.0, 0.5, 2, 1000, 40) == "Crossing"


def test_decline_and_peak():
    assert stage_for(-0.5, -0.3, 1, 1000, 40) == "Fading"
    assert stage_for(0.1, 0.0, 1, 1000, 40) == "Peak"
    assert stage_for(0.1, 0.0, 1, 100, 40) == "Steady"


def test_filters_low_volume_and_few_authors():
    assert evaluate(w(m12=30, m24=30, a12=30), 40, 15, 20, 3) is None
    assert evaluate(w(m12=100, m24=100, a12=10), 40, 15, 20, 3) is None


def test_filters_spammy_concentration():
    # 400 posts from 20 accounts = 20 per author: likely bots
    assert evaluate(w(m12=400, m24=400, a12=20), 40, 15, 20, 3) is None


def test_rising_term_scores_positive_and_falling_negative():
    up = evaluate(w(m12=300, mprev=20, m24=320, a12=200, m6=220, m6prev=80, tier_m24={"inner": 320}), 40, 15, 20, 3)
    down = evaluate(w(m12=50, mprev=300, m24=200, a12=40, m6=15, m6prev=35, tier_m24={"inner": 200}), 40, 15, 20, 3)
    assert up.score > 0 and up.velocity > 5
    assert down.score < 0 and down.stage == "Fading"
