"""Shared core of the bot farm apps (CBBF, OBF, SBF): code that is identical in all three.

This file is the same in every app. Change it in one app, then run tools/sync_core.py to copy it to the others
(tests/test_core.py fails in any app whose copy differs from the recorded fingerprint).
"""
import csv
import json
import math
import threading
import os
import pickle
import re
import shutil
import time
from datetime import datetime, timedelta, timezone


WARM = 30  # bars skipped at the start of a backtest so indicators have data


TIMEFRAMES = (1, 2, 3, 5, 10, 15, 30, 60)  # how many app bars a bot waits between decisions (x the Bar length setting)


BAR_CHOICES = (5 / 60, 10 / 60, 0.25, 0.5, 1, 5, 15, 60)  # decision speed in minutes: 5 s, 10 s, 15 s, 30 s, 1-60 min


CANDLE_GRANS = (60, 300, 900, 3600, 21600, 86400)  # what Coinbase's candle endpoint offers (seconds)


LOOKBACK = 300  # most bars any strategy looks back; longer history is trimmed for speed


class View:
    """A read-only window onto the first `end` items of a list, without copying it. Bots read price history
    through these, which keeps replays and backtests fast even with hundreds of bots and long histories."""
    __slots__ = ("seq", "end")

    def __init__(self, seq, end):
        if isinstance(seq, View):
            end, seq = min(end, seq.end), seq.seq
        self.seq, self.end = seq, max(0, min(end, len(seq)))

    def __len__(self):
        return self.end

    def __getitem__(self, k):
        if isinstance(k, slice):
            a, b, s = k.indices(self.end)
            return self.seq[a:b:s]
        if k < 0:
            k += self.end
        if not 0 <= k < self.end:
            raise IndexError("view index out of range")
        return self.seq[k]

    def __iter__(self):
        return iter(self.seq[:self.end])


def fmt_bar(minutes):
    """'15 sec', '1 min', '1 hr' ..."""
    m = float(minutes)
    if m < 1:
        s = round(m * 60, 1)
        return f"{s:g} sec"
    if m >= 60 and m % 60 == 0:
        return f"{m / 60:g} hr"
    return f"{m:g} min"


