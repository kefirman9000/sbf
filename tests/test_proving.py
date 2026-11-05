"""Proven bots must be reachable: exam trades count toward the trades a bot needs, and bots on course to earn a vote
aren't retired or dropped to make room before they can finish their record."""
import engine as E
from test_feature_tests import market


def farm():
    closes, extra = market(102, 900)
    opts = E.tournament_opts(E.DEFAULT_SETTINGS)
    if hasattr(E, "opts_for_bars"):
        opts = E.opts_for_bars(opts, 60)
    t = E.Tournament(list(closes), 1000.0, 0.006, 0.1, 24, opts=opts, cap=30)
    for i in range(E.WARM, 900):
        t.step({c: E.View(closes[c], i + 1) for c in closes}, E.slice_extra(extra, i + 1))
    return t, closes


def make_contender(t, v):
    for k in range(4):  # four good exams on unseen days, 5 trades each
        E.add_exam(v, "self-test", 1.0 + 0.1 * k, 5, when=f"2025-0{k + 1}-02", gran=60)
    assert E.exam_verdict(v, t.serial)[0] == "passed"


def plain(t):
    return next(v for v in t.vars if not getattr(v, "traits", {}).get("kami") and v.origin != "team")


def test_exam_trades_count_toward_the_trades_a_bot_needs():
    t, _ = farm()
    v = plain(t)
    v.exams = []
    need = int(t.o.get("proven_trades", 8))
    before = next((g for g in t.proven_gaps(v) if "finished trade" in g), None)
    make_contender(t, v)
    assert E.exam_trades(v) == 20
    assert not any("finished trade" in g for g in t.proven_gaps(v)), (before, need)


def test_a_contender_is_not_retired_or_dropped():
    t, closes = farm()
    v = plain(t)
    make_contender(t, v)
    assert t.contender(v) is not None and id(v) in t.protected()
    assert v not in [x for x, _ in t.weakest()]
    v.strikes = 10 ** 6  # would normally be retired at once
    for _ in range(3):
        t.evolve({c: E.View(closes[c], len(closes[c])) for c in closes})
    assert v in t.vars


def test_failing_its_exams_ends_the_protection():
    t, _ = farm()
    v = plain(t)
    v.exams = []
    for k in range(4):
        E.add_exam(v, "self-test", -2.0, 5, when=f"2025-0{k + 1}-02", gran=60)
    assert t.contender(v) is None and id(v) not in t.protected()


def test_protection_is_capped_so_the_farm_keeps_evolving():
    t, _ = farm()
    for v in t.vars:
        if v.origin != "team" and not getattr(v, "traits", {}).get("kami"):
            make_contender(t, v)
    assert len(t.protected()) <= max(1, t.cap // 3)
