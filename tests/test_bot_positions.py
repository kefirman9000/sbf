"""Bot positions: the bots' own pretend share positions. A practice reset closes them without changing any bot's record,
and a restarted farm still knows its prices (so no position looks worthless after a restart)."""
import json

import engine as E
from test_feature_tests import market


def holding_farm():
    closes, extra = market(102, 1200)
    t = E.Tournament(list(closes), 1000.0, 0.006, 0.1, 24, opts=E.opts_for_bars(E.tournament_opts(E.DEFAULT_SETTINGS), 60),
                     cap=30)
    for i in range(E.WARM, 1200):
        t.step({c: E.View(closes[c], i + 1) for c in closes}, E.slice_extra(extra, i + 1))
        if i > 400 and sum(1 for v in t.vars for q in v.sim.pos.values() if q > 0) >= 3:
            break
    return t


def holdings(t):
    return [(v, c) for v in t.vars for c, q in v.sim.pos.items() if q > 0]


def test_reset_closes_every_position_without_changing_equity_or_record():
    t = holding_farm()
    held = holdings(t)
    assert held, "the test farm should be holding some positions"
    before = {id(v): (round(v.sim.equity(t.last_px), 6), v.ntrades, v.wins, v.losses) for v in t.vars}
    assert t.close_positions() == len(held)
    assert not holdings(t)
    for v in t.vars:
        assert (round(v.sim.equity(t.last_px), 6), v.ntrades, v.wins, v.losses) == before[id(v)]
        assert not v.since and not v.entries


def test_no_price_at_all_gives_back_what_was_paid():
    t = holding_farm()
    v, c = holdings(t)[0]
    others = sum(v.sim.cost[c_] for c_, q in v.sim.pos.items() if q > 0)  # everything this bot holds
    cash = v.sim.cash
    t.last_px = {}
    t.close_positions()
    assert abs(v.sim.cash - (cash + others)) < 1e-6 and not holdings(t)


def test_given_prices_are_used_first():
    t = holding_farm()
    v, c = holdings(t)[0]
    cash = v.sim.cash
    worth_at = v.sim.value_of(c, t.last_px[c] * 1.05)
    t.close_positions({c: t.last_px[c] * 1.05})
    assert v.sim.cash >= cash + worth_at - 1e-6


def test_a_restarted_farm_still_knows_its_prices():
    t = holding_farm()
    t2 = E.Tournament(t.coins, 1000.0, 0.006, 0.1, 24, cap=30)
    assert t2.restore(json.loads(json.dumps(t.to_state())))
    assert t2.last_px == t.last_px and t2.last_px
    v2 = next(v for v in t2.vars for q in v.sim.pos.values() if q > 0)
    c = next(c for c, q in v2.sim.pos.items() if q > 0)
    assert v2.sim.value_of(c, t2.last_px[c]) > 0  # an open position is worth something, not $0


def test_a_restarted_farm_values_its_positions_the_same():
    t = holding_farm()
    t2 = E.Tournament(t.coins, 1000.0, 0.006, 0.1, 24, cap=30)
    t2.restore(json.loads(json.dumps(t.to_state())))
    a = sum(v.sim.value_of(c, t.last_px[c]) for v, c in holdings(t))
    b = sum(v.sim.value_of(c, t2.last_px[c]) for v, c in holdings(t2))
    assert a > 0 and abs(a - b) < 1e-6
