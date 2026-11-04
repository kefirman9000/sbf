"""Only proven bots decide the account's trades; everything else keeps training on its own pretend money."""
import json

import engine as E
from test_feature_tests import market


def farm(opts=None, n=900):
    closes, extra = market(102, n)
    o = E.opts_for_bars(E.tournament_opts(dict(E.DEFAULT_SETTINGS, **(opts or {}))), 60)
    t = E.Tournament(list(closes), 10000.0, 0.006, 0.1, 24, opts=o, cap=30)
    for i in range(E.WARM, n):
        t.step({c: E.View(closes[c], i + 1) for c in closes}, E.slice_extra(extra, i + 1))
    return t


def make_proven(t, v):
    """Gives a bot the record a proven bot has: trades, age, a lifetime gain over holding and passed exams."""
    v.wins, v.losses, v.age = 8, 4, t.window_for(v.tf) + 1
    v.sim.cash = v.sim.start * 3
    v.eq.append(v.sim.equity(t.last_px))
    v.exams = [dict(k="self-test", edge=5.0 + i, n=4, at=0, when=0, g=60) for i in range(5)]
    t.serial = 2


def test_a_new_bot_is_not_proven():
    t = farm()
    assert not any(t.proven(v) for v in t.vars if v.wins + v.losses < 8)


def test_a_bot_with_a_real_record_is_proven_and_others_are_not():
    t = farm()
    v = next(x for x in t.vars if not E.is_kami(x))
    make_proven(t, v)
    assert t.proven(v)
    v.exams = []  # no exams passed yet
    assert not t.proven(v)
    t.o["proven_exam"] = False
    assert t.proven(v)


def test_only_proven_winners_vote():
    t = farm()
    for v in t.vars:
        v.sig = {c: "buy" for c in t.coins}
    t.active = [v for v in t.vars if not E.is_kami(v)][:3]
    for v in t.active:
        t.info[id(v)].update(status="active", weight=1.0, stake=1.0, cons=1.0)
    c = t.coins[0]
    assert t.vote(c)[0] is None  # nobody proven: the account waits
    make_proven(t, t.active[0])
    assert t.vote(c)[0] == "buy"
    t.o["proven_only"] = False
    t.active[0].exams = []
    assert t.vote(c)[0] == "buy"  # with the gate off, any winner may


def test_kamikazes_never_decide_but_your_team_does():
    t = farm()
    k = next((v for v in t.vars if E.is_kami(v)), None)
    if k is not None:
        make_proven(t, k)
        assert not t.proven(k)
    v = t.vars[0]
    v.origin = "team"
    assert t.proven(v)


def test_old_farms_start_a_fresh_scoreboard_once():
    t = farm()
    v = t.vars[0]
    make_proven(t, v)
    v.best_tr = 46000.0
    st = json.loads(json.dumps(t.to_state()))
    st["scoreboard"] = 1  # saved before 1.23
    t2 = E.Tournament(t.coins, 10000.0, 0.006, 0.1, 24, cap=30)
    assert t2.restore(st) and t2.scoreboard_reset
    assert all(x.wins == x.losses == 0 and not x.exams and x.best_tr is None for x in t2.vars)
    assert [x.key() for x in t2.vars] == [x.key() for x in t.vars]  # genes kept
    t3 = E.Tournament(t.coins, 10000.0, 0.006, 0.1, 24, cap=30)
    assert t3.restore(json.loads(json.dumps(t2.to_state()))) and not t3.scoreboard_reset