def tail_series(p, tf=1, keep=LOOKBACK):
    """The last `keep` closes of tf-bar candles ending at the newest bar (cheap: never copies the whole history)."""
    if tf <= 1:
        return p[-keep:]
    last = len(p) - 1
    if last < 0:
        return []
    start = last - tf * min(keep - 1, last // tf)
    return p[start::tf]


_TX_CACHE = {}


def tail_extra(p, x, tf=1, keep=LOOKBACK):
    """Highs, lows and volumes matching tail_series(p, tf, keep): each tf-bar candle gets the highest
    high, lowest low and summed volume of its bars. None when the extra data doesn't line up.
    Many bots ask for the same summary on the same bar, so it's worked out once and shared (read-only)."""
    if not x or len(x.get("h", ())) != len(p) or len(x.get("l", ())) != len(p) or len(x.get("v", ())) != len(p):
        return None
    key = (id(x["h"]), id(x["v"]), len(p), tf, keep, p[-1] if len(p) else None, x["v"][-1] if len(p) else None)
    hit = _TX_CACHE.get(key)
    if hit is not None:
        return hit
    if len(_TX_CACHE) > 4096:
        _TX_CACHE.clear()
    out = _tail_extra(p, x, tf, keep)
    _TX_CACHE[key] = out
    return out


def _tail_extra(p, x, tf=1, keep=LOOKBACK):
    if tf <= 1:
        return dict(h=x["h"][-keep:], l=x["l"][-keep:], v=x["v"][-keep:])
    last = len(p) - 1
    start = last - tf * min(keep - 1, last // tf)
    a0 = max(0, start - tf + 1)
    H, Lo, V = x["h"][a0:last + 1], x["l"][a0:last + 1], x["v"][a0:last + 1]  # one copy each, then plain lists
    h, lo, v = [], [], []
    for j in range(start, last + 1, tf):
        a, b = max(0, j - tf + 1) - a0, j + 1 - a0
        h.append(max(H[a:b]))
        lo.append(min(Lo[a:b]))
        v.append(sum(V[a:b]))
    return dict(h=h, l=lo, v=v)


def clamp_tf(tf):
    """Nearest allowed timeframe for any value (bad input falls back to 1)."""
    try:
        tf = float(tf)
    except (TypeError, ValueError):
        return 1
    return min(TIMEFRAMES, key=lambda t: abs(t - tf))


FEATURE_KEEP = 40      # results kept per learning switch


FEATURE_MIN = 4        # tests before a verdict


FEATURE_CLEAR = 2.5    # how convincing a clear verdict must be (high: ~30 switches are tested, so some look good by luck)


FEATURE_LEAN = 1.5


FEATURE_SKIP = {"hindsight", "prove_live", "exam_gate"}


def pick_feature(cands, book, rng):
    """The switch tested least so far (ties at random), so every switch gets its turn."""
    live = [k for k in cands if feature_verdict(book.get(k))["verdict"] != "unused"] or cands  # (no turns for unused)
    if not live:
        return None
    n = {k: len(book.get(k) or []) for k in live}
    low = min(n.values())
    return rng.choice([k for k in live if n[k] == low])


def farm_pick_score(res, top=5):
    """How a practice farm's own picks did on prices they never saw: the mean exam edge over holding of its top
    `top` bots, ranked by TRAINING results the way the farm itself would choose them (ranking them by the exam
    would let luck on the exam pick the winners). None if too few of them traded."""
    rs = [r for r in res.get("results") or [] if r.get("exam_edge") is not None and (r.get("trades") or 0) >= 3]
    rs.sort(key=lambda r: -(r["train_edge"] if r.get("train_edge") is not None else (r.get("train_ret") or 0)))
    rs = rs[:top]
    return sum(r["exam_edge"] for r in rs) / len(rs) if len(rs) >= 2 else None


def record_feature(book, key, on, off, when=None, keep=FEATURE_KEEP):
    """Adds one paired result to the feature book (plain data, saved with Free roam). False if it couldn't be judged."""
    if on is None or off is None:
        return False
    rows = book.setdefault(key, [])
    rows.append([round(on, 6), round(off, 6), int(when or time.time())])
    del rows[:-keep]
    return True


def feature_verdict(rows):
    """-> dict(n, mean, t, verdict) for one switch. mean: how much better (+) or worse (-) the farm's picks did on
    their exam with it on than off, in % points. verdict: 'testing' (too few tests), 'helps', 'hurts',
    'leans helps', 'leans hurts', 'no clear effect', or 'unused' (it never made the slightest difference: practice
    farms don't seem to use it)."""
    d = [(a - b) * 100 for a, b, *_ in rows or []]
    n = len(d)
    if n == 0:
        return dict(n=0, mean=None, t=None, verdict="testing")
    mean = sum(d) / n
    sd = (sum((x - mean) ** 2 for x in d) / (n - 1)) ** 0.5 if n > 1 else 0.0
    t = mean / sd * n ** 0.5 if sd > 1e-12 else (0.0 if abs(mean) < 1e-12 else math.copysign(9.9, mean))
    if n < FEATURE_MIN:
        v = "testing"
    elif all(abs(x) < 1e-9 for x in d):
        v = "unused"
    elif t >= FEATURE_CLEAR:
        v = "helps"
    elif t <= -FEATURE_CLEAR:
        v = "hurts"
    elif t >= FEATURE_LEAN:
        v = "leans helps"
    elif t <= -FEATURE_LEAN:
        v = "leans hurts"
    else:
        v = "no clear effect"
    return dict(n=n, mean=round(mean, 2), t=round(t, 2), verdict=v)


FEATURE_VERDICT_TEXT = {
    "helps": "Helps: the farm picks better bots with it",
    "hurts": "Hurts: the farm picks better bots without it",
    "leans helps": "Leans helps: looks useful, needs more tests to be sure",
    "leans hurts": "Leans hurts: looks costly, needs more tests to be sure",
    "no clear effect": "No clear effect so far",
    "testing": "Still testing",
    "unused": "Not used by practice farms (it never changed a result), so it can't be measured this way",
}


class StretchPool:
    """Stretches of history Free roam has downloaded, kept in memory so each one can be studied a few times before a
    new one is fetched (far fewer downloads). Only the first visit is on prices the farm hasn't seen, so every later
    visit is handed out with info["revisit"] True: those studies may teach, but never promote (no exams, graduates or
    hatched eggs). Each caller gets its own copy, so one study can't change another's prices."""

    def __init__(self, keep=4):
        self.keep = keep
        self.items = []

    def take(self, key, reuse, min_bars=0, gran=None):
        """A stretch for another visit -> (closes, extra, info), or None when a new one should be downloaded."""
        import copy
        reuse = max(1, int(reuse))
        self.items = [x for x in self.items if x["key"] == key and x["uses"] < reuse]  # new session: start afresh
        fit = [x for x in self.items if x["info"].get("bars", 0) >= min_bars and (gran is None or x["info"].get("gran") == gran)]
        if not fit:
            return None
        x = min(fit, key=lambda x: x["uses"])  # the least-visited first
        x["uses"] += 1
        return copy.deepcopy(x["closes"]), copy.deepcopy(x["extra"]), dict(x["info"], revisit=True, visit=x["uses"])

    def add(self, key, reuse, closes, extra, info):
        """A freshly downloaded stretch (its first visit). -> info marked as a first visit."""
        import copy
        info = dict(info, revisit=False, visit=1)
        if int(reuse) > 1:
            self.items.append(dict(key=key, uses=1, closes=copy.deepcopy(closes), extra=copy.deepcopy(extra), info=info))
            del self.items[:-self.keep]
        return info


EXAM_KEEP = 12   # exams remembered per bot


EXAM_MIN = 3     # exams needed before a verdict


def exam_trades(v):
    """Trades a bot made in its exams (Free roam tests and day replays on days it never trained on)."""
    return sum(int(x.get("n", 0) or 0) for x in (getattr(v, "exams", None) or []) if isinstance(x, dict))


def add_exam(v, kind, edge, n, when=None, gran=None):
    """Records one exam (edge: % result on unseen prices, vs the market where there is one; n: its trades; gran: the
    candle size it was tested on, which is always the candle size it lives on)."""
    ex = list(getattr(v, "exams", None) or [])
    ex.append(dict(k=kind, edge=round(float(edge), 3), n=int(n), at=round(time.time()), when=when, g=gran))
    v.exams = ex[-EXAM_KEEP:]


def exam_bar(tried):
    """How convincing exam results must be when `tried` bots have been tried: the more bots a farm tries, the
    luckier its best one looks by chance alone, so the bar rises (about the best of `tried` coin flips)."""
    return math.sqrt(2 * math.log(max(2, tried))) / 2


def exam_verdict(v, tried=100):
    """-> (verdict, detail). verdict: 'passed', 'failed' or 'untested'. A bot passes when its exam results are
    positive, mostly winning, and steady enough (mean / its spread x sqrt(exams)) to clear the bar for how many bots
    the farm has tried. Exams with no trades say nothing and don't count."""
    ex = [x for x in (getattr(v, "exams", None) or []) if x.get("n", 0) > 0]
    k = len(ex)
    if k == 0:
        return "untested", dict(k=0, mean=None, score=None, bar=round(exam_bar(tried), 2), wins=0)
    e = [x["edge"] for x in ex]
    mean = sum(e) / k
    sd = (sum((x - mean) ** 2 for x in e) / (k - 1)) ** 0.5 if k > 1 else 0.0
    score = mean / sd * k ** 0.5 if sd > 1e-9 else (9.9 if mean > 0 else -9.9 if mean < 0 else 0.0)
    wins = sum(1 for x in e if x > 0)
    bar = exam_bar(tried)
    d = dict(k=k, mean=round(mean, 2), score=round(score, 2), bar=round(bar, 2), wins=wins)
    trades = sum(x["n"] for x in ex)
    if k >= 2 and trades >= 6 and wins == 0:
        return "failed", d
    if k < EXAM_MIN or trades < 6:
        return "untested", d
    if mean <= 0 or wins * 2 < k:
        return "failed", d
    if score >= bar:
        return "passed", d
    return "untested", d


_noted = {}


def to_dict(r):
    return r if isinstance(r, dict) else r.to_dict()


_PACE = {"calls": []}


_PACE_LOCK = threading.Lock()


def _merge_spans(spans):
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _missing(spans, a, b):
    out, cur = [], a
    for s0, e0 in _merge_spans(spans):
        if e0 <= cur:
            continue
        if s0 >= b:
            break
        if s0 > cur:
            out.append((cur, s0))
        cur = max(cur, e0)
    if cur < b:
        out.append((cur, b))
    return [(x, y) for x, y in out if y - x > 60]


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def common_stretch(data):
    """Lines several tickers up on the bar times they all have. -> (closes, extra)"""
    keys = [c for c, d in data.items() if d.get("t")]
    if not keys:
        return {}, {}
    shared = set(data[keys[0]]["t"])
    for c in keys[1:]:
        shared &= set(data[c]["t"])
    ts = sorted(shared)
    closes, extra = {}, {}
    for c in keys:
        d = data[c]
        idx = {t: i for i, t in enumerate(d["t"])}
        ii = [idx[t] for t in ts]
        closes[c] = [d["c"][i] for i in ii]
        extra[c] = {k: [d[k][i] for i in ii] for k in ("h", "l", "v")}
        extra[c]["t"] = list(ts)
    return closes, extra


_AVAIL = {}


def _avg(p, n, off=0):
    seg = p[len(p) - n - off: len(p) - off]
    return sum(seg) / n


def sig_sma(p, n, a):
    f, s = int(a["fast"]), int(a["slow"])
    if f >= s or len(p) < s + 1:
        return None
    f0, s0, f1, s1 = _avg(p, f, 1), _avg(p, s, 1), _avg(p, f), _avg(p, s)
    if f0 <= s0 and f1 > s1:
        return "buy"
    if f0 >= s0 and f1 < s1:
        return "sell"


def sig_rsi(p, n, a):
    k = int(a["period"])
    if len(p) < k + 1:
        return None
    d = [p[i] - p[i - 1] for i in range(len(p) - k, len(p))]
    gain = sum(x for x in d if x > 0) / k
    loss = -sum(x for x in d if x < 0) / k
    rsi = 100 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return "buy" if rsi < a["lo"] else "sell" if rsi > a["hi"] else None


def sig_mom(p, n, a):
    k = int(a["n"])
    if len(p) < k + 1:
        return None
    ch = p[-1] / p[-1 - k] - 1
    thr = a["thr"] / 100
    return "buy" if ch > thr else "sell" if ch < -thr else None


def sig_boll(p, n, a):
    k = int(a["n"])
    if len(p) < k:
        return None
    w = p[-k:]
    m = sum(w) / k
    sd = (sum((x - m) ** 2 for x in w) / k) ** 0.5
    if p[-1] < m - a["k"] * sd:
        return "buy"
    if p[-1] > m + a["k"] * sd:
        return "sell"


def sig_breakout(p, n, a):
    k = int(a["n"])
    if len(p) < k + 1:
        return None
    prev = p[-k - 1:-1]
    return "buy" if p[-1] > max(prev) else "sell" if p[-1] < min(prev) else None


def _ema(vals, k):
    m, e, out = 2.0 / (k + 1), vals[0], [vals[0]]
    for v in vals[1:]:
        e = v * m + e * (1 - m)
        out.append(e)
    return out


def sig_macd(p, n, a):
    f, s, g = int(a["fast"]), int(a["slow"]), int(a["signal"])
    if f >= s or len(p) < s + g + 2:
        return None
    seg = p[-(s * 3 + g):]
    ef, es = _ema(seg, f), _ema(seg, s)
    macd = [x - y for x, y in zip(ef, es)]
    sig = _ema(macd, g)
    d0, d1 = macd[-2] - sig[-2], macd[-1] - sig[-1]
    if d0 <= 0 < d1:
        return "buy"
    if d0 >= 0 > d1:
        return "sell"


def sig_revert(p, n, a):
    k = int(a["n"])
    if len(p) < k:
        return None
    dev = (p[-1] / _avg(p, k) - 1) * 100
    return "buy" if dev < -a["thr"] else "sell" if dev > a["thr"] else None


def sig_dca(p, n, a):
    return "buy" if n % int(a["every"]) == 0 else None


def sig_ema(p, n, a):
    """Fast/slow exponential average crossover. Reacts quicker than SMA, good for short bars."""
    f, s = int(a["fast"]), int(a["slow"])
    if f >= s or len(p) < s + 2:
        return None
    seg = p[-(s * 3):]
    ef, es = _ema(seg, f), _ema(seg, s)
    d0, d1 = ef[-2] - es[-2], ef[-1] - es[-1]
    gap = abs(d1) / es[-1] * 100 if es[-1] else 0
    if gap < a.get("min_gap", 0):  # ignore crossings too small to beat fees
        return None
    if d0 <= 0 < d1:
        return "buy"
    if d0 >= 0 > d1:
        return "sell"


def sig_stoch(p, n, a):
    """Stochastic oscillator on closes: where the price sits in its recent range (0-100).
    Buys when it climbs back up out of the bottom zone, sells when it drops back out of the top zone."""
    k = int(a["n"])
    if len(p) < k + 1:
        return None

    def pos(w):
        lo, hi = min(w), max(w)
        return 50.0 if hi == lo else (w[-1] - lo) / (hi - lo) * 100
    k0, k1 = pos(p[-k - 1:-1]), pos(p[-k:])
    if k0 < a["lo"] <= k1:
        return "buy"
    if k0 > a["hi"] >= k1:
        return "sell"


def _rsi(p, k):
    d = [p[i] - p[i - 1] for i in range(len(p) - k, len(p))]
    gain = sum(x for x in d if x > 0) / k
    loss = -sum(x for x in d if x < 0) / k
    return 100 if loss == 0 else 100 - 100 / (1 + gain / loss)


def sig_volbreak(p, n, a, x=None):
    """Breakout that only counts when volume is well above normal (a real crowd behind the move)."""
    k = int(a["n"])
    if not x or len(p) < k + 2:
        return None
    vol = x["v"]
    base = vol[-k - 1:-1]
    avgv = sum(base) / k
    if avgv <= 0:
        return None
    if p[-1] > max(x["h"][-k - 1:-1]) and vol[-1] >= a["vmult"] * avgv:
        return "buy"
    if p[-1] < min(x["l"][-k - 1:-1]):
        return "sell"


def sig_vwap(p, n, a, x=None):
    """Volume-weighted average price over the lookback. Buys well below it, sells well above it."""
    k = int(a["n"])
    if not x or len(p) < k:
        return None
    h, lo, v = x["h"][-k:], x["l"][-k:], x["v"][-k:]
    tv = sum(v)
    if tv <= 0:
        return None
    vw = sum((h[i] + lo[i] + p[-k + i]) / 3 * v[i] for i in range(k)) / tv
    dev = (p[-1] / vw - 1) * 100
    return "buy" if dev < -a["thr"] else "sell" if dev > a["thr"] else None


def sig_atr(p, n, a, x=None):
    """ATR channel (Keltner style): buys a push above the average plus m x the typical bar range
    (true range, from highs and lows), sells a drop below the average minus that."""
    k, m = int(a["n"]), a["m"]
    if not x or len(p) < k + 2:
        return None
    h, lo = x["h"], x["l"]
    tr = [max(h[i] - lo[i], abs(h[i] - p[i - 1]), abs(lo[i] - p[i - 1])) for i in range(len(p) - k, len(p))]
    atr = sum(tr) / k
    mid = _ema(p[-(k * 3):], k)[-1]
    if p[-1] > mid + m * atr:
        return "buy"
    if p[-1] < mid - m * atr:
        return "sell"


def sig_supertrend(p, n, a, x=None):
    """Supertrend: a trailing band m x ATR away from the price that flips sides when the trend turns."""
    k, m = int(a["n"]), a["m"]
    if not x or len(p) < k * 3:
        return None
    h, lo = x["h"], x["l"]
    L = len(p)
    tr = [max(h[i] - lo[i], abs(h[i] - p[i - 1]), abs(lo[i] - p[i - 1])) for i in range(L - k * 3 + 1, L)]
    up, prev_up, start = None, None, L - k * 2
    for i in range(start, L):
        atr = sum(tr[i - start:i - start + k]) / k
        mid = (h[i] + lo[i]) / 2
        upper, lower = mid + m * atr, mid - m * atr
        if up is None:
            up = p[i] > mid
            band = lower if up else upper
            continue
        prev_up = up
        if up:
            band = max(band, lower) if prev_up else lower
            if p[i] < band:
                up, band = False, upper
        else:
            band = min(band, upper)
            if p[i] > band:
                up, band = True, lower
    if prev_up is None:
        return None
    if up and not prev_up:
        return "buy"
    if prev_up and not up:
        return "sell"


def sig_ichimoku(p, n, a, x=None):
    """Ichimoku-style cross: the fast midline ((highest high + lowest low) / 2 over a short window) crossing the
    slow midline over a long window, only buying with the price above the slow line."""
    f, s = int(a["fast"]), int(a["slow"])
    if not x or f >= s or len(p) < s + 2:
        return None
    h, lo = x["h"], x["l"]

    def mid(k, off):
        hh, ll = h[len(h) - k - off:len(h) - off], lo[len(lo) - k - off:len(lo) - off]
        return (max(hh) + min(ll)) / 2
    f0, s0, f1, s1 = mid(f, 1), mid(s, 1), mid(f, 0), mid(s, 0)
    if f0 <= s0 and f1 > s1 and p[-1] > s1:
        return "buy"
    if f0 >= s0 and f1 < s1:
        return "sell"


def sig_roc(p, n, a):
    """Rate of change: % change over a lookback, crossing its own short average (momentum turning)."""
    k, sm = int(a["n"]), int(a["smooth"])
    if len(p) < k + sm + 2:
        return None
    roc = [(p[i] / p[i - k] - 1) * 100 for i in range(len(p) - sm - 1, len(p))]
    s0, s1 = sum(roc[:-1]) / sm, sum(roc[1:]) / sm
    if roc[-2] <= s0 and roc[-1] > s1 and roc[-1] > 0:
        return "buy"
    if roc[-2] >= s0 and roc[-1] < s1 and roc[-1] < 0:
        return "sell"


def sig_ribbon(p, n, a):
    """Moving-average ribbon: three averages stacked in order (fast > mid > slow) means a healthy uptrend: buy
    when they line up, sell when the fast one drops below the middle one."""
    f, m, s = int(a["fast"]), int(a["mid"]), int(a["slow"])
    if not f < m < s or len(p) < s * 3:
        return None
    seg = p[-(s * 3):]
    ef, em, es = _ema(seg, f), _ema(seg, m), _ema(seg, s)
    now = ef[-1] > em[-1] > es[-1]
    before = ef[-2] > em[-2] > es[-2]
    if now and not before:
        return "buy"
    if ef[-1] < em[-1] and ef[-2] >= em[-2]:
        return "sell"


def sig_trend_dip(p, n, a):
    """Buys short dips (low RSI) only while the price is above its long average (an uptrend).
    Sells on a pop (high RSI) or when the uptrend breaks."""
    t, k = int(a["trend"]), int(a["period"])
    if len(p) < max(t, k + 1):
        return None
    up = p[-1] > _avg(p, t)
    r = _rsi(p, k)
    if up and r < a["lo"]:
        return "buy"
    if r > a["hi"] or not up:
        return "sell"


CLASSICS = [
    dict(title="Golden cross", name="SMA crossover", params={"fast": 50, "slow": 200}, tf=1,
         note="The famous 50/200 moving-average cross. Slow, trend-following, popular with long-term investors."),
    dict(title="Turtle breakout", name="Breakout", params={"n": 20}, tf=1, risk={"sl": 2.0},
         note="The 1980s Turtle Traders' rule: buy a 20-bar high, sell a 20-bar low, with a tight stop."),
    dict(title="Wilder's RSI", name="RSI", params={"period": 14, "lo": 30, "hi": 70}, tf=1,
         note="J. Welles Wilder's original 1978 settings: buy oversold under 30, sell overbought over 70."),
    dict(title="Connors RSI-2", name="RSI", params={"period": 2, "lo": 10, "hi": 90}, tf=1,
         note="Larry Connors' very short RSI: buy sharp pullbacks, sell quick bounces."),
    dict(title="Classic MACD", name="MACD", params={"fast": 12, "slow": 26, "signal": 9}, tf=1,
         note="Gerald Appel's standard 12/26/9 MACD signal-line cross."),
    dict(title="Bollinger reversion", name="Bollinger bands", params={"n": 20, "k": 2.0}, tf=1,
         note="John Bollinger's 20-bar, 2-standard-deviation bands, traded as a snap-back."),
    dict(title="Day-trader EMA", name="EMA crossover", params={"fast": 9, "slow": 21, "min_gap": 0.0}, tf=1,
         note="The 9/21 EMA cross loved by day traders."),
    dict(title="Fibonacci ribbon", name="MA ribbon", params={"fast": 8, "mid": 13, "slow": 21}, tf=1,
         note="Averages on Fibonacci numbers lined up as a trend ribbon."),
    dict(title="Supertrend 10/3", name="Supertrend", params={"n": 10, "m": 3.0}, tf=1,
         note="The default Supertrend found on most charting sites."),
    dict(title="Ichimoku 9/26", name="Ichimoku cross", params={"fast": 9, "slow": 26}, tf=1,
         note="Goichi Hosoda's conversion / base line cross."),
    dict(title="Stochastic 14", name="Stochastic", params={"n": 14, "lo": 20, "hi": 80}, tf=1,
         note="George Lane's stochastic with the usual 20 / 80 levels."),
    dict(title="Buy the 5% dip", name="Mean reversion", params={"n": 50, "thr": 5.0}, tf=3,
         note="Buy when the price is 5% under its average, sell 5% over."),
    dict(title="Time-series momentum", name="Momentum", params={"n": 40, "thr": 1.0}, tf=10,
         note="Hold what has been rising, drop what has been falling (a staple of trend funds)."),
    dict(title="Average down", name="Mean reversion", params={"n": 30, "thr": 2.0}, tf=1, traits={"entries": 5, "exit": 1},
         risk={"tp": 4.0}, controversial=True,
         note="CONTROVERSIAL: keeps buying more as the price falls (martingale-style) with no stop, sells only at a "
              "profit target. Wins often, but one long slide can be painful."),
    dict(title="Pump chaser", name="Volume breakout", params={"n": 10, "vmult": 3.0}, tf=1, traits={"maxhold": 20},
         controversial=True, note="CONTROVERSIAL: jumps on sudden volume spikes and gets out fast. Often buys the top."),
    dict(title="Diamond hands", name="Trend + dip", params={"trend": 100, "period": 14, "lo": 35, "hi": 85}, tf=10,
         traits={"exit": 1}, risk={"tp": 15.0}, controversial=True,
         note="CONTROVERSIAL: buys dips in an uptrend and never sells at a loss, only at +15%."),
]


def market_mood(p, n=60):
    """-> (mood, efficiency 0..1, move %). Efficiency is how straight the price travelled: 1 = a clean
    one-way move, near 0 = lots of back and forth for little net change."""
    if len(p) < n + 1:
        return "unknown", 0.0, 0.0
    seg = p[-n - 1:]
    path = sum(abs(seg[i] - seg[i - 1]) for i in range(1, len(seg)))
    move = seg[-1] - seg[0]
    er = abs(move) / path if path else 0.0
    pct = move / seg[0] * 100 if seg[0] else 0.0
    if er >= 0.3:
        return ("trending up" if move > 0 else "trending down"), er, pct
    if path / seg[0] * 100 < 0.15 * n ** 0.5 / 4:  # barely moving at all
        return "quiet", er, pct
    return "choppy", er, pct


MOODS = ("trending up", "trending down", "choppy", "quiet")


EVENT_KINDS = {
    "cpi":    (10, "CPI inflation", 8, 30),
    "jobs":   (50, "Jobs report", 8, 30),
    "fomc":   (101, "Fed decision", 14, 0),
    "gdp":    (53, "GDP", 8, 30),
    "pce":    (54, "PCE inflation", 8, 30),
    "retail": (9, "Retail sales", 8, 30),
}


BIG_EVENTS = ("cpi", "jobs", "fomc")


_EVENTS = {"data": None, "mtime": 0}


def _ny_offset(d):
    """Hours New York is behind UTC on date d (daylight saving: 2nd Sunday of March to 1st Sunday of November)."""
    def nth_sunday(y, m, nth):
        first = datetime(y, m, 1)
        return 1 + (6 - first.weekday()) % 7 + 7 * (nth - 1)
    start = datetime(d.year, 3, nth_sunday(d.year, 3, 2))
    end = datetime(d.year, 11, nth_sunday(d.year, 11, 1))
    return 4 if start <= datetime(d.year, d.month, d.day) < end else 5


def ny_time(date_str, hh, mm):
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return d.replace(hour=hh, minute=mm, tzinfo=timezone.utc).timestamp() + _ny_offset(d) * 3600


def _bar_time(extra, c, sim=None):
    t = ((extra or {}).get(c) or {}).get("t")
    if t:
        return t[-1]
    return getattr(sim, "now", None) or time.time()


def data_now(extra, coins):
    """Time of the newest bar in this data (bar times travel with the highs/lows/volumes as 't')."""
    for c in coins:
        t = ((extra or {}).get(c) or {}).get("t")
        if t is not None and len(t):
            return t[-1]
    return time.time()


def risk_text(r):
    r = r or {}
    parts = ([f"stop {r['sl']:g}%"] if r.get("sl") else []) + ([f"target {r['tp']:g}%"] if r.get("tp") else [])
    return ", ".join(parts)


MAX_VETOES = 3


MAX_CTX = 3          # context studies per bot (on coins it watches but never trades)


CTX_KEY = "__ctx__"  # where the watched coins' prices travel inside the extra data


def ctx_text(e):
    up = "for" if e["sign"] > 0 else "against"
    down = "against" if e["sign"] > 0 else "for"
    if e.get("kind") == "study":
        return f"{e['sym']} {e['name']} buy votes {up} buying, sell votes {down}"
    return f"{e['sym']} up {e['th']:g}%+ over {e['lb']} decisions votes {up} buying, down that much votes {down}"


HTF_MULTS = (3, 4, 6, 8, 12)  # a slower-timeframe study looks at candles this many times longer than the bot's own


def _sub_txt(s):
    return f"{s['name']} ({'/'.join(f'{x:g}' for x in s['params'].values())})"


def _stat_txt(st, what):
    if st and st[0]:
        return f" [{'saved' if st[1] >= 0 else 'cost'} {abs(st[1]):.1f}% over {int(st[0])} {what}]"
    return ""


def metrics(eq):
    peak, dd = eq[0], 0.0
    for x in eq:
        peak = max(peak, x)
        dd = max(dd, (peak - x) / peak if peak else 0)
    return eq[-1] / eq[0] - 1, dd


def rel_edge(ret, hold):
    """How the bot did compared with just holding, as money: (1 + its return) / (1 + holding's return) - 1.
    -50% = it ended with half as much money as holding would have; it can never be below -100%. (Simply subtracting the
    two returns can show -300% when holding rallied 400% over a long stretch, which reads like an impossible loss.)"""
    if hold is None or hold <= -1:
        return ret
    return (1 + ret) / (1 + hold) - 1


def backtest_summary(r):
    """Small record of a backtest, kept with the bot (its memory) and in its saved file."""
    return dict(date=datetime.now().strftime("%Y-%m-%d %H:%M"), bars=r["bars"], ret=round(r["ret"] * 100, 2),
                edge=round(r["edge"] * 100, 2), maxdd=round(r["maxdd"] * 100, 2), trades=r["trades"],
                winrate=None if r["winrate"] is None else round(r["winrate"] * 100, 1),
                segments_passed=sum(1 for s in r["segments"] if s[2] > 0), segments=len(r["segments"]),
                fees2x=round(r["fees2x"] * 100, 2), mc_dd95=None if r["mc_dd95"] is None else round(r["mc_dd95"] * 100, 2))


def _merge_stats(dst, src):
    for k, x in (src or {}).items():
        d = dst.setdefault(k, [0.0, 0.0])
        d[0] += x[0]
        d[1] += x[1]


class TrainingStopped(Exception):
    pass


def _genes(v):
    return dict(name=v.name, params=dict(v.params), tf=v.tf, risk=dict(v.risk), traits=dict(v.traits),
                vetoes=dict(v.vetoes), layers=json.loads(json.dumps(v.layers)))


def slice_extra(extra, i):
    """The extra data (highs/lows/volumes) cut to the first i bars (as views, nothing is copied)."""
    if not extra:
        return None
    return {c: {k: View(v, i) for k, v in d.items()} for c, d in extra.items() if d}


STABLES = {"USDC", "USDT", "DAI", "PYUSD", "EURC", "GUSD", "USDS", "PAX", "TUSD", "FDUSD", "RLUSD"}


def alerts_configured(settings):
    return bool((settings.get("alert_discord") or "").strip()
                or ((settings.get("alert_tg_token") or "").strip() and (settings.get("alert_tg_chat") or "").strip()))


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


DISK_WRITES = {}  # what CBBF has written since it started: kind -> [times, bytes]


DISK_T0 = time.time()


def note_write(path, nbytes):
    base = os.path.basename(path)
    kind = ("Farm saves" if base.startswith("farm-") else "Session saves" if base.startswith("state-") else
            "Price history" if os.sep + "history" + os.sep in path or os.sep + "cache" + os.sep in path else
            "Slow farm" if "slow" in base else "Settings and other")
    w = DISK_WRITES.setdefault(kind, [0, 0])
    w[0] += 1
    w[1] += nbytes


BOT_FORMAT_VERSION = 1


MAX_BOT_FILE = 1024 * 1024  # bytes; a bot file is tiny, anything bigger is not one


def lifetime_record(lf, bar_minutes=None):
    """Plain-number lifetime stats for a bot file."""
    def pct(x):
        return None if x is None else round(x * 100, 2)
    out = dict(return_pct=pct(lf.get("ret")), vs_holding_pct=pct(lf.get("edge")), win_rate_pct=pct(lf.get("winrate")),
               wins=lf.get("wins", 0), losses=lf.get("losses", 0), trades=lf.get("trades", 0),
               worst_drop_pct=pct(lf.get("maxdd")), decisions_alive=lf.get("age", 0),
               decisions_winning=lf.get("win_steps", 0), best_market=lf.get("best_mood"),
               fees_paid=round(float(lf.get("fees") or 0), 2))
    if bar_minutes:
        out["bar_minutes"] = bar_minutes
        out["hours_winning"] = round(lf.get("win_steps", 0) * float(bar_minutes) / 60, 2)
    return out


MEMORY_FIELDS = ("twin_test_pct", "backtests", "retest", "layer_stats", "veto_stats")  # what a saved bot remembers


COLLECT_FIELDS = ("nickname", "rarity", "level", "xp", "caught_at", "source", "team", "shiny")


TEAM_MAX = 6


RARITIES = ("Common", "Uncommon", "Rare", "Epic", "Legendary")


RARITY_COLORS = {"Common": "#9ca3af", "Uncommon": "#22c55e", "Rare": "#3b82f6", "Epic": "#a855f7", "Legendary": "#f59e0b"}


_ADJ = ("Swift", "Silent", "Lucky", "Iron", "Golden", "Shadow", "Turbo", "Patient", "Crimson", "Frosty", "Clever", "Bold",
        "Sneaky", "Mighty", "Cosmic", "Thunder", "Velvet", "Rusty", "Neon", "Stormy", "Quiet", "Brave", "Jade", "Solar",
        "Lunar", "Atomic", "Wild", "Steady", "Nimble", "Electric", "Mystic", "Copper")


_BEAST = ("Falcon", "Otter", "Badger", "Lynx", "Mantis", "Viper", "Panda", "Heron", "Wolf", "Gecko", "Raven", "Bison",
          "Fox", "Shark", "Owl", "Tiger", "Moth", "Yak", "Cobra", "Koala", "Puma", "Hawk", "Beetle", "Marten", "Orca",
          "Ibex", "Lemur", "Jackal", "Stoat", "Kestrel", "Tapir", "Walrus")


def bot_nickname(key):
    """A stable, friendly name for a bot (the same settings always get the same name)."""
    import hashlib
    h = hashlib.sha1(repr(key).encode()).digest()
    return f"{_ADJ[h[0] % len(_ADJ)]} {_BEAST[h[1] % len(_BEAST)]}"


def is_shiny(key):
    import hashlib
    return hashlib.sha1(("shiny" + repr(key)).encode()).digest()[0] < 5  # about 1 in 50


def rarity_for(lt):
    """Rarity from a bot's track record (lifetime dict from a bot file): beating holding counts most,
    then win rate and time spent winning."""
    lt = lt or {}
    edge = float(lt.get("vs_holding_pct") or 0)
    wr = lt.get("win_rate_pct")
    hrs = float(lt.get("hours_winning") or 0)
    score = edge + (0 if wr is None else (float(wr) - 50) * 0.08) + min(hrs, 48) * 0.15
    if lt.get("source") == "Bot Lab unseen-data test" and edge > 0:
        score += 1.5  # passing on unseen prices is worth extra
    for name, need in (("Legendary", 12), ("Epic", 7), ("Rare", 3.5), ("Uncommon", 1.5)):
        if score >= need:
            return name
    return "Common"


def xp_for(lt):
    lt = lt or {}
    return int(float(lt.get("hours_winning") or 0) * 10 + float(lt.get("trades") or 0) * 2
               + max(0.0, float(lt.get("vs_holding_pct") or 0)) * 5)


def level_for(xp):
    return min(99, 1 + int((max(0, xp) / 10) ** 0.5))


def save_bots_file(path, payload):
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) < 1e12


def _clean_memory(d):
    """A saved bot's memory (backtests, retest, rule evidence, twin test), plain numbers only."""
    out = {}
    if _num(d.get("twin_test_pct")):
        out["twin_test_pct"] = float(d["twin_test_pct"])
    rt = d.get("retest")
    if isinstance(rt, dict):
        out["retest"] = {k: v for k, v in rt.items() if k in ("date", "edge", "ret", "rusty")
                         and (_num(v) or isinstance(v, bool) or (isinstance(v, str) and len(v) <= 20))}
    bts = d.get("backtests")
    if isinstance(bts, list):
        keep = []
        for b in bts[-10:]:
            if isinstance(b, dict):
                keep.append({k: v for k, v in b.items() if (_num(v) or v is None or (isinstance(v, str) and len(v) <= 20))})
        if keep:
            out["backtests"] = keep
    for f in ("layer_stats", "veto_stats"):
        st = d.get(f)
        if isinstance(st, dict):
            good = {k: [float(x[0]), float(x[1])] for k, x in st.items()
                    if isinstance(k, str) and len(k) <= 12 and isinstance(x, list) and len(x) == 2 and all(_num(y) for y in x)}
            if good:
                out[f] = good
    return out


TRADE_FIELDS = ["time", "mode", "side", "coin", "qty", "price", "usd", "pl", "reason", "who"]


def export_trades_csv(path, trades):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        w.writeheader()
        for t in trades:
            w.writerow({k: t.get(k, "") for k in TRADE_FIELDS})


SECRET_SETTINGS = ("alert_tg_token", "alert_discord")   # inside cfg["settings"]


SECRET_CFG = ("fred_key",)                              # at the top of cfg


def private_dir(path):
    """Makes the data folder readable by you only (Mac / Linux; Windows user folders already are)."""
    try:
        if os.name == "posix":
            os.makedirs(path, exist_ok=True)
            os.chmod(path, 0o700)
    except OSError:
        pass


def keyring_available():
    try:
        import keyring  # noqa: F401
        return True
    except ImportError:
        return False


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:4]) or (0,)


