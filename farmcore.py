"""Shared core of the bot farm apps (CBBF, OBF, SBF): code that is identical in all three.

This file is the same in every app. Change it in one app, then run tools/sync_core.py to copy it to the others
(tests/test_core.py fails in any app whose copy differs from the recorded fingerprint).
"""
import csv
import json
import math
import threading
import os
import re
import shutil
import time
from datetime import datetime, timezone


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
