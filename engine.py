"""Core logic for SBF (Stock Bot Farm): strategies, simulation, wildcard tournament,
historical candles and local storage. No GUI code lives here (and no order code: that's broker.py).

SBF grew out of OBF (the options farm), so some option code is still here (OptionSim, option chains, pricing).
SBF itself never trades options: every bot gets a plain share account (Sim, see OPTIONS below)."""
import csv
import bisect
import json
import threading
import math
import os
import importlib
import importlib.util
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone

from farmcore import (  # noqa: F401  shared with the other bot farm apps (see farmcore.py)
    LIMIT_DEFAULTS,
    BAR_CHOICES, BIG_EVENTS, BOT_FORMAT_VERSION, CANDLE_GRANS, CLASSICS, COLLECT_FIELDS, CTX_KEY, DISK_T0,
    DISK_WRITES, EVENT_KINDS, EXAM_KEEP, EXAM_MIN, FEATURE_CLEAR, FEATURE_KEEP, FEATURE_LEAN, FEATURE_MIN,
    FEATURE_SKIP, FEATURE_VERDICT_TEXT, HTF_MULTS, LOOKBACK, MAX_BOT_FILE, MAX_CTX, MAX_VETOES, MEMORY_FIELDS, MOODS,
    RARITIES, RARITY_COLORS, SECRET_CFG, SECRET_SETTINGS, SOUNDS, STABLES, StretchPool, TEAM_MAX, TIMEFRAMES,
    TRADE_FIELDS, TrainingStopped, UPDATE_FILES, View, WARM, _ADJ, _AVAIL, _BEAST, _EVENTS, _PACE, _PACE_LOCK,
    _TX_CACHE, _avg, _bar_time, _clean_memory, _ema, _genes, _iso, _merge_spans, _merge_stats, _missing, _noted,
    _num, _ny_offset, _requirement_lines, _rsi, _stat_txt, _sub_txt, _tail_extra, add_exam, alerts_configured,
    backtest_summary, bot_nickname, changelog_since, clamp_tf, common_stretch, ctx_text, data_now, exam_bar,
    exam_trades, exam_verdict, export_trades_csv, farm_pick_score, feature_verdict, fmt_bar, is_shiny,
    keyring_available, latest_backup, level_for, lifetime_record, load_json, market_mood, metrics, new_requirements,
    note_write, ny_time, pick_feature, private_dir, rarity_for, record_feature, rel_edge, restore_backup, risk_text,
    save_bots_file, sha256_from_file, sig_atr, sig_boll, sig_breakout, sig_dca, sig_ema, sig_ichimoku, sig_macd,
    sig_mom, sig_revert, sig_ribbon, sig_roc, sig_rsi, sig_sma, sig_stoch, sig_supertrend, sig_trend_dip,
    sig_volbreak, sig_vwap, slice_extra, tail_extra, tail_series, to_dict, version_tuple, xp_for,
)

HOME_DIR = os.path.join(os.path.expanduser("~"), ".sbf")
OLD_HOME_DIR = os.path.join(os.path.expanduser("~"), ".sbf-old")  # (no older data folder to bring across)


def migrate_home():
    """First start after the rename: copy the old data folder (sessions, trades, saved bots) across.
    The old folder is left in place untouched. Returns True if something was copied."""
    try:
        if not os.path.exists(HOME_DIR) and os.path.isdir(OLD_HOME_DIR):
            shutil.copytree(OLD_HOME_DIR, HOME_DIR)
            return True
    except OSError:
        pass
    return False


def candle_gran(bar_minutes):
    """Closest candle size for a bar length. Sub-minute bars use 1-minute candles (nothing shorter is offered),
    so their history is approximate until live bars replace it."""
    sec = float(bar_minutes) * 60
    return min(CANDLE_GRANS, key=lambda g: (abs(g - sec), g)) if sec >= 60 else 60


def strat_signal(name, p, n, params, tf=1, x=None):
    """One strategy's signal on the newest bar, using tf-bar candles. Strategies that need highs,
    lows or volume get them when available and stay quiet when not."""
    spec = STRATS[name]
    series = tail_series(p, tf, spec.get("keep", LOOKBACK))
    if spec.get("hlv"):
        return spec["fn"](series, n // tf if tf > 1 else n, params,
                          tail_extra(p, x, tf, spec.get("keep", LOOKBACK)))
    return spec["fn"](series, n // tf if tf > 1 else n, params)


# ---------------- feature tests: what each kind of learning is worth ----------------
# switches that act outside practice farms (Free roam's hindsight studies, live trading, exams from Free roam):
# a practice farm can't show what they're worth, so they're not tested this way


def feature_candidates(settings, keys):
    """The learning switches (from `keys`) that are on and actually change how a practice farm learns."""
    base = tournament_opts(settings)
    out = [k for k in keys if k not in FEATURE_SKIP and settings.get(k) is True
           and tournament_opts(dict(settings, **{k: False})) != base]
    tournament_opts(settings)  # (it also sets a few shared values: put them back)
    return out


def feature_test(closes, extra, settings, key, make_opts, seed=None, progress=None, **train_kw):
    """Runs the same practice farm twice on the same stretch, from the same seed bots and random seed: once as the
    settings are, once with `key` switched off. -> dict(key, on, off) (exam scores, see farm_pick_score), with
    on/off None when that run's picks didn't trade enough to judge."""
    out = dict(key=key)
    for i, (arm, s) in enumerate((("on", settings), ("off", dict(settings, **{key: False})))):
        def prog(done, total, text, i=i):
            if progress:
                progress(i * total + done, 2 * total, f"run {i + 1} of 2 ({'with' if i == 0 else 'without'} it): {text}")
        res = train_farm(closes, extra, make_opts(s), seed=seed, progress=prog, **train_kw)
        out[arm] = farm_pick_score(res)
    make_opts(settings)  # (put the shared values back)
    return out


# ---------------- Free roam: reuse downloaded history ----------------


# ---------------- errors the app recovered from ----------------


def note_error(where):
    """Records the exception being handled in errors.log, for errors the app carries on after (a failed save, a
    dropped connection). Each kind is written at most once every 10 minutes and the file stays under ~1 MB, so a
    repeating problem can't flood it. Never raises."""
    try:
        import traceback
        et, ev, _ = sys.exc_info()
        key = (where, et.__name__ if et else "")
        now = time.time()
        if now - _noted.get(key, 0) < 600:
            return
        _noted[key] = now
        os.makedirs(HOME_DIR, exist_ok=True)
        path = os.path.join(HOME_DIR, "errors.log")
        if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
            os.replace(path, path + ".old")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"--- {datetime.now():%Y-%m-%d %H:%M:%S} {where}\n{traceback.format_exc()}\n")
    except Exception:
        pass


# ---------------- network helpers ----------------
def http_get(url, timeout=10, retries=0):
    """GET with a few automatic retries for brief network blips, rate limits (429) and server errors."""
    delay = 1.0
    for attempt in range(retries + 1):
        wait = delay
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sbf"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as ex:
            if ex.code not in (429, 500, 502, 503, 504) or attempt >= retries:
                raise
            try:
                wait = min(max(float(ex.headers.get("Retry-After", delay)), delay), 10.0)
            except (TypeError, ValueError):
                pass
        except (urllib.error.URLError, OSError):  # timeouts, DNS, dropped connections
            if attempt >= retries:
                raise
        time.sleep(wait)
        delay *= 2


# ---------------- Alpaca: stock prices, history, market hours, account (orders live in broker.py) ----------------
ALPACA = {"key": "", "secret": "", "paper": True}  # filled in when you log in (data needs a free Alpaca key too)
DATA_URL = "https://data.alpaca.markets"
FEED = "iex"  # the free real-time stock feed; "sip" with a paid data plan
HIST_SIP = True  # history older than 15 minutes from the full market feed (free); the newest bars stay on FEED
DATA_PER_MIN = 190  # Alpaca's free plan allows 200 data calls a minute


def _pace():
    """Keeps data calls under the free plan's limit, so long downloads slow down instead of failing."""
    while True:
        with _PACE_LOCK:
            now = time.time()
            calls = [t for t in _PACE["calls"] if now - t < 60]
            _PACE["calls"] = calls
            if len(calls) < DATA_PER_MIN:
                calls.append(now)
                return
            wait = 60 - (now - calls[0]) + 0.05
        time.sleep(min(max(wait, 0.05), 5))


def trade_url():
    return "https://paper-api.alpaca.markets" if ALPACA.get("paper", True) else "https://api.alpaca.markets"


class NeedLogin(Exception):
    pass


class AlpacaError(RuntimeError):
    """Alpaca answered with an HTTP error. `code` is the status (422 = rejected, 5xx = their side had trouble)."""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def alp(method, url, body=None, timeout=12, retries=1, creds=None):
    """One Alpaca API call (JSON in, JSON out)."""
    cr = creds or ALPACA
    if not cr.get("key") or not cr.get("secret"):
        raise NeedLogin("Log in with your Alpaca API key first (Account > Log in). Paper keys are free.")
    data = None if body is None else json.dumps(body).encode()
    if url.startswith(DATA_URL):
        _pace()
    delay = 1.0
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "APCA-API-KEY-ID": cr["key"].strip(), "APCA-API-SECRET-KEY": cr["secret"].strip(),
            "Content-Type": "application/json", "User-Agent": "sbf"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as ex:
            if ex.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            try:
                msg = json.loads(ex.read() or b"{}").get("message") or ex.reason
            except Exception:
                msg = ex.reason
            raise AlpacaError(ex.code, f"Alpaca said {ex.code}: {msg}") from None
        except (urllib.error.URLError, OSError):
            if attempt >= retries:
                raise
            time.sleep(delay)
            delay *= 2


def spot(coin, quote="USD"):
    return spot_many([coin]).get(coin)