def changelog_since(text, current):
    """The changelog sections newer than `current` (sections start with '## <version>' or '## Unreleased')."""
    out, keep = [], False
    for line in (text or "").splitlines():
        m = re.match(r"^##\s+(.+)$", line)
        if m:
            head = m.group(1).strip()
            nums = re.findall(r"\d+\.\d+(?:\.\d+)?", head)
            keep = bool(nums) and version_tuple(nums[0]) > version_tuple(current)
        if keep:
            out.append(line)
    return "\n".join(out).strip()


def sha256_from_file(text):
    """The checksum in a `sha256sum`-style line ("<64 hex digits>  name"), or "" if there isn't one."""
    m = re.match(r"\s*([0-9a-fA-F]{64})\b", text or "")
    return m.group(1).lower() if m else ""


def _requirement_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [ln.split("#", 1)[0].strip() for ln in f if ln.split("#", 1)[0].strip()]
    except OSError:
        return []


def new_requirements(backup, here):
    """Requirement lines the freshly installed version has that the backed-up one didn't (new or changed)."""
    old = set(_requirement_lines(os.path.join(backup, "requirements.txt")))
    return [r for r in _requirement_lines(os.path.join(here, "requirements.txt")) if r not in old]


UPDATE_FILES = re.compile(r"^(?:[\w.-]+\.pyw?|VERSION|README\.md|CHANGELOG\.md|requirements\.txt|[\w .-]+\.bat)$")


