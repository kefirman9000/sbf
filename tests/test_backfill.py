"""Holes in the price history (bot stopped, computer asleep, connection down) are filled from Alpaca's history."""
import queue
import time
import types

import pytest

pytest.importorskip("tkinter")
import engine as E  # noqa: E402
import main as M  # noqa: E402


def app_with_hole(minutes_quiet=42, n=200):
    now = time.time()
    T = [now - (n - i) * 60 for i in range(n)]
    full = {c: [100.0 + i * 0.01 + (j * 3) for i in range(n)] for j, c in enumerate(("SPY", "QQQ"))}
    cut = n - minutes_quiet
    a = types.SimpleNamespace(quote="USD", q=queue.Queue(), session_coins=["SPY", "QQQ"], session_bar=1,
                              hist={c: full[c][:cut] for c in full}, ctx_hist={"TLT": [50.0] * cut},
                              hx={c: dict(h=list(full[c][:cut]), l=list(full[c][:cut]), v=[0.0] * cut, t=T[:cut]) for c in full},
                              logs=[])
    a.write = lambda msg, tag=None: a.logs.append(msg)
    a.draw_chart = lambda: None
    return a, full, T


def stub_fetch(monkeypatch, full, T, calls):
    def fetch_range(c, gran, a, b, quote="USD", note=None):
        calls.append(c)
        ii = [i for i, t in enumerate(T) if a <= t <= b]
        return dict(c=[full[c][i] for i in ii], h=[full[c][i] for i in ii], l=[full[c][i] for i in ii],
                    v=[1.0] * len(ii), t=[T[i] for i in ii])
    monkeypatch.setattr(E, "fetch_range", fetch_range)


def test_a_hole_is_filled_once_and_everything_lines_up(monkeypatch):
    a, full, T = app_with_hole()
    calls = []
    stub_fetch(monkeypatch, full, T, calls)
    M.App._find_gap(a, a.session_coins, 1, 60)
    M.App._find_gap(a, a.session_coins, 1, 60)
    assert calls == ["SPY", "QQQ"]  # one fetch per ticker, and none the second time
    kind, data = a.q.get_nowait()
    assert kind == "backfill"
    M.App.on_backfill(a, *data)
    for c in a.session_coins:
        assert len(a.hist[c]) == len(T)  # everything up to the last finished candle
        assert all(len(a.hx[c][k]) == len(a.hist[c]) for k in ("h", "l", "v", "t"))
        assert all(y > x for x, y in zip(a.hx[c]["t"], a.hx[c]["t"][1:]))
    assert len(a.ctx_hist["TLT"]) == len(a.hist["SPY"])
    assert a.logs and "Filled in" in a.logs[-1]


def test_no_hole_no_fetch(monkeypatch):
    a, full, T = app_with_hole(minutes_quiet=1)
    calls = []
    stub_fetch(monkeypatch, full, T, calls)
    M.App._find_gap(a, a.session_coins, 1, 60)
    assert not calls and a.q.empty()