def spot_many(coins, quote="USD"):
    """Latest trade price for many tickers in one request. Missing ones are left out."""
    if not coins:
        return {}
    q = urllib.parse.quote(",".join(coins))
    data = alp("GET", f"{DATA_URL}/v2/stocks/trades/latest?symbols={q}&feed={FEED}")
    out = {}
    for c, t in (data.get("trades") or {}).items():
        try:
            if float(t["p"]) > 0:
                out[c] = float(t["p"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _tf(gran):
    return {60: "1Min", 300: "5Min", 900: "15Min", 3600: "1Hour", 21600: "6Hour", 86400: "1Day"}.get(int(gran), "1Min")


def _candle_cache_path(coin, quote, gran):
    if not re.fullmatch(r"[A-Z0-9.\-]{1,12}", str(coin)):
        raise ValueError(f"not a ticker: {coin!r}")
    return os.path.join(HOME_DIR, "cache", f"{coin}-{int(gran)}.json")


def _iso_ts(s_):
    return datetime.fromisoformat(s_.replace("Z", "+00:00")).timestamp()


def fetch_ohlcv(coin, gran_seconds, count, quote="USD", cache=True):
    """{'c','h','l','v','t'} of the last `count` bars (oldest first). Stocks only trade ~6.5 hours a day, so the
    request reaches back far enough in calendar time. Bars are cached on disk; later calls only fetch what's new."""
    rows, newest = {}, None
    path = _candle_cache_path(coin, quote, gran_seconds)
    if cache:
        try:
            with open(path) as f:
                rows = {int(t): tuple(r) for t, r in json.load(f).items()}
            newest = max(rows) if rows else None
        except (OSError, ValueError, TypeError, AttributeError):
            rows, newest = {}, None
    per_day = max(1, int(6.5 * 3600 // gran_seconds)) if gran_seconds < 86400 else 1
    days = int(count / per_day * 7 / 5) + 5
    start = datetime.now(timezone.utc) - timedelta(days=days)
    if newest is not None and len(rows) >= count:
        start = datetime.fromtimestamp(newest, timezone.utc)
    base = (f"{DATA_URL}/v2/stocks/{urllib.parse.quote(coin)}/bars?timeframe={_tf(gran_seconds)}&limit=10000"
            f"&adjustment=split&start={start.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    # history from the full market (every exchange; free when it's older than 15 minutes), the newest bars from FEED.
    # The free real-time feed (IEX) only sees a few % of the volume, so its volumes are scaled up to match.
    cut = datetime.now(timezone.utc) - timedelta(minutes=16)
    legs = ([(f"&feed=sip&end={cut.strftime('%Y-%m-%dT%H:%M:%SZ')}", "sip"), ("&feed=" + FEED, FEED)]
            if HIST_SIP and FEED != "sip" and start < cut else [("&feed=" + FEED, FEED)])
    got = {}
    for extra_q, feed in legs:
        token, pages, part = None, 0, {}
        while pages < 60:
            try:
                data = alp("GET", base + extra_q + (f"&page_token={token}" if token else ""), timeout=20)
            except RuntimeError:
                if feed == "sip":  # not allowed on this account: everything from FEED instead
                    break
                raise
            for b in data.get("bars") or []:
                part[int(_iso_ts(b["t"]))] = (float(b["c"]), float(b["h"]), float(b["l"]), float(b["v"]))
            token = data.get("next_page_token")
            pages += 1
            if not token:
                break
        got[feed] = part
    sip, live = got.get("sip") or {}, got.get(FEED) or {}
    if sip and live and FEED != "sip":
        both = [t for t in live if t in sip and live[t][3] > 0]
        ratio = (sum(sip[t][3] for t in both) / max(1e-9, sum(live[t][3] for t in both))) if len(both) >= 5 else VOL_RATIO.get(coin, 0)
        if ratio > 0:
            VOL_RATIO[coin] = ratio
        live = {t: (r[0], r[1], r[2], r[3] * (VOL_RATIO.get(coin) or 1)) for t, r in live.items() if t not in sip}
    elif live and FEED != "sip" and VOL_RATIO.get(coin):
        live = {t: (r[0], r[1], r[2], r[3] * VOL_RATIO[coin]) for t, r in live.items()}
    rows.update(sip)
    rows.update(live)
    if gran_seconds < 86400:
        rows = {t: r for t, r in rows.items() if regular_hours(t, gran_seconds)}
    every = sorted(rows)
    keys = every[-count:]
    seq = [rows[t] for t in keys]
    if cache and seq:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({str(t): rows[t] for t in every[-max(count, 6000):]}, f)
            os.replace(tmp, path)
        except OSError:
            pass
    return dict(c=[r[0] for r in seq], h=[r[1] for r in seq], l=[r[2] for r in seq], v=[r[3] for r in seq], t=keys)


VOL_RATIO = {}  # ticker -> full-market volume / free-feed volume, learned from bars both feeds have
HIST_START = datetime(2016, 1, 4, tzinfo=timezone.utc).timestamp()  # the oldest stock history Alpaca serves
_SIP_OK = {"ok": True}


def regular_hours(t, gran):
    """True for a bar inside the regular session (9:30 to 4 pm New York, weekdays). Options only trade then, so the
    pre- and after-market bars Alpaca also sends are left out. Daily bars always count."""
    if gran >= 86400:
        return True
    d = datetime.fromtimestamp(t, timezone.utc)
    ny = d - timedelta(hours=_ny_offset(d))
    if ny.weekday() >= 5:
        return False
    m = ny.hour * 60 + ny.minute
    first = 9 * 60 if gran >= 3600 else 9 * 60 + 30  # the 9:00 hourly bar holds the 9:30 open
    return first <= m < 16 * 60


def _hist_path(coin, gran, year):
    if not re.fullmatch(r"[A-Z0-9.\-]{1,12}", str(coin)):
        raise ValueError(f"not a ticker: {coin!r}")
    return os.path.join(HOME_DIR, "history", f"{coin}-{int(gran)}", f"{year}.json")


def _load_shard(coin, gran, year):
    try:
        with open(_hist_path(coin, gran, year)) as f:
            d = json.load(f)
        return dict(spans=[tuple(x) for x in d.get("spans") or []],
                    rows={int(t): tuple(r) for t, r in zip(d["t"], zip(d["c"], d["h"], d["l"], d["v"]))})
    except (OSError, ValueError, KeyError, TypeError):
        return dict(spans=[], rows={})


def _save_shard(coin, gran, year, sh):
    p = _hist_path(coin, gran, year)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    ts = sorted(sh["rows"])
    rows = [sh["rows"][t] for t in ts]
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(dict(spans=sh["spans"], t=ts, c=[r[0] for r in rows], h=[r[1] for r in rows], l=[r[2] for r in rows],
                       v=[r[3] for r in rows]), f, separators=(",", ":"))
        n = f.tell()
    os.replace(tmp, p)
    note_write(p, n)


def _fetch_bars(coin, gran, a, b, feed):
    rows, token, pages = {}, None, 0
    url0 = (f"{DATA_URL}/v2/stocks/{urllib.parse.quote(coin)}/bars?timeframe={_tf(gran)}&limit=10000&adjustment=split"
            f"&feed={feed}&start={_iso(a)}&end={_iso(b)}")
    while pages < 400:
        data = alp("GET", url0 + (f"&page_token={token}" if token else ""), timeout=30, retries=2)
        for x in data.get("bars") or []:
            t = int(_iso_ts(x["t"]))
            if regular_hours(t, gran):
                rows[t] = (float(x["c"]), float(x["h"]), float(x["l"]), float(x["v"]))
        token = data.get("next_page_token")
        pages += 1
        if not token:
            break
    return rows


def fetch_range(coin, gran, start, end, quote="USD", note=None):
    """Bars between two dates, any time back to 2016: {'c','h','l','v','t'}, regular trading hours only. History is
    stored on disk by year, so each stretch is downloaded once; the newest 15 minutes come live from the free feed."""
    start, end = max(float(start), HIST_START), min(float(end), time.time())
    rows = {}
    cut = time.time() - 16 * 60
    y0, y1 = datetime.fromtimestamp(start, timezone.utc).year, datetime.fromtimestamp(end, timezone.utc).year
    for y in range(y0, y1 + 1):
        ya = datetime(y, 1, 1, tzinfo=timezone.utc).timestamp()
        yb = datetime(y + 1, 1, 1, tzinfo=timezone.utc).timestamp()
        a, b = max(start, ya), min(end, yb, cut)
        if a >= b:
            continue
        sh = _load_shard(coin, gran, y)
        miss = _missing(sh["spans"], a, b)
        for ma, mb in miss:
            if note:
                note(f"Downloading {coin} {datetime.fromtimestamp(ma).strftime('%b %Y')}"
                     + (f" to {datetime.fromtimestamp(mb).strftime('%b %Y')}" if mb - ma > 40 * 86400 else ""))
            got = None
            if HIST_SIP and _SIP_OK["ok"] and FEED != "sip":
                try:
                    got = _fetch_bars(coin, gran, ma, mb, "sip")
                except RuntimeError as e:
                    if "403" in str(e) or "subscription" in str(e).lower():
                        _SIP_OK["ok"] = False
                    else:
                        raise
            if got is None:
                got = _fetch_bars(coin, gran, ma, mb, FEED)
            sh["rows"].update(got)
            sh["spans"] = _merge_spans(sh["spans"] + [(int(ma), int(mb))])
        if miss:
            try:
                _save_shard(coin, gran, y, sh)
            except OSError:
                pass
        rows.update({t: r for t, r in sh["rows"].items() if start <= t <= end})
    if end > cut:  # the newest minutes, live
        try:
            live = _fetch_bars(coin, gran, max(start, cut - 3600), end, FEED)
            k = VOL_RATIO.get(coin) or 1
            rows.update({t: (r[0], r[1], r[2], r[3] * k) for t, r in live.items() if t not in rows})
        except Exception:
            note_error("fetch_range")
    ts = sorted(rows)
    seq = [rows[t] for t in ts]
    return dict(c=[r[0] for r in seq], h=[r[1] for r in seq], l=[r[2] for r in seq], v=[r[3] for r in seq], t=ts)


class NoHistory(RuntimeError):
    """There is no (or too little) history for what was asked."""


def coin_days(coin, quote="USD", max_age=6 * 3600):
    """The days (UTC midnight timestamps) on which there are daily bars for this ticker (a few requests, then stored
    like any other history). Tickers listed late simply have no days before that."""
    key = (coin, quote)
    hit = _AVAIL.get(key)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    d = fetch_range(coin, 86400, HIST_START, time.time(), quote)
    days = sorted({int(t) // 86400 * 86400 for t in d["t"]})
    _AVAIL[key] = (time.time(), days)
    return days


def common_days(coins, quote="USD"):
    """Days every one of these coins has history (sorted)."""
    sets = [set(coin_days(c, quote)) for c in coins]
    return sorted(set.intersection(*sets)) if sets else []


def coverage(days_set, a, b):
    """Share of the weekdays between a and b that are in days_set (holidays make it a little under 1)."""
    d0, d1 = int(a) // 86400 * 86400, int(b) // 86400 * 86400
    wk = [d for d in range(d0, d1 + 1, 86400) if datetime.fromtimestamp(d, timezone.utc).weekday() < 5]
    return sum(1 for d in wk if d in days_set) / max(1, len(wk))


def history_text(coin, quote="USD"):
    """'BTC: Jul 2015 to now' style description of a coin's history, including any long gap."""
    days = coin_days(coin, quote)
    if not days:
        return f"{coin}: no history"
    gaps = [(days[i - 1], days[i]) for i in range(1, len(days)) if days[i] - days[i - 1] > 10 * 86400]
    fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%b %Y")
    txt = f"{coin}: {fmt(days[0])} to " + ("now" if time.time() - days[-1] < 3 * 86400 else fmt(days[-1]))
    if gaps:
        txt += " (missing " + ", ".join(f"{fmt(a + 86400)} to {fmt(b - 86400)}" for a, b in gaps[:2]) + ")"
    return txt


def longest_run(times, gran):
    """(start index, end index) of the longest stretch of these bar times with no gap over 5 days (weekends and
    holidays are normal for stocks)."""
    if not times:
        return 0, 0
    lim = max(6 * gran, 5 * 86400)
    best, start = (0, 1), 0
    for i in range(1, len(times)):
        if times[i] - times[i - 1] > lim:
            if i - start > best[1] - best[0]:
                best = (start, i)
            start = i
    if len(times) - start > best[1] - best[0]:
        best = (start, len(times))
    return best


def bars_per_day(gran):
    """Regular-session candles in one trading day."""
    return 1 if gran >= 86400 else max(1, int(round(6.5 * 3600 / gran)))


def latest_volume(coin, gran_seconds, quote="USD"):
    """Volume of the latest 1-minute bar (None if unavailable)."""
    data = alp("GET", f"{DATA_URL}/v2/stocks/bars/latest?symbols={urllib.parse.quote(coin)}&feed={FEED}")
    b = (data.get("bars") or {}).get(coin)
    return float(b["v"]) * max(1, gran_seconds // 60) if b else None


_CLOCK = {"at": 0.0, "data": None}


def market_clock(max_age=60):
    """{'is_open', 'next_open', 'next_close'} (timestamps), cached for a minute."""
    if _CLOCK["data"] is None or time.time() - _CLOCK["at"] > max_age:
        d = alp("GET", f"{trade_url()}/v2/clock")
        _CLOCK.update(at=time.time(), data=dict(is_open=bool(d.get("is_open")), next_open=_iso_ts(d["next_open"]),
                                                next_close=_iso_ts(d["next_close"])))
    return dict(_CLOCK["data"])


def parse_occ(sym):
    """'SPY261113C00450000' -> (underlying, expiry timestamp (4 pm New York), 'call'/'put', strike)."""
    m = re.fullmatch(r"([A-Z.]{1,6})(\d{6})([CP])(\d{8})", sym or "")
    if not m:
        return None
    d = datetime.strptime(m.group(2), "%y%m%d").replace(hour=20, tzinfo=timezone.utc)
    return m.group(1), d.timestamp(), "call" if m.group(3) == "C" else "put", int(m.group(4)) / 1000


def option_chain(under, exp_from=None, exp_to=None, typ=None, strike_lo=None, strike_hi=None):
    """Live option chain: [{symbol, type, K, exp, bid, ask, last, iv, delta, gamma, theta, vega}], from Alpaca's free
    'indicative' option feed (prices are close to, but not exactly, the real quotes; OPRA with a paid plan)."""
    q = [f"feed={'opra' if FEED == 'sip' else 'indicative'}", "limit=1000"]
    if exp_from:
        q.append(f"expiration_date_gte={exp_from}")
    if exp_to:
        q.append(f"expiration_date_lte={exp_to}")
    if typ:
        q.append(f"type={typ}")
    if strike_lo is not None:
        q.append(f"strike_price_gte={strike_lo:g}")
    if strike_hi is not None:
        q.append(f"strike_price_lte={strike_hi:g}")
    out, token, pages = [], None, 0
    while pages < 10:
        d = alp("GET", f"{DATA_URL}/v1beta1/options/snapshots/{urllib.parse.quote(under)}?" + "&".join(q)
                + (f"&page_token={token}" if token else ""), timeout=20)
        for sym, sn in (d.get("snapshots") or {}).items():
            p_ = parse_occ(sym)
            if not p_:
                continue
            lq, lt, g = sn.get("latestQuote") or {}, sn.get("latestTrade") or {}, sn.get("greeks") or {}
            out.append(dict(symbol=sym, under=p_[0], exp=p_[1], type=p_[2], K=p_[3], bid=float(lq.get("bp") or 0),
                            ask=float(lq.get("ap") or 0), last=float(lt.get("p") or 0), iv=sn.get("impliedVolatility"),
                            delta=g.get("delta"), gamma=g.get("gamma"), theta=g.get("theta"), vega=g.get("vega")))
        token = d.get("next_page_token")
        pages += 1
        if not token:
            break
    return sorted(out, key=lambda r: (r["exp"], r["K"], r["type"]))


OPT_HIST_START = "2024-02-01"  # Alpaca keeps option history from February 2024


def _optbook_path(under, day):
    return os.path.join(HOME_DIR, "cache", "optbars", f"{under}-{day}.json")


def day_option_book(under, day, S, exp_days=8, width=0.06, per=12, note=None):
    """One past trading day's real option history for a ticker, from Alpaca: the listed contracts expiring 1 to
    exp_days days after that day with strikes within +-width of the price S (the nearest `per` strikes of each expiry and
    type), and every one's 1-minute prices during that day. Cached on disk (a past day never changes).
    -> {contracts: [{symbol, type, K, exp}], bars: {symbol: [[times], [closes]]}} or None (before Feb 2024, or nothing)"""
    if day < OPT_HIST_START:
        return None
    path = _optbook_path(under, day)
    hit = load_json(path, None)
    if isinstance(hit, dict) and "contracts" in hit:
        return hit if hit["contracts"] else None
    d0 = datetime.strptime(day, "%Y-%m-%d")
    lo_d, hi_d = (d0 + timedelta(days=1)).strftime("%Y-%m-%d"), (d0 + timedelta(days=exp_days)).strftime("%Y-%m-%d")
    raw = {}
    for status in ("inactive", "active"):  # contracts that have expired since, and ones still listed
        token, pages = None, 0
        while pages < 5:
            q = (f"underlying_symbols={urllib.parse.quote(under)}&expiration_date_gte={lo_d}&expiration_date_lte={hi_d}"
                 f"&strike_price_gte={S * (1 - width):.2f}&strike_price_lte={S * (1 + width):.2f}&status={status}&limit=10000")
            d = alp("GET", f"{trade_url()}/v2/options/contracts?{q}" + (f"&page_token={token}" if token else ""), timeout=20)
            for x in d.get("option_contracts") or []:
                try:
                    raw[x["symbol"]] = dict(symbol=x["symbol"], type=x["type"], K=float(x["strike_price"]),
                                            exp=ny_time(x["expiration_date"], 16, 0))
                except (KeyError, TypeError, ValueError):
                    continue
            token = d.get("next_page_token")
            pages += 1
            if not token:
                break
    groups = {}
    for x in raw.values():
        groups.setdefault((x["type"], x["exp"]), []).append(x)
    keep = []
    for g in groups.values():
        keep += sorted(g, key=lambda x: abs(x["K"] - S))[:per]
    bars = {}
    start, end = ny_time(day, 9, 30), ny_time(day, 16, 0)
    syms = [x["symbol"] for x in keep]
    for i in range(0, len(syms), 100):
        chunk = syms[i:i + 100]
        if note:
            note(f"Loading real option prices for {under} ({min(i + 100, len(syms))} of {len(syms)} contracts)")
        token, pages = None, 0
        while pages < 40:
            q = (f"symbols={urllib.parse.quote(','.join(chunk))}&timeframe=1Min&start={_iso(start)}&end={_iso(end)}&limit=10000")
            d = alp("GET", f"{DATA_URL}/v1beta1/options/bars?{q}" + (f"&page_token={token}" if token else ""), timeout=30)
            for sym, rows in (d.get("bars") or {}).items():
                b = bars.setdefault(sym, [[], []])
                for r in rows:
                    try:
                        b[0].append(_iso_ts(r["t"]))
                        b[1].append(float(r["c"]))
                    except (KeyError, TypeError, ValueError):
                        continue
            token = d.get("next_page_token")
            pages += 1
            if not token:
                break
    book = dict(contracts=[x for x in keep if x["symbol"] in bars], bars=bars)
    if keep and not book["contracts"]:
        return None  # contracts listed but no prices came back (a hiccup): don't remember the day as empty
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        save_json(path, book)
    except Exception:
        note_error("day_option_book")
    return book if book["contracts"] else None


def option_oi(under, exp_from=None, exp_to=None, typ=None):
    """{option symbol: open interest} from Alpaca's contract list (open interest is updated once a day)."""
    q = [f"underlying_symbols={urllib.parse.quote(under)}", "limit=10000", "status=active"]
    if exp_from:
        q.append(f"expiration_date_gte={exp_from}")
    if exp_to:
        q.append(f"expiration_date_lte={exp_to}")
    if typ:
        q.append(f"type={typ}")
    out, token = {}, None
    for _ in range(5):
        d = alp("GET", f"{trade_url()}/v2/options/contracts?" + "&".join(q) + (f"&page_token={token}" if token else ""))
        for r in d.get("option_contracts") or []:
            try:
                out[r["symbol"]] = int(float(r.get("open_interest") or 0))
            except (TypeError, ValueError, KeyError):
                continue
        token = d.get("next_page_token")
        if not token:
            break
    return out


def option_quotes(symbols):
    """{option symbol: (bid, ask, time)} for specific contracts."""
    out = {}
    syms = [x for x in symbols if x]
    for k in range(0, len(syms), 100):
        q = urllib.parse.quote(",".join(syms[k:k + 100]))
        d = alp("GET", f"{DATA_URL}/v1beta1/options/snapshots?symbols={q}&feed={'opra' if FEED == 'sip' else 'indicative'}")
        for sym, sn in (d.get("snapshots") or {}).items():
            lq = sn.get("latestQuote") or {}
            out[sym] = (float(lq.get("bp") or 0), float(lq.get("ap") or 0), time.time())
    return out


def liquid(r, max_spread_pct=15.0, min_oi=50):
    """Is this contract busy enough to trade? A narrow bid/ask gap and enough open contracts (when known)."""
    bid, ask = r.get("bid") or 0, r.get("ask") or 0
    if ask <= 0:
        return False
    if max_spread_pct and (bid <= 0 or (ask - bid) / ((ask + bid) / 2) * 100 > max_spread_pct):
        return False
    oi = r.get("oi")
    if min_oi and oi is not None and oi < min_oi:
        return False
    return True


def pick_contract(chain, S, typ, dte, otm_pct, now=None, min_sessions=1, max_spread_pct=0.0, min_oi=0):
    """The listed contract closest to what a bot wants: its type, about dte days out, about otm_pct% out of the money;
    only ones inside the price range, liquid enough, and with more than min_sessions trading days left."""
    now = now or time.time()
    want_exp = now + dte * 86400
    want_K = S * (1 + otm_pct / 100) if typ == "call" else S * (1 - otm_pct / 100)
    cands = [r for r in chain if r["type"] == typ and r["exp"] > now + 86400 and r["ask"] > 0 and prem_ok(r["ask"])
             and sessions_left(r["exp"], now) > min_sessions and liquid(r, max_spread_pct, min_oi)]
    if not cands:
        return None
    return min(cands, key=lambda r: (abs(r["exp"] - want_exp) / 86400 / max(3, dte)) + abs(r["K"] - want_K) / S * 10)


# ---------------- Economic calendar (release dates from FRED, the St. Louis Fed's free data service) ----------------
# kind: (FRED release id, name, release hour and minute in New York)


def _events_path():
    return os.path.join(HOME_DIR, "cache", "events.json")


def fetch_events(fred_key, start="2015-01-01"):
    """Downloads past and scheduled release times for every EVENT_KINDS entry and saves them. -> {kind: [ts, ...]}"""
    out, errs = {}, []
    for kind, (rid, name, hh, mm) in EVENT_KINDS.items():
        url = ("https://api.stlouisfed.org/fred/release/dates?file_type=json&include_release_dates_with_no_data=true"
               f"&sort_order=asc&limit=10000&realtime_start={start}&release_id={rid}&api_key={urllib.parse.quote(fred_key.strip())}")
        try:
            d = json.loads(http_get(url, timeout=20, retries=2))
            out[kind] = sorted({ny_time(r["date"], hh, mm) for r in d.get("release_dates") or [] if r.get("date")})
        except Exception as e:
            errs.append(f"{name}: {e}")
    if not out:
        raise RuntimeError("; ".join(errs) or "no release dates came back")
    os.makedirs(os.path.dirname(_events_path()), exist_ok=True)
    tmp = _events_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(dict(fetched=time.time(), events=out), f)
    os.replace(tmp, _events_path())
    _EVENTS["data"] = None
    return out, errs


def events():
    """{kind: [release times]} from the saved calendar ({} if it was never downloaded). Re-read when the file changes,
    so worker processes see it too."""
    try:
        mt = os.path.getmtime(_events_path())
    except OSError:
        return {}
    if _EVENTS["data"] is None or mt != _EVENTS["mtime"]:
        try:
            with open(_events_path()) as f:
                _EVENTS["data"] = {k: v for k, v in (json.load(f).get("events") or {}).items() if k in EVENT_KINDS}
        except (OSError, ValueError):
            _EVENTS["data"] = {}
        _EVENTS["mtime"] = mt
    return _EVENTS["data"]


def events_fetched():
    try:
        with open(_events_path()) as f:
            return float(json.load(f).get("fetched") or 0)
    except (OSError, ValueError):
        return 0.0


def next_event(ts, kinds=BIG_EVENTS):
    """(kind, time) of the next release after ts, or (None, None)."""
    best = (None, None)
    ev = events()
    for k in kinds:
        lst = ev.get(k) or []
        i = bisect.bisect_right(lst, ts)
        if i < len(lst) and (best[1] is None or lst[i] < best[1]):
            best = (k, lst[i])
    return best


def last_event(ts, kinds=BIG_EVENTS):
    best = (None, None)
    ev = events()
    for k in kinds:
        lst = ev.get(k) or []
        i = bisect.bisect_right(lst, ts) - 1
        if i >= 0 and (best[1] is None or lst[i] > best[1]):
            best = (k, lst[i])
    return best


def upcoming_events(now=None, days=14):
    now = now or time.time()
    ev = events()
    out = [(t, k) for k in EVENT_KINDS for t in ev.get(k) or [] if now - 3600 <= t <= now + days * 86400]
    return sorted(out)


# ---------------- Interactive Brokers (through IB Gateway / Trader Workstation on your computer) ----------------
IB_PORTS = {"IB Gateway, paper (4002)": 4002, "IB Gateway, live (4001)": 4001,
            "Trader Workstation, paper (7497)": 7497, "Trader Workstation, live (7496)": 7496}


def alp_account(creds=None):
    return alp("GET", f"{trade_url() if not creds else ('https://paper-api.alpaca.markets' if creds.get('paper', True) else 'https://api.alpaca.markets')}/v2/account",
               creds=creds)


class UnsafeOrder(RuntimeError):
    pass


def alp_positions():
    return alp("GET", f"{trade_url()}/v2/positions")


# ---------------- strategies: fn(prices, bar_index, params) -> "buy"/"sell"/None ----------------


# ---- scalp instincts: fast, in-and-out signals for the scalp breed (seconds to minutes, never overnight) ----
def sig_micro_pullback(p, n, a, x=None):
    """Micro pullback: in a short-term trend (price above a rising fast EMA), wait for a pause of a few bars that
    holds above the EMA, then buy the first bar that closes above the previous bar's high. Mirror for puts."""
    k, pb = int(a["n"]), int(a["pull"])
    if not x or len(p) < k * 3 + pb + 2:
        return None
    h, lo = x["h"], x["l"]
    e = _ema(p[-(k * 3 + pb + 2):], k)
    rising, falling = e[-1] > e[-1 - pb - 1], e[-1] < e[-1 - pb - 1]
    pulled = all(p[-1 - i] <= p[-2 - i] for i in range(1, pb + 1))
    popped = all(p[-1 - i] >= p[-2 - i] for i in range(1, pb + 1))
    if rising and pulled and min(lo[-pb - 1:-1]) >= e[-2] * 0.999 and p[-1] > h[-2]:
        return "buy"
    if falling and popped and max(h[-pb - 1:-1]) <= e[-2] * 1.001 and p[-1] < lo[-2]:
        return "sell"


def sig_volume_burst(p, n, a, x=None):
    """Volume burst: one bar trades several times its usual volume, moves at least thr %, and closes near its own
    high (buy) or low (sell). Rides the first push of a sudden crowd."""
    k = int(a["n"])
    if not x or len(p) < k + 2:
        return None
    v, h, lo = x["v"], x["h"], x["l"]
    avg = sum(v[-k - 1:-1]) / k
    if avg <= 0 or v[-1] < a["vmult"] * avg or not p[-2]:
        return None
    mv = (p[-1] / p[-2] - 1) * 100
    rng = h[-1] - lo[-1]
    if rng <= 0:
        return None
    where = (p[-1] - lo[-1]) / rng  # 1 = closed at the bar's high, 0 = at its low
    if mv >= a["thr"] and where >= 0.7:
        return "buy"
    if mv <= -a["thr"] and where <= 0.3:
        return "sell"


def sig_vwap_reclaim(p, n, a, x=None):
    """VWAP reclaim: the price crosses back over its short volume-weighted average on above-normal volume. Crossing
    up buys, crossing down sells (the opposite of a snap-back: it trades the cross, not the stretch)."""
    k = int(a["n"])
    if not x or len(p) < k + 2:
        return None
    h, lo, v = x["h"][-k:], x["l"][-k:], x["v"][-k:]
    tv = sum(v)
    if tv <= 0:
        return None
    vw = sum((h[i] + lo[i] + p[-k + i]) / 3 * v[i] for i in range(k)) / tv
    if v[-1] < a["vmin"] * tv / k:
        return None
    if p[-2] <= vw < p[-1]:
        return "buy"
    if p[-2] >= vw > p[-1]:
        return "sell"


def sig_range_scalp(p, n, a, x=None):
    """Range scalp: when the last n bars sit in a tight box (no wider than maxw %), buy a turn up near the bottom
    edge and sell a turn down near the top edge."""
    k = int(a["n"])
    if not x or len(p) < k + 2:
        return None
    hh, ll = max(x["h"][-k - 1:-1]), min(x["l"][-k - 1:-1])
    if ll <= 0 or hh <= ll or (hh - ll) / ll * 100 > a["maxw"]:
        return None
    band = (hh - ll) * a["edge"]
    if p[-1] <= ll + band and p[-1] > p[-2]:
        return "buy"
    if p[-1] >= hh - band and p[-1] < p[-2]:
        return "sell"


def sig_quick_flip(p, n, a):
    """Quick flip: a run of closes all in one direction adding up to at least thr %. Jumps on the run."""
    k = int(a["run"])
    if len(p) < k + 2 or not p[-1 - k]:
        return None
    d = [p[-i] - p[-i - 1] for i in range(1, k + 1)]
    mv = (p[-1] / p[-1 - k] - 1) * 100
    if all(x > 0 for x in d) and mv >= a["thr"]:
        return "buy"
    if all(x < 0 for x in d) and mv <= -a["thr"]:
        return "sell"


STRATS = {
    "SMA crossover": dict(fn=sig_sma, params={"fast": 5, "slow": 20},
                          bounds={"fast": (2, 60), "slow": (10, 200)}, keep=420),
    "RSI": dict(fn=sig_rsi, params={"period": 14, "lo": 30, "hi": 70},
                bounds={"period": (2, 30), "lo": (5, 40), "hi": (60, 95)}),
    "Momentum": dict(fn=sig_mom, params={"n": 10, "thr": 0.5},
                     bounds={"n": (3, 40), "thr": (0.1, 3.0)}),
    "Bollinger bands": dict(fn=sig_boll, params={"n": 20, "k": 2.0},
                            bounds={"n": (10, 50), "k": (1.0, 3.0)}),
    "Breakout": dict(fn=sig_breakout, params={"n": 20}, bounds={"n": (5, 60)}),
    "MACD": dict(fn=sig_macd, params={"fast": 12, "slow": 26, "signal": 9},
                 bounds={"fast": (3, 20), "slow": (15, 50), "signal": (3, 15)}),
    "Mean reversion": dict(fn=sig_revert, params={"n": 20, "thr": 1.0},
                           bounds={"n": (5, 60), "thr": (0.2, 5.0)}),
    "EMA crossover": dict(fn=sig_ema, params={"fast": 4, "slow": 12, "min_gap": 0.05},
                          bounds={"fast": (2, 20), "slow": (5, 60), "min_gap": (0.0, 0.5)}),
    "Stochastic": dict(fn=sig_stoch, params={"n": 14, "lo": 20, "hi": 80},
                       bounds={"n": (5, 40), "lo": (5, 35), "hi": (65, 95)}),
    "Supertrend": dict(fn=sig_supertrend, params={"n": 10, "m": 3.0}, hlv=True, keep=120,
                       bounds={"n": (5, 30), "m": (1.0, 5.0)}),
    "Ichimoku cross": dict(fn=sig_ichimoku, params={"fast": 9, "slow": 26}, hlv=True, keep=120,
                           bounds={"fast": (4, 20), "slow": (15, 60)}),
    "Rate of change": dict(fn=sig_roc, params={"n": 12, "smooth": 4}, bounds={"n": (3, 50), "smooth": (2, 12)}),
    "MA ribbon": dict(fn=sig_ribbon, params={"fast": 5, "mid": 13, "slow": 34},
                      bounds={"fast": (2, 15), "mid": (8, 40), "slow": (20, 100)}),
    "Trend + dip": dict(fn=sig_trend_dip, params={"trend": 60, "period": 7, "lo": 30, "hi": 70},
                        bounds={"trend": (20, 150), "period": (3, 20), "lo": (15, 40), "hi": (60, 85)}),
    "Volume breakout": dict(fn=sig_volbreak, params={"n": 20, "vmult": 1.8}, hlv=True, keep=80,
                            bounds={"n": (8, 60), "vmult": (1.0, 4.0)}),
    "VWAP snap-back": dict(fn=sig_vwap, params={"n": 30, "thr": 0.8}, hlv=True, keep=120,
                           bounds={"n": (8, 100), "thr": (0.15, 4.0)}),
    "ATR channel": dict(fn=sig_atr, params={"n": 20, "m": 1.5}, hlv=True, keep=200,
                        bounds={"n": (5, 60), "m": (0.5, 4.0)}),
    "Dollar-cost averaging": dict(fn=sig_dca, params={"every": 24},
                                  bounds={"every": (6, 96)}),
    # the scalp breed (scalp=True): only ever hatched as scalpers, see scalp_shape()
    "Micro pullback": dict(fn=sig_micro_pullback, params={"n": 9, "pull": 2}, hlv=True, keep=80, scalp=True,
                           bounds={"n": (4, 21), "pull": (1, 4)}),
    "Volume burst": dict(fn=sig_volume_burst, params={"n": 20, "vmult": 2.5, "thr": 0.08}, hlv=True, keep=80, scalp=True,
                         bounds={"n": (8, 60), "vmult": (1.5, 6.0), "thr": (0.02, 0.6)}),
    "VWAP reclaim": dict(fn=sig_vwap_reclaim, params={"n": 30, "vmin": 1.2}, hlv=True, keep=150, scalp=True,
                         bounds={"n": (10, 120), "vmin": (0.6, 3.0)}),
    "Range scalp": dict(fn=sig_range_scalp, params={"n": 20, "edge": 0.2, "maxw": 0.4}, hlv=True, keep=80, scalp=True,
                        bounds={"n": (8, 60), "edge": (0.05, 0.35), "maxw": (0.08, 1.5)}),
    "Quick flip": dict(fn=sig_quick_flip, params={"run": 3, "thr": 0.06}, keep=20, scalp=True,
                       bounds={"run": (2, 6), "thr": (0.02, 0.5)}),
}


# Famous strategies, some historically popular, some controversial. With Wildcard on they are mixed into the
# eggs alongside random ones, hatch from silver eggs, and have to earn their place like everyone else.


def fix_params(name, p):
    """Keeps related settings sensible (short average below long one, buy level below sell level)."""
    d = STRATS[name]["params"]
    if "fast" in p and "slow" in p and p["fast"] >= p["slow"]:
        p["slow"] = min(p["fast"] + 5, STRATS[name]["bounds"]["slow"][1])
        p["fast"] = min(p["fast"], p["slow"] - 1)
        if p["fast"] < STRATS[name]["bounds"]["fast"][0]:
            p["fast"], p["slow"] = d["fast"], d["slow"]
    if "lo" in p and "hi" in p and p["lo"] >= p["hi"] - 10:
        p["lo"], p["hi"] = d["lo"], d["hi"]
    if "mid" in p and not (p["fast"] < p["mid"] < p["slow"]):
        p["fast"], p["mid"], p["slow"] = d["fast"], d["mid"], d["slow"]
    return p
SCALP_STRATS = [n for n, s in STRATS.items() if s.get("scalp")]  # the scalp breed's own instincts
WILDCARD_STRATS = [n for n in STRATS if n != "Dollar-cost averaging" and n not in SCALP_STRATS]  # DCA never sells


def is_scalp(name):
    """Is this strategy one of the scalp breed's instincts?"""
    return bool(STRATS.get(name, {}).get("scalp"))


# The scalp breed's gene envelope. Whatever mutation or crossbreeding does, a scalper is always pulled back inside
# it: a fast pace, a tight stop AND target on the share price, a hard time limit, and flat before the close
# (scalpers never hold overnight).
SCALP_TF = (1, 2)
SCALP_RISK = {"sl": (0.15, 2.0), "tp": (0.2, 4.0)}  # from quick 0.2% nips to 4% runs: shares move far less than options
SCALP_TRAITS = {"vixmode": (1, 5), "vixwide": (1.2, 3.0), "maxhold": (2, 60), "flatby": (5, 45), "cool": (1, 10),
                "minhold": (2, 5)}
SCALP_NEEDS = {"maxhold": (3, 20), "flatby": (10, 30), "vixmode": (1, 5)}  # always carried (random start when missing)


SCALP_ONLY = ("flatby", "vixmode", "vixwide")  # genes only scalpers carry
BREED_ONLY = SCALP_ONLY + ("kami",)            # genes no ordinary egg grows by chance

# The kamikaze breed's envelope: a huge target and a very wide stop, so each bet rides a big move or a big loss.
# They trade tiny (KAMI_SAY of a normal bot's say in your account) and every one of them carries one or two untested
# rules, whose results the farm learns from (its "trial book").
KAMI_RISK = {"sl": (12.0, 25.0), "tp": (20.0, 60.0)}
KAMI_TRAITS = {"maxhold": (5, 60)}
KAMI_SAY = 0.3


def is_kami(v_or_traits):
    t = v_or_traits.traits if hasattr(v_or_traits, "traits") else (v_or_traits or {})
    return bool((t or {}).get("kami"))


def kami_shape(rng, risk, traits):
    """(risk, traits) pulled inside the kamikaze envelope (idempotent)."""
    r = dict(clean_risk(risk))
    for k, (lo, hi) in KAMI_RISK.items():
        val = float(r.get(k) or 0) or rng.uniform(lo, hi)
        r[k] = round(min(max(val, lo), hi), 2)
    t = {k: x for k, x in clean_traits(traits).items() if k not in SCALP_ONLY}
    t["kami"] = 1
    for k, (lo, hi) in KAMI_TRAITS.items():
        if not t.get(k):
            t[k] = rng.uniform(lo, hi) if isinstance(lo, float) else rng.randint(lo, hi)
        t[k] = min(max(t[k], lo), hi)
    return r, clean_traits(t)


def scalp_shape(rng, tf, risk, traits):
    """(tf, risk, traits) pulled inside the scalp envelope. Idempotent: genes already inside it are left alone."""
    tf = clamp_tf(tf)
    if tf not in SCALP_TF:
        tf = rng.choice((1, 1, 2))
    r = dict(clean_risk(risk))
    for k, (lo, hi) in SCALP_RISK.items():
        # new ones start anywhere on a log scale, so small and big targets are tried about equally often
        val = float(r.get(k) or 0) or math.exp(rng.uniform(math.log(lo), math.log(hi)))
        r[k] = round(min(max(val, lo), hi), 2)
    t = dict(clean_traits(traits))
    t.pop("oexit", None)  # replaced by flatby: a scalper is out the same day
    for k, (lo, hi) in SCALP_NEEDS.items():
        if not t.get(k):
            t[k] = rng.randint(lo, hi)
    for k, (lo, hi) in SCALP_TRAITS.items():
        if k in t:
            t[k] = min(max(t[k], lo), hi)
    if t.get("minhold", 0) >= t["maxhold"]:
        t.pop("minhold")
    return tf, r, clean_traits(t)


# ---- volatility (VIX) regime: what scalpers read before they jump in ----
VIX_MODES = {1: "only scalps calm markets", 2: "only scalps when volatility is rising", 3: "only scalps volatility spikes",
             4: "never scalps a volatility spike", 5: "ignores volatility (VIX)"}
_REG_CACHE = {}
IVX_KEY = "IVX"          # live implied volatility of SPY's ~30-day at-the-money options: what the VIX itself measures
IVX_LIVE = deque(maxlen=1200)  # (time, implied vol) readings, about one a minute while the market is open


def _ivx_path(day):
    return os.path.join(HOME_DIR, "cache", f"ivx-{day}.json")


def ivx_add(ts, iv):
    """Records one live reading and keeps today's path on disk (so a restart or the Scalp scan can use it)."""
    IVX_LIVE.append((ts, iv))
    try:
        day = ny_date(ts).isoformat()
        save_json(_ivx_path(day), [x for x in IVX_LIVE if ny_date(x[0]).isoformat() == day])
    except Exception:
        pass


def ivx_today(now=None):
    """Today's (time, implied vol) readings, oldest first."""
    day = ny_date(now or time.time()).isoformat()
    have = [x for x in IVX_LIVE if ny_date(x[0]).isoformat() == day]
    if not have:
        for t, iv in load_json(_ivx_path(day), []) or []:
            IVX_LIVE.append((t, iv))
        have = [x for x in IVX_LIVE if ny_date(x[0]).isoformat() == day]
    return have


def ivx_now(max_age=300):
    """The latest reading if it's fresh, else None."""
    return IVX_LIVE[-1][1] if IVX_LIVE and time.time() - IVX_LIVE[-1][0] <= max_age else None


def ivx_from_chain(rows, S, now=None):
    """SPY's ~30-day at-the-money implied volatility from an option chain (the expiry closest to 30 days, the strikes
    closest to the price, calls and puts averaged), the same idea as the VIX. None when the chain can't say."""
    now = now or time.time()
    rows = [r for r in rows if r.get("iv") and r["exp"] - now > 7 * 86400]
    if not rows or not S:
        return None
    exp = min({r["exp"] for r in rows}, key=lambda e: abs((e - now) / 86400 - 30))
    near = sorted((r for r in rows if r["exp"] == exp), key=lambda r: abs(r["K"] - S))[:4]
    ivs = [float(r["iv"]) for r in near if 0.01 < float(r["iv"]) < 3]
    return sum(ivs) / len(ivs) if ivs else None


def vol_regime(closes, extra, c):
    """-> (regime, VIXY % move over the last 15 bars, swing ratio, live implied-vol % change over 15 bars; each None
    when unknown). regime: 'calm', 'normal', 'rising' or 'spiking'. Three intraday readings, newest bar included:
    SPY's live ~30-day implied volatility (what the VIX measures, read from its options about once a minute), VIXY
    (a VIX fund that trades, but follows the VIX slowly), and how fast SPY itself is swinging vs its last two hours
    (reacts within minutes). Shared by every bot on the same bar."""
    ctx = (extra or {}).get(CTX_KEY) or {}
    q = ctx.get("VIXY")
    iq = ctx.get(IVX_KEY)
    ref = closes.get("SPY") if "SPY" in closes else ctx.get("SPY")
    if ref is None or len(ref) < 131:
        ref = closes[c]
    key = (id(ref), len(ref), ref[-1] if len(ref) else None, id(q) if q is not None else 0, len(q) if q is not None else 0,
           id(iq) if iq is not None else 0, len(iq) if iq is not None else 0)
    hit = _REG_CACHE.get(key)
    if hit is not None:
        return hit
    if len(_REG_CACHE) > 2048:
        _REG_CACHE.clear()
    vm = sr = im = None
    if q is not None and len(q) > 16 and q[-16]:
        vm = (q[-1] / q[-16] - 1) * 100
    if iq is not None and len(iq) > 16 and iq[-16] and iq[-1]:
        im = (iq[-1] / iq[-16] - 1) * 100  # e.g. 16.0 -> 16.8 = +5%
    if len(ref) > 131:
        seg = ref[-131:]
        moves = [abs(seg[i] / seg[i - 1] - 1) for i in range(1, len(seg)) if seg[i - 1]]
        if len(moves) == 130:
            base = sum(moves[:120]) / 120
            sr = (sum(moves[120:]) / 10) / base if base > 0 else None
    if (im is not None and im >= 5.0) or (vm is not None and vm >= 3.0) or (sr is not None and sr >= 2.2):
        reg = "spiking"
    elif (im is not None and im >= 2.0) or (vm is not None and vm >= 1.0) or (sr is not None and sr >= 1.4):
        reg = "rising"
    elif (im is None or im <= 0.5) and (vm is None or vm <= 0.3) and sr is not None and sr < 0.9:
        reg = "calm"
    else:
        reg = "normal"
    out = _REG_CACHE[key] = (reg, vm, sr, im)
    return out


def vix_allows(mode, reg):
    return {1: reg == "calm", 2: reg in ("rising", "spiking"), 3: reg == "spiking", 4: reg != "spiking"}.get(int(mode or 5), True)


# ---- what a scalper really pays to get in and out: the bid/ask gap, learned from live option quotes ----
SPREAD_FIT = {}            # ticker -> [a, b]: a full bid/ask gap of about $a + b x the price, per share
SPREAD_DEFAULT = (0.02, 0.03)
SCALP_SPREAD = [0.15]      # scalpers skip stocks whose bid/ask gap is wider than this % of the price
_SPREAD_LOADED = [False]


def _spreads_path():
    return os.path.join(HOME_DIR, "cache", "spreads.json")


def learn_spreads(c, rows, S=None):
    """Fits the bid/ask gap of one ticker's option chain as $a + b x price (contracts near the money, with real
    quotes) and saves it. Scalpers in Practice then pay that gap instead of a flat guess."""
    pts = [((r["bid"] + r["ask"]) / 2, r["ask"] - r["bid"]) for r in rows
           if r.get("bid", 0) > 0 and r.get("ask", 0) > r["bid"] and (not S or abs(r["K"] / S - 1) < 0.1)]
    if len(pts) < 8:
        return None
    n = len(pts)
    mx, my = sum(x for x, _ in pts) / n, sum(y for _, y in pts) / n
    vx = sum((x - mx) ** 2 for x, _ in pts)
    b = max(0.0, sum((x - mx) * (y - my) for x, y in pts) / vx) if vx > 0 else 0.0
    a = max(0.01, my - b * mx)
    _load_spreads()
    SPREAD_FIT[c] = [round(a, 4), round(min(b, 0.5), 4), time.time()]
    try:
        save_json(_spreads_path(), SPREAD_FIT)
    except Exception:
        note_error("learn_spreads")
    return SPREAD_FIT[c]


def _load_spreads():
    if not _SPREAD_LOADED[0]:
        _SPREAD_LOADED[0] = True
        for k, v in (load_json(_spreads_path(), {}) or {}).items():
            if isinstance(v, list) and len(v) >= 2 and k not in SPREAD_FIT:
                SPREAD_FIT[k] = v


def half_spread(c, prem_share):
    """Half the bid/ask gap as a fraction of a contract's price (what one fill costs), 0..0.5."""
    if prem_share <= 0:
        return 0.5
    _load_spreads()
    a, b = (SPREAD_FIT.get(c) or SPREAD_DEFAULT)[:2]
    return min(0.5, (a + b * prem_share) / 2 / prem_share)


def minutes_to_close(ts):
    """Minutes from ts until 4 pm New York on that day (negative after the close, over 390 before the 9:30 open)."""
    d = datetime.fromtimestamp(ts, timezone.utc)
    ny = d - timedelta(hours=_ny_offset(d))
    return (ny.replace(hour=16, minute=0, second=0, microsecond=0) - ny).total_seconds() / 60


# ---------------- settings ----------------
DEFAULT_SETTINGS = {
    "trade_pct": 10.0, "stop_loss": 5.0, "take_profit": 10.0, "daily_loss": 5.0,
    "bar_minutes": 1, "refresh": 2, "fee_pct": 0.005, "evolve_every": 24, "notify": True,
    "sound": False,              # (old setting, no longer used: SBF has its own chime now)
    "chime": True,               # play SBF's own sounds with notifications (not the Windows system sound)
    "chime_volume": 60,          # 0-100
    "sounds_off": [],            # which of SBF's sounds are switched off: trade, notify, hof, problem
    "notify_trades": False,      # also notify on every trade (off: only important events)
    "max_coin_pct": 30.0, "style": "Balanced", "keep_awake": True,
    "size_mult": {},  # manual-mode trade size per strategy name (1.0 = normal)
    # Wildcard: how winners and shaky strategies are treated
    "winners_trade_more": True,  # winners add to positions and get bigger trades
    "winner_boost": 2.5,         # biggest trade-size multiplier a top winner can earn
    "add_every": 3,              # decisions between add-on buys while a winner is holding
    "slow_every": 6,             # decisions between trades that only shaky strategies want
    "max_bots": 30,              # most strategies competing at once (14 to 200)
    "retire_hours": 12.0,        # replace a strategy that has not been winning for this long (0 = never)
    "fail_drop": 3.0,            # % drop from a strategy's recent peak that stops it at once (0 = off)
    "beat_hold": True,           # a strategy only counts as Winning if it also beats just holding
    "crossbreed": True,          # new strategies can mix the settings of two winners
    "regime_aware": True,        # favour strategies that do well in the current market mood
    # Hall of Fame: long-term winners are saved automatically
    "hof_hours": 6.0,            # hours a strategy must spend Winning (in total) to get in
    "hof_min_trades": 4,         # and at least this many finished (sold) trades
    "hof_load": True,            # bring the best Hall of Fame bots back when Wildcard starts
    "auto_catch": True,          # long-term winners are saved to your Bot Library automatically
    "auto_hof": False,           # ...and also put straight into the Hall of Fame (off: you choose who goes in)
    "animations": "Full",        # Full, Light (slower, lighter) or Off
    "characters": "Themed dragons",  # or "Classic dragons" (coloured by status)
    "egg_colors": "By dragon type",  # or "Purple"
    "auto_resume": True,         # resume the session that was running when the app closed
    "team_locked": True,         # trading with your team: keep exactly those bots (False = they may breed new ones)
    # Orders and costs
    "bot_stops": True,           # Wildcard: the account's safety stop / lock-in-profit follow what the winning bots learned
    "classics": True,            # mix famous strategies (golden cross, turtle, RSI-2...) into the eggs
    "classic_rate": 25,          # % of new eggs that are a famous strategy (when one isn't on the farm yet)
    "layers": True,              # bots can evolve extra rule layers: a confirming strategy, an exit strategy, a sizing rule
    "ctx_tickers": ["SPY", "VIXY", "TLT", "MSFT"],  # market context: watched (never traded); VIXY stands in for the VIX
    "ctx_studies": True,         # bots can evolve studies on the context tickers (VIX up = careful buying...)
    "htf_studies": True,         # bots can evolve a slower-timeframe study (the bigger trend) that must agree before buying
    "lead_study": True,          # bots can evolve a "market leads" study: SPY's recent move votes on other tickers
    "time_habits": True,         # bots can learn to skip the opening / closing rush (never-rules graded in hindsight)
    "agree_sizing": True,        # the account trades bigger when several different strategies agree, smaller when split
    "learn_losses": True,        # a bot takes on a never-rule itself when that rule would have stopped several of its losses
    "post_review": True,         # after losing trades the farm notes which never-rule would have helped, and tries those more
    "islands": 3,                # separate sub-farms that only swap a champion now and then (1 = one big farm)
    "adapt_exits": True,         # bots can evolve stops and targets that widen in wild markets and tighten in calm ones
    "mut_memory": True,          # the farm remembers which kinds of change make winners and does more of those
    "multicore": True,           # warm up the islands on several processor cores at once
    "shadow_live": True,         # follow your Hall of Fame bots with pretend money in real time (Shadow Live)
    "retest_nightly": False,     # once a day, re-backtest the Hall of Fame on the latest prices; stale ones get a RUSTY badge
    "graduation": True,          # before real money: check each Hall of Fame / team bot has earned it
    "grad_hours": 24.0,          # ...hours it must have spent Winning
    "trade_hours": "",           # account trades only in these hours, like "9-17" or "22-6" (empty = any time; stops always run)
    "weekdays_only": False,      # ...and only Monday to Friday
    "tg_remote": False,          # control SBF from Telegram: /status /pause /resume /report /kill
    "free_roam": False,          # (retired: Free roam only runs while its switch is ON)
    "free_roam_running": False,  # ...also while the bot runs (uses a little more computer power and network)
    "roam_reuse": 3,             # Free roam studies each downloaded stretch this many times before fetching a new one
    "feature_tests": True,       # Free roam tests what each learning switch is worth (reported, never changed for you)
    "report_detail": False,      # Export a report also adds the detailed part (bot trades, price feed, checks)
    "auto_exams": True,          # every 20 min: bots that need exams are tested on unseen history (no learning)
    "keep_variety": True,        # when one strategy fills half the farm, half the eggs try others; last 2 of a family go last
    "keep_veterans": True,       # bots with a winning lifetime record survive a bad patch (benched, not retired)
    "proven_only": True,         # only bots with a real track record decide the account's trades
    "proven_trades": 8,          # ...finished trades a bot needs first
    "proven_exam": True,         # ...and passed exams on prices it never trained on
    "alpaca_adopt_all": False,   # SBF manages every Alpaca share position nothing in SBF follows
    "slow_trades_account": False,  # the slow farm (hourly candles) may also trade your account
    "slow_size": 30,             # bots on the slow farm
    "coin_prefs": True,          # bots learn which coins they're bad at and skip them (re-checked in hindsight)
    "confidence_sizing": True,   # bots buy bigger when many of their studies agree, smaller on a close vote
    "twin_test": True,           # winners are re-run on an older stretch of prices; lucky ones count for less
    "elders": True,              # good bots that leave pass their settings on as a nudge to future eggs
    "seasons": True,             # the farm remembers a champion for each market mood and wakes it when that mood returns
    "bot_allowance": 25.0,       # account: most of the starting money one bot's votes may have tied up (% , 0 = no limit)
    "why_popup": True,           # LIVE: show a small "why did it trade?" note for every real trade
    "weekly_report": True,       # write a weekly report (and send it to Discord / Telegram if set up)
    "bg_mode": True,             # minimized: skip all drawing and check prices less often
    "tray_autotrain": True,      # started in the background (icon by the clock): train on practice prices right away
    "daily_recap": True,         # once a day: how the bots did vs holding (log + icon note)
    "auto_backup": True,         # weekly copy of bots/farms/settings in auto-backups/ (last 4, no keys)
    "vetoes": True,              # bots can evolve never-rules, each graded by what the blocked trade would have done
    "prove_live": True,          # a bot can only be Winning after it also made money on prices it hadn't seen yet
    "risk_adjusted": True,       # rank winners by gain per unit of downside swing, not raw gain
    "luck_check": True,          # a bot's score counts for less when it rests on only a few trades
    "exam_gate": True,           # a bot that fails its exams (days it never trained on) can't be a BEAST
    "cond_aware": True,          # each bot's say depends on its record in today's conditions (wild/calm, time, news)
    "think_between": True,       # a few times a minute the bots also decide on the candle that's still forming
    "long_holds": True,          # include slow, longer-holding bots from the start
    "show_broker_value": True,   # a tile with your Alpaca account's own value (logged in)
    "hindsight": True,           # bots learn from what would have worked (hindsight studies), and new eggs start from it
    "kamikazes": True,           # the kamikaze breed: big bets with wide stops, and the farm's rule testers
    "kami_share": 10,            # % of the farm kept for kamikazes (they have only a small say in your account)
    "scalpers": True,            # the scalp breed: fast in-and-out bots (tight stop + target, time limit, flat by the close)
    "scalp_share": 25,
    "scalp_spread": 0.15,        # scalpers skip stocks whose bid/ask gap is wider than this % of the price
    "min_hold_min": 0.0,         # account: minutes to hold before a strategy signal may sell (stops still sell)
    "fee_aware": True,           # don't sell for a profit smaller than the round-trip fees (losses and stops still sell)
    "max_trades_hour": 30,       # brake: most signal trades the account may make in an hour (0 = no limit)
    "trail_stop": 0.0,           # % trailing stop: sell if a stock falls this far from its high since buying (0 = off)
    "_broker": "alpaca",         # (kept in step with Account > broker)
    "pdt_guard": True,           # under $25k: never make a 4th day trade in 5 business days
    "max_spread_pct": 0.5,       # LIVE: skip a buy when the stock's bid/ask gap is wider than this % of the price (0 = off)
    "max_group_pct": 50,         # never more than this % of your money in stocks that move together
    "slippage": 0.03,            # % of the price lost to the bid/ask spread on each fill (Practice)
    "order_type": "limit",       # LIVE: limit orders at the ask (buys) / bid (sells), then market if they don't fill
    "maker_fee_pct": 0.0,        # (kept for old settings files: Practice uses fee_pct)
    "limit_wait": 20,            # seconds a limit order may wait for a fill
    "limit_fallback": True,      # if a limit order does not fill in time, use a market order instead
    # Alerts
    "alert_discord": "", "alert_tg_token": "", "alert_tg_chat": "",
    "alert_trades": True, "alert_hof": True, "alert_problems": True,
    # Coins
    "extra_coins": [],           # more tickers added from the ticker browser
    "auto_coins": 0,             # Wildcard: let the bot pick this many coins at start (0 = use the ticked ones)
}
STYLES = {  # risk presets: trade size, stop / target on the share price, daily limit, max in one ticker (all %)
    "Cautious": dict(trade_pct=5.0, stop_loss=3.0, take_profit=6.0, daily_loss=3.0, max_coin_pct=20.0),
    "Balanced": dict(trade_pct=10.0, stop_loss=5.0, take_profit=10.0, daily_loss=5.0, max_coin_pct=30.0),
    "Adventurous": dict(trade_pct=15.0, stop_loss=8.0, take_profit=20.0, daily_loss=10.0, max_coin_pct=45.0),
}


DEFAULT_SETTINGS.update(LIMIT_DEFAULTS)  # account-wide safety limits (farmcore)


def merge_settings(saved):
    s = dict(DEFAULT_SETTINGS)
    s["params"] = {n: dict(sp["params"]) for n, sp in STRATS.items()}
    for k, v in (saved or {}).items():
        if k == "params":
            for n, pp in (v or {}).items():
                if n in s["params"] and isinstance(pp, dict):
                    s["params"][n].update({pk: pv for pk, pv in pp.items() if pk in s["params"][n]})
        elif k in s:
            s[k] = v
    s["size_mult"] = dict(s.get("size_mult") or {})
    s["extra_coins"] = [c for c in (s.get("extra_coins") or []) if isinstance(c, str) and valid_coin(c)]
    if s["order_type"] not in ("market", "limit"):
        s["order_type"] = "market"
    try:
        s["bar_minutes"] = min(BAR_CHOICES, key=lambda b: abs(b - float(s["bar_minutes"])))
    except (TypeError, ValueError):
        s["bar_minutes"] = 1
    return s


def valid_coin(c):
    """A stock / ETF ticker like SPY, AAPL or BRK.B."""
    return bool(re.fullmatch(r"[A-Z][A-Z.]{0,5}", c or ""))


def cost_model(settings):
    """(fee, slippage) as fractions for simulated share trades: Alpaca charges no commission on stocks, so the fee is
    only the tiny regulatory charges on sells, and the slippage is half the bid/ask gap a fill pays."""
    return float(settings.get("fee_pct", 0.005)) / 100, float(settings.get("slippage", 0.03)) / 100


# ---------------- market mood ----------------


# ---------------- simulated portfolio ----------------
class Sim:
    def __init__(self, cash, fee, pct, max_coin=None, slip=0.0):
        self.start, self.cash, self.fee, self.pct, self.max_coin = cash, cash, fee, pct, max_coin
        self.slip = slip      # spread + slippage: buys fill a bit higher, sells a bit lower
        self.fees = 0.0       # total fees paid
        self.pos, self.cost = {}, {}
        self.tbars = deque()  # bar indices of recent trades
        self.last = None      # details of the trade just made (read and cleared by the Tournament)

    def buy(self, c, px, n=0, mult=1.0):
        spend = min(self.start * self.pct * mult, self.cash)
        if self.max_coin:
            spend = min(spend, self.start * self.max_coin - self.cost.get(c, 0))
        if spend < 1:
            return False
        fill = px * (1 + self.slip)
        self.cash -= spend
        self.fees += spend * self.fee
        qty = spend * (1 - self.fee) / fill
        self.pos[c] = self.pos.get(c, 0) + qty
        self.cost[c] = self.cost.get(c, 0) + spend
        self.tbars.append(n)
        self.last = dict(side="buy", coin=c, qty=qty, price=fill, usd=spend, pl=None)
        return True

    def sell(self, c, px, n=0):
        q = self.pos.get(c, 0)
        if q <= 0:
            return False
        fill = px * (1 - self.slip)
        proceeds = q * fill * (1 - self.fee)
        self.fees += q * fill * self.fee
        self.cash += proceeds
        self.last = dict(side="sell", coin=c, qty=q, price=fill, usd=proceeds, pl=proceeds - self.cost.get(c, 0))
        self.pos[c], self.cost[c] = 0, 0
        self.tbars.append(n)
        return True

    def act(self, c, sig, px, n=0):
        if sig == "buy":
            self.buy(c, px, n)
        elif sig == "sell":
            self.sell(c, px, n)

    def value_of(self, c, px):
        """What this account's holding of c is worth at price px."""
        return self.pos.get(c, 0) * px

    def equity(self, last):
        return self.cash + sum(q * last.get(c, 0) for c, q in self.pos.items())


# ---------------- options: pricing and a pretend options account ----------------
CONTRACT = 100            # shares per option contract
RATE = 0.04               # risk-free rate used for pricing
YEAR = 365.0 * 86400
IV_HINT = {}              # underlying -> implied volatility from the live option chain (used when known)
_VOL_CACHE = {}


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S, K, T, vol, typ):
    """Black-Scholes price of one share's worth of a European option (T in years)."""
    if S <= 0 or K <= 0:
        return 0.0
    if T <= 0 or vol <= 0:
        return max(0.0, S - K) if typ == "call" else max(0.0, K - S)
    sq = vol * math.sqrt(T)
    d1 = (math.log(S / K) + (RATE + vol * vol / 2) * T) / sq
    d2 = d1 - sq
    if typ == "call":
        return S * _ncdf(d1) - K * math.exp(-RATE * T) * _ncdf(d2)
    return K * math.exp(-RATE * T) * _ncdf(-d2) - S * _ncdf(-d1)


def bs_greeks(S, K, T, vol, typ):
    """(delta, gamma, theta per day, vega per 1 vol point) for one share's worth."""
    if S <= 0 or K <= 0 or T <= 0 or vol <= 0:
        itm = (S > K) if typ == "call" else (S < K)
        return (1.0 if itm else 0.0) * (1 if typ == "call" else -1), 0.0, 0.0, 0.0
    sq = vol * math.sqrt(T)
    d1 = (math.log(S / K) + (RATE + vol * vol / 2) * T) / sq
    d2 = d1 - sq
    pdf = math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi)
    delta = _ncdf(d1) if typ == "call" else _ncdf(d1) - 1
    gamma = pdf / (S * sq)
    th = -S * pdf * vol / (2 * math.sqrt(T))
    th += (-RATE * K * math.exp(-RATE * T) * _ncdf(d2)) if typ == "call" else (RATE * K * math.exp(-RATE * T) * _ncdf(-d2))
    return delta, gamma, th / 365.0, S * pdf * math.sqrt(T) / 100


def implied_vol(price, S, K, T, typ):
    """Volatility that makes Black-Scholes match a market price (None if it can't)."""
    if price <= 0 or T <= 0:
        return None
    lo, hi = 0.01, 5.0
    if bs_price(S, K, T, hi, typ) < price:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs_price(S, K, T, mid, typ) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def realized_vol(p, times=None, n=120):
    """Annualised volatility of the last n bars (uses the bar times when known)."""
    L = len(p)
    if L < 10:
        return 0.35
    key = (id(getattr(p, "seq", p)), L, n)
    v = _VOL_CACHE.get(key)
    if v is not None:
        return v
    seg = p[-min(n, L):] if not isinstance(p, View) else [p[i] for i in range(max(0, L - n), L)]
    rets = [math.log(seg[i] / seg[i - 1]) for i in range(1, len(seg)) if seg[i - 1] > 0 and seg[i] > 0]
    if len(rets) < 5:
        return 0.35
    m = sum(rets) / len(rets)
    sd = (sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
    step = 60.0
    if times is not None and len(times) >= 3:
        diffs = [times[-k] - times[-k - 1] for k in range(1, min(20, len(times) - 1)) if times[-k] > times[-k - 1]]
        if diffs:
            step = max(1.0, sorted(diffs)[len(diffs) // 2])
    per_year = 252 * 6.5 * 3600 / step if step < 86400 else 252 * 86400 / step
    v = min(3.0, max(0.08, sd * math.sqrt(per_year)))
    if len(_VOL_CACHE) > 4000:
        _VOL_CACHE.clear()
    _VOL_CACHE[key] = v
    return v


def pricing_vol(p, times=None):
    """The volatility practice contracts are priced with when the market's own (implied) volatility isn't known: about
    a week of candles (a few months on daily candles), not the last 120. On short candles the last 120 can jump from
    calm to wild in minutes, which repriced cheap contracts hundreds of times over on a move of a few percent: a gain
    that only existed in the model, which the farm then learned to chase. Only moves from one candle to the next count:
    a jump over a night, a weekend or a halt (earnings, say) isn't a one-minute move, and counting it as one made
    measured volatility several times too high."""
    L = len(p)
    if L < 10:
        return 0.35
    if times is None or len(times) != L:
        step = 60.0
        if times is not None and len(times) >= 3:
            diffs = [times[-k] - times[-k - 1] for k in range(1, min(20, len(times) - 1)) if times[-k] > times[-k - 1]]
            if diffs:
                step = max(1.0, sorted(diffs)[len(diffs) // 2])
        n = 120 if step >= 86400 else int(min(2400, max(120, 5 * 6.5 * 3600 / step)))
        return max(PRICING_VOL_FLOOR, realized_vol(p, times, n))
    key = ("pv", id(getattr(p, "seq", p)), L // 30)  # (a week-long measure barely moves: worked out every 30 candles)
    v = _VOL_CACHE.get(key)
    if v is not None:
        return v
    tail = [times[i] for i in range(max(0, L - 21), L)]
    diffs = sorted(b - a for a, b in zip(tail, tail[1:]) if b > a)
    step = max(1.0, diffs[len(diffs) // 2]) if diffs else 60.0
    n = 120 if step >= 86400 else int(min(2400, max(120, 5 * 6.5 * 3600 / step)))
    lo = max(1, L - n)
    rets = []
    for i in range(lo, L):
        a, b, ta, tb = p[i - 1], p[i], times[i - 1], times[i]
        if a > 0 and b > 0 and 0 < tb - ta <= (step * 1.5 if step < 86400 else step * 4):
            rets.append(math.log(b / a))
    if len(rets) < 5:
        return max(PRICING_VOL_FLOOR, realized_vol(p, times, n))
    m = sum(rets) / len(rets)
    sd = (sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
    per_year = 252 * 6.5 * 3600 / step if step < 86400 else 252 * 86400 / step
    v = max(PRICING_VOL_FLOOR, min(3.0, sd * math.sqrt(per_year)))
    if len(_VOL_CACHE) > 4000:
        _VOL_CACHE.clear()
    _VOL_CACHE[key] = v
    return v


PRICING_VOL_FLOOR = 0.12  # (no listed option trades below about this volatility for long)
MIN_PREMIUM = 5.0          # $ per contract: real far-out options rarely sell for less than $0.05 a share


CONTRACT_FEE = [0.04]    # $ per contract per side (commission + regulatory fees), set from the broker


def broker_contract_fee(broker, setting=-1.0):
    """Per-contract fee: the setting if you gave one, else the broker's usual (Alpaca: no commission, about $0.04 of
    regulatory fees; IBKR: about $0.65 commission plus the same fees)."""
    try:
        x = float(setting)
    except (TypeError, ValueError):
        x = -1.0
    if x >= 0:
        return x
    return 0.69 if broker == "ibkr" else 0.04


def ny_date(ts):
    d = datetime.fromtimestamp(ts, timezone.utc)
    return (d - timedelta(hours=_ny_offset(d))).date()


def sessions_left(exp, now=None):
    """Trading days from today until expiry day (expiry day counts, today doesn't): Wednesday for a Friday expiry = 2,
    Thursday = 1, expiry day itself = 0. Weekends are skipped (holidays aren't, so it errs on the early side)."""
    a, b = ny_date(now or time.time()), ny_date(exp)
    n, d = 0, a
    while d < b and n < 400:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


PREM_RANGE = [0.0, 0.0]  # contract price range bots may buy, $ per share; 0 = no limit on that side


def set_prem_range(lo, hi):
    try:
        lo, hi = max(0.0, float(lo or 0)), max(0.0, float(hi or 0))
    except (TypeError, ValueError):
        lo, hi = 0.0, 0.0
    if lo and hi and lo > hi:
        lo, hi = hi, lo
    PREM_RANGE[:] = [lo, hi]


def prem_ok(p):
    """Is a premium ($ per share) inside the chosen contract price range?"""
    lo, hi = PREM_RANGE
    return (not lo or p >= lo) and (not hi or p <= hi)


def prem_range_text():
    lo, hi = PREM_RANGE
    if not lo and not hi:
        return ""
    if lo and hi:
        return f"${lo:,.2f} to ${hi:,.2f} a share (${lo * 100:,.0f} to ${hi * 100:,.0f} a contract)"
    return (f"at least ${lo:,.2f} a share" if lo else f"at most ${hi:,.2f} a share (${hi * 100:,.0f} a contract)")


def strike_step(S):
    return 0.5 if S < 25 else 1.0 if S < 200 else 5.0 if S < 1000 else 10.0


def next_expiry(now, dte):
    """The Friday (4 pm New York, 20:00 UTC) on or after now + dte days, as a timestamp."""
    t = now + dte * 86400
    d = datetime.fromtimestamp(t, timezone.utc)
    ahead = (4 - d.weekday()) % 7
    fri = (d + timedelta(days=ahead)).replace(hour=20, minute=0, second=0, microsecond=0)
    if fri.timestamp() <= now + 3600:
        fri += timedelta(days=7)
    return fri.timestamp()


def occ_symbol(under, exp_ts, typ, K):
    """OCC option symbol, e.g. SPY250117C00450000."""
    d = datetime.fromtimestamp(exp_ts, timezone.utc)
    return f"{under}{d:%y%m%d}{'C' if typ == 'call' else 'P'}{int(round(K * 1000)):08d}"


def contract_text(o):
    d = datetime.fromtimestamp(o["exp"], timezone.utc)
    return f"{o.get('under', '')} {d:%b %d} {o['K']:g} {'call' if o['type'] == 'call' else 'put'}".strip()


class OptionSim(Sim):
    """Pretend options account for one bot. A buy signal buys a call (or closes a put), a sell signal sells the call
    (or buys a put). The bot's genes (traits) choose calls / puts / both, days to expiry, how far out of the money,
    and how many days before expiry to get out. Contracts are priced with Black-Scholes from the stock's recent swings
    (or the live implied volatility), and every fill pays the spread."""

    def __init__(self, cash, fee, pct, max_coin=None, slip=0.0, genes=None):
        super().__init__(cash, fee, pct, max_coin, slip)
        self.slip = max(slip, 0.02)  # half the bid/ask spread on option premiums
        self.genes = genes if genes is not None else {}
        self.opt = {}               # underlying -> {type, K, exp}
        self.now = time.time()
        self.vol_src = {}
        self.book = None            # past-day replays: {ticker: day_option_book(...)}: real listed contracts and prices
        self.real_fills = self.est_fills = 0  # fills priced from real option history vs estimated

    def _vol(self, c, closes=None, times=None):
        iv = IV_HINT.get(c)
        if iv:
            return iv
        return self.vol_src.get(c, 0.35)

    def mark(self, c, S):
        """Value of one contract right now."""
        if not S:
            return 0.0
        return self.mark_one(c, S)

    def _real_px(self, o):
        """The contract's real price (its last 1-minute bar at or before now, within 30 minutes), or None."""
        if not self.book or not o or not o.get("symbol"):
            return None
        b = (self.book.get(o.get("under")) or {}).get("bars", {}).get(o["symbol"])
        if not b:
            return None
        i = bisect.bisect_right(b[0], self.now) - 1
        if i < 0 or self.now - b[0][i] > 1800 or b[1][i] <= 0:
            return None
        return b[1][i] * CONTRACT

    def _real_slip(self, c, o, prem):
        """On a past day with real option history: half the bid/ask gap this contract really costs to trade (learned
        from live quotes of the same ticker), widened when its last real trade is getting old (a quiet contract's
        last price says less about what you could trade it at now). None when the price isn't a real one."""
        if not self.book or not o or not o.get("symbol"):
            return None
        b = (self.book.get(o.get("under") or c) or {}).get("bars", {}).get(o["symbol"])
        if not b:
            return None
        i = bisect.bisect_right(b[0], self.now) - 1
        if i < 0 or self.now - b[0][i] > 1800:
            return None
        hs = half_spread(c, prem / CONTRACT)
        age = self.now - b[0][i]
        return min(0.5, hs * (1 + min(1.0, max(0.0, age - 120) / 900)))

    def _pick_listed(self, c, typ, K, exp, S):
        """The real listed contract closest to the one wanted (that day's option history), or None."""
        bk = (self.book or {}).get(c)
        if not bk:
            return None
        dte = max(1.0, (exp - self.now) / 86400)
        cands = [x for x in bk["contracts"] if x["type"] == typ and x["exp"] > self.now + 86400
                 and self._real_px(dict(x, under=c))]
        if not cands:
            return None
        return min(cands, key=lambda x: abs(x["exp"] - exp) / 86400 / max(3.0, dte) + abs(x["K"] - K) / S * 10)

    def value_of(self, c, S):
        return self.pos.get(c, 0) * self.mark(c, S)

    def _open(self, c, typ, S, n, mult=1.0):
        g = self.genes or {}
        mode = int(g.get("omode", 3) or 3)
        if (typ == "call" and mode == 2) or (typ == "put" and mode == 1):
            return False
        otm = float(g.get("otm", 0) or 0) / 100
        step = strike_step(S)
        K = round((S * (1 + otm) if typ == "call" else S * (1 - otm)) / step) * step
        exp = next_expiry(self.now, int(g.get("odte", 14) or 14))
        self.opt[c] = dict(type=typ, K=K, exp=exp, under=c)
        real = self._pick_listed(c, typ, K, exp, S) if self.book else None
        if real:  # a past day with real option history: trade the listed contract at what it really traded at
            self.opt[c].update(K=real["K"], exp=real["exp"], symbol=real["symbol"])
            K, step = real["K"], strike_step(S)
        prem = self.mark_one(c, S)
        if (PREM_RANGE[0] or PREM_RANGE[1]) and not prem_ok(prem / CONTRACT):
            # outside the price range: walk the strike (further out of the money = cheaper, closer in = dearer)
            cheaper = 1 if typ == "call" else -1
            way = cheaper if (PREM_RANGE[1] and prem / CONTRACT > PREM_RANGE[1]) else -cheaper
            for _ in range(60):
                K = K + way * step
                if K <= 0:
                    break
                self.opt[c]["K"] = K
                self.opt[c].pop("symbol", None)  # walked off the real contract: estimated from here
                prem = self.mark_one(c, S)
                if prem_ok(prem / CONTRACT):
                    break
                if (way == cheaper and PREM_RANGE[0] and prem / CONTRACT < PREM_RANGE[0]) or \
                        (way != cheaper and PREM_RANGE[1] and prem / CONTRACT > PREM_RANGE[1]):
                    break  # stepped right over the range: no contract fits
            if not prem_ok(prem / CONTRACT):
                self.opt.pop(c, None)
                return False
        if prem < MIN_PREMIUM:  # (real contracts this cheap hardly ever trade)
            self.opt.pop(c, None)
            return False
        slip = self.slip
        rs = self._real_slip(c, self.opt[c], prem)
        if rs is not None:  # a real contract on a past day: buys pay the ask (its real bid/ask gap), for every bot
            slip = max(rs, half_spread(c, prem / CONTRACT) if g.get("flatby") else 0.0)
        if g.get("flatby"):  # scalper: the real bid/ask gap for this contract, and only tight ones
            slip = half_spread(c, prem / CONTRACT) if rs is None else slip
            if 2 * slip * 100 > SCALP_SPREAD[0]:
                self.opt.pop(c, None)
                return False
        fill = prem * (1 + slip)
        spend = min(self.start * self.pct * mult, self.cash)
        qty = int(spend // fill)
        if qty < 1 and fill <= min(self.cash * 0.3, spend * 2.5):
            qty = 1  # one contract is the smallest trade there is
        if qty < 1:
            self.opt.pop(c, None)
            return False
        cost = qty * fill * (1 + self.fee) + qty * CONTRACT_FEE[0]
        if cost > self.cash:
            self.opt.pop(c, None)
            return False
        self.cash -= cost
        self.fees += qty * fill * self.fee + qty * prem * slip + qty * CONTRACT_FEE[0]
        self.pos[c] = qty
        self.cost[c] = cost
        self.tbars.append(n)
        self.last = dict(side="buy", coin=c, qty=qty, price=fill, usd=cost, pl=None, contract=dict(self.opt[c]))
        self._count_fill(self.opt[c])
        return True

    def mark_one(self, c, S):
        o = self.opt.get(c)
        if not o:
            return 0.0
        real = self._real_px(o) if self.book else None
        if real:
            return real
        T = max(0.0, o["exp"] - self.now) / YEAR
        return bs_price(S, o["K"], T, self._vol(c), o["type"]) * CONTRACT

    def _count_fill(self, o):
        if self.book is not None:
            if self._real_px(o):
                self.real_fills += 1
            else:
                self.est_fills += 1

    def buy(self, c, px, n=0, mult=1.0):
        o = self.opt.get(c)
        if o and self.pos.get(c, 0) > 0:
            if o["type"] == "put":
                return self.sell(c, px, n)
            return False  # already holding a call: no adding to options
        return self._open(c, "call", px, n, mult)

    def sell(self, c, px, n=0):
        q = self.pos.get(c, 0)
        if q <= 0:
            return False
        o = dict(self.opt.get(c) or {})
        prem = self.mark_one(c, px)
        self._count_fill(o)
        expired = o and self.now >= o["exp"]
        slip = half_spread(c, prem / CONTRACT) if (self.genes or {}).get("flatby") else self.slip
        rs = self._real_slip(c, o, prem)
        if rs is not None:  # a real contract on a past day: sells get the bid
            slip = max(slip if (self.genes or {}).get("flatby") else 0.0, rs)
        fill = prem if expired else prem * (1 - slip)
        proceeds = q * fill * (1 - (0 if expired else self.fee)) - (0 if expired else q * CONTRACT_FEE[0])
        self.fees += 0 if expired else q * prem * slip + q * fill * self.fee + q * CONTRACT_FEE[0]
        self.cash += proceeds
        self.last = dict(side="sell", coin=c, qty=q, price=fill, usd=proceeds, pl=proceeds - self.cost.get(c, 0), contract=o)
        self.pos[c], self.cost[c] = 0, 0
        self.opt.pop(c, None)
        self.tbars.append(n)
        return True

    def act(self, c, sig, px, n=0):
        if sig == "buy":
            self.buy(c, px, n)
        elif sig == "sell":
            if self.pos.get(c, 0) > 0:
                o = self.opt.get(c) or {}
                if o.get("type") == "call":
                    self.sell(c, px, n)
            else:
                self._open(c, "put", px, n)

    def equity(self, last):
        return self.cash + sum(q * self.mark_one(c, last.get(c, 0)) for c, q in self.pos.items() if q > 0)


OPTIONS = False  # this app trades shares: every bot gets a plain share account (OptionSim is kept for imported options bots)


def make_sim(cash, fee, pct, max_coin=None, slip=0.0, genes=None):
    return OptionSim(cash, fee, pct, max_coin, slip, genes) if OPTIONS else Sim(cash, fee, pct, max_coin, slip)


def align_ctx(times, ctx_times, ctx_closes):
    """A context ticker's prices lined up with another ticker's bar times (each bar gets the latest known price)."""
    out, j, last = [], 0, None
    for t in times:
        while j < len(ctx_times) and ctx_times[j] <= t:
            last = ctx_closes[j]
            j += 1
        out.append(last if last is not None else (ctx_closes[0] if ctx_closes else 0.0))
    return out


def lead_ticker(closes):
    """The market leader the 'follow the leader' study watches: SPY (or QQQ) when the farm trades it."""
    for t in ("SPY", "QQQ", "BTC"):
        if t in closes:
            return t
    return None


# ---------------- wildcard tournament ----------------
def tournament_opts(settings):
    """Picks the winner/shaky-strategy knobs out of the app settings."""
    fee, slip = cost_model(settings)
    set_prem_range(settings.get("prem_min", 0), settings.get("prem_max", 0))
    CONTRACT_FEE[0] = broker_contract_fee(settings.get("_broker", "alpaca"), settings.get("contract_fee", -1))
    return dict(trade_more=bool(settings.get("winners_trade_more", True)),
                winner_boost=float(settings.get("winner_boost", 2.5)),
                add_every=int(settings.get("add_every", 3)),
                slow_every=int(settings.get("slow_every", 6)),
                fail_drop=float(settings.get("fail_drop", 3.0)),
                beat_hold=bool(settings.get("beat_hold", True)),
                crossbreed=bool(settings.get("crossbreed", True)),
                regime_aware=bool(settings.get("regime_aware", True)),
                slip=slip, fee_aware=bool(settings.get("fee_aware", True)),
                prove_live=bool(settings.get("prove_live", True)), risk_adjusted=bool(settings.get("risk_adjusted", True)), luck_check=bool(settings.get("luck_check", True)),
                exam_gate=bool(settings.get("exam_gate", True)), cond_aware=bool(settings.get("cond_aware", True)),
                classics=bool(settings.get("classics", True)), classic_rate=float(settings.get("classic_rate", 25)) / 100,
                vetoes=bool(settings.get("vetoes", True)), layers=bool(settings.get("layers", True)),
                long_holds=bool(settings.get("long_holds", True)),
                scalp_share=scalp_share(settings), hindsight=bool(settings.get("hindsight", True)),
                kami_share=(min(0.3, max(0.0, float(settings.get("kami_share", 10) or 0) / 100))
                            if settings.get("kamikazes", True) else 0.0),
                coin_prefs=bool(settings.get("coin_prefs", True)), confidence_sizing=bool(settings.get("confidence_sizing", True)),
                twin_test=bool(settings.get("twin_test", True)), elders=bool(settings.get("elders", True)),
                seasons=bool(settings.get("seasons", True)),
                htf_studies=bool(settings.get("htf_studies", True)), lead_study=bool(settings.get("lead_study", True)),
                ctx=[c for c in (settings.get("ctx_tickers") or []) if valid_coin(c)] if settings.get("ctx_studies", True) else [],
                post_review=bool(settings.get("post_review", True)), learn_losses=bool(settings.get("learn_losses", True)),
                time_habits=bool(settings.get("time_habits", True)), agree_sizing=bool(settings.get("agree_sizing", True)), islands=max(1, min(6, int(settings.get("islands", 3) or 1))),
                adapt_exits=bool(settings.get("adapt_exits", True)), mut_memory=bool(settings.get("mut_memory", True)),
                proven_only=bool(settings.get("proven_only", True)), keep_veterans=bool(settings.get("keep_veterans", True)), keep_variety=bool(settings.get("keep_variety", True)), proven_exam=bool(settings.get("proven_exam", True)),
                proven_trades=int(settings.get("proven_trades", 8) or 0),
                prem=list(PREM_RANGE), cfee=CONTRACT_FEE[0], sspread=_set_scalp_spread(settings))


def scalp_share(settings):
    """Share of the fast farm (0..0.6) kept for the scalp breed. 0 when switched off or when the bars are slower
    than 5 minutes (a scalper deciding once an hour isn't scalping)."""
    try:
        if not settings.get("scalpers", True) or float(settings.get("bar_minutes", 1)) > 5:
            return 0.0
        return min(0.6, max(0.0, float(settings.get("scalp_share", 25) or 0) / 100))
    except (TypeError, ValueError):
        return 0.0


def _set_scalp_spread(settings):
    try:
        SCALP_SPREAD[0] = max(0.02, float(settings.get("scalp_spread", 0.15) or 0.15))
    except (TypeError, ValueError):
        SCALP_SPREAD[0] = 0.15
    return SCALP_SPREAD[0]


def opts_for_bars(opts, gran_seconds):
    """Farm options for a farm running on candles of this size: no scalp breed on candles slower than 5 minutes."""
    return dict(opts, scalp_share=0.0) if float(gran_seconds or 0) > 300 else dict(opts)


RISK_BOUNDS = {"sl": (0.15, 25.0), "tp": (0.2, 60.0)}  # a bot's own stop / target on the share price it paid, % (0 = none)
FAST_RISK = {"sl": 8.0, "tp": 15.0}    # the usual farm evolves stops up to these (a stock rarely moves 8% in a day)
SLOW_RISK = {"sl": 25.0, "tp": 60.0}   # the slow farm (holds of days to weeks) can go this wide


def clean_risk(r):
    out = {}
    for k, (lo, hi) in RISK_BOUNDS.items():
        try:
            v = float((r or {}).get(k, 0) or 0)
        except (TypeError, ValueError):
            v = 0.0
        out[k] = 0.0 if v <= 0 else round(min(max(v, lo), hi), 2)
    return out


# Traits: extra habits every bot can evolve on top of its strategy. 0 / empty = off.
TRAITS = {
    "trail":   dict(lo=0.5, hi=12.0, kind=float, text=lambda v: f"trailing stop {v:g}%"),
    "maxhold": dict(lo=3, hi=200, kind=int, text=lambda v: f"sells after {v} decisions"),
    "minhold": dict(lo=2, hi=300, kind=int, text=lambda v: f"holds at least {v} decisions"),
    "cool":    dict(lo=1, hi=30, kind=int, text=lambda v: f"waits {v} decisions after selling"),
    "trend":   dict(lo=20, hi=250, kind=int, text=lambda v: f"buys only above its {v}-bar average"),
    "minvol":  dict(lo=0.02, hi=1.5, kind=float, text=lambda v: f"skips markets moving under {v:g}% a bar"),
    "rsicap":  dict(lo=55, hi=90, kind=int, text=lambda v: f"won't buy if RSI is over {v}"),
    "confirm": dict(lo=2, hi=3, kind=int, text=lambda v: f"waits for {v} signals in a row"),
    "entries": dict(lo=1, hi=5, kind=int, text=lambda v: f"buys at most {v}x per position"),
    "exit":    dict(lo=1, hi=1, kind=int, text=lambda v: "sells only through its stops"),
    "adapt":   dict(lo=1, hi=1, kind=int, text=lambda v: "stops widen in wild markets, tighten in calm ones"),
    "omode":   dict(lo=1, hi=3, kind=int, text=lambda v: {1: "calls only", 2: "puts only", 3: "calls and puts"}.get(int(v), "calls and puts")),
    "odte":    dict(lo=1, hi=60, kind=int, text=lambda v: f"buys contracts about {v} days from expiry"),
    "otm":     dict(lo=0.25, hi=12.0, kind=float, text=lambda v: f"strikes {v:g}% out of the money"),
    "oexit":   dict(lo=1, hi=10, kind=int, text=lambda v: f"gets out {v} day{'s' if v != 1 else ''} before expiry"),
    "flatby":  dict(lo=5, hi=45, kind=int, text=lambda v: f"scalper: flat {v} min before the close, never overnight"),
    "vixmode": dict(lo=1, hi=5, kind=int, text=lambda v: VIX_MODES.get(int(v), VIX_MODES[5])),
    "kami":    dict(lo=1, hi=1, kind=int, text=lambda v: "kamikaze: rides big moves with a wide stop, and a rule tester"),
    "vixwide": dict(lo=1.2, hi=3.0, kind=float, text=lambda v: f"widens its stop and target up to {v:g}x when volatility jumps"),
    "moods":   dict(lo=1, hi=14, kind=int, text=lambda v: "trades only when " + " / ".join(
        m for i, m in enumerate(MOODS) if v & (1 << i))),
}
TRAIT_NAMES = {"trail": "Trailing stop", "maxhold": "Max hold", "minhold": "Min hold", "cool": "Cooldown", "trend": "Trend filter",
               "minvol": "Min volatility", "rsicap": "Overbought guard", "confirm": "Confirmation",
               "entries": "Max entries", "exit": "Stops-only exit", "moods": "Mood gate", "adapt": "Adaptive stops",
               "omode": "Calls / puts (1 calls, 2 puts, 3 both)", "odte": "Days to expiry", "otm": "% out of the money",
               "oexit": "Exit days before expiry", "flatby": "Flat before the close (minutes, scalpers)",
               "vixmode": "Volatility gate (1 calm, 2 rising, 3 spikes, 4 not spikes, 5 any)",
               "vixwide": "Wider exits when volatility jumps (x)", "kami": "Kamikaze (1 = yes)"}
OPTION_TRAITS = ("omode", "odte", "otm", "oexit")  # option-contract choices: only an options farm grows them
if not OPTIONS:
    for _k in OPTION_TRAITS:
        TRAITS.pop(_k, None)
        TRAIT_NAMES.pop(_k, None)


def clean_traits(t):
    out = {}
    for k, spec in TRAITS.items():
        try:
            v = float((t or {}).get(k, 0) or 0)
        except (TypeError, ValueError):
            v = 0
        if v <= 0:
            continue
        v = min(max(v, spec["lo"]), spec["hi"])
        out[k] = int(round(v)) if spec["kind"] is int else round(v, 2)
    return out


def traits_text(t):
    return ", ".join(TRAITS[k]["text"](v) for k, v in sorted((t or {}).items()) if k in TRAITS)


# ---- "never" rules: things a bot refuses to do. Each one is graded by what the blocked trade would have done. ----
VETOES = {
    "knife":   dict(lo=1.0, hi=15.0, side="buy", name="No falling knives",
                    text=lambda x: f"never buys after a {x:g}% drop in the last 10 decisions"),
    "spike":   dict(lo=0.3, hi=5.0, side="buy", name="No spike buying",
                    text=lambda x: f"never buys right after a {x:g}%+ jump in one bar"),
    "chase":   dict(lo=1.0, hi=10.0, side="buy", name="No chasing",
                    text=lambda x: f"never buys more than {x:g}% above its 20-decision average"),
    "revenge": dict(lo=1, hi=50, side="buy", name="No revenge trades",
                    text=lambda x: f"never re-buys a ticker within {int(x)} decisions of losing on it"),
    "thin":    dict(lo=0.2, hi=1.0, side="buy", name="No thin markets",
                    text=lambda x: f"never buys when volume is under {x:g}x normal"),
    "mktdown": dict(lo=0.3, hi=5.0, side="buy", name="No buying into a sell-off",
                    text=lambda x: f"never buys while the market (SPY) is down {x:g}%+ over 10 decisions"),
    "preev":   dict(lo=2, hi=72, side="open", name="Quiet before big news",
                    text=lambda x: f"never buys in the {x:g} hours before CPI, the jobs report or a Fed decision"),
    "prefed":  dict(lo=2, hi=72, side="open", name="Quiet before the Fed",
                    text=lambda x: f"never buys in the {x:g} hours before a Fed decision"),
    "postev":  dict(lo=0.5, hi=24, side="open", name="Let the news settle",
                    text=lambda x: f"never buys in the {x:g} hours after CPI, the jobs report or a Fed decision"),
    "opening": dict(lo=5, hi=90, side="open", name="Skip the opening rush",
                    text=lambda x: f"never buys in the first {int(x)} minutes after the 9:30 open (New York)"),
    "closing": dict(lo=5, hi=90, side="open", name="Skip the closing rush",
                    text=lambda x: f"never buys in the last {int(x)} minutes before the 4 pm close (New York)"),
    "panic":   dict(lo=0.5, hi=5.0, side="sell", name="No panic selling",
                    text=lambda x: f"never sells on its strategy right after a {x:g}%+ one-bar drop (stops still work)"),
    "gap":     dict(lo=0.3, hi=4.0, side="open", name="No gap days",
                    text=lambda x: f"never buys on a day that opened {x:g}%+ away from the day before's close"),
    "weekday": dict(lo=1, hi=5, side="open", name="Skips one weekday",
                    text=lambda x: f"never buys on a {WEEKDAYS[int(x) - 1]}"),
    "pricey":  dict(lo=1.2, hi=3.0, side="open", name="Skips wild swings",
                    text=lambda x: f"never buys while the ticker swings {x:g}x more than usual (stops get hit more "
                                   f"then)"),
    "newsday": dict(lo=1, hi=1, side="open", name="Sits out news days",
                    text=lambda x: "never buys on a CPI, jobs-report or Fed day"),
}
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
EVENT_VETOES = ("preev", "prefed", "postev", "newsday")  # only grown once the economic calendar is downloaded
TIME_VETOES = ("opening", "closing", "gap")  # time-of-day habits (only on bars shorter than a day)


def clean_vetoes(d):
    out = {}
    for k, spec in VETOES.items():
        try:
            x = float((d or {}).get(k, 0) or 0)
        except (TypeError, ValueError):
            x = 0
        if x > 0 and len(out) < MAX_VETOES:
            x = min(max(x, spec["lo"]), spec["hi"])
            out[k] = int(round(x)) if k in ("revenge", "opening", "closing", "weekday", "newsday") else round(x, 2)
    return out


def vetoes_text(d, stats=None):
    parts = []
    for k, x in (d or {}).items():
        if k in VETOES:
            t = VETOES[k]["text"](x)
            st = (stats or {}).get(k)
            if st and st[0]:
                t += f" ({'saved' if st[1] >= 0 else 'cost'} {abs(st[1]):.1f}% over {int(st[0])} vetoes)"
            parts.append(t)
    return "; ".join(parts)


def _veto(v, k, x, c, closes, n, extra, coins):
    """True if never-rule k (value x) blocks a trade on coin c right now."""
    p = closes[c]
    L = 10 * v.tf
    if len(p) < L + 2:
        return False
    if k == "knife":
        return (p[-1] / p[-1 - L] - 1) * 100 <= -x
    if k == "spike":
        return abs(p[-1] / p[-2] - 1) * 100 >= x
    if k == "chase":
        seg = tail_series(p, v.tf, 21)
        return len(seg) >= 20 and (seg[-1] / (sum(seg[-20:]) / 20) - 1) * 100 > x
    if k == "revenge":
        return n - v.lost_at.get(c, -10 ** 9) < x * v.tf
    if k == "thin":
        vol = ((extra or {}).get(c) or {}).get("v")
        if not vol or len(vol) < L + 1:
            return False
        base = sum(vol[-L - 1:-1]) / L
        return base > 0 and vol[-1] < x * base
    if k in TIME_VETOES:
        d = datetime.fromtimestamp(_bar_time(extra, c, v.sim), timezone.utc)
        ny = d - timedelta(hours=_ny_offset(d))
        m = ny.hour * 60 + ny.minute - (9 * 60 + 30)  # minutes since the open
        return (0 <= m < x) if k == "opening" else (0 <= 390 - m <= x)
    if k in ("preev", "prefed", "postev"):
        if not events():
            return False
        now = _bar_time(extra, c, v.sim)
        if k == "postev":
            _, t = last_event(now)
            return t is not None and now - t < x * 3600
        _, t = next_event(now, ("fomc",) if k == "prefed" else BIG_EVENTS)
        return t is not None and t - now < x * 3600
    if k == "mktdown":
        ref = "SPY" if "SPY" in coins else ("BTC" if "BTC" in coins else None)
        if ref:
            q = closes[ref]
            return len(q) > L and (q[-1] / q[-1 - L] - 1) * 100 <= -x
        moves = [closes[k2][-1] / closes[k2][-1 - L] - 1 for k2 in coins if len(closes[k2]) > L]
        return bool(moves) and sum(moves) / len(moves) * 100 <= -x
    if k == "panic":
        return (p[-1] / p[-2] - 1) * 100 <= -x
    if k == "gap":
        g = day_gap(c, p, ((extra or {}).get(c) or {}).get("t"))
        return g is not None and abs(g) >= x
    if k == "weekday":
        d = datetime.fromtimestamp(_bar_time(extra, c, v.sim), timezone.utc)
        return (d - timedelta(hours=_ny_offset(d))).weekday() == int(x) - 1
    if k == "newsday":
        return bool(events()) and _news_day(_bar_time(extra, c, v.sim))
    if k == "pricey":
        ser = tail_series(p, v.tf, 201)
        if len(ser) < 60:
            return False
        mv = [abs(ser[i] / ser[i - 1] - 1) for i in range(1, len(ser)) if ser[i - 1]]
        base = sum(mv) / len(mv) if mv else 0
        return base > 0 and (sum(mv[-20:]) / len(mv[-20:])) / base >= x
    return False


_GAP_CACHE = {}


def day_gap(c, p, t):
    """% the ticker's day opened away from the previous day's last price (intraday bars with times), or None.
    A ticker's gap on a given day is a fact, so it's worked out once and remembered."""
    if not t or len(t) < 3 or len(p) < 3 or t[-1] - t[-2] >= 6 * 3600:
        return None
    day = ny_date(t[-1])
    key = (c, day)
    if key in _GAP_CACHE:
        return _GAP_CACHE[key]
    i, lim = len(t) - 1, min(len(t), len(p)) - 1
    j = 0
    while j < lim and ny_date(t[i - j]) == day and j < 1000:
        j += 1
    if j >= lim or j >= 1000:
        return None  # the day's start isn't in the data
    first, prev = p[len(p) - j], p[len(p) - j - 1]
    g = (first / prev - 1) * 100 if prev else None
    if len(_GAP_CACHE) > 20000:
        _GAP_CACHE.clear()
    _GAP_CACHE[key] = g
    return g


SCALP_JUDGE = 30   # a scalper is judged on its last this-many finished trades
SCALP_MIN = 8      # ...once it has at least this many


def scalp_record(v):
    """A scalper's (decision, equity) after each of its recent finished trades, when it has enough of them."""
    te = getattr(v, "trade_eq", None)
    return te if te is not None and len(te) > SCALP_MIN else None


def luck_factor(v):
    """0..1: how much to trust a bot's score given how many trades it rests on. 3 trades = mostly luck,
    40 trades = mostly skill. Slow bots trade rarely by design, so they need fewer."""
    te = scalp_record(v)
    n = len(te) - 1 if te else len(v.sim.tbars)
    k = 6 if v.tf < 10 else 2
    return n / (n + k) if n else 0.0


def _grade_vetoes(v, closes, n):
    """Settles blocked trades whose waiting time is up: how much did skipping them save (or cost)?"""
    keep = []
    for k, c, side, px, due in v.veto_wait:
        if n < due or c not in closes:
            keep.append((k, c, side, px, due))
            continue
        move = (closes[c][-1] / px - 1) * 100
        # a skipped buy saved money if the price fell (plus the fees it didn't pay); a skipped sell, if it rose
        saved = (-move + v.rt_cost * 100) if side == "buy" else move
        if k == "coin":  # a skipped coin: if it would have won, its bad record fades
            cp = v.coin_pl.setdefault(c, [0, 0.0])
            cp[1] = cp[1] * 0.9 + max(0.0, -saved)  # bad records also fade slowly, so it re-tries the coin one day
        st = (v.layer_stats if (k in ("plus", "minus", "confirm", "coin", "htf", "lead", "vix") or k.startswith("ctx:"))
              else v.veto_stats).setdefault(k, [0.0, 0.0])
        st[0] += 1
        st[1] += saved
    v.veto_wait = keep


# ---- rule layers: several positive studies, several negative studies, several exit studies, and a sizing rule ----
MAX_PLUS, MAX_MINUS, MAX_EXITS = 4, 3, 3


def _ctx_votes(v, L, extra, n):
    """[(sym, vote)] from the bot's context studies: +1 for buying, -1 against, 0 no opinion (or no data)."""
    out = []
    ctx = (extra or {}).get(CTX_KEY) or {}
    for i, e in enumerate(L.get("ctx", [])):
        q = ctx.get(e["sym"])
        vote = 0
        if q is not None and len(q) > 5:
            if e.get("kind") == "study":
                sig = strat_signal(e["name"], q, n, e["params"], v.tf)
                k = f"ctx{i}"
                if sig:
                    v.conf_last[k] = sig
                st = v.conf_last.get(k)
                vote = (1 if st == "buy" else -1 if st == "sell" else 0) * e["sign"]
            else:
                lb = e["lb"] * v.tf
                if len(q) > lb and q[-1 - lb]:
                    mv = (q[-1] / q[-1 - lb] - 1) * 100
                    vote = (1 if mv >= e["th"] else -1 if mv <= -e["th"] else 0) * e["sign"]
        out.append((e["sym"], vote))
    return out


def _clean_sub(d):
    if not isinstance(d, dict) or d.get("name") not in STRATS or d.get("name") == "Dollar-cost averaging":
        return None
    spec, p = STRATS[d["name"]], {}
    for k, dv in spec["params"].items():
        lo, hi = spec["bounds"][k]
        try:
            val = min(max(float((d.get("params") or {}).get(k, dv)), lo), hi)
        except (TypeError, ValueError):
            val = dv
        p[k] = int(round(val)) if isinstance(dv, int) else round(val, 2)
    return dict(name=d["name"], params=fix_params(d["name"], p))


def clean_layers(d):
    """Validated rule layers:
      plus  = extra positive studies (the main strategy always counts as one), up to 4
      minus = negative studies: each one whose latest signal is a sell counts against buying, up to 3
      exits = exit studies: the position is sold when any of them signals sell (instead of the main strategy), up to 3
      need  = how many positive studies (main included) must currently be on 'buy', after negatives are subtracted
      size  = {target}: trade size scaled to the market's swing"""
    d = d if isinstance(d, dict) else {}
    out = {}
    plus = list(d.get("plus") or []) + ([d["confirm"]] if isinstance(d.get("confirm"), dict) else [])
    exits = list(d.get("exits") or []) + ([d["exit"]] if isinstance(d.get("exit"), dict) else [])
    for key, items, cap in (("plus", plus, MAX_PLUS), ("minus", d.get("minus") or [], MAX_MINUS), ("exits", exits, MAX_EXITS)):
        subs = [x for x in (_clean_sub(i) for i in items if isinstance(i, dict)) if x][:cap]
        if subs:
            out[key] = subs
    if out.get("plus") or out.get("minus"):
        try:
            need = int(d.get("need", 1 + (len(out.get("plus", [])) + 1) // 2 if "need" in d or d.get("plus") else 1))
        except (TypeError, ValueError):
            need = 1
        out["need"] = min(max(need, 1), 1 + len(out.get("plus", [])))
    h = d.get("htf")
    if isinstance(h, dict):
        sub = _clean_sub(h)
        if sub:
            try:
                sub["mult"] = int(min(max(int(h.get("mult", 6)), HTF_MULTS[0]), HTF_MULTS[-1]))
            except (TypeError, ValueError):
                sub["mult"] = 6
            out["htf"] = sub
    cx = []
    for e in (d.get("ctx") or [])[:MAX_CTX]:
        if not isinstance(e, dict) or not valid_coin(e.get("sym", "")):
            continue
        sign = -1 if int(e.get("sign", 1) or 1) < 0 else 1
        if e.get("kind") == "study":
            sub = _clean_sub(e)
            if sub:
                cx.append(dict(sub, sym=e["sym"], kind="study", sign=sign))
        else:
            try:
                cx.append(dict(sym=e["sym"], kind="move", sign=sign, lb=int(min(max(int(e.get("lb", 10)), 2), 60)),
                               th=round(min(max(float(e.get("th", 1.0)), 0.1), 10.0), 2)))
            except (TypeError, ValueError):
                pass
    if cx:
        out["ctx"] = cx
    ld = d.get("lead")
    if isinstance(ld, dict):
        try:
            out["lead"] = dict(lb=int(min(max(int(ld.get("lb", 10)), 3), 40)),
                               th=round(min(max(float(ld.get("th", 0.8)), 0.2), 4.0), 2))
        except (TypeError, ValueError):
            pass
    sz = d.get("size")
    if isinstance(sz, dict):
        try:
            out["size"] = dict(target=round(min(max(float(sz.get("target", 0.5)), 0.1), 3.0), 2))
        except (TypeError, ValueError):
            pass
    return out


def layers_text(L, stats=None):
    stats = stats or {}
    parts = []
    if L.get("plus") or L.get("minus"):
        n_pos = 1 + len(L.get("plus", []))
        t = f"buys when at least {L.get('need', 1)} of {n_pos} positive studies say buy"
        if L.get("plus"):
            t += " (main strategy + " + ", ".join(_sub_txt(s) for s in L["plus"]) + ")"
        if L.get("minus"):
            t += "; each of these saying sell counts against it: " + ", ".join(_sub_txt(s) for s in L["minus"])
        t += _stat_txt(stats.get("plus"), "buys held back by too little agreement")
        t += _stat_txt(stats.get("minus"), "buys blocked by negative studies")
        parts.append(t)
    if L.get("htf"):
        parts.append(f"only buys when the bigger trend agrees: {_sub_txt(L['htf'])} on {L['htf']['mult']}x longer candles"
                     + _stat_txt(stats.get("htf"), "buys blocked by the bigger trend"))
    if L.get("ctx"):
        parts.append("watches the market: " + "; ".join(ctx_text(e) + _stat_txt(stats.get(f"ctx:{e['sym']}"), "trades it held back")
                                                        for e in L["ctx"]))
    if L.get("lead"):
        parts.append(f"The market leads: SPY up {L['lead']['th']:g}%+ over {L['lead']['lb']} decisions votes for buying other "
                     f"tickers, down that much votes against" + _stat_txt(stats.get("lead"), "buys blocked by the market"))
    if L.get("exits"):
        parts.append("sells when any of these says sell: " + ", ".join(_sub_txt(s) for s in L["exits"]))
    if L.get("size"):
        parts.append(f"sizes trades to the market's swing (aims for {L['size']['target']:g}% moves per decision)")
    return "; ".join(parts)


def rule_stack(v_or_entry):
    """The bot's rules as five layers, in plain English (for cards and exports)."""
    g = (lambda k, d=None: getattr(v_or_entry, k, d)) if not isinstance(v_or_entry, dict) else (lambda k, d=None: v_or_entry.get(k, d))
    tr, L, ls = g("traits", {}) or {}, g("layers", {}) or {}, g("layer_stats", {}) or {}
    market = [traits_text({k: tr[k]}) for k in ("moods", "minvol", "trend") if k in tr]
    if L.get("htf"):
        market.append(f"bigger trend must agree ({_sub_txt(L['htf'])} on {L['htf']['mult']}x candles)"
                      + _stat_txt(ls.get("htf"), "blocked buys"))
    if L.get("ctx"):
        market.append("watches " + "; ".join(ctx_text(e) for e in L["ctx"]))
    if L.get("lead"):
        market.append(f"The market leads (SPY {L['lead']['th']:g}% over {L['lead']['lb']} decisions)")
    pos = [f"{g('name')} (main)"] + [_sub_txt(s) for s in L.get("plus", [])]
    entry = [f"positive studies: {', '.join(pos)}" + (f"; needs {L.get('need', 1)} of {len(pos)} saying buy" if len(pos) > 1 or L.get("minus") else "")
             + _stat_txt(ls.get("plus"), "held-back buys")]
    entry += [traits_text({k: tr[k]}) for k in ("confirm", "rsicap", "cool", "entries") if k in tr]
    neg = ([f"negative studies: {', '.join(_sub_txt(s) for s in L['minus'])} (each saying sell counts against buying)"
            + _stat_txt(ls.get("minus"), "blocked buys")] if L.get("minus") else [])
    nev = vetoes_text(g("vetoes", {}), g("veto_stats", {}))
    sizing = [layers_text({"size": L["size"]})] if L.get("size") else ["flat size"]
    exits = [x for x in (risk_text(g("risk", {})),) if x] + [traits_text({k: tr[k]}) for k in ("trail", "maxhold", "minhold", "exit", "adapt") if k in tr] + \
        ([f"exit studies: {', '.join(_sub_txt(s) for s in L['exits'])} (any saying sell)"] if L.get("exits") else [f"{g('name')} sell signals"])
    return [("1. Market filter", ", ".join(market) or "trades in any market"),
            ("2. Positive studies", "; ".join(entry)),
            ("3. Negative studies", "; ".join(neg + ([f"never-rules: {nev}"] if nev else [])) or "none"),
            ("4. Sizing", "; ".join(sizing)),
            ("5. Exits", "; ".join(exits))]


def _study_states(v, L, key, closes, c, n, xc):
    """Updates and returns each study's latest signal ('buy' / 'sell' / None) for this coin. Studies are judged by
    their most recent signal, so a crossover from a few decisions ago still counts as that study's opinion."""
    out = []
    for i, s in enumerate(L.get(key, [])):
        sig = strat_signal(s["name"], closes[c], n, s["params"], v.tf, xc)
        k = f"{key}{i}:{c}"
        if sig:
            v.conf_last[k] = sig
        out.append((sig, v.conf_last.get(k)))
    return out


def bot_step(v, closes, n, coins, extra=None):
    """One decision of one bot on every coin: its own exits first (stop-loss, take-profit, trailing stop,
    max hold), then its strategy signal filtered by its traits. Returns the trades made [(coin, trade)]."""
    last = {c: closes[c][-1] for c in coins}
    tr, made = v.traits, []
    opt = isinstance(v.sim, OptionSim)
    now_ = data_now(extra, coins) if (opt or tr.get("flatby")) else None  # the bar's time: scalpers watch the clock
    if opt:
        v.sim.now = now_
        v.sim.genes = tr
        for c in coins:  # each coin's recent swing (computed once per bar and shared by every bot)
            v.sim.vol_src[c] = pricing_vol(closes[c], ((extra or {}).get(c) or {}).get("t"))
    if v.veto_wait:
        _grade_vetoes(v, closes, n)
    for c in coins:
        sim = v.sim
        sim.last = None
        q = sim.pos.get(c, 0)
        if q > 0:
            mk = sim.value_of(c, last[c]) / q if opt else last[c]  # options: judged on the contract's own value
            v.hi[c] = max(v.hi.get(c, mk), mk)
            r = (mk / (sim.cost[c] / q) - 1) * 100
            why = None
            o = sim.opt.get(c) if opt else None
            if o and sim.now >= o["exp"] - int(tr.get("oexit", 1) or 1) * 86400:
                why = "expiry"
            elif tr.get("flatby") and minutes_to_close(now_) <= tr["flatby"]:
                why = "flat"  # scalpers never hold into the close
            k_ = 1.0
            if tr.get("adapt") and n % v.tf == 0:  # adaptive stops: scaled by today's swing vs this bot's usual swing
                ser = tail_series(closes[c], v.tf, 21)
                if len(ser) > 5:
                    sw = sum(abs(ser[i] / ser[i - 1] - 1) for i in range(1, len(ser))) / (len(ser) - 1)
                    ref = v.swing_ref.get(c) or sw
                    v.swing_ref[c] = ref * 0.97 + sw * 0.03
                    v.adapt_k[c] = min(2.0, max(0.5, sw / ref)) if ref > 0 else 1.0
            if tr.get("adapt"):
                k_ = v.adapt_k.get(c, 1.0)
            if tr.get("vixwide"):  # a volatility jump: room to breathe (a spike gets the full width)
                reg_ = vol_regime(closes, extra, c)[0]
                k_ *= tr["vixwide"] if reg_ == "spiking" else (1 + tr["vixwide"]) / 2 if reg_ == "rising" else 1.0
            if why is not None:
                pass
            elif v.risk["sl"] and r <= -v.risk["sl"] * k_:
                why = "stop"
            elif v.risk["tp"] and r >= v.risk["tp"] * k_:
                why = "target"
            elif why is None and tr.get("trail") and mk <= v.hi[c] * (1 - tr["trail"] * k_ / 100):
                why = "trail"
            elif tr.get("maxhold") and n - v.since.get(c, n) >= tr["maxhold"] * v.tf:
                why = "time"
            if why:
                sim.sell(c, last[c], n)
                v.sig[c] = "sell"
                v.why[c] = {"expiry": "its option was close to expiry",
                            "stop": f"its own safety stop ({v.risk['sl']:g}%)", "target": f"its own profit target ({v.risk['tp']:g}%)",
                            "trail": f"its trailing stop ({tr.get('trail', 0):g}%)", "time": "it held as long as it allows",
                            "flat": f"scalpers are flat {tr.get('flatby', 0)} min before the close"}[why]
        if sim.last is None:
            if n % v.tf:  # between this bot's own decisions: no signal, no trade
                v.sig[c] = None
                continue
            s = strat_signal(v.name, closes[c], n, v.params, v.tf, (extra or {}).get(c))
            reason = f"{v.name} said {s}" if s else ""
            conf = 1.0
            L = v.layers
            if L:
                xc = (extra or {}).get(c)
                if L.get("exits"):  # exit studies: any of them saying sell closes the position (not the main strategy)
                    if s == "sell":
                        s = None
                    if any(fresh == "sell" for fresh, _ in _study_states(v, L, "exits", closes, c, n, xc)) and sim.pos.get(c, 0) > 0:
                        s = "sell"
                        reason = "an exit study said sell"
                lead_v = 0
                ldr = lead_ticker(closes)
                if L.get("lead") and ldr and c != ldr:
                    q_ = closes[ldr]
                    lb = L["lead"]["lb"] * v.tf
                    if len(q_) > lb:
                        mv = (q_[-1] / q_[-1 - lb] - 1) * 100
                        lead_v = 1 if mv >= L["lead"]["th"] else -1 if mv <= -L["lead"]["th"] else 0
                ctx_v = _ctx_votes(v, L, extra, n) if L.get("ctx") else []
                cpos = sum(1 for _, x in ctx_v if x > 0)
                cneg = sum(1 for _, x in ctx_v if x < 0)
                if opt and s == "sell" and sim.pos.get(c, 0) <= 0 and cpos > cneg:  # the market says up: no new put
                    sym_ = next(sy for sy, x in ctx_v if x > 0)
                    if not any(w[0] == f"ctx:{sym_}" and w[1] == c for w in v.veto_wait):
                        v.veto_wait.append((f"ctx:{sym_}", c, "sell", last[c], n + 10 * v.tf))
                    s = None
                if L.get("plus") or L.get("minus") or L.get("lead") or L.get("ctx"):
                    if s:
                        v.conf_last[f"main:{c}"] = s
                    plus = _study_states(v, L, "plus", closes, c, n, xc)
                    minus = _study_states(v, L, "minus", closes, c, n, xc)
                    trigger = s == "buy" or any(fresh == "buy" for fresh, _ in plus)
                    if trigger and sim.pos.get(c, 0) <= 0 or s == "buy":
                        votes = (v.conf_last.get(f"main:{c}") == "buy") + sum(1 for _, st_ in plus if st_ == "buy") + (lead_v > 0) + cpos
                        against = sum(1 for _, st_ in minus if st_ == "sell") + (lead_v < 0) + cneg
                        if votes - against >= L.get("need", 1):
                            if s != "sell":
                                s = "buy"
                                reason = (f"{votes} of its {1 + len(L.get('plus', []))} buy studies agreed"
                                          + (f", {against} said no" if against else "") + f" (needs {L.get('need', 1)})")
                                if v.conf_size:  # confidence: a clear vote buys bigger, a close one smaller
                                    margin = votes - against - L.get("need", 1)
                                    conf = min(1.6, 0.8 + 0.25 * margin) if (L.get("plus") or L.get("minus")) else 1.0
                        else:
                            why = ("lead" if lead_v < 0 and votes - against + 1 >= L.get("need", 1) else
                                   f"ctx:{next(sy for sy, x in ctx_v if x < 0)}" if cneg and votes - against + cneg >= L.get("need", 1) else
                                   "minus" if votes >= L.get("need", 1) else "plus")
                            if not any(w[0] == why and w[1] == c for w in v.veto_wait):
                                v.veto_wait.append((why, c, "buy", last[c], n + 10 * v.tf))
                            if s == "buy":
                                s = None
            if s == "buy" and L.get("htf") and sim.pos.get(c, 0) <= 0:  # the bigger trend must not be saying sell
                h_ = L["htf"]
                hs = strat_signal(h_["name"], closes[c], n, h_["params"], v.tf * h_["mult"], (extra or {}).get(c))
                if hs:
                    v.conf_last[f"htf:{c}"] = hs
                if v.conf_last.get(f"htf:{c}") == "sell":
                    if not any(w[0] == "htf" and w[1] == c for w in v.veto_wait):
                        v.veto_wait.append(("htf", c, "buy", last[c], n + 10 * v.tf))
                    s = None
            if tr.get("confirm"):
                v.run_sig[c] = (s, v.run_sig.get(c, (None, 0))[1] + 1) if s and s == v.run_sig.get(c, (None, 0))[0] else (s, 1 if s else 0)
                if s and v.run_sig[c][1] < tr["confirm"]:
                    s = None
            if s == "buy":
                ser = None
                if tr.get("cool") and n - v.sold_at.get(c, -10 ** 9) < tr["cool"] * v.tf:
                    s = None
                elif tr.get("entries") and v.entries.get(c, 0) >= tr["entries"]:
                    s = None
                else:
                    if tr.get("trend") or tr.get("minvol") or tr.get("rsicap"):
                        ser = tail_series(closes[c], v.tf)
                    if tr.get("trend") and (len(ser) < tr["trend"] or ser[-1] <= _avg(ser, tr["trend"])):
                        s = None
                    elif tr.get("minvol") and (len(ser) < 21 or sum(abs(ser[i] / ser[i - 1] - 1) for i in range(-20, 0)) / 20 * 100 < tr["minvol"]):
                        s = None
                    elif tr.get("rsicap") and len(ser) > 15 and _rsi(ser, 14) > tr["rsicap"]:
                        s = None
                    elif tr.get("moods") and not (tr["moods"] & (1 << MOODS.index(m)) if (m := market_mood(closes[c])[0]) in MOODS else False):
                        s = None
            elif s == "sell" and tr.get("exit") and (v.risk["sl"] or v.risk["tp"] or tr.get("trail") or tr.get("maxhold")):
                s = None  # this bot only leaves through its own exits
            elif s == "sell" and tr.get("minhold") and n - v.since.get(c, n) < tr["minhold"] * v.tf:
                s = None  # this bot holds for a while before its strategy may sell (its stops still work)
            elif s == "sell" and v.fee_guard and sim.pos.get(c, 0) > 0 and not opt:
                gain = last[c] / (sim.cost[c] / sim.pos[c]) - 1
                if 0 <= gain < v.fee_guard:
                    s = None  # fee-aware: a profit this small would be eaten by the fees, so keep holding
            if opt and s == "sell" and sim.pos.get(c, 0) <= 0 and int(tr.get("omode", 3) or 3) == 1:
                s = None  # calls-only bot with nothing to close
            if s and tr.get("flatby") and sim.pos.get(c, 0) <= 0 \
                    and not (tr["flatby"] < minutes_to_close(now_) <= 390):
                s = None  # scalper: only opens during the trading day, and not in its last minutes
            if s and int(tr.get("vixmode", 5) or 5) != 5 and sim.pos.get(c, 0) <= 0 \
                    and not vix_allows(tr["vixmode"], vol_regime(closes, extra, c)[0]):
                if not any(w[0] == "vix" and w[1] == c for w in v.veto_wait):  # graded in hindsight like a never-rule
                    v.veto_wait.append(("vix", c, s, last[c], n + 10 * v.tf))
                s = None
            if s and v.vetoes and (s == "buy" or sim.pos.get(c, 0) > 0 or opt):
                opening = sim.pos.get(c, 0) <= 0
                for k, x in v.vetoes.items():
                    side = VETOES[k]["side"]
                    if ((side == s or (side == "open" and opening)) and not (side == "sell" and opening)
                            and _veto(v, k, x, c, closes, n, extra, coins)):
                        if not any(w[0] == k and w[1] == c for w in v.veto_wait):  # one open what-if per rule and coin
                            v.veto_wait.append((k, c, s, last[c], n + 10 * v.tf))
                        s = None
                        break
            if s == "buy" and v.picky and sim.pos.get(c, 0) <= 0:
                cp = v.coin_pl.get(c)
                if cp and cp[0] >= 3 and cp[1] < -1.5:  # it keeps losing on this coin: skip it (graded in hindsight)
                    if not any(w[0] == "coin" and w[1] == c for w in v.veto_wait):
                        v.veto_wait.append(("coin", c, "buy", last[c], n + 10 * v.tf))
                    s = None
            v.sig[c] = s
            if s:
                v.why[c] = reason or f"{v.name} said {s}"
            if s == "buy" and (v.layers.get("size") or conf != 1.0):
                mult = conf
                if v.layers.get("size"):  # sizing layer: smaller in wild markets, bigger in calm ones
                    ser = tail_series(closes[c], v.tf, 22)
                    swing = sum(abs(ser[i] / ser[i - 1] - 1) for i in range(1, len(ser))) / max(1, len(ser) - 1) * 100
                    mult *= min(1.5, max(0.5, v.layers["size"]["target"] / swing)) if swing > 0 else 1.0
                sim.buy(c, last[c], n, mult)
            else:
                sim.act(c, s, last[c], n)
        t = sim.last
        if t:
            # what explains this trade, for reports: the stock's price, the volatility the contract was priced with
            # (implied = the market's own, measured = from past candles) and, for a sale, how long it was held
            t["under_px"] = last[c]
            if hasattr(sim, "_vol"):
                t["vol"] = round(sim._vol(c), 4)
                t["vol_src"] = ("real prices" if (t.get("contract") or {}).get("symbol") and getattr(sim, "book", None)
                                else "implied" if IV_HINT.get(c) else "measured")
            if t["side"] != "buy" and c in v.since:
                t["held"] = n - v.since[c]
            if t["side"] == "buy":
                if v.review and c not in v.entry_flags:  # which never-rules WOULD have blocked this buy (for the review)
                    v.entry_flags[c] = [k for k, spec in VETOES.items() if spec["side"] in ("buy", "open") and k not in v.vetoes
                                        and _veto(v, k, (spec["lo"] + spec["hi"]) / 2, c, closes, n, extra, coins)]
                v.entries[c] = v.entries.get(c, 0) + 1
                v.since.setdefault(c, n)
                v.hi[c] = max(v.hi.get(c, last[c]), last[c])
            else:
                v.entries.pop(c, None)
                v.since.pop(c, None)
                v.hi.pop(c, None)
                v.sold_at[c] = n
                if t.get("pl") is not None and t["pl"] < 0:
                    v.lost_at[c] = n
                if t.get("pl") is not None:  # per-coin record (recent results count most)
                    paid = t["usd"] - t["pl"]
                    cp = v.coin_pl.setdefault(c, [0, 0.0])
                    cp[0] = min(cp[0] + 1, 20)
                    cp[1] = cp[1] * 0.85 + (t["pl"] / paid * 100 if paid > 0 else 0.0)
            if t["side"] == "sell":
                t["flags"] = v.entry_flags.pop(c, [])
            t["why"] = v.why.get(c, "")
            made.append((c, t))
    return made


# ---------------- exams: results on days a bot never trained on ----------------


def trading_days(t0, t1):
    """Weekdays (New York dates) from t0 to t1, oldest first. Holidays are included: they're found out and skipped."""
    d, end, out = ny_date(t0), ny_date(t1), []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def is_exam_day(d):
    """One trading day in five is held back for exams: the same days on every computer, never trained on."""
    import hashlib
    return int(hashlib.md5(("sbf-exam-" + d.isoformat()).encode()).hexdigest(), 16) % 5 == 0


def exam_text(v, tried=100):
    verdict, d = exam_verdict(v, tried)
    if not d["k"]:
        return "no exams yet (Free roam tests it on days it never trained on)"
    base = f"{d['wins']} of {d['k']} won, average {d['mean']:+.1f}%"
    if verdict == "passed":
        return f"passed: {base}, steady enough to beat luck (score {d['score']:.1f}, needed {d['bar']:.1f})"
    if verdict == "failed":
        return f"failed: {base}. It can't be a BEAST until it does better"
    return f"not proven yet: {base}" + (f" (score {d['score']:.1f}, needs {d['bar']:.1f})" if d["k"] >= EXAM_MIN else
                                        f" ({EXAM_MIN - d['k']} more exam{'s' if EXAM_MIN - d['k'] > 1 else ''} needed)")


COND_TEXT = {"vol:calm": "calm markets", "vol:normal": "normal markets", "vol:wild": "wild markets",
             "tod:open": "the first hour", "tod:mid": "the middle of the day", "tod:late": "the last hour",
             "day:news": "news days (CPI, jobs, Fed)", "day:plain": "days without big news"}
COND_MIN = 30    # decisions in a condition before its record counts


def market_conds(closes, extra, coins):
    """The conditions right now, shared by every bot: how wild the market is, the time of day (bars shorter than a
    day only), and whether it's a big-news day."""
    out = []
    if not coins:
        return out
    try:
        reg = vol_regime(closes, extra, coins[0])[0]
        if reg:
            out.append("vol:" + ("calm" if reg == "calm" else "wild" if reg in ("rising", "spiking") else "normal"))
    except Exception:
        note_error("market_conds")
    t = ((extra or {}).get(coins[0]) or {}).get("t")
    if t and len(t) >= 2 and t[-1] - t[-2] < 6 * 3600:
        m = 390 - minutes_to_close(t[-1])  # minutes since the open
        if 0 <= m <= 390:
            out.append("tod:" + ("open" if m < 60 else "late" if m >= 330 else "mid"))
    if t and events():
        out.append("day:news" if _news_day(t[-1]) else "day:plain")
    return out


def cond_fit(v, cond):
    """Average gain per decision while `cond` held, or None with too little evidence."""
    k = (getattr(v, "cond_n", None) or {}).get(cond, 0)
    return v.cond_gain.get(cond, 0.0) / k if k >= COND_MIN else None


def good_at(v):
    """[(condition text, average % per decision)] where the bot does best, best first (only with enough evidence)."""
    fits = [(c, cond_fit(v, c)) for c in (getattr(v, "cond_n", None) or {})]
    fits = [(c, f) for c, f in fits if f is not None]
    return [(COND_TEXT.get(c, c), f * 100) for c, f in sorted(fits, key=lambda x: -x[1])]


_NEWS_CACHE = {}


def _news_day(ts):
    """True if a CPI, jobs-report or Fed decision falls on this New York day."""
    d = datetime.fromtimestamp(ts, timezone.utc)
    ny = (d - timedelta(hours=_ny_offset(d))).date()
    hit = _NEWS_CACHE.get(ny)
    if hit is None:
        day0 = ny_time(ny.isoformat(), 0, 0)
        _, t = next_event(day0, BIG_EVENTS)
        hit = t is not None and t < day0 + 86400
        if len(_NEWS_CACHE) > 5000:
            _NEWS_CACHE.clear()
        _NEWS_CACHE[ny] = hit
    return hit


class Variant:
    def __init__(self, name, params, sim, maxlen, tf=1, risk=None, traits=None, vetoes=None, layers=None):
        self.name, self.params, self.sim = name, params, sim
        self.risk = clean_risk(risk)
        self.traits = clean_traits(traits)
        self.fee_guard = 0.0  # round-trip cost (fraction) when fee-aware: profits smaller than this aren't taken
        self.rt_cost = 0.0    # round-trip cost (fraction) of a trade, used to grade its never-rules
        self.vetoes = clean_vetoes(vetoes)
        self.veto_stats = {}  # rule -> [times it blocked a trade, total % saved (negative = cost)]
        self.veto_wait = []   # blocked trades waiting to be graded: (rule, coin, side, price, decision due)
        self.lost_at = {}
        self.layers = clean_layers(layers)
        self.layer_stats = {}  # "confirm" -> [buys it blocked, total % saved]
        self.conf_last = {}
        self.coin_pl = {}     # coin -> [closed trades (capped), recent % result]: the bot learns its bad coins
        self.picky = True     # skip coins it keeps losing on
        self.conf_size = True # size buys by how strongly its studies agree
        self.why = {}         # coin -> plain-English reason for its latest signal
        self.twin = None      # twin test: its edge over holding on an older stretch it was never scored on
        self.island = 0       # which sub-farm it lives on
        self.review = True    # note which never-rules would have stopped its losing buys (post-trade review)
        self.entry_flags = {} # coin -> never-rules that would have blocked the open position's first buy
        self.own_review = {}  # its own losses: never-rule -> [its losing buys it would have blocked, winning buys]
        self.swing_ref, self.adapt_k = {}, {}  # adaptive stops: usual swing per coin, current stop scale
        self.mut_kinds = []   # what changed from its parent (mutation memory)
        self.credited = False # counted as a winner in the mutation memory
        self.backtests = []   # summaries of backtests saved to its memory
        self.power = 0.0      # 0..10: grows only with real money made on fresh prices while beating holding
        self.live_high = None
        self.hi, self.since, self.entries, self.sold_at, self.run_sig = {}, {}, {}, {}, {}
        self.uid, self.parents, self.gen = 0, [], 0
        self.tf = clamp_tf(tf)  # decides once every tf bars, on tf-bar candles
        self.eq = deque(maxlen=maxlen)
        self.sig = {}
        self.strikes = 0
        self.size_mult = 1.0  # you can raise or lower this per strategy
        # lifetime track record (since this bot was created)
        self.born_px = None
        self.age = self.ntrades = self.wins = self.losses = 0
        self.win_steps = self.streak = 0
        self.peak, self.maxdd = 0.0, 0.0
        self.mood_gain = {}   # mood -> summed equity change (fraction) while that mood ruled
        self.mood_n = {}
        self.inducted = False
        self.origin = ""
        self.royal = False       # descends from a bot you saved (Library / Hall of Fame / team): hatches from a royal egg
        self.classic = ""        # name of the famous strategy it is (silver egg), or "" 
        self.live_n = 0          # decisions made on prices that came in after the bot was created
        self.live_eq0 = None     # its equity when it started trading live (after any replay)
        self.log = deque(maxlen=300)  # (decision number, coin, side, price) of this bot's own trades, for charts
        self.best_tr = self.worst_tr = None  # its best and worst finished trade, % of what it paid
        self.log_ct = {}  # (decision, ticker) -> "call" / "put" of the contract traded there
        self.roam_scan = None  # scalpers: [trades, wins, pl, paid] from Free roam's scalp roams on past days (fading)
        self.trade_eq = deque(maxlen=SCALP_JUDGE + 1)  # scalpers: (decision, equity) after each finished trade
        self.exams = []        # results on days / stretches it never trained on: {k: kind, edge: %, n: trades, at: time}
        self.cond_gain = {}    # condition (e.g. "vol:wild", "tod:open") -> summed equity change while it held
        self.cond_n = {}
        self.hs_src = None     # hatched from the hindsight book: the instinct it came from (graded forward once)

    @property
    def label(self):
        rt = ", ".join(x for x in (risk_text(self.risk), traits_text(self.traits), vetoes_text(self.vetoes),
                                   layers_text(self.layers)) if x)
        return (f"{self.name} ({', '.join(f'{k}={v}' for k, v in self.params.items())})"
                + (f" on {self.tf}-bar candles" if self.tf > 1 else "") + (f" [{rt}]" if rt else ""))

    def key(self):
        """Identity of its genes (cached: genes never change after birth; restore() clears the cache)."""
        k = self.__dict__.get("_key")
        if k is None:
            k = self._key = (self.name, self.tf, tuple(sorted((k_, round(float(v), 4)) for k_, v in self.params.items())),
                             tuple(sorted(self.risk.items())), tuple(sorted(self.traits.items())),
                             tuple(sorted(self.vetoes.items())), json.dumps(self.layers, sort_keys=True))
        return k


def bot_key(d):
    return (d["name"], clamp_tf(d.get("tf", 1)),
            tuple(sorted((k, round(float(v), 4)) for k, v in d["params"].items())),
            tuple(sorted(clean_risk(d.get("risk")).items())),
            tuple(sorted(clean_traits(d.get("traits")).items())),
            tuple(sorted(clean_vetoes(d.get("vetoes")).items())),
            json.dumps(clean_layers(d.get("layers")), sort_keys=True))


class Tournament:
    """Runs many strategy variants as shadow portfolios on the same prices.

    Each variant is rated on recent return, how that compares to just holding the same coins,
    consistency (share of recent sub-periods that were profitable) and how far it has dropped
    from its recent peak.

      active   profitable, consistent and (with beat_hold) ahead of holding. The account follows it
               with bigger trades (up to winner_boost x for the strongest) and keeps adding while it holds.
      slowed   profitable but inconsistent, or not beating holding. Followed only with small trades
               and a cooldown between them.
      standby  qualifies as a winner but there is no room for it among the top few.
      benched  losing, not consistent at all, or dropped sharply. Zero trades. Variants that
               stay benched are retired and replaced by tweaked copies of the winners."""

    def __init__(self, coins, start_cash, fee, pct, evolve_every=24, window=100,
                 min_trades=2, max_active=5, cap=14, params=None, rng=None, retire_after=3, opts=None,
                 retire_steps=0, risk_caps=None):
        self.coins, self.start_cash, self.fee, self.pct = list(coins), start_cash, fee, pct
        self.risk_caps = dict(risk_caps or (opts or {}).get("risk_caps") or FAST_RISK)
        self.ctx_syms = [c for c in ((opts or {}).get("ctx") or []) if c not in coins]
        window = int((opts or {}).get("window", window))  # the slow farm judges over a different window
        self.evolve_every, self.W, self.min_trades = max(1, evolve_every), window, min_trades
        self.max_active, self.cap, self.retire_after = max_active, cap, retire_after
        self.family_cap = max(5, -(-self.cap // 4))
        self.retire_steps = max(0, int(retire_steps))  # decisions a bot may go without winning before it is replaced
        self.o = dict(trade_more=True, winner_boost=2.5, add_every=3, slow_every=6, fail_drop=3.0,
                      beat_hold=True, crossbreed=True, regime_aware=True, slip=0.0, fee_aware=True,
                      prove_live=True, risk_adjusted=True, long_holds=True, classics=True, classic_rate=0.25, vetoes=True,
                      layers=True)
        self.replaying = False
        self.conds = []  # today's conditions (wild/calm, time of day, news day), set on every decision
        self.o.update(opts or {})
        if self.o.get("prem"):  # worker processes get the contract price range this way
            set_prem_range(*self.o["prem"])
        if self.o.get("cfee") is not None:
            CONTRACT_FEE[0] = float(self.o["cfee"])
        if self.o.get("sspread"):  # worker processes get the scalpers' spread limit this way
            SCALP_SPREAD[0] = float(self.o["sspread"])
        self.slowed, self.cool, self.add_cool, self.kind = [], {}, {}, {}
        self.agree = {}  # coin -> (different strategies agreeing, size factor, how split the farm was)
        self.rng = rng or random.Random()
        self.n = 0
        self.events = []
        self.on_trade = None  # optional hook: fn(bot, ticker, trade, bar time), e.g. Free roam's live trade feed
        self.recording = False  # the app switches this on once live; history replays are never recorded
        self.shadow = []        # every bot's own pretend trades since the last pop_shadow()
        self.hold_ret = None    # what just holding the coins returned over the scoring window
        self.mood, self.moods = "unknown", {}
        self.last_px = {}
        self.xfull = None       # highs/lows/volumes lined up with the closes of the current step
        self.serial = 0
        self.lineage = {}       # uid -> family-tree record (kept after a bot is removed)
        self.elders = []        # genes of good bots that left the farm: they nudge future eggs (newest last)
        self.champions = {}     # mood -> genes and record of the best bot seen in that mood (seasons)
        self.season = None      # the market mood of the current season
        self.seasons_log = []   # (decision, mood) whenever the season changed
        self.review = {}        # never-rule -> [losing buys it would have blocked, winning buys it would have blocked]
        self.mut_stats = {}     # kind of change -> [children born with it, children that went on to win]
        self.instincts = {}     # instinct -> [record: slow average of its bots' edge over holding, reviews, best edge]
        self.instinct_best = {}  # instinct -> genes of the best bot it has had (kept after its family is gone)
        self.births, self.deaths = deque(maxlen=3000), deque(maxlen=3000)  # decision numbers (farm pulse)
        self.evolves = 0
        self.spawn_i = 0
        self.vars = []
        self.info, self.active = {}, []
        if self.o.get("classics", True):  # a few famous strategies join the starters
            for cl in self.rng.sample(CLASSICS, min(4, len(CLASSICS))):
                v = self._new(cl["name"], dict(cl["params"]), cl.get("tf", 1), cl.get("risk"), cl.get("traits"))
                v.classic = cl["title"]
                self.vars.append(v)
        for name in WILDCARD_STRATS:
            p = dict((params or {}).get(name) or STRATS[name]["params"])
            self.vars.append(self._new(name, p))
        for name in WILDCARD_STRATS:  # a slower-paced copy of each, so different speeds compete from the start
            p = dict((params or {}).get(name) or STRATS[name]["params"])
            self.vars.append(self._new(name, p, tf=10 if self.o.get("long_holds", True) else 3))
        while len(self.vars) < min(self.cap, 24):  # a bigger, more varied field from the start
            name = self.rng.choice(WILDCARD_STRATS)
            self.vars.append(self._new(name, self._rand_params(name), tf=self.rng.choice(TIMEFRAMES),
                                       risk=self.rand_risk(), traits=self.rand_traits(), vetoes=self.rand_vetoes(),
                                       layers=self.rand_layers()))
        n_sc = self.scalp_target()
        del self.vars[max(self.cap, 14) - n_sc:]
        for i in range(n_sc):  # the scalp breed: one of each instinct at its usual settings, then random ones
            if i < len(SCALP_STRATS):
                self.vars.append(self._new(SCALP_STRATS[i], dict(STRATS[SCALP_STRATS[i]]["params"])))
            else:
                self.vars.append(self._rand_scalper())
        k_isl = int(self.o.get("islands", 1) or 1)
        for i, v in enumerate(self.vars):
            self._register(v, [], "classic" if v.classic else "starter")
            v.island = i % k_isl

    def window_for(self, tf):
        """How many bars a bot is judged over: slow bots get a longer window so they make enough decisions in it."""
        return self.W * min(max(1, clamp_tf(tf) // 2), 10)

    def _new(self, name, params, tf=1, risk=None, traits=None, vetoes=None, layers=None):
        if is_scalp(name):  # the scalp breed always stays inside its envelope, however it was bred
            tf, risk, traits = scalp_shape(self.rng, tf, risk, traits)
        elif traits and traits.get("kami"):  # the kamikaze breed stays inside its envelope too
            risk, traits = kami_shape(self.rng, risk, traits)
        elif traits and any(k in traits for k in SCALP_ONLY):
            traits = {k: x for k, x in traits.items() if k not in SCALP_ONLY}
        v = Variant(name, params, make_sim(self.start_cash, self.fee, self.pct, slip=self.o.get("slip", 0.0)),
                    self.window_for(tf) + 1, tf, risk, traits, vetoes if self.o.get("vetoes", True) else None,
                    layers if self.o.get("layers", True) else None)
        v.sim.genes = v.traits
        v.picky = bool(self.o.get("coin_prefs", True))
        v.review = bool(self.o.get("post_review", True))
        if not self.o.get("adapt_exits", True):
            v.traits.pop("adapt", None)
        if not self.o.get("time_habits", True):
            for k in TIME_VETOES:
                (v.vetoes or {}).pop(k, None)
        if not self.o.get("htf_studies", True):
            v.layers.pop("htf", None)
        if not self.o.get("lead_study", True):
            v.layers.pop("lead", None)
        v.conf_size = bool(self.o.get("confidence_sizing", True))
        v.rt_cost = 2 * (self.fee + self.o.get("slip", 0.0))
        v.fee_guard = 2 * (self.fee + self.o.get("slip", 0.0)) if self.o.get("fee_aware", True) else 0.0
        return v

    # ---- the kamikaze breed: daring bets, and the farm's rule testers ----
    def kami_target(self):
        sh = float(self.o.get("kami_share", 0) or 0)
        return 0 if sh <= 0 else max(1, int(round(self.cap * sh)))

    def kamikazes(self):
        return [v for v in self.vars if is_kami(v)]

    def trial_rules(self):
        """One or two rules a kamikaze tries for the farm: never-rules and studies the farm hasn't much evidence on yet
        (the least-tested first), so the good bots don't have to pay for finding out what fails."""
        book = getattr(self, "trial_book", {})
        pool = [k for k in VETOES if self.rule_allowed(k)]
        pool.sort(key=lambda k: (book.get(f"rule:{k}", [0, 0])[0], self.rng.random()))
        vt = {k: self.rng.uniform(VETOES[k]["lo"], VETOES[k]["hi"]) for k in pool[:self.rng.randint(1, 2)]}
        ly = self.rand_layers(0.5) if self.o.get("layers", True) else {}
        return clean_vetoes(vt), ly

    def spawn_kami(self, closes=None):
        """Hatches one kamikaze: any instinct, big bets with wide stops, and one or two rules on trial."""
        if len(self.vars) >= self.cap:
            cands = [v for v, _ in self.weakest() if v not in self.active and not is_kami(v)
                     and not is_scalp(v.name) and sum(1 for x in self.vars if x.name == v.name) > 1]
            if not cands:
                return None
            self._remove(cands[0], "made room for a kamikaze")
        name = self.rng.choice(WILDCARD_STRATS)
        vt, ly = self.trial_rules()
        v = self._new(name, self._rand_params(name), self.rng.choice((1, 2, 3, 5)), None, dict(kami=1), vt, ly)
        self._register(v, [], "kamikaze")
        v.trials = sorted(vt)
        if closes:
            self.replay(v, closes)
        self.vars.append(v)
        self.events.append(f"Kamikaze egg: {v.label}")
        return v

    def kami_learn(self, v):
        """A kamikaze leaving the farm hands in what its trial rules did: the farm's trial book grows, and never-rules that
        kept saving money become more likely in ordinary eggs (costly ones less likely)."""
        book = self.__dict__.setdefault("trial_book", {})
        for k, st in (v.veto_stats or {}).items():
            b = book.setdefault(f"rule:{k}", [0, 0.0])
            b[0] += st[0]
            b[1] += st[1]
        for k, st in (v.layer_stats or {}).items():
            b = book.setdefault(f"study:{k}", [0, 0.0])
            b[0] += st[0]
            b[1] += st[1]
        for k, st in (v.veto_stats or {}).items():  # proven by a kamikaze: the post-trade review hears about it
            if st[0] >= 3:
                rv = self.review.setdefault(k, [0, 0])
                rv[0 if st[1] > 0 else 1] += int(st[0] // 3)

    def kami_balance(self, closes):
        tgt, ks = self.kami_target(), self.kamikazes()
        if not tgt:
            for v in ks:
                self._remove(v, "kamikazes switched off")
            return
        for _ in range(min(1, tgt - len(ks))):
            self.spawn_kami(closes)
        ks = self.kamikazes()
        if len(ks) > tgt * 2:
            for v in [v for v, _ in self.weakest() if is_kami(v) and v not in self.active][:len(ks) - tgt * 2]:
                self._remove(v, "too many kamikazes")

    # ---- hindsight: what would have worked, recorded so new eggs start from it ----
    def learn_hindsight(self, res):
        """Records a hindsight study: per instinct, how often its settings would have made money (on the stretch AND on
        the part after it they didn't see), and the best settings found. Older evidence slowly counts for less."""
        book = self.__dict__.setdefault("hindsight", {})
        for e in book.values():
            e["tries"] *= 0.9
            e["wins"] *= 0.9
        for r in res.get("results", []):
            g = r["genes"]
            e = book.setdefault(g["name"], dict(tries=0.0, wins=0.0, best=[]))
            e["tries"] += 1
            if r["ok"]:
                e["wins"] += 1
                e["best"].append(dict(genes=g, score=r["check_ret"], at=res.get("at", 0)))
                e["best"] = sorted(e["best"], key=lambda x: -x["score"])[:5]

    def hindsight_genes(self):
        """Genes for a new egg from the hindsight book (weighted by how often each instinct would have worked), or None."""
        book = getattr(self, "hindsight", {}) or {}
        cands = [(n, e) for n, e in book.items() if e["best"] and n in STRATS and n in WILDCARD_STRATS]
        if not cands or not self.o.get("hindsight", True):
            return None
        n, e = self.rng.choices(cands, weights=[(x["wins"] + 1) / (x["tries"] + 2) * self.hindsight_fwd(x) * 2
                                                 for _, x in cands])[0]
        g = json.loads(json.dumps(self.rng.choice(e["best"][:3])["genes"]))
        g["params"] = self.mutate(n, g["params"], 0.15)
        return g

    @staticmethod
    def hindsight_fwd(e):
        """0..1: how often eggs hatched from this hindsight lesson went on to do well on days that came after
        (their exams, or how they did by the time they left). 0.5 with no evidence yet."""
        f = e.get("fwd") or [0, 0]
        return (f[1] + 1) / (f[0] + 2)

    def grade_hindsight(self, v, ok):
        """A hindsight egg has shown how it does going forward: its lesson is graded (once per egg)."""
        src = getattr(v, "hs_src", None)
        if not src:
            return
        v.hs_src = None
        e = (getattr(self, "hindsight", {}) or {}).get(src)
        if e is None:
            return
        f = e.setdefault("fwd", [0, 0])
        f[0] += 1
        f[1] += 1 if ok else 0
        if not ok and e.get("best") and f[0] >= 3 and f[1] * 3 < f[0]:
            e["best"] = e["best"][1:]  # its lessons keep failing later on: drop the top one, try the next

    def hindsight_share(self):
        """Share of new eggs hatched from the hindsight book: 60% while its eggs hold up, down to 20% if they don't."""
        book = getattr(self, "hindsight", {}) or {}
        n = sum((e.get("fwd") or [0, 0])[0] for e in book.values())
        w = sum((e.get("fwd") or [0, 0])[1] for e in book.values())
        if n < 5:
            return 0.6
        return min(0.6, max(0.2, 0.6 * (w / n) / 0.5))

    # ---- the scalp breed ----
    def scalp_target(self):
        """How many scalpers this farm keeps at least (0 = the breed is switched off here)."""
        sh = float(self.o.get("scalp_share", 0) or 0)
        return 0 if sh <= 0 else max(2, int(round(self.cap * sh)))

    def scalpers(self):
        return [v for v in self.vars if is_scalp(v.name)]

    def _rand_scalper(self):
        name = self.rng.choice(SCALP_STRATS)
        return self._new(name, self._rand_params(name), self.rng.choice((1, 1, 2)), None, self.rand_traits(0.2),
                         self.rand_vetoes(), self.rand_layers(0.25))

    def spawn_scalp(self, closes=None):
        """Hatches one scalper: a tweaked copy of the best scalper (winners first), or a random one. Makes room by
        dropping the weakest long-term bot that isn't winning. Returns the new bot or None."""
        ranked = [v for v, _ in self.ranking() if is_scalp(v.name)]
        par = next((v for v in ranked if v in self.active), None)
        if par is None and ranked and self.rng.random() < 0.6:
            par = ranked[0]
        if len(self.vars) >= self.cap:
            cands = [v for v, _ in self.weakest() if v not in self.active and not is_scalp(v.name)]
            cands = [v for v in cands if sum(1 for x in self.vars if x.name == v.name) > 1] or cands
            if not cands:
                return None
            self._remove(cands[0], "made room for a scalper")
        if par is not None:
            vt, vs = self.inherit_vetoes([par])
            ly, ls = self.inherit_layers([par], par.name)
            child = self._new(par.name, self.mutate(par.name, par.params, 0.25), self.mutate_tf(par.tf),
                              self.mutate_risk(par.risk), self.mutate_traits(par.traits), vt, ly)
            child.veto_stats, child.layer_stats = vs, ls
            self._register(child, [par], "scalp hatch")
            note = f"Scalp egg: {child.label} (from {par.label})"
        else:
            child = self._rand_scalper()
            self._register(child, [], "random scalper")
            note = f"Scalp egg: {child.label}"
        if closes:
            self.replay(child, closes)
        self.vars.append(child)
        self.events.append(note)
        return child

    def scalp_balance(self, closes):
        """Keeps the scalp breed between its share of the farm and twice that (never over 60%): enough scalpers to
        find the quick moves, never so many that the long-term bots are crowded out."""
        tgt, sc = self.scalp_target(), self.scalpers()
        if not tgt:
            for v in sc:
                self._remove(v, "scalpers switched off")
            if sc:
                self.events.append(f"Scalpers are off: {len(sc)} scalp bot{'s' if len(sc) != 1 else ''} left the farm")
            return
        for _ in range(min(2, tgt - len(sc))):  # a couple per round, so the farm isn't flooded at once
            self.spawn_scalp(closes)
        most = max(tgt, min(tgt * 2, int(self.cap * 0.6)))
        sc = self.scalpers()
        if len(sc) > most:
            weak = [v for v, _ in self.weakest() if is_scalp(v.name) and v not in self.active]
            for v in weak[:len(sc) - most]:
                self._remove(v, "too many scalpers")
                self.events.append(f"Dropped {v.label} (the scalp den was full)")

    def rand_traits(self, chance=0.3):
        t = {}
        for k, spec in TRAITS.items():
            if k == "kami":
                continue
            if self.rng.random() < chance:
                v = self.rng.uniform(spec["lo"], spec["hi"])
                if k == "moods":
                    v = self.rng.randint(1, 14)
                t[k] = v
        return clean_traits(t)

    def _rand_sub(self, avoid=None):
        name = self.rng.choice([n for n in WILDCARD_STRATS if n != avoid] or WILDCARD_STRATS)
        return dict(name=name, params=self._rand_params(name))

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

    def rule_allowed(self, k):
        return (k not in EVENT_VETOES or events()) and (k not in TIME_VETOES or self.o.get("time_habits", True))

    def learn_from_losses(self, v, t):
        """A bot learns from its OWN losing trades while it lives: when one never-rule would have blocked at least
        3 of its losing buys and clearly fewer winners, the bot takes that rule on itself. The rule is then graded
        like any other, so it fades if it starts blocking good trades."""
        for k in t["flags"]:
            st = v.own_review.setdefault(k, [0, 0])
            st[0 if t["pl"] <= 0 else 1] += 1
        if len(v.vetoes) >= MAX_VETOES:
            return
        best = max(((st[0] - 2 * st[1], k) for k, st in v.own_review.items()
                    if k not in v.vetoes and st[0] >= 3 and st[0] > 2 * st[1] and self.rule_allowed(k)), default=None)
        if not best:
            return
        k = best[1]
        spec = VETOES[k]
        v.vetoes = clean_vetoes(dict(v.vetoes, **{k: (spec["lo"] + spec["hi"]) / 2}))
        v.__dict__.pop("_key", None)  # its genes changed: its identity (cached at birth) must be worked out again
        v.own_review.pop(k, None)
        if k in v.vetoes:
            v.mut_kinds = list(v.mut_kinds) + ["self_review"]
            self.events.append(f"{v.label} learned from its losses: {spec['text'](v.vetoes[k])}")

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

    def rand_layers(self, chance=0.35):
        """Extra rule layers for a new bot: a few positive studies, a few negative studies, exit studies, sizing."""
        if not self.o.get("layers", True):
            return {}
        out = {}
        if self.o.get("htf_studies", True) and self.rng.random() < chance * 0.6:
            out["htf"] = dict(self._rand_sub(), mult=self.rng.choice(HTF_MULTS))
        if self.ctx_syms and self.rng.random() < chance * 0.7:
            out["ctx"] = [self.rand_ctx() for _ in range(self.rng.randint(1, 2))]
        if self.o.get("lead_study", True) and self.rng.random() < chance * 0.4:
            out["lead"] = dict(lb=self.rng.randint(4, 30), th=round(self.rng.uniform(0.3, 2.5), 2))
        if self.rng.random() < chance:
            out["plus"] = [self._rand_sub() for _ in range(self.rng.randint(1, 3))]
        if self.rng.random() < chance:
            out["minus"] = [self._rand_sub() for _ in range(self.rng.randint(1, 2))]
        if self.rng.random() < chance * 0.7:
            out["exits"] = [self._rand_sub() for _ in range(self.rng.randint(1, 2))]
        if out.get("plus") or out.get("minus"):
            out["need"] = self.rng.randint(1, 1 + len(out.get("plus", [])))
        if self.rng.random() < chance * 0.6:
            out["size"] = dict(target=self.rng.uniform(0.15, 1.5))
        return clean_layers(out)

    def inherit_layers(self, parents, main_name):
        """Studies are pooled from the parents; each is usually kept (sometimes tweaked or swapped for another
        family), now and then dropped, and now and then a new one is tried. If negative studies have been costing
        money by blocking winning buys, they are more likely to be dropped; if they've been saving money, kept."""
        if not self.o.get("layers", True):
            return {}, {}
        out, stats = {}, {}
        caps = {"plus": MAX_PLUS, "minus": MAX_MINUS, "exits": MAX_EXITS}
        for key, cap in caps.items():
            pool = []
            for p in parents:
                for s in p.layers.get(key, []):
                    if all(json.dumps(s, sort_keys=True) != json.dumps(x, sort_keys=True) for x in pool):
                        pool.append(json.loads(json.dumps(s)))
            src_stats = [p.layer_stats.get(key) for p in parents if p.layer_stats.get(key)]
            st = max(src_stats, key=lambda x: x[0]) if src_stats else None
            bad = bool(st and st[0] >= 5 and st[1] < 0)
            good = bool(st and st[0] >= 5 and st[1] > 0)
            keep_p = 0.55 if bad else 0.92 if good else 0.8
            kept = []
            for s in pool:
                if self.rng.random() > keep_p:
                    continue
                roll = self.rng.random()
                if roll < 0.1:
                    s = self._rand_sub(avoid=s["name"])
                elif roll < 0.4:
                    s["params"] = self.mutate(s["name"], s["params"], 0.2)
                kept.append(s)
            self.rng.shuffle(kept)
            if len(kept) < cap and self.rng.random() < 0.12 * self.mut_bias("studies"):
                kept.append(self._rand_sub())
            if kept:
                out[key] = kept[:cap]
                if st and not bad and key in ("plus", "minus"):
                    stats[key] = [st[0] / 2, st[1] / 2]
        needs = [p.layers.get("need") for p in parents if p.layers.get("need")]
        if out.get("plus") or out.get("minus"):
            need = self.rng.choice(needs) if needs else 1 + len(out.get("plus", [])) // 2
            if self.rng.random() < 0.25:
                need += self.rng.choice((-1, 1))
            out["need"] = need
        if self.ctx_syms or any(p.layers.get("ctx") for p in parents):  # context studies, judged by their own evidence
            pool, seen = [], set()
            for p in parents:
                for e in p.layers.get("ctx", []):
                    k_ = json.dumps(e, sort_keys=True)
                    if k_ not in seen:
                        seen.add(k_)
                        pool.append(json.loads(k_))
            kept = []
            for e in pool:
                st = max((p.layer_stats.get(f"ctx:{e['sym']}") for p in parents if p.layer_stats.get(f"ctx:{e['sym']}")),
                         key=lambda x: x[0], default=None)
                bad, good = bool(st and st[0] >= 5 and st[1] < 0), bool(st and st[0] >= 5 and st[1] > 0)
                if self.rng.random() > (0.55 if bad else 0.92 if good else 0.8):
                    continue
                roll = self.rng.random()
                if roll < 0.06:
                    e["sign"] = -e["sign"]
                elif roll < 0.35:
                    if e.get("kind") == "study":
                        e["params"] = self.mutate(e["name"], e["params"], 0.2)
                    else:
                        e["lb"] = int(e["lb"] * self.rng.uniform(0.7, 1.4))
                        e["th"] = e["th"] * self.rng.uniform(0.75, 1.3)
                kept.append(e)
                if st and not bad:
                    stats[f"ctx:{e['sym']}"] = [st[0] / 2, st[1] / 2]
            if self.ctx_syms and len(kept) < MAX_CTX and self.rng.random() < 0.1 * self.mut_bias("studies"):
                kept.append(self.rand_ctx())
            if kept:
                out["ctx"] = kept[:MAX_CTX]
        for key, on in (("htf", self.o.get("htf_studies", True)), ("lead", self.o.get("lead_study", True))):
            if not on:
                continue
            have = [p.layers[key] for p in parents if p.layers.get(key)]
            st = max((p.layer_stats.get(key) for p in parents if p.layer_stats.get(key)), key=lambda x: x[0], default=None)
            bad = bool(st and st[0] >= 5 and st[1] < 0)
            if have and self.rng.random() < (0.55 if bad else 0.9):
                g = json.loads(json.dumps(self.rng.choice(have)))
                if self.rng.random() < 0.3:
                    if key == "htf":
                        if self.rng.random() < 0.5:
                            g["params"] = self.mutate(g["name"], g["params"], 0.2)
                        else:
                            g["mult"] = self.rng.choice(HTF_MULTS)
                    else:
                        g["lb"] = int(g["lb"] * self.rng.uniform(0.7, 1.4))
                        g["th"] = g["th"] * self.rng.uniform(0.75, 1.3)
                out[key] = g
                if st and not bad:
                    stats[key] = [st[0] / 2, st[1] / 2]
            elif not have and self.rng.random() < 0.06 * self.mut_bias("studies"):
                out[key] = (dict(self._rand_sub(), mult=self.rng.choice(HTF_MULTS)) if key == "htf"
                            else dict(lb=self.rng.randint(4, 30), th=round(self.rng.uniform(0.3, 2.5), 2)))
        sizes = [p.layers["size"] for p in parents if p.layers.get("size")]
        if sizes and self.rng.random() < 0.9:
            out["size"] = dict(target=self.rng.choice(sizes)["target"] * (self.rng.uniform(0.8, 1.25) if self.rng.random() < 0.4 else 1))
        elif not sizes and self.rng.random() < 0.06:
            out["size"] = dict(target=self.rng.uniform(0.15, 1.5))
        return clean_layers(out), stats

    def rand_vetoes(self, chance=0.35):
        if not self.o.get("vetoes", True) or self.rng.random() > chance:
            return {}
        out = {}
        for _ in range(self.rng.randint(1, 2)):
            k = self.review_pick([k for k in VETOES if k not in out and self.rule_allowed(k)])
            out[k] = self.rng.uniform(VETOES[k]["lo"], VETOES[k]["hi"])
        return clean_vetoes(out)

    def inherit_vetoes(self, parents):
        """Never-rules for a child: proven rules (saved money when they blocked trades) are very likely passed on,
        rules that keep blocking winners tend to be dropped, values get small tweaks, and now and then a new rule
        is tried. The child also inherits half of its parents' evidence for each rule."""
        if not self.o.get("vetoes", True):
            return {}, {}
        pool = {}
        for p in parents:
            for k, x in p.vetoes.items():
                st = p.veto_stats.get(k, [0.0, 0.0])
                avg = st[1] / st[0] if st[0] else 0.0
                if k not in pool or avg > pool[k][1]:
                    pool[k] = (x, avg, st)
        out, stats = {}, {}
        for k, (x, avg, st) in sorted(pool.items(), key=lambda kv: -kv[1][1]):
            proven_bad = st[0] >= 5 and avg < 0
            keep = 0.25 if proven_bad else 0.95 if avg > 0 and st[0] >= 5 else 0.8
            if self.rng.random() < keep and len(out) < MAX_VETOES:
                if self.rng.random() < 0.35:
                    x *= self.rng.uniform(0.8, 1.25)
                out[k] = x
                stats[k] = [st[0] / 2, st[1] / 2]
        if len(out) < MAX_VETOES and self.rng.random() < 0.15 * self.mut_bias("rules"):
            k = self.review_pick([k for k in VETOES if k not in out and self.rule_allowed(k)])
            out[k] = self.rng.uniform(VETOES[k]["lo"], VETOES[k]["hi"])
        out = clean_vetoes(out)
        return out, {k: s for k, s in stats.items() if k in out}

    def mutate_traits(self, t):
        out = dict(t)
        for k, spec in TRAITS.items():
            if k == "kami":  # a breed, not a habit: never switched on or off by mutation
                continue
            roll = self.rng.random()
            if roll < 0.08 * self.mut_bias("traits"):  # switch a trait on or off
                if k in out:
                    out.pop(k)
                else:
                    out[k] = self.rng.randint(1, 14) if k == "moods" else self.rng.uniform(spec["lo"], spec["hi"])
            elif k in out and roll < 0.35:
                if k == "vixmode":  # a choice, not an amount: try another one
                    out[k] = self.rng.randint(1, 5)
                elif k == "moods":
                    out[k] = out[k] ^ (1 << self.rng.randrange(len(MOODS))) or out[k]
                else:
                    out[k] = out[k] * self.rng.uniform(0.75, 1.3)
        return clean_traits(out)

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

    def _remove(self, v, why):
        if v in self.vars and getattr(v, "hs_src", None):  # a hindsight egg leaving before its exams: how did it do?
            self.grade_hindsight(v, bool(v.eq) and v.sim.start > 0 and v.eq[-1] > v.sim.start)
        if v in self.vars and is_kami(v):
            try:
                self.kami_learn(v)
            except Exception:
                note_error("_remove")
        if v in self.vars:
            self.vars.remove(v)
            if self.o.get("elders", True) and v.ntrades >= 3 and v.eq:
                lf = self.life(v)
                if lf["ret"] > 0 and (lf["edge"] is None or lf["edge"] > 0):
                    self.elders.append(dict(name=v.name, params=dict(v.params), tf=v.tf, edge=lf["edge"] or lf["ret"]))
                    del self.elders[:-40]
        self.deaths.append(self.n)
        rec = self.lineage.get(v.uid)
        if rec:
            rec.update(died=self.n, why=why, ret=v.eq[-1] / v.sim.start - 1 if v.eq else None, inducted=v.inducted)
        dead = [u for u, r in self.lineage.items() if r["died"] is not None]
        for u in dead[:-400]:  # keep the tree from growing forever
            del self.lineage[u]

    def rand_risk(self):
        """About half of new random bots carry their own stop-loss and/or take-profit."""
        r = {}
        for k, (lo, hi) in RISK_BOUNDS.items():
            hi = self.risk_caps.get(k, hi)
            r[k] = round(self.rng.uniform(lo, hi * 0.5), 2) if self.rng.random() < 0.5 else 0.0
        return r

    def mutate_risk(self, r):
        out = {}
        for k, (lo, hi) in RISK_BOUNDS.items():
            hi = self.risk_caps.get(k, hi)
            v = float(r.get(k, 0) or 0)
            roll = self.rng.random()
            if roll < 0.1 * self.mut_bias("stops"):  # switch it on or off
                v = 0.0 if v else self.rng.uniform(lo, hi * 0.5)
            elif v and roll < 0.45:
                v *= self.rng.uniform(0.7, 1.3)
            out[k] = min(v, hi)
        return clean_risk(out)

    def mutate_tf(self, tf):
        """Usually keep the parent's pace; sometimes move one step faster or slower."""
        i = TIMEFRAMES.index(clamp_tf(tf))
        if self.rng.random() < min(0.8, 0.4 * self.mut_bias("pace")):
            i = min(max(i + self.rng.choice((-1, 1)), 0), len(TIMEFRAMES) - 1)
        return TIMEFRAMES[i]

    def _run(self, v, closes, n, mood=None, extra=None):
        last = {c: closes[c][-1] for c in self.coins}
        if v.born_px is None:
            v.born_px = dict(last)
        prev_eq = v.eq[-1] if v.eq else None
        x = extra if extra is not None else self.xfull
        made = bot_step(v, closes, n, self.coins, x)
        self._book(v, made, last, n, x)
        e = v.sim.equity(last)
        if is_scalp(v.name) and (not v.trade_eq or any(t_["side"] == "sell" for _, t_ in made)):
            v.trade_eq.append((n, e))  # scalpers are judged trade by trade (the first entry is the starting point)
        if not self.replaying:
            if v.live_eq0 is None:
                v.live_eq0 = prev_eq if prev_eq else v.sim.start
            v.live_n += 1
        v.eq.append(e)
        v.age += 1
        v.peak = max(v.peak, e)
        if v.peak > 0:
            v.maxdd = max(v.maxdd, (v.peak - e) / v.peak)
        if mood and mood != "unknown" and prev_eq:
            v.mood_gain[mood] = v.mood_gain.get(mood, 0.0) + (e / prev_eq - 1)
            v.mood_n[mood] = v.mood_n.get(mood, 0) + 1
        if prev_eq:  # its record in each condition (wild or calm, time of day, news days): who to trust when
            g_ = e / prev_eq - 1
            for cd in getattr(self, "conds", ()):
                v.cond_gain[cd] = v.cond_gain.get(cd, 0.0) + g_
                v.cond_n[cd] = v.cond_n.get(cd, 0) + 1
        while v.sim.tbars and v.sim.tbars[0] <= n - (v.eq.maxlen - 1):
            v.sim.tbars.popleft()

    def think(self, closes, extra=None, parts=2):
        """A decision between candles: every bot looks at the candle that's still forming (the live price) and may act
        on it, its stops and exits included, without waiting for the minute to end. Its score still moves one candle at
        a time, so how bots are judged doesn't change. A ticker a bot already traded on this candle is left alone until
        the next one, so nobody flips in and out on the same candle."""
        self.xfull = extra
        self.thinks = getattr(self, "thinks", 0) + 1
        last = {c: closes[c][-1] for c in self.coins}
        n = self.n
        half = self.thinks % 2  # half the farm thinks each time (both halves every 30 s), so the screen stays smooth
        for i, v in enumerate(self.vars):
            if parts > 1 and i % parts != half % parts:
                continue
            coins = [c for c in self.coins if not (v.log and any(x[0] == n and x[1] == c for x in list(v.log)[-4:]))]
            if not coins:
                continue
            made = bot_step(v, closes, n, coins, extra)
            if made:
                self._book(v, made, last, n, extra)
                if is_scalp(v.name) and any(t_["side"] == "sell" for _, t_ in made):
                    v.trade_eq.append((n, v.sim.equity(last)))

    def _book(self, v, made, last, n, x):
        """Keeps the books on a bot's trades: counts, wins and losses, its best and worst, the lessons from its losses."""
        for c, t in made:
            v.log.append((n, c, t["side"], last[c]))
            typ_ = (t.get("contract") or {}).get("type")
            if typ_:  # call or put, for the chart marks and the farm's trade pop-ups
                v.log_ct[(n, c)] = typ_
                if len(v.log_ct) > 700:
                    keep = {(x[0], x[1]) for x in v.log}
                    v.log_ct = {k_: x for k_, x in v.log_ct.items() if k_ in keep}
            v.ntrades += 1
            if t["side"] == "sell":
                if t["pl"] > 0:
                    v.wins += 1
                else:
                    v.losses += 1
                paid_ = t["usd"] - t["pl"]
                if paid_ > 0:
                    pc_ = t["pl"] / paid_ * 100
                    v.best_tr = pc_ if v.best_tr is None else max(v.best_tr, pc_)
                    v.worst_tr = pc_ if v.worst_tr is None else min(v.worst_tr, pc_)
                if t.get("flags") and not self.replaying:
                    for k in t["flags"]:
                        self.review.setdefault(k, [0, 0])[0 if t["pl"] <= 0 else 1] += 1
                    if self.o.get("learn_losses", True):
                        self.learn_from_losses(v, t)
            if self.recording:
                self.shadow.append(dict(t, name=v.name, params=dict(v.params), tf=v.tf, uid=v.uid))
            if self.on_trade:
                try:
                    self.on_trade(v, c, t, data_now(x, [c]) if x else None)
                except Exception:
                    note_error("_book")

    def hold_over(self, k):
        """What just holding the coins (equal weight) returned over the last k decisions, or None."""
        hc = self.hold_curve
        return hc[-1] / hc[-1 - k] - 1 if len(hc) > k else None

    def _market(self, closes):
        if not hasattr(self, "hold_curve"):
            self.hold_curve = deque([1.0], maxlen=self.W * 10 + 2)
        if all(len(closes[c]) >= 2 and closes[c][-2] for c in self.coins):
            self.hold_curve.append(self.hold_curve[-1] * sum(closes[c][-1] / closes[c][-2] for c in self.coins) / len(self.coins))
        self.last_px = {c: closes[c][-1] for c in self.coins}
        L = min(len(closes[c]) for c in self.coins)
        self.hold_ret = (sum(closes[c][-1] / closes[c][-1 - self.W] for c in self.coins) / len(self.coins) - 1
                         if L > self.W else None)
        self.moods = {c: market_mood(closes[c])[0] for c in self.coins}
        known = [m for m in self.moods.values() if m != "unknown"]
        self.mood = max(set(known), key=known.count) if known else "unknown"
        self.conds = market_conds(closes, getattr(self, "xfull", None), self.coins) if self.o.get("cond_aware", True) else []

    def step(self, closes, extra=None):
        """closes: coin -> list of bar closes, newest last, same length per coin.
        extra (optional): coin -> {'h': highs, 'l': lows, 'v': volumes}, lined up with the closes."""
        self.n += 1
        self.xfull = extra
        self._market(closes)
        for k in set(self.coins) | set(self.cool) | set(self.add_cool):
            self.cool[k] = max(0, self.cool.get(k, 0) - 1)
            self.add_cool[k] = max(0, self.add_cool.get(k, 0) - 1)
        for v in self.vars:
            self._run(v, closes, self.n, self.mood)
        self._rate()
        for v in self.vars:
            if self.info[id(v)]["status"] == "active":
                v.win_steps += 1
                v.streak += 1
            else:
                v.streak = 0
        if self.retire_steps:
            self._rotate(closes)
        if self.n % self.evolve_every == 0:
            self.evolve(closes)

    def warmup(self, closes, start=WARM, progress=None, extra=None):
        L = min(len(closes[c]) for c in self.coins)
        for i in range(start, L):
            self.step({c: View(closes[c], i + 1) for c in self.coins}, slice_extra(extra, i + 1))
            if progress and (i - start) % 50 == 0:
                progress(i - start, L - start)

    def score(self, v):
        te = scalp_record(v)
        if te:  # a scalper: its gain over its last trades, however long it waited between them
            return te[-1][1] / te[0][1] - 1 if te[0][1] > 0 else None
        need = self.min_trades if v.tf < 10 else 1  # slow, long-holding bots trade rarely by design
        if len(v.eq) < v.eq.maxlen or len(v.sim.tbars) < need or v.eq[0] <= 0:
            return None
        return v.eq[-1] / v.eq[0] - 1

    def consistency(self, v):
        """Share of the 4 recent sub-periods in which the variant made money (0..1). Scalpers: 4 blocks of trades."""
        te = scalp_record(v)
        if te:
            eq = [x for _, x in te]
            size = (len(eq) - 1) // 4
            return sum(1 for k in range(4) if eq[(k + 1) * size] > eq[k * size]) / 4 if size >= 1 else 0.0
        eq = list(v.eq)
        size = (len(eq) - 1) // 4
        if len(eq) < v.eq.maxlen or size < 1:
            return 0.0
        return sum(1 for k in range(4) if eq[(k + 1) * size] > eq[k * size]) / 4

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

    def cond_mult(self, v):
        """How much more (or less) to trust a bot right now, from its own record in today's conditions compared with
        its record overall: a bot that shines in wild markets leads when the market is wild, and steps back when calm."""
        n_all = max(1, len(v.eq) - 1)
        overall = (v.eq[-1] / v.eq[0] - 1) / n_all if len(v.eq) > 1 and v.eq[0] > 0 else 0.0
        m = 1.0
        for cd in getattr(self, "conds", None) or []:
            f = cond_fit(v, cd)
            if f is None:
                continue
            m *= 0.75 if f <= 0 else 1.2 if f > overall else 1.0
        return min(1.6, max(0.4, m))

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

    def _rate(self):
        info, o = {}, self.o
        for v in self.vars:
            sc = self.score(v)
            if sc is None:
                info[id(v)] = dict(score=None, cons=None, weight=0.0, stake=0.0, status="warming up", failing=False,
                                   edge=None, behind=False)
                continue
            cons = self.consistency(v)
            te = scalp_record(v)
            span = (self.n - te[0][0]) if te else len(v.eq) - 1  # scalpers: holding over the stretch of their trades
            if hasattr(self, "hold_curve"):
                span = max(1, min(span, len(self.hold_curve) - 1))
            hold = self.hold_over(span) if hasattr(self, "hold_curve") else self.hold_ret
            edge = None if hold is None else sc - hold
            behind = bool(o["beat_hold"] and edge is not None and edge <= 0)
            failing = (o["fail_drop"] > 0 and self.drawdown(v) * 100 > o["fail_drop"]) or self.health(v) < 0.1
            self.update_power(v, None if self.hold_ret is None or hold is None else sc - hold)
            base = edge if (o["beat_hold"] and edge is not None and edge > 0) else sc
            proving = bool(o.get("prove_live", True)) and (
                v.live_n < max(10, (v.eq.maxlen - 1) // 4) or (v.eq and v.live_eq0 and v.eq[-1] < v.live_eq0))
            exam = exam_verdict(v, self.serial)[0] if o.get("exam_gate", True) and not is_kami(v) else "off"
            if failing or sc <= 0 or cons < 0.25:
                status, w = "benched", 0.0
            elif cons >= 0.5 and not behind and not proving and exam != "failed":
                status, w = "active", base * cons ** 2
            else:
                status, w = "slowed", sc * cons ** 2
            if exam in ("passed", "failed") and getattr(v, "hs_src", None):
                self.grade_hindsight(v, exam == "passed")
            if w > 0 and exam != "off":  # results on days it never trained on: passing counts for more, failing for less
                w *= {"passed": 1.3, "failed": 0.5}.get(exam, 0.85)
            if w > 0 and o.get("risk_adjusted", True):
                w *= 0.5 + min(self.sortino(v), 5.0) / 2  # steady gains count for more than bumpy ones
            if w > 0:
                w *= 1 + v.power / 10  # proven power: a bigger say
            if w > 0 and v.twin is not None and v.twin <= 0:
                w *= 0.6  # failed its twin test: probably lucky, so it counts for less
            if w > 0 and o.get("luck_check", True):
                w *= luck_factor(v)
            if w > 0 and is_kami(v):
                w *= KAMI_SAY  # kamikazes are tried, never trusted much
            scn = [a + b for a, b in zip(getattr(v, "scan", None) or [0, 0, 0.0, 0.0],
                                         getattr(v, "roam_scan", None) or [0, 0, 0.0, 0.0])]  # [trades, wins, pl, paid]
            if w > 0 and scn[0] >= 10 and scn[3] > 0:  # scalp scan today + scalp roams in the past, on other tickers
                w *= 1.2 if scn[2] > 0 else 0.75  # a great score from a handful of trades counts for less than one from many
            if w > 0 and o["regime_aware"]:
                fit = self.mood_fit(v)
                if fit is not None:
                    w *= 1.25 if fit > 0 else 0.8
            if w > 0 and o.get("cond_aware", True) and getattr(self, "conds", None):
                w *= self.cond_mult(v)
            info[id(v)] = dict(score=sc, cons=cons, weight=w, stake=0.0, status=status, failing=failing,
                               edge=edge, behind=behind, proving=proving and status == "slowed" and not behind,
                               exam=exam)
            if status == "active" and not v.credited:
                v.credited = True
                for k in v.mut_kinds:
                    self.mut_stats.setdefault(k, [0, 0])[1] += 1
        ranked = sorted((v for v in self.vars if info[id(v)]["status"] == "active"),
                        key=lambda v: -info[id(v)]["weight"])
        self.active = ranked[:self.max_active]
        top = info[id(self.active[0])]["weight"] if self.active else 0
        hi = max(1.0, o["winner_boost"] if o["trade_more"] else 2.0)
        for v in self.active:
            d = info[id(v)]
            d["stake"] = 0.5 + (hi * (1 + v.power / 20) - 0.5) * d["weight"] / top  # power lifts the size ceiling
        for v in ranked[self.max_active:]:
            info[id(v)].update(status="standby", weight=0.0)
        self.slowed = sorted((v for v in self.vars if info[id(v)]["status"] == "slowed"),
                             key=lambda v: -info[id(v)]["weight"])
        for v in self.slowed:
            info[id(v)]["stake"] = 0.25
        for v in self.vars:
            d = info[id(v)]
            if d["status"] in ("active", "slowed"):
                d["stake"] *= v.size_mult
        self.info = info

    def young(self, v):
        """Still within its first window of decisions: too new to judge, so never dropped to make room."""
        return self.score(v) is None and v.age < self.window_for(v.tf)

    def contender(self, v):
        """How close this bot is to earning a vote, or None when it isn't on course: it passed its exams, or its
        exams so far are positive and it isn't behind just holding. Bigger = closer."""
        if is_kami(v) or v.origin == "team":
            return None
        verdict, d = exam_verdict(v, self.serial)
        if verdict == "failed" or not d.get("k"):
            return None
        lf = self.life(v)
        if verdict == "passed":
            return 10.0 + (d.get("score") or 0.0)
        if (d.get("mean") or 0) <= 0 or (lf["edge"] is not None and lf["edge"] < 0):
            return None
        return float(d.get("mean") or 0.0) + 0.1 * (v.wins + v.losses + exam_trades(v))

    def protected(self):
        """Bots on course to earn a vote, kept off the retirement and make-room lists so they live long enough to
        finish their record (at most a third of the farm, closest first, so the farm keeps evolving)."""
        key = (self.n, len(self.vars), self.serial)
        if getattr(self, "_prot_key", None) == key:
            return self._prot
        if not self.o.get("proven_only", True):
            out = set()
        else:
            c = [(self.contender(v), v) for v in self.vars]
            c = sorted(((x, v) for x, v in c if x is not None), key=lambda xv: -xv[0])
            out = {id(v) for _, v in c[:max(1, self.cap // 3)]}
        if self.o.get("keep_veterans", True):
            # proven veterans: 8+ finished trades, money made over their whole life and more than just holding. A bad
            # patch benches them (no say in trades) but doesn't kill them; they only go once their lifetime record does.
            vet = []
            for v in self.vars:
                if id(v) in out or is_kami(v) or (v.wins + v.losses) < 8:
                    continue
                lf = self.life(v)
                if lf["ret"] > 0 and lf["edge"] is not None and lf["edge"] > 0:
                    vet.append((lf["edge"], v))
            vet.sort(key=lambda x: -x[0])
            out = out | {id(v) for _, v in vet[:max(1, self.cap // 6)]}
        self._prot_key, self._prot = key, out
        return out

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

    def ranking(self):
        r = [(v, self.score(v)) for v in self.vars]
        if self.o.get("luck_check", True):  # breeding and culling also trust long records over short lucky ones
            return sorted([x for x in r if x[1] is not None],
                          key=lambda x: -(x[1] * luck_factor(x[0]) if x[1] > 0 else x[1]))
        return sorted([x for x in r if x[1] is not None], key=lambda x: -x[1])

    def drivers(self):
        return list(self.active)

    def summary(self):
        """(number of active winners, best variant or None, any variant scored yet)"""
        scored = any(d["score"] is not None for d in self.info.values())
        return len(self.active), (self.active[0] if self.active else None), scored

    @staticmethod
    def _holding(v, c):
        return v.sim.pos.get(c, 0) > 0

    def proven(self, v):
        """May this bot help decide the account's trades? Only when it has a real track record: enough finished
        trades, enough time on the farm, money made over its lifetime and more than just holding, and (by default)
        passed exams on prices it never trained on. Kamikazes never decide; everything else keeps training on its own
        pretend money until it gets there. With the gate off (Settings), every winning bot may."""
        if not self.o.get("proven_only", True):
            return True
        return not self.proven_gaps(v)

    def proven_gaps(self, v):
        """What this bot still lacks before it may trade the account, in plain words ([] = proven)."""
        if v.origin == "team":  # bots you picked yourself to trade with (My team / Hall of Fame only)
            return []
        kami = is_kami(v)
        if kami and not self.o.get("proven_kamikazes", False):
            return ["kamikazes don't trade the account"]
        gaps = []
        need = int(self.o.get("proven_trades", 8) or 0)
        closed = v.wins + v.losses + exam_trades(v)  # trades on days it never trained on count: they're the best evidence
        if closed < need:
            gaps.append(f"{need - closed} more finished trade{'s' if need - closed != 1 else ''}")
        window = self.window_for(v.tf)
        if v.age < window:  # it has lived through at least one full scoring window of its own
            gaps.append(f"{window - v.age} more decisions on the farm")
        lf = self.life(v)
        if lf["ret"] <= 0:
            gaps.append(f"a lifetime profit (now {lf['ret'] * 100:+.1f}%)")
        elif lf["edge"] is not None and lf["edge"] <= 0:
            gaps.append(f"to beat just holding (now {lf['edge'] * 100:+.1f}% vs holding)")
        if self.o.get("proven_exam", True) and not kami:  # (kamikazes aren't examined: their record is their proof)
            verdict, d = exam_verdict(v, self.serial)
            if verdict == "failed":
                gaps.append("to stop failing its exams")
            elif verdict != "passed":
                gaps.append(f"passed exams ({d['k']} of at least {EXAM_MIN} taken)")
        return gaps

    def proven_bots(self):
        return [v for v in self.vars if self.proven(v)]

    def vote(self, c, breed=None, key=None):
        """-> (signal, size multiplier). Winners vote at full weight, shaky strategies at a
        small weight. A vote that only shaky strategies want must wait out a cooldown, so they
        trade slowly. When nobody has a fresh signal but strong winners are holding the coin,
        the account adds to the position now and then (self.kind[key] == "add").
        breed: "scalp" = only scalpers vote, "long" = everyone else, None = the whole farm. key: where this vote's
        cooldowns and notes are kept (the account's book for this ticker; defaults to the ticker)."""
        key = key or c
        keep = (lambda v: True) if breed is None else (lambda v: is_scalp(v.name) == (breed == "scalp"))
        gate = bool(self.o.get("proven_only", True))
        # proven only: just the winning bots with a real track record decide (shaky ones never do)
        active = [v for v in self.active if keep(v) and self.proven(v)]
        slowed = [] if gate else [v for v in self.slowed if keep(v)]
        self.kind[key] = None
        bw = sw = bs = ss = 0.0
        act_buy = act_sell = False
        for v in active + slowed:
            d, s = self.info[id(v)], v.sig.get(c)
            is_act = d["status"] == "active"
            w = d["weight"]
            if gate:  # the best lifetime records lead the way: up to 3x the say for a bot far ahead of holding
                lf = self.life(v)
                w *= 1 + min(2.0, max(0.0, lf["edge"] if lf["edge"] is not None else lf["ret"]))
            if s == "buy":
                bw += w
                bs += w * d["stake"]
                act_buy = act_buy or is_act
            elif s == "sell":
                sw += w
                ss += w * d["stake"]
                act_sell = act_sell or is_act
        sig, mult, from_winner = None, 1.0, False
        if bw > sw:
            sig, mult, from_winner = "buy", bs / bw, act_buy
        elif sw > bw:
            sig, mult, from_winner = "sell", ss / sw, act_sell
        if sig == "buy" and self.o.get("agree_sizing", True):
            # several DIFFERENT strategies agreeing = more confidence; a split farm = less
            kinds = {v.name for v in active + slowed if v.sig.get(c) == "buy"}
            split = sw / (bw + sw) if bw + sw else 0.0
            agree = min(1.3, 0.85 + 0.15 * (len(kinds) - 1)) * (1 - 0.6 * split)
            self.agree[key] = (len(kinds), round(agree, 2), round(split, 2))
            mult *= agree
        if sig and not from_winner:  # only shaky strategies want this: slow it down
            if self.cool.get(key, 0) > 0:
                return None, 1.0
            self.cool[key] = self.o["slow_every"]
            self.kind[key] = "slow"
        if sig is None and self.o["trade_more"] and self.add_cool.get(key, 0) == 0:
            holders = [v for v in active if self._holding(v, c) and self.info[id(v)]["cons"] >= 0.75]
            hw = sum(self.info[id(v)]["weight"] for v in holders)
            tw = sum(self.info[id(v)]["weight"] for v in active)
            if holders and hw * 2 > tw:  # winners with most of the weight are still in this coin
                self.add_cool[key] = self.o["add_every"]
                self.kind[key] = "add"
                avg = sum(self.info[id(v)]["weight"] * self.info[id(v)]["stake"] for v in holders) / hw
                return "buy", 0.5 * avg
        return sig, mult

    def board(self):
        """Rows for the scoreboard / farm. Cached until the ratings change, so drawing never recomputes it."""
        key = (id(self.info), self.n, len(self.vars))
        if getattr(self, "_board_key", None) == key:
            return self._board_rows
        self._board_rows = self._board()
        self._board_key = key
        return self._board_rows

    def _board(self):
        blank = dict(score=None, cons=None, stake=0.0, status="warming up", failing=False, edge=None, behind=False, proving=False)
        order = {"active": 0, "slowed": 1, "standby": 2, "benched": 3, "warming up": 4}
        rows = [(v, self.info.get(id(v), blank)) for v in self.vars]
        rows.sort(key=lambda x: (order[x[1]["status"]], -(x[1]["score"] or 0)))
        return [dict(name=v.name, params=dict(v.params), label=v.label, score=d["score"], cons=d["cons"],
                     stake=d["stake"], status=d["status"], trades=len(v.sim.tbars), strikes=v.strikes, tf=v.tf,
                     failing=d.get("failing", False), driving=d["status"] == "active", v=v, size=v.size_mult,
                     edge=d.get("edge"), behind=d.get("behind", False), proving=d.get("proving", False),
                     life=self.life(v), origin=v.origin,
                     risk=dict(v.risk), traits=dict(v.traits), uid=v.uid, parents=list(v.parents), gen=v.gen,
                     classic=v.classic, vetoes=dict(v.vetoes), veto_stats={k: list(s) for k, s in v.veto_stats.items()},
                     health=self.health(v), power=v.power, layers=json.loads(json.dumps(v.layers)),
                     layer_stats={k: list(s) for k, s in v.layer_stats.items()}, twin=v.twin,
                     bad_coins=[c for c, x in v.coin_pl.items() if x[0] >= 3 and x[1] < -1.5], island=v.island,
                     exam=d.get("exam", "off"))
                for v, d in rows]

    def mutate(self, name, params, scale=0.25):
        spec, new = STRATS[name], {}
        for k, val in params.items():
            lo, hi = spec["bounds"][k]
            nv = min(max(val * self.rng.uniform(1 - scale, 1 + scale), lo), hi)
            new[k] = int(round(nv)) if isinstance(spec["params"][k], int) else round(nv, 2)
        return fix_params(name, new)

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

    def instinct_tick(self):
        """At each review, every instinct's scored bots (averaged per instinct, so a big family counts no more than a
        small one) feed that instinct's lasting record: how far it beats just holding, on average, over time. Unlike
        'winning right now', the record builds up, so the farm keeps what it learned about an instinct even after a
        bad week or after its family has died out. Scalpers and kamikazes are left out (they're judged their own way)."""
        by = {}
        for v in self.vars:
            if v.name not in STRATS or is_kami(v) or is_scalp(v.name):
                continue
            sc = (self.info.get(id(v)) or {}).get("score")
            if sc is None:
                continue
            x = max(-1.0, min(1.0, sc - self.hold_ret if self.hold_ret is not None else sc))
            by.setdefault(v.name, []).append((x, v))
        for name, xs in by.items():
            obs = sum(x for x, _ in xs) / len(xs)
            top, tv = max(xs, key=lambda p: p[0])
            r = self.instincts.get(name)
            if r is None:
                self.instincts[name] = r = [obs, 1, top]
            else:
                a = max(INSTINCT_ALPHA, 1.0 / (r[1] + 1))  # the first reviews count fully, then a slow average
                r[0] += a * (obs - r[0])
                r[1] += 1
            if top >= r[2] or name not in self.instinct_best:
                r[2] = max(r[2], top)
                self.instinct_best[name] = _genes(tv)

    def instinct_proven(self, name):
        """Has this instinct earned its place here? Enough reviews, and on average its bots beat just holding."""
        r = self.instincts.get(name)
        return bool(r and r[1] >= INSTINCT_PROVEN and r[0] > 0)

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

    def evolve(self, closes):
        if self.drop_twins():
            self._rate()
        self.instinct_tick()
        for v in self.vars:  # strikes: checks spent benched (slowed counts half, standby none)
            st = self.info[id(v)]["status"]
            if st == "active":
                v.strikes = 0
            elif st == "benched":
                v.strikes += 1
            elif st == "slowed":
                v.strikes += 0.5
        retired = 0
        keep = self.protected()
        for v in sorted(self.vars, key=lambda x: -x.strikes):
            if id(v) in keep:  # on course to earn a vote: let it finish its record
                continue
            if v.strikes >= self.retire_after and sum(1 for x in self.vars if x.name == v.name) > 1:
                self._remove(v, "retired (kept losing)")
                retired += 1
                self.events.append(f"Retired {v.label} (kept losing)")
        strong = any(self.info[id(v)]["cons"] >= 0.75 and self.info[id(v)]["score"] > 0 for v in self.active)
        for _ in range(max(1, retired) + (1 if self.o["trade_more"] and strong else 0)):
            self._spawn(closes)  # strong winners get an extra tweaked copy
        for _ in range(min(3, self.cap - len(self.vars))):  # keep filling up to the limit
            self._spawn_or_random(closes)
        self.scalp_balance(closes)
        self.kami_balance(closes)
        self.evolves += 1
        k_isl = int(self.o.get("islands", 1) or 1)
        if k_isl > 1 and self.evolves % 8 == 0:
            self.migrate(closes)
        if self.o.get("twin_test", True):
            for v in [x for x in self.active if x.twin is None][:2]:
                self.twin_test(v, closes)
        if self.o.get("seasons", True):
            self.seasons_tick(closes)
        self._rate()

    def migrate(self, closes):
        """Islands: each island's best bot sends a copy of itself to the next island."""
        k_isl = int(self.o.get("islands", 1) or 1)
        moved = 0
        for i in range(k_isl):
            best = next((v for v, _ in self.ranking() if v.island == i and not (is_kami(v) and self.kami_full())), None)
            if not best:
                continue
            if len(self.vars) >= self.cap:
                cands = [v for v, _ in self.weakest() if v not in self.active and v is not best]
                if not cands:
                    continue
                self._remove(cands[0], "dropped to make room")
            child = self._new(best.name, dict(best.params), best.tf, best.risk, best.traits, best.vetoes, best.layers)
            self._register(child, [best], "migrant")
            child.island = (i + 1) % k_isl
            self.replay(child, closes)
            self.vars.append(child)
            moved += 1
        if moved:
            self.events.append(f"Migration: {moved} island champion{'s' if moved != 1 else ''} moved to the next island")

    # ---- elders, twins and seasons ----
    def elder_nudge(self, name, params):
        """Now and then pull a new egg's settings part of the way toward a good bot of its family that left."""
        if not self.o.get("elders", True) or self.rng.random() > 0.3:
            return params
        el = [e for e in self.elders if e["name"] == name]
        if not el:
            return params
        e = self.rng.choices(el, weights=[max(1e-4, x["edge"]) for x in el])[0]
        spec, out = STRATS[name], {}
        for k, val in params.items():
            t = e["params"].get(k, val)
            nv = val + (t - val) * self.rng.uniform(0.2, 0.6)
            out[k] = int(round(nv)) if isinstance(spec["params"][k], int) else round(nv, 2)
        return fix_params(name, out)

    def twin_test(self, v, closes):
        """Runs an exact copy of v on an older stretch of prices it was never scored on. Its edge over holding
        there is kept in v.twin: a winner whose twin loses to holding was probably just lucky."""
        L = min(len(closes[c]) for c in self.coins)
        m = v.eq.maxlen - 1
        end = L - m
        start = end - m
        if start < WARM:
            return None
        tw = self._new(v.name, dict(v.params), v.tf, v.risk, v.traits, v.vetoes, v.layers)
        x = self.xfull if self.xfull and all(len((self.xfull.get(c) or {}).get("h", ())) == len(closes[c])
                                             for c in self.coins) else None
        for i in range(start, end):
            bot_step(tw, {c: View(closes[c], i + 1) for c in self.coins}, i, self.coins, slice_extra(x, i + 1) if x else {})
        lastp = {c: closes[c][end - 1] for c in self.coins}
        ret = tw.sim.equity(lastp) / tw.sim.start - 1
        hold = sum(closes[c][end - 1] / closes[c][start - 1] for c in self.coins) / len(self.coins) - 1
        v.twin = ret - hold
        self.events.append(f"Twin test for {v.label}: {'passed' if v.twin > 0 else 'failed'} "
                           f"({v.twin * 100:+.1f}% vs the market on older prices)")
        return v.twin

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

    def kami_full(self):
        """The farm already has its share of kamikazes, so more would only be culled again."""
        return len(self.kamikazes()) >= self.kami_target()

    def dominant(self):
        """The strategy family that fills more than half of the farm, or None."""
        if len(self.vars) < 12:
            return None
        fam = {}
        for v in self.vars:
            fam[v.name] = fam.get(v.name, 0) + 1
        name, n = max(fam.items(), key=lambda x: x[1])
        return name if n / len(self.vars) > 0.5 else None

    def _spawn(self, closes):
        dom = self.dominant() if self.o.get("keep_variety", True) and not self.o.get("scalp_only") else None
        if dom and self.rng.random() < 0.5:
            # one strategy fills over half the farm: half the new eggs try other ideas, so there are backups ready
            # when the market stops suiting it
            return self.spawn_random(closes, avoid=dom)
        k_isl = int(self.o.get("islands", 1) or 1)
        no_kami = self.kami_full()  # eggs of kamikazes past their share were culled at once, wasting the farm's eggs
        pool = [v for v in self.active if not (no_kami and is_kami(v))]
        if k_isl > 1:  # islands take turns breeding, each from its own winners (or its own best)
            isl = self.spawn_i % k_isl
            self.spawn_i += 1
            pool = [v for v in self.active if v.island == isl and not (no_kami and is_kami(v))]
            if not pool:
                own = [v for v, _ in self.ranking() if v.island == isl and not (no_kami and is_kami(v))]
                pool = own[:1]
        if pool:
            # fitness sharing: a family's weight is shared among its members, so one lucky family can't take over
            fam_n = {}
            for x in self.vars:
                fam_n[x.name] = fam_n.get(x.name, 0) + 1
            parent = self.rng.choices(pool, weights=[max(1e-9, self.info.get(id(v), {}).get("weight", 0)) / fam_n[v.name]
                                                     * (1 + v.power / 10) for v in pool])[0]
        else:
            r = [x for x in self.ranking() if not (no_kami and is_kami(x[0]))]
            if not r:
                return
            parent = r[0][0]
        fam = sum(1 for x in self.vars if x.name == parent.name)
        if fam >= self.family_cap or len(self.vars) >= self.cap:
            cands = [v for v, _ in self.weakest() if v is not parent and v not in self.active
                     and sum(1 for x in self.vars if x.name == v.name) > 1]
            if fam >= self.family_cap:
                cands = [v for v in cands if v.name == parent.name]
            if not cands:
                return
            self._remove(cands[0], "dropped to make room")
            self.events.append(f"Dropped {cands[0].label}")
        mate = None
        if self.o.get("crossbreed", True) and self.rng.random() < 0.4:
            good = [v for v in self.vars if v is not parent and v.name == parent.name and v.island == parent.island
                    and is_kami(v) == is_kami(parent)
                    and self.info.get(id(v), {}).get("status") in ("active", "standby", "slowed")]
            if good:
                mate = self.rng.choice(good)
        if mate:
            risk = {k: self.rng.choice((parent.risk[k], mate.risk[k])) for k in RISK_BOUNDS}
            traits = {}
            for k in TRAITS:
                pick = self.rng.choice((parent, mate)).traits
                if k in pick:
                    traits[k] = pick[k]
            vt, vs = self.inherit_vetoes([parent, mate])
            ly, ls = self.inherit_layers([parent, mate], parent.name)
            child = self._new(parent.name, self.crossover(parent.name, parent.params, mate.params),
                              self.rng.choice((parent.tf, mate.tf)), self.mutate_risk(risk), self.mutate_traits(traits), vt, ly)
            child.veto_stats, child.layer_stats = vs, ls
            self._register(child, [parent, mate], "bred")
            note = f"Bred {child.label} from two winners"
        else:
            scale = 0.35 if not self.active else 0.15 if len(self.active) >= 3 else 0.25  # bolder when nothing works
            vt, vs = self.inherit_vetoes([parent])
            ly, ls = self.inherit_layers([parent], parent.name)
            child = self._new(parent.name, self.elder_nudge(parent.name, self.mutate(parent.name, parent.params, scale)),
                              self.mutate_tf(parent.tf),
                              self.mutate_risk(parent.risk), self.mutate_traits(parent.traits), vt, ly)
            child.veto_stats, child.layer_stats = vs, ls
            self._register(child, [parent], "tweaked")
            note = f"Spawned {child.label} (tweaked from {parent.label})"
        self.replay(child, closes)
        self.vars.append(child)
        self.events.append(note)

    def add_variant(self, name, params, closes=None, size=1.0, note="Added", tf=1, origin="added", risk=None, traits=None,
                    vetoes=None, layers=None):
        """Adds a strategy with the given settings (clamped to sane bounds) and replays recent prices."""
        if name not in STRATS:
            return None
        spec, p = STRATS[name], {}
        for k, dv in spec["params"].items():
            lo, hi = spec["bounds"][k]
            try:
                val = min(max(float(params.get(k, dv)), lo), hi)
            except (TypeError, ValueError):
                val = dv
            p[k] = int(round(val)) if isinstance(dv, int) else round(val, 2)
        fix_params(name, p)
        if len(self.vars) >= self.cap:  # a full farm makes room first (never for a bot too young to judge)
            cands = [x for x, _ in self.weakest() if x not in self.active]
            if cands:
                self._remove(cands[0], "dropped to make room")
                self.events.append(f"Dropped {cands[0].label}")
        v = self._new(name, p, tf, risk, traits, vetoes, layers)
        self._register(v, [], origin)
        v.size_mult = min(max(float(size), 0.25), 3.0)
        if closes:
            self.replay(v, closes)
        self.vars.append(v)
        self.events.append(f"{note} {v.label}")
        self._rate()
        return v

    def _rand_params(self, name):
        spec, p = STRATS[name], {}
        for k, dv in spec["params"].items():
            lo, hi = spec["bounds"][k]
            val = self.rng.uniform(lo, hi)
            p[k] = int(round(val)) if isinstance(dv, int) else round(val, 2)
        if "fast" in p and "slow" in p:
            p["fast"], p["slow"] = sorted((p["fast"], p["slow"]))
            if p["fast"] == p["slow"]:
                p["slow"] += 5
        return fix_params(name, p)

    def breed_pair(self, a, b, closes=None):
        """A child of two given bots (same strategy family: mixes settings; different families: takes one
        parent's strategy and mixes the shared habits). Starts as an egg, like every new bot."""
        pa, pb = (a, b) if self.rng.random() < 0.5 else (b, a)
        params = self.crossover(pa.name, pa.params, pb.params) if pa.name == pb.name else self.mutate(pa.name, pa.params, 0.1)
        risk = {k: self.rng.choice((pa.risk[k], pb.risk[k])) for k in RISK_BOUNDS}
        traits = {k: self.rng.choice((pa.traits, pb.traits))[k] for k in TRAITS
                  if k in self.rng.choice((pa.traits, pb.traits))}
        vt, vs = self.inherit_vetoes([pa, pb])
        ly, ls = self.inherit_layers([pa, pb], pa.name)
        child = self._new(pa.name, params, self.rng.choice((pa.tf, pb.tf)), self.mutate_risk(risk), self.mutate_traits(traits), vt, ly)
        child.veto_stats, child.layer_stats = vs, ls
        self._register(child, [pa, pb], "bred from your bots")
        if closes:
            self.replay(child, closes)
        self.vars.append(child)
        if len(self.vars) > self.cap:
            self.cap = len(self.vars)
        self.events.append(f"Bred {child.label} from two of your bots")
        self._rate()
        return child

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

    def spawn_random(self, closes=None, avoid=None):
        """A brand new strategy with random settings, or now and then a famous one. Makes room by dropping the
        weakest duplicate. A scalpers-only farm (the live scalp lab) hatches a scalper instead."""
        if self.o.get("scalp_only"):
            return self.spawn_scalp(closes)
        hg = self.hindsight_genes() if self.rng.random() < self.hindsight_share() else None
        if hg and avoid and hg.get("name") == avoid:
            hg = None  # making room for other ideas: not another egg of the strategy that already fills the farm
        if hg:  # an egg from what would have worked, rather than a random one
            if len(self.vars) >= self.cap:
                cands = [v for v, _ in self.weakest() if v not in self.active
                         and sum(1 for x in self.vars if x.name == v.name) > 1]
                if not cands:
                    return None
                self._remove(cands[0], "dropped to make room")
            v = self.add_variant(hg["name"], hg["params"], closes, note="Hindsight egg:", tf=hg.get("tf", 1),
                                 origin="hindsight", risk=hg.get("risk"), traits=hg.get("traits"),
                                 vetoes=hg.get("vetoes"), layers=hg.get("layers"))
            if v:
                v.hs_src = hg["name"]
            return v
        name = self.rng.choice([n for n in WILDCARD_STRATS if n != avoid] or list(WILDCARD_STRATS))
        p = self._rand_params(name)
        if len(self.vars) >= self.cap:
            cands = [v for v, _ in self.weakest() if v not in self.active
                     and sum(1 for x in self.vars if x.name == v.name) > 1]
            if cands:
                self._remove(cands[0], "dropped to make room")
                self.events.append(f"Dropped {cands[0].label}")
            else:
                return None
        if self.o.get("classics", True) and self.rng.random() < float(self.o.get("classic_rate", 0.25)):
            v = self.spawn_classic(closes)
            if v:
                return v
        return self.add_variant(name, p, closes, note="Random new strategy:", tf=self.rng.choice(TIMEFRAMES),
                                origin="random", risk=self.rand_risk(), traits=self.rand_traits(0.35),
                                vetoes=self.rand_vetoes(), layers=self.rand_layers())

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

    # ---- saving and restoring the whole farm, so it carries on where it left off ----
    _VFIELDS = ("name", "params", "tf", "risk", "traits", "size_mult", "uid", "parents", "gen", "origin", "royal",
                "classic", "inducted", "strikes", "born_px", "age", "ntrades", "wins", "losses", "win_steps", "streak",
                "peak", "maxdd", "mood_gain", "mood_n", "live_n", "live_eq0", "hi", "since", "entries", "sold_at",
                "fee_guard", "rt_cost", "vetoes", "veto_stats", "lost_at", "power", "live_high", "layers", "layer_stats",
                "conf_last", "coin_pl", "twin", "island", "entry_flags", "swing_ref", "mut_kinds", "credited",
                "backtests", "own_review", "roam_scan", "exams", "cond_gain", "cond_n", "hs_src")

    def to_state(self):
        """Everything needed to bring this farm back exactly as it is (plain data, JSON-ready)."""
        bots = []
        for v in self.vars:
            d = {k: getattr(v, k) for k in self._VFIELDS}
            d.update(eq=list(v.eq), eq_max=v.eq.maxlen, log=list(v.log), bad=getattr(v, "bad", 0),
                     sim=dict(cash=v.sim.cash, pos=v.sim.pos, cost=v.sim.cost, fees=v.sim.fees, tbars=list(v.sim.tbars),
                              opt=getattr(v.sim, "opt", {}), vols=dict(getattr(v.sim, "vol_src", {}) or {})))
            bots.append(d)
        lineage = dict(list(self.lineage.items())[-600:])
        return dict(version=1, scoreboard=SCOREBOARD, n=self.n, serial=self.serial, cap=self.cap, coins=self.coins,
                    start_cash=self.start_cash,
                    last_px={c: float(x) for c, x in (self.last_px or {}).items() if x},
                    elders=self.elders[-40:], champions=self.champions, season=self.season,
                    review=self.review, mut_stats=self.mut_stats, evolves=self.evolves,
                    seasons_log=self.seasons_log[-50:], hindsight=getattr(self, "hindsight", {}),
                    instincts={k: list(x) for k, x in self.instincts.items()}, instinct_best=dict(self.instinct_best),
                    trial_book=getattr(self, "trial_book", {}),
                    hold_curve=list(getattr(self, "hold_curve", []))[-(self.W * 10 + 2):], bots=bots,
                    lineage={str(k): v for k, v in lineage.items()})

    def close_positions(self, prices=None):
        """Closes every bot's open pretend positions at what they're worth right now, as if they had never been
        bought: the money goes back to the bot's cash, so its equity, score and record don't change, and no trade is
        counted. `prices` (ticker -> price) are used first, then the farm's own last prices; with no price at all
        the bot simply gets back what it paid. Used when you reset the practice account. -> positions closed."""
        n = 0
        for v in self.vars:
            for c, q in list(v.sim.pos.items()):
                if q > 0:
                    S = (prices or {}).get(c) or self.last_px.get(c)
                    v.sim.cash += (v.sim.value_of(c, S) if isinstance(v.sim, OptionSim) else q * S) if S else v.sim.cost.get(c, 0.0)
                    n += 1
                v.sim.pos.pop(c, None)
                v.sim.cost.pop(c, None)
                if hasattr(v.sim, "opt"):
                    v.sim.opt.pop(c, None)
                for d in (v.entries, v.since, v.hi, v.entry_flags):
                    d.pop(c, None)
            v.sim.last = None
        return n

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

    def restore(self, st):
        """Replaces this farm's bots with a saved farm (from to_state). Returns True on success."""
        if not isinstance(st, dict) or st.get("version") != 1 or set(st.get("coins") or []) != set(self.coins):
            return False
        vars_ = []
        for d in st.get("bots", []):
            try:
                if d["name"] not in STRATS:
                    continue
                v = self._new(d["name"], dict(d["params"]), d.get("tf", 1), d.get("risk"), d.get("traits"), d.get("vetoes"),
                              d.get("layers"))
                for k in self._VFIELDS:
                    if k in d and k not in ("name", "params", "tf", "risk", "traits"):
                        setattr(v, k, d[k])
                v.eq = deque(d.get("eq", []), maxlen=int(d.get("eq_max") or v.eq.maxlen))
                v.log = deque([tuple(x) for x in d.get("log", [])], maxlen=300)
                v.bad = d.get("bad", 0)
                s = d.get("sim") or {}
                v.sim.cash = float(s.get("cash", v.sim.cash))
                v.sim.pos = {c: float(q) for c, q in (s.get("pos") or {}).items()}
                v.sim.cost = {c: float(q) for c, q in (s.get("cost") or {}).items()}
                v.sim.fees = float(s.get("fees", 0))
                v.sim.tbars = deque(s.get("tbars", []))
                if isinstance(v.sim, OptionSim):
                    v.sim.opt = {c: dict(o) for c, o in (s.get("opt") or {}).items()}
                    v.sim.vol_src = {c: float(x) for c, x in (s.get("vols") or {}).items() if isinstance(x, (int, float))}
                    v.sim.genes = v.traits
                v.vetoes = clean_vetoes(v.vetoes)
                v.veto_stats = {k: list(s) for k, s in (v.veto_stats or {}).items() if k in v.vetoes}
                v.lost_at = dict(v.lost_at or {})
                v.layers = clean_layers(v.layers)
                v.layer_stats = {k: list(s) for k, s in (v.layer_stats or {}).items()}
                v.conf_last = dict(v.conf_last or {})
                v.coin_pl = {c: list(x) for c, x in (v.coin_pl or {}).items()}
                v.entry_flags = {c: list(x) for c, x in (v.entry_flags or {}).items()}
                v.own_review = {k: list(x) for k, x in (v.own_review or {}).items() if k in VETOES}
                v.swing_ref = dict(v.swing_ref or {})
                v.mut_kinds = list(v.mut_kinds or [])
                v.island = int(v.island or 0) % max(1, int(self.o.get("islands", 1) or 1))
                v.mood_gain = dict(v.mood_gain or {})
                # (1.20.x self-tests could use other candle sizes than the bot's own: those don't count as exams)
                v.exams = [dict(x) for x in (v.exams or []) if isinstance(x, dict)
                           and not (x.get("k") == "self-test" and not x.get("g"))][-EXAM_KEEP:]
                v.cond_gain, v.cond_n = dict(v.cond_gain or {}), dict(v.cond_n or {})
                v._key = None
                v.mood_n = dict(v.mood_n or {})
                vars_.append(v)
            except (KeyError, TypeError, ValueError):
                continue
        if not vars_:
            return False
        self.vars = vars_
        self.n = int(st.get("n", 0))
        self.serial = max(int(st.get("serial", 0)), max(v.uid for v in vars_))
        self.cap = max(self.cap, len(vars_))
        self.lineage = {int(k): v for k, v in (st.get("lineage") or {}).items()}
        self.elders = list(st.get("elders") or [])
        self.review = {k: list(x) for k, x in (st.get("review") or {}).items()}
        self.mut_stats = {k: list(x) for k, x in (st.get("mut_stats") or {}).items()}
        self.evolves = int(st.get("evolves", 0) or 0)
        self.champions = dict(st.get("champions") or {})
        self.season = st.get("season")
        self.seasons_log = [tuple(x) for x in st.get("seasons_log") or []]
        self.hindsight = dict(st.get("hindsight") or {})
        self.last_px = {c: float(x) for c, x in (st.get("last_px") or {}).items() if isinstance(x, (int, float)) and x > 0}
        self.instincts = {k: [float(x[0]), int(x[1]), float(x[2]) if len(x) > 2 else float(x[0])]
                          for k, x in (st.get("instincts") or {}).items() if isinstance(x, (list, tuple)) and len(x) >= 2}
        self.instinct_best = {k: g for k, g in (st.get("instinct_best") or {}).items() if isinstance(g, dict)}
        self.trial_book = {k: list(x) for k, x in (st.get("trial_book") or {}).items()}
        hc = st.get("hold_curve") or [1.0]
        self.hold_curve = deque(hc, maxlen=self.W * 10 + 2)
        self.scoreboard_reset = False
        if int(st.get("scoreboard", 1) or 1) < SCOREBOARD:
            fresh_scoreboard(self)
            self.scoreboard_reset = True
        self._rate()
        return True


# ---------------- backtesting ----------------
def simulate(closes, sigfn, start_cash, fee, pct, stop=0.0, take=0.0, warm=WARM, max_coin=None, slip=0.0, trail=0.0,
             extra=None):
    """sigfn(i, prefixes, extra_prefixes) -> {coin: signal}. Returns (equity_curve, trade_count)."""
    coins = list(closes)
    n = min(len(v) for v in closes.values())
    sim = Sim(start_cash, fee, pct, max_coin, slip)
    eq, trades = [start_cash], 0
    high = {}
    for i in range(n):
        pre = {c: View(closes[c], i + 1) for c in coins}
        sigs = sigfn(i, pre, slice_extra(extra, i + 1) if extra else None)
        if i < warm:
            continue
        px = {c: closes[c][i] for c in coins}
        for c in coins:
            q = sim.pos.get(c, 0)
            if q > 0:
                high[c] = max(high.get(c, px[c]), px[c])
                r = px[c] / (sim.cost[c] / q) - 1
                if (stop > 0 and r <= -stop) or (take > 0 and r >= take) or \
                        (trail > 0 and px[c] <= high[c] * (1 - trail)):
                    sim.sell(c, px[c], i)
                    high.pop(c, None)
                    trades += 1
                    continue
            s, m = sigs.get(c), 1.0
            if isinstance(s, tuple):
                s, m = s
            if s == "buy" and sim.buy(c, px[c], i, m):
                high[c] = max(high.get(c, px[c]), px[c])
                trades += 1
            elif s == "sell" and sim.sell(c, px[c], i):
                high.pop(c, None)
                trades += 1
        eq.append(sim.equity(px))
    return eq, trades


def replay_bot(v, closes, extra, start, start_cash, fee, pct, slip=0.0):
    """A frozen copy of bot v trading on its own from bar `start` to the end, exactly as it would in the
    tournament (strategy, pace, own stops and traits). Returns (equity curve, trade count)."""
    coins = list(closes)
    b = Variant(v.name, dict(v.params), make_sim(start_cash, fee, pct, slip=slip), 2, v.tf, dict(v.risk), dict(v.traits),
                dict(v.vetoes), json.loads(json.dumps(v.layers)))
    b.fee_guard, b.rt_cost = v.fee_guard, v.rt_cost
    L = min(len(x) for x in closes.values())
    eq, trades = [start_cash], 0
    for i in range(start, L):
        pre = {c: View(closes[c], i + 1) for c in coins}
        ex = slice_extra(extra, i + 1)
        trades += len(bot_step(b, pre, i, coins, ex))
        eq.append(b.sim.equity({c: closes[c][i] for c in coins}))
    return eq, trades


def _bt_bot(g, coins, start_cash, fee, pct, slip, picky=True):
    """A fresh bot built from genes g (a farm bot or a saved entry), ready to backtest."""
    gg = (lambda k, d=None: g.get(k, d)) if isinstance(g, dict) else (lambda k, d=None: getattr(g, k, d))
    b = Variant(gg("name"), dict(gg("params")), make_sim(start_cash, fee, pct, slip=slip), 2, gg("tf", 1), dict(gg("risk") or {}),
                dict(gg("traits") or {}), dict(gg("vetoes") or {}), json.loads(json.dumps(gg("layers") or {})))
    b.rt_cost = 2 * (fee + slip)
    b.fee_guard = b.rt_cost
    b.picky = picky
    return b


def backtest_bot(g, closes, extra=None, start_cash=1000.0, fee=0.006, pct=0.1, slip=0.0005, segments=4, progress=None,
                 on_trade=None):
    """Full backtest of one bot (its exact genes) on past prices. Returns a dict with:
    ret, hold, edge, maxdd, trades, winrate, sortino, eq (equity curve), hold_eq, marks [(bar, coin, side, price)],
    by_coin {coin: [trades, P/L $]}, by_mood {mood: [decisions, summed gain]}, layer_stats, veto_stats, coin_pl,
    segments [(ret, hold, edge)] (walk-forward: the same bot on each quarter of the history),
    mc_dd95 (shuffled-trade stress: the drawdown you'd see in a bad-luck ordering, 95th percentile),
    fees2x (return with double fees and slippage), review {never-rule: [losing buys it would have stopped, winners]}."""
    coins = list(closes)
    L = min(len(closes[c]) for c in coins)
    start = max(WARM, min(L - 2, max(60, LOOKBACK // 2)))
    b = _bt_bot(g, coins, start_cash, fee, pct, slip)
    eq, marks, rets = [start_cash], [], []
    by_coin, by_mood, review = {}, {}, {}
    mood = "unknown"
    hold_eq = [1.0]
    for i in range(start, L):
        pre = {c: View(closes[c], i + 1) for c in coins}
        if (i - start) % 10 == 0:
            ms = [market_mood(pre[c])[0] for c in coins]
            known = [m for m in ms if m != "unknown"]
            mood = max(set(known), key=known.count) if known else "unknown"
        for c, t in bot_step(b, pre, i, coins, slice_extra(extra, i + 1) if extra else None):
            marks.append((i, c, t["side"], closes[c][i]))
            if on_trade:
                tt = ((extra or {}).get(c) or {}).get("t")
                on_trade(c, t, tt[i] if tt and i < len(tt) else None)
            bc = by_coin.setdefault(c, [0, 0.0])
            bc[0] += 1
            if t["side"] == "sell" and t.get("pl") is not None:
                bc[1] += t["pl"]
                paid = t["usd"] - t["pl"]
                rets.append(t["pl"] / paid if paid > 0 else 0.0)
                for k in t.get("flags", []):
                    review.setdefault(k, [0, 0])[0 if t["pl"] <= 0 else 1] += 1
        e = b.sim.equity({c: closes[c][i] for c in coins})
        if mood != "unknown":
            bm = by_mood.setdefault(mood, [0, 0.0])
            bm[0] += 1
            bm[1] += e / eq[-1] - 1 if eq[-1] else 0
        eq.append(e)
        hold_eq.append(hold_eq[-1] * sum(closes[c][i] / closes[c][i - 1] for c in coins) / len(coins))
        if progress and (i - start) % 200 == 0:
            progress(i - start, L - start)
    ret, dd = metrics(eq)
    hold = hold_eq[-1] - 1
    steps = [eq[k] / eq[k - 1] - 1 for k in range(1, len(eq)) if eq[k - 1] > 0]
    mean = sum(steps) / len(steps) if steps else 0
    down = (sum(min(r, 0) ** 2 for r in steps) / len(steps)) ** 0.5 if steps else 0
    sortino = (mean / down * len(steps) ** 0.5 / 3) if down > 1e-12 else (5.0 if mean > 0 else 0.0)
    segs = []
    n = len(eq) - 1
    for k in range(segments):  # walk-forward: does it hold up in every part of the history?
        a, z = start + k * n // segments, start + (k + 1) * n // segments
        if z - a < 30:
            continue
        sub = backtest_simple(g, closes, extra, a, z, start_cash, fee, pct, slip)
        hr = sum(closes[c][z - 1] / closes[c][a - 1] for c in coins) / len(coins) - 1
        segs.append((sub, hr, sub - hr))
    rng = random.Random(7)
    dds = []
    if len(rets) >= 5:  # shuffled-trade stress: same trades, random order
        for _ in range(200):
            r = rets[:]
            rng.shuffle(r)
            x, pk, md = 1.0, 1.0, 0.0
            for q in r:
                x *= 1 + q * pct
                pk = max(pk, x)
                md = max(md, (pk - x) / pk)
            dds.append(md)
        dds.sort()
    f2 = backtest_simple(g, closes, extra, start, L, start_cash, fee * 2, pct, slip * 2)
    wins = sum(1 for r in rets if r > 0)
    return dict(
        ret=ret, hold=hold, edge=ret - hold, maxdd=dd, trades=len(marks), winrate=wins / len(rets) if rets else None,
        closed=len(rets), wins=wins, best_trade=max(rets) * 100 if rets else None,
        worst_trade=min(rets) * 100 if rets else None, sortino=sortino, eq=eq, hold_eq=[x * start_cash for x in hold_eq], marks=marks, start=start,
        by_coin=by_coin, by_mood=by_mood, layer_stats={k: list(v) for k, v in b.layer_stats.items()},
        veto_stats={k: list(v) for k, v in b.veto_stats.items()}, coin_pl={k: list(v) for k, v in b.coin_pl.items()},
        segments=segs, mc_dd95=dds[int(len(dds) * 0.95)] if dds else None, fees2x=f2, review=review,
        bars=L - start, fees=b.sim.fees)


def backtest_simple(g, closes, extra, a, z, start_cash, fee, pct, slip):
    """Return of bot genes g traded from bar a to bar z (exclusive)."""
    coins = list(closes)
    b = _bt_bot(g, coins, start_cash, fee, pct, slip)
    for i in range(a, z):
        bot_step(b, {c: View(closes[c], i + 1) for c in coins}, i, coins, slice_extra(extra, i + 1) if extra else None)
    return b.sim.equity({c: closes[c][z - 1] for c in coins}) / start_cash - 1


def remember_backtest(target, r, tourn=None):
    """Saves what a backtest learned into a bot's memory: how each of its studies and never-rules did (they then
    breed on with that evidence), which coins it does badly on, how it does in each market mood, and a record of
    the test. target is a farm bot (Variant) or a saved bot entry (dict). With tourn, the farm learns too: the
    post-trade review gets the losing buys, and a bot that beat holding becomes an elder and a season candidate."""
    rec = backtest_summary(r)
    if isinstance(target, dict):
        target.setdefault("backtests", []).append(rec)
        del target["backtests"][:-10]
        ls, vs = target.setdefault("layer_stats", {}), target.setdefault("veto_stats", {})
        _merge_stats(ls, r["layer_stats"])
        _merge_stats(vs, r["veto_stats"])
    else:
        target.backtests = (getattr(target, "backtests", None) or [])[-9:] + [rec]
        _merge_stats(target.layer_stats, r["layer_stats"])
        _merge_stats(target.veto_stats, {k: x for k, x in r["veto_stats"].items() if k in target.vetoes})
        for c, x in r["coin_pl"].items():
            cp = target.coin_pl.setdefault(c, [0, 0.0])
            cp[0] = min(20, cp[0] + x[0])
            cp[1] = cp[1] * 0.5 + x[1] * 0.5
        for m, (k, gsum) in r["by_mood"].items():
            target.mood_n[m] = target.mood_n.get(m, 0) + k
            target.mood_gain[m] = target.mood_gain.get(m, 0.0) + gsum
    if tourn is not None:
        for k, x in r.get("review", {}).items():
            d = tourn.review.setdefault(k, [0, 0])
            d[0] += x[0]
            d[1] += x[1]
        gg = target if isinstance(target, dict) else dict(name=target.name, params=target.params, tf=target.tf)
        if r["edge"] > 0 and r["trades"] >= 4 and gg["name"] in STRATS:
            tourn.elders.append(dict(name=gg["name"], params=dict(gg["params"]), tf=gg.get("tf", 1), edge=r["edge"]))
            del tourn.elders[:-40]
    return rec


def _fresh_money(t):
    """Training passes: every bot keeps its genes and what it learned (rule evidence, coin and mood records),
    but starts the next pass with fresh money and a fresh scoreboard."""
    for v in t.vars:
        v.sim = make_sim(t.start_cash, t.fee, t.pct, slip=t.o.get("slip", 0.0), genes=v.traits)
        v.eq.clear()
        v.hi, v.since, v.entries, v.sold_at, v.run_sig, v.sig = {}, {}, {}, {}, {}, {}
        v.veto_wait, v.lost_at, v.entry_flags, v.conf_last = [], {}, {}, {}
        v.log.clear()
        v.born_px = None
        v.age = v.ntrades = v.wins = v.losses = v.win_steps = v.streak = 0
        v.peak, v.maxdd, v.strikes, v.live_n, v.live_eq0 = 0.0, 0.0, 0, 0, None
        v.inducted = False
    t.hold_curve = deque([1.0], maxlen=t.W * 10 + 2)
    t.info, t.active, t.slowed = {}, [], []


SCOREBOARD = 2  # bumped when old track records can't be trusted any more (2 = practice options were mispriced before 1.23)


def fresh_scoreboard(t):
    """Once, for a farm saved before practice options were priced properly (1.23): every bot's money, track record,
    exams and test results start again, because they were earned on prices that were wildly too generous. Genes, rule
    evidence and everything the farm learned stay. Bots then have to prove themselves again before they may trade."""
    _fresh_money(t)
    for v in t.vars:
        v.exams, v.backtests, v.roam_scan = [], [], None
        v.best_tr = v.worst_tr = None
        v.coin_pl, v.mood_gain, v.mood_n, v.cond_gain, v.cond_n = {}, {}, {}, {}, {}
        v.live_high = None
        v.twin = None
        v._key = None


def train_farm(closes, extra, opts, cap=40, passes=2, exam_frac=0.25, seed_genes=None, start_cash=1000.0, fee=0.006,
               pct=0.1, evolve=24, params=None, retire_steps=0, progress=None, stop=None, seed=None, on_trade=None):
    """Training camp: a whole farm lives through a stretch of history, `passes` times over, breeding and learning as it
    goes (bots keep their genes and lessons between passes, with fresh money each time). The last exam_frac of the
    history is held back: afterwards the best bots are frozen and sent through it as a final exam on prices they never
    saw. Returns dict(tourn, results=[...best first], train_bars, exam_bars, passes_done).
    progress(done, total, text) is called now and then; stop() returning True ends it early (TrainingStopped)."""
    coins = list(closes)
    L = min(len(closes[c]) for c in coins)
    cut = int(L * (1 - exam_frac)) if exam_frac > 0 else L
    if cut - WARM < 100:
        raise ValueError("not enough history: pick a longer stretch or smaller candles")
    train = {c: closes[c][:cut] for c in coins}
    tx = {c: {k: x[:cut] for k, x in extra[c].items()} for c in coins} if extra else None
    if extra and extra.get(CTX_KEY):
        tx[CTX_KEY] = {k: x[:cut] for k, x in extra[CTX_KEY].items()}
    t = Tournament(coins, start_cash, fee, pct, evolve, params=params, opts=opts, cap=cap, retire_steps=retire_steps,
                   rng=random.Random(seed))
    t.on_trade = on_trade
    for g in seed_genes or []:
        if g.get("name") in STRATS:
            t.add_variant(g["name"], g.get("params") or {}, None, g.get("size", 1.0), note="Training seed:", tf=g.get("tf", 1),
                          origin="seeded", risk=g.get("risk"), traits=g.get("traits"), vetoes=g.get("vetoes"), layers=g.get("layers"))
    while len(t.vars) > max(cap, len(seed_genes or [])):
        drop = next((v for v in t.vars if v.origin != "seeded"), None)
        if drop is None:
            break
        t._remove(drop, "made room for your bots")
    total = passes * (cut - WARM)
    done = 0
    for p_ in range(passes):
        if p_:
            _fresh_money(t)
        for i in range(WARM, cut):
            t.step({c: View(train[c], i + 1) for c in coins}, slice_extra(tx, i + 1))
            done += 1
            if done % 100 == 0:
                if stop and stop():
                    raise TrainingStopped()
                if progress:
                    best = t.active[0] if t.active else None
                    progress(done, total, f"pass {p_ + 1} of {passes}, generation {max((v.gen for v in t.vars), default=0)}, "
                                          f"{len(t.vars)} bots" + (f", leading: {best.name}" if best else ""))
    def rank_key(v):  # bots that actually traded first, then by how far they beat holding
        lf = t.life(v)
        return (lf["trades"] < 3, -(lf["edge"] if lf["edge"] is not None else lf["ret"]))
    ranked = sorted(t.vars, key=rank_key)
    results = []
    exam_hold = (sum(closes[c][L - 1] / closes[c][cut - 1] for c in coins) / len(coins) - 1) if cut < L else None
    seen, picked = set(), []
    for v in ranked:  # identical twins count once
        if v.key() not in seen:
            seen.add(v.key())
            picked.append(v)
    for v in picked[:max(12, cap // 3)]:
        lf = t.life(v)
        r = dict(v=v, genes=_genes(v), train_ret=lf["ret"], train_edge=lf["edge"], trades=lf["trades"], winrate=lf["winrate"],
                 closed=lf["wins"] + lf["losses"], wins=lf["wins"], best_trade=getattr(v, "best_tr", None),
                 worst_trade=getattr(v, "worst_tr", None),
                 exam_ret=None, exam_edge=None, passed=None, layer_stats={k: list(x) for k, x in v.layer_stats.items()},
                 veto_stats={k: list(x) for k, x in v.veto_stats.items()})
        if cut < L:
            er = backtest_simple(r["genes"], closes, extra, cut, L, start_cash, fee, pct, t.o.get("slip", 0.0))
            r.update(exam_ret=er, exam_edge=er - exam_hold,
                     passed=er - exam_hold > 0 and (lf["edge"] or 0) > 0 and lf["trades"] >= 3)
        results.append(r)
        if stop and stop():
            break
    results.sort(key=lambda r: (not r["passed"], -(r["exam_edge"] if r["exam_edge"] is not None else r["train_edge"] or 0)))
    return dict(tourn=t, results=results, train_bars=cut, exam_bars=L - cut, passes_done=passes, exam_hold=exam_hold)


INSTINCT_ALPHA = 0.1   # how fast an instinct's record follows new reviews (about the last 10 count most)
INSTINCT_MIN = 3       # reviews before an instinct's record is shown
INSTINCT_PROVEN = 10   # reviews before a positive record counts as proven


def teach_from(dst, src):
    """Passes a trained farm's memory to another farm: elders, post-trade review, mutation memory and season champions."""
    dst.elders = (dst.elders + [e for e in src.elders])[-40:]
    for k, x in src.review.items():
        d = dst.review.setdefault(k, [0, 0])
        d[0] += x[0]
        d[1] += x[1]
    for k, x in src.mut_stats.items():
        d = dst.mut_stats.setdefault(k, [0, 0])
        d[0] += x[0]
        d[1] += x[1]
    for k, x in (getattr(src, "instincts", None) or {}).items():  # what it learned about instincts, at half weight
        d = dst.instincts.get(k)
        if d is None:
            dst.instincts[k] = [x[0], max(1, x[1] // 2), x[2]]
        else:
            d[0] += 0.5 * x[1] / (x[1] + d[1]) * (x[0] - d[0])
            d[1] += max(1, x[1] // 4)
        if k in getattr(src, "instinct_best", {}) and (k not in dst.instinct_best or x[2] > (d or [0, 0, -9])[2]):
            dst.instinct_best[k] = src.instinct_best[k]
            if d is not None:
                d[2] = max(d[2], x[2])
    for m, ch in src.champions.items():
        if ch.get("fit", 0) > dst.champions.get(m, {}).get("fit", 0):
            dst.champions[m] = dict(ch, uid=-1)  # not on this farm: woken up from its genes when its season comes


def _warm_island(args):
    """One island's warmup in its own process (multi-core). Returns its farm as plain data."""
    (coins, start_cash, fee, pct, evolve, params, opts, cap, retire_steps, hist, hx, seed, island) = args
    t = Tournament(coins, start_cash, fee, pct, evolve, params=params, opts=dict(opts, islands=1), cap=cap,
                   retire_steps=retire_steps, rng=random.Random(seed))
    t.warmup(hist, extra=hx)
    st = t.to_state()
    for b in st["bots"]:
        b["island"] = island
    return st


def warm_islands(coins, start_cash, fee, pct, evolve, params, opts, cap, retire_steps, hist, hx, workers=None, log=None):
    """Multi-core warmup: each island learns on its own processor core, then the islands join into one farm (they
    keep their island and only swap champions from then on). Falls back to one core if anything goes wrong."""
    k = max(1, int(opts.get("islands", 1) or 1))
    cpus = os.cpu_count() or 1
    if k < 2 or cpus < 2:
        return None
    per = max(6, -(-cap // k))
    jobs = [(coins, start_cash, fee, pct, evolve, params, opts, per, retire_steps, hist, hx, 1000 + i, i) for i in range(k)]
    try:
        import concurrent.futures as cf
        import multiprocessing as mp
        # "spawn" everywhere: forking a busy app (with threads running) can leave the helpers stuck on Linux
        ctx = mp.get_context("spawn")
        ex = cf.ProcessPoolExecutor(max_workers=min(k, workers or cpus), mp_context=ctx)
        try:
            states = list(ex.map(_warm_island, jobs, timeout=900))
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
    except Exception as e:  # e.g. a locked-down computer: warm up on one core instead
        if log:
            log(f"Multi-core warmup unavailable ({e}); using one core.")
        return None
    t = Tournament(coins, start_cash, fee, pct, evolve, params=params, opts=opts, cap=cap, retire_steps=retire_steps)
    merged = dict(states[0])
    bots, lineage, off = [], {}, 0
    for st in states:
        top = max([b["uid"] for b in st["bots"]] + [int(u) for u in st.get("lineage", {})] + [0])
        for b in st["bots"]:
            b = dict(b, uid=b["uid"] + off, parents=[p + off for p in b.get("parents", [])])
            bots.append(b)
        for u, rec in st.get("lineage", {}).items():
            lineage[str(int(u) + off)] = dict(rec, uid=int(u) + off, parents=[p + off for p in rec.get("parents", [])])
        off += top
    merged.update(bots=bots, lineage=lineage, serial=off, n=max(st["n"] for st in states),
                  elders=[e for st in states for e in st.get("elders", [])][-40:])
    rv, ms = {}, {}
    for st in states:
        _merge_stats(rv, st.get("review"))
        _merge_stats(ms, st.get("mut_stats"))
        for m, ch in (st.get("champions") or {}).items():
            if ch["fit"] > merged.get("champions", {}).get(m, {}).get("fit", -1):
                merged.setdefault("champions", {})[m] = ch
    merged.update(review=rv, mut_stats=ms)
    if not t.restore(merged):
        return None
    t.cap = cap
    while len(t.vars) > cap:
        worst = [v for v, _ in t.weakest() if v not in t.active] or \
            [v for v in t.vars if t.info.get(id(v), {}).get("status") == "warming up"]
        if not worst:
            break
        t._remove(worst[0], "farm made smaller")
    t._rate()
    return t


# ---------------- Hindsight: which instincts and settings WOULD have worked on a stretch of the past ----------------
def _hs_run(g, closes, extra, a, z, start_cash, fee, pct, slip):
    """One candidate traded from bar a to bar z: (return, closed trades, wins, best trade %, worst trade %)."""
    coins = list(closes)
    b = _bt_bot(g, coins, start_cash, fee, pct, slip)
    closed = wins = 0
    best = worst = None
    for i in range(a, z):
        for _, t in bot_step(b, {c: View(closes[c], i + 1) for c in coins}, i, coins, slice_extra(extra, i + 1) if extra else None):
            if t["side"] == "sell" and t.get("pl") is not None:
                closed += 1
                wins += t["pl"] > 0
                paid = t["usd"] - t["pl"]
                if paid > 0:
                    pc = t["pl"] / paid * 100
                    best = pc if best is None else max(best, pc)
                    worst = pc if worst is None else min(worst, pc)
    return b.sim.equity({c: closes[c][z - 1] for c in coins}) / start_cash - 1, closed, wins, best, worst


def hindsight_study(closes, extra, start_cash, fee, pct, slip, rng, n=48, prior=None, stop=None, progress=None,
                    risk_caps=None):
    """Looks back over a stretch: many instincts and settings are tried on its first 70% (the look-back), the most
    promising are checked on the last 30% they never saw (the check). A candidate 'would have worked' when it made
    money on both, with at least 3 finished trades. prior: the farm's hindsight book (its best settings are tried again,
    tweaked, so good finds get refined). -> dict(results=[...], look_bars, check_bars)"""
    coins = list(closes)
    L = min(len(closes[c]) for c in coins)
    a0 = max(WARM, min(L - 2, max(60, LOOKBACK // 2)))
    cut = a0 + int((L - a0) * 0.7)
    if cut - a0 < 60 or L - cut < 30:
        raise NoHistory("too few candles for a hindsight study")
    caps = dict(risk_caps or FAST_RISK)
    cands = []
    for e in (prior or {}).values():  # refine what worked before
        for b in (e.get("best") or [])[:2]:
            g = json.loads(json.dumps(b["genes"]))
            if g.get("name") in STRATS:
                cands.append(g)
    names = list(WILDCARD_STRATS)
    while len(cands) < n:
        nm = rng.choice(names)
        spec, p = STRATS[nm], {}
        for k, dv in spec["params"].items():
            lo, hi = spec["bounds"][k]
            p[k] = int(round(rng.uniform(lo, hi))) if isinstance(dv, int) else round(rng.uniform(lo, hi), 2)
        if len(cands) % 4 == 0:
            p = dict(spec["params"])  # the instinct's usual settings, too
        p = fix_params(nm, p)
        risk = {k: (round(rng.uniform(lo, caps.get(k, hi) * 0.5), 2) if rng.random() < 0.5 else 0.0)
                for k, (lo, hi) in RISK_BOUNDS.items()}
        cands.append(dict(name=nm, params=p, tf=rng.choice((1, 2, 3, 5, 10)), risk=risk, traits={}, vetoes={}, layers={}))
    cands = cands[:n]
    looked = []
    for i, g in enumerate(cands):
        if stop and stop():
            raise TrainingStopped()
        ret, closed, wins, best, worst = _hs_run(g, closes, extra, a0, cut, start_cash, fee, pct, slip)
        looked.append((g, ret, closed, wins, best, worst))
        if progress:
            progress(i + 1, len(cands) + 12)
    top = sorted([x for x in looked if x[2] >= 3], key=lambda x: -x[1])[:12]
    out = []
    for j, (g, ret, closed, wins, best, worst) in enumerate(top):
        if stop and stop():
            raise TrainingStopped()
        cret, cclosed, cwins, cbest, cworst = _hs_run(g, closes, extra, cut, L, start_cash, fee, pct, slip)
        out.append(dict(genes=g, look_ret=ret, check_ret=cret, closed=closed + cclosed, wins=wins + cwins,
                        best=max(x for x in (best, cbest) if x is not None) if (best is not None or cbest is not None) else None,
                        worst=min(x for x in (worst, cworst) if x is not None) if (worst is not None or cworst is not None) else None,
                        ok=ret > 0 and cret > 0 and closed >= 3))
        if progress:
            progress(len(cands) + j + 1, len(cands) + 12)
    out.sort(key=lambda r: (not r["ok"], -r["check_ret"]))
    return dict(results=out, tried=len(cands), look_bars=cut - a0, check_bars=L - cut, at=time.time())


# ---------------- Farm knowledge: everything a farm has learned, as items you can save, view and add elsewhere ----------------
KNOW_KINDS = ("Never-rule", "Study", "Instinct", "Volatility gate", "Season champion", "Elder", "Mutation lesson",
              "Proven bot", "Hindsight", "Ticker habit")
KNOW_FORMAT = "sbf-knowledge"


def _med(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def farm_knowledge(t):
    """Everything this farm has learned, as a list of items: {id, kind, title, detail, evidence, score, strong, data}.
    score is how much the item is worth by its own evidence (%), strong = proven by enough evidence to trust."""
    items = []
    ranked = [v for v, sc in t.ranking() if sc is not None]
    sc_of = {id(v): t.score(v) for v in t.vars}

    def best_with(pred):
        return next((v for v in ranked if pred(v)), None)
    # never-rules: the evidence of every bot that carries (or carried) them
    agg = {}
    for v in t.vars:
        for k, st in (v.veto_stats or {}).items():
            a = agg.setdefault(k, dict(n=0.0, saved=0.0, vals=[], bots=0))
            a["n"] += st[0]
            a["saved"] += st[1]
        for k, x in (v.vetoes or {}).items():
            a = agg.setdefault(k, dict(n=0.0, saved=0.0, vals=[], bots=0))
            a["vals"].append(x)
            a["bots"] += 1
    for k, a in agg.items():
        if k not in VETOES or (a["n"] < 1 and not a["bots"]):
            continue
        avg = a["saved"] / a["n"] if a["n"] else 0.0
        x = _med(a["vals"]) or (VETOES[k]["lo"] + VETOES[k]["hi"]) / 2
        rv = t.review.get(k, [0, 0])
        items.append(dict(id=f"rule:{k}", kind="Never-rule", title=VETOES[k]["name"], detail=VETOES[k]["text"](x),
                          evidence=f"{a['n']:.0f} trades blocked, {avg:+.2f}% saved each; {a['bots']} bots carry it"
                                   + (f"; would have stopped {rv[0]} losing buys" if rv[0] else ""),
                          score=round(avg, 3), strong=a["n"] >= 5 and avg > 0,
                          data=dict(key=k, value=x, n=a["n"], saved=a["saved"], review=list(rv))))
    # studies (extra reasons to buy or not, bigger trend, market leader, context tickers, volatility gate)
    names = {"plus": "Buy studies (extra reasons to buy)", "minus": "No-buy studies (reasons not to buy)",
             "confirm": "Confirmation", "htf": "Bigger-trend study", "lead": "Follow the market leader",
             "vix": "Volatility gate (scalpers)", "coin": "Skipping tickers it's bad at"}
    lagg = {}
    for v in t.vars:
        for k, st in (v.layer_stats or {}).items():
            a = lagg.setdefault(k, [0.0, 0.0])
            a[0] += st[0]
            a[1] += st[1]
    for k, (n, saved) in lagg.items():
        if n < 1:
            continue
        avg = saved / n
        title = names.get(k) or (f"Watching {k[4:]}" if k.startswith("ctx:") else k)
        carrier = (best_with(lambda v, k=k: k in (v.layer_stats or {}) and (sc_of.get(id(v)) or 0) > 0)
                   or best_with(lambda v, k=k: k in (v.layer_stats or {}))
                   or next((v for v in t.vars if k in (v.layer_stats or {})), None))
        items.append(dict(id=f"study:{k}", kind="Volatility gate" if k == "vix" else "Study", title=title,
                          detail="graded every time it changed a decision", evidence=f"{n:.0f} decisions, {avg:+.2f}% each",
                          score=round(avg, 3), strong=n >= 5 and avg > 0,
                          data=dict(key=k, n=n, saved=saved, genes=_genes(carrier) if carrier else None)))
    # instincts: each one's lasting record on this farm (it builds up over time), plus how its bots are doing now
    by = {}
    for v in t.vars:
        by.setdefault(v.name, []).append(v)
    rec = getattr(t, "instincts", None) or {}
    for nm in list(by) + [n for n in rec if n not in by and n in STRATS]:
        vs = by.get(nm, [])
        scored = [(sc_of.get(id(v)), v) for v in vs if sc_of.get(id(v)) is not None]
        best = max(scored, key=lambda x: x[0]) if scored else ((None, vs[0]) if vs else (None, None))
        win = sum(1 for v in vs if t.info.get(id(v), {}).get("status") == "active")
        r = rec.get(nm)
        now = f"{len(vs)} bots, {win} winning" if vs else "no bots on the farm now"
        if r and r[1] >= INSTINCT_MIN:
            evidence = f"record {r[0] * 100:+.2f}% vs holding over {r[1]} reviews; {now}"
            score = r[0] * 100
        else:
            evidence = now + (f"; best {best[0] * 100:+.1f}%" if best[0] is not None else "") + \
                (f" (record starts after {INSTINCT_MIN} reviews)" if vs else "")
            score = (best[0] or 0) * 100
        genes = _genes(best[1]) if best[1] is not None else (getattr(t, "instinct_best", {}) or {}).get(nm)
        items.append(dict(id=f"instinct:{nm}", kind="Instinct", title=nm + (" (scalper)" if is_scalp(nm) else ""),
                          detail=(f"best: {best[1].label}" if best[1] is not None else "best bot it had is kept in the book")[:160],
                          evidence=evidence, score=round(score, 3),
                          strong=bool(win) or (t.instinct_proven(nm) if hasattr(t, "instinct_proven") else False),
                          data=dict(name=nm, genes=genes)))
    # scalpers' volatility gates: which gate the winning scalpers use
    gates = {}
    for v in t.vars:
        if is_scalp(v.name):
            g = int(v.traits.get("vixmode", 5) or 5)
            a = gates.setdefault(g, [0, 0, None])
            a[0] += 1
            a[1] += t.info.get(id(v), {}).get("status") == "active"
            if (sc_of.get(id(v)) or -9) > ((sc_of.get(id(a[2])) or -9) if a[2] else -10):
                a[2] = v
    for g, (n, win, v) in gates.items():
        items.append(dict(id=f"gate:{g}", kind="Volatility gate", title=VIX_MODES.get(g, ""), detail="scalpers' choice",
                          evidence=f"{n} scalpers, {win} winning", score=float(win), strong=win > 0,
                          data=dict(mode=g, genes=_genes(v) if v else None)))
    # season champions, elders, mutation memory
    for m, ch in (t.champions or {}).items():
        items.append(dict(id=f"season:{m}", kind="Season champion", title=f"Champion of '{m}' markets",
                          detail=(ch.get("label") or ch.get("name", ""))[:160], evidence=f"gained {ch.get('fit', 0) * 100:+.3f}% a decision",
                          score=round(ch.get("fit", 0) * 100, 4), strong=True, data=dict(mood=m, champion=ch)))
    for i, e in enumerate(sorted(t.elders or [], key=lambda e: -(e.get("edge") or 0))[:12]):
        items.append(dict(id=f"elder:{e['name']}:{i}", kind="Elder", title=f"Elder {e['name']}",
                          detail=", ".join(f"{k}={x}" for k, x in (e.get("params") or {}).items())[:160],
                          evidence=f"edge {(e.get('edge') or 0) * 100:+.1f}%", score=round((e.get("edge") or 0) * 100, 3),
                          strong=(e.get("edge") or 0) > 0, data=dict(elder=e)))
    tb = sum(x[0] for x in (t.mut_stats or {}).values()) or 1
    tw = sum(x[1] for x in (t.mut_stats or {}).values())
    for k, (born, won) in (t.mut_stats or {}).items():
        if born < 3:
            continue
        rate, base = won / born, tw / tb
        items.append(dict(id=f"mut:{k}", kind="Mutation lesson", title=f"Changing its {k}",
                          detail=f"makes winners {rate / base:.1f}x as often as other changes" if base else "",
                          evidence=f"{born} children, {won} became winners", score=round((rate - base) * 100, 2),
                          strong=born >= 8, data=dict(key=k, stats=[born, won])))
    # the farm's best bots, and the tickers it has learned to skip
    for v in ranked[:8]:
        if (sc_of.get(id(v)) or 0) <= 0:
            continue
        items.append(dict(id=f"bot:{bot_nickname(v.key())}", kind="Proven bot", title=bot_nickname(v.key()),
                          detail=v.label[:160], evidence=f"{sc_of[id(v)] * 100:+.1f}% recently; {v.wins} wins, {v.losses} losses",
                          score=round(sc_of[id(v)] * 100, 3), strong=t.info.get(id(v), {}).get("status") == "active",
                          data=dict(genes=_genes(v))))
    for nm, e in (getattr(t, "hindsight", {}) or {}).items():  # what would have worked
        if not e.get("best"):
            continue
        b0 = e["best"][0]
        items.append(dict(id=f"hindsight:{nm}", kind="Hindsight", title=f"{nm} would have worked",
                          detail=", ".join(f"{k}={x}" for k, x in b0["genes"]["params"].items())[:160],
                          evidence=f"worked in {e['wins']:.0f} of {e['tries']:.0f} looks back; best {b0['score'] * 100:+.1f}% on unseen prices",
                          score=round(b0["score"] * 100, 3), strong=e["wins"] >= 2, data=dict(genes=b0["genes"])))
    for k, (n_, saved) in (getattr(t, "trial_book", {}) or {}).items():  # what the kamikazes found out
        if n_ < 3:
            continue
        kind_, key_ = k.split(":", 1)
        title = (VETOES[key_]["name"] if kind_ == "rule" and key_ in VETOES else key_) + " (tested by kamikazes)"
        items.append(dict(id=f"trial:{k}", kind="Never-rule" if kind_ == "rule" else "Study", title=title,
                          detail="tried first by throwaway kamikaze bots", evidence=f"{n_:.0f} decisions, {saved / n_:+.2f}% each",
                          score=round(saved / n_, 3), strong=n_ >= 5 and saved > 0, data=dict(key=key_, value=(VETOES[key_]["lo"] + VETOES[key_]["hi"]) / 2
                                                                                              if key_ in VETOES else 0, n=n_, saved=saved)))
    bad = {}
    for v in t.vars:
        for c, x in (v.coin_pl or {}).items():
            if x[0] >= 3 and x[1] < -1.5:
                bad[c] = bad.get(c, 0) + 1
    for c, n in bad.items():
        items.append(dict(id=f"ticker:{c}", kind="Ticker habit", title=f"Hard to trade: {c}", detail="bots skip it after repeated losses",
                          evidence=f"{n} bots learned this", score=-float(n), strong=n >= 3, data=dict(ticker=c, info_only=True)))
    order = {k: i for i, k in enumerate(KNOW_KINDS)}
    items.sort(key=lambda x: (order.get(x["kind"], 99), -x["score"]))
    return items


def knowledge_levels(items):
    """Ability levels for the knowledge screen: kind -> (items, strong items, level 0..10)."""
    out = {}
    for k in KNOW_KINDS:
        xs = [x for x in items if x["kind"] == k]
        strong = sum(1 for x in xs if x.get("strong"))
        out[k] = (len(xs), strong, min(10, round(strong * 10 / max(4, len(xs))) + (1 if strong else 0)))
    return out


def apply_knowledge(t, items, closes=None):
    """Adds the chosen knowledge to farm t. -> list of plain-English notes, one per item."""
    notes = []
    for it in items:
        k, d = it["kind"], it.get("data") or {}
        try:
            if k == "Never-rule":
                rv = t.review.setdefault(d["key"], [0, 0])
                if d.get("saved", 0) > 0:  # new eggs are more likely to grow this rule
                    rv[0] += max(3, int(d.get("n", 0) // 2))
                g = None
                best = next((v for v, sc in t.ranking() if (sc or 0) > 0 and d["key"] not in v.vetoes
                             and len(v.vetoes) < MAX_VETOES and t.rule_allowed(d["key"])), None)
                if best:  # and one egg of your best bot hatches with it
                    g = _genes(best)
                    g["vetoes"] = dict(g["vetoes"], **{d["key"]: d["value"]})
                    _know_hatch(t, g, closes, "Knowledge egg (never-rule):")
                notes.append(f"{it['title']}: new eggs favour it" + (", and an egg of your best bot carries it" if g else ""))
            elif k in ("Study", "Instinct", "Proven bot", "Volatility gate", "Hindsight"):
                g = d.get("genes")
                if not g:
                    notes.append(f"{it['title']}: nothing to hatch (no bot carries it)")
                elif is_scalp(g["name"]) and not (t.scalpers() or t.scalp_target()):
                    notes.append(f"{it['title']}: skipped (this farm has no scalpers)")
                else:
                    n0 = len(t.vars)
                    keys0 = {x.key() for x in t.vars}
                    v = _know_hatch(t, g, closes, f"Knowledge egg ({k.lower()}):")
                    notes.append(f"{it['title']}: couldn't hatch" if not v else
                                 f"{it['title']}: {bot_nickname(v.key())} already carries it here" if v.key() in keys0 and len(t.vars) <= n0
                                 else f"{it['title']}: hatched {bot_nickname(v.key())}")
            elif k == "Season champion":
                ch, m = d["champion"], d["mood"]
                if ch.get("fit", 0) > (t.champions.get(m) or {}).get("fit", -1e9):
                    t.champions[m] = dict(ch, uid=-1)
                    notes.append(f"{it['title']}: now this farm's champion for that season")
                else:
                    notes.append(f"{it['title']}: this farm's own champion is stronger, kept it")
            elif k == "Elder":
                e = d["elder"]
                if not any(x.get("name") == e["name"] and x.get("params") == e.get("params") for x in t.elders):
                    t.elders.append(dict(e))
                    del t.elders[:-40]
                notes.append(f"{it['title']}: nudges this farm's future eggs")
            elif k == "Mutation lesson":
                st = t.mut_stats.setdefault(d["key"], [0, 0])
                st[0] += d["stats"][0]
                st[1] += d["stats"][1]
                notes.append(f"{it['title']}: mutation memory updated")
            else:
                notes.append(f"{it['title']}: for reference only")
        except Exception as e:
            notes.append(f"{it['title']}: couldn't add ({e})")
    t._rate()
    return notes


def _know_hatch(t, g, closes, note):
    key = bot_key(g)
    have = next((v for v in t.vars if v.key() == key), None)
    if have is not None:  # already on this farm (another item pointed at the same bot)
        return have
    if len(t.vars) >= t.cap:
        cands = [v for v, _ in t.weakest() if v not in t.active] or [v for v in t.vars if v not in t.active]
        if cands:
            t._remove(cands[0], "made room for a knowledge egg")
    return t.add_variant(g["name"], g["params"], closes, 1.0, note=note, tf=g.get("tf", 1), origin="knowledge",
                         risk=g.get("risk"), traits=g.get("traits"), vetoes=g.get("vetoes"), layers=g.get("layers"))


def knowledge_dir():
    return os.path.join(HOME_DIR, "knowledge")


def save_knowledge(name, items, source=""):
    os.makedirs(knowledge_dir(), exist_ok=True)
    safe = re.sub(r"[^\w .-]", "", name).strip() or "knowledge"
    path = os.path.join(knowledge_dir(), f"{safe}.json")
    save_json(path, dict(format=KNOW_FORMAT, version=1, name=name, source=source, saved=time.time(), items=items))
    return path


def list_knowledge():
    """Saved knowledge books, newest first: [dict(name, source, saved, n, path)]."""
    out = []
    try:
        for f in os.listdir(knowledge_dir()):
            if f.endswith(".json"):
                d = load_json(os.path.join(knowledge_dir(), f), {}) or {}
                if d.get("format") == KNOW_FORMAT:
                    out.append(dict(name=d.get("name", f[:-5]), source=d.get("source", ""), saved=d.get("saved", 0),
                                    n=len(d.get("items") or []), path=os.path.join(knowledge_dir(), f)))
    except OSError:
        pass
    return sorted(out, key=lambda x: -x["saved"])


def load_knowledge(path):
    d = load_json(path, {}) or {}
    return list(d.get("items") or []) if d.get("format") == KNOW_FORMAT else []


# ---------------- Scalp scan: the scalpers roam the market during the trading day ----------------
SCAN_CASH = 100000.0  # pretend money each scalper gets on each ticker it's tried on (big enough for one contract)


SCAN_SLIP = [0.0003]  # half the bid/ask gap a scan's share fills pay (the app passes its own Practice slippage)


def _scan_one(g, sym, bars, day_start, ctx=None, feed=None, book=None):
    """Replays one scalper (plain genes) over one ticker's 1-minute bars. Only trades from day_start on count;
    earlier bars just warm up its indicators. ctx: {'VIXY': closes, 'SPY': closes} lined up with this ticker's bars, so
    its volatility gate and market studies see what they would have seen. -> dict of its day there."""
    v = Variant(g["name"], dict(g["params"]), make_sim(SCAN_CASH, 0.0, 0.05, slip=SCAN_SLIP[0], genes=g.get("traits")), 2,
                g.get("tf", 1), g.get("risk"), g.get("traits"), g.get("vetoes"), g.get("layers"))
    opt = isinstance(v.sim, OptionSim)
    if opt:
        v.sim.genes = v.traits
        if book:
            v.sim.book = {sym: book}
    c, L = bars["c"], len(bars["c"])
    ctx = ctx or {}
    out = dict(trades=0, wins=0, pl=0.0, paid=0.0, now=None, open=None, best=None, worst=None)
    for i in range(max(WARM, 1), L):
        x = {sym: {k: View(bars[k], i + 1) for k in ("h", "l", "v", "t")}}
        cl = {sym: View(c, i + 1)}
        if ctx:
            x[CTX_KEY] = {k: View(q, i + 1) for k, q in ctx.items()}
            if "SPY" in ctx and sym != "SPY":
                cl["SPY"] = View(ctx["SPY"], i + 1)  # the market leader, for "follow the leader" studies
        made = bot_step(v, cl, i, [sym], x)
        if i < day_start:
            continue
        for _, t in made:
            if feed is not None and len(feed) < 600:  # for Free roam's live trade list
                feed.append(dict(t=bars["t"][i], who=g["name"], uid=g.get("uid", 0), c=sym, side=t["side"],
                                 contract=t.get("contract"), price=t.get("price"), pl=t.get("pl"), usd=t.get("usd")))
            if t["side"] == "sell" and t.get("pl") is not None:
                out["trades"] += 1
                out["wins"] += t["pl"] > 0
                out["pl"] += t["pl"]
                out["paid"] += t["usd"] - t["pl"]
                if t["usd"] - t["pl"] > 0:
                    pc_ = t["pl"] / (t["usd"] - t["pl"]) * 100
                    out["best"] = pc_ if out["best"] is None else max(out["best"], pc_)
                    out["worst"] = pc_ if out["worst"] is None else min(out["worst"], pc_)
            elif t["side"] == "buy" and i == L - 1:  # a setup firing on the newest bar: it's happening right now
                out["now"] = dict(kind=(t.get("contract") or {}).get("type") or t["side"], price=c[-1], why=v.why.get(sym, ""))
    q = v.sim.pos.get(sym, 0)
    if q > 0:  # still in a scalp: count it at today's value
        o = (v.sim.opt.get(sym) or {}) if opt else {}
        worth = v.sim.value_of(sym, c[-1]) if opt else q * c[-1]
        out["open"] = dict(kind=o.get("type", ""), pl=worth - v.sim.cost.get(sym, 0), paid=v.sim.cost.get(sym, 0))
    out["real"], out["est"] = getattr(v.sim, "real_fills", 0), getattr(v.sim, "est_fills", 0)
    return out


def scan_scalps(job):
    """job: {'bars': {ticker: {c,h,l,v,t}}, 'genes': [{uid, name, params, ...}], 'day': New York date 'YYYY-MM-DD',
    'spread': {ticker: [a, b, ..]}, 'sspread': %}. Every scalper is tried on every ticker over today's session so far.
    Runs in its own process. -> {'tickers': {ticker: summary}, 'bots': {uid: [trades, wins, pl, paid]}, 'now': [...]}"""
    SPREAD_FIT.update(job.get("spread") or {})
    _SPREAD_LOADED[0] = True
    if job.get("slip") is not None:
        SCAN_SLIP[0] = float(job["slip"])
    IV_HINT.update(job.get("iv") or {})
    ctx_bars = job.get("ctx") or {}
    books = job.get("opt") or {}
    if job.get("sspread"):
        SCALP_SPREAD[0] = float(job["sspread"])
    day0 = ny_time(job["day"], 9, 30)
    res = dict(tickers={}, bots={}, now=[], feed=[])
    feed = res["feed"] if job.get("feed") else None
    for sym, bars in job["bars"].items():
        if len(bars.get("c") or ()) < WARM + 5:
            continue
        start = next((i for i, t in enumerate(bars["t"]) if t >= day0), len(bars["t"]))
        if start >= len(bars["t"]) - 1:
            continue
        tk_ = dict(trades=0, wins=0, pl=0.0, paid=0.0, best=None, best_pl=None, setups=0, real=0, est=0)
        ctx = {k: align_ctx(bars["t"], cb["t"], cb["c"]) for k, cb in ctx_bars.items() if cb.get("t")}
        tk_["regime"] = vol_regime({sym: bars["c"]}, {CTX_KEY: ctx}, sym)[0] if ctx else ""
        for g in job["genes"]:
            try:
                r = _scan_one(g, sym, bars, start, ctx, feed, books.get(sym))
            except Exception:
                continue
            pl = r["pl"] + (r["open"]["pl"] if r["open"] else 0.0)
            paid = r["paid"] + (r["open"]["paid"] if r["open"] else 0.0)
            tk_["trades"] += r["trades"]
            tk_["wins"] += r["wins"]
            tk_["real"] += r.get("real", 0)
            tk_["est"] += r.get("est", 0)
            tk_["pl"] += pl
            tk_["paid"] += paid
            tk_["setups"] += r["trades"] + (1 if r["open"] else 0)
            b = res["bots"].setdefault(g.get("uid", 0), [0, 0, 0.0, 0.0, None, None])  # trades, wins, P/L, paid, best %, worst %
            b[0] += r["trades"]
            b[1] += r["wins"]
            b[2] += r["pl"]
            b[3] += r["paid"]
            if r["best"] is not None:
                b[4] = r["best"] if b[4] is None else max(b[4], r["best"])
                b[5] = r["worst"] if b[5] is None else min(b[5], r["worst"])
            if r["trades"] and (tk_["best_pl"] is None or r["pl"] > tk_["best_pl"]):
                tk_["best"], tk_["best_pl"] = g["name"], r["pl"]
            if r["now"]:
                res["now"].append(dict(r["now"], ticker=sym, bot=g["name"], uid=g.get("uid", 0)))
        o, c = bars["c"][start - 1] if start else bars["c"][0], bars["c"][-1]
        tk_["move"] = (c / o - 1) * 100 if o else 0.0
        tk_["last"] = c
        res["tickers"][sym] = tk_
    return res


# ---------------- Bot Lab: evolve on old prices, test on newer unseen prices ----------------
# ---------------- popular tickers ----------------
# Leveraged and inverse ETFs (and ETNs): ticker -> (daily multiple, what it follows). A 3x fund moves about 3% when its
# index moves 1%; an inverse one (negative) RISES when its index falls, so a buy-only bot can still profit on a down day.
# They reset every day, so held for days they drift away from the multiple (choppy markets wear them down).
LEVERAGED = {
    "SPYU": (4, "S&P 500"), "UPRO": (3, "S&P 500"), "SPXL": (3, "S&P 500"), "SSO": (2, "S&P 500"),
    "TQQQ": (3, "Nasdaq-100"), "QLD": (2, "Nasdaq-100"), "SOXL": (3, "chip stocks"), "TNA": (3, "Russell 2000"),
    "SPXU": (-3, "S&P 500"), "SPXS": (-3, "S&P 500"), "SDS": (-2, "S&P 500"), "SH": (-1, "S&P 500"),
    "SQQQ": (-3, "Nasdaq-100"), "PSQ": (-1, "Nasdaq-100"), "SOXS": (-3, "chip stocks"), "TZA": (-3, "Russell 2000"),
}


def leverage(c):
    """How many times its index's daily move this ticker makes (1 for an ordinary stock or ETF)."""
    return abs(LEVERAGED.get(c, (1, ""))[0])


def kind_text(c):
    """'4x S&P 500', '-3x Nasdaq-100 (inverse)', or '' for an ordinary stock."""
    x = LEVERAGED.get(c)
    if not x:
        return ""
    m, what = x
    return f"{m:+d}x {what}".lstrip("+") + (" (inverse)" if m < 0 else "")


POPULAR = ["SPY", "QQQ", "IWM", "DIA", "AAPL", "NVDA", "TSLA", "AMD", "MSFT", "AMZN", "META", "GOOGL", "NFLX", "PLTR",
           "COIN", "MSTR", "SMCI", "AVGO", "BA", "JPM", "BAC", "XOM", "INTC", "UBER", "SOFI", "RIVN", "GME", "SNOW",
           "SHOP", "DIS", "NIO", "F", "GLD", "SLV", "TLT", "XLF", "XLE", "ARKK"] + list(LEVERAGED)


def list_products(quote="USD", symbols=None):
    """[{coin, price, change_24h, volume_usd}] for popular optionable tickers (or the ones given), busiest first."""
    syms = list(symbols or POPULAR)
    out = []
    for k in range(0, len(syms), 100):
        q = urllib.parse.quote(",".join(syms[k:k + 100]))
        d = alp("GET", f"{DATA_URL}/v2/stocks/snapshots?symbols={q}&feed={FEED}")
        for sym, sn in (d or {}).items():
            try:
                px = float((sn.get("latestTrade") or {}).get("p") or (sn.get("dailyBar") or {}).get("c") or 0)
                prev = float((sn.get("prevDailyBar") or {}).get("c") or 0)
                vol = float((sn.get("dailyBar") or {}).get("v") or 0)
                out.append(dict(coin=sym, price=px, change_24h=(px / prev - 1) * 100 if prev else None, volume_usd=vol * px,
                                kind=kind_text(sym)))
            except (TypeError, ValueError, AttributeError):
                continue
    return sorted(out, key=lambda r: -(r["volume_usd"] or 0))


INDEX_ETFS = ("SPY", "QQQ", "IWM", "DIA")


def scout_score(d, trade_size=None):
    """How good a ticker is for this farm today -> (score, plain-English why). Busy (volume: a busy stock has a tight
    bid/ask gap, so the bots lose little getting in and out) and moving (today's move counts, but only up to 3x, so a
    quiet but busy ETF still competes). Every stock is affordable: the bots buy fractional shares when one share costs
    more than a trade."""
    S = float(d.get("price") or 0)
    if S <= 0:
        return 0.0, "no price"
    liq = (d.get("volume_usd") or 0) ** 0.5
    chg = abs(d.get("change_24h") or 0)
    move = 1 + min(chg, 8) / 4
    vol_m = (d.get("volume_usd") or 0) / 1e6
    why = f"moved {chg:.1f}% today, ${vol_m:,.0f}M traded"
    if trade_size and S > trade_size:
        why += f", one share (${S:,.0f}) is more than a trade, so the bots buy part of one"
    return liq * move, why


def scout_coins(products, n, min_volume=5_000_000, trade_size=None, why=None):
    """The bot's own ticker pick for a new session: the best by scout_score, with one place kept for the best index
    ETF (SPY, QQQ, IWM, DIA) when picking 3 or more, so the farm always has a steady, liquid market to learn on.
    Leveraged and inverse ETFs move several times more, so they'd win every pick: at most half the picks are those.
    why (a dict) gets each pick's reason."""
    pool = [d for d in products if d["coin"] not in STABLES and (d["volume_usd"] or 0) >= min_volume
            and d["change_24h"] is not None]
    scored = []
    for d in pool:
        sc, w = scout_score(d, trade_size)
        if sc > 0:
            if d["coin"] in LEVERAGED:
                w += f", {kind_text(d['coin'])}"
            scored.append((sc, d["coin"], w))
    scored.sort(reverse=True)
    picks, lev = [], 0
    for _, c, _ in scored:
        if len(picks) >= n:
            break
        if c in LEVERAGED:
            if lev >= max(1, n // 2) if n >= 2 else True:
                continue
            lev += 1
        picks.append(c)
    if n >= 3 and not any(c in INDEX_ETFS for c in picks):
        etf = next(((sc, c) for sc, c, _ in scored if c in INDEX_ETFS), None)
        if etf:
            picks = picks[:n - 1] + [etf[1]]
    if why is not None:
        why.update({c: w for _, c, w in scored if c in picks})
    return picks


# ---------------- alerts (Discord / Telegram) ----------------
def send_alert(settings, text):
    """Posts a short message to a Discord webhook and/or a Telegram chat. Returns a list of problems."""
    problems = []
    hook = (settings.get("alert_discord") or "").strip()
    if hook:
        if not re.match(r"^https://(discord\.com|discordapp\.com|ptb\.discord\.com|canary\.discord\.com)/api/webhooks/", hook):
            problems.append("Discord: that is not a Discord webhook link.")
        else:
            try:
                req = urllib.request.Request(hook, data=json.dumps({"content": text[:1900]}).encode(),
                                             headers={"Content-Type": "application/json", "User-Agent": "sbf"})
                urllib.request.urlopen(req, timeout=10).read()
            except Exception as ex:
                problems.append(f"Discord: {ex}")
    tok, chat = (settings.get("alert_tg_token") or "").strip(), (settings.get("alert_tg_chat") or "").strip()
    if tok and chat:
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", tok):
            problems.append("Telegram: the bot token doesn't look right (it looks like 123456:ABC...).")
        else:
            try:
                body = urllib.parse.urlencode({"chat_id": chat, "text": text[:3500]}).encode()
                req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=body,
                                             headers={"User-Agent": "sbf"})
                urllib.request.urlopen(req, timeout=10).read()
            except Exception as ex:
                problems.append(f"Telegram: {ex}")
    return problems


def tg_updates(token, offset=None, timeout=20):
    """New Telegram messages to your bot: ([(chat id, text, sent at, private chat?, sender id)], next offset)."""
    url = f"https://api.telegram.org/bot{token.strip()}/getUpdates?timeout={int(timeout)}" + (f"&offset={offset}" if offset else "")
    data = json.loads(http_get(url, timeout + 10))
    out, nxt = [], offset
    for u in data.get("result", []):
        nxt = u["update_id"] + 1
        m = u.get("message") or {}
        if m.get("text"):
            ch = m.get("chat") or {}
            out.append((ch.get("id"), m["text"], float(m.get("date") or 0), ch.get("type") == "private",
                        (m.get("from") or {}).get("id")))
    return out, nxt


# ---------------- keep the computer awake while the bot runs ----------------
class AwakeLock:
    """Asks the operating system not to go to sleep while the bot is running (the screen may still
    turn off). A sleeping computer stops the bot and cuts its internet connection, which looks like
    the app 'disconnecting' after the idle timer runs out. Closing a laptop lid can still sleep it."""

    def __init__(self):
        self.proc, self.held = None, False

    def on(self):
        if self.held:
            return
        try:
            if sys.platform.startswith("win"):
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # CONTINUOUS | SYSTEM_REQUIRED
            elif sys.platform == "darwin" and shutil.which("caffeinate"):
                self.proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            elif shutil.which("systemd-inhibit"):
                self.proc = subprocess.Popen(["systemd-inhibit", "--what=sleep:idle", "--who=SBF",
                                              "--why=Trading bot is running", "--mode=block", "sleep", "infinity"])
            else:
                return
            self.held = True
        except Exception:
            self.proc, self.held = None, False

    def off(self):
        if not self.held:
            return
        try:
            if sys.platform.startswith("win"):
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            elif self.proc:
                self.proc.terminate()
        except Exception:
            pass
        self.proc, self.held = None, False


# ---------------- storage ----------------


def save_json(path, obj):
    if os.path.basename(path) == "config.json":
        obj = scrub_config(obj)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
        n = f.tell()
    os.replace(tmp, path)
    note_write(path, n)


def _sess_tag(m):
    """Which saved session: 'paper' (PRACTICE, inside SBF), 'alpaca' (ALPACA PAPER), 'ibkrpaper' (IBKR PAPER) or 'live'
    (real money). Old callers pass True / False for live / practice."""
    if m is True:
        return "live"
    if m in (False, None):
        return "paper"
    return str(m)


def state_path(live):
    return os.path.join(HOME_DIR, f"state-{_sess_tag(live)}.json")


def farm_path(live):
    return os.path.join(HOME_DIR, f"farm-{_sess_tag(live)}.json")


def migrate_paper_sessions(paper_login):
    """Before 1.17 an Alpaca paper session was saved as the LIVE session. If the saved login is a paper key and there's
    no ALPACA PAPER session yet, that session (and its farm) is moved to ALPACA PAPER, where it belongs."""
    if not paper_login or os.path.exists(state_path("alpaca")):
        return False
    moved = False
    for fn in (state_path, farm_path):
        try:
            if os.path.exists(fn(True)):
                os.replace(fn(True), fn("alpaca"))
                moved = True
        except OSError:
            pass
    return moved


def strategies_path():
    return os.path.join(HOME_DIR, "saved-strategies.json")


def save_strategies(lineup):
    save_json(strategies_path(), lineup)


def load_strategies():
    data = load_json(strategies_path(), [])
    if not isinstance(data, list):
        return []
    return [dict(d, risk=clean_risk(d.get("risk") if isinstance(d.get("risk"), dict) else None),
                 traits=clean_traits(d.get("traits") if isinstance(d.get("traits"), dict) else None))
            for d in data if isinstance(d, dict) and d.get("name") in STRATS and isinstance(d.get("params"), dict)]


# ---------------- sharing bots (export / import) ----------------
BOT_FORMAT = "sbf-bots"
BOT_FORMATS = ("sbf-bots", "obf-bots", "cbbf-bots")  # dragons from the options and crypto farms can be imported too


STRAT_DOCS = {  # plain-English rules for every strategy, for people and other programs reading a bot file
    "SMA crossover": ("Simple moving averages of the close over `fast` and `slow` bars.",
                      "Buy when the fast average crosses above the slow one; sell when it crosses below."),
    "EMA crossover": ("Exponential moving averages over `fast` and `slow` bars.",
                      "Buy when the fast EMA crosses above the slow EMA by at least `min_gap` %; sell on the opposite cross."),
    "MACD": ("MACD line = EMA(`fast`) - EMA(`slow`); signal line = EMA(`signal`) of the MACD line.",
             "Buy when MACD crosses above its signal line; sell when it crosses below."),
    "Momentum": ("Change in % over the last `n` bars.", "Buy when it is above +`thr` %; sell when below -`thr` %."),
    "RSI": ("Relative Strength Index over `period` bars (0-100).", "Buy when RSI < `lo`; sell when RSI > `hi`."),
    "Stochastic": ("Where the close sits in its `n`-bar high-low range of closes (0-100).",
                   "Buy when it rises back above `lo`; sell when it falls back below `hi`."),
    "Bollinger bands": ("`n`-bar average +/- `k` standard deviations.", "Buy below the lower band; sell above the upper band."),
    "Mean reversion": ("% distance of the close from its `n`-bar average.", "Buy when below -`thr` %; sell when above +`thr` %."),
    "VWAP snap-back": ("Volume-weighted average price over `n` bars (typical price = (high+low+close)/3).",
                       "Buy when the close is `thr` % below VWAP; sell when `thr` % above."),
    "Breakout": ("Highest and lowest close of the previous `n` bars.", "Buy above the high; sell below the low."),
    "Volume breakout": ("Highest high / lowest low of the previous `n` bars, and average volume.",
                        "Buy above the high when volume >= `vmult` x average; sell below the low."),
    "ATR channel": ("EMA(`n`) of the close +/- `m` x ATR(`n`) (average true range from highs and lows).",
                    "Buy above the upper line; sell below the lower line."),
    "Supertrend": ("Band at (high+low)/2 -/+ `m` x ATR(`n`) that trails the price and flips with the trend.",
                   "Buy when the trend flips up; sell when it flips down."),
    "Ichimoku cross": ("Midlines (highest high + lowest low)/2 over `fast` and `slow` bars.",
                       "Buy when the fast midline crosses above the slow one with the close above it; sell on the opposite cross."),
    "Rate of change": ("ROC = % change over `n` bars, and its `smooth`-bar average.",
                       "Buy when ROC crosses above its average while positive; sell when it crosses below while negative."),
    "MA ribbon": ("EMAs over `fast`, `mid` and `slow` bars.",
                  "Buy when they line up fast > mid > slow; sell when the fast EMA drops below the mid EMA."),
    "Trend + dip": ("SMA over `trend` bars and RSI over `period` bars.",
                    "Buy when the close is above the SMA and RSI < `lo`; sell when RSI > `hi` or the close drops below the SMA."),
    "Dollar-cost averaging": ("A counter of decisions.", "Buy every `every` decisions; never sells by itself."),
    # the scalp breed (always with a tight stop and target, a time limit, and flat before the 4 pm close)
    "Micro pullback": ("EMA(`n`) of the close, the last `pull` bars, and the previous bar's high and low.",
                       "Buy when the EMA is rising, the last `pull` closes eased back without breaking the EMA, and the "
                       "close tops the previous bar's high; sell on the mirror image."),
    "Volume burst": ("This bar's volume vs the `n`-bar average, its % move, and where it closed in its own range.",
                     "Buy when volume >= `vmult` x average, the bar rose >= `thr` % and closed in its top 30%; sell on the "
                     "mirror image."),
    "VWAP reclaim": ("Volume-weighted average price over `n` bars, and this bar's volume vs average.",
                     "Buy when the close crosses up through VWAP on volume >= `vmin` x average; sell on a cross down."),
    "Range scalp": ("Highest high / lowest low of the previous `n` bars (a box at most `maxw` % tall).",
                    "Inside a tight box only: buy a tick up within `edge` of the bottom; sell a tick down within `edge` of the top."),
    "Quick flip": ("The last `run` closes.", "Buy when all `run` closes rose and add up to >= `thr` %; sell when all fell."),
}


def describe_bot(b, bar_minutes=None):
    """Plain-English rules for one bot-file entry: what it reads, when it buys and sells, its pace, exits and habits."""
    calc, rule = STRAT_DOCS.get(b["name"], ("", ""))
    for k, v in b.get("params", {}).items():
        calc, rule = calc.replace(f"`{k}`", f"{v:g}"), rule.replace(f"`{k}`", f"{v:g}")
    pace = (f"every {fmt_bar(b.get('tf', 1) * bar_minutes)}" if bar_minutes
            else f"every {b.get('tf', 1)} bar(s) of your chosen bar length")
    exits = risk_text(b.get("risk"))
    habits = traits_text(b.get("traits"))
    nevers = vetoes_text(b.get("vetoes"))
    return (f"{b['name']}: {calc} {rule} It decides {pace}, on candles of that length."
            + (f" Own exits: {exits}." if exits else "") + (f" Habits: {habits}." if habits else "")
            + (f" Never-rules: it {nevers}." if nevers else "")
            + (f" Rule layers: {layers_text(b.get('layers') or {})}." if b.get("layers") else "")
            + " Each buy spends a fixed share of the starting money times its trade size; a sell closes the whole position.")


SCHEMA_NOTE = (
    "SBF bot file. Each item in `bots` is one trading bot. `name` is the strategy family (see `rules` for what it does), "
    "`params` its numbers, `tf` its pace in bars (multiply by `lifetime.bar_minutes` for minutes), `risk.sl` / `risk.tp` "
    "its own stop-loss / take-profit in % (0 = none), `traits` extra habits (trail = trailing stop %, minhold / maxhold = "
    "decisions, cool = decisions to wait after selling, trend = only buy above that many-bar average, minvol = minimum "
    "average move % per bar, rsicap = no buying above that RSI, confirm = signals in a row needed, entries = most buys "
    "per position, exit = 1 sells only through its own exits, moods = bitmask of allowed markets: 1 trending up, "
    "2 trending down, 4 choppy, 8 quiet), `vetoes` its never-rules (knife = no buying after that % drop over 10 "
    "decisions, spike = no buying after a one-bar jump of that %, chase = no buying that % above the 20-decision "
    "average, revenge = no re-buying a coin within that many decisions of a loss on it, thin = no buying under that "
    "x normal volume, mktdown = no buying while SPY is down that % over 10 decisions, panic = no strategy selling "
    "after a one-bar drop of that %), `layers` extra rule layers (plus = extra positive studies: each study's most recent "
    "signal is its vote, the main strategy is always one of them, and the bot buys when the number of positive studies "
    "voting buy minus the number of `minus` (negative) studies whose latest signal is sell is at least `need`; a buy "
    "is attempted when the main strategy or any positive study signals buy; exits = exit studies, any of which "
    "signalling sell closes the position instead of the main strategy's own sells; size.target = trade size scaled so "
    "target / average % move per decision, between 0.5x and 1.5x), `size` its trade-size multiplier. `stats` and `lifetime` are its results, "
    "after fees. Signals use bar closes (some families also highs, lows and volume). Results were simulated with fees "
    "and spread; past results do not predict future ones.")


def export_bots(rows, app_version="", note=""):
    """rows: tournament.board() entries to share. Returns plain data (strategy name, settings,
    trade size, and how it was doing) so someone else can load it and see how it behaves on
    their own prices. No keys, balances or trade history are included."""
    bots = []
    for r in rows:
        if r.get("stats") and "score" not in r:  # already a bot-file entry (Hall of Fame, Bot Lab)
            b = dict(name=r["name"], params=dict(r["params"]), size=round(float(r.get("size", 1.0)), 2),
                     tf=int(r.get("tf", 1)), risk=clean_risk(r.get("risk")), traits=clean_traits(r.get("traits")),
                     vetoes=clean_vetoes(r.get("vetoes")), layers=clean_layers(r.get("layers")), stats=dict(r["stats"]),
                     notes=r.get("notes", note))
            if r.get("lifetime"):
                b["lifetime"] = dict(r["lifetime"])
            for k in MEMORY_FIELDS:
                if r.get(k) is not None:
                    b[k] = json.loads(json.dumps(r[k]))
            bots.append(b)
            continue
        b = dict(
            name=r["name"], params=dict(r["params"]), size=round(float(r.get("size", 1.0)), 2), tf=int(r.get("tf", 1)),
            risk=clean_risk(r.get("risk")), traits=clean_traits(r.get("traits")), vetoes=clean_vetoes(r.get("vetoes")),
            layers=clean_layers(r.get("layers")), stats=dict(recent_gain_pct=None if r.get("score") is None else round(r["score"] * 100, 2),
                       consistency_pct=None if r.get("cons") is None else int(r["cons"] * 100),
                       trades=int(r.get("trades", 0)), status=r.get("status", "")),
            notes=note)
        lf = r.get("life")
        if lf:
            b["lifetime"] = lifetime_record(lf, r.get("bar_minutes"))
        if r.get("twin") is not None:
            b["twin_test_pct"] = round(r["twin"] * 100, 2)
        v = r.get("v")
        if v is not None and getattr(v, "backtests", None):
            b["backtests"] = json.loads(json.dumps(v.backtests))
        if r.get("layer_stats"):
            b["layer_stats"] = {k: [round(x, 3) for x in st] for k, st in r["layer_stats"].items()}
        if r.get("veto_stats"):
            b["veto_stats"] = {k: [round(x, 3) for x in st] for k, st in r["veto_stats"].items()}
        bots.append(b)
    for b in bots:
        b["rules"] = describe_bot(b, (b.get("lifetime") or {}).get("bar_minutes"))
    return dict(format=BOT_FORMAT, version=BOT_FORMAT_VERSION, app_version=app_version,
                exported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), how_to_read=SCHEMA_NOTE,
                strategies={n: dict(calculation=c, rules=r, settings=list(STRATS[n]["params"]))
                            for n, (c, r) in STRAT_DOCS.items() if n in {b["name"] for b in bots}},
                bots=bots)


# ---------------- Bot Collection (catch bots, give them names, rarities and levels, pick a team) ----------------


def collection_path():
    return os.path.join(HOME_DIR, "collection.json")


def load_collection():
    if not os.path.exists(collection_path()):
        return []
    bots, _ = read_bots_file(collection_path())
    return bots


def save_collection(bots):
    save_json(collection_path(), dict(format=BOT_FORMAT, version=BOT_FORMAT_VERSION, kind="collection",
                                      exported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                      bots=bots[:200]))


def catch(entries, source=""):
    """Adds bots to the collection, or updates ones you already have (they can level up and rank up,
    never down). Returns (new, levelled_up, [caught entries])."""
    col = load_collection()
    index = {bot_key(b): i for i, b in enumerate(col)}
    new = ups = 0
    out = []
    for e in entries:
        k = bot_key(e)
        lt = e.get("lifetime") or {}
        xp = xp_for(lt)
        r = rarity_for(lt)
        if k in index:
            old = col[index[k]]
            xp = max(xp, int(old.get("xp") or 0))
            if RARITIES.index(old.get("rarity", "Common")) > RARITIES.index(r):
                r = old.get("rarity", "Common")
            if level_for(xp) > int(old.get("level") or 1) or r != old.get("rarity"):
                ups += 1
            merged = dict(e, nickname=old.get("nickname") or bot_nickname(k), rarity=r, xp=xp, level=level_for(xp),
                          caught_at=old.get("caught_at"), source=old.get("source") or source, team=bool(old.get("team")),
                          shiny=bool(old.get("shiny")))
            for f in MEMORY_FIELDS:  # keep what it remembers (backtests, retests...) when the new copy doesn't carry it
                if f not in e and f in old:
                    merged[f] = old[f]
            col[index[k]] = merged
            out.append(merged)
        else:
            if len(col) >= 200:
                continue
            item = dict(e, nickname=bot_nickname(k), rarity=r, xp=xp, level=level_for(xp),
                        caught_at=datetime.now().isoformat(timespec="seconds"), source=source, team=False,
                        shiny=is_shiny(k))
            index[k] = len(col)
            col.append(item)
            out.append(item)
            new += 1
    save_collection(col)
    return new, ups, out


def release(keys):
    keys = set(keys)
    col = [b for b in load_collection() if bot_key(b) not in keys]
    save_collection(col)
    return col


def set_team(keys, on):
    """Adds bots to (or removes them from) your team. Returns (team size, refused because the team is full)."""
    keys, col, full = set(keys), load_collection(), 0
    size = sum(1 for b in col if b.get("team"))
    for b in col:
        if bot_key(b) in keys and bool(b.get("team")) != on:
            if on and size >= TEAM_MAX:
                full += 1
                continue
            b["team"] = on
            size += 1 if on else -1
    save_collection(col)
    return size, full


def team():
    return [b for b in load_collection() if b.get("team")]


# ---------------- Hall of Fame (long-term winners, saved automatically) ----------------
def hof_path():
    return os.path.join(HOME_DIR, "hall-of-fame.json")


def load_hof():
    """Hall of Fame entries (bot-file entries with lifetime stats), best first."""
    if not os.path.exists(hof_path()):
        return []
    bots, _ = read_bots_file(hof_path())
    return sorted(bots, key=lambda b: -((b.get("lifetime") or {}).get("vs_holding_pct") or 0))


def save_hof(bots, app_version=""):
    save_json(hof_path(), dict(format=BOT_FORMAT, version=BOT_FORMAT_VERSION, app_version=app_version,
                               exported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               kind="hall-of-fame", bots=bots[:200]))


def update_saved(key, fn, app_version=""):
    """Runs fn(entry) on the saved copies of one bot (Library and Hall of Fame) and saves them. Returns how many."""
    n = 0
    col = load_collection()
    for b in col:
        if bot_key(b) == key:
            fn(b)
            n += 1
    if n:
        save_collection(col)
    hof = load_hof()
    m = 0
    for b in hof:
        if bot_key(b) == key:
            fn(b)
            m += 1
    if m:
        save_hof(hof, app_version)
    return n + m


def induct(entries, app_version=""):
    """Adds or refreshes Hall of Fame entries (bot-file dicts). Returns how many were new."""
    hof = load_hof()
    index = {bot_key(b): i for i, b in enumerate(hof)}
    new = 0
    for e in entries:
        k = bot_key(e)
        e = dict(e, inducted_at=e.get("inducted_at") or datetime.now().isoformat(timespec="seconds"))
        if k in index:
            e["inducted_at"] = hof[index[k]].get("inducted_at", e["inducted_at"])
            for f in MEMORY_FIELDS:
                if f not in e and f in hof[index[k]]:
                    e[f] = hof[index[k]][f]
            hof[index[k]] = e
        else:
            index[k] = len(hof)
            hof.append(e)
            new += 1
    save_hof(sorted(hof, key=lambda b: -((b.get("lifetime") or {}).get("vs_holding_pct") or 0)), app_version)
    return new


def remove_from_hof(keys):
    hof = [b for b in load_hof() if bot_key(b) not in set(keys)]
    save_hof(hof)
    return hof


def read_bots_file(path):
    """-> (valid bots, list of problems). Never raises on bad content. Accepts a bot file, or the
    plain saved-lineup list. Only known strategy names and numeric settings get through; the
    file is data only and nothing in it is ever run."""
    problems = []
    try:
        if os.path.getsize(path) > MAX_BOT_FILE:
            return [], ["That file is too big to be a bot file."]
        with open(path) as f:
            data = json.load(f)
    except Exception as ex:
        return [], [f"Could not read the file ({ex.__class__.__name__})."]
    if isinstance(data, dict) and data.get("format") in BOT_FORMATS:
        raw = data.get("bots")
    elif isinstance(data, dict) and "name" in data and "params" in data:
        raw = [data]
    elif isinstance(data, list):
        raw = data
    else:
        return [], ["That does not look like an SBF or CBBF bot file."]
    if not isinstance(raw, list):
        return [], ["The bot list in this file is damaged."]
    good = []
    for i, d in enumerate(raw[:200], 1):
        if not isinstance(d, dict) or d.get("name") not in STRATS or d["name"] == "Dollar-cost averaging" \
                or not isinstance(d.get("params"), dict):
            problems.append(f"Skipped item {i}: unknown or unsupported strategy.")
            continue
        try:
            params = {k: float(v) for k, v in d["params"].items() if k in STRATS[d["name"]]["params"]}
            size = float(d.get("size", 1.0))
        except (TypeError, ValueError):
            problems.append(f"Skipped item {i}: settings are not numbers.")
            continue
        stats = d.get("stats") if isinstance(d.get("stats"), dict) else {}
        item = dict(name=d["name"], params=params, size=size, stats=stats, tf=clamp_tf(d.get("tf", 1)),
                    risk=clean_risk(d.get("risk") if isinstance(d.get("risk"), dict) else None),
                    traits=clean_traits(d.get("traits") if isinstance(d.get("traits"), dict) else None),
                    vetoes=clean_vetoes(d.get("vetoes") if isinstance(d.get("vetoes"), dict) else None),
                    layers=clean_layers(d.get("layers")))
        if isinstance(d.get("lifetime"), dict):
            item["lifetime"] = {k: v for k, v in d["lifetime"].items()
                                if isinstance(v, (int, float, str)) or v is None}
        if isinstance(d.get("inducted_at"), str):
            item["inducted_at"] = d["inducted_at"][:32]
        for k in COLLECT_FIELDS:  # Bot Collection extras (plain text / numbers only)
            v = d.get(k)
            if isinstance(v, bool) or isinstance(v, (int, float)) or (isinstance(v, str) and len(v) <= 80):
                item[k] = v
        if isinstance(d.get("notes"), str):
            item["notes"] = d["notes"][:200]
        mem = _clean_memory(d)
        item.update(mem)
        good.append(item)
    return good, problems


def append_trade_csv(row):
    path = os.path.join(HOME_DIR, "trades.csv")
    os.makedirs(HOME_DIR, exist_ok=True)
    if os.path.exists(path):  # an older file has fewer columns: keep it aside rather than mixing formats
        try:
            with open(path, newline="") as f:
                header = f.readline().strip()
            if header != ",".join(TRADE_FIELDS):
                os.replace(path, os.path.join(HOME_DIR, f"trades-before-{datetime.now():%Y%m%d-%H%M%S}.csv"))
        except OSError:
            pass
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in TRADE_FIELDS})


# ---------------- optional add-ons (installed on demand) ----------------
DEPENDENCIES = [  # keep in sync with requirements.txt
    dict(pip="keyring>=24.0", module="keyring", label="Secure login storage",
         why="Lets the app remember your Alpaca login in your computer's keychain."),
]


def dep_status():
    """[(dependency, installed?)]"""
    importlib.invalidate_caches()
    return [(d, importlib.util.find_spec(d["module"]) is not None) for d in DEPENDENCIES]


def missing_deps():
    return [d for d, ok in dep_status() if not ok]


def install_packages(specs, log=print):
    """Installs packages with pip into the Python running this app. Calls log(line) as pip works.
    Returns (ok, plain-English message)."""
    flags = {"creationflags": 0x08000000} if os.name == "nt" else {}  # no extra console window on Windows

    def run(cmd):
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, **flags)
        except OSError as e:
            return 1, str(e)
        out = []
        for line in p.stdout:
            out.append(line)
            log(line.rstrip())
        p.wait()
        return p.returncode, "".join(out)

    if importlib.util.find_spec("pip") is None:
        log("pip is missing, setting it up first...")
        run([sys.executable, "-m", "ensurepip", "--upgrade"])
    base = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input"]
    rc, out = run(base + list(specs))
    low = out.lower()
    if rc != 0 and any(x in low for x in ("permission", "access is denied", "externally-managed", "externally managed")):
        log("Retrying for your user account only...")
        rc, out = run(base + ["--user"] + list(specs))
        low = out.lower()
    if rc == 0:
        importlib.invalidate_caches()
        try:  # a fresh per-user install folder is not on the path until the next start
            import site
            u = site.getusersitepackages()
            if os.path.isdir(u) and u not in sys.path:
                sys.path.append(u)
        except Exception:
            note_error("install_packages")
        return True, "All set. The add-ons are installed and ready, no restart needed."
    if any(x in low for x in ("getaddrinfo", "connection", "timed out", "could not find a version", "no matching distribution",
                              "name or service not known", "network")):
        return False, "Couldn't download the add-ons. Check your internet connection and try again."
    if "externally-managed" in low or "externally managed" in low:
        return False, ("Your Python is locked by your operating system. Run this once in a terminal instead:\n"
                       f"{os.path.basename(sys.executable)} -m pip install --user --break-system-packages "
                       + " ".join(f'"{x}"' for x in specs))
    if "microsoft visual c++" in low or "failed building wheel" in low:
        return False, "An add-on needs build tools your computer doesn't have. Installing a newer Python (3.11 or 3.12) usually fixes it."
    return False, "The install didn't finish. The details are in the box above; sending them to me is the quickest way to get it fixed."


# ---------------- Alpaca login (API key) ----------------
KEYRING_SERVICE = "sbf"


def check_credentials(key, secret):
    if not (key or "").strip():
        return False, "The API Key ID box is empty. It looks like PK... (paper) or AK... (live)."
    if not (secret or "").strip():
        return False, "The secret key box is empty. Alpaca shows it once, when you create the key."
    if len(key.strip()) < 10 or len(secret.strip()) < 20:
        return False, "That doesn't look like an Alpaca key pair. Copy both values again from the Alpaca dashboard."
    return True, ""


def explain_login_error(err, secret=""):
    low = str(err).lower()
    if "401" in low or "403" in low or "unauthorized" in low or "forbidden" in low:
        return ("Alpaca did not accept this key.\n\nCheck that you picked the right account type: paper keys (they start "
                "with PK) only work with 'Paper', live keys (AK...) only with 'Live'.\n\n(Technical detail: " + str(err) + ")")
    return str(err)


# ---------------- other secrets: kept in the system keychain, never in config.json ----------------


def secret_set(name, value):
    """Stores a secret in the system keychain (Windows Credential Manager / macOS Keychain). False if unavailable."""
    try:
        import keyring
        if value:
            keyring.set_password(KEYRING_SERVICE, name, str(value))
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, name)
            except Exception:
                pass
        return True
    except Exception:
        return False


def secret_get(name):
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, name) or ""
    except Exception:
        return ""


def keychain_ok():
    try:
        import keyring
        keyring.get_password(KEYRING_SERVICE, "_probe")
        return True
    except Exception:
        return False


def scrub_config(cfg):
    """A copy of the config with its secrets moved to the keychain (if it works) and blanked."""
    if not isinstance(cfg, dict) or not keychain_ok():
        return cfg
    out = dict(cfg)
    st = dict(out.get("settings") or {})
    for k in SECRET_SETTINGS:  # an emptied field removes the stored secret too
        if k in st and secret_set(k, st.get(k) or ""):
            st[k] = ""
    for k in SECRET_CFG:
        if k in out and secret_set(k, out.get(k) or ""):
            out[k] = ""
    if "settings" in out:
        out["settings"] = st
    return out


def fill_secrets(cfg):
    """Puts the keychain's secrets back into a freshly loaded config (in memory only)."""
    st = cfg.setdefault("settings", {}) if isinstance(cfg.get("settings"), dict) or "settings" not in cfg else cfg["settings"]
    for k in SECRET_SETTINGS:
        if not st.get(k):
            v = secret_get(k)
            if v:
                st[k] = v
    for k in SECRET_CFG:
        if not cfg.get(k):
            v = secret_get(k)
            if v:
                cfg[k] = v
    return cfg


_REDACT = [
    (re.compile(r"(api_key=)[A-Za-z0-9]+"), r"\1[hidden]"),
    (re.compile(r"(api\.telegram\.org/bot)[^/\s]+"), r"\1[hidden]"),
    (re.compile(r"(discord(?:app)?\.com/api/webhooks/)\S+"), r"\1[hidden]"),
    (re.compile(r"\b(?:PK|AK)[A-Z0-9]{14,}\b"), "[hidden key]"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"), "[hidden token]"),
]


def redact(text):
    """Removes anything that looks like a key, token or webhook from text going into logs."""
    text = str(text)
    for rx, sub in _REDACT:
        text = rx.sub(sub, text)
    return text


def save_login(key, secret, paper=True):
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, "alpaca", json.dumps({"key": key, "secret": secret, "paper": bool(paper)}))
        return True
    except Exception:
        return False


def load_login():
    """(key, secret, paper) or None."""
    try:
        import keyring
        raw = keyring.get_password(KEYRING_SERVICE, "alpaca")
        if raw:
            d = json.loads(raw)
            return d["key"], d["secret"], bool(d.get("paper", True))
    except Exception:
        pass
    return None


def clear_login():
    try:
        import keyring
        keyring.delete_password(KEYRING_SERVICE, "alpaca")
    except Exception:
        pass


# ---------------- updates from GitHub ----------------


def latest_release(repo):
    """The newest published GitHub release of `repo`: its version, release-zip and checksum links, and notes.
    Updates come from releases (built and tagged by the Release workflow), never from whatever is on the main branch
    at that moment, so a half-finished push can't reach anyone."""
    d = json.loads(http_get(f"https://api.github.com/repos/{repo}/releases/latest", 15, retries=1))
    tag = str(d.get("tag_name") or "")
    assets = {a.get("name", ""): a.get("browser_download_url", "") for a in d.get("assets") or []}
    zip_name = next((n for n in assets if n.endswith(".zip")), "")
    return dict(version=tag.lstrip("vV"), tag=tag, zip_name=zip_name, zip_url=assets.get(zip_name, ""),
                sha_url=assets.get(zip_name + ".sha256", ""), notes=str(d.get("body") or ""))


def download_release(rel):
    """Downloads a release zip and checks it against the checksum published with it. Raises if anything's off,
    so nothing is installed from a damaged or incomplete download."""
    import hashlib
    if not rel.get("zip_url"):
        raise RuntimeError(f"release {rel.get('tag') or '?'} has no download attached")
    if not rel.get("sha_url"):
        raise RuntimeError(f"release {rel.get('tag') or '?'} has no checksum attached, so it wasn't installed")
    want = sha256_from_file(http_get(rel["sha_url"], 20, retries=2).decode("utf-8", "replace"))
    if not want:
        raise RuntimeError("the release's checksum file couldn't be read, so nothing was installed")
    data = http_get(rel["zip_url"], 120, retries=2)
    if hashlib.sha256(data).hexdigest() != want:
        raise RuntimeError("the download doesn't match its checksum (damaged or incomplete), so nothing was installed")
    return data


def install_update(zip_bytes, here, current_version, expect_version=None):
    """Unpacks a release zip (or a GitHub branch zip) over the app folder. The files being replaced are first copied to
    here/backups/<old version>-<time>/ so the update can be undone. With expect_version, the zip's VERSION file must
    say the same. Returns (files written, backup folder)."""
    import io
    import zipfile
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    picks = {}
    for name in z.namelist():
        rel = name.split("/", 1)[1] if "/" in name else ""
        if rel and "/" not in rel and UPDATE_FILES.match(rel):
            picks[rel] = name
    if "main.py" not in picks or "engine.py" not in picks:
        raise RuntimeError("the download doesn't look like this app (main.py / engine.py missing)")
    if expect_version:
        got = z.read(picks["VERSION"]).decode("utf-8", "replace").strip() if "VERSION" in picks else ""
        if got != str(expect_version).strip():
            raise RuntimeError(f"the download says it is version {got or '?'}, not {expect_version}")
    for rel in ("main.py", "engine.py", "broker.py"):  # never install something that doesn't even compile
        if rel in picks:
            compile(z.read(picks[rel]).decode("utf-8"), rel, "exec")
    backup = os.path.join(here, "backups", f"{current_version}-{datetime.now():%Y%m%d-%H%M%S}")
    os.makedirs(backup, exist_ok=True)
    for rel in os.listdir(here):
        if UPDATE_FILES.match(rel) and os.path.isfile(os.path.join(here, rel)):
            shutil.copy2(os.path.join(here, rel), os.path.join(backup, rel))
    written = []
    for rel, name in picks.items():
        tmp = os.path.join(here, rel + ".new")
        with open(tmp, "wb") as f:
            f.write(z.read(name))
        os.replace(tmp, os.path.join(here, rel))
        written.append(rel)
    return written, backup


# ---------------- SBF's own notification sounds ----------------


def sound_path(kind, volume=60):
    """Writes (once) and returns a small .wav for this kind of sound: soft bell-like tones with a quick
    fade, clearly different from the Windows system sounds."""
    import math as _m
    import struct
    import wave
    vol = max(0, min(100, int(volume)))
    folder = os.path.join(HOME_DIR, "sounds")
    path = os.path.join(folder, f"{kind}-{vol}.wav")
    if os.path.exists(path):
        return path
    os.makedirs(folder, exist_ok=True)
    rate = 22050
    notes = SOUNDS.get(kind, SOUNDS["notify"])
    total = max(st + ln for _, st, ln in notes) + 0.05
    buf = [0.0] * int(rate * total)
    for f, st, ln in notes:
        a = int(st * rate)
        for i in range(int(ln * rate)):
            t = i / rate
            env = min(1.0, t / 0.008) * _m.exp(-t * 6.5 / ln)  # quick attack, bell-like decay
            v = (_m.sin(2 * _m.pi * f * t) + 0.35 * _m.sin(2 * _m.pi * 2 * f * t) + 0.12 * _m.sin(2 * _m.pi * 3 * f * t)) * env
            if a + i < len(buf):
                buf[a + i] += v
    peak = max(1e-9, max(abs(x) for x in buf))
    scale = 32767 * 0.9 * (vol / 100) / peak
    with wave.open(path + ".tmp", "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(x * scale)) for x in buf))
    os.replace(path + ".tmp", path)
    return path


def play_sound(kind, volume=60):
    """Plays one of SBF's sounds without blocking. Silently does nothing if the computer can't."""
    def run():
        try:
            path = sound_path(kind, volume)
            if sys.platform.startswith("win"):
                import winsound
                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            elif sys.platform == "darwin":
                subprocess.Popen(["afplay", path])
            else:
                for player in ("paplay", "aplay", "pw-play"):
                    if shutil.which(player):
                        subprocess.Popen([player, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        break
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()