def latest_backup(here):
    root = os.path.join(here, "backups")
    try:
        dirs = sorted((d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))),
                      key=lambda d: os.path.getmtime(os.path.join(root, d)))
    except OSError:
        return None
    return os.path.join(root, dirs[-1]) if dirs else None


def restore_backup(here, backup):
    for rel in os.listdir(backup):
        if UPDATE_FILES.match(rel):
            shutil.copy2(os.path.join(backup, rel), os.path.join(here, rel))


SOUNDS = {  # (frequency Hz, start s, length s) notes; synthesized, so nothing to download
    "trade": [(988, 0.0, 0.09), (1319, 0.05, 0.12)],
    "notify": [(1319, 0.0, 0.35), (1760, 0.13, 0.5)],
    "hof": [(1047, 0.0, 0.22), (1319, 0.11, 0.22), (1568, 0.22, 0.22), (2093, 0.33, 0.6)],
    "problem": [(440, 0.0, 0.28), (349, 0.22, 0.42)],
}


# ---------------- account-wide safety limits ----------------
LIMIT_DEFAULTS = {"max_open": 10, "loss_streak": 5, "streak_pause": 30.0, "fill_learn": True, "earnings_guard": 1,
                  "study_cores": True, "rail_nav": True, "standin_n": 3, "standin_size": 0.5}  # (the limit per ticker already existed)

LIMIT_SETTINGS = (  # (key, explanation, type) for the Settings window, shown after the daily loss limit
    ("max_open", "Most open positions: the account never holds more than this many positions at once, whatever the bots "
                 "say (0 = no limit)", int),
    ("loss_streak", "Losing-streak pause: after this many losing trades in a row, open nothing new for a while "
                    "(0 = off). Sells and safety exits still happen", int),
    ("streak_pause", "How long the losing-streak pause lasts, in minutes", float),
)
STANDIN_SETTINGS = (
    ("standin_n", "...while no bot is proven, the best few bots may trade the account anyway, at a smaller size: how "
                  "many (0 = off, the account waits for a proven bot)", int),
    ("standin_size", "...their trade size, as a share of the normal size (0.5 = half)", float),
)
NAV_SETTINGS = (
    ("rail_nav", "New layout: Home plus Trading, Bots, Learning and Markets on a rail at the left (off = the old row of "
                 "tabs). Takes effect the next time the app starts", bool),
)
EARN_SETTINGS = (
    ("earnings_guard", "Earnings guard: the account opens nothing in a stock this many trading days before it reports "
                       "earnings (1 = not the day before or the day of; 0 = off). Prices often jump on the report, and "
                       "options lose much of their value right after it", int),
)
FILL_SETTINGS = (
    ("fill_learn", "Learn from paper fills: with 'Both' on, compare each practice trade with its Alpaca paper copy, and "
                   "make practice charge what real fills really cost (after 10 copies)", bool),
)


def book_ticker(c):
    """The ticker a book trades ('SPY~scalp' -> 'SPY')."""
    return str(c).split("~")[0]


class FarmAppMixin:  # (FillCheckMixin, defined below, joins it in apps with Alpaca paper copies)
    """App behaviour shared by CBBF, OBF and SBF: account-wide safety limits and the farm doctor. It relies on the
    attributes every app has (settings, pos, position_price, equity, tourn, running, cfg, root, write, ...)."""

    # ---- limits ----
    def limit_block(self, c, spend):
        """Why the account may not open (or add to) `c` for `spend` right now, or None. Checked before every new
        position, whatever the bots say; sells and safety exits are never blocked."""
        s = self.settings
        st = self.__dict__.setdefault("lim_state", dict(n=0, until=0.0))
        now = time.time()
        if st.get("until", 0) > now:
            return f"losing-streak pause ({int((st['until'] - now) // 60) + 1} min left)"
        ed = int(s.get("earnings_guard", 0) or 0)
        if ed > 0 and getattr(self, "HAS_EARNINGS", False):
            k = earn_days_ahead(c, now)
            if k is not None and k < ed:
                return earnings_text(c, now) + f" (you don't open anything {ed} trading day{'s' if ed > 1 else ''} before)"
        held = [k for k, q in self.pos.items() if q > 0]
        mo = int(s.get("max_open", 0) or 0)
        if mo > 0 and c not in held and len(held) >= mo:
            return f"already {len(held)} open positions (the most you allow is {mo})"
        return None

    def limit_blocked(self, c, spend):
        """limit_block, saying why in the log at most once every 10 minutes per reason. -> True when blocked."""
        why = self.limit_block(c, spend)
        if not why:
            return False
        seen = self.__dict__.setdefault("_lim_said", {})
        key = why.split(" (")[0]
        if time.time() - seen.get(key, 0) > 600:
            seen[key] = time.time()
            self.write(f"Safety limit: not opening {book_ticker(c)}: {why}. Change it in Settings > Safety limits.", "warn")
        return True

    def limit_note_close(self, pl):
        """Counts a finished trade toward the losing-streak pause."""
        if pl is None:
            return
        st = self.__dict__.setdefault("lim_state", dict(n=0, until=0.0))
        if pl < 0:
            st["n"] = st.get("n", 0) + 1
        else:
            st["n"] = 0
        k = int(self.settings.get("loss_streak", 0) or 0)
        if k > 0 and st["n"] >= k:
            mins = float(self.settings.get("streak_pause", 30) or 30)
            st["until"], st["n"] = time.time() + mins * 60, 0
            self.write(f"Safety limit: {k} losing trades in a row, so nothing new opens for {mins:g} minutes. "
                       "Open positions keep their stops and exits.", "warn")

    # ---- the farm doctor ----
    def doctor_findings(self):
        """Health checks on the farm and the app: [dict(key, level 'bad'|'warn'|'info', title, text, fix, fix_label)]."""
        out = []
        add = lambda key, level, title, text, fix=None, label=None: out.append(
            dict(key=key, level=level, title=title, text=text, fix=fix, fix_label=label))
        s, t, now = self.settings, getattr(self, "tourn", None), time.time()
        running = bool(getattr(self, "running", False))
        mkt = getattr(self, "market", None)
        market_open = True if not isinstance(mkt, dict) else bool(mkt.get("is_open", True))
        if t is not None and getattr(t, "vars", None):
            cap = int(s.get("max_bots", t.cap) or t.cap)
            n = len(t.vars)
            if n > cap * 1.1 + 1:
                add("overfull", "bad", f"The farm has {n} bots; its size is {cap}",
                    "A farm far past its size runs slower and judges its bots more slowly.", "trim", f"Trim to {cap}")
            unscored = [v for v in t.vars if t.score(v) is None]
            stuck = [v for v in unscored if not t.young(v)]
            if t.n > 300 and len(unscored) > 0.6 * n:
                add("warming", "warn", f"{len(unscored)} of {n} bots can't be judged yet",
                    (f"{len(stuck)} of them have had a full window and still hardly trade; they make room first when "
                     "new eggs come. " if stuck else "Most are new eggs: keep the farm running and they'll get scored. ")
                    + "If this lasts for days, new eggs may be arriving faster than bots can be judged.")
            kinds = {}
            for v in t.vars:
                kinds[v.name] = kinds.get(v.name, 0) + 1
            top, k = max(kinds.items(), key=lambda x: x[1])
            if n >= 10 and k > 0.7 * n:
                add("monoculture", "info", f"{k} of {n} bots share one strategy",
                    "The farm has little to fall back on if that strategy stops working. Backup strategies are kept "
                    "alive automatically; a fresh egg or two of other kinds can help.")
            if running and s.get("proven_only", True) and hasattr(t, "proven_bots"):
                if t.proven_bots():
                    self.__dict__["_doc_np_since"] = None
                else:
                    since = self.__dict__.get("_doc_np_since") or now
                    self.__dict__["_doc_np_since"] = since
                    hrs = (now - since) / 3600
                    si = t.standins() if hasattr(t, "standins") else []
                    if hrs >= 2:
                        add("unproven", "info" if si else "warn", f"No bot has been proven for {hrs:.0f} hours",
                            (f"Until one is, the {len(si)} best winners trade the account at a smaller size. " if si else
                             "Only proven bots trade the account, so it's standing still. ")
                            + "Free roam's exams and a running farm are how bots get proven.", "training", "How to train them")
            examined = [v for v in t.vars if getattr(v, "exams", None)]
            if t.n > 1000 and len(examined) < 0.2 * n and s.get("proven_exam", True):
                add("exams", "info", f"Only {len(examined)} of {n} bots have taken an exam",
                    "Bots need exams on days they never trained on to be proven. Free roam gives the most.",
                    None if self.cfg.get("roam_on") else "roam_on", None if self.cfg.get("roam_on") else "Turn on Free roam")
        last = getattr(self, "last_tick", None)
        if running and market_open and last is not None:
            age = (datetime.now() - last).total_seconds()
            if age > 300:
                add("stale", "bad", f"No new prices for {int(age // 60)} minutes",
                    "The bots can't decide without prices. Check your internet connection; the app keeps reconnecting.",
                    "reconnect", "Reconnect now")
        if self.cfg.get("roam_on") and getattr(self, "_roam_last", 0):
            idle = now - self._roam_last
            err = getattr(self, "roam_err", None)
            if err:
                add("roam_err", "warn", "Free roam hit a problem", str(err)[:300], "roam_restart", "Restart Free roam")
            elif idle > 45 * 60 and t is not None:
                add("roam_idle", "warn", f"Free roam hasn't studied anything for {int(idle // 60)} minutes",
                    "It may be waiting for history it can't download, or for a login.", "roam_restart", "Restart Free roam")
        if hasattr(self, "fill_stats"):
            fs = self.fill_stats()
            if fs["n"] >= 10 and fs["rate"] is not None and fs["rate"] < 0.7:
                add("fills", "warn", f"Only {fs['rate'] * 100:.0f}% of Alpaca paper copies filled",
                    "Practice assumes every order fills; on the real exchange many didn't, so practice results are "
                    "rosier than they'd be. Wider limit waits (Settings > Orders) or more liquid tickers help.")
            if fs["priced"] >= 10 and fs["median"] is not None and abs(fs["median"]) >= 1:
                add("fillcost", "info", f"Real fills are {fs['median']:+.1f}% away from practice",
                    self.fill_text())
        if getattr(self, "HAS_EARNINGS", False):
            soon = sorted({book_ticker(k) for k, q in self.pos.items() if q > 0
                           and (earn_days_ahead(k, now) is not None and earn_days_ahead(k, now) <= 1)})
            if soon:
                add("earnings", "warn", "Holding through earnings: " + ", ".join(soon),
                    "; ".join(earnings_text(x, now) for x in soon) + ". Prices often jump on the report, and options "
                    "usually lose much of their extra value right after it.")
        errs = int(getattr(self, "err_count", 0) or 0)
        if errs >= 5:
            add("errors", "warn", f"{errs} errors this run",
                "Something keeps going wrong. A report shows what it is.", "export", "Export a report for Claude")
        from datetime import date as _date
        if getattr(self, "halted_day", None) == _date.today():
            add("dayloss", "info", "Today's loss limit was reached",
                "The account opens nothing new until tomorrow (your daily loss limit). Open positions keep their exits.")
        st = self.__dict__.get("lim_state") or {}
        if st.get("until", 0) > now:
            add("streak", "info", "Paused after a losing streak",
                f"Nothing new opens for {int((st['until'] - now) // 60) + 1} more minutes.", "resume", "Resume now")
        return out

    def doctor_fix(self, key):
        t = getattr(self, "tourn", None)
        if key == "trim" and t is not None:
            t.cap = int(self.settings.get("max_bots", t.cap))
            gone = t.trim_to_cap()
            self.write(f"Farm doctor: {gone} bots made room, so the farm is back to its size of {t.cap}.", "good")
            try:
                self.save_farm()
            except Exception:
                pass
        elif key == "roam_on":
            self.toggle_roam(True)
        elif key == "roam_restart":
            self.roam_err = None
            self.toggle_roam(False)
            self.root.after(1500, lambda: self.toggle_roam(True))
        elif key == "training" and hasattr(self, "show_training_help"):
            self.show_training_help()
        elif key == "export":
            self.export_report()
        elif key == "reconnect" and getattr(self, "worker_args", None):
            self.write("Farm doctor: reconnecting the price feed.", "warn")
            self.launch_worker()
        elif key == "resume":
            self.__dict__.setdefault("lim_state", {})["until"] = 0.0
            self.write("Losing-streak pause lifted.", "good")
        self.doctor_tick(reschedule=False)

    def _doctor_colors(self):
        import sys
        g = vars(sys.modules[type(self).__module__])
        return {k: g.get(k, d) for k, d in (("BG", "#111827"), ("CARD", "#1f2937"), ("TEXT", "#e5e7eb"),
                                              ("MUTED", "#9ca3af"), ("GREEN", "#22c55e"), ("RED", "#ef4444"),
                                              ("LINE", "#374151"), ("ACCENT", "#3b82f6"))}

    def build_doctor_button(self, header):
        import tkinter as tk
        c = self._doctor_colors()
        self.doc_btn = tk.Button(header, text="✚ Doctor", command=self.open_doctor, relief="flat", bd=0, padx=10,
                                 pady=3, cursor="hand2", font=("", 9, "bold"), bg=c["LINE"], fg=c["TEXT"],
                                 activebackground=c["LINE"])
        self.doc_btn.pack(side="right", padx=(0, 8))
        self.root.after(20000, self.doctor_tick)

    def doctor_tick(self, reschedule=True):
        """Every minute: run the checks, colour the Doctor button, and say so once when something serious appears."""
        try:
            if getattr(self, "doc_btn", None) is None or not self.doc_btn.winfo_exists():
                return
            found = self.doctor_findings()
            c = self._doctor_colors()
            bad = [f for f in found if f["level"] == "bad"]
            warn = [f for f in found if f["level"] == "warn"]
            amber = "#f59e0b"
            if bad or warn:
                self.doc_btn.config(text=f"✚ Doctor ({len(bad) + len(warn)})", bg=c["RED"] if bad else amber,
                                    fg="white", activebackground=c["RED"] if bad else amber)
            else:
                self.doc_btn.config(text="✚ Doctor", bg=c["LINE"], fg=c["TEXT"], activebackground=c["LINE"])
            said = self.__dict__.setdefault("_doc_said", set())
            for f in bad + warn:
                if f["key"] not in said:
                    said.add(f["key"])
                    self.write(f"Farm doctor: {f['title']}. {f['text']} (click Doctor at the top)", "warn")
            said &= {f["key"] for f in found}
            win = getattr(self, "_doc_win", None)
            if win is not None and win.winfo_exists():
                self._fill_doctor(found)
        except Exception:
            try:
                import sys
                sys.modules["engine"].note_error("doctor_tick")
            except Exception:
                pass
        finally:
            if reschedule and not self.closing():
                self.root.after(60000, self.doctor_tick)

    def open_doctor(self):
        import tkinter as tk
        c = self._doctor_colors()
        w = getattr(self, "_doc_win", None)
        if w is not None and w.winfo_exists():
            w.lift()
            return self._fill_doctor(self.doctor_findings())
        w = self._doc_win = tk.Toplevel(self.root)
        w.title("Farm doctor")
        w.configure(bg=c["BG"])
        w.geometry("640x460")
        tk.Label(w, text="✚ Farm doctor", bg=c["BG"], fg=c["TEXT"], font=("", 13, "bold")).pack(anchor="w", padx=14,
                                                                                                    pady=(12, 0))
        tk.Label(w, text="Checks the farm and the app every minute, and offers a fix where there is one.", bg=c["BG"],
                 fg=c["MUTED"]).pack(anchor="w", padx=14)
        self._doc_body = tk.Frame(w, bg=c["BG"])
        self._doc_body.pack(fill="both", expand=True, padx=14, pady=10)
        tk.Button(w, text="Check again", command=lambda: self._fill_doctor(self.doctor_findings()), relief="flat",
                  bg=c["LINE"], fg=c["TEXT"], padx=10, pady=3).pack(anchor="e", padx=14, pady=(0, 12))
        self._fill_doctor(self.doctor_findings())

    def _fill_doctor(self, found):
        import tkinter as tk
        c = self._doctor_colors()
        body = self._doc_body
        for ch in body.winfo_children():
            ch.destroy()
        if not found:
            tk.Label(body, text="✓ Everything looks healthy.", bg=c["BG"], fg=c["GREEN"],
                     font=("", 11, "bold")).pack(anchor="w", pady=8)
            return
        dot = {"bad": c["RED"], "warn": "#f59e0b", "info": c["ACCENT"]}
        for f in sorted(found, key=lambda f: ("bad", "warn", "info").index(f["level"])):
            card = tk.Frame(body, bg=c["CARD"], padx=10, pady=8)
            card.pack(fill="x", pady=4)
            top = tk.Frame(card, bg=c["CARD"])
            top.pack(fill="x")
            tk.Label(top, text="●", bg=c["CARD"], fg=dot[f["level"]]).pack(side="left")
            tk.Label(top, text=f["title"], bg=c["CARD"], fg=c["TEXT"], font=("", 10, "bold")).pack(side="left", padx=4)
            if f.get("fix"):
                tk.Button(top, text=f["fix_label"] or "Fix", command=lambda k=f["fix"]: self.doctor_fix(k), relief="flat",
                          bg=c["ACCENT"], fg="white", padx=8, pady=1, cursor="hand2").pack(side="right")
            tk.Label(card, text=f["text"], bg=c["CARD"], fg=c["MUTED"], wraplength=560, justify="left",
                     anchor="w").pack(fill="x", pady=(3, 0))


