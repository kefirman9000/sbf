"""While no bot is proven, the best few winners may trade the account at a smaller size."""
from test_proving import farm, plain


def _setup():
    t, closes = farm()
    v = plain(t)
    t.proven = lambda x: False  # nobody is proven
    t.active = [v]
    v.wins, v.losses = 3, 1
    t.life = lambda x: dict(ret=0.05, edge=0.02, trades=8, winrate=75.0, maxdd=0.01, fees=0.0)
    t.info[id(v)] = dict(t.info.get(id(v), {}), status="active", weight=1.0, stake=1.0, cons=1.0)
    c = t.coins[0]
    v.sig[c] = "buy"
    return t, v, c


def _vote(t, c):
    try:
        return t.vote(c, breed=None)
    except TypeError:  # (an app whose vote takes no breed)
        return t.vote(c)


def test_standins_trade_at_a_smaller_size():
    t, v, c = _setup()
    assert t.standins() == [v]
    sig, mult = _vote(t, c)
    assert sig == "buy" and t.standin_on
    t.o["standin_size"] = 1.0
    t.cool.clear()
    sig2, mult2 = _vote(t, c)
    assert sig2 == "buy" and abs(mult - 0.5 * mult2) < 1e-9


def test_no_standins_when_switched_off_or_not_qualified():
    t, v, c = _setup()
    t.o["standin_n"] = 0
    assert t.standins() == [] and _vote(t, c)[0] is None
    t.o["standin_n"] = 3
    v.wins, v.losses = 1, 0  # too few finished trades
    assert t.standins() == []
    v.wins = 5
    t.life = lambda x: dict(ret=-0.01, edge=None, trades=8, winrate=50.0, maxdd=0.0, fees=0.0)  # behind over its life
    assert t.standins() == []
