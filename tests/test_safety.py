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
