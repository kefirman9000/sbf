"""The account's share trading (main.py), driven without a screen: Practice buys and sells, the account's own
safety exits, scalps flat before the close, live fills through the broker, and the copy to the paper account.
Tkinter is stubbed out, so this runs anywhere (CI included)."""
import sys
import types
from unittest import mock

import pytest


def _stub_tk():
    if "main" in sys.modules:
        return sys.modules["main"]
    try:
        import tkinter  # noqa: F401
        real = True
    except ImportError:
        real = False
    if not real:
        tk = types.ModuleType("tkinter")

        class _W:
            def __init__(self, *a, **k):
                pass

            def __getattr__(self, name):
                return mock.MagicMock()
        tk.Frame = tk.Canvas = tk.Tk = tk.Toplevel = tk.Label = _W
        tk.TclError = Exception
        for sub in ("ttk", "messagebox", "simpledialog", "filedialog", "font"):
            m = mock.MagicMock(name=f"tkinter.{sub}")
            setattr(tk, sub, m)
            sys.modules[f"tkinter.{sub}"] = m
        tk.__getattr__ = lambda name: mock.MagicMock(name=f"tkinter.{name}")
        sys.modules["tkinter"] = tk
    import main
    return main


M = _stub_tk()
E = M.E


class FakeBroker:
    """Stands in for broker.py: fills at the quote, records every order."""
    name, key = "Fake", "fake"

    def __init__(self, held=None, px=100.0):
        self.held_ = dict(held or {})
        self.px = px
        self.bought, self.sold = [], []
        self.open = set()

    def paper(self):
        return True

    def quotes(self, syms):
        return {s: (self.px - 0.01, self.px + 0.01, 0) for s in syms}

    def held(self, sym):
        return self.held_.get(sym, 0.0)

    def buy_shares(self, sym, spend, *a, **k):
        q = spend / (self.px + 0.01)
        self.held_[sym] = self.held_.get(sym, 0) + q
        self.bought.append((sym, spend))
        return q, self.px + 0.01

    def sell_shares(self, sym, qty, urgent=False, *a, **k):
        q = min(qty, self.held_.get(sym, 0))
        self.held_[sym] = self.held_.get(sym, 0) - q
        self.sold.append((sym, q, urgent))
        return q, self.px - 0.01

    def cancel(self, oid):
        self.open.discard(oid)


class Now:
    """Runs executor jobs straight away (so live orders finish inside the test)."""
    def submit(self, fn, *a, **k):
        fn(*a, **k)


def account(cash=1000.0, live=False, **settings):
    app = object.__new__(M.App)
    app.settings = E.merge_settings(dict(settings))
    app.cfg = {}
    app.start_cash = app.cash = cash
    app.pos, app.cost, app.opt, app.high, app.bought_at = {}, {}, {}, {}, {}
    app.hist, app.hx, app.last, app.trades, app.mirror, app.mirror_trades = {}, {}, {}, [], {}, []
    app.pending, app.pending_who, app.pending_detail, app.pending_voters = set(), {}, {}, {}
    app.bot_alloc, app.fees_paid, app.fails, app._pdt_note = {}, 0.0, 0, {}
    app.live, app.client, app.wild, app.tourn, app.running, app.session = live, None, False, None, True, True
    app.executor = Now()
    app.q = mock.MagicMock()
    for name in ("write", "notify", "toast", "alert", "refresh_view", "refresh_trades", "save_state", "cur_tab"):
        setattr(app, name, mock.MagicMock())
    app.acct_name = lambda live=None: "LIVE" if app.live else "PRACTICE"
    app.mirror_on = lambda: False
    app.pdt_blocked = lambda: False
    return app


@pytest.fixture(autouse=True)
def _no_files(monkeypatch):
    monkeypatch.setattr(E, "append_trade_csv", lambda row: None)