# ---------------- silent bots: loosen a gate instead of waiting for them to be dropped ----------------
LOOSEN_MAX = 4  # gates a bot may lose this way


def loosen_gate(v):
    """Drops the most restrictive entry gate a bot has (it went a whole window without trading). -> what it dropped,
    or None when it has nothing left to loosen. Its genes change, so it's a slightly different bot from here on."""
    tr, ly, ve = v.traits, v.layers if v.layers is not None else {}, v.vetoes if v.vetoes is not None else {}
    if tr.get("minvol"):  # the commonest silencer: on short candles most bars move less than it asks for
        tr.pop("minvol")
        return "skipping quiet markets"
    if tr.get("moods"):
        tr.pop("moods")
        return "trading only in some kinds of market"
    if tr.get("confirm"):
        tr.pop("confirm")
        return "waiting for several signals in a row"
    if ly.get("htf"):
        ly.pop("htf")
        return "waiting for the bigger trend to agree"
    if int(ly.get("need", 1) or 1) > 1:
        ly["need"] = int(ly["need"]) - 1
        return "one of the studies that had to agree before it bought"
    if tr.get("trend"):
        tr.pop("trend")
        return "buying only above its long-term trend line"
    if ly.get("plus"):
        ly["plus"] = list(ly["plus"])[:-1]
        return "one of its extra reasons it needed before buying"
    if ve:
        k = sorted(ve)[0]
        ve.pop(k)
        return f"its never-rule '{k}'"
    if ly.get("minus"):
        ly["minus"] = list(ly["minus"])[:-1]
        return "one of its reasons not to buy"
    if tr.get("rsicap"):
        tr.pop("rsicap")
        return "its 'too high to buy' limit"
    if tr.get("cool"):
        tr.pop("cool")
        return "its wait after selling"
    return None


def loosen_silent(t, every=10):
    """Bots that went a whole scoring window without a single trade can't be judged or learn anything; often they
    simply carry too many conditions before buying. Each gets up to LOOSEN_MAX of them removed, one per silent window,
    and a fresh window to show what it does (instead of just being dropped to make room)."""
    if t.n % every:
        return 0
    k = 0
    for v in list(t.vars):
        if getattr(v, "origin", "") == "team" or (v.traits or {}).get("kami") or v.sim.tbars:
            continue
        if v.age < t.window_for(v.tf) or getattr(v, "loosened", 0) >= LOOSEN_MAX:
            continue
        what = loosen_gate(v)
        if not what:
            v.loosened = LOOSEN_MAX
            continue
        v.loosened = getattr(v, "loosened", 0) + 1
        v._key = None
        v.age = 0  # a fresh window to be judged in
        k += 1
        t.events.append(f"Loosened {v.label}: dropped {what} (no trades in a whole window)")
    return k


# ---------------- practice vs Alpaca paper: what real fills cost ----------------
FILL_MIN = 10      # paper copies before practice prices adjust to them
FILL_KEEP = 400


