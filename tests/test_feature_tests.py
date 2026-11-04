"""Feature tests: Free roam measures what each learning switch is worth by running the same practice farm with and
without it, and only calls a verdict when the evidence is clear."""
import random
from datetime import datetime, timedelta, timezone

import engine as E

SBF = E.KEYRING_SERVICE in ("obf", "sbf")


def rows(diffs_pts):
    """Feature-book rows whose on-minus-off differences are these % points."""
    return [[0.01 + d / 100, 0.01, 0] for d in diffs_pts]


def test_too_few_tests_is_still_testing():
    assert E.feature_verdict(rows([5, 5, 5]))["verdict"] == "testing"
    assert E.feature_verdict([])["verdict"] == "testing"


def test_consistent_gains_help_and_consistent_losses_hurt():
    assert E.feature_verdict(rows([2.0, 2.5, 1.8, 2.2, 2.1]))["verdict"] == "helps"
    assert E.feature_verdict(rows([-2.0, -2.5, -1.8, -2.2, -2.1]))["verdict"] == "hurts"


def test_noise_is_no_clear_effect():
    assert E.feature_verdict(rows([3, -2, 1, -3, 2, -1]))["verdict"] == "no clear effect"


def test_one_lucky_result_is_not_enough():
    v = E.feature_verdict(rows([20, 0.1, -0.3, 0.2]))
    assert v["verdict"] in ("no clear effect", "leans helps")


def test_a_switch_that_never_changes_anything_is_unused():
    assert E.feature_verdict(rows([0, 0, 0, 0, 0]))["verdict"] == "unused"


def test_mean_is_in_percent_points():
    assert E.feature_verdict(rows([1, 2, 3, 2]))["mean"] == 2.0


def test_record_skips_runs_that_could_not_be_judged():
    book = {}
    assert not E.record_feature(book, "x", None, 0.1)
    assert E.record_feature(book, "x", 0.2, 0.1)
    for _ in range(100):
        E.record_feature(book, "x", 0.2, 0.1)
    assert len(book["x"]) == E.FEATURE_KEEP


def test_picks_the_least_tested_and_skips_unused():
    book = {"a": rows([1] * 5), "b": rows([1]), "u": rows([0] * 5)}
    rng = random.Random(0)
    assert {E.pick_feature(["a", "b", "c", "u"], book, rng) for _ in range(20)} == {"c"}
    assert E.pick_feature(["u"], book, rng) == "u"  # nothing else to test: still takes its turn
    assert E.pick_feature([], book, rng) is None


def test_score_ranks_by_training_not_by_exam():
    res = dict(results=[dict(train_edge=0.5, exam_edge=-0.10, trades=5),   # the farm's favourite: did badly on the exam
                        dict(train_edge=0.1, exam_edge=0.30, trades=5),
                        dict(train_edge=0.4, exam_edge=0.00, trades=5),
                        dict(train_edge=0.9, exam_edge=0.50, trades=1)],   # hardly traded: not judged
               )
    assert abs(E.farm_pick_score(res, top=2) - (-0.05)) < 1e-9


def test_score_needs_enough_judged_bots():
    assert E.farm_pick_score(dict(results=[dict(train_edge=1, exam_edge=1, trades=9)])) is None


def test_switches_that_only_act_outside_practice_farms_are_not_candidates():
    S = dict(E.DEFAULT_SETTINGS)
    c = E.feature_candidates(S, [k for k, v in S.items() if v is True])
    assert c and not set(c) & E.FEATURE_SKIP
    assert "crossbreed" in c


def market(seed, n=900):
    """Two made-up tickers with realistic, calm moves, on regular-hours 1-minute candles."""
    rng = random.Random(seed)
    times, d = [], datetime(2025, 3, 3, 14, 30, tzinfo=timezone.utc)
    while len(times) < n:
        if d.weekday() < 5:
            times += [(d + timedelta(minutes=i)).timestamp() for i in range(390)]
        d += timedelta(days=1)
    closes, extra = {}, {}
    for s in (["SPY", "QQQ"] if SBF else ["BTC", "ETH"]):
        p, xs = 100.0, []
        for _ in range(n):
            p *= 1 + rng.gauss(0.00002, 0.0007)
            xs.append(p)
        closes[s] = xs
        extra[s] = dict(h=[x * 1.001 for x in xs], l=[x * 0.999 for x in xs], v=[1000.0] * n, t=times[:n])
    return closes, extra


def make_opts(s):
    o = E.tournament_opts(s)
    return E.opts_for_bars(o, 60) if SBF else o


def test_paired_runs_differ_only_by_the_switch():
    S = dict(E.DEFAULT_SETTINGS)
    closes, extra = market(102)
    kw = dict(cap=20, passes=1, exam_frac=0.25, start_cash=1000.0, fee=0.006, pct=0.1, evolve=24, params=S.get("params"))
    base = E.farm_pick_score(E.train_farm(closes, extra, make_opts(S), seed=2, **kw))
    r = E.feature_test(closes, extra, S, "crossbreed", make_opts, seed=2, **kw)
    assert base is not None and r["on"] == base  # the "with it" run is exactly the farm as it is
    assert r["off"] is not None and r["off"] != r["on"]
    assert E.tournament_opts(S) == E.tournament_opts(dict(E.DEFAULT_SETTINGS))  # shared values put back