def test_a_practice_buy_spends_the_trade_size_on_shares_including_parts_of_one():
    a = account(trade_pct=10, slippage=0.0, fee_pct=0.0)
    a.last["SPY"] = 650.0
    a.buy("SPY", 650.0)
    assert a.cost["SPY"] == pytest.approx(100.0)
    assert a.pos["SPY"] == pytest.approx(100 / 650)  # part of one share
    assert a.cash == pytest.approx(900.0)
    assert a.trades[-1]["coin"] == "SPY" and a.trades[-1]["side"] == "buy"


def test_holding_a_stock_means_a_second_buy_signal_does_nothing():
    a = account()
    a.last["SPY"] = 100.0
    a.buy("SPY", 100.0)
    spent = a.cost["SPY"]
    a.buy("SPY", 100.0)
    assert a.cost["SPY"] == spent and len(a.trades) == 1


def test_a_sell_signal_with_nothing_held_never_sells_short():
    a = account()
    a.last["SPY"] = 100.0
    a.sell_signal("SPY", 100.0)
    assert not a.trades and not any(a.pos.values())


def test_a_round_trip_pays_the_spread_and_fees_and_books_the_profit():
    a = account(trade_pct=10, slippage=0.1, fee_pct=0.0)
    a.last["SPY"] = 100.0
    a.buy("SPY", 100.0)
    a.last["SPY"] = 110.0
    a.sell_signal("SPY", 110.0)
    sold = a.trades[-1]
    assert sold["side"] == "sell" and a.pos["SPY"] == 0
    # bought at 100.1, sold at 109.89: about +9.8% on $100
    assert float(sold["pl"]) == pytest.approx(100 / 100.1 * 109.89 - 100, abs=0.01)
    assert a.cash == pytest.approx(1000 + float(sold["pl"]), abs=0.01)


def test_the_safety_stop_sells_on_the_share_price():
    a = account(stop_loss=5, take_profit=0, trail_stop=0, bot_stops=False)
    a.last["SPY"] = 100.0
    a.buy("SPY", 100.0)
    a.last["SPY"] = 96.0
    a.risk_checks()
    assert a.pos["SPY"] > 0  # -4%: still held
    a.last["SPY"] = 94.0
    a.risk_checks()
    assert a.pos["SPY"] == 0 and a.trades[-1]["reason"] == "stop-loss"


def test_lock_in_profit_and_trailing_stop():
    a = account(stop_loss=0, take_profit=10, trail_stop=0, bot_stops=False)
    a.last["QQQ"] = 100.0
    a.buy("QQQ", 100.0)
    a.last["QQQ"] = 111.0
    a.risk_checks()
    assert a.trades[-1]["reason"] == "take-profit"
    b = account(stop_loss=0, take_profit=0, trail_stop=3, bot_stops=False)
    b.last["QQQ"] = 100.0
    b.buy("QQQ", 100.0)
    b.last["QQQ"] = 120.0
    b.risk_checks()
    b.last["QQQ"] = 116.0  # 3.3% off its high
    b.risk_checks()
    assert b.trades[-1]["reason"] == "trailing-stop"


def test_a_scalp_uses_the_scalpers_own_tight_exits(monkeypatch):
    monkeypatch.setattr(E, "minutes_to_close", lambda ts: 200)
    a = account(stop_loss=50, take_profit=100, bot_stops=False)
    a.last["SPY"] = 100.0
    k = M.scalp_key("SPY")
    a.buy(k, 100.0, genes={"_scalp": dict(sl=0.3, tp=0.5, mins=30, flatby=15)})
    assert a.opt[k]["scalp"]["tp"] == 0.5
    a.last["SPY"] = 100.6
    a.risk_checks()
    assert a.pos[k] == 0 and a.trades[-1]["reason"] == "take-profit"
    assert a.trades[-1]["coin"] == "SPY (scalp)"