class FillCheckMixin:
    """With 'Both' on, every practice trade is also placed on Alpaca paper. Comparing the two prices shows how far
    practice fills are from real ones; practice then charges the difference (Settings > Learn from paper fills)."""

    def _fill_path(self):
        import sys
        return os.path.join(sys.modules["engine"].HOME_DIR, "fill-check.json")

    def fill_book(self):
        b = self.__dict__.get("_fill_book")
        if b is None:
            try:
                with open(self._fill_path(), encoding="utf-8") as f:
                    b = json.load(f)
            except (OSError, ValueError):
                b = []
            b = self._fill_book = [x for x in b if isinstance(x, dict)][-FILL_KEEP:]
        return b

    def fill_note_practice(self, c, side, px):
        """The practice side of a trade that may be copied to Alpaca paper (called as practice trades are logged)."""
        self.__dict__.setdefault("_fill_px", {})[c] = (side, float(px), time.time())

    def fill_note_paper(self, c, side, paper_px):
        """The paper side: its fill price, or None when the copy didn't fill."""
        p = (self.__dict__.get("_fill_px") or {}).get(c)
        if not p or p[0] != side or time.time() - p[2] > 900 or p[1] <= 0:
            return
        x = dict(t=round(time.time()), ticker=book_ticker(c), side=side, practice=round(p[1], 6), filled=paper_px is not None)
        if paper_px is not None:
            d = (paper_px - p[1]) / p[1] if side == "buy" else (p[1] - paper_px) / p[1]
            x["paper"], x["cost"] = round(float(paper_px), 6), round(d * 100, 3)  # cost: % worse on paper (negative = better)
        b = self.fill_book()
        b.append(x)
        del b[:-FILL_KEEP]
        try:
            with open(self._fill_path(), "w", encoding="utf-8") as f:
                json.dump(b, f)
        except OSError:
            pass

    def fill_stats(self, side=None):
        """-> dict(n, filled, rate, median cost %) of the paper copies (one side or both)."""
        b = [x for x in self.fill_book() if side in (None, x.get("side"))]
        costs = sorted(x["cost"] for x in b if x.get("filled") and "cost" in x)
        med = costs[len(costs) // 2] if costs else None
        filled = sum(1 for x in b if x.get("filled"))
        return dict(n=len(b), filled=filled, rate=filled / len(b) if b else None, median=med, priced=len(costs))

    def fill_adjust(self, side):
        """Extra cost (a fraction of the price) practice adds to a buy or sell, learned from the paper copies:
        0 until there are enough of them, or with the switch off."""
        if not self.settings.get("fill_learn", True):
            return 0.0
        st = self.fill_stats(side)
        if st["priced"] < FILL_MIN or st["median"] is None:
            return 0.0
        return max(-0.01, min(0.05, st["median"] / 100))

    def fill_text(self):
        st, b, s = self.fill_stats(), self.fill_stats("buy"), self.fill_stats("sell")
        if not st["n"]:
            return "No paper copies yet (turn on 'Both' so practice trades are also placed on Alpaca paper)."
        pc = lambda x: "-" if x is None else f"{x:+.2f}%"
        txt = (f"{st['n']} copies, {st['rate'] * 100:.0f}% filled. Real fills vs practice: buys {pc(b['median'])}, "
               f"sells {pc(s['median'])} (median; positive = real fills were worse).")
        if st["priced"] < FILL_MIN:
            return txt + f" Practice adjusts after {FILL_MIN} priced copies."
        return txt + (f" Practice now charges buys {self.fill_adjust('buy') * 100:+.2f}% and sells "
                      f"{self.fill_adjust('sell') * 100:+.2f}% to match." if self.settings.get("fill_learn", True)
                      else " (Learning from them is off in Settings.)")


# ---------------- earnings dates (stocks and options; crypto has none) ----------------
EARN_URL = "https://api.nasdaq.com/api/calendar/earnings?date={d}"  # one day of US earnings reports, free
_EARN = {"book": None, "index": None}


def _earn_path():
    import sys
    return os.path.join(sys.modules["engine"].HOME_DIR, "cache", "earnings.json")


def earnings_book():
    """{"days": {date: {symbol: 'bmo'|'amc'|'?'}}, "fetched": {date: when}} from the saved calendar."""
    if _EARN["book"] is None:
        try:
            with open(_earn_path(), encoding="utf-8") as f:
                b = json.load(f)
        except (OSError, ValueError):
            b = {}
        _EARN["book"] = dict(days=dict(b.get("days") or {}), fetched=dict(b.get("fetched") or {}))
        _EARN["index"] = None
    return _EARN["book"]


def _earn_save():
    p = _earn_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(earnings_book(), f)
    os.replace(p + ".tmp", p)


def parse_earnings_day(text):
    """{symbol: 'bmo'|'amc'|'?'} from one day of Nasdaq's earnings calendar (JSON text)."""
    try:
        rows = ((json.loads(text) or {}).get("data") or {}).get("rows") or []
    except (ValueError, AttributeError):
        return None
    out = {}
    for r in rows:
        sym = str(r.get("symbol") or "").strip().upper()
        if sym:
            tm = str(r.get("time") or "")
            out[sym] = "bmo" if "pre-market" in tm else "amc" if "after-hours" in tm else "?"
    return out


def fetch_earnings(dates, get, pause=0.4, refetch_after=12 * 3600):
    """Downloads the earnings calendar for `dates` (YYYY-MM-DD) that aren't saved yet (future days are refreshed every
    12 hours, since companies confirm or move their dates). get(url) -> text. -> how many days were downloaded."""
    b, now, n = earnings_book(), time.time(), 0
    today = datetime.now().strftime("%Y-%m-%d")
    for d in dates:
        at = b["fetched"].get(d)
        if at and (d < today or now - at < refetch_after):
            continue
        day = parse_earnings_day(get(EARN_URL.format(d=d)))
        if day is None:
            continue
        b["days"][d], b["fetched"][d] = day, now
        n += 1
        time.sleep(pause)
    if n:
        _EARN["index"] = None
        _earn_save()
    return n


def _earn_index():
    if _EARN["index"] is None:
        idx = {}
        for d, syms in earnings_book()["days"].items():
            for s, when in (syms or {}).items():
                ts = ny_time(d, 9, 30) if when == "bmo" else ny_time(d, 16, 0)  # after the close unless said otherwise
                idx.setdefault(s, []).append(ts)
        _EARN["index"] = {s: sorted(v) for s, v in idx.items()}
    return _EARN["index"]


def next_earnings(sym, ts):
    """When `sym` next reports earnings after time `ts` (a timestamp), or None if no report is known."""
    for x in _earn_index().get(book_ticker(sym).upper(), ()):
        if x > ts:
            return x
    return None


def earn_days_ahead(sym, ts):
    """Weekdays from `ts` until `sym`'s next earnings report (0 = it reports later today), or None if unknown."""
    nx = next_earnings(sym, ts)
    if nx is None:
        return None
    a = datetime.fromtimestamp(ts, timezone.utc) - timedelta(hours=_ny_offset(datetime.fromtimestamp(ts, timezone.utc)))
    b = datetime.fromtimestamp(nx, timezone.utc) - timedelta(hours=_ny_offset(datetime.fromtimestamp(nx, timezone.utc)))
    days, d = 0, a.date()
    while d < b.date():
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def _earn_get(url):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", "replace")


def earnings_loop(app, every=6 * 3600):
    """Background: keeps the earnings calendar for the next six weeks (and the last week) up to date, every 6 hours,
    unless Essentials only is on. Failures are quiet: without a calendar the earnings rules simply don't act."""
    while True:
        try:
            if not app.cfg.get("essentials"):
                today = datetime.now()
                dates = [(today + timedelta(days=k)).strftime("%Y-%m-%d") for k in range(-7, 43)
                         if (today + timedelta(days=k)).weekday() < 5]
                fetch_earnings(dates, _earn_get)
        except Exception:
            pass
        time.sleep(every)


def earnings_text(sym, ts=None):
    nx = next_earnings(sym, ts or time.time())
    if nx is None:
        return None
    d = datetime.fromtimestamp(nx)
    return f"{book_ticker(sym)} reports earnings {d:%a %b %d} " + ("before the open" if d.hour < 12 else "after the close")


# ---------------- heavy studies on their own processor core ----------------
def _core_state(mod):
    """The engine's settings-like globals (UPPERCASE plain data), so a study in another process sees what this one
    sees (prices learned, contract limits, the data folder...). Logins are never copied."""
    out = {}
    for k, v in vars(mod).items():
        if not k.isupper() or k.startswith("_") or any(w in k for w in ("ALPACA", "KEY", "SECRET", "TOKEN", "IB_")):
            continue
        if isinstance(v, (dict, list, tuple, int, float, str, bool)) and not callable(v):
            try:
                pickle.dumps(v)
            except Exception:  # tables holding functions (rule texts...) are the same in every process anyway
                continue
            out[k] = v
    return out


def _core_call(modname, fn, state, args, kw, ev, q):
    import importlib
    mod = importlib.import_module(modname)
    for k, v in state.items():
        cur = getattr(mod, k, None)
        if isinstance(cur, dict) and isinstance(v, dict):
            cur.clear()
            cur.update(v)
        else:
            setattr(mod, k, v)
    if ev is not None:
        kw["stop"] = ev.is_set
    if q is not None:
        kw["progress"] = lambda *a: q.put(a)
    return getattr(mod, fn)(*args, **kw)


class CoreRunner:
    """Runs heavy studies (training camps, Free roam's studies, hindsight, backtests) in worker processes, so they
    use other processor cores and the window stays smooth: in the app's own process they'd compete with the screen
    for the same core. A `stop` callback and a `progress` callback still work (through a small helper process).
    If a worker can't be used (or the switch is off), the study simply runs here as before."""

    def __init__(self, modname="engine", workers=2, enabled=lambda: True):
        self.modname, self.workers, self.enabled = modname, workers, enabled
        self._ex = self._mgr = None
        self._lock = threading.Lock()
        self.used = self.fallbacks = 0

    def _pool(self):
        with self._lock:
            if self._ex is None:
                import concurrent.futures as cf
                import multiprocessing as mp
                ctx = mp.get_context("spawn")
                self._ex = cf.ProcessPoolExecutor(max_workers=self.workers, mp_context=ctx)
                self._mgr = ctx.Manager()
            return self._ex, self._mgr

    @staticmethod
    def _direct_kw(kw, stop, progress):
        """The keyword arguments for running a study here: only the callbacks that were given (backtests take no
        `stop`, for example)."""
        out = dict(kw)
        if stop is not None:
            out["stop"] = stop
        if progress is not None:
            out["progress"] = progress
        return out

    def call(self, fn, *args, stop=None, progress=None, **kw):
        import sys
        mod = sys.modules[self.modname]
        if not self.enabled() or kw.get("on_trade") is not None:
            return fn(*args, **self._direct_kw(kw, stop, progress))
        try:
            ex, mgr = self._pool()
            ev = mgr.Event() if stop else None
            q = mgr.Queue() if progress else None
            fut = ex.submit(_core_call, self.modname, fn.__name__, _core_state(mod), args, kw, ev, q)
        except Exception:
            self.fallbacks += 1
            self.shutdown()
            return fn(*args, **self._direct_kw(kw, stop, progress))
        self.used += 1
        import concurrent.futures as cf
        while True:
            try:
                return fut.result(timeout=0.25)
            except cf.TimeoutError:
                pass
            except cf.process.BrokenProcessPool:
                self.fallbacks += 1
                self.shutdown()
                return fn(*args, **self._direct_kw(kw, stop, progress))
            if stop is not None and ev is not None and not ev.is_set():
                try:
                    if stop():
                        ev.set()
                except Exception:
                    pass
            while q is not None:
                try:
                    item = q.get_nowait()
                except Exception:
                    break
                try:
                    progress(*item)
                except Exception:
                    pass

    def shutdown(self):
        with self._lock:
            ex, mgr, self._ex, self._mgr = self._ex, self._mgr, None, None
        if ex is not None:  # a study still running would otherwise hold up closing the app: end it
            try:
                procs = list((getattr(ex, "_processes", None) or {}).values())
                ex.shutdown(wait=False, cancel_futures=True)
                for p in procs:
                    p.terminate()
            except Exception:
                pass
        if mgr is not None:
            try:
                mgr.shutdown()
            except Exception:
                pass


# ---------------- the farm's own bookkeeping, identical in every app ----------------
class FarmMixin:
    """Tournament methods that are the same in CBBF, OBF and SBF (ranking, sizes, families, replays, seasons...).
    Each app's Tournament inherits them, so a fix here reaches all three."""

    def window_for(self, tf):
        """How many bars a bot is judged over: slow bots get a longer window so they make enough decisions in it."""
        return self.W * min(max(1, clamp_tf(tf) // 2), 10)

    def mut_bias(self, kind):
        """Mutation memory: how much more (or less) often this kind of change has produced winners than average
        (0.5 to 2). 1 when off or with too little evidence."""
        if not self.o.get("mut_memory", True):
            return 1.0
        st = self.mut_stats.get(kind)
        tb = sum(x[0] for x in self.mut_stats.values())
        if not st or st[0] < 8 or tb < 30:
            return 1.0
        avg = (sum(x[1] for x in self.mut_stats.values()) + 1) / (tb + 2)
        return min(2.0, max(0.5, ((st[1] + 1) / (st[0] + 2)) / avg))

    def review_pick(self, pool):
        """Post-trade review: a new never-rule is more likely to be one that would have stopped recent losing buys."""
        if not self.o.get("post_review", True) or not self.review:
            return self.rng.choice(pool)
        w = [(self.review.get(k, [0, 0])[0] + 1) / (self.review.get(k, [0, 0])[1] + 1) for k in pool]
        return self.rng.choices(pool, weights=w)[0]

    def rand_ctx(self):
        sym = self.rng.choice(self.ctx_syms)
        if self.rng.random() < 0.4:
            return dict(self._rand_sub(), sym=sym, kind="study", sign=self.rng.choice((1, -1)))
        return dict(sym=sym, kind="move", sign=self.rng.choice((1, -1)), lb=self.rng.randint(2, 30),
                    th=round(self.rng.uniform(0.2, 3.0), 2))

    def _register(self, v, parents, origin):
        """Gives a bot its id and family-tree entry."""
        self.serial += 1
        v.uid, v.parents, v.origin = self.serial, [p.uid for p in parents], origin
        v.royal = origin in ("hall of fame", "library", "team", "collection", "saved", "bred from your bots", "designed") or \
            any(getattr(p, "royal", False) for p in parents)
        v.gen = 1 + max((p.gen for p in parents), default=-1) if parents else 0
        k_isl = int(self.o.get("islands", 1) or 1)
        if parents:
            v.island = parents[0].island
        elif k_isl > 1 and self.vars:  # newcomers join the smallest island
            sizes = [sum(1 for x in self.vars if x.island == i) for i in range(k_isl)]
            v.island = sizes.index(min(sizes))
        if parents:
            p0 = parents[0]
            kinds = [k for k, same in (("pace", v.tf == p0.tf), ("stops", v.risk == p0.risk), ("traits", v.traits == p0.traits),
                                       ("rules", v.vetoes == p0.vetoes), ("studies", v.layers == p0.layers),
                                       ("settings", v.params == p0.params)) if not same]
            if len(parents) > 1:
                kinds.append("crossbreed")
            v.mut_kinds = kinds
            for k in kinds:
                self.mut_stats.setdefault(k, [0, 0])[0] += 1
        self.births.append(self.n)
        self.lineage[v.uid] = dict(uid=v.uid, label=v.label, name=v.name, tf=v.tf, parents=list(v.parents),
                                   gen=v.gen, origin=origin, born=self.n, died=None, why="", ret=None, inducted=False)

    def mutate_tf(self, tf):
        """Usually keep the parent's pace; sometimes move one step faster or slower."""
        i = TIMEFRAMES.index(clamp_tf(tf))
        if self.rng.random() < min(0.8, 0.4 * self.mut_bias("pace")):
            i = min(max(i + self.rng.choice((-1, 1)), 0), len(TIMEFRAMES) - 1)
        return TIMEFRAMES[i]

    def hold_over(self, k):
        """What just holding the coins (equal weight) returned over the last k decisions, or None."""
        hc = self.hold_curve
        return hc[-1] / hc[-1 - k] - 1 if len(hc) > k else None

    def warmup(self, closes, start=WARM, progress=None, extra=None):
        L = min(len(closes[c]) for c in self.coins)
        for i in range(start, L):
            self.step({c: View(closes[c], i + 1) for c in self.coins}, slice_extra(extra, i + 1))
            if progress and (i - start) % 50 == 0:
                progress(i - start, L - start)

    @property
    def prove_n(self):
        """Decisions on fresh prices a bot needs before it may be followed as a winner."""
        return max(10, self.W // 4)

    def sortino(self, v):
        """Average gain per decision divided by the typical size of its losing decisions (higher = steadier)."""
        eq = list(v.eq)
        rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
        if len(rets) < 10:
            return 0.0
        mean = sum(rets) / len(rets)
        down = (sum(min(r, 0) ** 2 for r in rets) / len(rets)) ** 0.5
        if down <= 1e-12:
            return 5.0 if mean > 0 else 0.0
        return max(0.0, mean / down * (len(rets) ** 0.5) / 3)

    def drawdown(self, v):
        """How far the variant's equity sits below its recent peak (0..1)."""
        eq = list(v.eq)
        peak = max(eq) if eq else 0
        return (peak - eq[-1]) / peak if peak > 0 else 0.0

    def mood_fit(self, v, mood=None):
        """Average gain per decision while `mood` ruled, or None with too little evidence."""
        mood = mood or self.mood
        k = v.mood_n.get(mood, 0)
        return v.mood_gain.get(mood, 0.0) / k if k >= 30 else None

    def best_mood(self, v):
        fits = [(self.mood_fit(v, m), m) for m in MOODS]
        fits = [x for x in fits if x[0] is not None]
        return max(fits)[1] if fits and max(fits)[0] > 0 else None

    def health(self, v):
        """0..1: how the bot is doing right now. Falls with drawdown from its recent best; never-rules that keep
        saving money add a little, rules that keep blocking winners take some away. Very low health = it rests."""
        h = 1.0 - self.drawdown(v) * 8 if v.eq else 1.0
        bonus = 0.0
        for k in v.vetoes:
            st = v.veto_stats.get(k)
            if st and st[0] >= 3:
                bonus += max(-0.15, min(0.1, st[1] / st[0] / 20))
        return max(0.0, min(1.0, h + bonus))

    def update_power(self, v, edge):
        """Power grows only from real money made on fresh prices (not replays) while beating just holding, and
        fades slowly otherwise. It raises the bot's trade-size ceiling, its say in votes and its breeding odds."""
        if self.replaying or not v.eq:
            return
        e = v.eq[-1]
        if v.live_high is None:
            v.live_high = e
        if e > v.live_high and (edge is None or edge > 0):
            v.power = min(10.0, v.power + (e / v.live_high - 1) * 40)  # a slow, earned climb
            v.live_high = e
        elif e < v.live_high * 0.98:
            v.power = max(0.0, v.power - 0.02)

    def life(self, v):
        """Lifetime track record since the bot was created."""
        ret = v.eq[-1] / v.sim.start - 1 if v.eq and v.sim.start else 0.0
        hold = None
        if v.born_px and self.last_px:
            r = [self.last_px[c] / v.born_px[c] for c in self.coins if v.born_px.get(c) and c in self.last_px]
            hold = sum(r) / len(r) - 1 if r else None
        closed = v.wins + v.losses
        return dict(ret=ret, hold=hold, edge=None if hold is None else ret - hold, trades=v.ntrades,
                    wins=v.wins, losses=v.losses, winrate=v.wins / closed if closed else None,
                    maxdd=v.maxdd, age=v.age, win_steps=v.win_steps, streak=v.streak, best_mood=self.best_mood(v),
                    fees=v.sim.fees)

    def standins(self, keep=lambda v: True):
        """While no bot is proven: the best few winning bots that may trade the account in the meantime (at a smaller
        size). Each must be winning now, have 3+ finished trades, be ahead over its life and not have failed its exams;
        kamikazes never stand in. -> [bots], best lifetime record first."""
        n = int(self.o.get("standin_n", 0) or 0)
        if n <= 0:
            return []
        out = []
        for v in self.active:
            if not keep(v) or (getattr(v, "traits", None) or {}).get("kami") or (v.wins + v.losses) < 3:
                continue
            lf = self.life(v)
            if lf["ret"] <= 0 or exam_verdict(v, getattr(self, "serial", 100))[0] == "failed":
                continue
            out.append((lf["ret"], v))
        return [v for _, v in sorted(out, key=lambda x: -x[0])[:n]]

    def young(self, v):
        """Still within its first window of decisions: too new to judge, so never dropped to make room."""
        return self.score(v) is None and v.age < self.window_for(v.tf)

    def weakest(self):
        """Who goes first when room is needed, as (bot, score) pairs: bots that have had a full window and still
        can't be scored (they hardly trade) first, then the scored ones from the bottom of the ranking. Bots too young
        to judge are left alone, so new eggs get their chance and the farm doesn't fill with unjudged eggs, and so are
        contenders for a vote (see protected)."""
        keep = self.protected()
        stuck = sorted((v for v in self.vars if self.score(v) is None and not self.young(v) and id(v) not in keep),
                       key=lambda v: len(v.sim.tbars))
        out = [(v, None) for v in stuck] + [(v, x) for v, x in reversed(self.ranking()) if id(v) not in keep]
        if self.o.get("keep_variety", True):  # the last two of a strategy family go last: backups for other markets
            fam = {}
            for v in self.vars:
                fam[v.name] = fam.get(v.name, 0) + 1
            out = [x for x in out if fam[x[0].name] > 2] + [x for x in out if fam[x[0].name] <= 2]
        return out

    def trim_to_cap(self):
        """Brings an overfull farm back down to its size (bots that can't be judged, then the weakest). -> removed"""
        n = 0
        while len(self.vars) > self.cap:
            cands = [x for x, _ in self.weakest() if x not in self.active]
            if not cands:
                break
            self._remove(cands[0], "farm over its size")
            n += 1
        return n

    def nursery_full(self):
        """True when too many bots are still too young to judge (40% of the farm): extra eggs then wait."""
        return sum(1 for v in self.vars if self.young(v)) >= 0.4 * max(1, self.cap)

    def drivers(self):
        return list(self.active)

    def summary(self):
        """(number of active winners, best variant or None, any variant scored yet)"""
        scored = any(d["score"] is not None for d in self.info.values())
        return len(self.active), (self.active[0] if self.active else None), scored

    @staticmethod
    def _holding(v, c):
        return v.sim.pos.get(c, 0) > 0

    def proven_bots(self):
        return [v for v in self.vars if self.proven(v)]

    def board(self):
        """Rows for the scoreboard / farm. Cached until the ratings change, so drawing never recomputes it."""
        key = (id(self.info), self.n, len(self.vars))
        if getattr(self, "_board_key", None) == key:
            return self._board_rows
        self._board_rows = self._board()
        self._board_key = key
        return self._board_rows

    def crossover(self, name, a, b):
        """Child settings: each setting taken from one parent or the other, then lightly tweaked."""
        mix = {k: (a[k] if self.rng.random() < 0.5 else b.get(k, a[k])) for k in a}
        return self.mutate(name, mix, scale=0.1)

    def replay(self, v, closes):
        L = min(len(closes[c]) for c in self.coins)
        m = min(v.eq.maxlen, L - WARM)
        rec, self.recording = self.recording, False  # replayed history is not a new trade
        self.replaying = True
        try:
            x = self.xfull if self.xfull and all(len((self.xfull.get(c) or {}).get("h", ())) == len(closes[c])
                                                 for c in self.coins) else None
            for j in range(m):
                i = L - m + j
                self._run(v, {c: View(closes[c], i + 1) for c in self.coins}, self.n - (m - 1 - j),
                          extra=slice_extra(x, i + 1) if x else {})
        finally:
            self.recording = rec
            self.replaying = False

    def drop_twins(self):
        """Exact copies on the same island (identical settings, rules and layers, from migration, training camps or
        starter sets) waste farm places and vote twice for the same idea. The best-scoring copy stays."""
        groups = {}
        for v in self.vars:
            groups.setdefault((getattr(v, "island", 0), v.key()), []).append(v)
        gone = 0
        for vs in groups.values():
            if len(vs) < 2:
                continue
            vs.sort(key=lambda x: (self.score(x) is not None, self.score(x) or 0.0, -x.uid), reverse=True)
            for v in vs[1:]:
                if v in self.vars:
                    self._remove(v, "exact copy of another bot")
                    gone += 1
        if gone:
            self.events.append(f"Removed {gone} exact cop{'y' if gone == 1 else 'ies'} of other bots (the best copy stays)")
        return gone

    def seasons_tick(self, closes):
        """Keeps a champion per market mood. When the mood changes, the new season's champion is woken up
        (hatched again from its genes) if it's no longer on the farm."""
        for v in self.vars:
            for m in MOODS:
                fit = self.mood_fit(v, m)
                if fit is not None and fit > 0 and fit > self.champions.get(m, {}).get("fit", 0):
                    self.champions[m] = dict(name=v.name, params=dict(v.params), tf=v.tf, risk=dict(v.risk),
                                             traits=dict(v.traits), vetoes=dict(v.vetoes),
                                             layers=json.loads(json.dumps(v.layers)), fit=fit, uid=v.uid,
                                             label=v.label)
        mood = self.mood
        if mood == "unknown" or mood == self.season:
            self._season_cand = None
            return
        # a new season only starts once the new mood has held for a while (no flicker)
        cand = getattr(self, "_season_cand", None)
        if not cand or cand[0] != mood:
            self._season_cand = (mood, self.n)
            if self.season is not None:
                return
        elif self.n - cand[1] < max(20, self.evolve_every * 2):
            return
        old, self.season = self.season, mood
        self.seasons_log.append((self.n, mood))
        del self.seasons_log[:-50]
        if old is None:
            return
        ch = self.champions.get(mood)
        if not ch:
            self.events.append(f"Season change: {mood}. No champion for it yet")
            return
        if any(x.uid == ch["uid"] for x in self.vars):
            self.events.append(f"Season change: {mood}. Its champion {ch['label']} is already on the farm")
            return
        v = self.add_variant(ch["name"], ch["params"], closes, note=f"Season change: {mood}. Woke its champion",
                             tf=ch["tf"], origin="season champion", risk=ch["risk"], traits=ch["traits"],
                             vetoes=ch["vetoes"], layers=ch["layers"])
        if v:
            ch["uid"] = v.uid
            if len(self.vars) > self.cap:
                cands = [x for x, _ in self.weakest() if x not in self.active and x is not v]
                if cands:
                    self._remove(cands[0], "dropped to make room")

    def _spawn_or_random(self, closes):
        before = len(self.vars)
        self._spawn(closes)
        if len(self.vars) == before and len(self.vars) < self.cap:
            self.spawn_random(closes)

    def _rotate(self, closes):
        """Replaces bots that have gone retire_steps decisions without winning (losing, or never trading)."""
        loosen_silent(self)  # silent bots first lose a gate and get another window (farmcore)
        gone = []
        for v in self.vars:
            st = self.info.get(id(v), {}).get("status")
            v.bad = 0 if st in ("active", "slowed", "standby") else getattr(v, "bad", 0) + 1
            if v.bad >= self.retire_steps and len(self.vars) - len(gone) > 8:
                gone.append(v)
        for v in gone:
            self._remove(v, "rotated out (not winning for a long time)")
            self.events.append(f"Rotated out {v.label} (not winning for a long time)")
        for _ in gone:
            self._spawn_or_random(closes)
        if gone:
            self._rate()

    def dominant(self):
        """The strategy family that fills more than half of the farm, or None."""
        if len(self.vars) < 12:
            return None
        fam = {}
        for v in self.vars:
            fam[v.name] = fam.get(v.name, 0) + 1
        name, n = max(fam.items(), key=lambda x: x[1])
        return name if n / len(self.vars) > 0.5 else None

    def spawn_classic(self, closes=None):
        """Hatches one famous strategy that isn't on the farm yet (silver egg). None if all are already here."""
        have = {v.classic for v in self.vars}
        pool = [c for c in CLASSICS if c["title"] not in have]
        if not pool:
            return None
        cl = self.rng.choice(pool)
        v = self.add_variant(cl["name"], dict(cl["params"]), closes, note=f"Classic strategy hatched: {cl['title']}:",
                             tf=cl.get("tf", 1), origin="classic", risk=cl.get("risk"), traits=cl.get("traits"))
        if v:
            v.classic = cl["title"]
        return v

    def hall_of_fame_candidates(self, min_win_steps, min_trades):
        """Bots that have earned a place: enough time Winning, enough finished trades, and ahead of
        both zero and just holding over their whole life."""
        out = []
        for v in self.vars:
            if v.inducted or v.win_steps < min_win_steps:
                continue
            lf = self.life(v)
            if (v.wins + v.losses) >= min_trades and lf["ret"] > 0 and (lf["edge"] is None or lf["edge"] > 0):
                out.append(v)
        return out

    def export_lineup(self):
        """Strategies worth keeping (everything not stopped), as plain data for saving."""
        keep = [v for v in self.vars if self.info.get(id(v), {}).get("status") != "benched"] or list(self.vars)
        return [dict(name=v.name, params=dict(v.params), size=v.size_mult, tf=v.tf, risk=dict(v.risk), traits=dict(v.traits))
                for v in keep]

    def family(self):
        """Family-tree records for living and recently removed bots, with live stats for the living."""
        alive = {v.uid: v for v in self.vars}
        out = []
        for uid, rec in self.lineage.items():
            r = dict(rec)
            v = alive.get(uid)
            if v:
                r.update(label=v.label, alive=True, status=self.info.get(id(v), {}).get("status", "warming up"),
                         ret=v.eq[-1] / v.sim.start - 1 if v.eq else None, inducted=v.inducted)
            else:
                r.update(alive=False, status=rec["why"] or "removed")
            out.append(r)
        return out

    def pop_shadow(self):
        s, self.shadow = self.shadow, []
        return s

    def pop_events(self):
        e, self.events = self.events, []
        return e

    def align_coins(self, coins, last=None):
        """Makes the farm trade exactly these coins (a safety net if they ever get out of step with the session).
        Positions in dropped coins are closed at their last price. Returns the new coin list."""
        last = dict(self.last_px, **(last or {}))
        gone = [c for c in self.coins if c not in coins]
        for v in self.vars:
            for c in gone:
                q = v.sim.pos.get(c, 0)
                if q > 0 and last.get(c):
                    v.sim.sell(c, last[c])
                v.sim.pos.pop(c, None)
                v.sim.cost.pop(c, None)
                v.sim.last = None
            if v.born_px:
                v.born_px = {c: x for c, x in v.born_px.items() if c in coins}
        self.coins = list(coins)
        return self.coins


# ---------------- the layout: a rail of five sections over the app's tabs ----------------
NAV_SECTIONS = (  # section -> the tabs it holds (matched by name), then windows it opens: (label, method)
    ("Home", ("Home",), ()),
    ("Trading", ("Portfolio", "Options", "Trades", "Alpaca account"), ()),
    ("Bots", ("Bot Farm", "Slow farm", "Scalpers", "Success", "Bot positions"),
     (("Library & Hall of Fame...", "open_collection"),)),
    ("Learning", ("Overview",), (("Free roam...", "open_roam"), ("Training camp...", "open_training"),
                                 ("Farm knowledge...", "open_knowledge"))),
    ("Markets", ("Markets",), ()),
)
NAV_WIDE = ("Home", "Trading", "Markets")  # sections that keep the account panel and the charts


class NavMixin:
    """Home plus four sections on a left rail (Trading, Bots, Learning, Markets). The app's tabs stay as they are; the
    rail picks which ones show, as pills along the top. Settings > Classic layout brings the old tab row back."""

    def build_nav(self):
        import tkinter as tk
        from tkinter import ttk
        if not self.settings.get("rail_nav", True):
            return
        c = self._doctor_colors()
        nb, split = self.nb, self.split
        self._nav_right = split.master
        body = self._nav_right.master
        st = ttk.Style()
        st.layout("Rail.TNotebook.Tab", [])  # the notebook's own tab row is replaced by the rail and the pills
        st.configure("Rail.TNotebook", borderwidth=0, tabmargins=0)
        nb.configure(style="Rail.TNotebook")
        home = self._home = tk.Frame(nb, bg=c["BG"])
        nb.insert(0, home, text="  ⌂ Home  ")
        self._build_home(home)
        rail = self._rail = tk.Frame(body, bg=c["CARD"], width=150)
        first = body.pack_slaves()[0] if body.pack_slaves() else None
        rail.pack(side="left", fill="y", padx=(0, 10), before=first)
        self._nav_btns = {}
        for name, _, _ in NAV_SECTIONS:
            b = tk.Button(rail, text=name, anchor="w", relief="flat", bd=0, padx=16, pady=10, cursor="hand2",
                          font=("", 10, "bold"), command=lambda n=name: self.nav_go(n))
            b.pack(fill="x", padx=6, pady=(6 if name == "Home" else 2, 0))
            self._nav_btns[name] = b
        self._pills = tk.Frame(nb.master, bg=c["BG"])
        self._pills.pack(fill="x", pady=(6, 0), before=nb)
        nb.bind("<<NotebookTabChanged>>", lambda e: self._nav_sync(), add="+")
        self._nav_sec = None
        self.nav_go("Home")
        self.root.after(3000, self._home_tick)

    def _nav_tab_ids(self, names):
        out = []
        for want in names:
            for t_ in self.nb.tabs():
                if want.lower() in self.nb.tab(t_, "text").lower() and t_ not in out:
                    out.append(t_)
        return out

    def _section_of(self, tab_id):
        for name, tabs, _ in NAV_SECTIONS:
            if tab_id in self._nav_tab_ids(tabs):
                return name
        return "Trading"  # anything not listed (an app's own extra tab) lives with the trading screens

    def nav_go(self, name, tab=None):
        """Shows one section: its first tab (or `tab`), its pills, and the account panel and charts where they belong."""
        ids = self._nav_tab_ids(dict((n, t) for n, t, _ in NAV_SECTIONS)[name])
        if name == "Trading":
            known = {t for _, tabs, _ in NAV_SECTIONS for t in self._nav_tab_ids(tabs)}
            ids += [t for t in self.nb.tabs() if t not in known]
        target = tab if tab in ids else (getattr(self, "_nav_last", {}).get(name) if getattr(self, "_nav_last", {}).get(name) in ids else (ids[0] if ids else None))
        self._nav_sec = name
        if target is not None and self.nb.select() != target:
            self.nb.select(target)
        self._nav_layout(name)
        self._nav_paint(name, ids)

    def _nav_sync(self):
        """A tab chosen some other way (a button, a link, the keyboard): the rail follows."""
        if not getattr(self, "_rail", None):
            return
        cur = self.nb.select()
        name = self._section_of(cur)
        self.__dict__.setdefault("_nav_last", {})[name] = cur
        if name != self._nav_sec:
            self._nav_sec = name
            self._nav_layout(name)
        ids = self._nav_tab_ids(dict((n, t) for n, t, _ in NAV_SECTIONS)[name])
        if name == "Trading":
            known = {t for _, tabs, _ in NAV_SECTIONS for t in self._nav_tab_ids(tabs)}
            ids += [t for t in self.nb.tabs() if t not in known]
        self._nav_paint(name, ids)

    def _nav_layout(self, name):
        """The account panel and the charts show in Home, Trading and Markets; the other sections get the room."""
        wide = name in NAV_WIDE
        left = getattr(self, "left_scroll", None)
        if left is not None:
            if wide and not left.winfo_ismapped():
                left.pack(side="left", fill="y", padx=(0, 10), before=self._nav_right)
            elif not wide and left.winfo_ismapped():
                left.pack_forget()
        cc, split = getattr(self, "chart_card", None), self.split
        if cc is not None:
            shown = str(cc) in [str(p) for p in split.panes()]
            if wide and not shown:
                split.add(cc, before=self.nb.master, minsize=150, stretch="never",
                          height=getattr(self, "_chart_h", 250))
            elif not wide and shown:
                self._chart_h = max(150, cc.winfo_height())
                split.forget(cc)

    def _nav_paint(self, name, ids):
        import tkinter as tk
        c = self._doctor_colors()
        for n, b in self._nav_btns.items():
            on = n == name
            b.config(bg=c["ACCENT"] if on else c["CARD"], fg="white" if on else c["TEXT"],
                     activebackground=c["ACCENT"] if on else c["LINE"])
        for ch in self._pills.winfo_children():
            ch.destroy()
        openers = dict((n, o) for n, _, o in NAV_SECTIONS)[name]
        if name == "Home" or (len(ids) <= 1 and not openers):
            return
        cur = self.nb.select()
        for t_ in ids:
            on = t_ == cur
            txt = self.nb.tab(t_, "text").strip()
            tk.Button(self._pills, text=txt, relief="flat", bd=0, padx=14, pady=5, cursor="hand2", font=("", 9, "bold"),
                      bg=c["ACCENT"] if on else c["CARD"], fg="white" if on else c["TEXT"],
                      activebackground=c["LINE"], command=lambda t=t_: self.nav_go(name, t)).pack(side="left", padx=(0, 6))
        for label, meth in openers:
            fn = getattr(self, meth, None)
            if fn is not None:
                tk.Button(self._pills, text=label, relief="flat", bd=0, padx=12, pady=5, cursor="hand2", font=("", 9),
                          bg=c["BG"], fg=c["ACCENT"], activebackground=c["LINE"],
                          command=fn).pack(side="left", padx=(0, 6))

    # ---- Home ----
    def _build_home(self, f):
        import tkinter as tk
        c = self._doctor_colors()
        f.configure(padx=4, pady=8)
        row = tk.Frame(f, bg=c["BG"])
        row.pack(fill="x")
        self._home_cards = {}
        for i, (key, title) in enumerate((("run", "IS IT RUNNING?"), ("proven", "IS ANYONE PROVEN?"),
                                          ("needs", "WHAT NEEDS YOU"))):
            card = tk.Frame(row, bg=c["CARD"], padx=14, pady=12)
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 10, 0))
            row.grid_columnconfigure(i, weight=1, uniform="home")
            tk.Label(card, text=title, bg=c["CARD"], fg=c["MUTED"], font=("", 8, "bold")).pack(anchor="w")
            big = tk.Label(card, text="", bg=c["CARD"], fg=c["TEXT"], font=("", 15, "bold"), anchor="w", justify="left")
            big.pack(anchor="w", fill="x", pady=(2, 0))
            body = tk.Frame(card, bg=c["CARD"])
            body.pack(fill="both", expand=True, pady=(4, 0))
            self._home_cards[key] = (card, big, body)
        low = tk.Frame(f, bg=c["BG"])
        low.pack(fill="both", expand=True, pady=(10, 0))
        self._home_low = {}
        for i, (key, title) in enumerate((("trades", "Latest trades"), ("learn", "Learning right now"))):
            card = tk.Frame(low, bg=c["CARD"], padx=14, pady=10)
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 10, 0))
            low.grid_columnconfigure(i, weight=1, uniform="low")
            low.grid_rowconfigure(0, weight=1)
            tk.Label(card, text=title, bg=c["CARD"], fg=c["TEXT"], font=("", 10, "bold")).pack(anchor="w")
            body = tk.Frame(card, bg=c["CARD"])
            body.pack(fill="both", expand=True, pady=(4, 0))
            self._home_low[key] = body
        self.refresh_home()

    def _home_tick(self):
        try:
            if getattr(self, "_home", None) is not None and self.nb.select() == str(self._home) and not self.in_bg():
                self.refresh_home()
        except Exception:
            pass
        if not self.closing():
            self.root.after(3000, self._home_tick)

    def refresh_home(self):
        import tkinter as tk
        c = self._doctor_colors()
        lbl = lambda parent, text, fg=None, bold=False: tk.Label(
            parent, text=text, bg=c["CARD"], fg=fg or c["TEXT"], anchor="w", justify="left", wraplength=360,
            font=("", 9, "bold" if bold else "normal")).pack(fill="x", anchor="w")

        def clear(w):
            for ch in w.winfo_children():
                ch.destroy()
        # is it running?
        _, big, body = self._home_cards["run"]
        clear(body)
        running = bool(getattr(self, "running", False))
        acct = self.acct_name() if hasattr(self, "acct_name") else ""
        big.config(text=(f"Running · {acct.title()}" if running else "Stopped"), fg=c["GREEN"] if running else c["MUTED"])
        try:
            eq = self.equity()
            day0 = getattr(self, "day_eq", None)
            d = eq - day0 if day0 else 0.0
            lbl(body, f"Account ${eq:,.2f} · today {d:+,.2f}", c["GREEN"] if d > 0 else c["RED"] if d < 0 else None)
        except Exception:
            pass
        mkt = getattr(self, "market", None)
        if isinstance(mkt, dict):
            lbl(body, "Market open" if mkt.get("is_open") else "Market closed", c["MUTED"])
        tk.Button(body, text="Stop the bot" if running else "Start the bot", relief="flat", bd=0, padx=12, pady=5,
                  cursor="hand2", bg=c["RED"] if running else c["GREEN"], fg="white",
                  command=lambda: self.btn.invoke()).pack(anchor="w", pady=(8, 0))
        # is anyone proven?
        _, big, body = self._home_cards["proven"]
        clear(body)
        t = getattr(self, "tourn", None)
        if t is None:
            big.config(text="No farm yet", fg=c["MUTED"])
            lbl(body, "Press Start: the farm hatches and starts learning.", c["MUTED"])
        else:
            pv = t.proven_bots() if hasattr(t, "proven_bots") else []
            big.config(text=f"{len(pv)} proven bot{'s' if len(pv) != 1 else ''}", fg=c["GREEN"] if pv else c["TEXT"])
            rk = t.ranking()
            if rk:
                v, sc = rk[0]
                lbl(body, f"Leading: {bot_nickname(v.key())} ({v.name}) {sc * 100:+.1f}%")
            si = t.standins() if not pv and hasattr(t, "standins") else []
            if si:
                lbl(body, "Standing in at a smaller size: " + ", ".join(bot_nickname(v.key()) for v in si), "#f59e0b")
            if not pv and hasattr(t, "proven_gaps"):
                near = sorted(((len(g), v, g) for v in t.vars for g in [t.proven_gaps(v)] if g), key=lambda x: x[0])[:1]
                for _, v, g in near:
                    lbl(body, f"Closest: {bot_nickname(v.key())} needs {', '.join(g[:2])}", c["MUTED"])
            lbl(body, f"{len(t.vars)} bots on the farm", c["MUTED"])
            tk.Button(body, text="See the bots", relief="flat", bd=0, padx=12, pady=5, cursor="hand2", bg=c["LINE"],
                      fg=c["TEXT"], command=lambda: self.nav_go("Bots")).pack(anchor="w", pady=(8, 0))
        # what needs you
        card, big, body = self._home_cards["needs"]
        clear(body)
        found = [f for f in self.doctor_findings() if f["level"] in ("bad", "warn")]
        big.config(text="All good" if not found else f"{len(found)} thing{'s' if len(found) != 1 else ''}",
                   fg=c["GREEN"] if not found else "#f59e0b")
        for x in found[:3]:
            r = tk.Frame(body, bg=c["CARD"])
            r.pack(fill="x", pady=2)
            tk.Label(r, text=x["title"], bg=c["CARD"], fg=c["TEXT"], anchor="w", justify="left", wraplength=260,
                     font=("", 9)).pack(side="left", fill="x", expand=True)
            if x.get("fix"):
                tk.Button(r, text=x["fix_label"] or "Fix", relief="flat", bd=0, padx=8, pady=2, cursor="hand2",
                          bg=c["ACCENT"], fg="white", command=lambda k=x["fix"]: (self.doctor_fix(k), self.refresh_home())
                          ).pack(side="right")
        if not found:
            lbl(body, "The farm doctor checks every minute.", c["MUTED"])
        # latest trades
        body = self._home_low["trades"]
        clear(body)
        rows = list(getattr(self, "trades", []) or [])[-6:][::-1]
        if not rows:
            lbl(body, "No account trades yet. The account only trades when a proven bot says so.", c["MUTED"])
        for r in rows:
            pl = r.get("pl")
            txt = f"{str(r.get('time', ''))[11:16]}  {'Bought' if r.get('side') == 'buy' else 'Sold'} {r.get('coin', '')}"
            line = tk.Frame(body, bg=c["CARD"])
            line.pack(fill="x")
            tk.Label(line, text=txt, bg=c["CARD"], fg=c["TEXT"], anchor="w", font=("", 9)).pack(side="left")
            if pl not in ("", None):
                tk.Label(line, text=f"{float(pl):+,.2f}", bg=c["CARD"], fg=c["GREEN"] if float(pl) >= 0 else c["RED"],
                         font=("", 9, "bold")).pack(side="right")
        tk.Button(body, text="All trades", relief="flat", bd=0, padx=10, pady=3, cursor="hand2", bg=c["LINE"], fg=c["TEXT"],
                  command=lambda: self.nav_go("Trading", (self._nav_tab_ids(("Trades",)) or [None])[0])).pack(anchor="w", pady=(6, 0))
        # learning right now
        body = self._home_low["learn"]
        clear(body)
        on = lambda x: ("On", c["GREEN"]) if x else ("Off", c["MUTED"])
        items = [("Free roam", self.cfg.get("roam_on")), ("Auto camp", self.cfg.get("auto_camp"))]
        sl = getattr(self, "slow", None)
        if isinstance(sl, dict):
            items.append(("Slow farm", bool(sl.get("t")) and bool(sl.get("on", sl.get("running", False)))))
        if self.cfg.get("essentials"):
            lbl(body, "Essentials only is on: nothing learns in the background.", "#f59e0b")
        for nm, val in items:
            txt, col = on(val)
            line = tk.Frame(body, bg=c["CARD"])
            line.pack(fill="x")
            tk.Label(line, text=nm, bg=c["CARD"], fg=c["TEXT"], anchor="w", font=("", 9)).pack(side="left")
            tk.Label(line, text=txt, bg=c["CARD"], fg=col, font=("", 9, "bold")).pack(side="right")
        now_ = getattr(self, "roam_now", None)
        if isinstance(now_, dict) and now_.get("text"):
            lbl(body, now_["text"], c["MUTED"])
        tk.Button(body, text="Everything in Learning", relief="flat", bd=0, padx=10, pady=3, cursor="hand2", bg=c["LINE"],
                  fg=c["TEXT"], command=lambda: self.nav_go("Learning")).pack(anchor="w", pady=(6, 0))
