"""Instinct record: each instinct builds a lasting record on the farm, so 'Main instincts' in Farm knowledge grows
as the farm learns instead of only counting who happens to be winning right now."""
import json

import engine as E
from test_feature_tests import market


def stepped_farm(n=1500, seed=5):
    closes, extra = market(seed, n)
    t = E.Tournament(list(closes), 1000.0, 0.006, 0.1, 24, opts=E.opts_for_bars(E.tournament_opts(E.DEFAULT_SETTINGS), 60),
                     cap=30)
    for i in range(E.WARM, n):
        t.step({c: E.View(closes[c], i + 1) for c in closes}, E.slice_extra(extra, i + 1))
    return t


def test_record_builds_up_and_survives_a_save():
    t = stepped_farm()
    assert t.instincts and max(r[1] for r in t.instincts.values()) >= E.INSTINCT_MIN
    assert set(t.instinct_best) <= set(t.instincts)
    t2 = E.Tournament(t.coins, 1000.0, 0.006, 0.1, 24, cap=30)
    assert t2.restore(json.loads(json.dumps(t.to_state())))
    assert t2.instincts == t.instincts and t2.instinct_best == t.instinct_best


def test_record_follows_reviews_slowly():
    t = E.Tournament(["SPY"], 1000.0, 0.006, 0.1, 24, cap=10)
    v = next(v for v in t.vars if not E.is_kami(v) and not E.is_scalp(v.name))
    t.instincts = {v.name: [0.10, 50, 0.2]}
    t.hold_ret = 0.0
    t.info = {id(x): dict(score=None) for x in t.vars}
    t.info[id(v)] = dict(score=-0.10)
    t.instinct_tick()
    for x in t.vars:  # only this family is scored this review
        if x.name == v.name:
            t.info[id(x)] = dict(score=-0.10)
    t.instinct_tick()
    assert abs(t.instincts[v.name][0] - (0.10 - 0.02 - 0.018)) < 1e-9  # each bad review moves it a tenth of the way
    assert t.instincts[v.name][1] == 52


def test_proven_needs_enough_reviews_and_a_positive_record():
    t = E.Tournament(["SPY"], 1000.0, 0.006, 0.1, 24, cap=10)
    t.instincts = {"a": [0.02, E.INSTINCT_PROVEN, 0.1], "b": [0.02, E.INSTINCT_PROVEN - 1, 0.1],
                   "c": [-0.01, 40, 0.1]}
    assert t.instinct_proven("a") and not t.instinct_proven("b") and not t.instinct_proven("c")


def test_knowledge_keeps_an_instinct_after_its_family_is_gone():
    t = E.Tournament(["SPY"], 1000.0, 0.006, 0.1, 24, cap=10)
    gone = next(n for n in E.WILDCARD_STRATS if all(v.name != n for v in t.vars))
    t.instincts = {gone: [0.03, 20, 0.08]}
    t.instinct_best = {gone: E._genes(t.vars[0])}
    it = next(x for x in E.farm_knowledge(t) if x["id"] == f"instinct:{gone}")
    assert it["strong"] and "no bots on the farm now" in it["evidence"] and it["data"]["genes"]


def test_practice_farm_records_pass_on_at_half_weight():
    src = E.Tournament(["SPY"], 1000.0, 0.006, 0.1, 24, cap=10)
    dst = E.Tournament(["SPY"], 1000.0, 0.006, 0.1, 24, cap=10)
    src.instincts = {"SMA crossover": [0.04, 20, 0.1]}
    src.instinct_best = {"SMA crossover": E._genes(src.vars[0])}
    dst.instincts = {"SMA crossover": [0.00, 20, 0.05]}
    E.teach_from(dst, src)
    assert 0.0 < dst.instincts["SMA crossover"][0] < 0.04 and dst.instinct_best["SMA crossover"]


def test_kamikazes_past_their_share_lay_no_eggs():
    """A lucky kamikaze becomes a 'winner'; its eggs used to be culled at once, wasting the farm's breeding."""
    closes, extra = market(7, 400)
    t = E.Tournament(list(closes), 1000.0, 0.006, 0.1, 24, opts=E.opts_for_bars(E.tournament_opts(E.DEFAULT_SETTINGS), 60),
                     cap=30)
    t.cap = len(t.vars) + 10  # room for its kamikazes (a fresh farm is full of eggs too young to drop)
    for _ in range(20):
        if t.kami_full():
            break
        t.spawn_kami(closes)
    assert t.kami_full()
    kami = t.kamikazes()[0]
    t.active = [kami]  # the only winner is a kamikaze
    t.info = {id(v): dict(score=0.1, weight=1.0, status="active" if v is kami else "standby", cons=1.0) for v in t.vars}
    before = len(t.kamikazes())
    for _ in range(10):
        t._spawn(closes)
    assert len(t.kamikazes()) <= before