def test_scalps_are_flat_before_the_close(monkeypatch):
    mins = {"v": 200}
    monkeypatch.setattr(E, "minutes_to_close", lambda ts: mins["v"])
    a = account()
    a.last["SPY"] = 100.0
    k = M.scalp_key("SPY")
    a.buy(k, 100.0, genes={"_scalp": dict(sl=1, tp=2, mins=600, flatby=15)})
    mins["v"] = 10
    a.risk_checks()
    assert a.pos[k] == 0 and a.trades[-1]["reason"] == "scalp-flat"


def test_the_long_term_book_ignores_scalp_genes():
    a = account()
    a.last["SPY"] = 100.0
    a.buy(M.scalp_key("SPY"), 100.0, genes={})  # a scalp book needs the scalpers' vote
    assert not a.trades


def test_live_buys_and_sells_go_through_the_broker_and_settle_on_the_fill(monkeypatch):
    fb = FakeBroker(px=200.0)
    monkeypatch.setattr(M.B, "get", lambda key="alpaca": fb)
    a = account(live=True, trade_pct=10)
    a.last["SPY"] = 200.0
    a.buy("SPY", 200.0)
    assert fb.bought == [("SPY", 100.0)]
    kind, data = a.q.put.call_args[0][0]
    assert kind == "fill"
    a.on_fill(*data)
    assert a.pos["SPY"] == pytest.approx(100 / 200.01) and a.cash == pytest.approx(900.0, abs=0.01)
    a.sell("SPY", 200.0, "stop-loss")
    assert fb.sold and fb.sold[0][2] is True  # a stop sells at market
    kind, data = a.q.put.call_args[0][0]
    a.on_fill(*data)
    assert a.pos["SPY"] == 0 and a.trades[-1]["side"] == "sell"


def test_a_live_buy_skips_a_stock_with_a_wide_spread(monkeypatch):
    fb = FakeBroker(px=10.0)
    fb.quotes = lambda syms: {s: (9.0, 11.0, 0) for s in syms}  # a 20% gap
    monkeypatch.setattr(M.B, "get", lambda key="alpaca": fb)
    a = account(live=True, max_spread_pct=0.5)
    a.last["XYZ"] = 10.0
    a.buy("XYZ", 10.0)
    kind, data = a.q.put.call_args[0][0]
    assert kind == "fail" and "bid/ask gap" in data[3] and not fb.bought


def test_both_mode_copies_the_practice_trade_to_the_paper_account(monkeypatch):
    fb = FakeBroker(px=50.0)
    monkeypatch.setattr(M.B, "get", lambda key="alpaca": fb)
    a = account(trade_pct=10)
    a.mirror_on = a.is_paper_key = lambda: True
    a.q = types.SimpleNamespace(put=lambda msg: msg[1]() if msg[0] == "call" else None)
    a.last["IWM"] = 50.0
    a.buy("IWM", 50.0)
    assert fb.bought == [("IWM", pytest.approx(100.0))]
    assert a.mirror["IWM"]["qty"] == pytest.approx(100 / 50.01)
    a.sell_signal("IWM", 50.0)
    assert fb.sold and "IWM" not in a.mirror
    assert [r["side"] for r in a.mirror_trades] == ["buy", "sell"]


def test_moving_together_caps_the_group():
    a = account(trade_pct=40, max_group_pct=50, max_coin_pct=100)
    up = [100 * (1.001 ** i) for i in range(200)]
    a.hist = {"SPY": up, "QQQ": [x * 2 for x in up]}
    a.last = {"SPY": up[-1], "QQQ": up[-1] * 2}
    a.buy("SPY", a.last["SPY"])
    a.buy("QQQ", a.last["QQQ"])
    assert a.cost["SPY"] == pytest.approx(400) and a.cost["QQQ"] == pytest.approx(100)


def test_position_value_is_the_share_price():
    a = account()
    a.last["SPY"] = 100.0
    a.buy("SPY", 100.0)
    a.last["SPY"] = 105.0
    assert a.position_price("SPY") == 105.0
    assert a.equity() == pytest.approx(a.cash + a.pos["SPY"] * 105.0)
