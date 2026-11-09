"""Account-wide safety limits and the farm doctor (shared in farmcore.py)."""
import time
from datetime import datetime, timedelta

from farmcore import FarmAppMixin, LIMIT_DEFAULTS


class _Tourn:
    def __init__(self, n, cap, scored=0, n_steps=5000):
        self.vars = [type("V", (), {"name": ("RSI", "MACD", "SMA")[i % 3], "exams": []})() for i in range(n)]
        self.cap, self.n, self._scored = cap, n_steps, set(range(scored))
        self.removed = 0

    def score(self, v):
        return 0.1 if self.vars.index(v) in self._scored else None

    def young(self, v):
        return False

    def proven_bots(self):
        return []

    def trim_to_cap(self):
        k = max(0, len(self.vars) - self.cap)
        del self.vars[self.cap:]
        return k


class App(FarmAppMixin):
    def __init__(self, **st):
        self.settings = dict(LIMIT_DEFAULTS, max_bots=30, proven_only=True, proven_exam=True)
        self.settings.update(st)
        self.pos, self.prices, self.cfg, self.said = {}, {}, {}, []
        self.tourn, self.running, self.last_tick = None, False, None

    def position_price(self, c):
        return self.prices.get(c, 1.0)

    def equity(self):
        return 1000.0

    def write(self, text, tag=None):
        self.said.append(text)

    def save_farm(self):
        pass


def test_max_open_blocks_new_positions_only():
    a = App(max_open=2)
    a.pos = {"SPY": 1, "QQQ~scalp": 2}
    assert a.limit_block("AAPL", 50) and "2 open positions" in a.limit_block("AAPL", 50)
    assert a.limit_block("SPY", 50) is None  # adding to one it holds isn't a new position
    a.settings["max_open"] = 0
    assert a.limit_block("AAPL", 50) is None


def test_losing_streak_pauses_then_lifts():
    a = App(loss_streak=3, streak_pause=10)
    for pl in (-1, -2):
        a.limit_note_close(pl)
    assert a.limit_block("SPY", 10) is None
    a.limit_note_close(5)  # a win resets the count
    for pl in (-1, -1, -1):
        a.limit_note_close(pl)
    why = a.limit_block("SPY", 10)
    assert why and "losing-streak pause" in why
    assert a.limit_blocked("SPY", 10) and any("Safety limit" in x for x in a.said)
    a.lim_state["until"] = time.time() - 1
    assert a.limit_block("SPY", 10) is None


def test_doctor_finds_overfull_and_trims():
    a = App(max_bots=30)
    a.tourn = _Tourn(70, 70, scored=60)
    keys = {f["key"] for f in a.doctor_findings()}
    assert "overfull" in keys
    a.doctor_tick = lambda reschedule=True: None
    a.doctor_fix("trim")
    assert len(a.tourn.vars) == 30 and a.tourn.cap == 30


def test_doctor_warming_stale_and_healthy():
    a = App()
    a.tourn = _Tourn(30, 30, scored=5)
    a.running, a.last_tick = True, datetime.now() - timedelta(minutes=9)
    keys = {f["key"] for f in a.doctor_findings()}
    assert {"warming", "stale", "exams"} <= keys
    a.tourn = _Tourn(30, 30, scored=30, n_steps=100)
    a.last_tick = datetime.now()
    a.running = False
    assert a.doctor_findings() == []


def test_silent_bot_loses_a_gate_and_gets_a_fresh_window():
    from farmcore import loosen_gate, loosen_silent

    class Sim:
        tbars = []

    class V:
        def __init__(self):
            self.traits, self.layers, self.vetoes = {"minvol": 0.8, "confirm": 3}, {"need": 2, "plus": [{}]}, {}
            self.sim, self.age, self.tf, self.label, self.origin = Sim(), 900, 1, "bot", "bred"

    class T:
        n, events = 20, []

        def __init__(self):
            self.vars = [V()]

        def window_for(self, tf):
            return 100

    t = T()
    assert loosen_silent(t) == 1
    v = t.vars[0]
    assert "minvol" not in v.traits and v.age == 0 and v.loosened == 1 and "Loosened" in t.events[-1]
    assert loosen_silent(t) == 0  # young again: it gets a whole window first
    v.age = 900
    seen = [loosen_gate(v) for _ in range(5)]
    assert seen[-1] is None and v.traits == {} and v.layers.get("need") == 1 and not v.layers.get("plus")


def test_paper_fills_teach_practice_its_costs(tmp_path, monkeypatch):
    import sys
    import types
    from farmcore import FillCheckMixin, FILL_MIN

    monkeypatch.setitem(sys.modules, "engine", types.SimpleNamespace(HOME_DIR=str(tmp_path)))

    class A(App, FillCheckMixin):
        pass

    a = A()
    assert a.fill_adjust("buy") == 0.0 and "No paper copies" in a.fill_text()
    for i in range(FILL_MIN):
        a.fill_note_practice("SPY", "buy", 2.00)
        a.fill_note_paper("SPY", "buy", 2.04)  # real buys paid 2% more
        a.fill_note_practice("SPY", "sell", 3.00)
        a.fill_note_paper("SPY", "sell", 3.00 if i % 2 else None)  # half the sells didn't fill
    assert abs(a.fill_adjust("buy") - 0.02) < 1e-9 and a.fill_adjust("sell") == 0.0
    st = a.fill_stats()
    assert st["n"] == 2 * FILL_MIN and abs(st["rate"] - 0.75) < 1e-9
    assert "fills" not in {f["key"] for f in a.doctor_findings()}  # 75% filled: fine
    a.settings["fill_learn"] = False
    assert a.fill_adjust("buy") == 0.0
    b = A()  # a fresh app reads the saved comparison back
    assert b.fill_stats()["n"] == 2 * FILL_MIN


def test_earnings_calendar_guard_and_rule(tmp_path, monkeypatch):
    import sys
    import types
    import farmcore as F

    monkeypatch.setitem(sys.modules, "engine", types.SimpleNamespace(HOME_DIR=str(tmp_path)))
    monkeypatch.setattr(F, "_EARN", {"book": None, "index": None})
    day = {"data": {"rows": [{"symbol": "AAPL", "time": "time-after-hours"}, {"symbol": "MSFT", "time": "time-pre-market"}]}}
    import json as _j
    got = F.fetch_earnings(["2026-11-04"], lambda url: _j.dumps(day), pause=0)
    assert got == 1 and F.earnings_book()["days"]["2026-11-04"] == {"AAPL": "amc", "MSFT": "bmo"}
    mon = F.ny_time("2026-11-02", 12, 0)  # Monday noon, two trading days before
    assert F.earn_days_ahead("AAPL", mon) == 2 and F.earn_days_ahead("AAPL~scalp", mon) == 2
    assert F.earn_days_ahead("MSFT", F.ny_time("2026-11-04", 10, 0)) is None  # already reported that morning
    assert F.earn_days_ahead("NVDA", mon) is None

    class A(App):
        HAS_EARNINGS = True

    a = A(earnings_guard=1)
    assert a.limit_block("AAPL", 10) is None or "earnings" not in a.limit_block("AAPL", 10)
    monkeypatch.setattr(F.time, "time", lambda: F.ny_time("2026-11-04", 11, 0))  # the day it reports
    assert "earnings" in a.limit_block("AAPL", 10)
    a.settings["earnings_guard"] = 0
    assert a.limit_block("AAPL", 10) is None
