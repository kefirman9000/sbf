"""SBF (Stock Bot Farm) - stock trading bot GUI.

Practice mode (default) simulates share trades on live prices with pretend money.
The bots read stock charts and buy and sell shares (whole or fractional). Only proven bots get a say in the
account's trades. Live mode trades your broker account (Alpaca paper or real) through broker.py.
"""

import sys


def _stay_open(msg):
    """Print a problem and keep the window open so it can be read (Windows closes it otherwise)."""
    if sys.stdout is None and sys.platform.startswith("win"):  # started without a console (pythonw): show a box
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, "SBF could not start", 0x10)
        except Exception:
            E.note_error("_stay_open")
        sys.exit(1)
    print("\n" + "=" * 60 + "\nSBF could not start\n" + "=" * 60 + "\n" + msg + "\n")
    try:
        input("Press Enter to close this window...")
    except Exception:
        E.note_error("_stay_open")
    sys.exit(1)


if sys.version_info < (3, 8):
    _stay_open("SBF needs Python 3.8 or newer. You have " + sys.version.split()[0] +
               ".\nGet the latest from https://www.python.org/downloads/ (tick 'Add Python to PATH').")
try:
    import tkinter  # noqa: F401
except ImportError:
    _stay_open("Your Python has no Tkinter (the window toolkit).\nRe-run the Python installer, choose Modify, "
               "and make sure 'tcl/tk and IDLE' is ticked.")
try:
    import engine  # noqa: F401
except ImportError as _e:
    _stay_open(f"Could not load engine.py ({_e}).\nExtract the whole zip first (right-click > Extract All), "
               "then run main.py from the extracted folder. main.py and engine.py must sit side by side.")

import bisect
import csv
import json
import math
import os
import queue
import re
import webbrowser
import shutil
import subprocess
import random
import threading
import time
import tkinter as tk
import zipfile
import collections
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from tkinter import filedialog, messagebox, simpledialog, ttk

import engine as E
import broker as B

# ---- Update settings: change to your GitHub "user/repo" after publishing ----
UPDATE_REPO = "kefirman9000/sbf"
DONATE_URL = "https://github.com/sponsors/kefirman9000"  # the Donate button (top right)
HERE = os.path.dirname(os.path.abspath(__file__))
APP_SHORT = "SBF"
CONFIG_PATH = os.path.join(E.HOME_DIR, "config.json")

COINS = ["SPY", "QQQ", "IWM", "AAPL", "NVDA", "TSLA", "AMD", "MSFT", "AMZN", "META",
         "SPYU", "TQQQ", "SQQQ"]  # tickers (the last three: leveraged and inverse ETFs, see E.LEVERAGED)
MIN_LIVE_ORDER = 2.0   # USD; smaller live buys are skipped
MAX_FAILS = 3          # consecutive order failures before auto-stop
BAR_CHOICES = E.BAR_CHOICES
BAR_LABELS = {b: E.fmt_bar(b) for b in BAR_CHOICES}
LAB_CANDLES = {"1 min": 60, "5 min": 300, "15 min": 900, "1 hr": 3600, "6 hr": 21600}
BG, CARD, TEXT, MUTED = "#f3f4f6", "#ffffff", "#111827", "#6b7280"
ACCENT, GREEN, RED, LINE = "#2563eb", "#16a34a", "#dc2626", "#e5e7eb"
LIGHT_P = dict(BG="#f3f4f6", CARD="#ffffff", TEXT="#111827", MUTED="#6b7280",
               ACCENT="#2563eb", GREEN="#16a34a", RED="#dc2626", LINE="#e5e7eb")
DARK_P = dict(BG="#111827", CARD="#1f2937", TEXT="#f3f4f6", MUTED="#9ca3af",
              ACCENT="#3b82f6", GREEN="#22c55e", RED="#ef4444", LINE="#374151")
EXTRA_TO_DARK = {"#eff6ff": "#1e3a5f", "#1e3a8a": "#bfdbfe", "#b45309": "#fbbf24"}  # headline bar, warning text
DARK = True
LIVE_BLUE = "#0052ff"  # Live mode colour (your Alpaca account); Practice mode is green


def apply_theme(root, dark):
    """Sets the colour palette (module-level colours used by every widget built or restyled afterwards)."""
    global BG, CARD, TEXT, MUTED, ACCENT, GREEN, RED, LINE, DARK
    DARK = dark
    p = DARK_P if dark else LIGHT_P
    BG, CARD, TEXT, MUTED = p["BG"], p["CARD"], p["TEXT"], p["MUTED"]
    ACCENT, GREEN, RED, LINE = p["ACCENT"], p["GREEN"], p["RED"], p["LINE"]
    root.configure(bg=BG)
    for opt, val in (("*Toplevel.background", BG), ("*Text.background", CARD), ("*Text.foreground", TEXT),
                     ("*Text.insertBackground", TEXT), ("*TCombobox*Listbox.background", CARD),
                     ("*TCombobox*Listbox.foreground", TEXT)):
        root.option_add(opt, val)


def recolor(widget, mapping):
    """Walks every widget and swaps old palette colours for the new ones."""
    for opt in ("bg", "fg", "background", "foreground", "highlightbackground", "highlightcolor",
                "insertbackground", "activebackground", "activeforeground", "selectbackground"):
        try:
            new = mapping.get(str(widget.cget(opt)).lower())
            if new:
                widget.configure(**{opt: new})
        except Exception:
            pass
    for ch in widget.winfo_children():
        recolor(ch, mapping)
SETTING_GROUPS = [
    ("Basics", "Your account's money and safety rules, used for the trades your account makes (the bots' practice "
               "trades follow their own rules). All % are of the money you started this session with. Not sure? Pick "
               "a Risk style on the main screen (Cautious, Balanced, Adventurous): it fills in the first five for you. "
               "In Wildcard, 'Let the winning bots set stops' (Orders tab) lets the winners' learned Safety stop and "
               "Lock in profit take over; these numbers are then the fallback. Fees are always counted.", [
        ("show_broker_value", "Show your Alpaca account's own value in a tile at the top (while you're logged in), next to "
                              "the PRACTICE numbers", bool),
        ("trade_pct", "Trade size: each buy spends this % of your starting money. Example: 10 with $1,000 = $100 per buy "
                      "(winning bots can scale this up to their trade-size multiplier)", float),
        ("max_group_pct", "Tickers that move together: never have more than this % of your starting money in a group "
                          "of stocks whose prices have been moving in step, like SPY and QQQ (0 = off)", float),
        ("max_coin_pct", "Limit per ticker: never have more than this % of your starting money in any single stock. "
                         "Example: 30 with $1,000 = at most $300 in SPY", float),
        ("stop_loss", "Safety stop: automatically sell a stock if it falls this % below the price you paid. "
                      "Example: 5 = sell if a $100 position drops to $95 (0 = off). On a leveraged ETF it's the ETF's own "
                      "price: 5% on SPYU (4x) is about a 1.25% move in the S&P 500", float),
        ("take_profit", "Lock in profit: automatically sell a stock once it is up this % on what you paid. "
                        "Example: 10 = sell when a $100 position reaches $110 (0 = off)", float),
        ("trail_stop", "Trailing stop: sell a stock if it falls this % from its highest price since you bought it. "
                       "Example: 4 = a stock that rose to $120 is sold at $115.20 (0 = off)", float),
        ("min_hold_min", "Minimum hold: keep a stock at least this many minutes before a strategy may sell it. "
                         "Safety stops still sell at any time (0 = off)", float),
        ("daily_loss", "Daily loss limit: stop trading for the rest of the day if the account falls this % since "
                       "midnight. Open positions are kept, not sold (0 = off). The TODAY tile shows how close you are", float),
        ("pdt_guard", "Day-trade guard: under $25,000, never make a 4th day trade (buy and sell the same stock the same "
                      "day) in 5 business days, so the broker doesn't flag the account as a pattern day trader. Safety "
                      "exits (stops, kill switch, your own sells) still go through", bool),
        ("max_spread_pct", "LIVE: skip a buy when the stock's bid/ask gap is wider than this % of its price (thinly "
                           "traded stocks fill badly). Example: 0.5 (0 = off)", float),
        ("bot_allowance", "Allowance per bot: the most of your starting money that one bot's buy votes may have tied up "
                          "at once. Stops one bad dragon draining the account. Example: 25 with $1,000 = $250 per bot (0 = no limit)", float),
    ]),
    ("Wildcard", "How the bot treats strategies that are winning, shaky or failing.", [
        ("auto_exams", "Exams with Free roam off: every 20 minutes, up to 3 bots that still need exams are tested on history "
                       "they never trained on (they learn nothing from it; it only records how they did)", bool),
        ("keep_variety", "Keep backup strategies: when one strategy fills over half the farm, half the new eggs try other "
                         "strategies, and the last two of any strategy are dropped last", bool),
        ("keep_veterans", "Keep proven veterans: a bot with 8+ finished trades that made money over its life and beat just "
                          "holding survives a bad patch (it's benched, not retired) until its lifetime record turns", bool),
        ("proven_only", "Only proven bots trade the account (practice, Alpaca paper and LIVE): a bot needs a real track "
                        "record first. Every other bot keeps training on its own pretend money", bool),
        ("proven_trades", "...finished trades a bot needs before it may trade the account", int),
        ("proven_exam", "...and it must have passed its exams (Free roam tests it on days it never trained on)", bool),
        ("winner_boost", "Biggest trade size a top winner can earn (1 = normal, 2.5 = 2.5 times)", float),
        ("slow_every", "Shaky strategies may only trade every N decisions", int),
        ("fail_drop", "Stop a strategy at once if it falls this % from its peak (0 = off)", float),
        ("evolve_every", "Review and replace strategies every N decisions", int),
        ("max_bots", "Farm size: most bots competing at once (14 to 200)", int),
        ("retire_hours", "Replace a strategy that hasn't been winning for this many hours (0 = never)", float),
        ("auto_coins", "Let the bot pick this many stocks when Wildcard starts (0 = use the ticked ones)", int),
    ]),
    ("Learning", "What the bots are allowed to learn from. Everything is on by default; untick anything you'd rather "
                 "they didn't use. Changes apply to new eggs right away, and to the whole farm as it breeds.", [
        (None, "How bots are judged", "head"),
        ("beat_hold", "Must beat the market (its tickers left alone) to count as Winning", bool),
        ("risk_adjusted", "Rank by steady gains (gain per unit of downside), not the biggest gain", bool),
        ("prove_live", "Must also win on fresh prices it hadn't seen (guards against luck)", bool),
        ("twin_test", "Twin test: a copy re-run on older prices; lucky winners count for less", bool),
        ("regime_aware", "Favour bots that do well in today's market mood", bool),
        (None, "Breeding and evolution", "head"),
        ("crossbreed", "Crossbreeding: children can mix two winners' genes", bool),
        ("islands", "Islands: separate sub-farms that only swap a champion now and then (1 = one big farm, up to 6)", int),
        ("elders", "Elders teach: good bots that leave nudge future eggs of their family", bool),
        ("mut_memory", "Mutation memory: do more of the kinds of change that have made winners", bool),
        ("classics", "Famous strategies hatch from silver eggs (golden cross, turtle, RSI-2...)", bool),
        ("classic_rate", "...% of new eggs that are a famous strategy", float),
        ("long_holds", "Slow, longer-holding bots from the start", bool),
        ("hindsight", "Hindsight: Free roam also looks back at what WOULD have worked (many instincts and settings tried on "
                      "a stretch, checked on the part after it), and new eggs start from what worked instead of at random", bool),
        ("kamikazes", "Kamikaze breed: big bets with wide stops and huge targets; each one also tests one or two risky "
                      "rules so your good bots don't have to. They never get a say in your account", bool),
        ("kami_share", "...% of the farm kept for kamikazes", int),
        ("scalpers", "Scalp breed (wind dragons): fast in-and-out bots with their own stop and target (from quick 0.2% "
                     "nips up to 4% runs), a time limit, and flat before the close. Needs a decision speed of 5 min "
                     "or faster", bool),
        ("scalp_share", "...% of the farm kept for scalpers (they can win up to twice that, never over 60%)", int),
        ("scalp_spread", "...scalpers skip stocks whose bid/ask gap is wider than this % of the price (LIVE checks the "
                         "live quote first)", float),
        (None, "Rules a bot can grow", "head"),
        ("layers", "Studies: several reasons to buy, several reasons not to, exit studies and sizing", bool),
        ("htf_studies", "Bigger-trend study: a slower timeframe must agree before buying", bool),
        ("ctx_studies", "Market context: bots can grow studies on the tickers below (e.g. VIX jumping = no new buys)", bool),
        ("ctx_tickers", "Tickers the bots WATCH but never trade, separated by commas (VIXY stands in for the VIX index, "
                        "which isn't in the free data feed)", "tickers"),
        ("vetoes", "Never-rules: things a bot refuses to do, each graded in hindsight", bool),
        ("adapt_exits", "Adaptive stops: wider in wild markets, tighter in calm ones", bool),
        ("luck_check", "Luck check: a bot's great score counts for less while it rests on only a few trades", bool),
        ("exam_gate", "Exams: bots are tested on days they never trained on; failing keeps them out of BEASTS", bool),
        ("cond_aware", "Right bot for the moment: each bot's say follows its record in today's conditions", bool),
        ("think_between", "Think between candles: a few times a minute the bots also decide on the candle that's still "
                          "forming (the live price), so they can act without waiting for the minute to end", bool),
        ("time_habits", "Time habits: bots can learn to skip the opening or closing rush (graded in hindsight)", bool),
        ("agree_sizing", "Crowd confidence: your account trades bigger when several different strategies agree, smaller "
                         "when the farm is split", bool),
        ("confidence_sizing", "Confidence sizing: bigger when its studies clearly agree", bool),
        (None, "Memory", "head"),
        ("seasons", "Seasons: a champion per market mood, woken when that mood returns", bool),
        ("coin_prefs", "Favourite stocks: skip stocks it keeps losing on (re-checked in hindsight)", bool),
        ("post_review", "Post-trade review: losing trades teach which never-rules to try next", bool),
        ("learn_losses", "Learn from its own losses: a bot takes on a never-rule that would have stopped several of its "
                         "losing trades (and few winners)", bool),
        ("multicore", "Learn on several processor cores at once at the start (faster warm-up)", bool),
        ("roam_reuse", "Free roam studies each stretch of history it downloads this many times before downloading a "
                       "new one (1 = always new). Only the first visit can promote bots (exams, graduates); repeat "
                       "visits only teach", int),
        ("feature_tests", "Free roam tests what each kind of learning is worth: the same practice farm with one switch on, "
                          "then off (results: Free roam > What learning is worth). Nothing is changed for you", bool),
    ]),
    ("Library & Hall of Fame", "The Bot Library keeps every bot you save, plus long-term winners the farm saves for you. "
                               "The Hall of Fame holds only the bots you promote yourself (right-click a bot).", [
        ("hof_hours", "Hours a strategy must spend Winning (in total) to count as a long-term winner", float),
        ("hof_min_trades", "...and at least this many finished (sold) trades", int),
        ("auto_catch", "Save long-term winners to the Bot Library automatically", bool),
        ("auto_hof", "Also put long-term winners straight into the Hall of Fame (off = you choose who goes in)", bool),
        ("auto_resume", "Pick up where you left off: if the bot was running when SBF closed, resume that session on the "
                        "next start (Live asks first)", bool),
        ("hof_load", "When the whole farm trades, bring the 10 best Hall of Fame bots back to compete", bool),
        ("team_locked", "Trading with my team: keep exactly those bots (untick to let the team breed new bots)", bool),
        ("shadow_live", "Shadow Live: follow your Hall of Fame bots with pretend money in real time (see the "
                        "'Hall of Fame shadow' chart)", bool),
        ("retest_nightly", "Nightly retest: once a day, re-backtest the Hall of Fame on the latest prices; ones that "
                           "stopped working get a RUSTY badge", bool),
        ("graduation", "Graduation: before real money, check each Hall of Fame / team bot has earned it", bool),
        ("grad_hours", "...hours a bot must have spent Winning to graduate", float),
    ]),
    ("Orders", "How orders are placed, and the extra costs Practice mode pretends to pay.", [
        ("limit_wait", "Limit orders: seconds to wait for a fill", int),
        ("limit_fallback", "...and if nobody takes it in time, buy or sell at market instead (off = skip that trade)", bool),
        ("max_trades_hour", "Brake: at most this many strategy trades per hour (0 = no limit). Safety stops always run", int),
        ("trade_hours", "Trading hours: the account only makes strategy trades in these hours, like 9-17 or 22-6 "
                        "(empty = any time). The bots keep learning and safety stops always run", str),
        ("weekdays_only", "...and only Monday to Friday", bool),
        ("fee_aware", "Fee-aware: bots and the account don't sell for a profit too small to cover the round-trip fees "
                      "(losses, stops and Sell everything still sell)", bool),
        ("bot_stops", "Wildcard: let the winning bots set the Safety stop and Lock-in-profit levels they learned "
                      "(your Basics numbers are used when they don't)", bool),
        ("fee_pct", "Fees on each share trade in Practice mode (%). Alpaca charges no commission on stocks; 0.005 covers "
                    "the small regulatory charges on sells", float),
        ("slippage", "Half the bid/ask gap each Practice fill pays (%): buys fill a little higher, sells a little lower. "
                     "0.03 suits busy stocks and ETFs", float),
    ]),
    ("Timing", "How often the bot looks at prices.", [
        ("bar_minutes", "How often strategies decide (15 sec is the fastest)", "bar"),
        ("refresh", "Check prices every N seconds (1 or more)", int),
        ("animations", "Animations (Light uses less computer power)", "anim"),
        ("egg_colors", "Egg colours: by dragon type, or all purple (royal eggs from your own bots are always gold, "
                       "famous strategies silver)", "eggs"),
        ("daily_recap", "Daily recap: once a day, how the bots did against the market (in the log, and as a note on the "
                        "icon by the clock)", bool),
        ("alpaca_adopt_all", "Let SBF manage every untracked Alpaca stock: any share position in your Alpaca account "
                             "that nothing in SBF is following is picked up and sold by your stop, target and trailing "
                             "rules (also for a LIVE key, so only turn this on if that's what you want: it includes "
                             "shares you meant to keep)", bool),
        ("report_detail", "Detailed reports for Claude: Export a report also adds every bot's recent trades (with the "
                          "prices and volatility behind them), the price feed's gaps, errors and automatic checks", bool),
        ("auto_backup", "Weekly backup: keep a copy of your bots, farms and settings in the data folder (last 4 kept, no keys)", bool),
        ("bg_mode", "Background mode: while SBF is minimized, skip all drawing and check watchlist prices less often", bool),
        ("tray_autotrain", "Icon by the clock: start training on practice prices as soon as SBF starts in the background "
                           "(File > Run in the background). LIVE never starts by itself", bool),
        ("characters", "Dragons: themed by strategy (fire, ice, lightning, space; resting bots are skeletons) or classic (coloured by status)", "chars"),
    ]),
    ("Alerts", "Get a message on your phone when something happens. Leave blank to turn off.", [
        ("alert_discord", "Discord webhook link (Server settings > Integrations > Webhooks)", str),
        ("alert_tg_token", "Telegram bot token (from @BotFather)", str),
        ("alert_tg_chat", "Telegram chat ID", str),
        ("alert_trades", "Alert on every trade the account makes", bool),
        ("alert_hof", "Alert when a bot joins the Hall of Fame", bool),
        ("alert_problems", "Alert on problems (daily limit hit, order failures, prices down)", bool),
        ("why_popup", "LIVE: show a small 'why did it trade?' note for every real trade (also on the Trades tab: double-click a trade)", bool),
        ("weekly_report", "Weekly report: once a week, write what the farm learned and how the account did (sent here too if set up)", bool),
        ("tg_remote", "Phone remote (Telegram): send /status, /pause, /resume, /report or /kill to your bot from the chat "
                      "above. Only that chat is obeyed", bool),
    ]),
]
SETTING_FIELDS = [f for _, _, fields in SETTING_GROUPS for f in fields if f[2] != "head"]


def parse_hours(txt):
    """'9-17' -> (9, 17); '22-6' wraps past midnight. None if it isn't valid."""
    try:
        a, b = (int(x) for x in str(txt).replace(" ", "").split("-"))
        return (a, b) if 0 <= a <= 24 and 0 <= b <= 24 and a != b else None
    except (TypeError, ValueError):
        return None


def in_hours(txt, weekdays_only=False, now=None):
    """True if now is inside the trading hours (empty = always)."""
    now = now or datetime.now()
    if weekdays_only and now.weekday() >= 5:
        return False
    h = parse_hours(txt) if txt else None
    if not h:
        return True
    a, b = h
    return a <= now.hour < b if a < b else (now.hour >= a or now.hour < b)
STATUS_TEXT = {"active": "Winning (following)", "slowed": "Shaky (slowed)", "standby": "Good (on standby)",
               "benched": "Stopped", "warming up": "Learning..."}
STATUS_TAG = {"active": "win", "slowed": "shaky", "standby": "standby", "benched": "stopped", "warming up": "learning"}
ORDER_LABELS = {"market": "Market (fills now, pays the spread)", "limit": "Limit at the bid / ask (market if it doesn't fill)"}
SCALP_BOOK = "~scalp"  # the account keeps scalps in their own book per ticker: "SPY" (long-term) and "SPY~scalp"


def scalp_key(c):
    return c + SCALP_BOOK


def tick_of(k):
    """The ticker behind an account book key ("SPY~scalp" -> "SPY")."""
    return k.split("~", 1)[0] if isinstance(k, str) else k


def trade_kind(tr):
    """'call' / 'put' of an account trade row, or ''."""
    txt = f"{tr.get('contract', '')} {tr.get('coin', '')}".lower()
    if " call" in txt or re.search(r"\d(c)\b", txt):
        return "call"
    if " put" in txt or re.search(r"\d(p)\b", txt):
        return "put"
    return ""


def is_scalp_book(k):
    return isinstance(k, str) and k.endswith(SCALP_BOOK)


STRAT_LABELS = {
    "SMA crossover": "Trend follower", "RSI": "Buy dips / sell peaks", "Momentum": "Ride the rise",
    "Bollinger bands": "Buy low / sell high", "Dollar-cost averaging": "Steady buyer (DCA)",
    "Breakout": "Breakout chaser", "MACD": "Momentum shift", "Mean reversion": "Snap-back",
    "EMA crossover": "Quick trend (EMA)", "Stochastic": "Range bounce", "Trend + dip": "Dips in an uptrend",
    "Supertrend": "Supertrend", "Ichimoku cross": "Cloud cross (Ichimoku)", "Rate of change": "Speed shift (ROC)",
    "MA ribbon": "Trend ribbon",
    "Volume breakout": "Volume breakout", "VWAP snap-back": "VWAP snap-back", "ATR channel": "Range channel (ATR)",
    "Micro pullback": "Scalp: micro pullback", "Volume burst": "Scalp: volume burst", "VWAP reclaim": "Scalp: VWAP reclaim",
    "Range scalp": "Scalp: range edges", "Quick flip": "Scalp: quick flip",
}
STRAT_TIPS = {
    "SMA crossover": "Buys when the short-term price trend crosses above the long-term trend, sells when it crosses below.",
    "RSI": "Buys after a coin has dropped a lot (looks oversold), sells after it has jumped a lot (looks overbought).",
    "Momentum": "Buys coins that are rising quickly and sells coins that are falling quickly.",
    "Bollinger bands": "Buys when the price is unusually low compared to recent prices, sells when unusually high.",
    "Dollar-cost averaging": "Buys a small fixed amount on a regular schedule. Never sells by itself.",
    "Breakout": "Buys when the price breaks above its recent high, sells when it breaks below its recent low.",
    "MACD": "Buys when short-term momentum turns up through its smoothed line, sells when it turns down.",
    "Mean reversion": "Buys when the price is well below its recent average, sells when well above, expecting a snap back.",
    "EMA crossover": "A faster trend follower: reacts to short-term turns quickly. Ignores tiny crossings that "
                     "wouldn't cover fees. Suited to short decision speeds.",
    "Stochastic": "Buys when the price climbs back up out of the bottom of its recent range, sells when it falls "
                  "back from the top.",
    "Trend + dip": "Only buys short dips while the bigger trend is up, and gets out when the uptrend breaks.",
    "Volume breakout": "Buys a breakout above the recent high only when trading volume is well above normal, "
                       "sells when the price breaks below the recent low.",
    "VWAP snap-back": "Buys when the price is well below its volume-weighted average price, sells when well above.",
    "Supertrend": "Rides a trailing band a few typical bar-ranges away from the price; buys when the trend flips up, sells "
                  "when it flips down.",
    "Ichimoku cross": "Buys when the short-term midpoint of highs and lows crosses above the long-term one (with the price "
                      "above it), sells on the opposite cross.",
    "Rate of change": "Watches how fast the price is moving; buys when the speed turns up while rising, sells when it turns "
                      "down while falling.",
    "MA ribbon": "Buys when a fast, middle and slow average line up in order (a healthy uptrend), sells when the fast one "
                 "drops below the middle one.",
    "ATR channel": "Buys a push above the average plus a few typical bar ranges (from highs and lows), sells a drop "
                   "below it.",
    "Micro pullback": "Scalper. In a short-term trend, waits for a brief pause that holds the trend, then jumps in when "
                      "the price pushes past the last bar. Out within minutes, flat before the close.",
    "Volume burst": "Scalper. Rides the first push when one bar suddenly trades several times its usual volume and "
                    "closes strong. Out within minutes, flat before the close.",
    "VWAP reclaim": "Scalper. Trades the moment the price crosses back over its volume-weighted average on busy "
                    "volume. Out within minutes, flat before the close.",
    "Range scalp": "Scalper. When the price is stuck in a tight box, buys a turn up at the bottom edge and sells a turn "
                   "down at the top edge. Out within minutes, flat before the close.",
    "Quick flip": "Scalper. Jumps on a short run of closes all in one direction. Out within minutes, flat before the close.",
}
PARAM_NAMES = {"fast": "short average", "slow": "long average", "period": "lookback", "lo": "buy below",
               "hi": "sell above", "n": "lookback", "thr": "move needed %", "k": "band width", "every": "buy every N decisions",
               "signal": "signal smoothing", "min_gap": "smallest gap %", "trend": "trend lookback",
               "vmult": "volume x normal", "m": "range multiple", "mid": "middle average", "smooth": "smoothing",
               "pull": "pullback bars", "vmin": "volume needed x normal", "edge": "edge of the box",
               "maxw": "widest box %", "run": "closes in a row"}
REASON_TEXT = {"bot-test": "bot's own test trade", "signal": "strategy signal", "stop-loss": "safety stop", "take-profit": "locked in profit",
               "manual": "you sold by hand", "add-on": "adding to a winning strategy's position",
               "trailing-stop": "trailing stop (fell from its high)", "manual buy": "you bought by hand",
               "slow farm": "slow farm's winners agreed",
               "scalp-time": "scalp held as long as the scalpers allow", "scalp-flat": "scalp closed before the bell"}
MOOD_TEXT = {"trending up": "Trending up", "trending down": "Trending down", "choppy": "Choppy (back and forth)",
             "quiet": "Quiet", "unknown": "-"}


def pct_txt(x, digits=1):
    return "-" if x is None else f"{x * 100:+.{digits}f}%"


class ScrollFrame(tk.Frame):
    """A frame with a vertical scrollbar that only appears when the content is taller than the space."""

    def __init__(self, parent, bg, width=270):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, width=width)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", self._update)
        self.canvas.bind("<Configure>", self._update)
        for w in (self.canvas, self.inner):
            w.bind("<Enter>", lambda e: self._wheel(True))
            w.bind("<Leave>", lambda e: self._wheel(False))

    def _update(self, _=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.canvas.itemconfigure(self.win, width=self.canvas.winfo_width())
        need = self.inner.winfo_reqheight() > self.canvas.winfo_height() + 2
        if need and not self.sb.winfo_ismapped():
            self.sb.pack(side="right", fill="y")
        elif not need and self.sb.winfo_ismapped():
            self.sb.pack_forget()
            self.canvas.yview_moveto(0)

    def _wheel(self, on):
        if on:
            self.canvas.bind_all("<MouseWheel>", self._scroll)
            self.canvas.bind_all("<Button-4>", lambda e: self._scroll(e, -1))
            self.canvas.bind_all("<Button-5>", lambda e: self._scroll(e, 1))
        else:
            for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.canvas.unbind_all(ev)

    def _scroll(self, e, d=None):
        if not self.sb.winfo_ismapped():
            return
        if d is None:
            d = -1 if e.delta > 0 else 1
        self.canvas.yview_scroll(d, "units")
STYLE_TIPS = {
    "Cautious": "Small trades and tight safety stops. Slower and steadier.",
    "Balanced": "Medium trades and sensible stops. A good starting point.",
    "Adventurous": "Bigger trades and wider stops. More upside, more risk.",
}


def friendly_text(t):
    for k, v in STRAT_LABELS.items():
        t = t.replace(k, v)
    return t


def friendly_label(name, params, tf=1, bar=None):
    """Strategy name and settings, plus how often it decides (e.g. 'checks every 15 min')."""
    pace = ""
    if tf and tf > 1:
        pace = f", every {E.fmt_bar(tf * bar)}" if bar else f", every {tf} bars"
    return f"{STRAT_LABELS.get(name, name)} ({'/'.join(str(v) for v in params.values())}{pace})"


def _sort_key(v):
    s = str(v).replace(",", "").replace("$", "").replace("%", "").replace("x", "").replace("+", "").strip()
    try:
        return (0, float(s), "")
    except ValueError:
        return (1, 0.0, str(v).lower())


def sort_tree(t, col, reverse=None):
    """Sorts a list by a column (click again to flip). The choice sticks across refreshes."""
    cur = getattr(t, "_sort", None)
    if reverse is None:
        reverse = (not cur[1]) if cur and cur[0] == col else False
    t._sort = (col, reverse)
    items = sorted(((_sort_key(t.set(i, col)), i) for i in t.get_children("")), reverse=reverse)
    for idx, (_, i) in enumerate(items):
        t.move(i, "", idx)


def resort(t):
    cur = getattr(t, "_sort", None)
    if cur:
        try:
            sort_tree(t, cur[0], cur[1])
        except Exception:
            pass



COIN_COLORS = {"SPY": "#2563eb", "QQQ": "#7c3aed", "IWM": "#0891b2", "AAPL": "#6b7280", "MSFT": "#16a34a",
               "NVDA": "#65a30d", "TSLA": "#dc2626", "AMZN": "#f59e0b", "META": "#1d4ed8", "AMD": "#be123c",
               "VIXY": "#f97316", "TLT": "#0f766e"}


def coin_color(c):
    if c in COIN_COLORS:
        return COIN_COLORS[c]
    h = sum(ord(ch) * (i + 7) for i, ch in enumerate(c))
    return "#%02x%02x%02x" % (80 + h % 150, 80 + (h // 7) % 150, 80 + (h // 49) % 150)


def draw_coin_badge(cv, x, y, size, c):
    """A round coin badge in the coin's colour with its ticker (not an official logo)."""
    col = coin_color(c)
    cv.create_oval(x, y, x + size, y + size, fill=col, outline="")
    cv.create_oval(x + 3, y + 3, x + size - 3, y + size - 3, outline="#ffffff", width=1)
    label = c if len(c) <= 3 else c[:3]
    cv.create_text(x + size / 2, y + size / 2, text=label, fill="white", font=("", max(7, int(size / (2.6 + len(label) * 0.35))), "bold"))


def fmt_px(px):
    if not px:
        return "-"
    return f"${px:,.2f}" if px >= 1 else f"${px:,.6f}"


def draw_egg(c, x, y, size, color, progress=0.0, phase=0, tags=(), royal=False, classic=False):
    """A speckled dragon egg. It wobbles more and cracks open as it gets closer to hatching (progress 0..1)."""
    k = size / 40.0
    wob = math.sin(phase / 1.7) * (1 + 3 * progress) if phase else 0
    cx = x + 20 * k + wob

    def P(*pts):
        return [v * k + (x + wob if i % 2 == 0 else y) for i, v in enumerate(pts)]
    c.create_oval(x + 6 * k, y + 50 * k, x + 34 * k, y + 56 * k, fill="#111827", outline="", tags=tags)  # shadow
    c.create_oval(*P(4, 4, 36, 52), fill="#f5f0e1", outline=color, width=2, tags=tags)
    for sx, sy, r in ((12, 18, 3), (25, 12, 2.5), (27, 30, 3.5), (14, 38, 2.5), (22, 44, 2)):
        c.create_oval(*P(sx - r, sy - r, sx + r, sy + r), fill=color, outline="", tags=tags)
    if progress > 0.3:
        c.create_line(*P(8, 26, 13, 22, 17, 27, 21, 23), fill="#57534e", width=max(1, 1.5 * k), tags=tags)
    if progress > 0.6:
        c.create_line(*P(21, 23, 25, 28, 29, 24, 33, 27), fill="#57534e", width=max(1, 1.5 * k), tags=tags)
    if classic and not royal:  # silver egg: a famous strategy
        c.create_line(*P(5, 30, 35, 30), fill="#e2e8f0", width=max(1, 3 * k), tags=tags)
        c.create_text(*P(20, 22), text="\u272a", fill="#475569", font=("", max(7, int(9 * k)), "bold"), tags=tags)
    if royal:  # royal egg: gold band and a little crown mark
        c.create_line(*P(5, 30, 35, 30), fill="#facc15", width=max(1, 3 * k), tags=tags)
        c.create_text(*P(20, 22), text="\u265b", fill="#a16207", font=("", max(7, int(9 * k)), "bold"), tags=tags)
        if phase and phase % 10 < 3:
            c.create_text(*P(33, 6), text="\u2726", fill="#fde047", font=("", max(6, int(7 * k))), tags=tags)
    if progress > 0.85:
        c.create_oval(*P(15, 14, 18, 17), fill="#111827", outline="", tags=tags)  # someone peeking out
        c.create_oval(*P(22, 14, 25, 17), fill="#111827", outline="", tags=tags)
    _ = cx


def draw_dragon(c, x, y, size, color, awake=True, phase=0, mood="plain", fire=False, flash=False, gold=False, tags=(),
                crown=False, pal=None):
    """A small dragon on a Tk canvas, inside a size x size box at (x, y). Awake dragons flap their wings
    and look around; sleeping ones curl up with their eyes shut. fire = a little puff of flame."""
    k = size / 26.0

    def mk(kind, *a, **kw):
        return getattr(c, "create_" + kind)(*a, tags=tags, **kw)

    def P(*pts):
        return [v * k + (x if i % 2 == 0 else y) for i, v in enumerate(pts)]
    pal = pal or {}
    belly = "#fde68a" if gold else pal.get("belly", "#f5f5f4")
    edge = "white" if flash else pal.get("outline", "")
    wing = pal.get("wing", color)
    horn = pal.get("horn", belly)
    eye = pal.get("eye", "#111827")
    flap = (math.sin(phase / 1.6) * 5) if awake else 0
    # tail
    mk('line', *P(6, 20, 1, 22, -1, 17, 1, 14), fill=color, width=max(1, 2 * k), smooth=True)
    mk('polygon', *P(1, 14, -2, 12, 2, 11), fill=color, outline="")
    # far wing (flaps)
    if awake:
        mk('polygon', *P(9, 12, 3, 2 - flap, 8, 5 - flap / 2, 14, 10), fill=wing, outline=edge, stipple="gray75")
    else:
        mk('polygon', *P(8, 13, 6, 9, 14, 11), fill=color, outline="")
    # body and belly
    mk('oval', *P(4, 11, 21, 25), fill=color, outline=edge, width=1)
    mk('oval', *P(9, 16, 19, 24), fill=belly, outline="")
    # legs
    mk('line', *P(8, 23, 7, 26), fill=color, width=max(1, 2 * k))
    mk('line', *P(16, 23, 17, 26), fill=color, width=max(1, 2 * k))
    hy = 0 if awake else 6  # sleeping dragons rest their head low
    look = math.sin(phase / 5.0) * 1.2 if awake else 0
    # neck and head
    mk('polygon', *P(15, 14, 18, 8 + hy, 21, 9 + hy, 19, 16), fill=color, outline="")
    mk('oval', *P(16 + look, 3 + hy, 26 + look, 11 + hy), fill=color, outline=edge)
    # horns
    mk('line', *P(18 + look, 4 + hy, 17 + look, 0 + hy), fill=horn, width=max(1, 1.5 * k))
    mk('line', *P(21 + look, 3 + hy, 21 + look, -1 + hy), fill=horn, width=max(1, 1.5 * k))
    # eye
    if awake:
        mk('oval', *P(21 + look, 5 + hy, 23.5 + look, 7.5 + hy), fill="white", outline="")
        mk('oval', *P(22 + look, 5.6 + hy, 23.3 + look, 7 + hy), fill=eye, outline="")
    else:
        mk('line', *P(20.5, 6.5 + hy, 23.5, 6.5 + hy), fill="#111827", width=max(1, 1.2 * k))
    if mood == "happy" and awake:
        mk('line', *P(23 + look, 9 + hy, 25 + look, 8.5 + hy), fill="#111827", width=1)
    # near wing
    if awake:
        mk('polygon', *P(10, 13, 6, 3 + flap, 12, 6 + flap / 2, 16, 12), fill=wing, outline=pal.get("outline", "#111827"), width=1)
    if crown:
        mk('polygon', *P(17 + look, 2 + hy, 17.5 + look, -3 + hy, 19.5 + look, -0.5 + hy, 21.5 + look, -4 + hy,
                         23.5 + look, -0.5 + hy, 25 + look, -3 + hy, 25 + look, 2 + hy), fill="#facc15", outline="#a16207")
    if fire:
        f = (phase % 3)
        mk('polygon', *P(26 + look, 7 + hy, 31 + f, 5 + hy, 30 + f, 7 + hy, 33 + f, 8 + hy, 26 + look, 9 + hy),
                         fill="#f97316", outline="#fde047")



THEMES = {  # themed dragons: palette per type
    "fire": dict(body="#dc2626", belly="#fbbf24", horn="#111827", wing="#7f1d1d", eye="#fde047"),
    "ice": dict(body="#7dd3fc", belly="#f0f9ff", horn="#e0f2fe", wing="#0ea5e9", eye="#0c4a6e"),
    "lightning": dict(body="#facc15", belly="#fef9c3", horn="#1e3a8a", wing="#2563eb", eye="#1e3a8a"),
    "space": dict(body="#3730a3", belly="#a78bfa", horn="#e9d5ff", wing="#1e1b4b", eye="#f0abfc"),
    "kami": dict(body="#1c1917", belly="#f97316", horn="#fbbf24", wing="#7c2d12", eye="#fde047", outline="#ea580c"),
    "wind": dict(body="#2dd4bf", belly="#ccfbf1", horn="#134e4a", wing="#0d9488", eye="#f8fafc"),
    "skeleton": dict(body="#1f2937", belly="#1f2937", horn="#e7e5e4", wing="#374151", eye="#4ade80", outline="#e7e5e4"),
}
KAMI_MARK = "\u2604 "  # cards of the kamikaze breed are drawn by passing the strategy name with this mark in front
FAMILY_THEME = {  # strategy family -> dragon type
    "SMA crossover": "fire", "EMA crossover": "fire", "MACD": "fire", "Momentum": "fire",
    "RSI": "ice", "Stochastic": "ice", "Bollinger bands": "ice", "Mean reversion": "ice", "VWAP snap-back": "ice",
    "Breakout": "lightning", "Volume breakout": "lightning", "ATR channel": "lightning",
    "Trend + dip": "space", "Dollar-cost averaging": "space",
    "Supertrend": "lightning", "Ichimoku cross": "space", "Rate of change": "fire", "MA ribbon": "fire",
    "Micro pullback": "wind", "Volume burst": "wind", "VWAP reclaim": "wind", "Range scalp": "wind", "Quick flip": "wind",
}
THEME_NAMES = {"fire": "Fire dragon (trend follower)", "ice": "Ice dragon (buys dips)",
               "lightning": "Lightning dragon (breakouts)", "space": "Space dragon (dips in an uptrend)",
               "wind": "Wind dragon (scalper: in and out within minutes)",
               "kami": "Kamikaze dragon (all-or-nothing bets, tests risky rules for the farm)",
               "skeleton": "Skeleton dragon (resting: not winning right now)"}
CHARACTER_MODE = {"mode": "Themed dragons"}  # or "Classic dragons" (coloured by status)


def theme_for(strat, status=None):
    if CHARACTER_MODE["mode"] != "Themed dragons":
        return None
    if status == "benched":
        return "skeleton"
    if str(strat).startswith(KAMI_MARK):
        return "kami"
    return FAMILY_THEME.get(strat, "fire")


def draw_creature(c, strat, x, y, size, color, status=None, **kw):
    """The bot's character. Themed mode: the dragon type comes from the strategy family (resting bots are
    skeletons) and its status colour glows on the ground under it. Classic mode: a dragon in the status colour."""
    th = theme_for(strat, status)
    tags = kw.get("tags", ())
    k = size / 26.0
    if th is None:
        return draw_dragon(c, x, y, size, color, **kw)
    if color:
        c.create_oval(x + 1 * k, y + 24 * k, x + 24 * k, y + 29 * k, fill=color, outline="", stipple="gray50", tags=tags)
    pal = THEMES[th]
    awake, phase = kw.get("awake", True), kw.get("phase", 0)
    draw_dragon(c, x, y, size, pal["body"], pal=pal, **kw)

    def P(*pts):
        return [v * k + (x if i % 2 == 0 else y) for i, v in enumerate(pts)]
    if th == "fire":  # devil tail tip and rising embers
        c.create_polygon(*P(-3, 12, 0, 15, 2, 11), fill="#111827", outline="", tags=tags)
        if awake:
            for n in range(3):
                t = (phase + n * 4) % 12
                c.create_oval(*P(6 + n * 6, 10 - t, 7.5 + n * 6, 11.5 - t), fill="#f97316" if t < 8 else "#fde047",
                              outline="", tags=tags)
    elif th == "ice":  # drifting snowflakes
        for n in range(2 if awake else 1):
            t = (phase + n * 7) % 14
            c.create_text(*P(3 + n * 15, -2 + t), text="\u2744", fill="#e0f2fe", font=("", max(6, int(6 * k))), tags=tags)
    elif th == "lightning":  # a crackle of lightning now and then
        if awake and phase % 6 < 2:
            c.create_line(*P(4, -4, 1, 2, 5, 2, 2, 8), fill="#fde047", width=max(1, 1.5 * k), tags=tags)
        if awake and phase % 6 == 3:
            c.create_line(*P(27, -2, 24, 3, 28, 3, 25, 9), fill="#93c5fd", width=max(1, 1.5 * k), tags=tags)
    elif th == "kami":  # the kamikaze breed: a burning tail and sparks
        f_ = phase % 4
        c.create_polygon(*P(-6 - f_, 12, 0, 9, 1, 15), fill="#f97316", outline="#fde047", tags=tags)
        if awake:
            for n in range(2):
                t_ = (phase + n * 5) % 10
                c.create_oval(*P(2 + n * 4, 8 - t_, 3.6 + n * 4, 9.6 - t_), fill="#fbbf24", outline="", tags=tags)
    elif th == "wind":  # the scalp breed: speed streaks rushing past
        n_ = 3 if awake else 1
        for n in range(n_):
            t = (phase * 3 + n * 5) % 6
            yy = 8 + n * 6
            c.create_line(*P(1 - t, yy, 7 - t, yy), fill="#99f6e4", width=max(1, 1.2 * k), tags=tags)
    elif th == "space":  # twinkling stars and a tiny orbiting moon
        for n, (sx, sy) in enumerate(((2, 2), (25, 18), (3, 24))):
            if (phase + n * 3) % 8 < 5:
                c.create_text(*P(sx, sy), text="\u2726", fill="#f0abfc", font=("", max(5, int(5 * k))), tags=tags)
        if awake:
            a = phase / 4.0
            c.create_oval(*P(13 + 13 * math.cos(a) - 1.5, 15 + 5 * math.sin(a) - 1.5,
                             13 + 13 * math.cos(a) + 1.5, 15 + 5 * math.sin(a) + 1.5), fill="#e9d5ff", outline="", tags=tags)
    elif th == "skeleton":  # ribs
        for n in range(3):
            c.create_line(*P(10, 16 + n * 2.5, 18, 16 + n * 2.5), fill="#e7e5e4", width=1, tags=tags)


class Tip:
    """Small hover tooltip."""

    def __init__(self, widget, text):
        self.w, self.text, self.tw = widget, text, None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, _=None):
        if self.tw:
            return
        self.tw = tk.Toplevel(self.w)
        self.tw.wm_overrideredirect(True)
        self.tw.wm_geometry(f"+{self.w.winfo_rootx() + 20}+{self.w.winfo_rooty() + 24}")
        tk.Label(self.tw, text=self.text, bg=LINE if DARK else "#111827", fg="white", padx=8, pady=4,
                 wraplength=280, justify="left").pack()

    def hide(self, _=None):
        if self.tw:
            self.tw.destroy()
            self.tw = None



def read_version():
    try:
        with open(os.path.join(HERE, "VERSION")) as f:
            return f.read().strip()
    except OSError:
        return "0.0.0"


class App:
    def __init__(self, root, tray=False):
        self.tray_mode, self.tray = bool(tray), None  # tray: started in the background (icon by the clock)
        # Background learning (Free roam, Slow farm, Training camp) shares Python with the window. Handing control
        # back more often keeps the window responsive while they crunch numbers (default is every 5 ms).
        sys.setswitchinterval(0.0005)
        self.root = root
        self.version = read_version()
        root.title("SBF")
        root.withdraw()  # stay hidden until the window is sized and centred
        try:  # the little dragon in the title bar and taskbar
            import icon_data
            self._icons = [tk.PhotoImage(data="".join(icon_data.ICON_64)), tk.PhotoImage(data="".join(icon_data.ICON_32))]
            root.iconphoto(True, *self._icons)
        except Exception:
            pass
        root.geometry("1050x720")
        self.q = queue.Queue()
        self.running = False
        self.client = None
        self.usd_avail = 0.0
        self.balances = {}
        self.last_bal = 0.0
        self.live = False
        self.session = False
        self.logging_in = False
        self.migrated = E.migrate_home()  # first start after the rename: bring the old data folder across
        self.cfg = E.load_json(CONFIG_PATH, {})
        apply_theme(root, bool(self.cfg.get("dark", True)))
        self.dark_var = tk.BooleanVar(value=DARK)
        E.private_dir(E.HOME_DIR)
        E.fill_secrets(self.cfg)
        self.settings = E.merge_settings(self.cfg.get("settings"))
        self.settings["_broker"] = self.cfg.get("broker", "alpaca")
        self.cfg["auto_camp"] = False  # Auto camp too: it only teaches after you switch it on
        self.cfg["roam_on"] = False  # Free roam always starts OFF: it only studies after you switch it on
        wl = self.cfg.get("watchlist")
        if isinstance(wl, list) and wl and all(x in ("BTC", "ETH", "SOL", "XRP", "DOGE", "ADA") for x in wl):
            self.cfg["watchlist"] = ["SPY", "QQQ", "NVDA"]  # a crypto-era default list
        if not self.cfg.get("bg_learning_off"):  # 1.5: no learning runs by itself any more (Free roam only when ON)
            self.settings["retest_nightly"] = False
            self.cfg["bg_learning_off"] = True
            self.cfg["settings"] = self.settings
            E.save_json(CONFIG_PATH, self.cfg)
        CHARACTER_MODE["mode"] = self.settings.get("characters", "Themed dragons")
        if not self.cfg.get("live_feel_v1"):  # one-time move to the faster defaults (1-minute bars, 2-second prices)
            self.settings["bar_minutes"], self.settings["refresh"] = 1, 2
            self.cfg["settings"], self.cfg["live_feel_v1"] = self.settings, True
            try:
                E.save_json(CONFIG_PATH, self.cfg)
            except OSError:
                pass
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.awake = E.AwakeLock()
        self.worker_gen, self.worker_thread, self.worker_args = 0, None, None
        self._err_seen, self._gap_ref, self._stale_sent = {}, None, False
        root.report_callback_exception = self.on_cb_error
        self.reset_state(1000.0)
        self.build_ui()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.protocol("WM_SAVE_YOURSELF", self.on_shutdown)  # Windows is signing out or shutting down
        root.after(200, self.drain)
        root.after(5000, self.watchdog)
        root.after(400, self.auto_login)
        if not self.tray_mode:  # no pop-ups while starting quietly in the background
            root.after(700, self.maybe_welcome)
        root.after(4000, self.start_tg_remote)
        self._pdt_note, self._lapsed = {}, set()
        root.after(30000, self.managed_guard)
        if self.cfg.get("fred_key") and time.time() - E.events_fetched() > 7 * 86400:
            root.after(6000, lambda: self.fetch_calendar(quiet=True))
        root.after(20000, self.start_free_roam)
        if not self.tray_mode:
            root.after(1500, self.maybe_setup)
        root.after(4000, self.auto_check_update)
        root.after(1200, self.restore_last_session)
        root.after(90000, self.hourly)  # weekly backup, daily recap
        root.after(10 * 60 * 1000, self.exam_pass)  # exams for bots that need them, even with Free roam off
        root.after(120000 if self.tray_mode else 2500, self.tray_autotrain if self.tray_mode else self.auto_resume)  # tray: let Windows finish starting first
        self.fit_and_center()
        if self.tray_mode:
            root.withdraw()
            root.after(300, self.tray_boot)

    # ---------------- state ----------------
    def reset_state(self, cash):
        self.start_cash = cash
        self.cash = cash
        self.pos, self.cost = {}, {}
        self.hist, self.last = {}, {}
        self.trades, self.curve = [], []
        self.shadow_trades = []  # every bot's own pretend trades (Wildcard), shown in the Trades tab
        self.mirror, self.mirror_trades = {}, []  # "Both": copies of practice trades on the Alpaca paper account
        self.bench_units = {}
        self.pending = set()
        self.pending_who = {}
        self.pending_detail = {}   # coin -> "why did it trade?" text for the order on its way
        self.pending_voters = {}   # coin -> uids of the bots whose votes made the buy
        self.bot_alloc = {}        # coin -> {bot uid: $ of the open position its votes bought}
        self._trade_rows = {}      # Trades tab row id -> trade
        self.shadow_live = None    # Shadow Live: pretend account that follows only your Hall of Fame bots
        self.shadow_curve = []     # (time, its value)
        self.farm_hist = deque(maxlen=6000)  # farm replay: (time, eggs, rising, beasts, events) per decision
        self.inc = {}
        self.fails = 0
        self.bar_n = 0
        self.last_bar = 0.0
        self.tourn = None
        self.seeded = False
        self.wild = False
        self.active = []
        self.session_coins = []
        self.session_bar = self.settings["bar_minutes"]
        self.day, self.day_eq, self.halted_day = None, None, None
        self.wild_msg, self.last_wild_msg = "", ""
        self.quote = "USD"
        self.start_px, self.last_tick = {}, None
        self.high = {}            # highest price since buying, per coin (trailing stop)
        self.trade_marks = []     # (chart point number, side) for the buy/sell markers
        self.curve_n = 0          # chart points added so far this session
        self.inducted = set()     # Hall of Fame keys added this session
        self.fees_paid = 0.0      # fees paid by the account this session
        self.bought_at = {}       # coin -> time the current position was first bought (minimum hold)
        self.hx = {}              # coin -> {'h','l','v'} lined up with self.hist (highs, lows, volumes per bar)
        self.bar_hl = {}          # coin -> [high, low] of the ticks inside the bar being built
        self.cur_vol = {}         # ticker -> latest volume per bar
        self.opt = {}             # account book -> extra facts about its position (a scalp's own exits: {"scalp": ...})
        self.market = None        # Alpaca market clock (is_open, next_open, next_close)
        self.ctx_hist = {}        # context ticker -> prices lined up with the session's bars (watched, never traded)
        self.tick_px = {}         # coin -> deque of (time, price) for the tick chart
        self.brake_note = 0.0

    def save_state(self, force=True):
        if not self.session:
            return
        if not force:
            if time.time() - getattr(self, "_saved_at", 0) < 30:  # routine saves at most every 30 s (trades save at once)
                if not getattr(self, "_save_pending", False):
                    self._save_pending = True
                    self.root.after(30000, lambda: (setattr(self, "_save_pending", False), self.save_state()))
                return
        self._saved_at = time.time()
        E.save_json(E.state_path(self.sess_tag()), dict(
            start_cash=self.start_cash, cash=self.cash, pos=self.pos, cost=self.cost,
            trades=self.trades[-10000:], shadow=self.shadow_trades[-4000:], curve=self.curve[-8000:], bench_units=self.bench_units,
            coins=self.session_coins, bar_n=self.bar_n, bar=self.session_bar, quote=self.quote, high=self.high,
            fees_paid=self.fees_paid, bought_at=self.bought_at,
            bot_alloc={c: {str(u): x for u, x in a.items()} for c, a in self.bot_alloc.items()}, opt=self.opt,
            shadow_live=None if not self.shadow_live else dict(cash=self.shadow_live.cash, pos=self.shadow_live.pos,
                                                               cost=self.shadow_live.cost, fees=self.shadow_live.fees),
            shadow_curve=self.shadow_curve[-4000:], farm_hist=list(self.farm_hist)[-3000:],
            mirror=getattr(self, "mirror", {}), mirror_trades=list(getattr(self, "mirror_trades", []))[-5000:]))

    def save_farm(self, force=False):
        """Saves the whole farm (every bot, its history and family tree) next to the session, so it carries on
        where it left off. Done every 20 decisions and whenever the bot stops, switches mode or the app closes."""
        if not (self.session and self.tourn and self.seeded):
            return
        if not force and (self.bar_n % 20 or time.time() - getattr(self, "_farm_saved", 0) < 300):
            return  # routine farm saves: every 20 decisions, and never more than once every 5 minutes
        self._farm_saved = time.time()
        try:
            E.save_json(E.farm_path(self.sess_tag()), dict(coins=self.session_coins, bar=self.session_bar,
                                                      saved_at=time.time(), farm=self.tourn.to_state()))
        except Exception:
            note = self._record_error("saving the farm")
            if note:
                self.write(note, "bad")

    def load_farm(self):
        """The saved farm for this session, ready to carry on (or None)."""
        d = E.load_json(E.farm_path(self.sess_tag()))
        if not isinstance(d, dict) or d.get("coins") != self.session_coins or float(d.get("bar", -1)) != float(self.session_bar):
            return None
        t = E.Tournament(self.session_coins, self.start_cash, *E.cost_model(self.settings)[:1],
                         self.settings["trade_pct"] / 100, self.settings["evolve_every"], params=self.settings["params"],
                         opts=E.tournament_opts(self.settings), cap=int(self.settings["max_bots"]),
                         retire_steps=self.retire_steps(self.session_bar))
        try:
            if not t.restore(d.get("farm")):
                return None
            if getattr(t, "scoreboard_reset", False):
                self.q.put(("log", ("This farm's track records were earned on practice prices that were far too generous "
                                    "(fixed in 1.23), so every bot starts a fresh scoreboard: money, results and exams "
                                    "start again, genes and what they learned are kept. Bots have to prove themselves "
                                    "again before they trade the account.", "warn")))
            gone = t.trim_to_cap()  # Free roam could push a farm past its size before 1.21
            if gone:
                self.q.put(("log", (f"Your farm had grown past its size of {t.cap}; {gone} bots that couldn't be judged or "
                                    "were weakest made room.", "info")))
            return t
        except Exception:
            # never lose a farm to a bug: keep a copy of the file before anything could save over it
            try:
                import shutil
                shutil.copy2(E.farm_path(self.sess_tag()), E.farm_path(self.sess_tag()) + f".couldnt-load-{int(time.time())}.json")
            except OSError:
                pass
            self._farm_load_failed = True
            raise

    def restore_last_session(self):
        """On start-up: put the last session (and its farm) back on screen, paused, exactly where it left off."""
        if self.session:
            return
        mode = self.cfg.get("last_mode", "paper")
        if mode == "live":  # SBF always opens in Practice; the saved LIVE session waits behind the LIVE tab
            if self.saved_session(True):
                nm_ = {"alpaca": "ALPACA PAPER"}.get(self.cfg.get("last_tag"), "LIVE")
                self.write(f"Your {nm_} session is saved. SBF opens in Practice; "
                           + ("choose 'Send to my Alpaca paper account' under Practice" if nm_ != "LIVE" else "click LIVE at the top")
                           + " to go back to it.")
            mode = "paper"
        saved = self.saved_session(False)
        if not saved:
            return
        self._auto_resume = True
        try:
            ok = self.begin_session(list(saved.get("coins") or []), quiet=True)
        finally:
            self._auto_resume = False
        if not ok:
            return
        self.wild_var.set(bool(self.cfg.get("last_wild", True)))
        self.toggle_wild()
        if self.wild_var.get():
            t = self.load_farm()
            if t:
                self.tourn, self.seeded, self._restored_farm = t, False, t
                self.refresh_board()
        self.update_status()
        self.refresh_view()
        self.write(f"Welcome back: your {'LIVE' if mode == 'live' else 'Practice'} session is just as you left it"
                   + (f", with all {len(self.tourn.vars)} bots" if self.tourn else "") + ". Press Continue to carry on.", "good")

    def load_state(self, s):
        self.start_cash, self.cash = s["start_cash"], s["cash"]
        self.pos, self.cost = s["pos"], s["cost"]
        self.trades, self.curve = s["trades"], [tuple(x) for x in s["curve"]]
        self.shadow_trades = [r for r in s.get("shadow", []) if isinstance(r, dict)]
        self.bench_units, self.session_coins = s["bench_units"], s["coins"]
        self.bar_n, self.session_bar = s["bar_n"], s.get("bar", 5)
        self.quote = s.get("quote", "USD")
        self.high = {c: float(v) for c, v in (s.get("high") or {}).items() if isinstance(v, (int, float))}
        self.fees_paid = float(s.get("fees_paid", 0) or 0)
        self.bought_at = {c: float(t) for c, t in (s.get("bought_at") or {}).items() if isinstance(t, (int, float))}
        self.opt = {c: dict(o) for c, o in (s.get("opt") or {}).items() if self.pos.get(c, 0) > 0}
        self.mirror = {c: dict(m) for c, m in (s.get("mirror") or {}).items() if not m.get("pending")}
        for m in self.mirror.values():
            m.pop("closing", None)
        self.mirror_trades = [r for r in s.get("mirror_trades") or [] if isinstance(r, dict)]
        try:
            self.bot_alloc = {c: {int(u): float(x) for u, x in a.items()} for c, a in (s.get("bot_alloc") or {}).items()}
        except (TypeError, ValueError, AttributeError):
            self.bot_alloc = {}
        try:
            sl = s.get("shadow_live")
            if sl:
                self.shadow_live = E.Sim(self.start_cash, 0.006, self.settings["trade_pct"] / 100)
                self.shadow_live.cash, self.shadow_live.fees = float(sl["cash"]), float(sl.get("fees", 0))
                self.shadow_live.pos = {c: float(q) for c, q in sl["pos"].items()}
                self.shadow_live.cost = {c: float(q) for c, q in sl["cost"].items()}
            self.shadow_curve = [tuple(x) for x in s.get("shadow_curve") or []]
            self.farm_hist = deque([tuple(x) for x in s.get("farm_hist") or []], maxlen=6000)
        except (TypeError, ValueError, KeyError, AttributeError):
            self.shadow_live, self.shadow_curve = None, []

    def on_close(self):
        if self.tray is not None and not getattr(self, "_quitting", False):
            return self.hide_to_tray()  # the X hides the window; Quit (in the File menu or on the icon) quits
        quiet = getattr(self, "_quitting", False) == "tray" and not (self.running and self.live)
        self._quitting = False
        if self.running and not quiet and not messagebox.askyesno(
                "Quit", "The bot is running. Quitting stops it (open positions stay open).\n\nQuit anyway?"):
            return
        self.cfg["was_running"] = bool(self.running)  # closed while running: pick it up next time
        self.save_farm(force=True)
        try:
            self.slow_save()
        except Exception:
            E.note_error("on_close")
        self.running = False
        self.awake.off()
        try:
            self.cfg["size"] = f"{self.root.winfo_width()}x{self.root.winfo_height()}"
            E.save_json(CONFIG_PATH, self.cfg)
        except Exception:
            E.note_error("on_close")
        self.save_state()
        if self.tray is not None:
            self.tray.stop()
        self._gone = True
        self.root.destroy()

    def on_shutdown(self):
        """The computer is shutting down: save the farm and the session right now, and leave the "was running" mark,
        so the bots carry on from this exact point next time."""
        try:
            self.save_farm(force=True)
            self.slow_save()
        except Exception:
            E.note_error("on_shutdown")
        try:
            self.cfg["was_running"] = bool(self.running)
            E.save_json(CONFIG_PATH, self.cfg)
            self.save_state()
        except Exception:
            E.note_error("on_shutdown")

    LIQUID = ("SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "TSLA", "META", "AMD")

    def thin_check(self):
        """Once a day: a ticker that went without trades in a fifth or more of its recent bars is a thin market. Its
        prices jump between few trades, which fools the bots and fills badly; suggest well-traded replacements."""
        if not (self.running and self.session_coins) or time.time() - float(self.cfg.get("thin_check_t", 0) or 0) < 86400:
            return
        thin = []
        for c in self.session_coins:
            px = list(self.hist.get(c) or [])[-1441:]
            vol = list((self.hx.get(c) or {}).get("v") or [])[-1440:]
            if len(px) < 300:
                continue
            n = len(px) - 1
            vol = [None] * (n - len(vol)) + vol[-n:]
            # a bar with no trades: no volume, or not even a tick of price movement
            share = sum(1 for i in range(1, len(px)) if px[i] == px[i - 1] or (vol[i - 1] is not None and vol[i - 1] <= 0)) / n
            if share >= 0.2:
                thin.append((c, share))
        if not thin:
            return
        self.cfg["thin_check_t"] = time.time()
        try:
            E.save_json(CONFIG_PATH, self.cfg)
        except OSError:
            pass
        alt = [x for x in self.LIQUID if x not in self.session_coins][:4]
        what = ", ".join(f"{c} (no trades in {sh * 100:.0f}% of its recent bars)" for c, sh in thin)
        msg = (f"Thin market: {what}. Prices jump between few trades there, which fools the bots and fills badly. "
               f"Well-traded tickers to consider instead: {', '.join(alt)}.")
        self.write(msg, "warn")
        if self.tray is not None:
            self.tray.notify(msg[:250], "Thin market")

    def essentials_block(self, what):
        """True if Essentials only is on and stays on (asks first); False if it's off or you switch it off."""
        if not self.cfg.get("essentials"):
            return False
        if messagebox.askyesno("Essentials only is on", f"{what} downloads extra history and works the disk in the "
                               "background, so Essentials only keeps it off.\n\nSwitch Essentials only off and start it?"):
            self.toggle_essentials(False)
            return False
        return True

    def toggle_essentials(self, on=None):
        on = (not self.cfg.get("essentials")) if on is None else bool(on)
        self.cfg["essentials"] = on
        E.save_json(CONFIG_PATH, self.cfg)
        if on:
            stopped = []
            if self.cfg.get("roam_on"):
                self.toggle_roam(False)
                stopped.append("Free roam")
            if self.cfg.get("auto_camp"):
                self.toggle_auto_camp(False)
                stopped.append("Auto camp")
            if self.cfg.get("slow_on") and hasattr(self, "slow_toggle"):
                self.slow_toggle()
                stopped.append("the Slow farm (it keeps what it learned)")
            if self.cfg.get("lab_on"):
                self.toggle_lab()
                stopped.append("the Scalp lab")
            self.write("Essentials only ON: only normal trading and its prices run. "
                       + (f"Stopped {', '.join(stopped)}. " if stopped else "")
                       + "Background exams, the economic calendar refresh and weekly backups are paused too. "
                       "Press the button again to allow them.", "good")
        else:
            self.write("Essentials only OFF: Free roam, the Slow farm and the other extras can run again (each still "
                       "only runs when you switch it on; background exams and weekly backups resume by themselves).")
        self.paint_essentials()

    def paint_essentials(self):
        if not hasattr(self, "ess_btn"):
            return
        on = bool(self.cfg.get("essentials"))
        self.ess_btn.config(text="\u25cf Essentials only: ON" if on else "\u25cb Essentials only",
                            bg=GREEN if on else LINE, fg="#0b1220" if on else TEXT,
                            activebackground=GREEN if on else LINE, activeforeground="#0b1220" if on else TEXT)

    AUTO_CAMP_MAX = 1500  # most farm decisions in one Auto camp stretch

    def toggle_auto_camp(self, on=None):
        on = (not self.cfg.get("auto_camp")) if on is None else bool(on)
        if on and self.essentials_block("Auto camp"):
            return
        self.cfg["auto_camp"] = on
        E.save_json(CONFIG_PATH, self.cfg)
        if hasattr(self, "camp_btn"):
            self.camp_btn.config(text="\u2694 Auto camp: ON" if on else "\u2694 Auto camp")
        if on:
            self.write("\u2694 Auto camp ON: while the farm runs, practice farms live through stretches of past prices it "
                       f"picks itself (at most {self.AUTO_CAMP_MAX:,} decisions each), and your farm learns from every "
                       "one." + ("" if self.tourn else " Start the farm (Wildcard) and it begins."), "good")
            th = getattr(self, "_camp_worker", None)
            if th is None or not th.is_alive():
                self._camp_worker = threading.Thread(target=self._auto_camp_loop, daemon=True, name="auto camp")
                self._camp_worker.start()
        else:
            self.write("\u2694 Auto camp OFF: it stops the stretch in progress; nothing learns in the background.")

    def _auto_camp_loop(self):
        """Auto camp, one stretch after another while it's ON and the farm runs (Free roam, when on, runs its own)."""
        self._camp_thread = threading.get_ident()
        quiet = (E.NoHistory, E.TrainingStopped) + ((E.NeedLogin,) if hasattr(E, "NeedLogin") else ())
        try:
            while self.cfg.get("auto_camp") and not self.cfg.get("essentials"):
                t = self.tourn
                if not (self.running and self.wild and t and self.seeded) or self.cfg.get("roam_on"):
                    time.sleep(20)
                    continue
                pause = 60
                try:
                    self._auto_camp_once(t)
                except quiet:
                    pause = 120  # nothing to study right now (offline, a gap in the data): try again shortly
                except Exception:
                    self._record_error("auto camp")
                    pause = 300
                if not self.cfg.get("roam_on"):
                    self.roam_now = None  # (the stretch picker shares Free roam's status line)
                for _ in range(pause):  # a breather between stretches, so the app stays light
                    if not self.cfg.get("auto_camp"):
                        break
                    time.sleep(1)
        finally:
            self._camp_thread = None

    def _camp_trim(self, closes, extra, info, n):
        """Keeps the latest n candles of a stretch (every coin and context series alike)."""
        L = min(len(x) for x in closes.values())
        if L <= n:
            return closes, extra, info
        closes = {c: list(x[-n:]) for c, x in closes.items()}
        if extra:
            extra = {c: {k: (list(v[-n:]) if isinstance(v, (list, tuple)) and len(v) >= n else v) for k, v in d.items()}
                     if isinstance(d, dict) else d for c, d in extra.items()}
        info = dict(info, bars=n)
        ts = next((d.get("t") for d in (extra or {}).values() if isinstance(d, dict) and d.get("t")), None)
        if ts:
            info["start"] = ts[0]
        return closes, extra, info

    def _auto_camp_once(self, t):
        import random as _r
        closes, extra, info = self._roam_pick(_r.Random(), 400, prefer=self.farm_gran())
        closes, extra, info = self._camp_trim(closes, extra, info, self.AUTO_CAMP_MAX)
        lead = list(t.active[:4]) or [v for v, _ in t.ranking()][:4] or list(t.vars[:4])
        seeds = [E._genes(v) for v in lead]
        opts = E.tournament_opts(self.settings)
        if hasattr(E, "opts_for_bars"):
            opts = E.opts_for_bars(opts, info["gran"])
        fee, slip = E.cost_model(self.settings)
        self.camp_status = f"Auto camp: a practice farm is living through {self._when(info['start'], True)} ({info['bars']:,} candles)"
        res = E.train_farm(closes, extra, opts, cap=20, passes=1, exam_frac=0.25, seed_genes=seeds,
                           start_cash=self.start_cash, fee=fee, pct=self.settings["trade_pct"] / 100,
                           evolve=int(self.settings["evolve_every"]), params=self.settings["params"],
                           stop=lambda: self.tourn is not t or not self.cfg.get("auto_camp") or bool(self.cfg.get("essentials")))

        def apply():
            if self.tourn is not t:
                return
            E.teach_from(t, res["tourn"])
            passed = [r for r in res["results"] if r["passed"]]
            grad = ""
            nursery = getattr(t, "nursery_full", lambda: False)()
            if passed and self.seeded and len(t.vars) < t.cap + 2 and not nursery and not info.get("revisit"):
                g = passed[0]["genes"]
                v = t.add_variant(g["name"], g["params"], {c: self.hist[c] for c in self.session_coins}, 1.0,
                                  note="Auto camp graduate:", tf=g["tf"], origin="training camp", risk=g["risk"],
                                  traits=g["traits"], vetoes=g["vetoes"], layers=g["layers"])
                if v:
                    v.layer_stats = {k: list(x) for k, x in passed[0]["layer_stats"].items()}
                    grad = E.bot_nickname(v.key())
                    self.refresh_board()
            self.cfg["auto_camps"] = int(self.cfg.get("auto_camps", 0) or 0) + 1
            self.camp_status = ""
            visit = " (a repeat visit: it only teaches)" if info.get("revisit") else ""
            self.write(f"\u2694 Auto camp: a practice farm lived through {self._when(info['start'], True)} to "
                       f"{self._when(info['end'], True)} ({info['bars']:,} decisions){visit}; {len(passed)} passed the exam. "
                       "Your farm learned its lessons" + (f", and {grad} joined your farm." if grad else "."))
            self.save_farm(force=True)
        self.q.put(("call", apply))

    def exam_pass(self):
        """Every 20 minutes while the farm runs and Free roam is OFF: up to 3 bots that still need exams are tested on
        a stretch of history none of them trained on, at their own candle size. It's only an exam: the bots learn
        nothing from it (Free roam does the learning), it only records how they did on unseen prices. A stretch is
        downloaded once and kept on disk; without a connection it simply waits for the next round."""
        self.root.after(20 * 60 * 1000, self.exam_pass)
        t = self.tourn
        if (not self.settings.get("auto_exams", True) or self.cfg.get("roam_on") or self.cfg.get("essentials")
                or getattr(self, "_exam_busy", False)
                or not (self.running and self.wild and t and self.seeded)):
            return
        ranked = [v for v, _ in t.ranking()]
        need = [v for v in list(dict.fromkeys(list(t.active) + ranked[:25]))
                if not E.is_kami(v) and E.exam_verdict(v, t.serial)[0] == "untested"]
        picks = need[:3]
        if not picks:
            return
        self._exam_busy = True
        fee, slip = E.cost_model(self.settings)
        pct, cash, gran = self.settings["trade_pct"] / 100, self.start_cash, self.farm_gran()
        names = [E.bot_nickname(v.key()) for v in picks]

        def work():
            import random as _r
            self._exam_thread = threading.get_ident()
            try:
                closes, extra, info = self._roam_pick(_r.Random(), prefer=gran)
                if info.get("revisit") or info.get("gran") != gran:
                    return  # only first visits at their own candle size count as exams
                out = [(v, E.backtest_bot(v, closes, extra, cash, fee, pct, slip)) for v in picks]

                def apply():
                    if self.tourn is not t:
                        return
                    done = []
                    for (v, r), nm in zip(out, names):
                        if v in t.vars:
                            E.add_exam(v, "self-test", r["edge"] * 100, r.get("closed", r["trades"]), info["start"], info["gran"])
                            done.append(f"{nm} {r['edge'] * 100:+.1f}% vs holding")
                    if done:
                        t._rate()
                        self.write("Exams on prices they never saw (" + self._when(info["start"], True) + "): "
                                   + ", ".join(done) + ".")
                self.q.put(("call", apply))
            except Exception:
                pass  # offline, no stretch with history, or the farm changed: try again next round
            finally:
                self._exam_thread = None
                self._exam_busy = False
                if not self.cfg.get("roam_on"):
                    self.roam_now = None
        threading.Thread(target=work, daemon=True, name="exam pass").start()

    def hourly(self):
        """Light housekeeping once an hour: the weekly backup, the daily recap, and (in the background) the update check."""
        for fn in (self.auto_backup, self.daily_recap, self.thin_check):
            try:
                fn()
            except Exception:
                E.note_error("hourly")
        if self.tray_hidden():
            try:
                self.auto_check_update()
            except Exception:
                E.note_error("hourly")
        self.root.after(3600 * 1000, self.hourly)

    def recap_base(self):
        beasts = sum(1 for r in self.tourn.board() if self.tier(r) == "beast") if self.tourn else 0
        return dict(day=date.today().isoformat(), eq=self.equity(), hold=self.bench_equity(), beasts=beasts,
                    live=bool(self.live), sid=[float(self.start_cash), sorted(self.session_coins)])

    def daily_recap(self):
        """Once a day: how the bots did yesterday against the market (a note on the icon, and in the log)."""
        if not self.settings.get("daily_recap", True) or not (self.session and self.running):
            return
        base = self.cfg.get("recap_base")
        today = date.today().isoformat()
        if isinstance(base, dict) and base.get("day") == today and base.get("live") == bool(self.live):
            return
        if not self.last_tick or (datetime.now() - self.last_tick).total_seconds() > 600:
            return  # no fresh prices (not logged in, offline, market closed): values would be wrong, try later
        now = self.recap_base()
        if isinstance(base, dict) and base.get("sid") != now["sid"]:
            base = None  # a different session since (reset, new money or coins): start a fresh recap from here
        if isinstance(base, dict) and base.get("live") == bool(self.live) and base.get("eq"):
            got = (now["eq"] / base["eq"] - 1) * 100
            hold = (now["hold"] / base["hold"] - 1) * 100 if base.get("hold") else 0.0
            new = now["beasts"] - int(base.get("beasts", 0))
            where = "LIVE" if self.live else "practice"
            msg = (f"Since {base['day']}: the bots made {got:+.2f}% on {where} vs {hold:+.2f}% for the market"
                   + (f", {new} new BEAST{'S' if new != 1 else ''}" if new > 0 else "") + ".")
            self.write("Daily recap: " + msg, "good" if got >= hold else None)
            if self.tray is not None:
                self.tray.notify(msg, f"{APP_SHORT} daily recap")
        self.cfg["recap_base"] = now
        try:
            E.save_json(CONFIG_PATH, self.cfg)
        except OSError:
            pass

    def live_checks(self):
        """(passed, label, detail) evidence that the practice farm is ready for real money."""
        t, out = self.tourn, []
        if not (self.session and t and t.vars):
            return [(False, "A practice farm is running", "Start the practice bot with Wildcard on and let it learn.")]
        live_now = bool(self.live)
        out.append((not live_now, "You're looking at the practice farm", "Switch to Practice to judge it." if live_now else "Yes."))
        out.append((t.n >= 2000, "The farm has had time to learn", f"{t.n:,} decisions so far (want 2,000+)."))
        act = list(t.active)
        out.append((bool(act), "There are Winning bots steering the account", f"{len(act)} Winning right now."))
        top = act[0] if act else None
        if top:
            d = t.info.get(id(top), {})
            out.append((d.get("edge") is not None and d["edge"] > 0, "The top bot beats the market",
                        f"{(d.get('edge') or 0) * 100:+.1f}% vs the market over its recent window."))
            n = len(top.sim.tbars)
            out.append((E.luck_factor(top) >= 0.75 if hasattr(E, "luck_factor") else n >= 20,
                        "The top bot's record rests on enough trades", f"{n} trades in its window (want about 20+)."))
            out.append((top.twin is not None and top.twin > 0, "The top bot passed its twin test",
                        "Not tested yet." if top.twin is None else f"{top.twin * 100:+.1f}% vs the market on older prices."))
            dd = t.drawdown(top) * 100
            out.append((dd < 10, "The top bot isn't in a deep slump", f"{dd:.1f}% below its recent best (want under 10%)."))
        pl, hold = self.equity() - self.start_cash, self.bench_equity() - self.start_cash
        out.append((pl > 0 and pl >= hold, "The practice account is beating the market",
                    f"{pl:+,.2f} vs {hold:+,.2f} for the market this session."))
        return out

    def open_live_check(self):
        """Account > Ready for LIVE?: a plain checklist of evidence, not a promise."""
        w = tk.Toplevel(self.root)
        w.title("Ready for LIVE?")
        w.configure(bg=BG)
        w.transient(self.root)
        f = ttk.Frame(w, padding=16)
        f.pack(fill="both", expand=True)
        checks = self.live_checks()
        good = sum(1 for c in checks if c[0])
        verdict = ("Looks ready to try LIVE with a small amount." if good == len(checks) else
                   "Not yet: keep it training on practice." if good < len(checks) - 2 else
                   "Close. Give it more time on practice.")
        ttk.Label(f, text=f"{good} of {len(checks)} checks passed. {verdict}", font=("", 12, "bold")).pack(anchor="w")
        for ok, label, detail in checks:
            row = ttk.Frame(f)
            row.pack(fill="x", pady=3)
            tk.Label(row, text="\u2713" if ok else "\u2717", fg=GREEN if ok else RED, bg=BG, font=("", 13, "bold"),
                     width=2).pack(side="left", anchor="n")
            col = ttk.Frame(row)
            col.pack(side="left", fill="x")
            ttk.Label(col, text=label, font=("", 10, "bold")).pack(anchor="w")
            ttk.Label(col, text=detail, foreground=MUTED, wraplength=440).pack(anchor="w")
        ttk.Label(f, text="Past results never guarantee future ones. Start LIVE small and keep the safety stops on.",
                  foreground=MUTED, wraplength=470).pack(anchor="w", pady=(10, 4))
        ttk.Button(f, text="Close", command=w.destroy).pack(anchor="e")

    # ---------------- background mode (icon by the clock) ----------------
    def tray_lib(self):
        try:
            import tray as T
            return T
        except Exception:
            return None

    def go_background(self):
        """File > Run in the background: hide the window to an icon by the clock (installs the icon add-on once)."""
        T = self.tray_lib()
        if T is None:
            return messagebox.showerror("Background mode", "tray.py is missing from the SBF folder. Updating SBF puts it back.")
        if self.tray is not None:
            return self.hide_to_tray()
        if T.tray_ready():
            return self.start_tray(hide=True)
        if not messagebox.askyesno("Background mode", "Background mode needs two small add-ons (pystray and pillow) "
                                                      "for the icon by the clock.\n\nInstall them now?"):
            return
        self.write("Installing the add-ons for background mode...")

        def work():
            ok, msg = E.install_packages(T.TRAY_PIP, lambda line: None)
            self.q.put(("call", lambda: self.start_tray(hide=True) if ok and T.tray_ready() else
                        self.write("Background mode couldn't be set up: " + msg, "bad")))
        threading.Thread(target=work, daemon=True).start()

    def tray_boot(self):
        """Started with --tray (for example when Windows starts): icon by the clock, no window."""
        T = self.tray_lib()
        if T is None or not T.tray_ready():
            self.tray_mode = False
            self.root.deiconify()
            return self.write("Background mode needs its add-ons: use File > Run in the background once to set it up.", "warn")
        self.start_tray(hide=True, quiet=True)

    def start_tray(self, hide=True, quiet=False):
        T = self.tray_lib()
        if self.tray is None:
            import icon_data
            acts = dict(show=self.show_window, toggle=self.tray_toggle, live=self.tray_live,
                        autostart=self.tray_autostart, quit=self.tray_quit)
            self.tray = T.Tray("SBF", "".join(icon_data.ICON_64), lambda fn: self.q.put(("call", fn)),
                               lambda: getattr(self, "_tray_st", {}), acts)
            try:
                self.tray_tick(loop=False)
                self.tray.start()
            except Exception as e:
                self.tray = None
                self.root.deiconify()
                return self.write(f"Background mode couldn't start its icon ({e}).", "bad")
            self.root.after(10000, self.tray_tick)
        if hide:
            self.hide_to_tray(quiet=quiet)

    def bg_workers(self):
        """Processor cores for the start-up warm-up: just one while running as the icon (barely noticeable),
        all of them with the window open. Safe to call from background threads (reads a plain flag)."""
        return 1 if getattr(self, "_in_tray", False) else None

    def hide_to_tray(self, quiet=False):
        self._in_tray = True
        self.root.withdraw()
        self.apply_awake()
        if not quiet and not getattr(self, "_tray_told", False):
            self._tray_told = True
            self.tray.notify("SBF keeps working here. Click the icon to open it, right-click for options.")
        self.write("Running in the background (icon by the clock).")

    def show_window(self):
        self._in_tray = False
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except Exception:
            pass
        self.apply_awake()
        self.on_restore()

    def tray_hidden(self):
        try:
            return self.tray is not None and self.root.state() == "withdrawn"
        except Exception:
            return False

    def tray_tick(self, loop=True):
        """Keeps the icon's tooltip and menu up to date (every 10 seconds, cheap)."""
        T = self.tray_lib()
        live = bool(self.running and self.live)
        if self.running and self.session:
            pl = self.equity() - self.start_cash
            line = (f"LIVE bot running, {pl:+,.2f}" if live else f"Training on practice prices, {pl:+,.2f}")
        else:
            line = "Paused"
        self._tray_st = dict(line=line, live=bool(self.session and self.live),
                             toggle=("Stop the LIVE bot" if live else "Pause training") if self.running else
                             ("Start the LIVE bot..." if self.session and self.live else "Resume training"),
                             autostart=bool(T and T.autostart_enabled("SBF")))
        if self.tray is not None:
            self.tray.set_title(f"SBF: {line}")
            self.tray.refresh_menu()
            if loop:
                self.root.after(10000, self.tray_tick)

    def tray_autotrain(self):
        """Background start: train on practice prices. A LIVE session is never started from here."""
        if self.cfg.get("last_mode") == "live" and self.cfg.get("was_running") and self.tray is not None:
            self.tray.notify("Your LIVE bot was running when SBF last closed. It stays paused until you choose "
                             "Go LIVE on this icon (you'll type LIVE).")
        if self.running or (self.session and self.live) or not self.settings.get("tray_autotrain", True):
            return
        if not self.session:
            self.mode_var.set("paper")
            self.wild_var.set(True)
            self.toggle_wild()
        self._auto_resume = True
        try:
            self.toggle()
        finally:
            self._auto_resume = False
        if self.running:
            self.write("Background mode: the bots are training on practice prices.", "good")
        self.tray_tick(loop=False)

    def tray_toggle(self):
        if not self.running and self.session and self.live:
            self.show_window()  # starting LIVE always goes through the window (type LIVE)
        self.toggle()
        self.tray_tick(loop=False)

    def tray_live(self):
        self.show_window()
        if not (self.session and self.live):
            self.switch_mode("live")  # asks first
        if self.mode_var.get() == "live" and not self.running:
            self.toggle()  # asks you to type LIVE
        self.tray_tick(loop=False)

    def tray_autostart(self):
        T = self.tray_lib()
        if T is None:
            return
        on = not T.autostart_enabled("SBF")
        if T.set_autostart("SBF", on, os.path.join(HERE, "main.py")):
            self.write("SBF will start in the background when you log in to Windows (practice training only)."
                       if on else "SBF won't start with Windows any more.", "good")
        else:
            self.write("Couldn't change the Windows start-up setting.", "bad")
        self.tray_tick(loop=False)

    def menu_autostart(self):
        """File > Start with Windows. Sets up the icon add-on first if it isn't installed yet."""
        T = self.tray_lib()
        want = bool(self._autostart_var.get())
        if T is None:
            self._autostart_var.set(False)
            return messagebox.showerror("Start with Windows", f"tray.py is missing from the {APP_SHORT} folder. Updating puts it back.")
        if want and not T.tray_ready():
            self._autostart_var.set(False)
            if not messagebox.askyesno("Start with Windows", f"Starting with Windows runs {APP_SHORT} as a small icon by the clock, "
                                       "which needs two small add-ons (pystray and pillow).\n\nInstall them now?"):
                return
            self.write("Installing the add-ons for background mode...")

            def work():
                ok, msg = E.install_packages(T.TRAY_PIP, lambda line: None)
                self.q.put(("call", lambda: (self._autostart_var.set(True), self.menu_autostart()) if ok and T.tray_ready()
                            else self.write("Background mode couldn't be set up: " + msg, "bad")))
            threading.Thread(target=work, daemon=True).start()
            return
        if T.autostart_enabled(APP_SHORT) != want:
            self.tray_autostart()

    def tray_quit(self):
        self._quitting = "tray"
        self.on_close()

    def poll_show(self):
        """Another copy of SBF was opened: show this one instead (checked every 2 seconds)."""
        T = self.tray_lib()
        if T and T.show_requested(E.HOME_DIR):
            self.show_window()
        self.root.after(2000, self.poll_show)

    # ---------------- UI ----------------
    def build_ui(self):
        root = self.root
        root.configure(bg=BG)
        root.minsize(980, 640)
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        self.setup_styles()

        self.build_menu()
        root.bind("<Map>", self.on_restore, add="+")
        # A binding on the window itself fires for every one of its widgets too (hundreds per resize step, each a
        # trip into Python): a tag of its own makes it fire for the window alone.
        root.bindtags(("OBFRoot",) + root.bindtags())
        root.bind_class("OBFRoot", "<Configure>", self._on_root_configure, add="+")

        header = tk.Frame(root, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        header.pack(fill="x")
        tk.Label(header, text="SBF", bg=CARD, fg=TEXT, font=("", 14, "bold")).pack(side="left", padx=14, pady=10)
        self.mode_var = tk.StringVar(value="paper")
        segs = tk.Frame(header, bg=CARD)
        segs.pack(side="left")
        self.seg_paper = tk.Label(segs, text="PRACTICE", font=("", 10, "bold"), padx=14, pady=4, cursor="hand2")
        self.seg_live = tk.Label(segs, text="LIVE", font=("", 10, "bold"), padx=14, pady=4, cursor="hand2")
        self.seg_paper.pack(side="left")
        self.seg_live.pack(side="left", padx=(2, 0))
        self.seg_paper.bind("<Button-1>", lambda e: self.click_practice())
        self.seg_live.bind("<Button-1>", lambda e: self.click_live())
        Tip(self.seg_paper, "PRACTICE: pretend money, no real money ever. Trades stay inside SBF (PRACTICE), or go to your "
                            "Alpaca paper account (ALPACA PAPER): choose under Account on the left.")
        Tip(self.seg_live, "LIVE: real money, through your broker. Needs your real-money (live) Alpaca key. Has its own session.")
        self.mode_pill = tk.Label(header, text="", bg=CARD, fg=GREEN, font=("", 9, "bold"), padx=10)
        self.mode_pill.pack(side="left")
        self.kill_btn = tk.Button(header, text="\u25a0 KILL", command=self.kill_switch, relief="flat", bd=0, bg="#b91c1c",
                                  fg="white", activebackground="#7f1d1d", activeforeground="white", padx=10, pady=3,
                                  cursor="hand2", font=("", 9, "bold"))
        self.kill_btn.pack(side="left", padx=(6, 0))
        Tip(self.kill_btn, "Kill switch: stops the bot, cancels waiting orders, sells everything at market and locks LIVE "
                           "mode until you unlock it.")
        self.donate_btn = tk.Button(header, text="\u2665 Donate", command=lambda: webbrowser.open(DONATE_URL),
                                    relief="flat", bd=0, bg="#db2777", fg="white", activebackground="#be185d",
                                    activeforeground="white", padx=12, pady=3, cursor="hand2", font=("", 9, "bold"))
        self.donate_btn.pack(side="right", padx=(0, 14))  # packed first, so it sits in the top-right corner
        Tip(self.donate_btn, "SBF is free and made in spare time. Donations (GitHub Sponsors) pay for new breeds, new "
                             "tricks and more time to build them. Opens in your browser.")
        self.theme_btn = tk.Button(header, text="Light mode" if DARK else "Dark mode", command=lambda: self.set_dark(not DARK),
                                   relief="flat", bd=0, bg=LINE, fg=TEXT, activebackground=LINE, activeforeground=TEXT,
                                   padx=10, pady=3, cursor="hand2")
        self.theme_btn.pack(side="right", padx=(0, 8))
        self.mute_btn = tk.Button(header, text="\u266a off" if self.cfg.get("muted") else "\u266a", command=self.toggle_mute,
                                  relief="flat", bd=0, bg=CARD, fg=TEXT, activebackground=CARD, cursor="hand2", font=("", 11))
        self.mute_btn.pack(side="right", padx=(0, 8))
        Tip(self.mute_btn, "Mute or unmute all notifications. Sounds are off unless you turn them on in Settings.")
        self.upd_btn = tk.Button(header, text="\u27f3 Check for updates", command=self.update_clicked, relief="flat", bd=0,
                                 bg=LINE, fg=TEXT, activebackground=LINE, activeforeground=TEXT, padx=10, pady=3,
                                 cursor="hand2", font=("", 9, "bold"))
        self.upd_btn.pack(side="right", padx=(0, 8))
        self.ess_btn = tk.Button(header, command=self.toggle_essentials, relief="flat", bd=0, padx=10, pady=3,
                                 cursor="hand2", font=("", 9, "bold"))
        self.ess_btn.pack(side="right", padx=(0, 8))
        Tip(self.ess_btn, "Essentials only: switches off everything that downloads extra history or works the disk in "
                          "the background (Free roam, Auto camp, the Slow farm, the Scalp lab, background exams, the economic "
                          "calendar refresh, weekly backups). Normal trading and its prices keep running.")
        self.paint_essentials()
        Tip(self.upd_btn, f"You have version {self.version}. Checks GitHub for a newer one; nothing installs until you accept.")
        self.conn_lbl = tk.Label(header, text="Not logged in", bg=CARD, fg=MUTED)
        self.conn_lbl.pack(side="right", padx=14)
        self.net_lbl = tk.Label(header, text="\u25cf", bg=CARD, fg=MUTED, font=("", 11))
        self.net_lbl.pack(side="right")
        self.net_tip = Tip(self.net_lbl, "Price connection: not started yet.")
        self.tick_lbl = tk.Label(header, text="", bg=CARD, fg=MUTED)
        self.tick_lbl.pack(side="right", padx=14)
        self.err_lbl = tk.Label(header, text="", bg=CARD, fg=RED, cursor="hand2", font=("", 9, "bold"))
        self.err_lbl.bind("<Button-1>", lambda e: self.show_diagnostics())

        self.mode_stripe = tk.Frame(root, height=4, bg=GREEN)
        self.mode_stripe.pack(fill="x")
        self.headline_lbl = tk.Label(root, text="", bg="#1e3a5f" if DARK else "#eff6ff", fg="#bfdbfe" if DARK else "#1e3a8a", anchor="w", justify="left",
                                     padx=14, pady=7, wraplength=1000, font=("", 10))
        self.headline_lbl.pack(fill="x")

        body = self.body = tk.Frame(root, bg=BG, highlightthickness=0)
        body.pack(fill="both", expand=True, padx=10, pady=10)
        self.left_scroll = ScrollFrame(body, BG, width=262)
        self.left_scroll.pack(side="left", fill="y", padx=(0, 10))
        left = self.left_scroll.inner
        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        # --- account card
        f, c = self.card(left, "ACCOUNT")
        f.pack(fill="x", pady=(0, 8))
        self.mode_desc = tk.Label(c, text="", bg=CARD, fg=TEXT, font=("", 10, "bold"), anchor="w", justify="left",
                                  wraplength=230)
        self.mode_desc.pack(fill="x")
        self.dest_row = tk.Frame(c, bg=CARD)
        self.dest_row.pack(fill="x", pady=(4, 0))
        tk.Label(self.dest_row, text="Practice trades go to", bg=CARD, fg=MUTED).pack(anchor="w")
        self.dest_var = tk.StringVar(value="Stay in SBF  (PRACTICE)")
        self.dest_box = ttk.Combobox(self.dest_row, textvariable=self.dest_var, state="readonly", width=34,
                                     values=["Stay in SBF  (PRACTICE)", "My Alpaca paper account  (ALPACA PAPER)",
                                             "Both: SBF and my Alpaca paper account"])
        self.dest_box.pack(fill="x")
        self.dest_box.bind("<<ComboboxSelected>>", lambda e: self.pick_practice_dest())
        Tip(self.dest_box, "PRACTICE: pretend trades simulated inside SBF. ALPACA PAPER: every order really goes to your Alpaca "
                           "paper account (needs a paper key). BOTH: the PRACTICE session runs inside SBF and every one of its "
                           "trades is also copied to your Alpaca paper account (labelled ALPACA PAPER). Never real money.")
        self.budget_lbl = tk.Label(c, text="Practice money (pretend, USD)", bg=CARD, fg=MUTED)
        self.budget_lbl.pack(anchor="w", pady=(8, 0))
        self.cash_var = tk.StringVar(value=self.default_cash_text())
        cash_row = tk.Frame(c, bg=CARD)
        cash_row.pack(fill="x")
        self.cash_default = tk.BooleanVar(value=bool(self.cfg.get("default_cash")))
        cd = ttk.Checkbutton(cash_row, text="Default", variable=self.cash_default, style="Card.TCheckbutton",
                             command=self.toggle_default_cash)
        cd.pack(side="right", padx=(6, 0))
        ttk.Entry(cash_row, textvariable=self.cash_var).pack(side="left", fill="x", expand=True)
        Tip(cd, "Tick to make this amount your default practice money: it's filled in here whenever SBF starts, and used "
                "for new sessions and resets. Untick to go back to $10,000.")
        self.cash_var.trace_add("write", lambda *a: self._sync_default_cash())
        tk.Label(c, text="Risk style", bg=CARD, fg=MUTED).pack(anchor="w", pady=(8, 0))
        self.style_var = tk.StringVar(value=self.settings.get("style", "Balanced"))
        cb = ttk.Combobox(c, textvariable=self.style_var, values=list(E.STYLES) + ["Custom"], state="readonly")
        cb.pack(fill="x")
        cb.bind("<<ComboboxSelected>>", self.on_style)
        Tip(cb, "Sets trade size and safety limits in one go. Cautious = small and steady, Adventurous = big and bold.")
        self.quote_var = tk.StringVar(value="USD")  # stocks are always in US dollars
        self.login_btn = ttk.Button(c, text="Log in to Alpaca", style="Plain.TButton", command=self.account_action)
        self.login_btn.pack(fill="x", pady=(8, 0))
        tk.Label(c, text="Practice prices come from Alpaca (a free paper key).", bg=CARD, fg=MUTED, font=("", 8)).pack(anchor="w")

        # --- coins card
        f, c = self.card(left, "TICKERS")
        f.pack(fill="x", pady=(0, 8))
        self.coin_grid = tk.Frame(c, bg=CARD)
        self.coin_grid.pack(anchor="w", fill="x")
        self.coin_vars = {}
        ticked = self.cfg.get("ticked_coins") or ["SPY", "QQQ"]
        for coin in self.all_coins():
            self.coin_vars[coin] = tk.BooleanVar(value=coin in ticked)
        self.build_coin_grid()
        crow = tk.Frame(c, bg=CARD)
        crow.pack(fill="x", pady=(6, 0))
        mc = ttk.Button(crow, text="More tickers...", style="Plain.TButton", command=self.open_coin_browser)
        mc.pack(side="left")
        Tip(mc, "Browse popular stocks and ETFs, with today's change and volume, and add any of them. (Ctrl+K)")
        self.auto_coin_var = tk.BooleanVar(value=int(self.settings.get("auto_coins", 0)) > 0)
        ab = ttk.Checkbutton(crow, text="Bot picks", variable=self.auto_coin_var, style="Card.TCheckbutton",
                             command=self.toggle_auto_coins)
        ab.pack(side="left", padx=(8, 0))
        Tip(ab, "Wildcard only: when you start a new session, the bot scouts about 40 popular tickers and picks the best "
                "for its money: busy (so it costs little to get in and out) and moving (up to a point). "
                "One place is kept for the best index ETF (SPY, QQQ, IWM, DIA). How many: Settings > Wildcard.")

        # --- market context card: tickers the bots watch but never trade
        f, c = self.card(left, "MARKET CONTEXT (WATCHED)")
        f.pack(fill="x", pady=(0, 8))
        self.ctx_box = tk.Frame(c, bg=CARD)
        self.ctx_box.pack(fill="x")
        crow2 = tk.Frame(c, bg=CARD)
        crow2.pack(fill="x", pady=(4, 0))
        ttk.Button(crow2, text="What they learned...", style="Plain.TButton", command=self.open_context).pack(side="left")
        ttk.Button(crow2, text="Edit", style="Plain.TButton", command=self.edit_context).pack(side="left", padx=(4, 0))
        Tip(self.ctx_box, "The bots watch these tickers without trading them, and can evolve studies on them: for example "
                          "'when VIXY (the VIX stand-in) jumps, don't buy' or 'when TLT trends up, buy more readily'. "
                          "Every such study is graded by what the trades it held back would have done.")
        self.ctx_labels = {}
        self.root.after(200, self.ctx_card_build)

        # --- upcoming news card: the economic calendar
        f, c = self.card(left, "UPCOMING NEWS")
        f.pack(fill="x", pady=(0, 8))
        self.news_box = tk.Frame(c, bg=CARD)
        self.news_box.pack(fill="x")
        nrow = tk.Frame(c, bg=CARD)
        nrow.pack(fill="x", pady=(4, 0))
        ttk.Button(nrow, text="Calendar...", style="Plain.TButton", command=self.open_calendar).pack(side="left")
        Tip(self.news_box, "Big scheduled news: CPI, the jobs report, Fed decisions and more. Bots can grow never-rules "
                           "around them ('stay out the day before CPI'), graded by what the trades they skipped would have done.")
        self.root.after(300, self.news_card_refresh)

        # --- strategy card
        f, c = self.card(left, "STRATEGY")
        f.pack(fill="x", pady=(0, 8))
        self.wild_var = tk.BooleanVar(value=False)
        wb = ttk.Checkbutton(c, text="Let the bot choose (Wildcard)", variable=self.wild_var,
                             style="Card.TCheckbutton", command=self.toggle_wild)
        wb.pack(anchor="w")
        Tip(wb, "The bot tests every strategy in the background, trades bigger with the ones that are winning "
                "consistently, and benches or retires the ones that aren't. Recommended if you're not sure.")
        self.more_var = tk.BooleanVar(value=bool(self.settings.get("winners_trade_more", True)))
        mb = ttk.Checkbutton(c, text="Winners trade more", variable=self.more_var, style="Card.TCheckbutton",
                             command=self.toggle_more)
        mb.pack(anchor="w", padx=(18, 0))
        rrow = tk.Frame(c, bg=CARD)
        rrow.pack(fill="x", padx=(18, 0), pady=(2, 0))
        tk.Label(rrow, text="Bots trading:", bg=CARD, fg=MUTED, font=("", 8)).pack(side="left")
        if "roster_paper" not in self.cfg:  # first start after this feature: keep an old "trade with my team" choice
            self.cfg["roster_paper"] = "team" if self.cfg.get("team_mode") else "farm"
        self.roster_var = tk.StringVar()
        self.roster_box = ttk.Combobox(rrow, textvariable=self.roster_var, state="readonly", width=20)
        self.roster_box.pack(side="left", padx=(4, 0))
        self.roster_box.bind("<<ComboboxSelected>>", lambda e: self.set_roster())
        Tip(self.roster_box, "Which bots may trade (Wildcard). Whole farm: everyone competes and the winners are followed. "
                             "My team: only your team from the Bot Library. Hall of Fame only: only the champions you "
                             "picked, nothing new is bred. Practice and Live each remember their own choice.")
        self.update_team_label()
        Tip(mb, "Strategies that keep winning get bigger trades and keep adding while they hold a coin. "
                "Shaky ones are slowed down and failing ones are stopped. Turn off for calmer, less frequent trading. "
                "More trading means more fees.")
        self.strat_box = tk.Frame(c, bg=CARD)
        self.strat_box.pack(fill="x")
        self.wild_note = tk.Label(c, text="The bot runs every strategy and follows the winners. Untick Wildcard to pick your own.",
                                  bg=CARD, fg=MUTED, font=("", 8), justify="left", wraplength=220)
        tk.Label(self.strat_box, text="Or pick your own:", bg=CARD, fg=MUTED, font=("", 8)).pack(anchor="w", pady=(4, 0))
        self.strat_vars, self.strat_btns = {}, []
        self.size_lbls = {}
        for name in E.STRATS:
            v = tk.BooleanVar(value=name == "SMA crossover")
            row = tk.Frame(self.strat_box, bg=CARD)
            row.pack(fill="x")
            b = ttk.Checkbutton(row, text=STRAT_LABELS[name], variable=v, style="Card.TCheckbutton")
            b.pack(side="left")
            Tip(b, STRAT_TIPS[name])
            ttk.Button(row, text="+", width=2, command=lambda n=name: self.bump_size(n, 0.25)).pack(side="right")
            lbl = tk.Label(row, text="", bg=CARD, fg=MUTED, font=("", 8), width=5)
            lbl.pack(side="right")
            Tip(lbl, "Trade size for this strategy when you pick strategies yourself. 1.00x = normal.")
            ttk.Button(row, text="-", width=2, command=lambda n=name: self.bump_size(n, -0.25)).pack(side="right")
            self.size_lbls[name] = lbl
            self.strat_vars[name] = v
            self.strat_btns.append(b)

        self.btn = ttk.Button(left, text="Start the bot", style="Accent.TButton", command=lambda: self.start_choice("continue"))
        self.btn.pack(fill="x", pady=(2, 4))
        self.new_btn = ttk.Button(left, text="Start new", style="Plain.TButton", command=lambda: self.start_choice("new"))
        self.new_btn.pack(fill="x", pady=(0, 4))
        self.new_btn.pack_forget()
        self._new_after = self.btn
        Tip(self.new_btn, "Start a fresh session instead of continuing the saved one. Your trade history stays in the "
                          "Trades tab and trades.csv.")
        self.acct_reset_btn = ttk.Button(left, text="Reset account", style="Plain.TButton", command=self.reset_account)
        self.acct_reset_btn.pack(fill="x", pady=(0, 4))
        Tip(self.acct_reset_btn, "Puts the account back to its starting money with no positions and no trades. "
                                 "Your farms, bots and everything they've learned are kept.")
        self.reset_btn = ttk.Button(left, text="More reset options \u25be", style="Plain.TButton")
        self.reset_btn.configure(command=self.open_reset)
        self.reset_btn.pack(fill="x", pady=(0, 4))
        Tip(self.reset_btn, "Start over: reset the practice account (cash and trades), the farm (fresh eggs), or both. "
                            "Your Library and Hall of Fame are always kept.")
        ttk.Button(left, text="Sell everything now", style="Plain.TButton", command=self.sell_all).pack(fill="x")
        trow = tk.Frame(left, bg=BG)
        trow.pack(fill="x", pady=(4, 0))
        ttk.Button(trow, text="Buy...", style="Accent.TButton", command=lambda: self.trade_dialog("buy")).pack(side="left", expand=True, fill="x")
        ttk.Button(trow, text="Sell...", style="Plain.TButton", command=lambda: self.trade_dialog("sell")).pack(side="left", expand=True, fill="x", padx=(4, 0))
        tk.Label(left, text=f"v{self.version}", bg=BG, fg=MUTED, font=("", 8)).pack(anchor="w", pady=(6, 0))

        # --- right side: stat tiles, chart, tabs
        tiles = tk.Frame(right, bg=BG)
        tiles.pack(fill="x")
        self.tiles = {}
        self.tile_frames = {}
        for i, (key, title) in enumerate((("equity", "TOTAL VALUE"), ("pl", "PROFIT / LOSS"), ("day", "TODAY"), ("cash", "CASH LEFT"),
                                          ("hold", "VS THE MARKET"), ("broker", "ALPACA ACCOUNT"))):
            f, c = self.card(tiles, title)
            self.tile_frames[key] = f
            f.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))
            tiles.columnconfigure(i, weight=1)
            if key == "hold":
                self.hold_title = c.winfo_children()[0]
            self.__dict__.setdefault("tile_titles", {})[key] = c.winfo_children()[0]
            lbl = tk.Label(c, text="-", bg=CARD, fg=TEXT, font=("", 15, "bold"))
            lbl.pack(anchor="w")
            self.tiles[key] = lbl
            if key in ("pl", "hold", "broker"):
                sub = tk.Label(c, text="", bg=CARD, fg=MUTED, font=("", 7))
                sub.pack(anchor="w")
                self.__dict__.setdefault("tile_subs", {})[key] = sub
            if key == "day":
                self.day_sub = tk.Label(c, text="", bg=CARD, fg=MUTED, font=("", 7))
                self.day_sub.pack(anchor="w")
                Tip(self.day_sub, "How much of today's loss limit is used up. At 100% the bot stops trading for the rest "
                                  "of the day (Settings > Basics > Daily loss limit).")
            Tip(lbl, {"equity": "Cash plus the current value of every coin you own.",
                      "pl": "How much your account (practice or LIVE) has made or lost since this session started. The "
                            "farm's own test trades are pretend and never count here.",
                      "cash": "Money not currently invested in a coin.",
                      "hold": "Your account's profit/loss minus the market's: the market = your starting money split across "
                              "your tickers at the start and left alone. Positive = your account did better (or lost less).",
                      "day": "How much your total value has changed since today began.",
                      "broker": "Your Alpaca account's own value, straight from Alpaca (paper or real money), with its change "
                                "since yesterday's close. PRACTICE runs on its own pretend money inside SBF, so these numbers "
                                "differ. Turn this tile off in Settings > Basics."}[key])
        self.tile_frames["broker"].grid_remove()  # shown once there's an Alpaca account to read
        tiles.columnconfigure(5, weight=0)
        threading.Thread(target=self._broker_loop, daemon=True).start()
        self.root.after(50, self.apply_compact)
        self.verdict_lbl = tk.Label(right, text="", bg=BG, fg=MUTED, anchor="w", font=("", 10, "bold"))
        self.verdict_lbl.pack(fill="x", pady=(6, 0))
        self.activity_lbl = tk.Label(right, text="", bg=BG, fg=MUTED, anchor="w", font=("", 8))
        self.activity_lbl.pack(fill="x")
        Tip(self.activity_lbl, "The bots trade with pretend money inside the farm to find out what works; those trades "
                               "never change your balance. Your account (the tiles above) only trades when it copies "
                               "the winners, so it trades less often than the bots.")

        split = tk.PanedWindow(right, orient="vertical", bg=BG, sashwidth=6, bd=0, sashrelief="flat")
        split.pack(fill="both", expand=True, pady=(8, 0))
        f, c = self.card(split, None)
        self.build_chart_controls(c)
        self.charts_frame = tk.Frame(c, bg=CARD)
        self.charts_frame.pack(fill="both", expand=True)
        self.chart_card = f
        self.panels = []
        self.build_panels()
        split.add(f, minsize=150, stretch="never",
                  height={1: 250, 2: 250, 3: 260, 4: 400, 6: 420}[self.LAYOUTS[self.chart_layout.get()][0] * self.LAYOUTS[self.chart_layout.get()][1]])
        nbf = tk.Frame(split, bg=BG)
        split.add(nbf, minsize=180, stretch="always")
        self.split = split
        nb = self.nb = ttk.Notebook(nbf)
        nb.pack(fill="both", expand=True, pady=(8, 0))
        nb.bind("<<NotebookTabChanged>>", lambda e: self.refresh_visible())
        t0, t1, t7, tm, tt, tb, ts, tf_, tsu, tal = (ttk.Frame(nb) for _ in range(10))
        nb.add(tf_, text="  \u2630 Overview  ")
        nb.add(t0, text="  \u25c6 Bot Farm  ")
        nb.add(t7, text="  \u25c7 Slow farm  ")
        nb.add(t1, text="  Portfolio  ")
        nb.add(tb, text="  Bot positions  ")
        nb.add(tal, text="  Alpaca account  ")
        nb.add(tsu, text="  \u2605 Success  ")
        nb.add(ts, text="  \u26a1 Scalpers  ")
        nb.add(tm, text="  Markets  ")
        nb.add(tt, text="  Trades  ")
        # Markets = watchlist + live prices; Trades = trades + what the bot did + the strategy scoreboard
        self.subnb = {}
        for outer, names in ((tm, ("Watchlist", "Live prices")), (tt, ("Trades", "What the bot did", "Strategy scoreboard"))):
            sn = ttk.Notebook(outer)
            sn.pack(fill="both", expand=True, pady=(4, 0))
            sn.bind("<<NotebookTabChanged>>", lambda e: self.refresh_visible())
            self.subnb[nb.tab(outer, "text").strip()] = sn
            fr = []
            for nm in names:
                f_ = ttk.Frame(sn)
                sn.add(f_, text=f"  {nm}  ")
                fr.append(f_)
            if outer is tm:
                t6, t4 = fr
            else:
                t5, t3, t2 = fr
        self.build_grid_tab(t0)
        self.build_slow_tab(t7)
        self.build_portfolio(t1)
        self.build_botpos_tab(tb)
        self.build_alpaca_tab(tal)
        self.build_features_tab(tf_)
        self.build_success_tab(tsu)
        sn_ = ttk.Notebook(ts)  # Scalpers = the live scalp lab + the market scan
        sn_.pack(fill="both", expand=True, pady=(4, 0))
        sn_.bind("<<NotebookTabChanged>>", lambda e: self.refresh_visible())
        self.subnb[nb.tab(ts, "text").strip()] = sn_
        f_lab, f_scan = ttk.Frame(sn_), ttk.Frame(sn_)
        sn_.add(f_lab, text="  Scalp lab  ")
        sn_.add(f_scan, text="  Scalp scan  ")
        self.build_lab_tab(f_lab)
        self.build_scan_tab(f_scan)
        self.build_watchlist(t6)
        tk.Label(t2, bg=CARD, fg=MUTED, justify="left", anchor="w", padx=8, pady=4, wraplength=700,
                 text="The bot tests every strategy with pretend trades. Winning = copied with bigger trades. "
                      "Shaky = copied slowly with small trades. Stopped = ignored.").pack(fill="x", side="top")
        bar2 = ttk.Frame(t2)
        bar2.pack(fill="x", padx=4, pady=(0, 2))
        bar3 = ttk.Frame(t2)
        bar3.pack(fill="x", padx=4, pady=(0, 4))
        for i, (txt, cmd, tip) in enumerate((
                ("New random strategy", self.new_random,
                 "Adds a brand new strategy with random settings. It replays recent prices, then competes with the rest."),
                ("Size -", lambda: self.bump_variant(-0.25), "Click a row first, then make that strategy's trades smaller."),
                ("Size +", lambda: self.bump_variant(0.25), "Click a row first, then make that strategy's trades bigger."),
                ("Save lineup", self.save_lineup, "Saves every strategy that isn't stopped, with its settings and size."),
                ("Load saved", self.load_lineup,
                 "Adds your saved strategies to this run. They also load automatically when Wildcard starts."),
                ("Export bots...", self.export_bots,
                 "Saves the strategies you selected (Ctrl/Shift-click for several) to a file to share or keep. "
                 "With none selected it exports everything that isn't stopped."),
                ("Import bots...", self.import_bots,
                 "Loads bots from a file. They replay recent prices, then compete with your own."),
                ("Hall of Fame...", self.open_hof,
                 "Bots that kept winning for a long time are saved here automatically. Export them or bring them back. (Ctrl+H)"),
                ("Catch!", self.catch_selected, "Catch the selected bots for your Bot Collection. They get a name, a rarity "
                 "and a level, and you can trade with your favourite team later."),
                ("Library...", self.open_collection, "Your saved bots (the Bot Library): rarities, levels, your team, and who is in the Hall of Fame. (Ctrl+B)"),
                ("Family...", self.open_family, "Which bot was bred or tweaked from which, as a list and as drawn bloodlines."))):
            sb = ttk.Button(bar2 if i < 6 else bar3, text=txt, style="Accent.TButton" if txt == "Catch!" else "Plain.TButton",
                            command=cmd)
            sb.pack(side="left", padx=(0, 4))
            Tip(sb, tip)
        self.best_lbl = tk.Label(t2, text="", bg=BG, fg=MUTED, anchor="w", padx=6)
        self.best_lbl.pack(fill="x")
        self.build_farm_strip(t2)
        self.board = self.make_tree(t2, ("strategy", "recent", "edge", "consistent", "winrate", "life", "size", "status", "trades"),
                                    (230, 70, 80, 70, 65, 70, 55, 170, 55),
                                    ("Strategy", "Recent gain", "vs the market", "Wins lately", "Win rate", "Lifetime",
                                     "Size", "What the bot does", "Trades"))
        self.board.bind("<Double-1>", lambda e: self.bot_detail(quiet=True) if self.on_row(self.board, e) else None)
        for btn in ("<Button-3>", "<Button-2>"):
            self.board.bind(btn, lambda e: (lambda iid: self.bot_menu(e, next((v for v in self.tourn.vars if str(id(v)) == iid), None))
                                            if iid and self.tourn else None)(self.board.identify_row(e.y)))
        Tip(self.board, "Double-click a strategy for its full track record.")
        self.style_board_tags()
        self.empty_board = tk.Label(t2, text="Tick 'Let the bot choose (Wildcard)' and press Start.\nStrategies will compete here.",
                                    bg=CARD, fg=MUTED, pady=18)
        self.log = tk.Text(t3, height=10, state="disabled", bg=CARD, fg=TEXT, relief="flat", font=("", 10), wrap="word", padx=6, pady=4)
        for tag, color in (("good", GREEN), ("bad", RED), ("warn", "#fbbf24" if DARK else "#b45309"), ("info", TEXT)):
            self.log.tag_configure(tag, foreground=color)
        self.log.pack(fill="both", expand=True)
        self.prices_tree = self.make_tree(
            t4, ("coin", "price", "since", "hr", "day", "mood", "age"), (70, 120, 120, 90, 100, 170, 80),
            ("Ticker", "Price now", "Since this run began", "Last hour", "Last 24 hours", "Market mood", "Updated"))
        trade_bar = ttk.Frame(t5)
        trade_bar.pack(fill="x", padx=4, pady=4)
        self.trade_count_lbl = ttk.Label(trade_bar, text="All paper + live trades")
        self.trade_count_lbl.pack(side="left")
        ttk.Button(trade_bar, text="Refresh", style="Plain.TButton",
                   command=self.refresh_trades).pack(side="right")
        self.trade_acct = tk.StringVar(value="All accounts")
        acb = ttk.Combobox(trade_bar, textvariable=self.trade_acct, state="readonly", width=14,
                           values=["All accounts", "PRACTICE", "ALPACA PAPER", "LIVE", "FARM TEST"])
        acb.pack(side="right", padx=(8, 0))
        acb.bind("<<ComboboxSelected>>", lambda e: self.refresh_trades())
        ttk.Label(trade_bar, text="Show").pack(side="right")
        self.show_bots_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(trade_bar, text="Also show the farm's test trades (pretend, never your balance)", variable=self.show_bots_var,
                        command=self.refresh_trades).pack(side="right", padx=8)
        self.board.bind("<<TreeviewSelect>>", lambda e: self.soon(self._after_select))
        self.trade_tree = self.make_tree(
            t5, ("time", "mode", "side", "coin", "qty", "usd", "price", "pl", "who", "why"),
            (125, 125, 55, 55, 105, 80, 90, 80, 200, 170),
            ("Time", "Account", "Action", "Stock", "Shares", "Value ($)", "Price", "Profit / loss", "Strategy behind it", "Why"))
        self.trade_tree.tag_configure("buy", foreground=GREEN)
        self.trade_tree.tag_configure("sell", foreground=RED)
        self.trade_tree.tag_configure("bot", foreground=MUTED)
        self.show_sizes()
        self.trade_tree.bind("<Double-1>", lambda e: self.show_trade_why(self._trade_rows[self.trade_tree.focus()])
                             if self.trade_tree.focus() in self._trade_rows else None)
        Tip(self.trade_tree, "Double-click a trade to see why it happened: which bots voted and what each one saw.")
        self.toggle_wild()
        self.refresh_trades()
        self.update_status()
        self.refresh_view()
        for seq, fn in (("<Control-Return>", self.toggle), ("<Control-l>", self.open_learning), ("<Control-h>", self.open_hof),
                        ("<Control-k>", self.open_coin_browser), ("<Control-b>", self.open_collection), ("<Control-e>", self.open_designer), ("<Control-comma>", self.open_settings),
                        ("<F5>", self.refresh_trades)):
            root.bind_all(seq, lambda e, f=fn: (f(), "break")[1])
        root.after(60, self.set_farm_style)

    def build_menu(self):
        bar = tk.Menu(self.root)
        m = tk.Menu(bar, tearoff=0)
        m.add_command(label="Export trades CSV...", command=self.export_csv)
        m.add_command(label="Export bots...", command=self.export_bots)
        m.add_command(label="Import bots...", command=self.import_bots)
        m.add_command(label="Back up everything...", command=self.backup_all)
        m.add_command(label="Restore from a backup...", command=self.restore_all)
        m.add_command(label="Open data folder", command=self.open_data_folder)
        m.add_command(label="Export a report for Claude...", command=self.export_report)
        m.add_command(label="Export a detailed report for Claude...", command=lambda: self.export_report(detailed=True))
        m.add_separator()
        m.add_command(label="Reset...", command=self.open_reset)
        m.add_command(label="Run in the background (icon by the clock)", command=self.go_background)
        if sys.platform.startswith("win"):
            self._autostart_var = tk.BooleanVar(value=False)
            m.add_checkbutton(label="Start with Windows (as the icon, practice training only)",
                              variable=self._autostart_var, command=self.menu_autostart)
            m.configure(postcommand=lambda: self._autostart_var.set(bool((T := self.tray_lib()) and
                                                                          T.autostart_enabled(APP_SHORT))))
        m.add_command(label="Quit", command=lambda: (setattr(self, "_quitting", True), self.on_close()))
        bar.add_cascade(label="File", menu=m)
        m = tk.Menu(bar, tearoff=0)
        m.add_command(label="Log in to Alpaca...", command=self.open_login)
        m.add_command(label="Log out", command=self.logout)
        m.add_separator()
        m.add_command(label="Sell everything now", command=self.sell_all)
        m.add_command(label="KILL SWITCH (stop, cancel, sell all, lock Live)", command=self.kill_switch)
        m.add_separator()
        m.add_command(label="Ready for LIVE? (checklist)...", command=self.open_live_check)
        bar.add_cascade(label="Account", menu=m)
        m = tk.Menu(bar, tearoff=0)
        m.add_command(label="Learning: backtests, Training camp, Free roam...", command=self.open_learning, accelerator="Ctrl+L")
        m.add_command(label="Insights: market context, calendar, report, replay...", command=self.open_insights)
        m.add_command(label="What learning is worth (feature tests)...", command=self.open_learning_report)
        m.add_separator()
        m.add_command(label="Bot Library...", command=self.open_collection, accelerator="Ctrl+B")
        m.add_command(label="Hall of Fame...", command=self.open_hof, accelerator="Ctrl+H")
        m.add_command(label="Family (tree and bloodlines)...", command=self.open_family)
        m.add_command(label="Compare two bots...", command=lambda: self.compare_bots())
        m.add_command(label="Design an egg...", command=self.open_designer, accelerator="Ctrl+E")
        m.add_command(label="Browse tickers...", command=self.open_coin_browser, accelerator="Ctrl+K")
        m.add_separator()
        m.add_command(label="Settings...", command=self.open_settings, accelerator="Ctrl+,")
        pm = tk.Menu(m, tearoff=0, postcommand=lambda: self.fill_profiles(pm))
        m.add_cascade(label="Settings profiles", menu=pm)
        m.add_checkbutton(label="Dark mode", variable=self.dark_var, command=lambda: self.set_dark(self.dark_var.get()))
        self.compact_var = tk.BooleanVar(value=bool(self.cfg.get("compact", False)))
        m.add_checkbutton(label="Compact top bar", variable=self.compact_var, command=self.toggle_compact)
        m.add_command(label="System: security, disk use, diagnostics...", command=self.open_system)
        m.add_command(label="Install add-ons (setup)...", command=self.open_setup)
        m.add_separator()
        m.add_command(label="Check for updates", command=self.check_update)
        m.add_command(label="Donate (GitHub Sponsors)...", command=lambda: webbrowser.open(DONATE_URL))
        m.add_command(label="Undo last update", command=self.undo_update)
        bar.add_cascade(label="Tools", menu=m)
        m = tk.Menu(bar, tearoff=0)
        m.add_command(label="Help...", command=self.open_help)
        bar.add_cascade(label="Help", menu=m)
        self.root.config(menu=bar)

    def fit_and_center(self):
        """Opens centred on the screen, big enough to show all of the left-hand panel (never bigger than the screen)."""
        r = self.root
        try:
            r.update_idletasks()
            sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
            maxw, maxh = sw - 40, sh - 110  # leave room for the title bar and taskbar
            try:
                want_w, want_h = (int(x) for x in str(self.cfg.get("size", "")).split("x"))
            except ValueError:
                want_w = want_h = 0
            need_h = r.winfo_reqheight() + 12
            w = min(max(want_w, r.winfo_reqwidth(), 1050), maxw)
            h = min(max(want_h, need_h, 720), maxh)
            r.geometry(f"{w}x{h}+{max((sw - w) // 2, 0)}+{max((sh - h) // 2 - 20, 0)}")
            r.minsize(min(980, maxw), min(need_h, maxh))
        except Exception:
            pass
        r.deiconify()

    def setup_styles(self):
        st = ttk.Style()
        st.configure(".", background=BG, foreground=TEXT)
        st.configure("TFrame", background=BG)
        st.configure("Card.TCheckbutton", background=CARD, foreground=TEXT)
        st.map("Card.TCheckbutton", background=[("active", CARD)])
        st.configure("Seg.TRadiobutton", background=CARD, padding=(10, 4))
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("Treeview", rowheight=24, background=CARD, fieldbackground=CARD, borderwidth=0)
        st.configure("Treeview.Heading", background=LINE, relief="flat", font=("", 9, "bold"))
        st.configure("Accent.TButton", background=ACCENT, foreground="white", font=("", 11, "bold"), padding=8)
        st.map("Accent.TButton", background=[("active", "#1d4ed8")])
        st.configure("Stop.TButton", background=RED, foreground="white", font=("", 11, "bold"), padding=8)
        st.configure("Live.TButton", background=LIVE_BLUE, foreground="white", font=("", 11, "bold"), padding=8)
        st.map("Live.TButton", background=[("active", "#003ecb")])
        st.configure("Go.TButton", background="#16a34a", foreground="white", font=("", 11, "bold"), padding=8)
        st.map("Go.TButton", background=[("active", "#15803d")])
        st.map("Stop.TButton", background=[("active", "#b91c1c")])
        st.configure("Plain.TButton", padding=5)
        st.configure("Treeview", foreground=TEXT)
        st.configure("Treeview.Heading", foreground=TEXT)
        st.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "white")])
        st.configure("TNotebook.Tab", background=LINE, foreground=TEXT)
        st.map("TNotebook.Tab", background=[("selected", CARD)])
        st.configure("TEntry", fieldbackground=CARD, foreground=TEXT, insertcolor=TEXT)
        st.configure("TSpinbox", fieldbackground=CARD, foreground=TEXT, insertcolor=TEXT, background=LINE, arrowcolor=TEXT)
        st.map("TSpinbox", fieldbackground=[("readonly", CARD), ("disabled", CARD)], foreground=[("disabled", MUTED)])
        st.configure("TCombobox", fieldbackground=CARD, foreground=TEXT, background=LINE, arrowcolor=TEXT)
        st.map("TCombobox", fieldbackground=[("readonly", CARD)], foreground=[("readonly", TEXT)])
        st.configure("TButton", background=LINE, foreground=TEXT)
        st.map("TButton", background=[("active", "#4b5563" if DARK else "#d1d5db")])
        st.configure("Seg.TRadiobutton", foreground=TEXT)
        st.configure("TCheckbutton", foreground=TEXT)
        st.configure("TScrollbar", background=LINE, troughcolor=BG)

    def style_board_tags(self):
        amber = "#fbbf24" if DARK else "#b45309"
        for tag, color in (("win", GREEN), ("shaky", amber), ("standby", TEXT), ("stopped", MUTED), ("learning", MUTED)):
            self.board.tag_configure(tag, foreground=color)
        self.board.tag_configure("hof", font=("", 9, "bold"))

    def style_log_tags(self):
        self.style_board_tags()
        for tag, color in (("good", GREEN), ("bad", RED), ("warn", "#fbbf24" if DARK else "#b45309"), ("info", TEXT)):
            self.log.tag_configure(tag, foreground=color)
        self.trade_tree.tag_configure("buy", foreground=GREEN)
        self.trade_tree.tag_configure("sell", foreground=RED)
        self.trade_tree.tag_configure("bot", foreground=MUTED)

    def set_dark(self, dark):
        """Switches between dark and light mode immediately and remembers the choice."""
        if dark == DARK:
            return
        old, new = (DARK_P, LIGHT_P) if DARK else (LIGHT_P, DARK_P)
        mapping = {old[k].lower(): new[k] for k in old}
        for light_c, dark_c in EXTRA_TO_DARK.items():
            if dark:
                mapping[light_c] = dark_c
            else:
                mapping[dark_c] = light_c
        recolor(self.root, mapping)  # first: apply_theme sets the window background to its final colour itself
        apply_theme(self.root, dark)
        self.setup_styles()
        self.style_log_tags()
        self.cfg["dark"] = dark
        E.save_json(CONFIG_PATH, self.cfg)
        self.dark_var.set(dark)
        self.theme_btn.config(text="Light mode" if dark else "Dark mode")
        self.paint_essentials()
        self.update_status()
        self.refresh_view()
        self.refresh_trades()
        self.grid_refresh()
        self._grid_paint_chips()
        self.paint_range_btns()

    @staticmethod
    def card(parent, title):
        f = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        inner = tk.Frame(f, bg=CARD, padx=10, pady=8)
        inner.pack(fill="both", expand=True)
        if title:
            tk.Label(inner, text=title, bg=CARD, fg=MUTED, font=("", 8, "bold")).pack(anchor="w", pady=(0, 4))
        return f, inner

    @staticmethod
    def make_tree(parent, cols, widths, heads=None):
        t = ttk.Treeview(parent, columns=cols, show="headings", height=8)
        sb = ttk.Scrollbar(parent, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        for i, (c, w) in enumerate(zip(cols, widths)):
            t.heading(c, text=heads[i] if heads else c.title(), command=lambda c=c: sort_tree(t, c))
            t.column(c, width=w, anchor="w" if c in ("strategy", "status", "time", "mode", "side", "why", "who") else "e")
        t.pack(side="left", fill="both", expand=True)
        return t

    # ---------------- cash currency ----------------
    def cur_quote(self):
        return self.quote if self.session else self.quote_var.get()

    def avail(self):
        return self.balances.get(self.cur_quote(), 0.0)

    @staticmethod
    def read_balances(acct):
        """Money available for share trades at the broker: the account's cash (SBF never borrows on margin)."""
        try:
            v = acct.get("cash") or 0
            return {"USD": float(v)}
        except (TypeError, ValueError, AttributeError):
            return {"USD": 0.0}

    # ---------------- strategy size / random / save ----------------
    def bump_size(self, name, d):
        cur = self.settings["size_mult"].get(name, 1.0)
        self.settings["size_mult"][name] = round(min(max(cur + d, 0.25), 3.0), 2)
        self.cfg["settings"] = self.settings
        E.save_json(CONFIG_PATH, self.cfg)
        self.show_sizes()

    def show_sizes(self):
        for n, lbl in self.size_lbls.items():
            lbl.config(text=f"{self.settings['size_mult'].get(n, 1.0):.2f}x")

    def selected_variant(self):
        sel = self.board.selection()
        if not self.tourn or not sel:
            return None
        return next((v for v in self.tourn.vars if str(id(v)) == sel[0]), None)

    def bump_variant(self, d):
        v = self.selected_variant()
        if not v:
            return messagebox.showinfo("Strategy size", "Click a strategy in the scoreboard first.")
        v.size_mult = round(min(max(v.size_mult + d, 0.25), 3.0), 2)
        self.tourn._rate()
        self.refresh_board()
        self.write(f"{friendly_label(v.name, v.params)} trade size is now {v.size_mult:.2f}x its normal size.")

    def flush_events(self):
        for e in self.tourn.pop_events():
            self.write("Wildcard: " + friendly_text(e[0].lower() + e[1:]))

    def new_random(self):
        if not self.tourn or not self.seeded:
            return messagebox.showinfo("New strategy", "Start the bot with 'Let the bot choose (Wildcard)' ticked first.")
        self.tourn.spawn_random({c: self.hist[c] for c in self.session_coins})
        self.flush_events()
        self.refresh_board()

    def save_lineup(self):
        if not self.tourn:
            return messagebox.showinfo("Save", "Nothing to save yet. Start the bot with Wildcard ticked.")
        lineup = self.tourn.export_lineup()
        E.save_strategies(lineup)
        self.write(f"Saved {len(lineup)} strategies to {E.strategies_path()}")

    def selected_variants(self):
        ids = set(self.board.selection())
        if not self.tourn or not ids:
            return []
        return [v for v in self.tourn.vars if str(id(v)) in ids]

    def export_bots(self):
        """Export window: choose which bots (all, sections, Hall of Fame, Library or your selection)."""
        w = tk.Toplevel(self.root)
        w.title("Export bots")
        w.resizable(False, False)
        f = ttk.Frame(w, padding=16)
        f.pack()
        ttk.Label(f, text="Export bots", font=("", 12, "bold")).pack(anchor="w")
        ttk.Label(f, foreground=MUTED, wraplength=440, text=(
            "The file is plain JSON. Besides each bot's settings and results it spells out its rules in plain English "
            "and explains every setting, so another app or an AI can read and rebuild it. No keys or balances are included.")
        ).pack(anchor="w", pady=(2, 10))
        board = self.tourn.board() if self.tourn else []
        tiers = [(r, self.tier(r)) for r in board]
        sel_ids = {id(v) for v in self.selected_variants()}
        scopes = [
            ("selected", f"The bots I selected ({len(sel_ids)})", [r for r in board if id(r["v"]) in sel_ids]),
            ("all", f"All bots on the farm ({len(board)})", board),
            ("beasts", f"BEASTS only ({sum(1 for _, t in tiers if t == 'beast')})", [r for r, t in tiers if t == "beast"]),
            ("rising", f"Rising ({sum(1 for _, t in tiers if t == 'rising')})", [r for r, t in tiers if t == "rising"]),
            ("eggs", f"Eggs ({sum(1 for _, t in tiers if t == 'egg')})", [r for r, t in tiers if t == "egg"]),
            ("good", f"Everything that isn't resting ({sum(1 for r in board if r['status'] != 'benched')})",
             [r for r in board if r["status"] != "benched"]),
            ("hof", f"My Hall of Fame ({len(self.hof())})", self.hof()),
            ("library", f"My Bot Library ({len(self.library())})", self.library()),
        ]
        first = next((k for k, _, it in scopes if it), "hof")
        choice = tk.StringVar(value=first)
        for key, label, items in scopes:
            ttk.Radiobutton(f, text=label, value=key, variable=choice,
                            state="normal" if items else "disabled").pack(anchor="w")
        also_txt = tk.BooleanVar(value=bool(self.cfg.get("export_txt", False)))
        ttk.Checkbutton(f, text="Also save a plain-text summary (.txt) next to it", variable=also_txt).pack(anchor="w", pady=(10, 0))

        def go():
            items = dict((k, it) for k, _, it in scopes)[choice.get()]
            if not items:
                return messagebox.showinfo("Export bots", "There are no bots in that group yet.", parent=w)
            p = filedialog.asksaveasfilename(title="Export bots", defaultextension=".json", parent=w,
                                             initialfile=f"sbf-{choice.get()}-bots.json", filetypes=[("Bot file", "*.json")])
            if not p:
                return
            rows = [dict(r, bar_minutes=self.session_bar) if "v" in r else r for r in items]
            payload = E.export_bots(rows, self.version)
            E.save_bots_file(p, payload)
            self.cfg["export_txt"] = also_txt.get()
            if also_txt.get():
                txt = os.path.splitext(p)[0] + ".txt"
                with open(txt, "w", encoding="utf-8") as fh:
                    fh.write(f"SBF bots, exported {payload['exported_at']}\n\n{payload['how_to_read']}\n\n")
                    for n, b in enumerate(payload["bots"], 1):
                        lt = b.get("lifetime") or {}
                        fh.write(f"{n}. {E.bot_nickname(E.bot_key(b))}\n   {b['rules']}\n")
                        if lt:
                            fh.write(f"   Results: return {lt.get('return_pct')}%, vs the market {lt.get('vs_holding_pct')}%, "
                                     f"win rate {lt.get('win_rate_pct')}%, fees ${lt.get('fees_paid', '-')}\n")
                        fh.write("\n")
            self.write(f"Exported {len(payload['bots'])} bots to {p}" + (" (+ .txt summary)" if also_txt.get() else ""))
            w.destroy()
        b = ttk.Frame(f)
        b.pack(anchor="e", pady=(12, 0))
        ttk.Button(b, text="Cancel", command=w.destroy).pack(side="left", padx=6)
        ttk.Button(b, text="Export...", style="Accent.TButton", command=go).pack(side="left")

    def import_bots(self):
        if not self.tourn or not self.seeded:
            return messagebox.showinfo("Import bots", "Start the bot with Wildcard ticked first, then import.")
        paths = filedialog.askopenfilenames(title="Import bots", filetypes=[("Bot file", "*.json"), ("All files", "*.*")])
        if not paths:
            return
        closes = {c: self.hist[c] for c in self.session_coins}
        added = 0
        for p in paths:
            bots, problems = E.read_bots_file(p)
            for msg in problems:
                self.write(f"{os.path.basename(p)}: {msg}", "warn")
            for d in bots:
                if any(v.name == d["name"] and v.tf == d.get("tf", 1)
                       and all(abs(float(v.params.get(k, 0)) - x) < 1e-9 for k, x in d["params"].items())
                       for v in self.tourn.vars):
                    self.write(f"Skipped {friendly_label(d['name'], d['params'])}: you already have it.", "warn")
                    continue
                if self.tourn.add_variant(d["name"], d["params"], closes, d["size"], note="Imported", tf=d.get("tf", 1),
                                          risk=d.get("risk"), traits=d.get("traits"), vetoes=d.get("vetoes"), layers=d.get("layers"), origin="imported") is None:
                    continue
                added += 1
                s = d.get("stats") or {}
                if s.get("recent_gain_pct") is not None:
                    self.write(f"Imported {friendly_label(d['name'], d['params'])}: it made {s['recent_gain_pct']:+.1f}% "
                               f"on its owner's prices. Now it has to prove itself on yours.")
        self.flush_events()
        self.refresh_board()
        if added:
            self.write(f"Imported {added} bot{'s' if added != 1 else ''}. They compete like any other strategy and only get followed if they do well on your prices.")
        else:
            messagebox.showinfo("Import bots", "No usable bots were found in that file.")

    def load_lineup(self):
        if not self.tourn or not self.seeded:
            return messagebox.showinfo("Load", "Start the bot with Wildcard ticked first.")
        saved = E.load_strategies()
        if not saved:
            return messagebox.showinfo("Load", "No saved strategies yet. Use 'Save lineup' first.")
        closes = {c: self.hist[c] for c in self.session_coins}
        for d in saved:
            self.tourn.add_variant(d["name"], d["params"], closes, d.get("size", 1.0), note="Loaded saved", tf=d.get("tf", 1),
                                   risk=d.get("risk"), traits=d.get("traits"), vetoes=d.get("vetoes"), layers=d.get("layers"), origin="saved")
        self.flush_events()
        self.refresh_board()

    # ---------------- trades list / live prices ----------------
    def refresh_trades(self):
        """Refresh the combined paper/live ledger, tolerating older or partially-written rows."""
        rows, seen = [], set()

        def add_rows(items):
            if not isinstance(items, (list, tuple)):
                return
            for r in items:
                if not isinstance(r, dict):
                    continue
                key = tuple(str(r.get(k, "")) for k in
                            ("time", "mode", "side", "coin", "qty", "price", "usd", "pl", "reason", "who"))
                if key not in seen:
                    seen.add(key)
                    rows.append(r)

        # Durable ledger first, then recover trades from both saved mode sessions.
        ledger = os.path.join(E.HOME_DIR, "trades.csv")
        try:
            with open(ledger, newline="", encoding="utf-8-sig") as f:
                add_rows(csv.DictReader(f))
        except (OSError, csv.Error, UnicodeError):
            pass
        for is_live in ("paper", "alpaca", "live"):
            saved = E.load_json(E.state_path(is_live), {})
            if isinstance(saved, dict):
                add_rows(saved.get("trades", []))
        add_rows(self.trades)
        add_rows(getattr(self, "mirror_trades", []))
        show_bots = True
        try:
            show_bots = bool(self.show_bots_var.get())
        except Exception:
            pass
        want = getattr(self, "trade_acct", None).get() if hasattr(self, "trade_acct") else "All accounts"
        if show_bots or want == "FARM TEST":
            add_rows(self.shadow_trades)

        def acct_of(r):
            m = str(r.get("mode", "")).lower()
            return str(r.get("acct") or "LIVE") if m == "live" else {"paper": "PRACTICE", "alpaca": "ALPACA PAPER",
                                                                         "bot": "FARM TEST"}.get(m, m.upper())
        if want != "All accounts":
            rows[:] = [r for r in rows if acct_of(r) == want]

        def number(value, default=0.0):
            try:
                result = float(value)
                return result if math.isfinite(result) else default
            except (TypeError, ValueError, OverflowError):
                return default

        t = self.trade_tree
        t.delete(*t.get_children())
        self._trade_rows = {}
        rows.sort(key=lambda item: str(item.get("time", "")), reverse=True)
        if hasattr(self, "trade_count_lbl"):
            paper_n = sum(str(r.get("mode", "")).lower() == "paper" for r in rows)
            live_by = {}
            for r in rows:
                if str(r.get("mode", "")).lower() in ("live", "alpaca"):
                    k_ = "ALPACA PAPER" if str(r.get("mode", "")).lower() == "alpaca" else str(r.get("acct") or "LIVE")
                    live_by[k_] = live_by.get(k_, 0) + 1
            live_by = live_by or {"LIVE": 0}
            bot_n = sum(str(r.get("mode", "")).lower() == "bot" for r in rows)
            self.trade_count_lbl.config(
                text=f"PRACTICE: {paper_n} trades  |  " + "  |  ".join(f"{k}: {n}" for k, n in sorted(live_by.items()))
                     + (f"  |  Farm tests (pretend): {bot_n}" if bot_n else ""))
        for r in rows[:1000]:
            side = str(r.get("side", "")).lower()
            pl = r.get("pl")
            try:
                is_bot = str(r.get("mode", "")).lower() == "bot"
                iid = t.insert("", "end", tags=("bot",) if is_bot else ("buy" if side == "buy" else "sell",), values=(
                    str(r.get("time", "")).replace("T", " "),
                    (str(r.get("acct") or "LIVE") if str(r.get("mode", "")).lower() == "live" else
                     {"paper": "PRACTICE", "alpaca": "ALPACA PAPER", "bot": "FARM TEST"}.get(str(r.get("mode", "")).lower(),
                                                                                              str(r.get("mode", "")).upper())),
                    side.upper() if side in ("buy", "sell") else str(r.get("side", "")).upper(),
                    r.get("coin", ""),
                    f"{number(r.get('qty')):.6f}".rstrip("0").rstrip("."),
                    f"{number(r.get('usd')):,.2f}", f"{number(r.get('price')):,.2f}",
                    "" if pl in ("", None) else f"{number(pl):+,.2f}", r.get("who", ""),
                    REASON_TEXT.get(r.get("reason"), r.get("reason", ""))))
                self._trade_rows[iid] = r
            except Exception:  # one odd row must not hide all the others
                continue
        resort(t)

    def refresh_prices(self):
        t = self.prices_tree
        t.delete(*t.get_children())
        stamp = self.last_tick.strftime("%H:%M:%S") if self.last_tick else "-"
        nxt = ""
        if self.last_tick and self.running and self.seeded:
            left = max(0, int(self.last_bar + self.session_bar * 60 - time.time()))
            nxt = f"  |  next decision in {left // 60}:{left % 60:02d}"
        self.tick_lbl.config(text=f"Prices updated {stamp}{nxt}" if self.last_tick else "")
        n1, n24 = max(1, int(60 // self.session_bar)), int(1440 // self.session_bar)

        def chg(px, ref):
            return "-" if not ref else f"{(px / ref - 1) * 100:+.2f}%"
        for c in (self.session_coins or [k for k, v in self.coin_vars.items() if v.get()]):
            px = self.last.get(c)
            if px is None:
                continue
            h = self.hist.get(c, [])
            mood, er, _ = E.market_mood(h)
            mood_txt = MOOD_TEXT.get(mood, mood) + (f"  ({er:.2f})" if mood != "unknown" else "")
            t.insert("", "end", values=(
                c, f"{px:,.6f}" if px < 1 else f"{px:,.4f}" if px < 10 else f"{px:,.2f}", chg(px, self.start_px.get(c)),
                chg(px, h[-n1 - 1] if len(h) > n1 else None), chg(px, h[-n24 - 1] if len(h) > n24 else None),
                mood_txt, stamp))

    def toggle_more(self):
        on = self.more_var.get()
        self.settings["winners_trade_more"] = on
        self.cfg["settings"] = self.settings
        E.save_json(CONFIG_PATH, self.cfg)
        if self.tourn:
            self.tourn.o["trade_more"] = on
        self.write("Winners will now trade more." if on else "Winners will trade at their normal pace.")

    def toggle_wild(self):
        on = self.wild_var.get()
        for b in self.strat_btns:
            b.config(state="disabled" if on else "normal")
        if on:
            self.strat_box.pack_forget()
            self.wild_note.pack(anchor="w", pady=(4, 0))
        else:
            self.wild_note.pack_forget()
            self.strat_box.pack(fill="x")

    # ---------------- coins ----------------
    def all_coins(self):
        out = list(COINS)
        for c in self.settings.get("extra_coins", []):
            if c not in out:
                out.append(c)
        return out

    def build_coin_grid(self):
        for w in self.coin_grid.winfo_children():
            w.destroy()
        for i, coin in enumerate(self.all_coins()):
            v = self.coin_vars.setdefault(coin, tk.BooleanVar(value=False))
            cb = ttk.Checkbutton(self.coin_grid, text=coin, variable=v, style="Card.TCheckbutton", command=self.save_ticked)
            cb.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 10), pady=1)

    def save_ticked(self):
        self.cfg["ticked_coins"] = [c for c, v in self.coin_vars.items() if v.get()]
        try:
            E.save_json(CONFIG_PATH, self.cfg)
        except OSError:
            pass

    def toggle_auto_coins(self):
        n = int(self.settings.get("auto_coins", 0))
        if self.auto_coin_var.get():
            self.settings["auto_coins"] = n if n > 0 else int(self.cfg.get("last_auto_coins", 4))
            if not self.wild_var.get():
                self.wild_var.set(True)
                self.toggle_wild()
            self.write(f"The bot will pick its own {self.settings['auto_coins']} coins when you press Start (Wildcard).")
        else:
            if n > 0:
                self.cfg["last_auto_coins"] = n
            self.settings["auto_coins"] = 0
        self.cfg["settings"] = self.settings
        E.save_json(CONFIG_PATH, self.cfg)

    def add_coins(self, coins):
        added = []
        for c in coins:
            c = c.strip().upper()
            if E.valid_coin(c) and c not in self.all_coins():
                self.settings["extra_coins"].append(c)
                added.append(c)
        for c in coins:
            c = c.strip().upper()
            if c in self.all_coins():
                self.coin_vars.setdefault(c, tk.BooleanVar()).set(True)
        self.cfg["settings"] = self.settings
        E.save_json(CONFIG_PATH, self.cfg)
        self.build_coin_grid()
        self.save_ticked()
        return added

    def open_coin_browser(self):
        if getattr(self, "coin_win", None) is not None and self.coin_win.winfo_exists():
            return self.coin_win.lift()
        w = self.coin_win = tk.Toplevel(self.root)
        w.title("Tickers")
        w.geometry("720x560")
        top = ttk.Frame(w, padding=(10, 10, 10, 4))
        top.pack(fill="x")
        ttk.Label(top, text="Popular stocks and ETFs (type any other ticker below)", font=("", 12, "bold")).pack(anchor="w")
        ttk.Label(top, foreground=MUTED, wraplength=680, text=(
            "Busiest first. Tick the ones you want and press Add. Thinly traded stocks have wider spreads and jump "
            "around more, which can eat into profits. Leveraged ETFs (like SPYU, 4x the S&P 500) make bigger moves; "
            "inverse ones (like SQQQ) rise when their index falls, so the buy-only bots can profit on down days too. "
            "Both reset daily, so held for days they drift from their multiple.")).pack(anchor="w", pady=(2, 6))
        row = ttk.Frame(top)
        row.pack(fill="x")
        ttk.Label(row, text="Search").pack(side="left")
        q = tk.StringVar()
        ttk.Entry(row, textvariable=q, width=16).pack(side="left", padx=6)
        status = ttk.Label(row, text="Loading the ticker list...", foreground=MUTED)
        status.pack(side="left", padx=8)
        mid = ttk.Frame(w, padding=(10, 0))
        mid.pack(fill="both", expand=True)
        t = self.make_tree(mid, ("coin", "kind", "price", "chg", "vol", "have"), (70, 170, 90, 80, 140, 90),
                           ("Ticker", "What it is", "Price", "Today", "Volume today ($)", "In your list"))
        t.column("kind", anchor="w")
        t.configure(selectmode="extended")
        data = {"all": []}

        def fill(*_):
            t.delete(*t.get_children())
            needle = q.get().strip().upper()
            mine = set(self.all_coins())
            for d in data["all"]:
                if needle and needle not in d["coin"]:
                    continue
                pr = d["price"]
                t.insert("", "end", iid=d["coin"], values=(
                    d["coin"], d.get("kind") or E.kind_text(d["coin"]) or "stock / ETF", "-" if pr is None else (f"{pr:,.6f}" if pr < 1 else f"{pr:,.2f}"),
                    "-" if d["change_24h"] is None else f"{d['change_24h']:+.2f}%",
                    "-" if d["volume_usd"] is None else f"{d['volume_usd']:,.0f}",
                    "yes" if d["coin"] in mine else ""))
            resort(t)
        q.trace_add("write", fill)

        def loaded(items, err=None):
            if not w.winfo_exists():
                return
            if err:
                status.config(text=f"Couldn't load the list: {err}", foreground=RED)
                return
            data["all"] = items
            status.config(text=f"{len(items)} tickers")
            fill()

        def work():
            try:
                items = E.list_products(self.cur_quote())
                self.q.put(("call", lambda: loaded(items)))
            except Exception as ex:
                msg = str(ex)
                self.q.put(("call", lambda: loaded([], msg)))
        threading.Thread(target=work, daemon=True).start()

        def add_sel():
            sel = list(t.selection())
            if not sel:
                return messagebox.showinfo("Tickers", "Select one or more tickers first (Ctrl/Shift-click for several).", parent=w)
            added = self.add_coins(sel)
            self.write(f"Added {', '.join(added)} to your tickers." if added else f"Ticked {', '.join(sel)}.")
            fill()

        def remove_sel():
            gone = [c for c in t.selection() if c in self.settings["extra_coins"]]
            if not gone:
                return messagebox.showinfo("Tickers", f"Select tickers you added yourself. The {len(COINS)} built-in ones stay.", parent=w)
            if self.session and set(gone) & set(self.session_coins):
                return messagebox.showinfo("Tickers", "That ticker is in the running session. Reset the session first.", parent=w)
            self.settings["extra_coins"] = [c for c in self.settings["extra_coins"] if c not in gone]
            for c in gone:
                self.coin_vars.pop(c, None)
            self.cfg["settings"] = self.settings
            E.save_json(CONFIG_PATH, self.cfg)
            self.build_coin_grid()
            self.save_ticked()
            fill()

        def bot_pick():
            if not data["all"]:
                return
            n = max(2, int(self.settings.get("auto_coins") or self.cfg.get("last_auto_coins", 4)))
            picks = E.scout_coins(data["all"], n)
            if not picks:
                return messagebox.showinfo("Coins", "No coin has enough volume right now.", parent=w)
            for c, v in self.coin_vars.items():
                v.set(False)
            self.add_coins(picks)
            self.write(f"The bot picked {', '.join(picks)} (busy coins that are moving). They are ticked now.")
            t.selection_set([c for c in picks if t.exists(c)])
            fill()
        b = ttk.Frame(w, padding=10)
        b.pack(fill="x")
        ttk.Button(b, text="Add / tick selected", style="Accent.TButton", command=add_sel).pack(side="left")
        ttk.Button(b, text="Remove selected", style="Plain.TButton", command=remove_sel).pack(side="left", padx=6)
        bp = ttk.Button(b, text="Let the bot pick now", style="Plain.TButton", command=bot_pick)
        bp.pack(side="left")
        Tip(bp, "Ticks the busiest coins that are actually moving today (stablecoins skipped).")
        ttk.Button(b, text="Close", command=w.destroy).pack(side="right")
        t.bind("<Double-1>", lambda e: add_sel() if t.identify_region(e.x, e.y) in ("cell", "tree") else None)

    def ui_running(self, on):
        self.root.after(50, self.grid_refresh)
        live = self.live if self.session else self.mode_var.get() == "live"
        self.btn.config(text=("Stop the LIVE bot" if live else "Stop the practice bot") if on else
                        ("Start LIVE bot (real money)" if live else "Start practice bot"),
                        style="Stop.TButton" if on else ("Live.TButton" if live else "Go.TButton"))
        if not on:
            self._gap_ref = None
        self.apply_awake()
        self.update_status()

    def apply_awake(self):
        quiet_bg = self.tray_hidden() and not (self.running and self.live)
        if self.running and self.settings.get("keep_awake", True) and not quiet_bg:
            self.awake.on()
        else:
            self.awake.off()  # background practice training never keeps the computer awake
        if self.tray is not None and getattr(self, "_low_prio", None) != quiet_bg:
            self._low_prio = quiet_bg
            T = self.tray_lib()
            if T:
                T.set_low_priority(quiet_bg)  # whatever you're doing on the computer comes first

    def update_status(self):
        live = self.live if self.session else self.mode_var.get() == "live"
        acct = self.acct_name(live) if (not live or self.client) else "LIVE"
        real = acct == "LIVE" and live  # real money: the only thing that's blue and says LIVE
        self.mode_pill.config(text="\u25cf  RUNNING" if self.running else "", fg=LIVE_BLUE if real else GREEN)
        on_p, on_l = not real, real
        both = not live and bool(self.cfg.get("mirror_paper"))
        self.seg_paper.config(bg=GREEN if on_p else LINE, fg="white" if on_p else MUTED,
                              text=("PRACTICE  +  ALPACA PAPER" if both else
                                    "PRACTICE" if acct == "PRACTICE" or real else f"PRACTICE  \u2022  {acct}")
                                   + ("  \u25cf" if on_p and self.running else ""))
        self.seg_live.config(bg=LIVE_BLUE if on_l else LINE, fg="white" if on_l else MUTED,
                             text=("LIVE  \u2022  REAL MONEY" + ("  \u25cf" if self.running else "")) if on_l else "LIVE")
        self.mode_stripe.config(bg=LIVE_BLUE if real else GREEN, height=6 if real else 4)
        self.body.config(highlightthickness=3 if real else 0, highlightbackground=LIVE_BLUE, highlightcolor=LIVE_BLUE)
        try:
            if real:
                self.dest_row.pack_forget()
            else:
                self.dest_row.pack(fill="x", pady=(4, 0), after=self.mode_desc)
                want = ("My Alpaca paper account  (ALPACA PAPER)" if live else
                        "Both: SBF and my Alpaca paper account" if both else "Stay in SBF  (PRACTICE)")
                if self.dest_var.get() != want:
                    self.dest_var.set(want)
        except tk.TclError:
            pass
        if real:
            self.headline_lbl.config(bg="#0a1f4d" if DARK else "#eff6ff", fg="#bfdbfe" if DARK else "#0b3aa8")
        else:
            self.headline_lbl.config(bg="#052e16" if DARK else "#ecfdf5", fg="#bbf7d0" if DARK else "#065f46")
        self.mode_desc.config(text=(("PRACTICE + ALPACA PAPER: simulated inside SBF, and every trade also copied to your Alpaca "
                                     "paper account." + ("" if self.is_paper_key() else " (Log in with your paper key to "
                                                                                        "start copying.)")) if both else {
                                    "PRACTICE": "PRACTICE: pretend money on real prices, simulated inside SBF.",
                                    "ALPACA PAPER": "ALPACA PAPER: pretend money, orders sent to your Alpaca paper account."}.get(
                                        acct, "LIVE: REAL MONEY through your broker.")), fg=LIVE_BLUE if real else GREEN)
        if not self.running:
            saved = None if self.session else self.saved_session(live)
            if self.session:
                txt = "Continue LIVE bot (real money)" if live else "Continue practice bot"
            elif saved:
                txt = (f"Continue {'LIVE' if live else 'practice'} session  "
                       f"(${float(saved.get('cash', 0)) + 0:,.0f} cash, {len(saved.get('trades', []))} trades)")
            else:
                txt = "Start LIVE bot (real money)" if live else "Start practice bot"
            self.btn.config(text=txt, style="Live.TButton" if live else "Go.TButton")
            if saved and not self.session:
                self.new_btn.config(text=f"Start a new {'LIVE' if live else 'practice'} session")
                if not self.new_btn.winfo_ismapped():
                    self.new_btn.pack(fill="x", pady=(0, 4), after=self.btn)
            elif self.new_btn.winfo_ismapped():
                self.new_btn.pack_forget()
        elif self.new_btn.winfo_ismapped():
            self.new_btn.pack_forget()
        if self.logging_in:
            self.conn_lbl.config(text="Logging in...", fg=MUTED)
        elif self.client:
            self.conn_lbl.config(text=f"Alpaca {'PAPER' if E.ALPACA.get('paper', True) else 'LIVE'} connected  |  buying power ${self.avail():,.2f}", fg=GREEN)
        else:
            self.conn_lbl.config(text="Not logged in" + ("  (needed for Live)" if live else ""), fg=MUTED)
        self.login_btn.config(text="Log out" if self.client else "Log in to Alpaca")
        self.budget_lbl.config(text=f"Money to trade with ({self.cur_quote()})" if live else f"Practice money (pretend {self.cur_quote()})")

    def saved_session(self, live=None):
        """The saved session for a mode (cached; re-read only when the file changes), or None."""
        if live is None:
            live = self.mode_var.get() == "live"
        path = E.state_path(self.sess_tag(live))
        return self._cached(path, lambda: E.load_json(path)) if os.path.exists(path) else None

    def start_choice(self, choice):
        """Start button: continue the saved session (or start one), or start a new one. While running it stops."""
        if self.running:
            return self.toggle()
        self._choice = choice
        try:
            self.toggle()
        finally:
            self._choice = "continue"

    # ---------------- "Both": practice trades copied to the Alpaca paper account ----------------
    def mirror_on(self):
        """Copy practice trades to Alpaca paper? Only in PRACTICE, only with Both chosen, and only ever with a PAPER key."""
        return (not self.live and bool(self.cfg.get("mirror_paper")) and self.is_paper_key())

    def mirror_open(self, c, qty, reason):
        if not self.mirror_on() or qty <= 0 or c in self.mirror:
            return
        self.mirror[c] = dict(pending=True)
        self.executor.submit(self._mirror_open, c, float(self.cost.get(c, 0)), reason)

    def _mirror_open(self, c, spend, reason):
        """Buys the same stock for the same money on the Alpaca paper account."""
        try:
            bk = self.brk()
            if not bk.paper():
                raise E.UnsafeOrder("the key isn't a paper key, so nothing was copied")
            got, avg = bk.buy_shares(tick_of(c), spend, self.settings.get("limit_wait", 20), True, self._stuck_order)
            if got <= 0:
                return self.q.put(("call", lambda: self._mirror_done(c, None, reason, "the order didn't fill in time")))
            rec = dict(symbol=tick_of(c), qty=got, avg=avg, cost=got * avg)
            self.q.put(("call", lambda: self._mirror_done(c, rec, reason)))
        except Exception as e:
            self.q.put(("call", lambda e=e: self._mirror_done(c, None, reason, str(e))))

    def _mirror_done(self, c, rec, reason, err=""):
        if rec is not None and c not in self.mirror:  # the account was reset while this copy was on its way
            self.mirror[c] = rec
            self.keep_mirror_copies()
            return
        if rec is None:
            self.mirror.pop(c, None)
            if time.time() - getattr(self, "_mirror_note", 0) > 600:
                self._mirror_note = time.time()
                self.write(f"ALPACA PAPER: the copy of {self.contract_label(c)} wasn't placed ({err}). The PRACTICE trade "
                           "itself is unaffected.", "warn")
            return
        self.mirror[c] = rec
        self._mirror_row("buy", c, rec["qty"], rec["avg"], rec["cost"], None, reason, rec)
        if self.pos.get(c, 0) <= 0:  # the practice trade closed while the copy was on its way
            self.mirror_close(c, "sync")
        self.save_state()

    def mirror_close(self, c, reason):
        rec = getattr(self, "mirror", {}).get(c)
        if not rec or rec.get("pending") or rec.get("closing") or not self.is_paper_key():
            return
        rec["closing"] = True
        self.executor.submit(self._mirror_close, c, dict(rec), reason)

    def _mirror_close(self, c, rec, reason):
        try:
            sym = rec["symbol"]
            bk = self.brk()
            if bk.held(sym) <= 0:
                return self.q.put(("call", lambda: self._mirror_closed(c, (0, 0.0), reason, gone=True)))
            urgent = reason in ("stop-loss", "manual", "trailing-stop", "scalp-flat", "kill switch", "sync")
            got, avg = bk.sell_shares(sym, rec["qty"], urgent, self.settings.get("limit_wait", 20), True, self._stuck_order)
            self.q.put(("call", lambda: self._mirror_closed(c, (got, avg) if got > 0 else None, reason)))
        except Exception as e:
            self.q.put(("call", lambda e=e: self._mirror_closed(c, None, reason, str(e))))

    def _mirror_closed(self, c, fill, reason, err="the order didn't fill", gone=False):
        rec = self.mirror.get(c)
        if not rec:
            return
        if gone:  # Alpaca no longer holds it (sold there by hand): just forget the copy
            self.mirror.pop(c, None)
            self.save_state()
            return
        if fill is None:
            rec.pop("closing", None)
            if time.time() - getattr(self, "_mirror_note", 0) > 600:
                self._mirror_note = time.time()
                self.write(f"ALPACA PAPER: couldn't sell the copy of {self.contract_label(c)} yet ({err}); trying again.", "warn")
            return
        got, avg = fill
        proceeds = got * avg
        basis = rec["cost"] * min(1.0, got / rec["qty"]) if rec["qty"] else rec["cost"]
        self._mirror_row("sell", c, got, avg, proceeds, proceeds - basis, reason, rec)
        rec["qty"] -= got
        rec["cost"] -= basis
        if rec["qty"] <= 1e-9:
            self.mirror.pop(c, None)
        else:
            rec.pop("closing", None)
        self.save_state()

    def _mirror_row(self, side, c, qty, px, usd, pl, reason, rec):
        label = self.contract_label(c)
        row = dict(time=datetime.now().isoformat(timespec="seconds"), mode="alpaca", acct="ALPACA PAPER", side=side, coin=label,
                   qty=qty, price=round(px, 6), usd=round(usd, 2), pl="" if pl is None else round(pl, 2), reason=reason,
                   who="copy of a PRACTICE trade")
        self.mirror_trades.append(row)
        del self.mirror_trades[:-5000]
        try:
            E.append_trade_csv(row)
        except OSError:
            pass
        self.write(f"ALPACA PAPER: {'bought' if side == 'buy' else 'sold'} {qty:,.4g} {label} at ${px:,.2f} "
                   + (f"({pl:+,.2f}) " if pl is not None else "") + "- a copy of the PRACTICE trade.")
        self._trades_dirty = True

    def is_paper_key(self):
        return bool(self.client) and self.broker() == "alpaca" and bool(E.ALPACA.get("paper", True))

    def click_practice(self):
        """PRACTICE on the switch: practice inside SBF (or stay on Alpaca paper if that's where practice already is)."""
        live = self.live if self.session else self.mode_var.get() == "live"
        if not live or self.acct_name(True) != "LIVE":
            return  # already practising (inside SBF, on the paper account, or both)
        self.switch_mode("paper")

    def click_live(self):
        """LIVE on the switch: real money only. With a paper key, explain where paper trading lives now."""
        if self.client and self.acct_name(True) != "LIVE":
            return messagebox.showinfo(
                "LIVE is real money", "LIVE trades real money and needs your live (real-money) Alpaca key. You're logged in "
                                      "with a paper key.\n\nTo trade your Alpaca paper account, stay in PRACTICE and choose "
                                      "'My Alpaca paper account' under Practice trades go to (Account, on the left).\n\n"
                                      "To trade real money, log out (Account menu) and log in with your live key.")
        self.switch_mode("live")

    def pick_practice_dest(self):
        """Practice trades go to: inside SBF (PRACTICE) or your Alpaca paper account (ALPACA PAPER)."""
        choice = self.dest_var.get()
        want_paper, both = choice.startswith("My Alpaca"), choice.startswith("Both")
        live = self.live if self.session else self.mode_var.get() == "live"
        if not want_paper and not both:
            self.cfg["mirror_paper"] = False
            E.save_json(CONFIG_PATH, self.cfg)
            if live:
                self.switch_mode("paper")
            self.update_status()
            return
        if not self.client:
            self.dest_var.set("Stay in SBF  (PRACTICE)")
            if messagebox.askyesno("Alpaca paper", "Sending practice trades to Alpaca needs your Alpaca PAPER key.\n\nLog in now?"):
                self.open_login()
            return
        if not self.is_paper_key():
            self.dest_var.set("Stay in SBF  (PRACTICE)")
            return messagebox.showinfo("Alpaca paper", "You're logged in with a real-money (live) key. Log out (Account "
                                                       "menu) and log in with your paper key to send practice trades to "
                                                       "your Alpaca paper account.")
        if both:
            self.cfg["mirror_paper"] = True
            E.save_json(CONFIG_PATH, self.cfg)
            if live:
                self.switch_mode("paper")
            self.write("PRACTICE + ALPACA PAPER: the practice session runs inside SBF, and every practice trade is also "
                       "copied to your Alpaca paper account (shown as ALPACA PAPER in the Trades list).", "good")
            self.update_status()
            return
        self.cfg["mirror_paper"] = False
        E.save_json(CONFIG_PATH, self.cfg)
        if not live:
            self.switch_mode("live", quiet=True)

    def switch_mode(self, target, quiet=False):
        """Practice and Live are separate: each keeps its own session. Switching saves and closes the current
        one, clears the screen, then shows the other mode (press Start to resume its saved session)."""
        cur = "live" if (self.live if self.session else self.mode_var.get() == "live") else "paper"
        if target == cur:
            return
        name = {"paper": "PRACTICE", "live": self.acct_name(True) if self.client else "LIVE"}
        if target == "live" and not quiet and name["live"] == "LIVE" and not messagebox.askyesno(
                "Switch to LIVE?", "LIVE trades your Alpaca account (real money with a live key).\n\nSwitch to LIVE mode?\n"
                                   "(No = stay in Practice. Nothing real happens until you also press Start and type LIVE.)"):
            self.write("Stayed in Practice.", "good")
            return
        if self.running and not messagebox.askyesno(
                "Switch mode", f"The {name[cur]} bot is running. Stop it and switch to {name[target]}?\n\n"
                               f"Your {name[cur]} session is saved and can be resumed later. Open positions stay open."):
            return
        if self.running:
            self.running = False
            self.ui_running(False)
        if self.session:
            self.save_state()
            self.save_farm(force=True)
            self.write(f"{name[cur]} session saved.")
        self.session = False
        try:
            cash = float(self.cash_var.get())
        except ValueError:
            cash = 1000.0
        self.reset_state(cash)
        self.live = target == "live"
        self.mode_var.set(target)
        self.refresh_board()
        self.refresh_trades()
        self.refresh_view()
        saved = E.load_json(E.state_path(self.sess_tag(target == "live")))
        if saved:
            self.write(f"Now in {name[target]}. A saved {name[target]} session is waiting (cash ${saved.get('cash', 0):,.2f}, "
                       f"{len(saved.get('trades', []))} trades): press Start to resume it.", "good" if target == "paper" else "warn")
        else:
            self.write(f"Now in {name[target]}.", "good" if target == "paper" else "warn")
        self.live = False  # no session yet; the mode switch decides what Start opens
        self.on_mode_change()

    def on_mode_change(self):
        self.update_team_label()
        if self.mode_var.get() == "live" and not self.client and not self.session:
            if messagebox.askyesno("Live mode", "Live mode trades your Alpaca account and needs your Alpaca login.\n\n"
                                                "Log in now? (You can stay in Practice mode instead.)"):
                self.open_login()
        self.update_status()

    def apply_style(self, name):
        if name in E.STYLES:
            self.settings.update(E.STYLES[name])
            self.settings["style"] = name
            self.style_var.set(name)
            self.cfg["settings"] = self.settings
            E.save_json(CONFIG_PATH, self.cfg)

    def on_style(self, _=None):
        name = self.style_var.get()
        if name in E.STYLES:
            self.apply_style(name)
            self.write(f"Risk style set to {name}: {STYLE_TIPS[name]}")

    def headline(self):
        live = self.live if self.session else self.mode_var.get() == "live"
        mode = {"PRACTICE": "PRACTICE (pretend money, inside SBF)", "ALPACA PAPER": "PRACTICE on your ALPACA PAPER account"
                }.get(self.acct_name(live), "LIVE (real money)")
        if not live and self.cfg.get("mirror_paper"):
            mode = "PRACTICE (inside SBF, every trade also copied to your ALPACA PAPER account)"
        if not self.running:
            if not self.session:
                return f"Ready. You're in {mode}. Pick your coins, then press Start."
            return f"Paused in {mode}. Press Start to continue."
        if not self.seeded:
            return "Loading recent price history..."
        text = f"{mode}: watching {', '.join(self.session_coins)}."
        if self.wild and self.wild_msg:
            text += " " + self.wild_msg
        if self.trades:
            t = self.trades[-1]
            text += f" Last action: {'bought' if t['side'] == 'buy' else 'sold'} {t['coin']} ({t['time'][11:16]})."
        else:
            text += " No trades yet. The bot only trades when a strategy sees a good moment, so waiting is normal."
        return text

    def write(self, msg, tag=None):
        if tag is None:
            low = msg.lower()
            tag = ("bad" if low.startswith(("halted", "order failed", "login failed", "stopped after")) or "lost $" in low
                   else "good" if "bought" in low or "made $" in low
                   else "warn" if "can't get prices" in low or "retired" in low or "no strategy" in low
                   else "info")
        try:
            path = os.path.join(E.HOME_DIR, "activity.log")
            os.makedirs(E.HOME_DIR, exist_ok=True)
            if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
                os.replace(path, path + ".old")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {E.redact(msg)}\n")
        except Exception:
            pass
        if self.closing():
            return
        self.log.config(state="normal")
        self.log.insert("end", f"{datetime.now():%H:%M}  {msg}\n", tag)
        self.log.see("end")
        self.log.config(state="disabled")

    def notify(self, title, msg, important=True, sound="notify"):
        """Desktop notification. Routine events (every trade) only notify if 'notify_trades' is on, the
        system sound only plays if 'sound' is on, and the mute button in the top bar silences everything."""
        if not self.settings.get("notify") or self.cfg.get("muted"):
            return
        if not important and not self.settings.get("notify_trades", False):
            return
        try:
            if self.settings.get("chime", True) and sound not in (self.settings.get("sounds_off") or []):
                E.play_sound(sound, self.settings.get("chime_volume", 60))
            msg = msg.replace('"', "'")
            if sys.platform == "darwin":
                subprocess.Popen(["osascript", "-e", f'display notification "{msg}" with title "{title}"'])
            elif sys.platform.startswith("linux") and shutil.which("notify-send"):
                subprocess.Popen(["notify-send", title, msg])
            elif sys.platform.startswith("win"):
                self.toast(title, msg)
        except Exception:
            pass

    def toast(self, title, msg, ms=6000):
        """A small silent pop-up in the corner of the screen (Windows), gone after 6 seconds."""
        try:
            w = tk.Toplevel(self.root)
            w.overrideredirect(True)
            w.attributes("-topmost", True)
            f = tk.Frame(w, bg=CARD, highlightthickness=1, highlightbackground=ACCENT, padx=12, pady=8)
            f.pack()
            tk.Label(f, text=title, bg=CARD, fg=TEXT, font=("", 9, "bold")).pack(anchor="w")
            tk.Label(f, text=msg, bg=CARD, fg=MUTED, wraplength=280, justify="left").pack(anchor="w")
            w.update_idletasks()
            sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
            w.geometry(f"+{sw - w.winfo_reqwidth() - 20}+{sh - w.winfo_reqheight() - 70}")
            w.bind("<Button-1>", lambda e: w.destroy())
            w.after(ms, w.destroy)
        except Exception:
            pass

    def toggle_mute(self):
        self.cfg["muted"] = not self.cfg.get("muted", False)
        E.save_json(CONFIG_PATH, self.cfg)
        self.mute_btn.config(text="\u266a off" if self.cfg["muted"] else "\u266a")
        self.write("Notifications muted." if self.cfg["muted"] else "Notifications on.")

    # ---------------- help / welcome ----------------
    def info_window(self, title, text, parent=None):
        if parent is None:
            w = tk.Toplevel(self.root)
            w.title(title)
        else:
            w = parent
        t = tk.Text(w, width=78, height=24, wrap="word", padx=14, pady=12, relief="flat", bg=CARD, fg=TEXT, font=("", 10))
        t.insert("1.0", text)
        t.config(state="disabled")
        t.pack(fill="both", expand=True, padx=6, pady=6)

    def show_guide(self, parent=None):
        if parent is None:
            return self.open_hub("help", 'How this works')
        self.info_window("How this works", (
            "THE SHORT VERSION\n"
            "A farm of bots watches stock prices and buys and sells shares. Each bot is a mix of genes (strategies, "
            "rules, stops, patience). Winners breed, losers retire, and only PROVEN bots get a vote on your account's "
            "trades: the best track records get the biggest say. You start in Practice, with pretend money.\n\n"
            "GETTING STARTED\n"
            "1. Log in with a free Alpaca paper key (Account > Log in to Alpaca): that's where prices come from.\n"
            "2. Tick the tickers to trade (or leave 'Bot picks' on), keep 'Let the bot choose (Wildcard)' ticked.\n"
            "3. Pick a Risk style, then press Start. Blue line = your account, grey = the market (your tickers bought at the start and left alone).\n\n"
            "LEARNING FROM THE PAST\n"
            "Tools > Learning: backtest any bot on any stretch since 2016, send a whole farm through Training camp, or "
            "switch on Free roam and let the farm study by itself (it only runs while it's switched ON).\n\n"
            "SAFETY BUILT IN\n"
            "SBF only buys shares and only sells shares it holds: it never sells short or borrows (no margin), so the "
            "most a trade can lose is what it paid. Under $25,000 it won't break the day-trade rule. Live mode needs you "
            "to type LIVE, and the KILL SWITCH (Account menu) stops everything.\n\n"
            "GOING LIVE\n"
            "Practice for a good while first, then try your Alpaca paper account, then small real amounts.\n\n"
            "HONEST WARNING\n"
            "Stocks can fall fast, and no strategy wins reliably. If the blue line stays below the grey one, the bots "
            "aren't beating the market, and you'd have done better just holding."), parent=parent)

    def show_glossary(self, parent=None):
        if parent is None:
            return self.open_hub("help", 'Words')
        self.info_window("What do these words mean?", (
            "Share: a small piece of a company (or of a fund, for ETFs like SPY). SBF can buy part of a share, so any "
            "trade size works on any stock.\n"
            "ETF: a fund that trades like a stock. SPY follows the S&P 500, QQQ the Nasdaq 100.\n"
            "Leveraged ETF: moves a multiple of its index each day (SPYU 4x the S&P 500, TQQQ 3x the Nasdaq 100). Inverse "
            "ETF: moves the opposite way (SQQQ rises 3% when the Nasdaq 100 falls 1%), so buy-only bots can profit on down "
            "days. Both reset daily: held for days they drift from their multiple, and choppy markets wear them down.\n"
            "Bid / ask: what buyers pay / sellers want. A wide gap between them means a thin, costly stock to trade.\n"
            "Limit order: buy or sell only at this price or better. Market order: right now, at whatever the price is.\n"
            "Short selling: selling shares you don't own. SBF never does it.\n"
            "Day trade: buying and selling the same stock on the same day. Under $25,000, 4 in 5 business days "
            "gets an account flagged, so SBF stops at 3.\n"
            "Proven bot: one with a real track record (finished trades, money made over its lifetime, passed exams). "
            "Only proven bots vote on your account's trades, and the best records get the biggest say.\n"
            "Practice / Live: pretend money / your broker account (paper or real).\n"
            "Wildcard: the farm tests many bots at once and the account follows the winners.\n"
            "Egg, Rising, BEAST: a bot's stage, from newborn to dominant. Hall of Fame: the champions you pick.\n"
            "Never-rule: something a bot refuses to do (like 'no new buys before the Fed'), graded in hindsight.\n"
            "Backtest: replaying past prices to see how a bot would have done. Past results don't predict the future.\n"
            "vs the market: how much better (or worse) a bot did than the market, meaning its tickers bought at the start and left alone.\n"
            "Drawdown: the biggest drop from a peak, a measure of how bumpy the ride was.\n"
            "Volatility: how much a price swings. VIXY stands in for the VIX, the market's 'fear gauge'."), parent=parent)

    def maybe_welcome(self):
        if not self.cfg.get("welcomed"):
            self.show_welcome()

    def show_welcome(self):
        w = tk.Toplevel(self.root)
        w.title("Welcome")
        w.bind("<Destroy>", lambda e: self.root.after(700, self.maybe_tour) if e.widget is w else None)
        w.resizable(False, False)
        f = ttk.Frame(w, padding=18)
        f.pack()
        ttk.Label(f, text="Welcome to SBF, the Stock Bot Farm", font=("", 14, "bold")).pack(anchor="w")
        ttk.Label(f, wraplength=440, text=(
            "A farm of bots watches stock prices and trades shares. They breed, compete and learn as they go, and "
            "only the proven ones get a say in your account's trades. You'll start in Practice mode with pretend "
            "money, so nothing real is at risk. Real trading only happens if you switch to Live and log in.")).pack(anchor="w", pady=(6, 12))
        ttk.Label(f, text="How much pretend money do you want to practice with?").pack(anchor="w")
        cash = tk.StringVar(value=self.default_cash_text())
        ttk.Entry(f, textvariable=cash, width=14).pack(anchor="w", pady=(2, 10))
        ttk.Label(f, text="What kind of trader should it be?").pack(anchor="w")
        style = tk.StringVar(value="Balanced")
        for name, tip in STYLE_TIPS.items():
            ttk.Radiobutton(f, text=f"{name}: {tip}", variable=style, value=name).pack(anchor="w")
        wild = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Let the bot choose its own strategies (recommended)", variable=wild).pack(
            anchor="w", pady=(10, 12))

        def go():
            try:
                assert float(cash.get()) > 0
            except Exception:
                return messagebox.showerror("Welcome", "Please type an amount of money, like 1000.", parent=w)
            self.cash_var.set(cash.get())
            self.apply_style(style.get())
            self.wild_var.set(wild.get())
            self.toggle_wild()
            self.cfg["welcomed"] = True
            E.save_json(CONFIG_PATH, self.cfg)
            w.destroy()
            if not self.running:
                self.toggle()

        def later():
            self.cfg["welcomed"] = True
            E.save_json(CONFIG_PATH, self.cfg)
            w.destroy()
        row = ttk.Frame(f)
        row.pack(anchor="e")
        ttk.Button(row, text="Look around first", command=later).pack(side="left", padx=6)
        ttk.Button(row, text="Start practicing", style="Accent.TButton", command=go).pack(side="left")

    # ---------------- chart / views ----------------
    # ---------------- chart ----------------
    CHART_RANGES = (("30 ticks", -30), ("2 min", 120), ("5 min", 300), ("15 min", 900), ("1 hr", 3600),
                    ("4 hr", 14400), ("1 day", 86400), ("1 wk", 604800), ("All", 0))
    CANDLE_CHOICES = {"Auto": 0, "5 s": 5, "15 s": 15, "1 min": 60, "5 min": 300, "15 min": 900, "1 hr": 3600, "4 hr": 14400}
    CHART_TYPES = ("Line", "Area", "Candles")
    CANDLE_SECS = (5, 10, 15, 30, 60, 120, 300, 900, 1800, 3600, 7200, 14400)

    LAYOUTS = {"1 chart": (1, 1), "2 charts": (1, 2), "3 charts": (1, 3), "4 charts (2x2)": (2, 2), "6 charts (3x2)": (2, 3)}
    PANEL_VIEWS = ("Account value", "Profit / loss %", "Drawdown", "Cash vs invested", "Selected bot", "Hall of Fame shadow")
    PANEL_TIPS = {"Hall of Fame shadow": "Shadow Live: what your Hall of Fame bots alone would have made with pretend "
                                         "money (gold), next to your account (blue).",
                  "Account value": "Your account's value (blue) next to the market (grey): your tickers bought at the start and left alone.",
                  "Profit / loss %": "Profit or loss since the start, in %, for the account and for the market.",
                  "Drawdown": "How far the account is below its best point so far. Closer to 0 is better.",
                  "Cash vs invested": "How much is in cash and how much is in coins over time.",
                  "Selected bot": "The equity of the bot selected on the farm or scoreboard (or the best one)."}

    def build_chart_controls(self, parent):
        bar = tk.Frame(parent, bg=CARD)
        bar.pack(fill="x", pady=(0, 4))
        tk.Label(bar, text="Charts", bg=CARD, fg=MUTED, font=("", 8, "bold")).pack(side="left")
        self.chart_layout = tk.StringVar(value=self.cfg.get("chart_layout", "2 charts"))
        if self.chart_layout.get() not in self.LAYOUTS:
            self.chart_layout.set("2 charts")
        lb = ttk.Combobox(bar, textvariable=self.chart_layout, values=list(self.LAYOUTS), state="readonly", width=14)
        lb.pack(side="left", padx=(4, 0))
        lb.bind("<<ComboboxSelected>>", lambda e: self.build_panels())
        Tip(lb, "How many charts to show at once. Pick what each one shows with the menu at its top-left. "
                "Drag the divider under the charts to make them taller.")
        tk.Label(bar, text="Candles", bg=CARD, fg=MUTED, font=("", 8, "bold")).pack(side="left", padx=(10, 0))
        self.candle_sel = tk.StringVar(value=self.cfg.get("candle_sel", "Auto"))
        cs = ttk.Combobox(bar, textvariable=self.candle_sel, values=list(self.CANDLE_CHOICES), state="readonly", width=6)
        cs.pack(side="left", padx=(4, 0))
        cs.bind("<<ComboboxSelected>>", lambda e: (self.cfg.__setitem__("candle_sel", self.candle_sel.get()), self.draw_chart()))
        Tip(cs, "Candle timeframe: how much time each candle covers on Candles charts. Auto picks one to suit the range.")
        tk.Label(bar, text="Range", bg=CARD, fg=MUTED, font=("", 8, "bold")).pack(side="left", padx=(10, 0))
        self.chart_range = tk.StringVar(value=self.cfg.get("chart_range", "All"))
        self.range_btns = {}
        rb = tk.Frame(bar, bg=CARD)
        rb.pack(side="left", padx=(2, 8))
        for label, _ in self.CHART_RANGES:
            b = tk.Label(rb, text=label, bg=CARD, fg=MUTED, padx=5, pady=1, cursor="hand2", font=("", 8))
            b.pack(side="left")
            b.bind("<Button-1>", lambda e, l=label: self.set_range(l))
            self.range_btns[label] = b
        self.chart_pct = tk.BooleanVar(value=bool(self.cfg.get("chart_pct", False)))
        self.chart_hold = tk.BooleanVar(value=bool(self.cfg.get("chart_hold", True)))
        self.chart_marks = tk.BooleanVar(value=bool(self.cfg.get("chart_marks", True)))
        self.chart_bot = tk.BooleanVar(value=bool(self.cfg.get("chart_bot", True)))
        self.chart_grid = tk.BooleanVar(value=bool(self.cfg.get("chart_grid", True)))
        for var, txt, tip in ((self.chart_grid, "Grid", "Background grid lines."),
                              (self.chart_bot, "Bot trades",
                               "On coin charts: the buys (hollow up arrows) and sells (hollow down arrows) of the "
                               "bot selected on the farm or scoreboard, or the best one if none is selected."),
                              (self.chart_marks, "My trades", "Filled arrows where your account bought and sold."),
                              (self.chart_hold, "Market", "The grey 'market' line on account charts: your tickers bought at the start and left alone."),
                              (self.chart_pct, "%", "Show change in % instead of dollars or prices.")):
            cb = ttk.Checkbutton(bar, text=txt, variable=var, style="Card.TCheckbutton", command=self.chart_opts_changed)
            cb.pack(side="right", padx=(5, 0))
            Tip(cb, tip)
        self.hover = {}
        self.paint_range_btns()

    def default_panels(self):
        coins = list(self.session_coins or [c for c, v in self.coin_vars.items() if v.get()]) or ["SPY"]
        views = ["Account value", coins[0], "Profit / loss %", coins[1] if len(coins) > 1 else "Drawdown",
                 "Cash vs invested", "Selected bot"]
        return [dict(view=v, type="Line") for v in views]

    def build_panels(self):
        """(Re)builds the chart grid for the chosen layout. Each panel picks what it shows and its chart type."""
        lay = self.chart_layout.get()
        self.cfg["chart_layout"] = lay
        rows, cols = self.LAYOUTS[lay]
        saved = self.cfg.get("chart_panels") or []
        specs = (saved + self.default_panels()[len(saved):])[:6] if isinstance(saved, list) else self.default_panels()
        for p in self.panels:
            p["frame"].destroy()
        self.panels = []
        fr = self.charts_frame
        for r in range(2):
            fr.rowconfigure(r, weight=1 if r < rows else 0)
        for cidx in range(3):
            fr.columnconfigure(cidx, weight=1 if cidx < cols else 0)
        for k in range(rows * cols):
            spec = specs[k] if k < len(specs) and isinstance(specs[k], dict) else self.default_panels()[k]
            pf = tk.Frame(fr, bg=CARD, highlightthickness=1 if rows * cols > 1 else 0, highlightbackground=LINE)
            pf.grid(row=k // cols, column=k % cols, sticky="nsew", padx=2, pady=2)
            head = tk.Frame(pf, bg=CARD)
            head.pack(fill="x")
            view = tk.StringVar(value=spec.get("view", "Account value"))
            kind = tk.StringVar(value=spec.get("type", "Line") if spec.get("type") in self.CHART_TYPES else "Line")
            vb = ttk.Combobox(head, textvariable=view, width=15, state="readonly")
            vb.pack(side="left")
            tb = ttk.Combobox(head, textvariable=kind, values=self.CHART_TYPES, width=7, state="readonly")
            tb.pack(side="left", padx=(4, 0))
            cv = tk.Canvas(pf, height=120, bg=CARD, highlightthickness=0)
            cv.pack(fill="both", expand=True)
            cv.bind("<Configure>", lambda e: self.later(self.draw_chart, 180))
            cv.bind("<Motion>", lambda e, cv=cv: self.chart_hover(e, cv))
            cv.bind("<Leave>", lambda e: (self.hover.clear(), self.draw_chart()))
            for box in (vb, tb):
                box.bind("<<ComboboxSelected>>", lambda e: self.panels_changed())
            self.panels.append(dict(frame=pf, view=view, type=kind, canvas=cv, box=vb))
        self.chart = self.panels[0]["canvas"]
        self.chart_view, self.chart_type = self.panels[0]["view"], self.panels[0]["type"]
        if hasattr(self, "split") and hasattr(self, "chart_card"):
            want = {1: 250, 2: 250, 3: 260, 4: 400, 6: 420}[rows * cols]
            try:
                self.split.paneconfigure(self.chart_card, height=max(want, int(self.cfg.get("chart_h", 0) or 0) if rows * cols < 4 else want))
            except tk.TclError:
                pass
        self.update_view_choices()
        self.draw_chart()

    def panels_changed(self):
        specs = [dict(view=p["view"].get(), type=p["type"].get()) for p in self.panels]
        old = self.cfg.get("chart_panels") or []
        self.cfg["chart_panels"] = specs + old[len(specs):6] if isinstance(old, list) else specs
        try:
            E.save_json(CONFIG_PATH, self.cfg)
        except OSError:
            pass
        self.draw_chart()

    def show_on_chart(self, view):
        """Puts a coin (or view) on a chart: one that already shows it, else the second chart (or the only one)."""
        if any(p["view"].get() == view for p in self.panels):
            return self.draw_chart()
        p = self.panels[1] if len(self.panels) > 1 else self.panels[0]
        p["view"].set(view)
        self.panels_changed()

    def set_range(self, label):
        self.chart_range.set(label)
        self.cfg["chart_range"] = label
        self.paint_range_btns()
        self.draw_chart()

    def paint_range_btns(self):
        for l, b in self.range_btns.items():
            on = l == self.chart_range.get()
            b.config(bg=ACCENT if on else CARD, fg="white" if on else MUTED)

    def chart_opts_changed(self):
        for k, v in (("chart_pct", self.chart_pct), ("chart_hold", self.chart_hold), ("chart_marks", self.chart_marks),
                     ("chart_bot", self.chart_bot), ("chart_grid", self.chart_grid)):
            self.cfg[k] = bool(v.get())
        self.draw_chart()

    def update_view_choices(self):
        coins = list(self.session_coins or [c for c, v in self.coin_vars.items() if v.get()])
        vals = list(self.PANEL_VIEWS) + coins
        for p in self.panels:
            if list(p["box"].cget("values")) != vals:
                p["box"].config(values=vals)
            if p["view"].get() not in vals:
                p["view"].set(coins[0] if coins and p is not self.panels[0] else "Account value")

    @staticmethod
    def _ts(iso):
        try:
            return datetime.fromisoformat(str(iso)).timestamp()
        except (TypeError, ValueError):
            return None

    def bar_time(self, n):
        """Approximate clock time of tournament decision n (decisions happen every bar)."""
        if not self.tourn:
            return None
        ref = self.last_bar or time.time()
        return ref - (self.tourn.n - n) * self.session_bar * 60

    def chart_bot_variant(self):
        if not self.tourn:
            return None
        v = self.selected_variant()
        if v:
            return v
        return self.tourn.active[0] if self.tourn.active else None

    def _range_seconds(self):
        return dict(self.CHART_RANGES).get(self.chart_range.get(), 0)

    def chart_data(self, view):
        """-> (series [(name, [(t, y)], color, width)], markers [(t, y, side, filled, label)], unit, coin or None)
        unit: '$' money, '%' already a percentage, 'px' a coin price."""
        series, marks = [], []
        pts = self.curve
        if pts and len(pts[0]) < 3:
            step, now = max(1, int(self.settings["refresh"])), time.time()
            pts = [(p[0], p[1], now - (len(pts) - 1 - i) * step) for i, p in enumerate(pts)]
        if view == "Profit / loss %":
            sc = self.start_cash or 1
            series.append(("account", [(p[2], (p[0] / sc - 1) * 100) for p in pts], ACCENT, 2))
            if self.chart_hold.get():
                series.insert(0, ("market", [(p[2], (p[1] / sc - 1) * 100) for p in pts], "#9ca3af", 1))
            return series, marks, "%", None
        if view == "Drawdown":
            peak, dd = 0.0, []
            for p in pts:
                peak = max(peak, p[0])
                dd.append((p[2], (p[0] / peak - 1) * 100 if peak else 0.0))
            series.append(("below best", dd, RED, 2))
            return series, marks, "%", None
        if view == "Cash vs invested":
            al = list(getattr(self, "alloc_hist", ()))
            series.append(("cash", [(t, ca) for t, ca, _ in al], "#9ca3af", 1))
            series.append(("in coins", [(t, inv) for t, _, inv in al], ACCENT, 2))
            return series, marks, "$", None
        if view == "Selected bot":
            v = self.chart_bot_variant()
            if v and v.eq and self.tourn:
                n0 = self.tourn.n - (len(v.eq) - 1)
                series.append((E.bot_nickname(v.key()), [(self.bar_time(n0 + i), e) for i, e in enumerate(v.eq)], "#a855f7", 2))
                for n, coin, side, px in v.log:
                    if n >= n0:
                        marks.append((self.bar_time(n), v.eq[n - n0], side, False,
                                      f"{coin} {getattr(v, 'log_ct', {}).get((n, coin), '')}".strip()))
            return series, marks, "$", None
        if view == "Hall of Fame shadow":
            series.append(("account", [(p[2], p[0]) for p in pts] if pts and len(pts[0]) >= 3 else [], ACCENT, 1))
            series.append(("Hall of Fame shadow", list(self.shadow_curve), "#f59e0b", 2))
            return series, marks, "$", None
        if view == "Account value" or view not in (self.session_coins or []):
            if pts and len(pts[0]) < 3:  # older saved sessions have no times: space them by the refresh rate
                step, now = max(1, int(self.settings["refresh"])), time.time()
                pts = [(p[0], p[1], now - (len(pts) - 1 - i) * step) for i, p in enumerate(pts)]
            series.append(("account", [(p[2], p[0]) for p in pts], ACCENT, 2))
            if self.chart_hold.get():
                series.insert(0, ("market", [(p[2], p[1]) for p in pts], "#9ca3af", 1))
            if self.chart_marks.get() and pts:
                import bisect
                ts_list = [p[2] for p in pts]
                for tr in self.trades[-400:]:
                    t = self._ts(tr.get("time"))
                    if t is None or t < ts_list[0]:
                        continue
                    i = min(bisect.bisect_left(ts_list, t), len(pts) - 1)
                    marks.append((t, pts[i][0], tr.get("side"), True,
                                  f"{str(tr.get('coin', '')).split(' ')[0]} {trade_kind(tr)}".strip()))
            return series, marks, "$", None
        c = view
        ticks = list(self.tick_px.get(c, ()))
        rng = self._range_seconds()
        h = self.hist.get(c, [])
        span_ticks = ticks[-1][0] - ticks[0][0] if len(ticks) > 1 else 0
        if (len(ticks) < 2 or (rng > 0 and rng > span_ticks * 1.2) or (rng == 0 and len(h) * self.session_bar * 60 > span_ticks * 1.5)) and h:
            ref = self.last_bar or time.time()  # longer than the live ticks cover: use the bar history
            hh = h[-6000:]
            bars = [(ref - (len(hh) - 1 - i) * self.session_bar * 60, px) for i, px in enumerate(hh)]
            ticks = bars + [t for t in ticks if t[0] > bars[-1][0]]
        series.append((c, ticks, coin_color(c) if self.cfg.get("chart_coin_colors", True) else ACCENT, 2))
        if self.chart_marks.get() and ticks:
            import bisect
            tts = [x[0] for x in ticks]
            for tr in self.trades[-400:]:
                if str(tr.get("coin", "")).split(" ")[0] == c:
                    t = self._ts(tr.get("time"))
                    if t is not None:  # the mark sits on the chart's price at that moment
                        i = min(bisect.bisect_left(tts, t), len(ticks) - 1)
                        marks.append((t, ticks[i][1], tr.get("side"), True,
                                      trade_kind(tr) + (" scalp" if "(scalp)" in str(tr.get("coin", "")) else "")))
        v = self.chart_bot_variant() if self.chart_bot.get() else None
        if v:
            for n, coin, side, px in v.log:
                if coin == c:
                    t = self.bar_time(n)
                    if t is not None:
                        marks.append((t, px, side, False, getattr(v, "log_ct", {}).get((n, coin), "")))
        return series, marks, "px", c

    def chart_tick(self):
        """Price ticks redraw the chart at most about once a second (Light animations: every 2 s); clicks and
        resizes still redraw at once. Most of the app's drawing time went into redrawing the same chart."""
        gap = 2.0 if self.settings.get("animations", "Full") != "Full" else 1.0
        since = time.time() - getattr(self, "_chart_at", 0)
        if since >= gap:
            self.draw_chart()
        else:
            self.later(self._chart_due, int((gap - since) * 1000) + 20)

    def _chart_due(self):
        if time.time() - getattr(self, "_chart_at", 0) >= 0.5:
            self.draw_chart()

    def draw_chart(self):
        self._chart_at = time.time()
        if self.in_bg():
            self._bg_dirty = True
            return
        if self.moving():  # dragging the window: draw once it stops
            return self.later(self.draw_chart, 300)
        try:
            self.update_view_choices()
        except Exception:
            pass
        for p in getattr(self, "panels", []):
            self.draw_one(p["canvas"], p["view"].get(), p["type"].get())

    def draw_one(self, c, view, kind_pick="Line"):
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        try:
            series, marks, unit, coin = self.chart_data(view)
        except Exception:
            series, marks, unit, coin = [], [], "$", None
        money = unit == "$"
        main = series[-1][1] if series else []
        if len(main) < 2 or w < 80:
            tip = self.PANEL_TIPS.get(view, "")
            c.create_text(w // 2, h // 2, width=max(120, w - 30), justify="center", fill=MUTED,
                          text=("The chart fills in once prices start coming in." if view != "Selected bot"
                                else "Select a bot on the farm or scoreboard.") + ("\n" + tip if tip and h > 90 else ""))
            return
        rng = self._range_seconds()
        t1 = main[-1][0]
        if rng < 0:
            t0 = main[max(0, len(main) + rng)][0]
        elif rng > 0:
            t0 = t1 - rng
        else:
            t0 = main[0][0]
        if t1 <= t0:
            t0 = t1 - 1
        pct = self.chart_pct.get() and unit != "%"
        kind = kind_pick if coin else ("Area" if kind_pick == "Area" else "Line")
        vis = []
        for name, pts, color, width in series:
            seg = [(t, y) for t, y in pts if t >= t0]
            if pts and (not seg or seg[0][0] > t0):  # carry the line in from the left edge
                before = [p for p in pts if p[0] < t0]
                if before:
                    seg.insert(0, (t0, before[-1][1]))
            vis.append((name, seg, color, width))
        base = {name: (seg[0][1] if seg and seg[0][1] else 1) for name, seg, _, _ in vis}
        main_name, main_seg = vis[-1][0], vis[-1][1]
        main_base = base[main_name]

        def val(name, y):
            return (y / base[name] - 1) * 100 if pct else y
        candles = []
        if kind == "Candles" and main_seg:
            span = max(t1 - t0, 1)
            step = self.CANDLE_CHOICES.get(self.candle_sel.get(), 0) or min(self.CANDLE_SECS, key=lambda s: abs(s - span / 70))
            step = max(step, self.session_bar * 60 if not self.CANDLE_CHOICES.get(self.candle_sel.get(), 0) and len(main_seg) and (main_seg[-1][0] - main_seg[0][0]) / max(len(main_seg), 1) >= self.session_bar * 50 else 0)
            cur = None
            for t, y in main_seg:
                b = int(t // step) * step
                if cur is None or cur[0] != b:
                    if cur:
                        candles.append(cur)
                    cur = [b, y, y, y, y]  # time, open, high, low, close
                else:
                    cur[2], cur[3], cur[4] = max(cur[2], y), min(cur[3], y), y
            if cur:
                candles.append(cur)
            for k in range(1, len(candles)):  # each candle opens where the last one closed
                candles[k][1] = candles[k - 1][4]
                candles[k][2] = max(candles[k][2], candles[k][1])
                candles[k][3] = min(candles[k][3], candles[k][1])
        thin = []
        for name, seg, color, width in vis:
            if len(seg) > 2 * w:  # thin out very dense lines; keep the first and last points
                kk = len(seg) // (2 * w) + 1
                seg = seg[:-1:kk] + [seg[-1]]
            thin.append((name, seg, color, width))
        vis = thin
        ys = [val(n, y) for n, seg, _, _ in vis for _, y in seg]
        if candles:
            ys += [val(main_name, y) for cd in candles for y in (cd[2], cd[3])]
        vm = [(t, (y / main_base - 1) * 100 if pct else y, side, filled, lab)
              for t, y, side, filled, lab in marks if t0 <= t <= t1 and y]
        ys += [m[1] for m in vm]
        if not ys:
            return
        lo, hi = min(ys), max(ys)
        pad = (hi - lo) * 0.08 or (abs(hi) * 0.001 + 0.01)
        lo, hi = lo - pad, hi + pad
        left, right, top, bot = (64, 10, 18, 18) if w > 380 else (52, 6, 16, 16)

        def X(t):
            return left + (t - t0) / (t1 - t0) * (w - left - right)

        def Y(v):
            return h - bot - (v - lo) / (hi - lo) * (h - top - bot)

        def fmt(v):
            if pct or unit == "%":
                return f"{v:+.2f}%"
            a = abs(v)
            return f"{v:,.0f}" if a >= 1000 else f"{v:,.2f}" if a >= 1 else f"{v:,.5f}"
        for k in range(5):
            v = lo + (hi - lo) * k / 4
            y = Y(v)
            if self.chart_grid.get():
                c.create_line(left, y, w - right, y, fill=LINE)
            c.create_text(left - 6, y, text=fmt(v), anchor="e", fill=MUTED, font=("", 8))
        span = t1 - t0
        for k in range(5):
            t = t0 + span * k / 4
            fmt_t = "%H:%M:%S" if span < 900 else "%H:%M" if span < 172800 else "%b %d"
            if self.chart_grid.get() and 0 < k < 4:
                c.create_line(X(t), top, X(t), h - bot, fill=LINE, dash=(1, 4))
            c.create_text(X(t), h - 4, text=datetime.fromtimestamp(t).strftime(fmt_t), anchor="s", fill=MUTED, font=("", 7))
        for name, seg, color, width in vis:
            if name == main_name and candles:
                continue
            if len(seg) >= 2:
                xy = [z for t, y in seg for z in (X(t), Y(val(name, y)))]
                if kind == "Area" and name == main_name:
                    c.create_polygon(X(seg[0][0]), h - bot, *xy, X(seg[-1][0]), h - bot, fill=color, outline="", stipple="gray25")
                c.create_line(*xy, fill=color, width=width)
        if candles:
            cw = max(1.0, (w - left - right) / max(len(candles), 1) * 0.6)
            for t, o, hi_, lo_, cl in candles:
                xm = X(t + (candles[1][0] - candles[0][0]) / 2 if len(candles) > 1 else t)
                col = GREEN if cl >= o else RED
                c.create_line(xm, Y(val(main_name, hi_)), xm, Y(val(main_name, lo_)), fill=col)
                y_o, y_c = Y(val(main_name, o)), Y(val(main_name, cl))
                c.create_rectangle(xm - cw / 2, min(y_o, y_c), xm + cw / 2, max(y_o, y_c) + 1, fill=col, outline=col)
        for t, y, side, filled, lab in vm:
            x, yy = X(t), Y(y)
            col = GREEN if side == "buy" else RED
            if side == "buy":
                pts = (x, yy + 3, x - 6, yy + 13, x + 6, yy + 13)
            else:
                pts = (x, yy - 3, x - 6, yy - 13, x + 6, yy - 13)
            c.create_polygon(*pts, fill=col if filled else CARD, outline=col, width=1 if filled else 2)
            if "scalp" in str(lab).split():  # an S marks the scalps
                c.create_text(x, yy + 20 if side == "buy" else yy - 20, text="S", fill=col, font=("", 7, "bold"))
        legend = [(n, col) for n, seg, col, _ in vis]
        if candles:
            legend[-1] = (f"{main_name} candles", legend[-1][1])
        xx = w - right
        if any(not m[3] for m in vm):
            v = self.chart_bot_variant()
            if v:
                legend.insert(0, (f"bot: {E.bot_nickname(v.key())}", MUTED))
        for name, col in reversed(legend):
            item = c.create_text(xx, 4, text=name, anchor="ne", fill=col, font=("", 8, "bold"))
            xx = c.bbox(item)[0] - 12
        hx = self.hover.get(str(c))
        if hx is not None and left <= hx <= w - right:
            t = t0 + (hx - left) / (w - left - right) * (t1 - t0)
            c.create_line(hx, top, hx, h - bot, fill=MUTED, dash=(2, 3))
            lines = [datetime.fromtimestamp(t).strftime("%H:%M:%S")]
            for name, seg, col, _ in vis:
                if seg:
                    p = min(seg, key=lambda q: abs(q[0] - t))
                    v = val(name, p[1])
                    lines.append(f"{name}: {fmt(v) if (pct or unit == '%') else '$' + fmt(v)}")
                    c.create_oval(X(p[0]) - 3, Y(v) - 3, X(p[0]) + 3, Y(v) + 3, outline=col, width=2)
            near = [m for m in vm if abs(X(m[0]) - hx) < 6]
            for m in near[:3]:
                lines.append(f"{'you' if m[3] else 'bot'} {m[2]} {m[4]}".rstrip())
            txt = c.create_text(0, 0, text="\n".join(lines), anchor="nw", fill=TEXT, font=("", 8))
            x0, y0, x1, y1 = c.bbox(txt)
            bx = hx + 10 if hx + 10 + (x1 - x0) < w else hx - 14 - (x1 - x0)
            c.move(txt, bx + 4, top + 4)
            bg = c.create_rectangle(bx, top, bx + (x1 - x0) + 8, top + (y1 - y0) + 8, fill=CARD, outline=LINE)
            c.tag_raise(txt, bg)

    def chart_hover(self, e, cv=None):
        self.hover = {str(cv or self.chart): e.x}
        if not getattr(self, "_hover_pending", False):  # redraw at most once per screen refresh
            self._hover_pending = True
            self.root.after(30, self._hover_draw)

    def _hover_draw(self):
        self._hover_pending = False
        self.draw_chart()

    def position_price(self, coin):
        """Value of ONE share the account holds in this book: the stock's latest price (what it paid per share when no
        price has come in yet)."""
        t = tick_of(coin)
        S = self.last.get(t) or (self.hist.get(t) or [0])[-1]
        if S:
            return S
        qty = self.pos.get(coin, 0)
        return self.cost.get(coin, 0) / qty if qty else 0.0

    def ctx_list(self, coins=None):
        """The market-context tickers the bots watch (minus any they trade)."""
        coins = coins if coins is not None else self.session_coins
        out = [c for c in (self.settings.get("ctx_tickers") or []) if E.valid_coin(c) and c not in coins]
        if E.scalp_share(self.settings):  # scalpers' volatility gates always read VIXY and SPY
            out += [c for c in ("VIXY", "SPY") if c not in out and c not in coins]
        return out

    def extra_now(self):
        """The extra data for a farm step: highs/lows/volumes/times per traded ticker, plus the context tickers."""
        x = {c: self.hx[c] for c in self.session_coins}
        if self.ctx_hist:
            x[E.CTX_KEY] = self.ctx_hist
        return x

    def add_ctx(self, extra, gran, count, quote="USD"):
        """Fetches the context tickers for a stretch of history and lines them up with its bar times."""
        coins = [c for c in extra if c != E.CTX_KEY]
        if not coins:
            return extra
        times = list(extra[coins[0]].get("t") or [])
        if not times:
            return extra
        out = {}
        for sym in self.ctx_list(coins):
            try:
                d = (E.fetch_range(sym, gran, times[0] - gran * 3, times[-1] + gran, quote) if not count or times[0] < time.time() - 120 * 86400
                     else E.fetch_ohlcv(sym, gran, count, quote))
                if d.get("t"):
                    out[sym] = E.align_ctx(times, d["t"], d["c"])
            except Exception:
                continue
        if out:
            extra[E.CTX_KEY] = out
        return extra

    def contract_label(self, c):
        """What the account holds in this book, for the log and the screens: 'SPY', or 'SPY (scalp)'."""
        return tick_of(c) + (" (scalp)" if is_scalp_book(c) else "")

    def equity(self):
        # Total value is always cash plus the marked-to-market value of open positions.
        return self.cash + sum(q * self.position_price(c) for c, q in self.pos.items())

    def _broker_loop(self):
        """Once a minute while you're logged in: your Alpaca account's own value, for its tile."""
        while True:
            try:
                if self.client and self.settings.get("show_broker_value", True) and self.broker() == "alpaca":
                    a = E.alp_account() or {}
                    eq, last = float(a.get("equity") or 0), float(a.get("last_equity") or 0)
                    self.broker_acct = dict(equity=eq, last=last, paper=bool(E.ALPACA.get("paper", True)), at=time.time())
                else:
                    self.broker_acct = None
                self.q.put(("call", self.refresh_broker_tile))
            except Exception:
                E.note_error("_broker_loop")
            time.sleep(60)

    def refresh_broker_tile(self):
        f = getattr(self, "tile_frames", {}).get("broker")
        b = getattr(self, "broker_acct", None)
        if f is None:
            return
        if not b or not self.client or not self.settings.get("show_broker_value", True):
            f.grid_remove()
            f.master.columnconfigure(5, weight=0)  # no empty gap where it was
            return
        f.grid()
        f.master.columnconfigure(5, weight=1)
        self.tile_titles["broker"].config(text="ALPACA PAPER ACCOUNT" if b["paper"] else "ALPACA ACCOUNT (LIVE)")
        self.tiles["broker"].config(text=f"${b['equity']:,.2f}", fg=TEXT)
        ch = b["equity"] - b["last"] if b["last"] else 0.0
        self.tile_subs["broker"].config(text=f"today {ch:+,.2f} ({ch / b['last'] * 100 if b['last'] else 0:+.2f}%)",
                                        fg=GREEN if ch > 0 else RED if ch < 0 else MUTED)

    def sess_tag(self, live=None):
        """The saved-session file for a mode: PRACTICE, ALPACA PAPER and LIVE each keep their own."""
        live = (self.live if self.session else self.mode_var.get() == "live") if live is None else live
        return {"PRACTICE": "paper", "ALPACA PAPER": "alpaca"}.get(self.acct_name(live), "live")

    def acct_name(self, live=None):
        """What the account is called on screen: PRACTICE (simulated inside SBF), ALPACA PAPER (the broker's own paper
        account) or LIVE (real money)."""
        live = (self.live if self.session else self.mode_var.get() == "live") if live is None else live
        if not live:
            return "PRACTICE"
        return "ALPACA PAPER" if E.ALPACA.get("paper", True) else "LIVE"

    def bench_equity(self):
        """What your starting money would be worth now if it had bought the session's tickers at the start (equal
        shares) and kept them: 'the market'. A ticker with no price right now counts at its last known price."""
        if not self.bench_units:
            return self.equity()
        share = self.start_cash / max(1, len(self.bench_units))
        tot = 0.0
        for c, u in self.bench_units.items():
            px = self.last.get(c) or (self.hist.get(c) or [0])[-1] or (share / u if u else 0)
            tot += u * px
        return tot

    def refresh_view(self):
        eq = self.equity()
        pl = eq - self.start_cash
        hold = self.bench_equity() - self.start_cash
        nxt = ""
        if self.running and self.seeded and self.last_tick:
            left = max(0, int(self.last_bar + self.session_bar * 60 - time.time()))
            nxt = f" | next {left // 60}:{left % 60:02d}"
        live = self.live if self.session else self.mode_var.get() == "live"
        mode = {"LIVE": "LIVE (REAL MONEY)"}.get(self.acct_name(live), self.acct_name(live))
        self.root.title(f"SBF {mode} - {'RUNNING ' if self.running else ''}{pl:+,.2f}{nxt}" if self.session else f"SBF {mode}")
        self.tiles["equity"].config(text=f"${eq:,.2f}")
        pct = pl / self.start_cash * 100 if self.start_cash else 0.0
        self.tiles["pl"].config(text=f"{pl:+,.2f}  ({pct:+.2f}%)", fg=GREEN if pl >= 0 else RED)
        subs = getattr(self, "tile_subs", {})
        if "pl" in subs:
            subs["pl"].config(text=self.acct_name(live) if self.session else "")
        if "hold" in subs:
            subs["hold"].config(text=(f"you {pl:+,.2f}   |   market {hold:+,.2f}" if self.session and self.curve and self.bench_units
                                      else ""))
        self.tiles["cash"].config(text=f"${self.cash:,.2f}")
        lead = pl - hold
        if not self.session or not self.curve:
            self.hold_title.config(text="VS THE MARKET")
            self.tiles["hold"].config(text="-", fg=TEXT)
        elif abs(lead) < 0.005 * self.start_cash:
            self.hold_title.config(text="SAME AS THE MARKET")
            self.tiles["hold"].config(text=f"{lead:+,.2f}", fg=MUTED)
        elif lead > 0:
            self.hold_title.config(text="BEATING THE MARKET")
            self.tiles["hold"].config(text=f"{lead:+,.2f}", fg=GREEN)
        else:
            self.hold_title.config(text="BEHIND THE MARKET")
            self.tiles["hold"].config(text=f"{lead:+,.2f}", fg=RED)
        dd = eq - self.day_eq if self.day_eq else 0.0
        self.tiles["day"].config(text=f"{dd:+,.2f}", fg=GREEN if dd >= 0 else RED)
        lim = float(self.settings.get("daily_loss", 0) or 0)
        if hasattr(self, "day_sub"):
            if lim > 0 and self.day_eq:
                used = max(0.0, -dd) / (self.day_eq * lim / 100)
                self.day_sub.config(text=f"{min(used, 1) * 100:.0f}% of today's loss limit used",
                                    fg=RED if used >= 0.75 else "#f59e0b" if used >= 0.4 else MUTED)
            else:
                self.day_sub.config(text="no daily limit" if lim <= 0 else "")
        try:
            self.refresh_portfolio()
        except Exception:
            note = self._record_error("updating the portfolio")
            if note:
                self.write(note, "bad")
        gap = pl - hold
        if not self.session or not self.curve:
            nm_ = self.acct_name()
            self.verdict_lbl.config(text="Press 'Start the bot' to begin. " + (
                "LIVE uses REAL money." if nm_ == "LIVE" else f"{nm_} uses pretend money."), fg=RED if nm_ == "LIVE" else MUTED)
        elif abs(gap) < 0.005 * self.start_cash:
            self.verdict_lbl.config(text="Doing about the same as the market so far.", fg=MUTED)
        elif gap > 0:
            self.verdict_lbl.config(text=f"The bot is ahead of the market by ${gap:,.2f}.", fg=GREEN)
        else:
            self.verdict_lbl.config(text=f"The bot is behind the market by ${-gap:,.2f}. That's common, "
                                         f"test the bots under Tools > Learning.", fg=RED)
        if self.session:
            last = self.trades[-1] if self.trades else None
            lt = (f" (last: {last['side'].upper()} {last['coin']} at {str(last.get('time', ''))[11:16]})" if last else "")
            self.activity_lbl.config(text=f"Your account ({self.acct_name()}) made {len(self.trades)} trades{lt}: "
                                          f"those are what the tiles above count. Separately, the farm's bots made "
                                          f"{len(self.shadow_trades)} test trades with pretend money inside the farm "
                                          f"(they never touch your balance).")
        else:
            self.activity_lbl.config(text="")
        if self.moving():
            return self.later(self.refresh_view, 300)
        self.ctx_card_refresh()
        self.refresh_prices()
        self.chart_tick()
        self.headline_lbl.config(text=self.headline())

    def refresh_board(self):
        if self.in_bg():
            self._bg_dirty = True
            return
        try:
            on_board = self.cur_tab() == "Strategy scoreboard"
        except Exception:
            on_board = True
        if self.tourn and not on_board:
            # hidden: only keep its list of bots in step (it also holds the farm's selection); details fill in when opened
            have = set(self.board.get_children())
            want = {str(id(v)) for v in self.tourn.vars}
            gone = have - want
            if gone:
                self.board.delete(*gone)
            for iid in want - have:
                self.board.insert("", "end", iid=iid, values=())
            self._board_dirty = True
            self.empty_board.place_forget()
            self.grid_refresh()
            return
        sel = self.board.selection()
        self.board.delete(*self.board.get_children())
        if not self.tourn:
            self.grid_refresh()
            self.empty_board.place(relx=0.5, rely=0.6, anchor="center")
            self.empty_board.lift()
            if hasattr(self, "best_lbl"):
                self.best_lbl.config(text="")
            return
        self.empty_board.place_forget()
        hofk = self.hof_keys()
        for r in self.tourn.board():
            sc = "-" if r["score"] is None else f"{r['score'] * 100:+.1f}%"
            cons = "-" if r["cons"] is None else f"{int(r['cons'] * 100)}%"
            size = f"{r['stake']:.2f}x".replace(".00x", "x") if r["status"] in ("active", "slowed") else "-"
            status = STATUS_TEXT.get(r["status"], r["status"])
            if r["status"] == "benched" and r.get("failing"):
                status = "Stopped (dropped fast)"
            elif r["status"] == "slowed" and r.get("behind"):
                status = "Shaky (not beating the market)"
            elif r["status"] == "slowed" and r.get("proving"):
                status = "Proving itself (live prices)"
            elif r["status"] == "slowed" and r.get("exam") == "failed":
                status = "Failed its exams (unseen days)"
            elif r["status"] == "active" and r.get("exam") == "passed":
                status += " \u2713 exams"
            lf = r["life"]
            in_hof = r["v"].key() in hofk
            star = "\u2605 " if in_hof else ""
            tags = (STATUS_TAG.get(r["status"], "learning"),) + (("hof",) if in_hof else ())
            self.board.insert("", "end", iid=str(id(r["v"])), tags=tags, values=(
                star + friendly_label(r["name"], r["params"], r.get("tf", 1), self.session_bar), sc, pct_txt(r.get("edge")),
                cons, "-" if lf["winrate"] is None else f"{lf['winrate'] * 100:.0f}%", pct_txt(lf["ret"]),
                size, status, lf["trades"]))
        resort(self.board)
        keep = [i for i in sel if self.board.exists(i)]
        if keep:
            self.board.selection_set(keep)
        self.grid_refresh()
        scored = [r for r in self.tourn.board() if r["score"] is not None]
        if scored and hasattr(self, "best_lbl"):
            best, worst = max(scored, key=lambda r: r["score"]), min(scored, key=lambda r: r["score"])
            lab = lambda r: f"{friendly_label(r['name'], r['params'], r.get('tf', 1), self.session_bar)} {r['score'] * 100:+.1f}%"
            self.best_lbl.config(text=f"Best: {lab(best)}   |   Worst: {lab(worst)}   |   {len(self.tourn.vars)} bots competing")
        elif hasattr(self, "best_lbl"):
            self.best_lbl.config(text=f"{len(self.tourn.vars)} bots competing (still learning)")

    # ---------------- controls ----------------
    def toggle(self):
        if self.running:
            self.running = False
            self.cfg["was_running"] = False  # stopped on purpose: don't auto-resume next time
            self.save_farm(force=True)
            try:
                E.save_json(CONFIG_PATH, self.cfg)
            except OSError:
                pass
            self.ui_running(False)
            self.save_state()
            if self.session:
                pl, hold = self.equity() - self.start_cash, self.bench_equity() - self.start_cash
                self.write(f"Stopped. Profit/loss {pl:+,.2f} (the market would be {hold:+,.2f}).")
            return
        resuming = bool(E.load_json(E.state_path(self.sess_tag(self.mode_var.get() == "live")))) and \
            getattr(self, "_choice", "continue") != "new"
        if (not self.session and not resuming and self.wild_var.get() and int(self.settings.get("auto_coins", 0)) > 0
                and not getattr(self, "_scouted", False)):
            return self.scout_then_start()
        self._scouted = False
        if self.session:
            coins = self.session_coins
        else:
            coins = [c for c, v in self.coin_vars.items() if v.get()]
        if not coins:
            return messagebox.showinfo("Pick coins", "Tick at least one coin for the bot to watch.")
        if not self.wild_var.get() and not any(v.get() for v in self.strat_vars.values()):
            return messagebox.showinfo("Pick strategies", "Tick a strategy, or tick 'Let the bot choose (Wildcard)'.")
        if not self.session and not self.begin_session(coins):
            return
        coins = list(self.session_coins) or coins  # a resumed session keeps its own coins
        if self.halted_day == date.today():
            if not messagebox.askyesno("Daily limit", "The daily max-loss limit was hit today. Resume anyway?"):
                return
            self.halted_day, self.day_eq = None, self.equity()
        self.wild = self.wild_var.get()
        self.active = [n for n, v in self.strat_vars.items() if v.get()]
        if self.wild and self.tourn is None:
            self.seeded = False
            if not getattr(self, "_restored_farm", None):
                t = self.load_farm()
                if t:
                    self._restored_farm = t
        if self.session and self.live and not self.client:
            return messagebox.showerror("Live mode", "Log in to Alpaca first.")
        if (not self.client and not self.logging_in and not E.ALPACA.get("key") and not self.tray_mode
                and not self.in_bg() and E.load_login() is None):
            ans = messagebox.askyesnocancel(
                "Log in for prices", "Practice trades pretend money, but the prices come from Alpaca, so the bots need "
                "a login before they can do anything. A free Alpaca paper key is enough.\n\nYes = log in now\n"
                "No = start anyway (the bots wait until you log in)\nCancel = don't start")
            if ans is None:
                return
            if ans:
                return self.open_login()
        self.running = True
        self.cfg.update(was_running=True, last_mode="live" if self.live else "paper", last_tag=self.sess_tag(),
                        last_wild=bool(self.wild_var.get()))
        try:
            E.save_json(CONFIG_PATH, self.cfg)
        except OSError:
            pass
        self.ui_running(True)
        self.write(f"Started ({'LIVE' if self.live else 'paper'}, {'wildcard' if self.wild else ', '.join(self.active)}, "
                   f"{E.fmt_bar(self.session_bar)} bars, limit orders).")
        self.worker_args = (list(coins), self.session_bar, self.wild, self.settings["fee_pct"] / 100,
                            self.settings["trade_pct"] / 100, self.settings["evolve_every"])
        self.launch_worker()

    def begin_session(self, coins, quiet=False):
        live = self.mode_var.get() == "live"
        saved = E.load_json(E.state_path(self.sess_tag(live)))
        resume = False
        if saved:  # the Continue / Start new buttons decide (no pop-up)
            resume = getattr(self, "_auto_resume", False) or getattr(self, "_choice", "continue") != "new"
        if not resume:
            self._restored_farm = None
            try:
                os.remove(E.farm_path(self.sess_tag(live)))  # a new session starts a new farm
            except OSError:
                pass
        try:
            budget = float(self.cash_var.get())
            assert budget > 0
        except Exception:
            if not resume:
                messagebox.showerror("Money amount", "Please type how much money to start with, like 1000.")
                return False
        if resume:
            self.reset_state(saved["start_cash"])
            self.load_state(saved)
            self.quote_var.set(self.quote)
        else:
            self.reset_state(budget)
            self.session_coins = list(coins)
            self.quote = self.quote_var.get()
        self.usd_avail = self.balances.get(self.quote, 0.0)
        self.refresh_trades()
        self.live = live
        if live and not self.confirm_live(resume):
            self.reset_state(1000.0)
            self.live = False
            self.write("LIVE was not confirmed, so you're back in Practice. Nothing real happened.", "good")
            self.root.after(10, lambda: self.switch_mode("paper"))
            return False
        self.session = True
        self.save_state()
        # Refresh immediately when starting or resuming, before the first price-feed tick.
        self.refresh_view()
        return True


    # ---------------- combined windows (hubs): related tools as tabs of one window ----------------
    HUB_SPEC = {
        "learning": ("Learning", ("Backtest a bot", "Hall of Fame test", "Training camp", "Free roam", "Farm knowledge"), (1180, 900)),
        "insights": ("Insights", ("Market context", "Economic calendar", "Weekly report", "Farm replay"), (1000, 720)),
        "system": ("System", ("Security", "Disk use", "Diagnostics"), (820, 700)),
        "family": ("Family", ("Family tree", "Bloodlines"), (1120, 680)),
        "help": ("Help", ("How this works", "Words", "Shortcuts"), (820, 640)),
    }
    HUB_REBUILD = {"Market context", "Weekly report", "Security", "Diagnostics", "Hall of Fame test", "Family tree",
                   "Bloodlines", "Farm replay", "Farm knowledge"}  # light tabs that show fresh data each time they're opened

    def _hub_builders(self):
        return {"Backtest a bot": lambda f, v=None: self.open_backtest(v, parent=f),
                "Hall of Fame test": lambda f: self.backtest_hof(parent=f),
                "Training camp": lambda f: self.open_training(parent=f),
                "Free roam": lambda f: self.open_roam(parent=f),
                "Farm knowledge": lambda f: self.open_knowledge(parent=f),
                "Market context": lambda f: self.open_context(parent=f),
                "Economic calendar": lambda f: self.open_calendar(parent=f),
                "Weekly report": lambda f: self.show_report(parent=f),
                "Farm replay": lambda f: self.open_replay(parent=f),
                "Security": lambda f: self.open_security(parent=f),
                "Disk use": lambda f: self.open_disk(parent=f),
                "Diagnostics": lambda f: self.show_diagnostics(parent=f),
                "Family tree": lambda f: self.open_family(parent=f),
                "Bloodlines": lambda f: self.open_bloodlines(parent=f),
                "How this works": lambda f: self.show_guide(parent=f),
                "Words": lambda f: self.show_glossary(parent=f),
                "Shortcuts": lambda f: self.show_shortcuts(parent=f)}

    def open_hub(self, key, tab=None, **kw):
        """Opens (or brings forward) one of the combined windows, on the given tab."""
        hubs = self.__dict__.setdefault("_hubs", {})
        h = hubs.get(key)
        if h is None or not h["w"].winfo_exists():
            title, tabs, (W, H) = self.HUB_SPEC[key]
            w = tk.Toplevel(self.root)
            w.title(title)
            w.geometry(f"{min(W, self.root.winfo_screenwidth() - 40)}x{min(H, self.root.winfo_screenheight() - 80)}")
            w.minsize(760, 520)
            w.configure(bg=BG)
            if key == "help":
                hb = tk.Frame(w, bg=BG, padx=10, pady=6)
                hb.pack(fill="x")
                ttk.Button(hb, text="Take the tour", style="Plain.TButton", command=self.start_tour).pack(side="left")
                ttk.Button(hb, text="Welcome screen", style="Plain.TButton", command=self.show_welcome).pack(side="left", padx=6)
            nb = ttk.Notebook(w)
            nb.pack(fill="both", expand=True, padx=8, pady=8)
            frames, built = {}, {}
            for t in tabs:
                f = tk.Frame(nb, bg=BG)
                nb.add(f, text=f"  {t}  ")
                frames[t] = f
            h = hubs[key] = dict(w=w, nb=nb, frames=frames, built=built, kw={})

            def on_tab(_=None, h=h):
                try:
                    t = nb.tab(nb.select(), "text").strip()
                except tk.TclError:
                    return
                if not built.get(t) or (t in self.HUB_REBUILD and built.get(t) != "fresh"):
                    self._hub_build(h, t)
                for o in built:
                    if o != t and built[o] == "fresh":
                        built[o] = True  # rebuild light tabs next time they're opened
            nb.bind("<<NotebookTabChanged>>", on_tab)
        if tab:
            if kw:
                h["kw"][tab] = kw
                if h["built"].get(tab):
                    self._hub_build(h, tab)
            h["nb"].select(h["frames"][tab])
        try:
            h["w"].deiconify()
            h["w"].lift()
            h["w"].focus_force()
        except tk.TclError:
            pass
        return h["w"]

    def _hub_build(self, h, t):
        f = h["frames"][t]
        for c in f.winfo_children():
            c.destroy()
        h["built"][t] = "fresh"
        try:
            self._hub_builders()[t](f, **h["kw"].pop(t, {}))
        except Exception:
            note = self._record_error(f"opening {t}")
            tk.Label(f, text=f"Couldn't open {t}. " + (note or ""), bg=BG, fg=RED, wraplength=700, justify="left").pack(padx=20, pady=20)

    def _nothing(self, parent, title, msg):
        """'Nothing to show yet', as a note in the tab (or a message box when opened on its own)."""
        if parent is None:
            return messagebox.showinfo(title, msg)
        tk.Label(parent, text=msg, bg=BG, fg=MUTED, wraplength=700, justify="left", font=("", 11)).pack(padx=30, pady=40, anchor="w")

    def open_learning(self, tab="Backtest a bot"):
        return self.open_hub("learning", tab)

    def open_insights(self, tab="Market context"):
        return self.open_hub("insights", tab)

    def open_system(self, tab="Security"):
        return self.open_hub("system", tab)

    def open_help(self, tab="How this works"):
        return self.open_hub("help", tab)

    def open_reset(self):
        """One Reset window: the account, the farm (optionally keeping some of its dens), a saved copy, or a saved farm back."""
        w = tk.Toplevel(self.root)
        w.title("Reset")
        w.resizable(False, False)
        f = ttk.Frame(w, padding=16)
        f.pack()
        ttk.Label(f, text="What should be reset?", font=("", 12, "bold")).pack(anchor="w")
        acct = tk.BooleanVar(value=True)
        farm = tk.BooleanVar(value=False)
        what = "LIVE session tracking" if self.live else "practice account"
        ttk.Checkbutton(f, text=f"The {what}: cash, positions and trades"
                                + ("" if self.live else " (you start again with the practice money on the left), and "
                                                        "the bots' own practice positions"),
                        variable=acct).pack(anchor="w", pady=(10, 0))
        ttk.Checkbutton(f, text="The farm: fresh eggs hatch at the next Start", variable=farm).pack(anchor="w", pady=(4, 0))
        # which dens to keep when the farm is reset
        dens = {"beast": [], "rising": [], "egg": [], "hof": []}
        if self.tourn:
            hof_keys = {E.bot_key(b) for b in E.load_hof()}
            for r in self.tourn.board():
                if r["v"].key() in hof_keys:
                    dens["hof"].append(r)
                dens[self.tier(r)].append(r)
        kf = ttk.Frame(f, padding=(22, 2, 0, 0))
        kf.pack(anchor="w")
        ttk.Label(kf, text="...but keep these bots (they rejoin the new farm, with what they learned):", foreground=MUTED).pack(anchor="w")
        keep = {}
        row = ttk.Frame(kf)
        row.pack(anchor="w", pady=(2, 0))
        for key, lab, on in (("beast", "BEASTS", True), ("rising", "Rising", False), ("egg", "Eggs", False),
                             ("hof", "Hall of Fame bots on the farm", True)):
            keep[key] = tk.BooleanVar(value=on and bool(dens[key]))
            ttk.Checkbutton(row, text=f"{lab} ({len(dens[key])})", variable=keep[key]).pack(side="left", padx=(0, 12))
        save = tk.BooleanVar(value=True)
        ttk.Checkbutton(kf, text="Save a copy of the whole farm first (you can bring it back below)", variable=save).pack(anchor="w", pady=(4, 0))

        def sync(*_):
            st = "normal" if farm.get() else "disabled"
            for wd in [c for r_ in (row,) for c in r_.winfo_children()] + [kf.winfo_children()[-1]]:
                try:
                    wd.config(state=st)
                except tk.TclError:
                    pass
        farm.trace_add("write", sync)
        sync()
        ttk.Label(f, foreground=MUTED, wraplength=520, justify="left", text=(
            "Your Library, Hall of Fame, settings and downloaded history are always kept."
            + ("\nLIVE positions stay in your broker account but won't be tracked by SBF any more."
               if self.live and any(q > 0 for q in self.pos.values()) else ""))).pack(anchor="w", pady=(10, 0))

        def go():
            if not acct.get() and not farm.get():
                return w.destroy()
            kept, note = [], ""
            if farm.get():
                if save.get() and self.tourn:
                    note = self.save_farm_copy()
                rows = []
                for key in ("beast", "rising", "egg", "hof"):
                    if keep[key].get():
                        rows += [r for r in dens[key] if r not in rows]
                if rows:
                    kept = E.export_bots(rows, self.version, note="kept through a farm reset")["bots"]
            w.destroy()
            if farm.get():
                self.reset_farm(ask=False)
                if kept:
                    self.cfg["carry_bots"] = kept
                    E.save_json(CONFIG_PATH, self.cfg)
            closed = 0
            if acct.get():
                if not self.live and not farm.get():  # (a farm reset clears the bots anyway)
                    closed = self.close_bot_positions()
                self.reset(ask=False)
            self.write("Reset " + " and ".join(x for x, on in ((what, acct.get()), ("the farm", farm.get())) if on)
                       + (f", keeping {len(kept)} bots for the new farm" if kept else "")
                       + (f", and closed the bots' {closed} open practice position{'s' if closed != 1 else ''}" if closed else "")
                       + "." + (f" {note}" if note else "")
                       + " Press Start to begin again.", "good")

        def bring_back():
            folder = os.path.join(E.HOME_DIR, "saved-farms")
            p_ = filedialog.askopenfilename(title="Bring back a saved farm", initialdir=folder if os.path.isdir(folder) else E.HOME_DIR,
                                            filetypes=[("Saved farm", "*.json")], parent=w)
            if not p_:
                return
            d = E.load_json(p_, None)
            if not isinstance(d, dict) or not isinstance(d.get("farm"), dict):
                return messagebox.showerror("Reset", "That file isn't a saved farm.", parent=w)
            if self.running:
                self.toggle()
            if self.tourn:
                self.save_farm_copy()
            shutil.copy2(p_, E.farm_path(self.sess_tag()))
            w.destroy()
            if self.session and d.get("coins") == self.session_coins and float(d.get("bar", -1)) == float(self.session_bar):
                t_ = self.load_farm()
                if t_:
                    self.tourn, self.seeded, self._restored_farm = t_, False, t_
                    self.refresh_board()
                    return self.write(f"Brought back the saved farm ({len(t_.vars)} bots). Press Start to carry on.", "good")
            self.write(f"The saved farm is ready, but it was made for {', '.join(d.get('coins') or [])} on "
                       f"{E.fmt_bar(float(d.get('bar', 1)))} decisions. Start a session with those to carry on with it "
                       f"(your farm from just now was saved first).", "warn")
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(14, 0))
        ttk.Button(b, text="Bring back a saved farm...", command=bring_back).pack(side="left")
        ttk.Button(b, text="Cancel", command=w.destroy).pack(side="right")
        ttk.Button(b, text="Reset", style="Accent.TButton", command=go).pack(side="right", padx=6)

    def save_farm_copy(self):
        """Saves the current farm to saved-farms/ (to bring back later). Returns a short note."""
        if not self.tourn:
            return ""
        folder = os.path.join(E.HOME_DIR, "saved-farms")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"farm-{'live' if self.live else 'practice'}-{datetime.now():%Y%m%d-%H%M%S}.json")
        try:
            E.save_json(path, dict(coins=self.session_coins, bar=self.session_bar, saved_at=time.time(),
                                   farm=self.tourn.to_state()))
            return f"A copy of the old farm ({len(self.tourn.vars)} bots) was saved first."
        except Exception:
            return ""

    def reset_farm(self, ask=True):
        if ask and not messagebox.askyesno("Reset the farm", "Clear every bot on the farm and hatch a fresh one next time you "
                                                             "press Start?\n\nYour Library and Hall of Fame are kept, and the "
                                                             "account's cash and trades are not touched."):
            return
        was = self.running
        if was:
            self.toggle()
        for live in (self.live,):
            try:
                os.remove(E.farm_path(self.sess_tag(live)))
            except OSError:
                pass
        self.tourn, self.seeded, self._restored_farm = None, False, None
        self.shadow_live, self.shadow_curve = None, []
        self.farm_hist.clear()
        self.__dict__.pop("tier_seen", None)
        self.refresh_board()
        self.write("The farm was reset: fresh eggs hatch when you press Start.", "good")

    def close_bot_positions(self):
        """Practice reset: the bots' own open pretend positions (Bot positions) close at what they're worth now, on the
        Bot Farm and the Slow farm. Their scores and learning don't change. -> how many were closed."""
        n = 0
        for t_ in (self.tourn, (getattr(self, "slow", None) or {}).get("t")):
            if t_ is not None:
                n += t_.close_positions(dict(self.last or {}))
        if n:
            try:
                self.save_farm(force=True)
            except Exception:
                E.note_error("close_bot_positions")
        try:
            self.refresh_botpos()
        except Exception:
            E.note_error("close_bot_positions")
        return n

    def default_cash_text(self):
        """The practice money SBF fills in: your saved default, or $10,000."""
        v = self.cfg.get("default_cash")
        try:
            return f"{float(v):g}" if v and float(v) > 0 else "10000"
        except (TypeError, ValueError):
            return "10000"

    def toggle_default_cash(self):
        """The Default box next to Practice money: ticked saves the amount as the default, unticked clears it."""
        if self.cash_default.get():
            try:
                v = float(str(self.cash_var.get()).replace(",", "").replace("$", ""))
                assert v > 0
            except (ValueError, AssertionError):
                self.cash_default.set(False)
                return messagebox.showinfo("Practice money", "Type an amount first (like 25000), then tick Default.")
            self.cfg["default_cash"] = v
            self.write(f"${v:,.2f} is now your default practice money.", "good")
        else:
            self.cfg.pop("default_cash", None)
            self.write("Practice money is back to the usual default ($10,000).")
        try:
            E.save_json(CONFIG_PATH, self.cfg)
        except OSError:
            E.note_error("toggle_default_cash")

    def _sync_default_cash(self):
        """The box shows ticked only while the amount typed is your saved default."""
        if not hasattr(self, "cash_default"):
            return
        try:
            v = float(str(self.cash_var.get()).replace(",", "").replace("$", ""))
        except ValueError:
            v = None
        d = self.cfg.get("default_cash")
        self.cash_default.set(bool(d) and v is not None and abs(v - float(d)) < 1e-9)

    def reset_account(self):
        """Reset account: starting money again, no positions, no trades. The farm and everything it learned stay."""
        if self.pending:
            return messagebox.showinfo("Reset account", "An order is on its way. Try again in a moment.")
        try:
            cash = float(self.cash_var.get())
        except ValueError:
            cash = self.start_cash or 1000.0
        msg = (f"Reset the {self.acct_name()} account to ${cash:,.2f}?\n\nOpen positions and the trade list are cleared. "
               "Your farms, bots and everything they've learned are kept.")
        if not self.live:
            msg += "\n\nThe bots' own practice positions (Bot positions) are closed too, at what they're worth now."
        if self.session and self.live and any(q > 0 for q in self.pos.values()):
            msg += ("\n\nThis only resets what SBF tracks: positions it bought stay in your broker account, "
                    "and you can close them there.")
        if self.mirror:
            msg += ("\n\nCopies on your Alpaca paper account stay open there, and SBF keeps looking after them with your "
                    "safety stop, target and trailing stop (see the Alpaca account tab).")
        if not messagebox.askyesno("Reset account", msg):
            return
        self.start_cash = self.cash = cash
        self.pos, self.cost, self.opt = {}, {}, {}
        self.trades, self.curve = [], []
        kept = self.keep_mirror_copies()  # (and the history of Alpaca copies is Alpaca's, so it stays in Trades)
        self.bench_units, self.high, self.bought_at, self.bot_alloc = {}, {}, {}, {}
        self.pending_who, self.pending_detail, self.pending_voters = {}, {}, {}
        self.trade_marks, self.curve_n, self.fees_paid = [], 0, 0.0
        self.day, self.day_eq, self.halted_day = None, None, None
        self.shadow_live, self.shadow_curve = None, []
        self.start_px = dict(self.last) if self.last else {}
        if self.session:
            self.save_state(force=True)
        for fn in (self.refresh_trades, self.update_status, self.refresh_view, self.refresh_board, self.draw_chart):
            try:
                fn()
            except Exception:
                E.note_error("reset_account")
        closed = 0 if self.live else self.close_bot_positions()
        if kept:
            self.write(f"{kept} cop{'ies' if kept != 1 else 'y'} on your Alpaca paper account stay open; SBF keeps looking after "
                       "them (Alpaca account tab).")
        self.write(f"Account reset to ${cash:,.2f}. The farm and its learning are kept"
                   + (f"; the bots' {closed} open practice position{'s' if closed != 1 else ''} were closed." if closed else "."))

    def reset(self, ask=True):
        if ask and (self.trades or self.pos):
            msg = "Clear this session and its saved state?"
            if self.live and any(q > 0 for q in self.pos.values()):
                msg += "\n\nLive shares the app bought will stay in your Alpaca account but won't be tracked."
            if not messagebox.askyesno("Reset", msg):
                return
        if self.running:
            self.running = False
            self.save_farm(force=True)
        self.ui_running(False)
        keep_trades = list(getattr(self, "mirror_trades", []) or []) if not self.live else []
        kept = self.keep_mirror_copies() if not self.live else 0
        try:
            os.remove(E.state_path(self.sess_tag()))
        except OSError:
            pass
        self.session = False
        try:
            cash = float(self.cash_var.get())
        except ValueError:
            cash = 1000.0
        self.reset_state(cash)
        self.mirror_trades = keep_trades
        self.refresh_trades()
        self.update_status()
        self.refresh_view()
        self.refresh_board()
        self.write("Session reset." + (f" {kept} cop{'ies' if kept != 1 else 'y'} on your Alpaca paper account stay open; SBF "
                                       "keeps looking after them (Alpaca account tab)." if kept else ""))

    def launch_worker(self):
        """Starts the price-feed thread. Any older feed notices the new number and exits."""
        self.worker_gen += 1
        t = threading.Thread(target=self.worker, daemon=True, args=tuple(self.worker_args) + (self.worker_gen,))
        self.worker_thread = t
        t.start()

    def retire_steps(self, bar_min):
        return int(float(self.settings["retire_hours"]) * 60 / max(float(bar_min), 0.25))

    def _seed(self, coins, bar_min, wild, fee, pct, evolve):
        gran = E.candle_gran(bar_min)
        for _ in range(40):  # a saved login still connecting: wait for it (history comes from Alpaca) up to 20 s
            if E.ALPACA.get("key") or not self.logging_in:
                break
            time.sleep(0.5)
        if bar_min < 1:
            self.q.put(("log", (f"Loading history. Nothing shorter than 1-minute bars is offered, so the bots warm up "
                                f"on those and switch to real {E.fmt_bar(bar_min)} bars as live prices arrive.", "info")))
        else:
            self.q.put(("log", f"Loading {E.fmt_bar(bar_min)} history..."))
        hist, hx = {}, {}
        for c in coins:
            try:
                d = E.fetch_ohlcv(c, gran, 2000, self.quote)  # enough for slow, long-holding bots too
                hist[c], hx[c] = d["c"], dict(h=d["h"], l=d["l"], v=d["v"], t=list(d.get("t") or []))
            except Exception as e:
                hist[c], hx[c] = [], dict(h=[], l=[], v=[], t=[])
                self.q.put(("log", f"History unavailable for {c}: {e}"))
        L = min(len(v) for v in hist.values())
        hist = {c: v[-L:] if L else [] for c, v in hist.items()}
        hx = {c: {k: v[-L:] if L else [] for k, v in d.items()} for c, d in hx.items()}
        if bar_min < 1:  # scale 1-minute volume to the shorter bar so volume strategies compare like with like
            hx = {c: dict(d, v=[x * bar_min for x in d["v"]]) for c, d in hx.items()}
        ctx = {}
        if L and self.settings.get("ctx_studies", True):
            try:
                ctx = self.add_ctx({coins[0]: hx[coins[0]]}, gran, 2000, self.quote).get(E.CTX_KEY) or {}
            except Exception:
                ctx = {}
        self.ctx_pending = {k: list(v_) for k, v_ in ctx.items()}
        hx_all = dict(hx)
        if ctx:
            hx_all[E.CTX_KEY] = ctx
        tourn = None
        restored = getattr(self, "_restored_farm", None)
        self._restored_farm = None
        if wild and restored is not None and set(restored.coins) == set(coins):
            restored.coins = list(coins)
            restored.o.update(E.tournament_opts(self.settings))
            restored.xfull = None
            self.q.put(("log", (f"Carrying on with your farm of {len(restored.vars)} bots where it left off.", "good")))
            self.q.put(("seed", (hist, restored, hx)))
            return
        if wild:
            opts = E.tournament_opts(self.settings)
            tourn = None
            if self.settings.get("multicore", True) and L > E.WARM and opts.get("islands", 1) > 1:
                self.q.put(("log", f"Warming up {opts['islands']} islands on several processor cores..."))
                tourn = E.warm_islands(coins, self.start_cash, fee, pct, evolve, self.settings["params"], opts,
                                       int(self.settings["max_bots"]), self.retire_steps(bar_min), hist, hx_all,
                                       log=lambda m: self.q.put(("log", m)), workers=self.bg_workers())
            if tourn is None:
                tourn = E.Tournament(coins, self.start_cash, fee, pct, evolve, params=self.settings["params"],
                                     opts=opts, cap=int(self.settings["max_bots"]),
                                     retire_steps=self.retire_steps(bar_min))
                if L > E.WARM:
                    tourn.warmup(hist, extra=hx_all)
            roster = self.roster("live" if self.live else "paper")
            squad = (E.team() if roster == "team" else self.hof()[:max(14, int(self.settings["max_bots"]))]
                     if roster == "hof" else [])
            if self.live and getattr(self, "grad_only", False):  # graduation: only bots that earned real money
                squad = [b for b in squad if self.graduated(b)[0]]
            if squad:
                for v in list(tourn.vars):
                    tourn._remove(v, "benched for your team")
                for b in squad:
                    tourn.add_variant(b["name"], b["params"], hist, b.get("size", 1.0),
                                      note=f"{'Team' if roster == 'team' else 'Hall of Fame'}: {b.get('nickname') or E.bot_nickname(E.bot_key(b))}",
                                      tf=b.get("tf", 1), risk=b.get("risk"), traits=b.get("traits"), vetoes=b.get("vetoes"), layers=b.get("layers"), origin="team")
                locked = roster == "hof" or self.settings.get("team_locked", True)
                if locked:
                    tourn.retire_steps, tourn.evolve_every = 0, 10 ** 9
                who = ", ".join(b.get("nickname") or E.bot_nickname(E.bot_key(b)) for b in squad)
                self.q.put(("log", (f"Trading with {'your team' if roster == 'team' else 'your Hall of Fame only'}: {who}"
                                    + (" (locked: no new bots)." if locked else " (they may breed)."), "good")))
            saved = [] if squad else E.load_strategies()
            for d in saved:
                tourn.add_variant(d["name"], d["params"], hist, d.get("size", 1.0), note="Loaded saved", tf=d.get("tf", 1),
                                  risk=d.get("risk"), traits=d.get("traits"), vetoes=d.get("vetoes"), layers=d.get("layers"), origin="saved")
            if saved:
                self.q.put(("log", f"Loaded {len(saved)} saved strategies into the tournament."))
            carry = self.cfg.pop("carry_bots", None) or []
            for b in carry if not squad else []:  # bots kept through a farm reset
                if b.get("name") not in E.STRATS:
                    continue
                v = tourn.add_variant(b["name"], b["params"], hist, b.get("size", 1.0), note="Kept from your last farm:",
                                      tf=b.get("tf", 1), risk=b.get("risk"), traits=b.get("traits"), vetoes=b.get("vetoes"),
                                      layers=b.get("layers"), origin="saved")
                if v:
                    for k_ in ("layer_stats", "veto_stats"):
                        if isinstance(b.get(k_), dict):
                            setattr(v, k_, {kk: list(x) for kk, x in b[k_].items()})
            if carry:
                self.q.put(("call", lambda: E.save_json(CONFIG_PATH, self.cfg)))
                self.q.put(("log", (f"{len(carry)} bots you kept rejoined the new farm.", "good")))
            if self.settings.get("hof_load", True) and not squad:
                have = {v.key() for v in tourn.vars}
                hof = [b for b in E.load_hof() if E.bot_key(b) not in have][:10]
                for b in hof:
                    v = tourn.add_variant(b["name"], b["params"], hist, b.get("size", 1.0), note="Hall of Fame:",
                                          tf=b.get("tf", 1), risk=b.get("risk"), traits=b.get("traits"), vetoes=b.get("vetoes"), layers=b.get("layers"), origin="hall of fame")
                    if v:
                        v.inducted = True
                if hof:
                    self.q.put(("log", (f"Brought back {len(hof)} Hall of Fame bots. They still have to win on today's prices.", "good")))
        self.q.put(("seed", (hist, tourn, hx)))

    def worker(self, coins, bar_min, wild, fee, pct, evolve, gen=0):
        def alive():
            return self.running and self.worker_gen == gen
        if not self.seeded:
            try:
                self._seed(coins, bar_min, wild, fee, pct, evolve)
            except Exception:
                self.q.put(("log", (self._record_error("loading history") + " Starting without history.", "bad")))
                tourn = (E.Tournament(coins, self.start_cash, fee, pct, evolve, params=self.settings["params"],
                                      opts=E.tournament_opts(self.settings), cap=int(self.settings["max_bots"]),
                                      retire_steps=self.retire_steps(bar_min))
                         if wild else None)
                self.q.put(("seed", ({c: [] for c in coins}, tourn, {c: dict(h=[], l=[], v=[]) for c in coins})))
        down_since, last_warn = None, 0.0
        batch_ok, last_vol = True, 0.0
        gran = E.candle_gran(bar_min)
        closed_note = 0.0
        while alive():
            try:
                try:  # stocks only trade 9:30-16:00 New York time on weekdays
                    clk = E.market_clock()
                    self.q.put(("market", clk))
                except E.NeedLogin as e:
                    if time.time() - closed_note > 600:
                        closed_note = time.time()
                        self.q.put(("log", (str(e), "warn")))
                    time.sleep(5)
                    continue
                except Exception:
                    clk = {"is_open": True}
                if not clk.get("is_open"):
                    if time.time() - closed_note > 1800:
                        closed_note = time.time()
                        self.q.put(("log", (f"The market is closed; it opens {datetime.fromtimestamp(clk['next_open']):%a %b %d %H:%M}. "
                                            f"The farm waits (Free roam and the Training camp can keep learning from the past).", "info")))
                    for _ in range(150):
                        if not alive():
                            return
                        time.sleep(0.2)
                    continue
                prices, fails = {}, 0
                t_req = time.time()
                if batch_ok:  # one request for every coin
                    try:
                        prices = E.spot_many(coins + [x for x in self.ctx_list(coins) if x not in coins], self.quote)
                    except Exception:
                        prices = {}
                    if not prices:
                        batch_ok = False
                        self.q.put(("log", ("Fast all-coins price request isn't working, using one request per coin.", "warn")))
                missing = [c for c in coins if c not in prices]
                for c in missing:
                    if not alive():
                        return
                    try:
                        prices[c] = E.spot(c, self.quote)
                        fails = 0
                    except Exception:
                        fails += 1
                        if fails >= 2:  # the connection is down, don't wait on every remaining coin
                            break
                now = time.time()
                self.q.put(("net", (sum(1 for c in coins if c in prices), len(coins), now - t_req)))
                if prices and now - last_vol >= max(60.0, bar_min * 60):  # real trading volume for volume strategies
                    last_vol = now
                    vols = {}
                    for c in coins:
                        if not alive():
                            return
                        try:
                            v = E.latest_volume(c, gran, self.quote)
                            if v is not None:
                                vols[c] = v * (bar_min if bar_min < 1 else 1)
                        except Exception:
                            E.note_error("worker")
                    if vols:
                        self.q.put(("vols", vols))
                if all(c in prices for c in coins):
                    if down_since is not None:
                        mins = (now - down_since) / 60
                        self.q.put(("log", (f"Prices are back after {mins:.0f} min." if mins >= 1
                                            else "Prices are back.", "good")))
                    down_since = None
                elif down_since is None:
                    down_since = last_warn = now
                    self.q.put(("log", ("Can't get prices from Alpaca right now. Check your internet "
                                        "connection. I'll keep trying.", "warn")))
                elif now - last_warn >= 600:
                    last_warn = now
                    self.q.put(("log", (f"Still no prices after {(now - down_since) / 60:.0f} min. Still trying.", "warn")))
                if prices and all(c in prices for c in coins):
                    try:
                        self._find_gap(coins, bar_min, gran)
                    except Exception:
                        E.note_error("backfill")
                if prices:
                    self.q.put(("prices", prices))
                if self.live and self.client and time.time() - self.last_bal > 30:
                    self.last_bal = time.time()
                    try:
                        acct = E.alp_account()
                        self.acct_info = acct
                        self.q.put(("balances", self.read_balances(acct)))
                    except Exception:
                        E.note_error("worker")
            except Exception:  # never let the feed die
                self.q.put(("log", (self._record_error("fetching prices"), "bad")))
                time.sleep(2)
            for _ in range(max(1, int(self.settings["refresh"])) * 5):
                if not alive():
                    return
                time.sleep(0.2)

    # ---------------- filling holes in the price history ----------------
    def _find_gap(self, coins, bar_min, gran):
        """(Price feed thread.) If the last candle is more than 3 candles old while the market is open (the bot was
        stopped, the computer slept, the connection dropped or one ticker's price didn't come back), the missing
        candles are fetched from Alpaca's history and handed over before the next new candle, so the charts and the
        bots' indicators have the real prices instead of a hole. The farm doesn't trade them after the fact."""
        step = max(60.0, bar_min * 60)
        try:
            last = min(float(self.hx[c]["t"][-1]) for c in coins)
        except (KeyError, IndexError, ValueError, TypeError):
            return
        now = time.time()
        if now - last < 3 * step or last == getattr(self, "_gap_tried", None):
            return
        self._gap_tried = last  # one try per hole
        start = max(last + 1, now - 6.5 * 3600)  # (at most one trading day back)
        data = {}
        for c in coins:
            d = E.fetch_range(c, gran, start, now - step, self.quote)
            if not d.get("t"):
                return
            data[c] = d
        closes, extra = E.common_stretch(data)
        ts = (next(iter(extra.values()), {}) or {}).get("t") or []
        if ts:
            self.q.put(("backfill", (closes, extra, last)))

    def on_backfill(self, closes, extra, since):
        """Adds the fetched candles (only ones newer than the last candle there is) to each ticker's history."""
        coins = list(self.session_coins)
        if not coins or set(closes) != set(coins):
            return
        try:
            last = min(float(self.hx[c]["t"][-1]) for c in coins)
        except (KeyError, IndexError, ValueError, TypeError):
            return
        ts = extra[coins[0]]["t"]
        new = [i for i, t_ in enumerate(ts) if t_ > last + 1]
        if not new:
            return
        keep = min(8000, max(2400, int(1440 / self.session_bar) + 20))
        for c in coins:
            h, x = self.hist[c], self.hx[c]
            for i in new:
                h.append(closes[c][i])
                x["h"].append(extra[c]["h"][i])
                x["l"].append(extra[c]["l"][i])
                x["v"].append(extra[c]["v"][i])
                x["t"].append(ts[i])
            del h[:-keep]
            for k in ("h", "l", "v", "t"):
                del x[k][:-keep]
        for sym, q in self.ctx_hist.items():  # watched tickers stay lined up (flat over the hole: no move read into it)
            if q:
                q.extend([q[-1]] * len(new))
                del q[:-len(self.hist[coins[0]])]
        a, b = datetime.fromtimestamp(since), datetime.fromtimestamp(ts[new[-1]])
        self.write(f"Filled in {len(new)} missing candle{'s' if len(new) != 1 else ''} from Alpaca's history "
                   f"({a:%H:%M} to {b:%H:%M}), so the charts have no hole. The bots didn't trade them after the fact.", "info")
        try:
            self.draw_chart()
        except Exception:
            E.note_error("on_backfill")

    def watchdog(self):
        """Every 5 seconds: restart the price feed if it died, and say so if prices have gone stale."""
        try:
            self._show_err_badge()
            if self.running:
                t = self.worker_thread
                if t is not None and not t.is_alive() and self.worker_args:
                    self.write("The price feed stopped unexpectedly. Restarting it.", "warn")
                    self.launch_worker()
                if self.last_tick:
                    age = (datetime.now() - self.last_tick).total_seconds()
                    if age > max(45, 6 * int(self.settings["refresh"])):
                        mins, secs = divmod(int(age), 60)
                        self.tick_lbl.config(fg=RED, text=f"No new prices for {mins}m {secs:02d}s. Reconnecting...")
                        if age > 300 and not self._stale_sent:
                            self._stale_sent = True
                            self.notify("SBF", "No prices for over 5 minutes. Check your connection.", sound="problem")
                    else:
                        self._stale_sent = False
                        self.tick_lbl.config(fg=MUTED)
            else:
                self.tick_lbl.config(fg=MUTED)
        except Exception:
            self.write(self._record_error("checking the connection"), "bad")
        finally:
            self.root.after(5000, self.watchdog)

    def _record_error(self, where, text=None):
        """Saves a traceback to crash.log and returns a one-line message. Safe to call from any thread."""
        import traceback
        text = text or traceback.format_exc()
        last = (text.strip().splitlines() or ["error"])[-1]
        key, now = (where, last), time.time()
        if now - self._err_seen.get(key, 0) >= 60:  # don't repeat the same problem every few seconds
            self._err_seen[key] = now
            self.err_count = getattr(self, "err_count", 0) + 1
            try:
                os.makedirs(E.HOME_DIR, exist_ok=True)
                with open(os.path.join(E.HOME_DIR, "crash.log"), "a") as f:
                    f.write(f"\n--- {datetime.now():%Y-%m-%d %H:%M:%S} ({where}) ---\n{E.redact(text)}")
            except OSError:
                pass
            return f"Internal error while {where}: {last}. The bot kept running (details in crash.log)."
        return ""

    def _show_err_badge(self):
        n = getattr(self, "err_count", 0)
        if n and hasattr(self, "err_lbl"):
            self.err_lbl.config(text=f"{n} error{'s' if n != 1 else ''} - click for details")
            self.err_lbl.pack(side="right", padx=14)

    def diagnostics_text(self):
        def tail(name, n):
            try:
                with open(os.path.join(E.HOME_DIR, name), encoding="utf-8", errors="replace") as f:
                    return "".join(f.readlines()[-n:]).strip() or "(empty)"
            except OSError:
                return "(not found)"
        try:
            tk_ver = str(tk.TkVersion)
        except Exception:
            tk_ver = "?"
        info = [f"SBF {self.version}", f"Python {sys.version.split()[0]}  Tk {tk_ver}  {sys.platform}",
                f"Data folder: {E.HOME_DIR}",
                f"Session: {self.session}  live: {self.live}  running: {self.running}",
                f"Cash: {self.cash:.2f}  start: {self.start_cash:.2f}  positions: {dict(self.pos)}",
                f"Trades in memory: {len(self.trades)}  rows shown: {len(self.trade_tree.get_children())}",
                f"Errors this run: {getattr(self, 'err_count', 0)}",
                "", "--- crash.log (last 60 lines) ---", tail("crash.log", 60),
                "", "--- activity.log (last 30 lines) ---", tail("activity.log", 30),
                "", "--- trades.csv (first 5 lines) ---"]
        try:
            with open(os.path.join(E.HOME_DIR, "trades.csv"), encoding="utf-8", errors="replace") as f:
                info.append("".join(f.readlines()[:5]).strip() or "(empty)")
        except OSError:
            info.append("(not found)")
        return "\n".join(info)

    SECRET_WORDS = ("key", "secret", "token", "password", "pass", "account", "acct", "email", "user")

    def _scrub(self, d):
        """Settings with anything that could be a login, key or account number taken out."""
        if isinstance(d, dict):
            return {k: ("(hidden)" if any(w in str(k).lower() for w in self.SECRET_WORDS) else self._scrub(x)) for k, x in d.items()}
        if isinstance(d, list):
            return [self._scrub(x) for x in d]
        return d

    def _bot_rows(self, t, n=25):
        out = []
        for v, sc in t.ranking()[:n]:
            d = t.info.get(id(v), {})
            lf = t.life(v)
            out.append(dict(
                nickname=E.bot_nickname(v.key()), uid=v.uid, gen=v.gen, origin=v.origin, strategy=v.name, params=v.params,
                tf=v.tf, scalper=E.is_scalp(v.name), kamikaze=E.is_kami(v), status=d.get("status"),
                score_pct=None if sc is None else round(sc * 100, 2), consistency=d.get("cons"), weight=round(d.get("weight", 0) or 0, 4),
                exam=E.exam_verdict(v, t.serial), exams=v.exams, good_at=[(c, round(x, 4)) for c, x in E.good_at(v)],
                lifetime=dict(ret_pct=round(lf["ret"] * 100, 2), edge_pct=None if lf["edge"] is None else round(lf["edge"] * 100, 2),
                              trades=lf["trades"], winrate=lf["winrate"], maxdd_pct=round(lf["maxdd"] * 100, 2), fees=round(lf["fees"], 2)),
                best_trade_pct=v.best_tr, worst_trade_pct=v.worst_tr, power=round(v.power, 2), twin=v.twin,
                risk=v.risk, traits=v.traits, vetoes=v.vetoes, veto_stats=v.veto_stats, layers=v.layers,
                layer_stats=v.layer_stats, roam_scan=v.roam_scan, backtests=v.backtests[-5:],
                rules=E.rule_stack(v)))
        return out

    def report_data(self):
        """Everything worth knowing about what SBF is doing and learning, as plain data (no keys or account numbers)."""
        def tail(name, n):
            try:
                with open(os.path.join(E.HOME_DIR, name), encoding="utf-8", errors="replace") as f:
                    return f.readlines()[-n:]
            except OSError:
                return []
        farms = {}
        for name, t in (("Bot Farm", self.tourn), ("Slow farm", getattr(self, "slow", {}).get("t")),
                        ("Scalp lab", getattr(self, "lab", {}).get("t"))):
            if t is None:
                continue
            vs = list(t.vars)
            try:
                know = [dict(kind=x.get("kind"), title=x.get("title"), detail=x.get("detail"), evidence=x.get("evidence"),
                             score=x.get("score"), strong=x.get("strong")) for x in E.farm_knowledge(t)]
            except Exception:
                know = []
            farms[name] = dict(
                bots=len(vs), tried=t.serial, decisions=t.n, cap=t.cap, coins=t.coins, mood=t.mood, conditions=getattr(t, "conds", []),
                status=dict(collections.Counter(t.info.get(id(v), {}).get("status") for v in vs)),
                breeds=dict(collections.Counter(STRAT_LABELS.get(v.name, v.name) for v in vs)),
                exams=dict(collections.Counter(E.exam_verdict(v, t.serial)[0] for v in vs if not E.is_kami(v))),
                exam_bar=round(E.exam_bar(t.serial), 2),
                hindsight={k: dict(tries=round(e.get("tries", 0), 2), wins=round(e.get("wins", 0), 2), forward=e.get("fwd"),
                                   best=[b.get("score") for b in e.get("best", [])]) for k, e in (getattr(t, "hindsight", {}) or {}).items()},
                hindsight_share=t.hindsight_share(), trial_book=getattr(t, "trial_book", {}),
                rule_review=t.review, mutation_stats=t.mut_stats, elders=len(t.elders), knowledge=know,
                top_bots=self._bot_rows(t), events=list(getattr(t, "events", []))[-40:],
                all_bots=[dict(nickname=E.bot_nickname(v.key()), strategy=v.name, tf=v.tf, origin=v.origin,
                               status=t.info.get(id(v), {}).get("status"), age=v.age, live_n=v.live_n,
                               eq=f"{len(v.eq)}/{v.eq.maxlen}", trades_in_window=len(v.sim.tbars), trades=v.ntrades,
                               wins=v.wins, losses=v.losses, young=t.young(v), bad=getattr(v, "bad", 0),
                               ret_pct=round((v.eq[-1] / v.sim.start - 1) * 100, 2) if v.eq and v.sim.start else None)
                          for v in vs],
                deaths=[dict(uid=u, why=r.get("why"), ret=r.get("ret")) for u, r in list(t.lineage.items())[-60:]
                        if r.get("died") is not None])
        trades = [dict(x) for x in self.trades[-300:]]
        for x in trades:
            for k in list(x):
                if not isinstance(x[k], (str, int, float, bool, type(None), list, dict)):
                    x[k] = str(x[k])
        return dict(
            made=datetime.now().isoformat(timespec="seconds"), version=self.version, python=sys.version.split()[0],
            platform=sys.platform, account=dict(name=self.acct_name(), session=self.session, live=self.live, running=self.running,
                                                start_cash=self.start_cash, cash=round(self.cash, 2), positions=dict(self.pos),
                                                books=self.opt, trades=len(self.trades), coins=self.session_coins,
                                                bar_minutes=self.session_bar, logged_in=bool(self.client)),
            settings=self._scrub(dict(self.settings)), switches=self._scrub({k: self.cfg.get(k) for k in self.cfg if k.endswith("_on")}),
            farms=farms, free_roam=dict(on=bool(self.cfg.get("roam_on")), studies=getattr(self, "roam_n", 0),
                                        days_trained=sorted(getattr(self, "roam_days", set()) or []),
                                        holidays=sorted(getattr(self, "roam_dead", set()) or []),
                                        log=list(getattr(self, "roam_log", []) or [])[-80:],
                                        error=getattr(self, "roam_err", None)),
            spreads={k: list(x) for k, x in E.SPREAD_FIT.items()}, recent_trades=trades,
            errors_this_run=getattr(self, "err_count", 0), crash_log=tail("crash.log", 300), activity_log=tail("activity.log", 300))

    def export_report(self, detailed=None):
        """Saves a .zip with everything worth knowing about what SBF is doing (no keys, passwords or account numbers),
        ready to attach in a chat with Claude. detailed (or the 'Detailed reports' setting) adds detail.json and
        checks.txt: every bot's recent trades with what explains them, the price feed's gaps, errors, and checks."""
        if detailed is None:
            detailed = bool(self.settings.get("report_detail", False))
        from tkinter import filedialog
        import zipfile
        name = f"SBF-{'detailed-' if detailed else ''}report-{datetime.now():%Y-%m-%d-%H%M}.zip"
        start = os.path.join(os.path.expanduser("~"), "Desktop")
        path = filedialog.asksaveasfilename(title="Export a report for Claude", initialfile=name, defaultextension=".zip",
                                            initialdir=start if os.path.isdir(start) else os.path.expanduser("~"),
                                            filetypes=[("Zip file", "*.zip")])
        if not path:
            return
        try:
            data = self.report_data()
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("README.txt", "SBF report: what the app is doing and learning, for analysis.\n"
                                         "report.json = everything (farms, top bots and their rules, exams, Free roam log, "
                                         "settings, recent trades, logs). summary.txt = the short version. "
                                         + ("detail.json = the detailed part (every bot's recent trades with the prices "
                                            "and volatility behind them, the price feed's gaps, errors, learning records); "
                                            "bot_trades.csv = those trades as a table; checks.txt = automatic checks "
                                            "for things that look wrong. " if detailed else "") +
                                         "No API keys, passwords or account numbers are included.\n")
                z.writestr("report.json", json.dumps(data, indent=1, default=str))
                z.writestr("summary.txt", self.diagnostics_text() + "\n\n" + self._report_summary(data))
                if detailed:
                    det = self.report_detail()
                    z.writestr("detail.json", json.dumps(det, indent=1, default=str))
                    z.writestr("checks.txt", self._report_checks(det))
                    z.writestr("bot_trades.csv", self._trades_csv(det["bot_trades"]))
            self.write(f"Report saved: {path}. Attach it in your chat with Claude.")
            messagebox.showinfo("Report saved", f"Saved to:\n{path}\n\nAttach this file in your chat with Claude. It has no API "
                                                "keys, passwords or account numbers.")
        except Exception as e:
            self._record_error("exporting the report")
            messagebox.showerror("Report", f"Couldn't save the report: {e}")

    def report_detail(self):
        """The detailed part of a report: what's needed to see WHY the bots do what they do."""
        import statistics as _st

        def tail(name, n):
            try:
                with open(os.path.join(E.HOME_DIR, name), encoding="utf-8", errors="replace") as f:
                    return f.readlines()[-n:]
            except OSError:
                return []
        names = {}
        for t_ in (self.tourn, getattr(self, "slow", {}).get("t"), getattr(self, "lab", {}).get("t")):
            for v in (t_.vars if t_ else []):
                names[v.uid] = E.bot_nickname(v.key())
        trades = []
        for r in list(getattr(self, "shadow_trades", []) or [])[-3000:]:
            x = {k: r.get(k) for k in ("time", "side", "coin", "qty", "price", "usd", "pl", "who", "strategy", "under_px",
                                        "vol", "vol_src", "held", "why")}
            x["bot"] = names.get(r.get("bot"), r.get("bot"))
            try:
                paid = float(r["usd"]) - float(r["pl"])
                x["pl_pct"] = round(float(r["pl"]) / paid * 100, 2) if r.get("side") == "sell" and paid > 0 else None
            except (TypeError, ValueError, KeyError):
                x["pl_pct"] = None
            trades.append(x)
        sells = [x for x in trades if x.get("pl_pct") is not None]

        def dist(xs):
            xs = sorted(xs)
            if not xs:
                return None
            q = lambda f: xs[min(len(xs) - 1, int(f * len(xs)))]
            return dict(n=len(xs), median=round(_st.median(xs), 2), p10=round(q(0.1), 2), p90=round(q(0.9), 2),
                        best=round(xs[-1], 2), worst=round(xs[0], 2), won=sum(1 for v in xs if v > 0),
                        over_50=sum(1 for v in xs if v > 50))
        by_strat = {}
        for x in sells:
            by_strat.setdefault(STRAT_LABELS.get(x.get("strategy"), x.get("strategy")), []).append(x["pl_pct"])
        feed = {}
        for c in list(self.session_coins or []):
            ts = list((self.hx.get(c) or {}).get("t") or [])
            if len(ts) < 3:
                feed[c] = dict(bars=len(ts))
                continue
            step = sorted(b - a for a, b in zip(ts[-200:-1], ts[-199:]) if b > a)
            step = step[len(step) // 2] if step else 60
            day = time.time() - 86400
            gaps = [(datetime.fromtimestamp(a).strftime("%m-%d %H:%M"), datetime.fromtimestamp(b).strftime("%H:%M"),
                     round((b - a) / 60, 1)) for a, b in zip(ts[:-1], ts[1:]) if b >= day and b - a > 3 * step
                    and E.minutes_to_close(a) > 0 and datetime.fromtimestamp(a).date() == datetime.fromtimestamp(b).date()]
            feed[c] = dict(bars=len(ts), candle_s=step, first=datetime.fromtimestamp(ts[0]).isoformat(timespec="minutes"),
                           last=datetime.fromtimestamp(ts[-1]).isoformat(timespec="minutes"),
                           last_age_s=round(time.time() - ts[-1]), gaps_today=gaps[-30:],
                           swing=round(E.realized_vol(self.hist.get(c) or [1], ts), 4))
        farms = {}
        for name, t_ in (("Bot Farm", self.tourn), ("Slow farm", getattr(self, "slow", {}).get("t")),
                         ("Scalp lab", getattr(self, "lab", {}).get("t"))):
            if t_ is None:
                continue
            sc = [t_.score(v) for v in t_.vars]
            farms[name] = dict(bots=len(t_.vars), scored=sum(1 for x in sc if x is not None),
                               no_trades_yet=sum(1 for v in t_.vars if not v.ntrades),
                               score_pct=dist([x * 100 for x in sc if x is not None]),
                               instincts={k: dict(record_pct=round(r[0] * 100, 2), reviews=r[1])
                                          for k, r in (getattr(t_, "instincts", {}) or {}).items()},
                               departures=dict(collections.Counter(r.get("why") for r in t_.lineage.values()
                                                                   if r.get("died") is not None)))
        return dict(bot_trades=trades, sells_pl_pct=dist([x["pl_pct"] for x in sells]),
                    sells_by_strategy={k: dist(v) for k, v in by_strat.items()}, price_feed=feed,
                    farms=farms, feature_tests=getattr(self, "roam_features", {}),
                    free_roam_log=list(getattr(self, "roam_log", []) or [])[-300:],
                    account_trades=[dict(x) for x in self.trades[-300:]], errors_log=tail("errors.log", 300))

    @staticmethod
    def _trades_csv(rows):
        import io as _io
        cols = ["time", "bot", "who", "side", "coin", "qty", "price", "usd", "pl", "pl_pct", "held", "why"]
        buf = _io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in cols})
        return buf.getvalue()

    @staticmethod
    def _report_checks(d):
        """Plain-English checks for things that look wrong, at the top of a detailed report."""
        out = ["Automatic checks (things that look wrong, and what was seen)", ""]
        sp = d.get("sells_pl_pct")
        if sp:
            out.append(f"- Bot trades closed: {sp['n']}, {sp['won']} won; median {sp['median']}%, best {sp['best']}%, "
                       f"worst {sp['worst']}%.")
            if sp.get("over_50"):
                out.append(f"  ! {sp['over_50']} single trades made over +50%. Shares rarely do that in a short hold: "
                           "check the price feed for bad prints (price_feed).")
        for name, f in (d.get("farms") or {}).items():
            out.append(f"- {name}: {f['bots']} bots, {f['scored']} have a score, {f['no_trades_yet']} haven't traded yet.")
            if f["bots"] and f["scored"] < 0.25 * f["bots"]:
                out.append(f"  ! Under a quarter of {name}'s bots can be judged, so the farm learns slowly.")
        for c, x in (d.get("price_feed") or {}).items():
            if x.get("gaps_today"):
                g = x["gaps_today"]
                out.append(f"  ! {c}: {len(g)} gap(s) in today's prices while the market was open, the longest "
                           f"{max(y[2] for y in g):.0f} min (e.g. {g[-1][0]} to {g[-1][1]}).")
            if x.get("last_age_s", 0) > 600:
                out.append(f"  ! {c}: no new candle for {x['last_age_s'] // 60} min.")
        if d.get("errors_log"):
            out.append(f"- errors.log: {sum(1 for l in d['errors_log'] if l.startswith('---'))} recent entries.")
        if len(out) == 2:
            out.append("Nothing looks wrong.")
        return "\n".join(out) + "\n"

    @staticmethod
    def _report_summary(d):
        out = ["--- account ---", json.dumps(d["account"], default=str)]
        for name, f in d["farms"].items():
            out += [f"--- {name} ---", f"{f['bots']} bots ({f['tried']} tried), status {f['status']}, exams {f['exams']} "
                                       f"(bar {f['exam_bar']}), conditions now {f['conditions']}", "top bots:"]
            out += [f"  {b['nickname']} {b['strategy']} {b['status']} score {b['score_pct']}% exam {b['exam'][0]} "
                    f"trades {b['lifetime']['trades']} winrate {b['lifetime']['winrate']}" for b in f["top_bots"][:10]]
        fr = d["free_roam"]
        out += ["--- Free roam ---", f"on {fr['on']}, {fr['studies']} studies, {len(fr['days_trained'])} days trained"]
        out += [f"  {r.get('kind')} {r.get('day') or ''} {r.get('summary', '')}" for r in fr["log"][-25:]]
        return "\n".join(out)

    def show_diagnostics(self, parent=None):
        if parent is None:  # it lives as a tab in the system window
            return self.open_hub('system', 'Diagnostics')
        text = self.diagnostics_text()
        w = parent
        t = tk.Text(w, width=100, height=32, wrap="none", padx=8, pady=6)
        t.insert("1.0", text)
        t.pack(fill="both", expand=True, padx=6, pady=6)

        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            btn.config(text="Copied")
        row = ttk.Frame(w)
        row.pack(pady=(0, 8))
        btn = ttk.Button(row, text="Copy all", command=copy)
        btn.pack(side="left", padx=4)
        ttk.Button(row, text="Export a full report for Claude...", command=self.export_report).pack(side="left", padx=4)

    def on_cb_error(self, exc, val, tb):
        import traceback
        msg = self._record_error("updating the screen", "".join(traceback.format_exception(exc, val, tb)))
        if msg:
            try:
                self.write(msg, "bad")
            except Exception:
                E.note_error("on_cb_error")

    def drain(self):
        """Handles messages from background threads. One bad message can no longer stop the loop."""
        try:
            for _ in range(300):
                if self.closing():  # the window is gone (quitting / restarting): nothing left to update
                    return
                try:
                    kind, data = self.q.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_msg(kind, data)
                except Exception:
                    if self.closing():
                        return
                    msg = self._record_error(f"handling '{kind}'")
                    if msg:
                        self.write(msg, "bad")
        finally:
            if not self.closing():
                self.root.after(200, self.drain)
            if not getattr(self, "_think_started", False):
                self._think_started = True
                self.root.after(self.THINK_EVERY * 1000, self.think_tick)

    THINK_EVERY = 10  # seconds between looks at the forming candle (half the farm each time)

    def think_tick(self):
        """Thinking between candles: every few seconds, while the farm runs and the market is open, half the bots look at
        the candle that's still forming (the live price) and may act on it instead of waiting for the minute to end."""
        if self.closing():
            return
        try:
            self._think()
        except Exception:
            note = self._record_error("thinking between candles")
            if note:
                self.write(note, "bad")
        self.root.after(self.THINK_EVERY * 1000, self.think_tick)

    def _think(self):
        t, coins = self.tourn, list(self.session_coins)
        if not (self.settings.get("think_between", True) and self.session and self.running and self.wild and t is not None
                and coins and float(self.session_bar or 0) >= 1):
            return
        if self.market is not None and not self.market.get("is_open", True):
            return  # nothing moves while the market is closed
        since = time.time() - getattr(self, "_bar_at", 0)
        if since < 8 or since > self.session_bar * 60 - 4:
            return  # a candle has just closed, or is about to: that decision is coming anyway
        if self.moving():
            return  # the window is being moved or resized: wait, so it stays smooth
        if not all(self.hist.get(c) and self.last.get(c) for c in coins):
            return
        now = time.time()
        closes, extra = {}, {}
        for c in coins:
            px = self.last[c]
            closes[c] = self.hist[c] + [px]
            x = self.hx.get(c) or {}
            hi, lo = self.bar_hl.get(c, [px, px])
            if len(x.get("t", [])) == len(self.hist[c]):
                extra[c] = dict(h=x["h"] + [max(hi, px)], l=x["l"] + [min(lo, px)],
                                v=x["v"] + [float(self.cur_vol.get(c, 0.0))], t=x["t"] + [now])
        if self.ctx_hist:
            extra[E.CTX_KEY] = {k: list(q) + [q[-1]] for k, q in self.ctx_hist.items() if q}
        if t.coins != coins:
            return
        t.think(closes, extra or None)
        self.collect_shadow()
        for e in t.pop_events():
            self.write("Wildcard: " + friendly_text(e[0].lower() + e[1:]))

    def closing(self):
        """True once the main window has been destroyed (the app is quitting or restarting)."""
        if getattr(self, "_gone", False):
            return True
        try:
            return not self.root.winfo_exists()
        except Exception:
            return True

    def _handle_msg(self, kind, data):
        if kind == "log":
            self.write(*data) if isinstance(data, tuple) else self.write(data)
        elif kind == "seed":
            self.hist, self.tourn = data[0], data[1]
            self.ctx_hist = dict(getattr(self, "ctx_pending", {}) or {})
            self.hx = data[2] if len(data) > 2 else {c: dict(h=[], l=[], v=[]) for c in self.hist}
            if self.tourn:
                self.tourn.recording = True  # history is loaded; from now on bot trades are listed
            self.seeded, self.last_bar = True, 0.0
            n = min((len(v) for v in self.hist.values()), default=0)
            self.write(f"History loaded ({n} bars per coin)." if n else "Starting without history (cold start).")
        elif kind == "prices":
            if self.running:
                self.on_prices(data)
        elif kind == "backfill":
            if self.running:
                self.on_backfill(*data)
        elif kind == "connected":
            self._login_tries = 0
            self.client, self.balances = data
            self.usd_avail = self.avail()
            self.logging_in = False
            self.update_status()
            short = min((len(self.hist.get(c) or []) for c in self.session_coins), default=0) if self.session_coins else 0
            if self.running and self.seeded and self.session_coins and short < 120:
                # started without history (no login yet): load it now, for the same farm, so the bots see a full chart
                self._restored_farm, self.seeded = self.tourn, False
                self.write("Logged in: loading the price history the bots missed (your farm carries on as it was).")
                self.launch_worker()
            self.write(f"Logged in to Alpaca ({'PAPER' if E.ALPACA.get('paper', True) else 'LIVE'}). Buying power: ${self.usd_avail:,.2f} "
                       f"")
        elif kind == "balances":
            self.balances = data
            self.usd_avail = self.avail()
            self.update_status()
        elif kind == "saved_login_rejected":
            self.logging_in = False
            self.update_status()
            self.write("Your saved Alpaca key was rejected (it may have been regenerated or revoked), so it was forgotten. "
                       "Log in again with your current key and tick Remember: until then there are no prices.", "bad")
            if not self.tray_mode:
                messagebox.showwarning("Saved key rejected", "Alpaca didn't accept the key saved on this computer (it may "
                                       "have been regenerated or revoked), so it was forgotten.\n\nPlease log in with "
                                       "your current key and tick 'Remember'. Until then the bots have no prices.")
                self.open_login()
        elif kind == "saved_login_retry":
            self.logging_in = False
            self.update_status()
            n = self._login_tries = getattr(self, "_login_tries", 0) + 1
            wait = min(600, 30 * 2 ** (n - 1))  # 30 s, 1, 2, 4, 8 min, then every 10 min
            self.write(f"Couldn't reach Alpaca yet ({data}). Trying the saved login again in "
                       f"{wait // 60} min." if wait >= 60 else
                       f"Couldn't reach Alpaca yet ({data}). Trying the saved login again in {wait} seconds.", "warn")
            self.root.after(wait * 1000, self.auto_login)
        elif kind == "login_failed":
            self.logging_in = False
            self.update_status()
            self.write(f"Login failed: {data}")
            messagebox.showerror("Login failed", str(data))
        elif kind == "fill":
            self.on_fill(*data)
        elif kind == "market":
            self.market = data
            self.update_status()
        elif kind == "fail":
            self.on_fail(*data)
        elif kind == "call":  # run a small UI update that a background thread asked for
            data()
        elif kind == "watch":
            self.on_watch(*data)
        elif kind == "net":
            self.on_net(*data)
        elif kind == "vols":
            self.cur_vol.update(data)
        elif kind == "unfilled":
            self.on_unfilled(*data)
        elif kind == "scouted":
            self.on_scouted(*data)
        elif kind == "update":
            self.on_update_msg(*data)
        elif kind == "dep_log":
            self.setup_log(data)
        elif kind == "dep_done":
            self.setup_done(*data)

    # ---------------- price handling / strategy loop ----------------
    def on_prices(self, prices):
        if not prices:
            return
        self.last.update(prices)
        now = datetime.now()
        ts = time.time()
        for c, px in prices.items():
            hl = self.bar_hl.get(c)
            self.bar_hl[c] = [max(hl[0], px), min(hl[1], px)] if hl else [px, px]
            self.tick_px.setdefault(c, deque(maxlen=8000)).append((ts, px))
        if self._gap_ref and (now - self._gap_ref).total_seconds() > max(120, 8 * int(self.settings["refresh"])):
            self.write(f"No prices for {(now - self._gap_ref).total_seconds() / 60:.0f} min "
                       f"(computer asleep or offline). Back online now.", "warn")
        self.last_tick = self._gap_ref = now
        for c, p in prices.items():
            self.start_px.setdefault(c, p)
        if not self.bench_units and all(c in self.last for c in self.session_coins):
            share = self.start_cash / len(self.session_coins)
            self.bench_units = {c: share / self.last[c] for c in self.session_coins}
        self.risk_checks()
        if time.time() - self.last_bar >= self.session_bar * 60 and all(c in prices for c in self.session_coins):
            self.last_bar = time.time()
            self.on_bar(prices)
        self.curve.append((self.equity(), self.bench_equity(), ts))
        del self.curve[:-8000]
        if not hasattr(self, "alloc_hist"):
            self.alloc_hist = deque(maxlen=8000)
        self.alloc_hist.append((ts, self.cash, self.equity() - self.cash))
        self.check_daily()
        self.refresh_view()

    def on_bar(self, prices):
        self.bar_n += 1
        keep = min(8000, max(2400, int(1440 / self.session_bar) + 20))
        for c in self.session_coins:
            h = self.hist.setdefault(c, [])
            x = self.hx.setdefault(c, dict(h=[], l=[], v=[], t=[]))
            if len(x["h"]) != len(h):  # line the extra data up with the closes (older sessions had none)
                x["h"] = list(h)
                x["l"] = list(h)
                x["v"] = [0.0] * len(h)
            if len(x.get("t", [])) != len(h):
                now_ = time.time()
                x["t"] = [now_ - (len(h) - i) * self.session_bar * 60 for i in range(len(h))]
            hi, lo = self.bar_hl.pop(c, [prices[c], prices[c]])
            h.append(prices[c])
            x["h"].append(max(hi, prices[c]))
            x["l"].append(min(lo, prices[c]))
            x["v"].append(float(self.cur_vol.get(c, 0.0)))
            x["t"].append(time.time())
            del h[:-keep]
            for k in ("h", "l", "v", "t"):
                del x[k][:-keep]
        L0 = len(self.hist[self.session_coins[0]]) if self.session_coins else 0
        iv = E.ivx_now()
        if iv and L0:  # SPY's live implied volatility (the VIX idea), lined up with the bars like any watched ticker
            q = self.ctx_hist.get(E.IVX_KEY)
            if q is None:
                self.ctx_hist[E.IVX_KEY] = [iv] * (L0 - 1)  # no reading before now: flat, so no move is read into it
                q = self.ctx_hist[E.IVX_KEY]
            q.append(iv)
            del q[:-L0]
        for sym in list(self.ctx_hist):  # the watched tickers move along with every bar
            if sym == E.IVX_KEY:
                continue
            q = self.ctx_hist[sym]
            px = prices.get(sym) or self.last.get(sym) or (q[-1] if q else None)
            if px:
                q.append(px)
                L = len(self.hist[self.session_coins[0]]) if self.session_coins else len(q)
                del q[:-L]
        if self.wild and self.tourn and set(self.tourn.coins) != set(self.session_coins):
            fixed = self.tourn.align_coins(self.session_coins, {c: self.hist[c][-1] for c in self.session_coins if self.hist.get(c)})
            self.write(f"The farm's coins were out of step with the session; it now trades {', '.join(fixed)}.", "warn")
        if self.wild and self.tourn:
            self.save_farm()
            self._bar_at = time.time()
            self.tourn.step({c: self.hist[c] for c in self.session_coins}, self.extra_now())
            self.collect_shadow()
            for fn in (self.shadow_live_step, self.record_farm, self.maybe_retest):
                try:
                    fn()
                except Exception:
                    note = self._record_error(fn.__name__)
                    if note:
                        self.write(note, "bad")
            self.check_hof()
            for e in self.tourn.pop_events():
                self.write("Wildcard: " + friendly_text(e[0].lower() + e[1:]))
            self.update_wild_msg()
            self.refresh_board()
        books = [("long", c, c) for c in self.session_coins]
        if self.wild and self.tourn and self.tourn.scalpers():  # scalpers trade their own book on each ticker
            books += [("scalp", c, scalp_key(c)) for c in self.session_coins]
        for breed, c, k in books:  # c = the ticker, k = the account's book for it
            if k in self.pending:
                continue
            mult, who, voters = 1.0, [], []
            if self.wild and self.tourn:
                sig, mult = self.tourn.vote(c, breed if len(books) > len(self.session_coins) else None, k)
                if sig:
                    add = self.tourn.kind.get(k) == "add"
                    pool = self.tourn.active if add else self.tourn.active + self.tourn.slowed
                    voters = [v for v in pool if (E.is_scalp(v.name) == (breed == "scalp") or len(books) == len(self.session_coins))
                              and (self.tourn._holding(v, c) if add else v.sig.get(c) == sig)]
                    who = [v.name for v in voters]
            else:
                votes = [E.strat_signal(n, self.hist[c], self.bar_n, self.settings["params"][n], 1, self.hx.get(c))
                         for n in self.active]
                b, s = votes.count("buy"), votes.count("sell")
                sig = "buy" if b > s else "sell" if s > b else None
                if sig:
                    ms = [self.settings["size_mult"].get(n, 1.0) for n, v in zip(self.active, votes) if v == sig]
                    mult = sum(ms) / len(ms)
                    who = [n for n, v in zip(self.active, votes) if v == sig]
            if sig and not self.brake_ok():
                sig = None
            if sig and not in_hours(self.settings.get("trade_hours", ""), self.settings.get("weekdays_only", False)):
                if time.time() - getattr(self, "_hours_note", 0) > 3600:
                    self._hours_note = time.time()
                    self.write("Outside your trading hours: the account waits (the bots keep learning, stops still work). "
                               "Settings > Orders > Trading hours", "warn")
                sig = None
            hold_min = float(self.settings.get("min_hold_min", 0) or 0)
            if sig == "sell" and hold_min > 0 and k in self.bought_at and time.time() - self.bought_at[k] < hold_min * 60:
                sig = None  # minimum hold time not reached yet
            if sig == "sell" and self.settings.get("fee_aware", True) and self.pos.get(k, 0) > 0 and self.cost.get(k):
                fee, slip = E.cost_model(self.settings)
                gain = self.position_price(k) / (self.cost[k] / self.pos[k]) - 1
                if 0 <= gain < 2 * (fee + slip):
                    sig = None  # the profit wouldn't cover the spread: keep holding
            if sig == "buy" and voters:
                mult = self.allowance_mult(k, voters, mult)
                if mult <= 0:
                    sig = None
            if sig:
                self.pending_detail[k] = self.why_text(k, sig, voters, who)
                ag = (self.tourn.agree.get(k) if self.tourn and sig == "buy" and self.tourn.kind.get(k) != "add" else None)
                if ag and self.wild:
                    self.pending_detail[k] += (f" {ag[0]} different strateg{'y' if ag[0] == 1 else 'ies'} agreed"
                                               + (f" while {ag[2] * 100:.0f}% of the votes said sell" if ag[2] else "")
                                               + f", so the size was x{ag[1]:g}.")
                self.pending_voters[k] = [v.uid for v in voters]
                counts = {}
                for n in who:
                    counts[n] = counts.get(n, 0) + 1
                self.pending_who[k] = ", ".join(STRAT_LABELS.get(n, n) + (f" x{x}" if x > 1 else "") for n, x in counts.items())
            genes = self.driver_genes(c, voters)
            if breed == "long":
                genes.pop("_scalp", None)  # the long-term book never makes scalps
            if sig == "buy":
                kind = self.tourn.kind.get(k) if (self.wild and self.tourn) else None
                if kind != "add":  # the account doesn't add to a position it already holds
                    self.buy(k, prices[c], "signal", mult, genes)
            elif sig == "sell":
                self.sell_signal(k, prices[c], "signal", mult, genes)
            if k not in self.pending:  # nothing is waiting on a live order, so drop any unused note
                self.pending_who.pop(k, None)
                self.pending_detail.pop(k, None)
                self.pending_voters.pop(k, None)
        self.save_state(force=False)
        if self.bar_n % 60 == 0:
            self.weekly_report()

    def brake_ok(self):
        """Max-trades-per-hour brake for strategy trades (safety stops are never braked)."""
        lim = int(self.settings.get("max_trades_hour", 0) or 0)
        if lim <= 0:
            return True
        cut = datetime.now().timestamp() - 3600
        n = sum(1 for t in self.trades[-500:] if t.get("reason") in ("signal", "add-on")
                and (self._ts(t.get("time")) or 0) >= cut) + len(self.pending)
        if n < lim:
            return True
        if time.time() - self.brake_note > 900:
            self.brake_note = time.time()
            self.write(f"Brake: {lim} strategy trades in the last hour, so new ones wait. Safety stops still work. "
                       f"(Settings > Orders)", "warn")
        return False

    # ================= 1.13: trust, insight and convenience =================
    # ---------------- kill switch ----------------
    def kill_switch(self, ask=True):
        """Stop everything now: stop the bot, cancel waiting orders, sell every position at market, lock LIVE."""
        if ask and not messagebox.askyesno("KILL SWITCH", "Stop the bot, cancel any waiting orders, SELL EVERYTHING at "
                                                          "market and lock LIVE mode?\n\n(Use it when something looks wrong.)"):
            return
        if self.running:
            self.toggle()
        oids = list(set(getattr(self, "open_oids", set())) | set(self.brk().open))
        if oids:
            def cancel():
                try:
                    for oid in oids:
                        self.brk().cancel(oid)
                except Exception as e:
                    self.q.put(("log", (f"Kill switch: could not cancel orders: {e}", "bad")))
            self.executor.submit(cancel)
        sold = 0
        for c, q in list(self.pos.items()):
            if q > 0 and tick_of(c) in self.last and c not in self.pending:
                self.sell(c, self.last[tick_of(c)], "manual")
                sold += 1
        if self.live:
            self.cfg["live_locked"] = True
            E.save_json(CONFIG_PATH, self.cfg)
        msg = (f"KILL SWITCH: bot stopped, {len(oids)} waiting order(s) cancelled, {sold} position(s) sold"
               + (", LIVE mode locked (type UNLOCK to use it again)." if self.live else "."))
        self.write(msg, "bad")
        self.alert("problems", msg)
        self.notify("SBF", msg, sound="problem")
        return msg

    # ---------------- graduation ----------------
    def graduated(self, b):
        """(ok, [what's missing]) for a saved bot before it may trade real money."""
        if not self.settings.get("graduation", True):
            return True, []
        lt = b.get("lifetime") or {}
        miss = []
        need = float(self.settings.get("grad_hours", 24) or 0)
        hrs = lt.get("hours_winning") or 0
        if hrs < need:
            miss.append(f"{hrs:.1f} of {need:g} h winning")
        if (lt.get("return_pct") or 0) <= 0:
            miss.append("no lifetime profit")
        if lt.get("vs_holding_pct") is None or lt["vs_holding_pct"] <= 0:
            miss.append("not ahead of the market")
        if (lt.get("trades") or 0) < 5:
            miss.append("fewer than 5 trades")
        bts = b.get("backtests") or []
        twin = b.get("twin_test_pct")
        if not ((twin is not None and twin > 0) or any(x.get("edge", 0) > 0 for x in bts)):
            miss.append("no passed twin test or backtest")
        if (b.get("retest") or {}).get("rusty"):
            miss.append("rusty in the last retest")
        return not miss, miss

    # ---------------- Shadow Live ----------------
    def shadow_live_step(self):
        """A pretend account that follows only the Hall of Fame bots on the farm, so you can see how they'd do with
        real money before you trust them with it."""
        if not self.settings.get("shadow_live", True) or not self.tourn:
            return
        hofk = self.hof_keys()
        members = [v for v in self.tourn.vars if v.key() in hofk]
        if self.shadow_live is None:
            fee, slip = E.cost_model(self.settings)
            self.shadow_live = E.Sim(self.start_cash, fee, self.settings["trade_pct"] / 100,
                                     self.settings["max_coin_pct"] / 100, slip)
        sim = self.shadow_live
        for c in self.session_coins:
            px = self.last.get(c) or (self.hist.get(c) or [None])[-1]
            if not px:
                continue
            b = sum(1 for v in members if v.sig.get(c) == "buy")
            sl = sum(1 for v in members if v.sig.get(c) == "sell")
            if b > sl:
                sim.buy(c, px)
            elif sl > b:
                sim.sell(c, px)
        eq = sim.equity({c: self.last.get(c, 0) for c in self.session_coins})
        self.shadow_curve.append((time.time(), eq))
        del self.shadow_curve[:-8000]

    # ---------------- farm pulse ----------------
    def pulse_text(self):
        t = self.tourn
        if not t:
            return "Farm pulse: the farm hasn't started yet."
        hour = max(1, int(60 / max(self.session_bar, 0.25)))
        born = sum(1 for n in t.births if n > t.n - hour)
        died = sum(1 for n in t.deaths if n > t.n - hour)
        best, bv = None, -9.0
        for v in t.vars:
            k = min(len(v.eq) - 1, hour)
            if k >= 3 and v.eq[-1 - k] > 0:
                g = v.eq[-1] / v.eq[-1 - k] - 1
                if g > bv:
                    best, bv = v, g
        parts = [f"◉ gen {max((v.gen for v in t.vars), default=0)}", f"+{born} born / -{died} retired this hour",
                 f"season: {MOOD_TEXT.get(t.season, t.season) if t.season else 'settling'}"]
        if best:
            parts.append(f"best of the hour: {E.bot_nickname(best.key())} {bv * 100:+.2f}%")
        if getattr(self, "roam_n", 0):
            parts.append(f"free roam: {self.roam_n} self-studies")
        if self.shadow_curve and self.settings.get("shadow_live", True):
            parts.append(f"Hall of Fame shadow: {(self.shadow_curve[-1][1] / self.start_cash - 1) * 100:+.1f}%")
        return "   •   ".join(parts)

    # ---------------- achievements ----------------
    def achievements(self, v, lf):
        """Badges a dragon has earned: (name, what it means)."""
        out = []
        hrs = v.win_steps * self.session_bar / 60
        closed = (lf.get("wins") or 0) + (lf.get("losses") or 0)
        for ok, name, why in (
                ((lf.get("ret") or 0) >= 0.10, "First 10%", "made 10% or more in its life"),
                ((lf.get("edge") or 0) >= 0.05, "Beat the market", "5%+ ahead of the market"),
                ((lf.get("hold") or 0) <= -0.05 and (lf.get("ret") or 0) > 0, "Crash survivor", "made money while the market lost 5%+"),
                ((lf.get("winrate") or 0) >= 0.7 and closed >= 10, "Sharpshooter", "70%+ wins over 10+ trades"),
                (hrs >= 24, "Marathon", "a full day spent Winning"),
                (v.gen >= 5, "Dynasty", "fifth generation or later"),
                (v.twin is not None and v.twin > 0, "Twin-proven", "passed its twin test"),
                (any(b.get("edge", 0) > 0 for b in v.backtests), "Backtested", "beat the market in a saved backtest"),
                (v.power >= 5, "Powerhouse", "power 5 or more")):
            if ok:
                out.append((name, why))
        return out

    # ---------------- nightly retest ----------------
    def maybe_retest(self):
        if not self.settings.get("retest_nightly", True) or self.cfg.get("last_retest") == date.today().isoformat():
            return
        if not self.hof() or not self.hist or min(len(self.hist.get(c, [])) for c in self.session_coins) < 400:
            return
        self.cfg["last_retest"] = date.today().isoformat()
        E.save_json(CONFIG_PATH, self.cfg)
        self.retest_hof(quiet=True)

    def retest_hof(self, quiet=False):
        """Re-backtests every Hall of Fame bot on the latest prices (in the background). Bots that lose to just
        holding get a RUSTY badge until they pass again."""
        hof = list(self.hof())
        if not hof or not self.session_coins:
            return messagebox.showinfo("Retest", "No Hall of Fame bots, or no prices yet.") if not quiet else None
        closes = {c: list(self.hist[c]) for c in self.session_coins}
        extra = {c: {k: list(x) for k, x in self.hx[c].items()} for c in self.session_coins} if self.hx else None
        fee, slip = E.cost_model(self.settings)
        pct = self.settings["trade_pct"] / 100

        def work():
            res = []
            for b in hof:
                try:
                    r = E.backtest_bot(b, closes, extra, self.start_cash, fee, pct, slip)
                    res.append((E.bot_key(b), b, r))
                except Exception:
                    continue

            def done():
                rusty = 0
                for k, b, r in res:
                    rt = dict(date=date.today().isoformat(), edge=round(r["edge"] * 100, 2), ret=round(r["ret"] * 100, 2),
                              rusty=r["edge"] <= 0)
                    rusty += rt["rusty"]
                    E.update_saved(k, lambda e, rt=rt: e.__setitem__("retest", rt), self.version)
                self.__dict__.pop("_file_cache", None)
                msg = f"Hall of Fame retest: {len(res) - rusty} still sharp, {rusty} rusty (losing to the market lately)."
                self.write(msg, "good" if not rusty else "warn")
                self.after_hof_change()
            self.q.put(("call", done))
        threading.Thread(target=work, daemon=True).start()
        self.write("Retesting the Hall of Fame on the latest prices...")

    # ---------------- backtest a bot ----------------
    # history lengths in calendar days, and candle sizes, shared by Backtest and Training camp
    PERIODS = (("1 day", 1), ("3 days", 3), ("1 week", 7), ("2 weeks", 14), ("1 month", 30), ("3 months", 91),
               ("6 months", 182), ("1 year", 365), ("2 years", 730), ("5 years", 1826), ("Since 2016", 4000))
    CANDLES = (("1 min", 60), ("5 min", 300), ("15 min", 900), ("1 hr", 3600), ("1 day", 86400))
    BT_MAX = 25000  # candles per ticker a backtest will chew through

    def period_bounds(self, length, when, from_date):
        """(start, end) timestamps for a period picker: the most recent N days, or N days from a date."""
        days = dict(self.PERIODS)[length]
        if when == "Most recent" or not from_date.strip():
            end = time.time()
            return max(E.HIST_START, end - days * 86400), end
        try:
            st = datetime.strptime(from_date.strip(), "%Y-%m-%d").timestamp()
        except ValueError:
            raise ValueError("Write the start date like 2020-03-02.")
        st = max(st, E.HIST_START)
        return st, min(time.time(), st + days * 86400)

    @staticmethod
    def est_candles(start, end, gran):
        return int((end - start) / 86400 * 5 / 7 * E.bars_per_day(gran))

    def test_coins_field(self, parent, prefix, default):
        """A box for which tickers to test on (any ticker with history, not just the ones the farm trades)."""
        var = tk.StringVar(value=self.cfg.get(prefix + "_coins") or ", ".join(default))
        e = ttk.Entry(parent, textvariable=var, width=34)
        Tip(e, "Any tickers, separated by commas, up to 8. For example: SPY, QQQ, AAPL, NVDA, MSFT, TSLA, AMD. The bots don't have to trade them "
               "in your farm: this is just what they're tested on.")
        return var, e

    def parse_coins(self, text):
        out = []
        for x in re.split(r"[,\s;]+", (text or "").upper()):
            if E.valid_coin(x) and x not in out:
                out.append(x)
        return out[:8]

    def load_stretch(self, coins, gran, start, end, note=None, max_bars=None):
        """Past prices for these tickers between two dates, lined up bar by bar, with the watched tickers.
        -> (closes, extra, info)"""
        data = {}
        for c in coins:
            if note:
                note(f"Loading {c}...")
            d = E.fetch_range(c, gran, start, end, self.quote, note=note)
            if len(d["c"]) < 60:
                try:
                    had = E.history_text(c, self.quote)
                except Exception:
                    had = f"{c} may not have traded then"
                raise E.NoHistory(f"Not enough {c} history in that period ({len(d['c'])} candles). There's {had}. "
                                  f"Pick a stretch inside that, or untick {c}.")
            data[c] = d
        closes, extra = E.common_stretch(data)
        L = len(next(iter(closes.values()))) if closes else 0
        if L < 60:
            raise E.NoHistory("These tickers share too few candles in that period: " + "; ".join(
                E.history_text(c, self.quote) for c in coins) + ".")
        # a ticker that was suspended for a while leaves a hole: keep the longest unbroken stretch
        t_ = next(iter(extra.values()))["t"]
        a_, b_ = E.longest_run(t_, gran)
        gap = (b_ - a_) < L
        if gap:
            closes = {c: x[a_:b_] for c, x in closes.items()}
            extra = {c: {k: x[a_:b_] for k, x in d.items()} for c, d in extra.items()}
            L = b_ - a_
            if L < 60:
                raise E.NoHistory("That stretch has a long hole in its history (a ticker wasn't trading for a while). Pick another stretch.")
        trimmed = bool(max_bars and L > max_bars)
        if trimmed:
            closes = {c: x[-max_bars:] for c, x in closes.items()}
            extra = {c: {k: x[-max_bars:] for k, x in d.items()} for c, d in extra.items()}
        if note:
            note("Loading the watched tickers...")
        self.add_ctx(extra, gran, 0, self.quote)
        t = next(iter(extra.values()))["t"]
        return closes, extra, dict(start=t[0], end=t[-1], bars=len(t), trimmed=trimmed, gap=gap)

    def period_picker(self, parent, prefix, default_len="1 month", default_candle="15 min"):
        """History length + candles + 'most recent' or 'starting on'. -> dict of tk variables."""
        c = self.cfg
        v = dict(length=tk.StringVar(value=c.get(prefix + "_period", default_len)),
                 candle=tk.StringVar(value=c.get(prefix + "_candles", default_candle)),
                 when=tk.StringVar(value=c.get(prefix + "_when", "Most recent")),
                 date=tk.StringVar(value=c.get(prefix + "_from", "2022-01-03")))
        if v["length"].get() not in dict(self.PERIODS):
            v["length"].set(default_len)
        if v["candle"].get() not in dict(self.CANDLES):
            v["candle"].set(default_candle)
        ttk.Label(parent, text="History").grid(row=0, column=0, sticky="w")
        ttk.Combobox(parent, textvariable=v["length"], values=[x[0] for x in self.PERIODS], state="readonly",
                     width=10).grid(row=1, column=0, sticky="w", padx=(0, 12))
        ttk.Label(parent, text="Candles").grid(row=0, column=1, sticky="w")
        ttk.Combobox(parent, textvariable=v["candle"], values=[x[0] for x in self.CANDLES], state="readonly",
                     width=7).grid(row=1, column=1, sticky="w", padx=(0, 12))
        ttk.Label(parent, text="Which stretch").grid(row=0, column=2, sticky="w")
        ttk.Combobox(parent, textvariable=v["when"], values=["Most recent", "Starting on"], state="readonly",
                     width=12).grid(row=1, column=2, sticky="w", padx=(0, 6))
        ttk.Label(parent, text="Start date (YYYY-MM-DD)").grid(row=0, column=3, sticky="w")
        de = ttk.Entry(parent, textvariable=v["date"], width=12)
        de.grid(row=1, column=3, sticky="w", padx=(0, 12))
        Tip(de, "Any date back to January 2016. Try 2020-02-18 (the Covid crash), 2022-01-03 (the 2022 bear market) "
                "or 2023-01-03 (the 2023 rally).")

        def sync(*_):
            de.config(state="normal" if v["when"].get() == "Starting on" else "disabled")
        v["when"].trace_add("write", sync)
        sync()

        def save():
            c.update({prefix + "_period": v["length"].get(), prefix + "_candles": v["candle"].get(),
                      prefix + "_when": v["when"].get(), prefix + "_from": v["date"].get()})
            E.save_json(CONFIG_PATH, c)
        v["save"] = save
        v["bounds"] = lambda: self.period_bounds(v["length"].get(), v["when"].get(), v["date"].get())
        v["gran"] = lambda: dict(self.CANDLES)[v["candle"].get()]
        return v

    def _bt_sources(self):
        """Bots you can backtest: farm bots first, then Library bots. -> [(label, genes, farm bot or None, saved key)]"""
        out = []
        if self.tourn:
            for r in self.tourn.board():
                v = r["v"]
                out.append((f"Farm: {E.bot_nickname(v.key())} ({STRAT_LABELS.get(v.name, v.name)})", v, v, v.key()))
        seen = {x[3] for x in out}
        for b in self.library():
            k = E.bot_key(b)
            if k not in seen:
                out.append((f"Library: {b.get('nickname') or E.bot_nickname(k)} ({STRAT_LABELS.get(b['name'], b['name'])})", b, None, k))
        return out

    def open_backtest(self, v=None, parent=None):
        if parent is None:  # it lives as a tab in the learning window
            return self.open_hub('learning', 'Backtest a bot', v=v)
        srcs = self._bt_sources()
        if not srcs:
            return self._nothing(parent, "Backtest", "No bots yet: start the farm or save some bots to your Library first.")
        w = parent
        w.configure(bg=BG)
        coins = list(self.session_coins or [c for c, x in self.coin_vars.items() if x.get()] or ["SPY"])

        # --- settings card
        sf, sc = self.card(w, "TEST")
        sf.pack(fill="x", padx=12, pady=(12, 0))
        names = [x[0] for x in srcs]
        pick = tk.StringVar(value=next((x[0] for x in srcs if x[2] is v), names[0]))
        r0 = tk.Frame(sc, bg=CARD)
        r0.pack(fill="x")
        tk.Label(r0, text="Bot", bg=CARD, fg=TEXT).pack(side="left")
        ttk.Combobox(r0, textvariable=pick, values=names, state="readonly", width=58).pack(side="left", padx=8)
        tk.Label(r0, text="on", bg=CARD, fg=TEXT).pack(side="left", padx=(6, 4))
        coins_var, ce = self.test_coins_field(r0, "bt", coins)
        ce.pack(side="left")
        r1 = ttk.Frame(sc)
        r1.pack(fill="x", pady=(8, 0))
        per = self.period_picker(r1, "bt", "1 month", "15 min")
        go = ttk.Button(r1, text="Run backtest", style="Accent.TButton")
        go.grid(row=1, column=5, sticky="w", padx=(12, 0))
        r2 = tk.Frame(sc, bg=CARD)
        r2.pack(fill="x", pady=(8, 0))
        pbar = ttk.Progressbar(r2, mode="indeterminate", length=160)
        pbar.pack(side="left")
        status = tk.Label(r2, text="", bg=CARD, fg=MUTED, anchor="w")
        status.pack(side="left", fill="x", expand=True, padx=8)

        def est(*_):
            try:
                a, b = per["bounds"]()
                n = self.est_candles(a, b, per["gran"]())
                status.config(text=f"About {n:,} candles per ticker"
                                   + (f" (more than {self.BT_MAX:,}: only the latest {self.BT_MAX:,} are used; pick bigger candles)"
                                      if n > self.BT_MAX else "") + ".  Older history downloads once, then it's instant.",
                              fg=MUTED)
            except ValueError:
                status.config(text="Write the start date like 2020-03-02.", fg=RED)
        for k in ("length", "candle", "when", "date"):
            per[k].trace_add("write", est)
        est()

        # --- result: fixed-size tiles, verdict, chart (fixed height) and a scrolling details box
        tiles = tk.Frame(w, bg=BG)
        tiles.pack(fill="x", padx=12, pady=(10, 0))
        tile_v = {}
        for i, (key, lab) in enumerate((("ret", "RETURN"), ("hold", "THE MARKET"), ("edge", "VS THE MARKET"),
                                        ("dd", "WORST DROP"), ("trades", "TRADES / WIN RATE"), ("parts", "PARTS WON"))):
            f = tk.Frame(tiles, bg=CARD, highlightthickness=1, highlightbackground=LINE, padx=10, pady=6)
            f.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0))
            tiles.columnconfigure(i, weight=1, uniform="t")
            tk.Label(f, text=lab, bg=CARD, fg=MUTED, font=("", 7, "bold"), anchor="w").pack(fill="x")
            tile_v[key] = tk.Label(f, text="-", bg=CARD, fg=TEXT, font=("", 13, "bold"), anchor="w")
            tile_v[key].pack(fill="x")
        verdict = tk.Label(w, text="Pick a bot and a stretch of history, then Run backtest.", bg=BG, fg=MUTED,
                           anchor="w", justify="left", font=("", 10, "bold"), height=1)
        verdict.pack(fill="x", padx=12, pady=(8, 0))
        chart_f = tk.Frame(w, bg=CARD, highlightthickness=1, highlightbackground=LINE, height=300)
        chart_f.pack(fill="x", padx=12, pady=(6, 0))
        chart_f.pack_propagate(False)
        cv = tk.Canvas(chart_f, bg=CARD, highlightthickness=0, cursor="crosshair")
        cv.pack(fill="both", expand=True)
        btns = tk.Frame(w, bg=BG)
        btns.pack(side="bottom", fill="x", padx=12, pady=10)
        save_btn = ttk.Button(btns, text="Save to its memory", state="disabled")
        save_btn.pack(side="left")
        teach_btn = ttk.Button(btns, text="Save + teach the farm", state="disabled")
        teach_btn.pack(side="left", padx=6)
        Tip(save_btn, "Keeps what this test learned with the bot: how each of its studies and never-rules did, which "
                      "tickers it struggles with, how it does in each market mood, and the test result. Its children inherit it.")
        Tip(teach_btn, "Also teaches the whole farm: losing buys go into the post-trade review, and if it beat the market "
                       "it becomes an elder that nudges future eggs.")
        ttk.Button(btns, text="Close", command=lambda: w.winfo_toplevel().destroy()).pack(side="right")
        df = tk.Frame(w, bg=BG)
        df.pack(fill="both", expand=True, padx=12, pady=(8, 0))
        details = tk.Text(df, bg=CARD, fg=TEXT, relief="flat", wrap="word", height=8, padx=10, pady=8,
                          font=("", 9), highlightthickness=1, highlightbackground=LINE)
        dsb = ttk.Scrollbar(df, orient="vertical", command=details.yview)
        details.configure(yscrollcommand=dsb.set)
        dsb.pack(side="right", fill="y")
        details.pack(side="left", fill="both", expand=True)
        details.tag_configure("h", foreground=ACCENT, font=("", 8, "bold"), spacing1=6)
        details.tag_configure("good", foreground=GREEN)
        details.tag_configure("bad", foreground=RED)
        details.insert("end", "After a run you'll find the details here: each ticker, each market mood, each part of the "
                              "walk-forward check, every rule's record, and the stress tests.")
        details.config(state="disabled")
        state = dict(hover=None)

        def run():
            src = next(x for x in srcs if x[0] == pick.get())
            try:
                a, b = per["bounds"]()
            except ValueError as e:
                return messagebox.showerror("Backtest", str(e), parent=w)
            gran = per["gran"]()
            picked = self.parse_coins(coins_var.get())
            if not picked:
                return messagebox.showerror("Backtest", "Type at least one ticker, like SPY.", parent=w)
            coins[:] = picked
            self.cfg["bt_coins"] = ", ".join(picked)
            per["save"]()
            go.config(state="disabled")
            pbar.start(12)
            fee, slip = E.cost_model(self.settings)
            pct, cash = self.settings["trade_pct"] / 100, self.start_cash

            def note(txt):
                self.q.put(("call", lambda: status.config(text=txt, fg=MUTED) if w.winfo_exists() else None))

            def work():
                try:
                    closes, extra, info = self.load_stretch(coins, gran, a, b, note, self.BT_MAX)
                    note(f"Testing on {info['bars']:,} candles...")
                    r = E.backtest_bot(src[1], closes, extra, cash, fee, pct, slip)
                    t = next(iter(extra.values()))["t"]
                    self.q.put(("call", lambda: show(src, r, closes, gran, info, t)))
                except Exception as e:
                    self.q.put(("call", lambda e=e: (pbar.stop(), go.config(state="normal"),
                                                     status.config(text=f"Couldn't run it: {e}", fg=RED)) if w.winfo_exists() else None))
            threading.Thread(target=work, daemon=True).start()

        def put(txt, tag=None):
            details.insert("end", txt, tag or ())

        def show(src, r, closes, gran, info, times):
            if not w.winfo_exists():
                return
            pbar.stop()
            go.config(state="normal")
            segs = r["segments"]
            passed = sum(1 for x in segs if x[2] > 0)
            st0 = r["start"] - 1
            state.update(src=src, r=r, times=list(times[st0:st0 + len(r["eq"])]), segs=segs)
            cname = dict((y, x) for x, y in self.CANDLES)[gran]
            status.config(text=f"{datetime.fromtimestamp(info['start']).strftime('%b %d %Y')} to "
                               f"{datetime.fromtimestamp(info['end']).strftime('%b %d %Y')}   |   {info['bars']:,} candles of "
                               f"{cname}" + ("   |   trimmed to the latest candles" if info["trimmed"] else ""), fg=MUTED)
            col = lambda x: GREEN if x > 0 else RED if x < 0 else TEXT
            tile_v["ret"].config(text=f"{r['ret'] * 100:+.2f}%", fg=col(r["ret"]))
            tile_v["hold"].config(text=f"{r['hold'] * 100:+.2f}%", fg=TEXT)
            tile_v["edge"].config(text=f"{r['edge'] * 100:+.2f}%", fg=col(r["edge"]))
            tile_v["dd"].config(text=f"{r['maxdd'] * 100:.1f}%", fg=TEXT)
            tile_v["trades"].config(text=f"{r['trades']}  /  " + ("-" if r["winrate"] is None else f"{r['winrate'] * 100:.0f}%"), fg=TEXT)
            tile_v["parts"].config(text=f"{passed} of {len(segs)}", fg=GREEN if passed >= max(1, len(segs) - 1) else TEXT)
            if r["edge"] > 0 and passed >= max(1, len(segs) - 1):
                verdict.config(text="Solid: it beat the market overall and in most parts of this stretch.", fg=GREEN)
            elif r["edge"] > 0:
                verdict.config(text="Mixed: it beat the market overall, but not in every part. Could be luck.", fg="#f59e0b")
            else:
                verdict.config(text="Weak: it did worse than the market on this stretch.", fg=RED)
            details.config(state="normal")
            details.delete("1.0", "end")
            put("STRESS TESTS\n", "h")
            put(f"Double fees and slippage: {r['fees2x'] * 100:+.2f}%   |   bad-luck drop (95% of shuffled orders): "
                + ("-" if r["mc_dd95"] is None else f"{r['mc_dd95'] * 100:.1f}%") + f"   |   fees paid ${r['fees']:,.2f}\n")
            put("WALK-FORWARD PARTS (the bot vs the market, in each quarter)\n", "h")
            for i, (a_, h_, e_) in enumerate(segs):
                put(f"  Part {i + 1}: {a_ * 100:+.1f}% vs {h_ * 100:+.1f}%   ")
                put("won\n" if e_ > 0 else "lost\n", "good" if e_ > 0 else "bad")
            if r["by_coin"]:
                put("PER TICKER\n", "h")
                for c_, x in r["by_coin"].items():
                    put(f"  {c_}: {x[0]} trades, ")
                    put(f"${x[1]:+,.2f}\n", "good" if x[1] > 0 else "bad")
            if r["by_mood"]:
                put("PER MARKET MOOD\n", "h")
                for m_, x in r["by_mood"].items():
                    put(f"  {MOOD_TEXT.get(m_, m_)}: ")
                    put(f"{x[1] * 100:+.2f}%\n", "good" if x[1] > 0 else "bad")
            st = dict(r["layer_stats"], **r["veto_stats"])
            if st:
                put("RULES (what each study and never-rule saved or cost)\n", "h")
                for k_, x in sorted(st.items(), key=lambda kv: -abs(kv[1][1])):
                    put(f"  {E.VETOES[k_]['name'] if k_ in E.VETOES else k_}: ")
                    put(f"{'saved' if x[1] >= 0 else 'cost'} {abs(x[1]):.1f}%", "good" if x[1] >= 0 else "bad")
                    put(f" over {int(x[0])}\n")
            if r["review"]:
                rv = [(k_, x) for k_, x in r["review"].items() if k_ in E.VETOES]
                if rv:
                    put("LOSING BUYS A NEVER-RULE WOULD HAVE STOPPED\n", "h")
                    put("  " + ", ".join(f"{E.VETOES[k_]['name']} {x[0]}" for k_, x in rv) + "\n")
            details.config(state="disabled")
            save_btn.config(state="normal")
            teach_btn.config(state="normal")
            draw()

        def geom():
            r = state["r"]
            W, H = cv.winfo_width(), cv.winfo_height()
            eq, he = r["eq"], r["hold_eq"]
            lo, hi = min(eq + he), max(eq + he)
            pad = (hi - lo) * 0.08 or 1
            lo, hi = lo - pad, hi + pad
            n = len(eq)
            X = lambda i: 64 + i * (W - 76) / max(1, n - 1)
            Y = lambda y: H - 26 - (y - lo) / (hi - lo) * (H - 52)
            return W, H, n, lo, hi, X, Y

        def draw(_=None):
            cv.delete("all")
            r = state.get("r")
            W, H = cv.winfo_width(), cv.winfo_height()
            if not r:
                cv.create_text(W // 2, H // 2, text="The bot's account (blue) and the market (grey) appear here.", fill=MUTED)
                return
            W, H, n, lo, hi, X, Y = geom()
            eq, he, times = r["eq"], r["hold_eq"], state["times"]
            segs = state["segs"] or [(0, 0, 0)]
            ns = len(segs)
            for k in range(ns):  # walk-forward parts as faint bands, each with its result
                x0, x1 = X(k * (n - 1) / ns), X((k + 1) * (n - 1) / ns)
                if k % 2:
                    cv.create_rectangle(x0, 22, x1, H - 26, fill=BG, outline="")
                e_ = segs[k][2]
                cv.create_text((x0 + x1) / 2, 30, text=f"part {k + 1}: {e_ * 100:+.1f}% vs the market",
                               fill=GREEN if e_ > 0 else RED, font=("", 7))
            for k in range(4):
                yv = lo + (hi - lo) * k / 3
                cv.create_line(64, Y(yv), W - 12, Y(yv), fill=LINE, dash=(2, 4))
                cv.create_text(58, Y(yv), text=f"${yv:,.0f}", anchor="e", fill=MUTED, font=("", 8))
            if times:  # dates along the bottom
                for k in range(5):
                    i = int(k * (n - 1) / 4)
                    if i < len(times):
                        cv.create_text(X(i), H - 12, text=datetime.fromtimestamp(times[i]).strftime("%b %d %Y" if times[-1] - times[0] > 200 * 86400 else "%b %d"),
                                       anchor="n" if 0 < k < 4 else ("nw" if k == 0 else "ne"), fill=MUTED, font=("", 7))
            if times and E.events():  # big news days
                t0, t1 = times[0], times[-1]
                for kind in E.BIG_EVENTS:
                    for et in E.events().get(kind) or []:
                        if t0 <= et <= t1:
                            i = bisect.bisect_left(times, et)
                            cv.create_line(X(i), H - 30, X(i), H - 26, fill="#f59e0b", width=2)
            step = max(1, n // 1400)
            cv.create_line(*[z for i in range(0, n, step) for z in (X(i), Y(he[i]))], fill="#9ca3af", width=1)
            cv.create_line(*[z for i in range(0, n, step) for z in (X(i), Y(eq[i]))], fill=ACCENT, width=2)
            for i, c, side, px in r["marks"][-400:]:
                j = i - r["start"] + 1
                if 0 <= j < n:
                    x, y = X(j), Y(eq[j])
                    cv.create_polygon(*((x, y + 3, x - 4, y + 10, x + 4, y + 10) if side == "buy" else (x, y - 3, x - 4, y - 10, x + 4, y - 10)),
                                      fill=GREEN if side == "buy" else RED, outline="")
            cv.create_text(W - 12, 6, anchor="ne", fill=MUTED, font=("", 8),
                           text="blue = the bot   grey = the market   orange ticks = big news days")
            hover()

        def hover(e=None):
            """Crosshair + readout drawn inside the chart, so nothing around it moves."""
            cv.delete("hover")
            r = state.get("r")
            if e is not None:
                state["hover"] = e.x
            x = state.get("hover")
            if not r or x is None:
                return
            W, H, n, lo, hi, X, Y = geom()
            i = int(round((x - 64) / max(1, W - 76) * (n - 1)))
            if not 0 <= i < n:
                return
            xi = X(i)
            cv.create_line(xi, 22, xi, H - 26, fill=MUTED, dash=(2, 2), tags="hover")
            cv.create_oval(xi - 3, Y(r["eq"][i]) - 3, xi + 3, Y(r["eq"][i]) + 3, fill=ACCENT, outline="", tags="hover")
            times = state["times"]
            when = datetime.fromtimestamp(times[i]).strftime("%a %b %d %Y %H:%M") if i < len(times) else ""
            b0 = r["eq"][0] or 1
            txt = (f"{when}    bot ${r['eq'][i]:,.0f} ({(r['eq'][i] / b0 - 1) * 100:+.1f}%)    "
                   f"the market ${r['hold_eq'][i]:,.0f} ({(r['hold_eq'][i] / (r['hold_eq'][0] or 1) - 1) * 100:+.1f}%)")
            cv.create_rectangle(64, 4, 64 + 7 * len(txt), 18, fill=CARD, outline="", tags="hover")
            cv.create_text(68, 11, text=txt, anchor="w", fill=TEXT, font=("", 8, "bold"), tags="hover")

        def leave(_=None):
            state["hover"] = None
            cv.delete("hover")
        cv.bind("<Configure>", draw)
        cv.bind("<Motion>", hover)
        cv.bind("<Leave>", leave)

        def save(teach):
            src, r = state["src"], state["r"]
            rec = None
            if src[2] is not None and self.tourn and src[2] in self.tourn.vars:
                rec = E.remember_backtest(src[2], r, self.tourn if teach else None)
            E.update_saved(src[3], lambda e: E.remember_backtest(e, r), self.version)
            if rec is None and teach and self.tourn:
                E.remember_backtest(dict(src[1]) if isinstance(src[1], dict) else src[1], r, self.tourn)
            self.__dict__.pop("_file_cache", None)
            self.write(f"Saved the backtest to {src[0].split(': ', 1)[-1]}'s memory" + (" and taught the farm." if teach else "."), "good")
            save_btn.config(state="disabled")
            teach_btn.config(state="disabled")
            self.save_farm(force=True)
        save_btn.config(command=lambda: save(False))
        teach_btn.config(command=lambda: save(True))
        go.config(command=run)
        if v is not None:
            run()

    def backtest_hof(self, parent=None):
        """Backtests every Hall of Fame bot on the current prices and ranks them."""
        if parent is None:  # it lives as a tab in the learning window
            return self.open_hub('learning', 'Hall of Fame test')
        hof = list(self.hof())
        if not hof or not self.session_coins:
            return self._nothing(parent, "Backtest the Hall of Fame", "You need Hall of Fame bots and a running session (for prices).")
        w = parent
        bb = ttk.Frame(w, padding=8)
        bb.pack(side="bottom", fill="x")
        lbl = ttk.Label(w, text="Testing...", padding=8)
        lbl.pack(side="bottom", fill="x")
        t = self.make_tree(w, ("name", "strategy", "ret", "edge", "parts", "dd", "trades", "fees2x"),
                           (150, 180, 80, 90, 80, 80, 60, 90),
                           ("Bot", "Strategy", "Return", "vs the market", "Parts won", "Worst drop", "Trades", "2x fees"))
        closes = {c: list(self.hist[c]) for c in self.session_coins}
        extra = {c: {k: list(x) for k, x in self.hx[c].items()} for c in self.session_coins} if self.hx else None
        fee, slip = E.cost_model(self.settings)
        pct, cash = self.settings["trade_pct"] / 100, self.start_cash
        res = []

        def work():
            for b in hof:
                try:
                    res.append((b, E.backtest_bot(b, closes, extra, cash, fee, pct, slip)))
                except Exception:
                    continue
            self.q.put(("call", fill))

        def fill():
            if not w.winfo_exists():
                return
            for b, r in sorted(res, key=lambda x: -x[1]["edge"]):
                t.insert("", "end", values=(b.get("nickname") or E.bot_nickname(E.bot_key(b)), STRAT_LABELS.get(b["name"], b["name"]),
                                            f"{r['ret'] * 100:+.2f}%", f"{r['edge'] * 100:+.2f}%",
                                            f"{sum(1 for x in r['segments'] if x[2] > 0)}/{len(r['segments'])}",
                                            f"{r['maxdd'] * 100:.1f}%", r["trades"], f"{r['fees2x'] * 100:+.2f}%"),
                         tags=("up" if r["edge"] > 0 else "down",))
            t.tag_configure("up", foreground=GREEN)
            t.tag_configure("down", foreground=RED)
            lbl.config(text=f"{len(res)} bots tested on {len(closes)} coins, {min(len(x) for x in closes.values())} bars each.")

        def save_all():
            for b, r in res:
                E.update_saved(E.bot_key(b), lambda e, r=r: E.remember_backtest(e, r), self.version)
                if self.tourn:
                    live = next((v for v in self.tourn.vars if v.key() == E.bot_key(b)), None)
                    if live is not None:
                        E.remember_backtest(live, r, self.tourn)
            self.__dict__.pop("_file_cache", None)
            self.write(f"Saved {len(res)} Hall of Fame backtests to their memories.", "good")
        ttk.Button(bb, text="Save all to their memories", style="Accent.TButton", command=save_all).pack(side="left")
        ttk.Button(bb, text="Close", command=lambda: w.winfo_toplevel().destroy()).pack(side="right")
        threading.Thread(target=work, daemon=True).start()

    # ---------------- training camp ----------------
    TC_MAX = 12000

    def open_training(self, parent=None):
        if parent is None:  # it lives as a tab in the learning window
            return self.open_hub('learning', 'Training camp')
        w = self.tc_win = parent
        w.configure(bg=BG)
        f = ttk.Frame(w, padding=12)
        f.pack(fill="x")
        ttk.Label(f, text="Training camp", font=("", 13, "bold")).grid(row=0, column=0, columnspan=6, sticky="w")
        ttk.Label(f, foreground=MUTED, wraplength=940, justify="left", text=(
            "A whole farm lives through a stretch of past prices, as many times as you like: hatching, competing, breeding "
            "and learning the whole way (between passes the bots keep their genes and lessons, with fresh money). The most "
            "recent part is held back as a final exam: the best bots are frozen and tested on prices they never saw. "
            "Only the ones that do well in training AND in the exam pass.")).grid(row=1, column=0, columnspan=6,
                                                                                           sticky="w", pady=(2, 10))
        c = self.cfg
        pf = ttk.Frame(f)
        pf.grid(row=2, column=0, columnspan=6, sticky="w", pady=(0, 8))
        per = self.period_picker(pf, "tc", "3 months", "1 hr")
        passes = tk.StringVar(value=str(c.get("tc_passes", 2)))
        size = tk.StringVar(value=str(c.get("tc_size", int(self.settings.get("max_bots", 30)))))
        exam = tk.StringVar(value=c.get("tc_exam", "25%"))
        start = tk.StringVar(value=c.get("tc_start", "Fresh eggs"))
        maxdec = tk.StringVar(value=str(c.get("tc_max_dec", 5000)))
        for idx, (lab, var, vals, wd) in enumerate((
                ("Passes", passes, ["1", "2", "3", "4", "5"], 3), ("Farm size", size, ["20", "30", "40", "60", "80", "120"], 4),
                ("Final exam", exam, ["None", "15%", "25%", "35%"], 6),
                ("Start from", start, ["Fresh eggs", "My current farm", "Hall of Fame", "Library"], 15))):
            r0, col = 4, idx
            ttk.Label(f, text=lab).grid(row=r0, column=col, sticky="w", padx=(0, 14))
            ttk.Combobox(f, textvariable=var, values=vals, state="readonly" if lab not in ("Farm size",) else "normal",
                         width=wd).grid(row=r0 + 1, column=col, sticky="w", padx=(0, 14))
        coins = list(self.session_coins or [k for k, x in self.coin_vars.items() if x.get()] or ["SPY", "QQQ"])
        ttk.Label(f, text="Max decisions").grid(row=4, column=4, sticky="w", padx=(0, 14))
        md = ttk.Combobox(f, textvariable=maxdec, values=["1500", "3000", "5000", "8000", "12000"], width=7)
        md.grid(row=5, column=4, sticky="w", padx=(0, 14))
        Tip(md, "The most farm decisions one run may take (candles x passes). Longer periods are cut to their latest "
                "candles, so no run grows huge. Auto and Randomize stay under it too.")

        def max_dec():
            try:
                return max(300, min(self.TC_MAX * 5, int(float(maxdec.get()))))
            except ValueError:
                return 5000

        def limit_for(n_pass):  # candles per run, so candles x passes stays under Max decisions
            return max(300, min(self.TC_MAX, max_dec() // max(1, n_pass)))
        cf_ = ttk.Frame(f)
        cf_.grid(row=3, column=0, columnspan=6, sticky="w", pady=(0, 8))
        ttk.Label(cf_, text="Tickers to train on").pack(side="left")
        tc_coins, tce = self.test_coins_field(cf_, "tc", coins)
        tce.pack(side="left", padx=8)
        note = ttk.Label(f, text="", foreground=MUTED)
        note.grid(row=6, column=0, columnspan=6, sticky="w", pady=(6, 0))

        def update_note(*_):
            try:
                a, b = per["bounds"]()
            except ValueError:
                return note.config(text="Write the start date like 2020-03-02.")
            n = self.est_candles(a, b, per["gran"]())
            try:
                lim = limit_for(int(passes.get() or 1))
            except ValueError:
                lim = limit_for(1)
            cap = f" (only the latest {lim:,} are used, to stay under Max decisions)" if n > lim else ""
            note.config(text=f"Tickers: {', '.join(self.parse_coins(tc_coins.get()) or coins)}   |   about {min(n, lim):,} candles per ticker{cap}   |   "
                             f"about {min(n, lim) * int(passes.get() or 1):,} farm decisions in total")
        for var in (per["length"], per["candle"], per["when"], per["date"], passes, tc_coins, maxdec):
            var.trace_add("write", update_note)
        update_note()
        row = ttk.Frame(f)
        row.grid(row=7, column=0, columnspan=6, sticky="we", pady=(10, 0))
        go = ttk.Button(row, text="⚔ Start training", style="Accent.TButton")
        go.pack(side="left")
        stop_b = ttk.Button(row, text="Stop", state="disabled")
        stop_b.pack(side="left", padx=6)
        rnd_b = ttk.Button(row, text="\U0001f3b2 Randomize", style="Plain.TButton")
        rnd_b.pack(side="left", padx=(0, 6))
        Tip(rnd_b, "Picks a random period, candle size, passes, farm size and starting bots for you, always under Max "
                   "decisions (mostly on your farm's own candle size, so its graduates fit your farm).")
        auto_var = tk.BooleanVar(value=False)
        auto_cb = ttk.Checkbutton(row, text="Auto", variable=auto_var)
        auto_cb.pack(side="left", padx=(0, 4))
        ttk.Label(row, text="wait").pack(side="left")
        wait_var = tk.StringVar(value=str(self.cfg.get("tc_wait", 10)))
        wait_cb = ttk.Combobox(row, textvariable=wait_var, values=["0", "5", "10", "30", "60"], width=3, state="readonly")
        wait_cb.pack(side="left", padx=(2, 2))
        ttk.Label(row, text="s").pack(side="left", padx=(0, 6))
        Tip(wait_cb, "Seconds Auto waits between camps (a breather for the app). Stop cancels the next camp while it waits.")
        Tip(auto_cb, "Auto: runs one Training camp after another with random settings (under Max decisions). After each "
                     "run your farm learns from it and the bots that passed the exam join your farm. Runs while this "
                     "window is open; Stop or untick to end it. Essentials only keeps it off.")
        bar = ttk.Progressbar(row, length=360, mode="determinate")
        bar.pack(side="left", padx=10)
        status = ttk.Label(row, text="", foreground=MUTED)
        status.pack(side="left")
        bb = ttk.Frame(w, padding=10)
        bb.pack(side="bottom", fill="x")
        tbox = tk.Frame(w, bg=BG)
        tbox.pack(fill="both", expand=True, padx=12)
        tree = self.make_tree(tbox, ("name", "strategy", "pace", "train", "exam", "trades", "wr", "passed"),
                              (150, 190, 90, 110, 110, 60, 60, 80),
                              ("Bot", "Strategy", "Decides", "Training return", "Exam return", "Trades", "Win rate", "Best trade",
                               "Worst trade", "Result"))
        tree.configure(selectmode="extended")
        tree.tag_configure("pass", foreground=GREEN)
        tree.tag_configure("fail", foreground=MUTED)
        b_bring = ttk.Button(bb, text="Bring passed bots to my farm", state="disabled")
        b_teach = ttk.Button(bb, text="Teach my farm what it learned", state="disabled")
        b_lib = ttk.Button(bb, text="Save passed bots to the Library", state="disabled")
        for b in (b_bring, b_teach, b_lib):
            b.pack(side="left", padx=(0, 6))
        Tip(b_bring, "The bots that passed join your farm (as royal eggs, keeping what they learned) and compete on live prices. "
                     "Select rows first to bring only those.")
        Tip(b_teach, "Gives your farm the camp's memory: its elders (nudge future eggs), its post-trade review, its mutation "
                     "memory and its season champions (woken up when their market mood comes back).")
        Tip(b_lib, "Saves the passed bots to your Library, with their exam result in their memory.")
        ttk.Button(bb, text="Close", command=lambda: w.winfo_toplevel().destroy()).pack(side="right")
        st = dict(stop=False, res=None)

        def run():
            try:
                pa, pb = per["bounds"]()
            except ValueError as e:
                return messagebox.showerror("Training camp", str(e), parent=w)
            gran = per["gran"]()
            picked = self.parse_coins(tc_coins.get())
            if not picked:
                return messagebox.showerror("Training camp", "Type at least one ticker, like SPY.", parent=w)
            coins[:] = picked
            self.cfg["tc_coins"] = ", ".join(picked)
            per["save"]()
            try:
                n_pass = min(5, max(1, int(passes.get())))
                cap = min(200, max(14, int(float(size.get()))))
            except ValueError:
                return messagebox.showerror("Training camp", "Farm size must be a number (14 to 200).", parent=w)
            ex = {"None": 0.0, "15%": 0.15, "25%": 0.25, "35%": 0.35}[exam.get()]
            self.cfg.update(tc_passes=n_pass, tc_size=cap, tc_exam=exam.get(), tc_max_dec=max_dec(),
                            tc_start=start.get())
            E.save_json(CONFIG_PATH, self.cfg)
            seeds = []
            if start.get() == "My current farm" and self.tourn:
                seeds = [dict(E._genes(v), size=v.size_mult) for v in self.tourn.vars]
            elif start.get() == "Hall of Fame":
                seeds = [dict(b) for b in self.hof()]
            elif start.get() == "Library":
                seeds = [dict(b) for b in self.library()]
            opts = E.opts_for_bars(E.tournament_opts(self.settings), gran)
            fee, slip = E.cost_model(self.settings)
            pct, cash, quote = self.settings["trade_pct"] / 100, self.start_cash, self.quote
            evolve, params = int(self.settings["evolve_every"]), self.settings["params"]
            retire = int(float(self.settings["retire_hours"]) * 3600 / gran)
            st["stop"] = False
            go.config(state="disabled")
            stop_b.config(state="normal")
            for b in (b_bring, b_teach, b_lib):
                b.config(state="disabled")
            tree.delete(*tree.get_children())
            status.config(text="Loading prices...")
            bar.config(value=0)

            def prog(done, total, text):
                self.q.put(("call", lambda: (bar.config(value=100 * done / max(1, total)),
                                             status.config(text=f"{done:,} of {total:,} decisions: {text}"))
                            if w.winfo_exists() else None))

            def work():
                try:
                    closes, extra, info = self.load_stretch(
                        coins, gran, pa, pb, lambda txt: self.q.put(("call", lambda: status.config(text=txt) if w.winfo_exists() else None)),
                        limit_for(n_pass))
                    if info["bars"] < 300:
                        raise RuntimeError(f"only {info['bars']} candles in that period: pick a longer period or smaller candles")
                    res = E.train_farm(closes, extra, opts, cap=cap, passes=n_pass, exam_frac=ex, seed_genes=seeds,
                                       start_cash=cash, fee=fee, pct=pct, evolve=evolve, params=params,
                                       retire_steps=retire, progress=prog, stop=lambda: st["stop"])
                    res["gran"] = gran
                    self.q.put(("call", lambda: show(res)))
                except E.TrainingStopped:
                    self.q.put(("call", lambda: finish("Stopped.")))
                except Exception as e:
                    self.q.put(("call", lambda e=e: auto_failed(e) if w.winfo_exists() else None))
            threading.Thread(target=work, daemon=True).start()

        def auto_failed(e):
            finish("")
            if not auto_var.get():
                return messagebox.showerror("Training camp", f"Couldn't finish: {e}", parent=w)
            st["fails"] = st.get("fails", 0) + 1
            if st["fails"] >= 5:
                auto_var.set(False)
                return status.config(text=f"Auto stopped after 5 runs in a row couldn't finish (last: {e}).")
            status.config(text=f"That one couldn't run ({e}); trying other settings...")
            auto_next(10)

        def finish(text):
            if not w.winfo_exists():
                return
            go.config(state="normal")
            stop_b.config(state="disabled")
            status.config(text=text)

        def show(res):
            if not w.winfo_exists():
                return
            st["res"] = res
            bar.config(value=100)
            t = res["tourn"]
            passed = sum(1 for r in res["results"] if r["passed"])
            gen = max((v.gen for v in t.vars), default=0)
            finish(f"Done: {res['passes_done']} pass(es) over {res['train_bars']:,} candles, {gen} generations, "
                   f"{len(t.lineage)} bots lived. " + (f"Exam on the last {res['exam_bars']:,} candles: {passed} passed."
                                                     if res["exam_bars"] else ""))
            for i, r in enumerate(res["results"]):
                g = r["genes"]
                tree.insert("", "end", iid=str(i), tags=("pass" if r["passed"] else "fail",), values=(
                    E.bot_nickname(r["v"].key()), STRAT_LABELS.get(g["name"], g["name"]), E.fmt_bar(g["tf"] * res["gran"] / 60),
                    pct_txt(r["train_ret"]), pct_txt(r["exam_ret"]), r["trades"],
                    "-" if r["winrate"] is None else f"{r['winrate'] * 100:.0f}%",
                    "-" if r.get("best_trade") is None else f"{r['best_trade']:+.1f}%",
                    "-" if r.get("worst_trade") is None else f"{r['worst_trade']:+.1f}%",
                    "✓ passed" if r["passed"] else ("no exam" if r["passed"] is None else "failed")))
            for b in (b_bring, b_teach, b_lib):
                b.config(state="normal")
            self.write(f"⚔ Training camp finished: {passed} of {len(res['results'])} top bots passed the exam.", "good")
            if auto_var.get():
                st["fails"] = 0
                try:
                    auto_apply(res)
                except Exception:
                    E.note_error("auto training camp")
                auto_next()

        def chosen():
            res = st["res"]
            sel = [res["results"][int(i)] for i in tree.selection()]
            return sel or [r for r in res["results"] if r["passed"] or (r["passed"] is None and (r["train_edge"] or 0) > 0)]

        def bring():
            if not self.tourn or not self.seeded:
                return self._nothing(parent, "Training camp", "Start your farm first (Wildcard), then bring the bots over.", parent=w)
            picks = chosen()
            if not picks:
                return self._nothing(parent, "Training camp", "No bots passed. Select rows to bring some anyway.", parent=w)
            closes = {c_: self.hist[c_] for c_ in self.session_coins}
            n = 0
            for r in picks:
                g = r["genes"]
                v = self.tourn.add_variant(g["name"], g["params"], closes, 1.0, note="From the training camp:", tf=g["tf"],
                                           origin="training camp", risk=g["risk"], traits=g["traits"], vetoes=g["vetoes"],
                                           layers=g["layers"])
                if v:
                    v.royal = True
                    v.layer_stats = {k: list(x) for k, x in r["layer_stats"].items()}
                    v.veto_stats = {k: list(x) for k, x in r["veto_stats"].items() if k in v.vetoes}
                    if r["exam_edge"] is not None:
                        v.backtests.append(dict(date=datetime.now().strftime("%Y-%m-%d %H:%M"), bars=st["res"]["exam_bars"],
                                                ret=round(r["exam_ret"] * 100, 2), edge=round(r["exam_edge"] * 100, 2),
                                                maxdd=None, trades=r["trades"], winrate=None, segments_passed=int(bool(r["passed"])),
                                                segments=1, fees2x=None, mc_dd95=None))
                    n += 1
            self.tourn.cap = max(self.tourn.cap, len(self.tourn.vars))
            self.flush_events()
            self.refresh_board()
            self.write(f"⚔ {n} bots from the training camp joined your farm. They still have to prove themselves on live prices.", "good")

        def teach():
            if not self.tourn:
                return self._nothing(parent, "Training camp", "Start your farm first (Wildcard).", parent=w)
            E.teach_from(self.tourn, st["res"]["tourn"])
            self.save_farm(force=True)
            self.write("Your farm learned from the training camp: elders, post-trade review, mutation memory and season champions.", "good")

        def save_lib():
            picks = chosen()
            if not picks:
                return self._nothing(parent, "Training camp", "No bots passed. Select rows to save some anyway.", parent=w)
            t = st["res"]["tourn"]
            ids = {id(r["v"]) for r in picks}
            rows = [dict(r, bar_minutes=st["res"]["gran"] / 60) for r in t.board() if id(r["v"]) in ids]
            entries = E.export_bots(rows, self.version, note="training camp graduate")["bots"]
            by_key = {r["v"].key(): r for r in picks}
            for e in entries:
                r = by_key.get(E.bot_key(e))
                if r and r["exam_edge"] is not None:
                    e["backtests"] = [dict(date=datetime.now().strftime("%Y-%m-%d %H:%M"), bars=st["res"]["exam_bars"],
                                           ret=round(r["exam_ret"] * 100, 2), edge=round(r["exam_edge"] * 100, 2),
                                           trades=r["trades"], segments_passed=int(bool(r["passed"])), segments=1)]
            self.catch_rows(entries, "training camp")
        go.config(command=run)
        def stop_all():
            auto_var.set(False)
            aid = st.pop("after", None)
            if aid:  # waiting for the next camp: cancel it
                try:
                    w.after_cancel(aid)
                except Exception:
                    pass
                stop_b.config(state="disabled")
                return status.config(text="Auto stopped.")
            st["stop"] = True
            status.config(text="Stopping...")
        stop_b.config(command=stop_all)

        def randomize():
            import random as _r
            rng = _r.Random()
            farm_s = int(self.farm_gran())
            cands = [nm for nm, g in self.CANDLES if g == farm_s] or [self.CANDLES[0][0]]
            others = [nm for nm, g in self.CANDLES if g != farm_s and g <= 3600]
            cn = cands[0] if rng.random() < 0.75 or not others else rng.choice(others)
            gran = dict(self.CANDLES)[cn]
            n_pass = rng.choice([1, 1, 2, 2, 3])
            lim = limit_for(n_pass)

            def est(nm):  # candles in that period (markets that close count only their open hours)
                try:
                    a_, b_ = self.period_bounds(nm, "Most recent", per["date"].get())
                    return self.est_candles(a_, b_, gran)
                except Exception:
                    return dict(self.PERIODS)[nm] * 86400 / gran
            fits = [nm for nm, _ in self.PERIODS if 600 <= est(nm) <= lim * 1.2] or \
                [min(self.PERIODS, key=lambda x: abs(est(x[0]) - lim))[0]]
            per["candle"].set(cn)
            per["length"].set(rng.choice(fits))
            days = dict(self.PERIODS)[per["length"].get()]
            if rng.random() < 0.3:
                per["when"].set("Most recent")
            else:  # somewhere in the last 3 years (newer tickers don't go back further)
                back = rng.uniform(days + 2, max(days + 3, 3 * 365))
                per["when"].set("Starting on")
                per["date"].set((datetime.now() - timedelta(days=back)).strftime("%Y-%m-%d"))
            passes.set(str(n_pass))
            size.set(str(rng.choice([20, 30, 40])))
            exam.set("25%")
            starts = ["Fresh eggs"] + (["My current farm"] * 2 if self.tourn else []) + (["Hall of Fame"] if self.hof() else [])
            start.set(rng.choice(starts))
            update_note()

        def auto_apply(res):
            """Auto: the farm learns from the run and its exam passers join it (no pop-ups)."""
            if not (self.tourn and self.seeded):
                return self.write("\u2694 Auto training camp: start your farm (Wildcard) and the camps' lessons and "
                                  "graduates go to it.", "warn")
            E.teach_from(self.tourn, res["tourn"])
            closes = {c_: self.hist[c_] for c_ in self.session_coins}
            n = 0
            for r in [r for r in res["results"] if r["passed"]][:3]:
                g = r["genes"]
                v = self.tourn.add_variant(g["name"], g["params"], closes, 1.0, note="From the auto training camp:",
                                           tf=g["tf"], origin="training camp", risk=g["risk"], traits=g["traits"],
                                           vetoes=g["vetoes"], layers=g["layers"])
                if v:
                    v.layer_stats = {k: list(x) for k, x in r["layer_stats"].items()}
                    n += 1
            self.flush_events()
            self.refresh_board()
            self.save_farm(force=True)
            self.write("\u2694 Auto training camp: your farm learned from it" +
                       (f", and {n} bot{'s' if n != 1 else ''} that passed joined your farm." if n else "."), "good")

        def auto_next(delay=None):
            if not (auto_var.get() and w.winfo_exists()):
                return
            if self.cfg.get("essentials"):
                auto_var.set(False)
                return status.config(text="Auto stopped: Essentials only is on.")
            if delay is None:
                try:
                    delay = max(0, min(600, int(wait_var.get())))
                except ValueError:
                    delay = 10
                self.cfg["tc_wait"] = delay
            stop_b.config(state="normal")  # Stop works while it waits, too

            def tick(left):
                st.pop("after", None)
                if not w.winfo_exists():
                    return
                if not auto_var.get():  # Auto was unticked while waiting
                    if str(go.cget("state")) != "disabled":
                        stop_b.config(state="disabled")
                    return status.config(text="Auto stopped.")
                if left > 0:
                    status.config(text=f"Auto: next camp in {left} second{'s' if left != 1 else ''} (Stop cancels it)")
                    st["after"] = w.after(1000, lambda: tick(left - 1))
                    return
                if str(go.cget("state")) != "disabled":
                    randomize()
                    run()
            tick(delay)

        def auto_toggled():
            if auto_var.get():
                if self.essentials_block("Auto training camp"):
                    return auto_var.set(False)
                st["fails"] = 0
                if str(go.cget("state")) != "disabled":
                    auto_next(1)
        auto_cb.config(command=auto_toggled)
        rnd_b.config(command=randomize)
        b_bring.config(command=bring)
        b_teach.config(command=teach)
        b_lib.config(command=save_lib)

    # ---------------- free roam (self-study) ----------------
    ROAM_CANDLES = (60, 300, 900, 3600, 86400)  # 1 min to 1 day: studies use the farm's own candle size when they can

    def farm_gran(self):
        """The candle size the farm lives on (seconds), which is what its bots must be studied and examined on."""
        return E.candle_gran(float(getattr(self, "session_bar", 0) or self.settings.get("bar_minutes", 1)))

    # ---------------- farm knowledge: see, save and share what the farms have learned ----------------
    KNOW_TEXT = {"Never-rule": "Things bots refuse to do", "Study": "Extra reasons for and against",
                 "Instinct": "Main instincts", "Volatility gate": "Reading volatility", "Season champion": "Seasons mastered",
                 "Elder": "Elders' wisdom", "Mutation lesson": "How to breed winners", "Proven bot": "Proven dragons",
                 "Hindsight": "What would have worked",
                 "Ticker habit": "Tickers it struggles with"}

    def know_sources(self):
        """[(label, farm or None, saved book path or None)]: the live farms first, then saved books."""
        out = []
        if self.tourn:
            out.append(("Bot Farm (now)", self.tourn, None))
        if getattr(self, "slow", {}).get("t"):
            out.append(("Slow farm (now)", self.slow["t"], None))
        if getattr(self, "lab", {}).get("t"):
            out.append(("Scalp lab (now)", self.lab["t"], None))
        for b in E.list_knowledge():
            out.append((f"Book: {b['name']}  ({datetime.fromtimestamp(b['saved']):%b %d %Y}, {b['n']} items)", None, b["path"]))
        return out

    def know_targets(self):
        out = []
        if self.tourn:
            out.append(("Bot Farm", self.tourn, (lambda: {c: self.hist[c] for c in self.session_coins}) if self.seeded else (lambda: None)))
        sl = getattr(self, "slow", {})
        if sl.get("t"):
            out.append(("Slow farm", sl["t"], lambda: {c: sl["hist"][c] for c in sl["t"].coins if c in sl.get("hist", {})} or None))
        if getattr(self, "lab", {}).get("t"):
            out.append(("Scalp lab", self.lab["t"], lambda: None))
        return out

    def open_knowledge(self, parent=None):
        """Farm knowledge: ability meters for everything the farm has learned, a grid of the items with their evidence,
        saved knowledge books, and 'Add selected to' another farm."""
        if parent is None:
            return self.open_hub("learning", "Farm knowledge")
        w = parent
        w.configure(bg=BG)
        top = tk.Frame(w, bg=BG, padx=10, pady=8)
        top.pack(fill="x")
        tk.Label(top, text="\U0001f4d6 Farm knowledge", bg=BG, fg=TEXT, font=("", 15, "bold")).pack(side="left")
        tk.Label(top, text="From", bg=BG, fg=MUTED).pack(side="left", padx=(16, 4))
        srcs = self.know_sources()
        src = tk.StringVar(value=srcs[0][0] if srcs else "")
        src_box = ttk.Combobox(top, textvariable=src, values=[x[0] for x in srcs], state="readonly", width=46)
        src_box.pack(side="left")
        ttk.Button(top, text="Save as a book...", style="Plain.TButton", command=lambda: save_book()).pack(side="left", padx=6)
        ttk.Button(top, text="Delete book", style="Plain.TButton", command=lambda: del_book()).pack(side="left")
        meters = tk.Canvas(w, height=168, bg=CARD, highlightthickness=0)
        meters.pack(fill="x", padx=10)
        fbar = tk.Frame(w, bg=BG, padx=10)
        fbar.pack(fill="x", pady=(8, 2))
        tk.Label(fbar, text="Show", bg=BG, fg=MUTED).pack(side="left")
        kind = tk.StringVar(value="Everything")
        ttk.Combobox(fbar, textvariable=kind, values=["Everything"] + [self.KNOW_TEXT[k] for k in E.KNOW_KINDS], state="readonly",
                     width=26).pack(side="left", padx=6)
        proven = tk.BooleanVar(value=False)
        ttk.Checkbutton(fbar, text="Proven only", variable=proven).pack(side="left")
        tk.Label(fbar, text="   Click rows to choose (Ctrl / Shift for several).", bg=BG, fg=MUTED, font=("", 8)).pack(side="left")
        gf = tk.Frame(w, bg=BG, padx=10)
        gf.pack(fill="both", expand=True)
        grid = self.make_tree(gf, ("kind", "title", "proven", "evidence", "detail"), (215, 260, 60, 330, 380),
                              ("Kind", "What it learned", "Proven", "Evidence", "Details"))
        grid.configure(selectmode="extended", height=14)
        for c_ in ("kind", "title", "evidence", "detail", "proven"):
            grid.column(c_, anchor="w")
        grid.tag_configure("strong", foreground=GREEN)
        grid.tag_configure("info", foreground=MUTED)
        bot = tk.Frame(w, bg=BG, padx=10, pady=8)
        bot.pack(fill="x")
        ttk.Button(bot, text="Select all shown", style="Plain.TButton", command=lambda: grid.selection_set(grid.get_children())).pack(side="left")
        ttk.Button(bot, text="Select proven", style="Plain.TButton",
                   command=lambda: grid.selection_set([i for i in grid.get_children() if "strong" in grid.item(i, "tags")])).pack(side="left", padx=6)
        tk.Label(bot, text="Add selected to", bg=BG, fg=TEXT).pack(side="left", padx=(16, 4))
        tgt = tk.StringVar(value=(self.know_targets() or [("", None, None)])[0][0])
        tgt_box = ttk.Combobox(bot, textvariable=tgt, values=[x[0] for x in self.know_targets()], state="readonly", width=14)
        tgt_box.pack(side="left")
        ttk.Button(bot, text="Add", style="Accent.TButton", command=lambda: add()).pack(side="left", padx=6)
        res_lbl = tk.Label(w, text="", bg=BG, fg=MUTED, anchor="w", justify="left", padx=10, wraplength=1100, font=("", 8))
        res_lbl.pack(fill="x", pady=(0, 8))
        st = dict(items=[])

        def current():
            for lab, farm, path in self.know_sources():
                if lab == src.get():
                    if farm is not None:
                        lock = getattr(self, "_lab_lock", None) if farm is getattr(self, "lab", {}).get("t") else None
                        if lock:
                            with lock:
                                return E.farm_knowledge(farm)
                        return E.farm_knowledge(farm)
                    return E.load_knowledge(path)
            return []

        def draw_meters(items):
            meters.delete("all")
            W = max(600, meters.winfo_width())
            lv = E.knowledge_levels(items)
            kinds = [k for k in E.KNOW_KINDS if k != "Ticker habit"]
            cols = 4
            cw = (W - 20) / cols
            for i, k in enumerate(kinds):
                n, strong, level = lv[k]
                x0, y0 = 10 + (i % cols) * cw, 8 + (i // cols) * 40
                meters.create_text(x0, y0, anchor="nw", text=self.KNOW_TEXT[k], fill=TEXT, font=("", 9, "bold"))
                meters.create_text(x0 + cw - 14, y0, anchor="ne", text=f"{strong} proven / {n}", fill=MUTED, font=("", 8))
                for j in range(10):
                    px = x0 + j * (cw - 24) / 10
                    meters.create_rectangle(px, y0 + 17, px + (cw - 24) / 10 - 3, y0 + 27, outline="",
                                            fill=(GREEN if level >= 7 else ACCENT if level >= 4 else "#f59e0b") if j < level else LINE)
            tot = sum(1 for x in items if x.get("strong"))
            meters.create_text(10, 154, anchor="w", fill=MUTED, font=("", 8),
                               text=f"{len(items)} things learned, {tot} proven by their own evidence. Meters fill "
                                    "as more are proven (instincts: winning now, or a record that beats holding).")

        def fill():
            items = st["items"]
            grid.delete(*grid.get_children())
            want = kind.get()
            for i, x in enumerate(items):
                if want != "Everything" and self.KNOW_TEXT.get(x["kind"]) != want:
                    continue
                if proven.get() and not x.get("strong"):
                    continue
                tag = "info" if (x.get("data") or {}).get("info_only") else "strong" if x.get("strong") else ""
                grid.insert("", "end", iid=str(i), tags=(tag,), values=(self.KNOW_TEXT.get(x["kind"], x["kind"]), x["title"],
                                                                        "\u2714" if x.get("strong") else "", x["evidence"], x["detail"]))

        def reload(*_):
            st["items"] = current()
            draw_meters(st["items"])
            fill()

        def save_book():
            if not st["items"]:
                return messagebox.showinfo("Farm knowledge", "Nothing learned to save yet.", parent=w)
            name = simpledialog.askstring("Save knowledge", "Name this book (e.g. 'Spring 2026 farm'):", parent=w)
            if not name:
                return
            E.save_knowledge(name, st["items"], src.get())
            src_box.config(values=[x[0] for x in self.know_sources()])
            res_lbl.config(text=f"Saved '{name}' ({len(st['items'])} items). Pick it under 'From' any time, or add it to a farm.",
                           fg=GREEN)

        def del_book():
            hit = next((x for x in self.know_sources() if x[0] == src.get() and x[2]), None)
            if not hit:
                return messagebox.showinfo("Farm knowledge", "Pick a saved book under 'From' first.", parent=w)
            if messagebox.askyesno("Farm knowledge", f"Delete {hit[0]}?", parent=w):
                try:
                    os.remove(hit[2])
                except OSError:
                    pass
                vals = [x[0] for x in self.know_sources()]
                src_box.config(values=vals)
                src.set(vals[0] if vals else "")

        def add():
            picks = [st["items"][int(i)] for i in grid.selection()]
            hit = next((x for x in self.know_targets() if x[0] == tgt.get()), None)
            if not picks:
                return messagebox.showinfo("Farm knowledge", "Choose one or more rows first.", parent=w)
            if not hit:
                return messagebox.showinfo("Farm knowledge", "There's no farm to add to yet: press Start once.", parent=w)
            name, farm, closes = hit
            lock = getattr(self, "_lab_lock", None) if name == "Scalp lab" else None
            if lock:
                with lock:
                    notes = E.apply_knowledge(farm, picks, closes())
            else:
                notes = E.apply_knowledge(farm, picks, closes())
            self.write(f"\U0001f4d6 Added {len(picks)} piece{'s' if len(picks) != 1 else ''} of knowledge to the {name}.", "good")
            res_lbl.config(text=f"Added to the {name}:  " + "   \u2022  ".join(notes[:12]) + ("  ..." if len(notes) > 12 else ""), fg=TEXT)
            self.refresh_board()
            reload()
        src_box.bind("<<ComboboxSelected>>", reload)
        kind.trace_add("write", lambda *a: fill())
        proven.trace_add("write", lambda *a: fill())
        meters.bind("<Configure>", lambda e: draw_meters(st["items"]))
        reload()

    # ---------------- feature tests: what each kind of learning is worth ----------------
    @staticmethod
    def learning_switches():
        """The on/off learning switches from Settings (Wildcard and Learning) a feature test can try."""
        return [f[0] for g, _, fields in SETTING_GROUPS if g in ("Wildcard", "Learning") for f in fields
                if f[0] and f[2] is bool]

    @staticmethod
    def switch_label(key):
        """A short name for a learning switch: its Settings text up to the first colon."""
        text = next((f[1] for f in SETTING_FIELDS if f[0] == key), key)
        head = text.split(":")[0] if ":" in text[:60] else text
        return head if len(head) <= 70 else head[:67].rstrip() + "..."

    def _roam_features(self, t):
        """Feature test: the same practice farm lives through the same stretch twice, with one learning switch on and
        then off, and its best bots (by training) sit an exam on prices they never saw. The difference goes into the
        feature book, which the 'What learning is worth' report reads. Settings are never changed."""
        import random as _r
        rng = _r.Random()
        book = self.__dict__.setdefault("roam_features", {})
        key = E.pick_feature(E.feature_candidates(self.settings, self.learning_switches()), book, rng)
        if not key:  # every switch is off: nothing to compare
            return self._roam_check(t)
        closes, extra, info = self._roam_pick(rng, 700, prefer=self.farm_gran())
        lead = t.active[:4] or t.vars[:4]
        seeds = [E._genes(v) for v in lead]
        label = self.switch_label(key)
        self.roam_now = dict(info, kind="Feature test", phase="study", text=f"Testing what '{label}' is worth",
                             bots=[], done=0, total=1, t0=time.time())
        cur = self.roam_now  # this study's own status (a newer study replaces self.roam_now)
        self.roam_feed.append(dict(kind="sep", text=f"Feature test ({label}) on {self._when(info['start'], True)} to "
                                                    f"{self._when(info['end'], True)}  ({E.fmt_bar(info['gran'] / 60)} candles)"))
        fee, slip = E.cost_model(self.settings)

        def prog(done, total, text):
            cur.update(done=done, total=total, text=f"{label}: {text}")
        r = E.feature_test(closes, extra, dict(self.settings), key, (lambda s_: E.opts_for_bars(E.tournament_opts(s_), info["gran"])), seed=rng.randrange(1 << 30),
                           progress=prog, cap=20, passes=1, exam_frac=0.25, seed_genes=seeds, start_cash=self.start_cash,
                           fee=fee, pct=self.settings["trade_pct"] / 100, evolve=int(self.settings["evolve_every"]),
                           params=self.settings["params"], stop=lambda: self.tourn is not t or not self.cfg.get("roam_on"))

        def apply():
            judged = E.record_feature(book, key, r["on"], r["off"])
            fv = E.feature_verdict(book.get(key))
            if judged:
                summary = (f"{label}: the farm's picks did {r['on'] * 100:+.1f}% with it and {r['off'] * 100:+.1f}% without, "
                           f"vs holding, on prices they hadn't seen. After {fv['n']} test(s): "
                           f"{E.FEATURE_VERDICT_TEXT[fv['verdict']].split(':')[0].lower()}")
            else:
                summary = f"{label}: couldn't be judged this time (one run's picks hardly traded)"
            rec = dict(time=time.time(), kind="Feature test", revisit=bool(info.get("revisit")), start=info["start"],
                       end=info["end"], gran=info["gran"], bars=info["bars"], hold=round(info["hold"] * 100, 2),
                       mood=info["mood"], results=[], feature=key, on=r["on"], off=r["off"], summary=summary)
            self._roam_finish(rec, cur)
        self.q.put(("call", apply))

    def feature_rows(self):
        """One row per learning switch for the report: (key, label, verdict dict), most useful first."""
        book = getattr(self, "roam_features", None)
        if book is None:
            book = dict((E.load_json(self.roam_path(), {}) or {}).get("features") or {})
        keys = list(dict.fromkeys(E.feature_candidates(self.settings, self.learning_switches()) + list(book)))
        order = {"helps": 0, "leans helps": 1, "no clear effect": 2, "testing": 3, "leans hurts": 4, "hurts": 5, "unused": 6}
        rows = [(k, self.switch_label(k), E.feature_verdict(book.get(k))) for k in keys]
        return sorted(rows, key=lambda x: (order[x[2]["verdict"]], -(x[2]["mean"] or 0), x[1]))

    def build_feature_report(self, parent):
        """The 'What learning is worth' table, inside `parent`. -> a function that refreshes it."""
        tk.Label(parent, bg=BG, fg=MUTED, justify="left", wraplength=900, anchor="w", text=(
            "Free roam tests the learning switches one at a time: the same practice farm lives through the same stretch "
            "twice, with the switch on and then off, and its best bots (picked by how they trained, as the farm would "
            "pick them) sit an exam on prices they never saw. 'With vs without' is how much better (+) or worse (-) "
            "those picks did with it on, in % points, on average. A verdict needs at least 4 tests and a steady "
            "difference, because with about 30 switches a few will look good by luck alone. Nothing is changed for "
            "you: switch things off in Settings > Learning if you decide to.")).pack(fill="x", pady=(6, 4))
        box = tk.Frame(parent, bg=BG)
        box.pack(fill="both", expand=True)
        tree = self.make_tree(box, ("what", "n", "diff", "why"), (330, 60, 120, 470),
                              ("Learning", "Tests", "With vs without", "Verdict"))
        tree.configure(height=16)
        tree.column("what", anchor="w")
        tree.tag_configure("good", foreground=GREEN)
        tree.tag_configure("bad", foreground=RED)
        tree.tag_configure("dim", foreground=MUTED)
        note = tk.Label(parent, bg=BG, fg=MUTED, anchor="w", justify="left", wraplength=900)
        note.pack(fill="x", pady=(4, 0))

        def fill():
            tree.delete(*tree.get_children())
            rows = self.feature_rows()
            for key, label, fv in rows:
                v = fv["verdict"]
                tag = "good" if v in ("helps", "leans helps") else "bad" if v in ("hurts", "leans hurts") else \
                    "dim" if v in ("testing", "unused") else ""
                tree.insert("", "end", values=(label, fv["n"], "-" if fv["mean"] is None else f"{fv['mean']:+.2f} pts",
                                               E.FEATURE_VERDICT_TEXT[v]), tags=(tag,))
            done = sum(fv["n"] for _, _, fv in rows)
            on = self.cfg.get("roam_on") and self.settings.get("feature_tests", True)
            note.config(text=(f"{done} feature tests so far. " + (
                "They run as part of Free roam, about one study in seven." if on else
                "They run as part of Free roam: switch Free roam on (and 'Free roam tests what each kind of learning is "
                "worth' in Settings > Learning) to add more.")))
        fill()
        return fill

    def open_learning_report(self):
        w = tk.Toplevel(self.root)
        w.title("What learning is worth")
        w.configure(bg=BG)
        f = tk.Frame(w, bg=BG, padx=14, pady=10)
        f.pack(fill="both", expand=True)
        tk.Label(f, text="What each kind of learning is worth", bg=BG, fg=TEXT, font=("", 13, "bold")).pack(anchor="w")
        fill = self.build_feature_report(f)
        ttk.Button(f, text="Close", command=w.destroy).pack(anchor="e", pady=(8, 0))

        def tick():
            if w.winfo_exists():
                fill()
                w.after(15000, tick)
        w.after(15000, tick)

    def roam_path(self):
        return os.path.join(E.HOME_DIR, "free-roam.json")

    def load_roam_state(self):
        """Free roam's saved study log and status (also needed by Auto camp and the exam pass)."""
        saved = E.load_json(self.roam_path(), {}) or {}
        self.roam_log = list(saved.get("log") or [])[-300:]
        self.roam_features = dict(saved.get("features") or {})  # learning switch -> [[with, without, when], ...]
        self.roam_n = int(saved.get("n", len(self.roam_log)))
        self.roam_days = set(saved.get("days") or [])   # past days the scalpers have trained on (day replays)
        self.roam_dead = set(saved.get("dead") or [])   # weekdays nobody traded (holidays)
        self.roam_now = None
        self.roam_err = None
        self.roam_feed = deque(maxlen=400)
        self.roam_trades = 0

    def start_free_roam(self):
        """Free roam: the farm goes back in time on its own, ONLY while Free roam is switched ON (nothing runs in the
        background otherwise). Each study either backtests some of its own
        bots on a stretch of history (anywhere back to 2016), or runs a small practice farm there; every lesson is saved."""
        if getattr(self, "_roam_thread", None) and self._roam_thread.is_alive():
            return
        self.load_roam_state()

        def loop():
            k = 0
            while True:
                time.sleep(1)
                try:
                    t = self.tourn
                    if not self.cfg.get("roam_on") or t is None or not self.session_coins:
                        continue
                    if not E.ALPACA.get("key"):  # history comes from Alpaca: wait for a login instead of failing
                        if not getattr(self, "_roam_login_note", False):
                            self._roam_login_note = True
                            self.q.put(("log", ("Free roam is waiting for you to log in to Alpaca (Account > Log in, a "
                                                "free paper key is enough): it studies history from there.", "warn")))
                        self.roam_err = (time.time(), "waiting for an Alpaca login (Account > Log in)")
                        continue
                    self._roam_login_note = False
                    wait = self.roam_wait()
                    if time.time() - getattr(self, "_roam_last", 0) < wait:
                        continue
                    k += 1
                    if self.settings.get("hindsight", True) and k % 5 == 4:
                        self._roam_hindsight(t)
                    elif self.cfg.get("roam_scalp", True) and (t.scalpers() or t.kamikazes()) and k % 4 == 2:
                        self._roam_scalp(t)
                    elif self.settings.get("feature_tests", True) and k % 7 == 6:
                        self._roam_features(t)
                    elif k % 3 == 0:
                        self._roam_camp(t)
                    else:
                        self._roam_check(t)
                    self.roam_err = None
                    self._roam_last = time.time()
                except E.TrainingStopped:  # the farm changed (reset, new session): just move on
                    self.roam_now = None
                except E.NeedLogin:  # logged out in the middle of a study: not a crash, just wait for the login
                    self.roam_now = None
                    self.roam_err = (time.time(), "waiting for an Alpaca login (Account > Log in)")
                except E.NoHistory as e:  # nothing to study there (a holiday, a gap in the data): not a crash
                    self.roam_now = None
                    self.roam_err = (time.time(), str(e))
                    self._roam_last = time.time() - max(0, self.roam_wait() - 60)
                except Exception as e:
                    self.roam_now = None
                    msg = str(e) or e.__class__.__name__
                    self.roam_err = (time.time(), msg)
                    self._record_error("free roam")
                    self.q.put(("log", (f"Free roam hit a problem: {msg[:160]}. It tries again in a minute.", "warn")))
                    self._roam_last = time.time() - max(0, self.roam_wait() - 60)
        self._roam_thread = threading.Thread(target=loop, daemon=True)
        self._roam_thread.start()

    def roam_wait(self):
        return 3

    def roam_check_on(self, t=None):
        """Stops the study in progress as soon as Free roam is switched off (or the farm changes)."""
        if threading.get_ident() in (getattr(self, "_exam_thread", None), getattr(self, "_camp_thread", None)):
            # the exam pass and Auto camp use Free roam's stretch picker with Free roam off
            if t is not None and self.tourn is not t:
                raise E.TrainingStopped()
            return
        if not self.cfg.get("roam_on") or (t is not None and self.tourn is not t):
            raise E.TrainingStopped()

    def roam_watching(self):
        """Is the Free roam screen open? (A flag the screen sets: background threads must never touch Tk.)"""
        return time.time() - getattr(self, "_roam_seen", 0) < 2

    def _roam_feed(self, who, c, tr, ts, kind):
        """One trade from a Free roam study, for the live trade list."""
        if kind == "test":
            self.roam_check_on()
        self.roam_trades = getattr(self, "roam_trades", 0) + 1
        o = tr.get("contract")
        what = E.contract_text(dict(o, under=c)) if o else c
        self.roam_feed.append(dict(t=ts, who=who, side=tr["side"], what=what, price=tr.get("price"),
                                   pl=tr.get("pl"), usd=tr.get("usd"), kind=kind))
        if self.roam_watching():
            time.sleep(0.03 if kind == "test" else 0.004)  # someone's watching: let the trades scroll by

    def _roam_pick(self, rng, min_bars=400, **kw):
        """Where the next Free roam study looks. Each stretch it downloads is studied `roam_reuse` times (Settings >
        Learning, default 3) before a new one is fetched. Repeat visits come back with info["revisit"] True: they teach,
        but never promote anyone (see E.StretchPool)."""
        if not hasattr(self, "roam_log"):  # (Auto camp or the exam pass can get here before Free roam ever started)
            self.load_roam_state()
        try:
            reuse = max(1, min(10, int(self.settings.get("roam_reuse", 3) or 1)))
        except (TypeError, ValueError):
            reuse = 3
        pool = self.__dict__.setdefault("_roam_pool", E.StretchPool())
        key = tuple(self.session_coins)
        again = pool.take(key, reuse, min_bars // 2, kw.get("prefer"))
        if again:
            self.roam_check_on()
            return again
        closes, extra, info = self._roam_fetch(rng, min_bars, **kw)
        return closes, extra, pool.add(key, reuse, closes, extra, info)

    def _roam_fetch(self, rng, min_bars=400, prefer=None):
        """The farm chooses where to learn: a candle size, then a stretch of history it has studied least, but only
        stretches where every ticker really traded (checked against each ticker's daily history first)."""
        coins = list(self.session_coins)
        now = time.time()
        earliest = max(E.HIST_START, datetime(int(self.settings.get("roam_from", 2016)), 1, 4).timestamp())
        covered = [(x["start"], x["end"]) for x in self.roam_log[-150:]]
        self.roam_now = dict(kind="Getting ready", phase="download", gran=None, coins=coins, start=None, end=None,
                             text="Checking which stretches of history your tickers have", t0=time.time(), done=0, total=1)
        coins, left_out = list(coins), []
        days = [d for d in E.common_days(coins, self.quote) if d >= earliest - 86400]
        self.roam_check_on()

        def room(ds):  # whole days of history these tickers share
            return (ds[-1] - ds[0]) / 86400 + 1 if len(ds) >= 3 else 0
        # a ticker listed much later than the others can leave too little shared history: study the rest this time
        while len(coins) > 1 and room(days) * E.bars_per_day(self.ROAM_CANDLES[0]) * 0.9 < min_bars:
            short = min(coins, key=lambda c: len(E.coin_days(c, self.quote)))
            coins.remove(short)
            left_out.append(short)
            days = [d for d in E.common_days(coins, self.quote) if d >= earliest - 86400]
        if len(days) < 3:
            raise E.NoHistory("Your tickers don't share enough history to study: "
                              + "; ".join(E.history_text(c, self.quote) for c in list(self.session_coins)))
        dset = set(days)
        last_err = None
        fits = [g for g in self.ROAM_CANDLES if room(days) * E.bars_per_day(g) / (7 / 5 * 1.04 / 1.0) * 0.9 >= min_bars]
        for _attempt in range(10):
            if _attempt and _attempt % 5 == 0 and len(coins) > 1:
                # 5 tries without a clean stretch: a ticker with gaps in its history (days without a single trade) or a
                # short one is in the way. Leave out the one that costs the shared history most, and try again.
                def cost(c):
                    rest = [x for x in coins if x != c]
                    return -len([d for d in E.common_days(rest, self.quote) if d >= earliest - 86400])
                worst = min(coins, key=cost)
                coins.remove(worst)
                left_out.append(worst)
                days = [d for d in E.common_days(coins, self.quote) if d >= earliest - 86400]
                if len(days) < 3:
                    break
                dset = set(days)
                fits = [g for g in self.ROAM_CANDLES if room(days) * E.bars_per_day(g) / (7 / 5 * 1.04 / 1.0) * 0.9 >= min_bars]
            # the farm's own candle size first: a bot that decides every 15 one-minute candles learns nothing true
            # about itself on daily candles (it would be deciding every 15 days)
            gran = prefer if prefer in fits and _attempt < 6 else rng.choice(fits or self.ROAM_CANDLES[:1])
            most = int(room(days) * E.bars_per_day(gran) / (7 / 5 * 1.04) * 0.9)  # never longer than the shared history
            n = rng.randint(min(min_bars, most), max(min(min_bars, most), min(1500, most)))
            span = n / E.bars_per_day(gran) * 7 / 5 * 1.04 * 86400
            # start days where the whole window has history (every ticker traded on nearly all of its days)
            ok = [d for d in days if d + span <= min(now, days[-1] + 86400) + 86400]
            starts = [d for d in rng.sample(ok, min(len(ok), 120)) if E.coverage(dset, d, d + span) >= 0.95]
            if not starts:
                last_err = E.NoHistory("no stretch that long where every ticker was trading")
                continue
            best = min(starts, key=lambda a: sum(max(0, min(a + span, e) - max(a, s0)) for s0, e in covered))
            a, b = best, min(now, best + span)
            self.roam_now = dict(kind="Getting ready", phase="download", gran=gran, coins=coins, start=a, end=b,
                                 text=f"Fetching {E.fmt_bar(gran / 60)} candles for {self._when(a, True)} to {self._when(b, True)}",
                                 t0=time.time(), done=0, total=1)
            try:
                closes, extra, info = self.load_stretch(
                    coins, gran, a, b, note=lambda txt: (self.roam_check_on(), self.roam_now and self.roam_now.update(text=txt)),
                    max_bars=2500)
                if info["bars"] >= min_bars // 2:
                    break
                last_err = E.NoHistory("too few candles there")
            except E.NoHistory as e:
                last_err = e
        else:
            raise last_err or E.NoHistory("couldn't find a stretch of history to study")
        if left_out and time.time() - getattr(self, "_roam_left_note", 0) > 6 * 3600:
            self._roam_left_note = time.time()
            self.q.put(("log", (f"\u25c7 Free roam: {', '.join(left_out)} only has a short or patchy history (days without trades), so Free roam studies the "
                                f"other tickers' longer past without {'it' if len(left_out) == 1 else 'them'} (the farm still "
                                f"trades {'it' if len(left_out) == 1 else 'them'} as usual).", "info")))
        times = next(iter(extra.values()))["t"]
        span = len(times)
        hold = sum(closes[c][-1] / closes[c][0] for c in coins) / len(coins) - 1
        moods = [E.market_mood(closes[c])[0] for c in coins]
        known = [m for m in moods if m != "unknown"]
        step = max(1, span // 300)
        prev = [sum(closes[c][i] / closes[c][0] for c in coins) / len(coins) for i in range(0, span, step)]
        info = dict(gran=gran, coins=coins, start=times[0], end=times[-1], bars=span, hold=hold, preview=prev,
                    preview_t=[times[i] for i in range(0, span, step)], oldest=earliest,
                    mood=max(set(known), key=known.count) if known else "unknown")
        return closes, extra, info

    @staticmethod
    def _when(ts, year=False):
        return datetime.fromtimestamp(ts).strftime("%b %d %Y" if year else "%b %d %Y %H:%M")

    def _roam_finish(self, rec, cur=None):
        self.roam_log.append(rec)
        del self.roam_log[:-300]
        self.roam_n = getattr(self, "roam_n", 0) + 1
        if cur is None or self.roam_now is cur:  # a newer study may already be under way: leave its status alone
            self.roam_now = None
        try:
            E.save_json(self.roam_path(), dict(n=self.roam_n, log=self.roam_log, features=getattr(self, "roam_features", {}), days=sorted(self.roam_days),
                                               dead=sorted(self.roam_dead)))
        except OSError:
            pass

    def _roam_check(self, t):
        """Self-test: the leader and a few others backtest themselves on the chosen stretch."""
        import random as _r
        rng = _r.Random()
        vars_ = list(t.vars)
        if not vars_:
            return
        closes, extra, info = self._roam_pick(rng, prefer=self.farm_gran())
        ranked = [v for v, _ in t.ranking()]
        extra_picks = [next((v for v in ranked if E.is_kami(v)), None)]  # every self-test includes a kamikaze...
        if info["gran"] <= 300:  # ...and, on short candles, a scalper (they trade inside one day)
            extra_picks.append(next((v for v in ranked if E.is_scalp(v.name)), None))
        # the ones that most need an exam first: BEASTS and the top of the ranking that haven't proven themselves yet
        need = [v for v in list(t.active) + ranked[:12] if not E.is_kami(v) and E.exam_verdict(v, t.serial)[0] == "untested"]
        picks = list(dict.fromkeys(([t.active[0]] if t.active else []) + need[:3] + [v for v in extra_picks if v]
                                   + rng.sample(vars_, min(2, len(vars_)))))[:6]
        names = [E.bot_nickname(v.key()) for v in picks]
        self.roam_now = dict(info, kind="Self-test", phase="study", text=f"{len(picks)} bots are testing themselves",
                             bots=names, done=0, total=len(picks), t0=time.time())
        cur = self.roam_now  # this study's own status (a newer study replaces self.roam_now)
        self.roam_feed.append(dict(kind="sep", text=f"Self-test on {self._when(info['start'], True)} to "
                                                    f"{self._when(info['end'], True)}  ({E.fmt_bar(info['gran'] / 60)} candles)"))
        fee, slip = E.cost_model(self.settings)
        out = []
        for i, v in enumerate(picks):
            if self.tourn is not t:
                return
            cur.update(done=i, text=f"{names[i]} is testing itself ({i + 1} of {len(picks)})")

            def prog(d, n, i=i):
                self.roam_check_on(t)
                if cur:
                    cur["done"] = i + d / max(1, n)
            out.append((v, E.backtest_bot(v, closes, extra, self.start_cash, fee, self.settings["trade_pct"] / 100, slip,
                                          progress=prog,
                                          on_trade=lambda c, tr, ts, nm=names[i]: self._roam_feed(nm, c, tr, ts, "test"))))

        def apply():
            if self.tourn is not t:
                return
            res = []
            taught = dict(bots=0, exams=0, elders=len(t.elders))
            for v, r in out:
                if v in t.vars:
                    E.remember_backtest(v, r, t)
                    taught["bots"] += 1
                    if not E.is_kami(v) and info["gran"] == self.farm_gran() and not info.get("revisit"):
                        # unseen prices (a first visit), its own candles: an exam
                        E.add_exam(v, "self-test", r["edge"] * 100, r.get("closed", r["trades"]), info["start"], info["gran"])
                        taught["exams"] += 1
                res.append((E.bot_nickname(v.key()), round(r["ret"] * 100, 2), r["trades"], round(r["ret"] * 100, 2),
                            r.get("closed", 0), r.get("wins", 0), r.get("best_trade"), r.get("worst_trade")))
            good = sum(1 for x in res if x[1] > 0)
            rec = dict(time=time.time(), kind="Self-test", revisit=bool(info.get("revisit")), start=info["start"], end=info["end"], gran=info["gran"],
                       bars=info["bars"], hold=round(info["hold"] * 100, 2), mood=info["mood"], results=res,
                       summary=f"{good} of {len(res)} made money; taught: {taught['bots']} bots' rule evidence and memory"
                               + (f", {taught['exams']} exams" if taught["exams"] else "")
                               + (f", {len(t.elders) - taught['elders']} new elders" if len(t.elders) > taught["elders"] else ""))
            self._roam_finish(rec, cur)
            if False:
                self.write(f"◇ Free roam: {len(res)} bots tested themselves on {self._when(info['start'], True)} to "
                           f"{self._when(info['end'], True)}; {good} made money. Lessons saved.")
        self.q.put(("call", apply))

    def _roam_hindsight(self, t):
        """Hindsight study: looking back at a stretch, which instincts and settings WOULD have made money (checked on the
        part after it they didn't see). What worked goes into the farm's hindsight book, which new eggs start from, and
        the best find hatches on your farm."""
        rng = random.Random()
        closes, extra, info = self._roam_pick(rng, 600, prefer=self.farm_gran())
        self.roam_now = dict(info, kind="Hindsight", phase="study", text="Looking back: which instincts would have worked here?",
                             bots=[], done=0, total=1, t0=time.time())
        cur = self.roam_now
        self.roam_feed.append(dict(kind="sep", text=f"Hindsight on {self._when(info['start'], True)} to "
                                                    f"{self._when(info['end'], True)}  ({E.fmt_bar(info['gran'] / 60)} candles)"))
        fee, slip = E.cost_model(self.settings)

        def prog(d, n):
            self.roam_check_on(t)
            cur.update(done=d, total=n, text=f"Looking back: tried {d} of {n} instincts and settings")
        res = E.hindsight_study(closes, extra, self.start_cash, fee, self.settings["trade_pct"] / 100, slip, rng,
                                prior=getattr(t, "hindsight", {}), stop=lambda: self.tourn is not t or not self.cfg.get("roam_on"),
                                progress=prog, risk_caps=t.risk_caps)

        def apply():
            if self.tourn is not t:
                return
            t.learn_hindsight(res)
            ok = [r for r in res["results"] if r["ok"]]
            grad = ""
            if ok and self.seeded and not t.nursery_full() and not info.get("revisit"):  # (when the farm is full of young eggs the lesson waits in the book)
                g = ok[0]["genes"]
                v = t.add_variant(g["name"], g["params"], {c: self.hist[c] for c in self.session_coins}, 1.0,
                                  note="Hindsight egg:", tf=g["tf"], origin="hindsight", risk=g["risk"], traits=g["traits"],
                                  vetoes=g["vetoes"], layers=g["layers"])
                if v:
                    v.hs_src = g["name"]  # graded later on days it didn't look back at
                    grad = E.bot_nickname(v.key())
                    self.refresh_board()
            rows = [(STRAT_LABELS.get(r["genes"]["name"], r["genes"]["name"]) + " (" + ", ".join(f"{k}={x}" for k, x in r["genes"]["params"].items()) + ")",
                     round(r["check_ret"] * 100, 2), r["closed"], round(r["check_ret"] * 100, 2), r["closed"], r["wins"],
                     r["best"], r["worst"]) for r in res["results"][:6]]
            rec = dict(time=time.time(), kind="Hindsight", revisit=bool(info.get("revisit")), start=info["start"], end=info["end"], gran=info["gran"], bars=info["bars"],
                       hold=round(info["hold"] * 100, 2), mood=info["mood"], results=rows, passed=len(ok), graduate=grad,
                       summary=f"tried {res['tried']} instincts and settings; {len(ok)} would have worked (on prices they "
                               f"hadn't seen too)" + (f"; {grad} hatched from the best" if grad else ""))
            self._roam_finish(rec, cur)
        self.q.put(("call", apply))

    def _roam_scalp(self, t):
        """Scalp roam: your best scalpers, plus a few new ones, try a whole trading day on 1-minute prices across many
        tickers (with VIXY and SPY for their volatility gates). While the market is open that day is today, live;
        otherwise a random past day the scalpers haven't studied yet. Lessons are saved; a new scalper that clearly
        beats yours joins your farm."""
        rng = random.Random()
        live = False
        try:
            # while the market is open, half the roams watch today live and half replay a past day on real prices
            live = bool(self.client) and E.market_clock()["is_open"] and rng.random() < 0.5
        except Exception:
            E.note_error("_roam_scalp")
        uni = self.scan_universe()
        tickers = list(dict.fromkeys(list(self.session_coins) + rng.sample(uni, min(len(uni), 14))))[:16]
        covered = {r.get("day") for r in self.roam_log if r.get("kind") == "Scalp roam" and not r.get("live")}
        covered |= self.roam_days
        earliest = max(E.HIST_START, datetime(int(self.settings.get("roam_from", 2016)), 1, 4).timestamp())
        # every third replay is an exam on a held-back day (one in five, never trained on); the others work through
        # the training days in order, oldest first, so every day gets its turn
        self.roam_scalp_n = getattr(self, "roam_scalp_n", 0) + 1
        exam = not live and self.roam_scalp_n % 3 == 0 and bool(self.scan_genes())
        days = [d for d in E.trading_days(earliest, time.time() - 86400) if d.isoformat() not in self.roam_dead]
        if live:
            order = [E.ny_date(time.time())]
        elif exam:
            order = [d for d in days if E.is_exam_day(d)]
            rng.shuffle(order)
        else:
            order = [d for d in days if not E.is_exam_day(d) and d.isoformat() not in covered]
            rng.shuffle(order)
        self.roam_now = dict(kind="Scalp roam", phase="download", gran=60, coins=tickers, start=None, end=None,
                             text="Choosing a trading day for the scalpers", t0=time.time(), done=0, total=1, exam=exam)
        cur = self.roam_now
        day = None
        for d in order[:8]:
            day0 = E.ny_time(d.isoformat(), 9, 30)
            close = day0 + 390 * 60
            bars = {}
            got_any = False
            for i, sym in enumerate(tickers + ["VIXY", "SPY"]):
                self.roam_check_on(t)
                cur.update(start=day0, end=close, text=f"Loading 1-minute prices for {d:%b %d %Y}: {sym} ({i + 1} of {len(tickers) + 2})")
                try:
                    x = (E.fetch_ohlcv(sym, 60, 470, self.quote) if live
                         else E.fetch_range(sym, 60, day0 - 4 * 86400, close + 60, self.quote))
                except Exception:
                    continue
                got_any = got_any or bool(x.get("t"))
                keep = [j for j, tt in enumerate(x.get("t") or []) if tt <= close + 60][-470:]
                if sum(1 for j in keep if x["t"][j] >= day0) >= (20 if live else 200):  # it really traded that day
                    bars[sym] = {k: [x[k][j] for j in keep] for k in ("c", "h", "l", "v", "t")}
            if len([c for c in bars if c not in ("VIXY",)]) >= 4:
                day = d
                if not live and not exam:  # taken: the next replay moves on to the next day
                    self.roam_days.add(d.isoformat())
                break
            if live:
                break
            if got_any and not bars:  # prices came back but nobody traded: a holiday, never ask again
                self.roam_dead.add(d.isoformat())
        if day is None:
            raise E.NoHistory("no trading day with enough 1-minute prices for the scalp roam")
        ctx = {k: dict(c=bars[k]["c"], t=bars[k]["t"]) for k in ("VIXY", "SPY") if k in bars}
        if live and E.ivx_today():
            ctx[E.IVX_KEY] = dict(c=[x[1] for x in E.ivx_today()], t=[x[0] for x in E.ivx_today()])
        trade = {c: b for c, b in bars.items() if c in tickers}
        genes = self.scan_genes()
        kamis = [v for v, _ in t.ranking() if E.is_kami(v)][:3] or t.kamikazes()[:3]  # kamikazes try the day too
        genes += [dict(E._genes(v), uid=v.uid) for v in kamis]
        ranked = [v for v, _ in t.ranking() if E.is_scalp(v.name)]
        newbies = []
        for j in range(0 if exam else 4):  # a few new scalpers try out too: two tweaked from your best, two brand new
            par = (ranked[0] if j == 0 else rng.choice(ranked[:4])) if ranked and j < 2 else None
            nv = (t._new(par.name, t.mutate(par.name, par.params, 0.3), t.mutate_tf(par.tf), t.mutate_risk(par.risk),
                         t.mutate_traits(par.traits), dict(par.vetoes), json.loads(json.dumps(par.layers)))
                  if par else t._rand_scalper())
            newbies.append(dict(E._genes(nv), uid=-(j + 1)))
        allg = genes + newbies
        who = {g["uid"]: E.bot_nickname(E.bot_key(g)) for g in allg}
        day0 = E.ny_time(day.isoformat(), 9, 30)
        close = day0 + 390 * 60
        on_day = {c: [p for p, tt in zip(b["c"], b["t"]) if tt >= day0] for c, b in trade.items()}
        on_day = {c: p for c, p in on_day.items() if len(p) > 2}
        n_ = min(len(p) for p in on_day.values())
        step = max(1, n_ // 300)
        prev = [sum(p[i] / p[0] for p in on_day.values()) / len(on_day) for i in range(0, n_, step)]
        t0_ = next(iter(trade.values()))["t"]
        tday = [tt for tt in t0_ if tt >= day0]
        hold = sum(p[-1] / p[0] for p in on_day.values()) / len(on_day) - 1
        mood = E.market_mood(on_day.get("SPY") or next(iter(on_day.values())))[0]
        cur.update(phase="study", start=day0, end=min(close, time.time()), bars=n_, preview=prev,
                   preview_t=[tday[i] for i in range(0, min(n_, len(tday)), step)], bots=[who[g["uid"]] for g in allg],
                   total=1, done=0, text=f"{len(allg)} scalpers are trading {len(trade)} tickers through {day:%a %b %d %Y}"
                                         + (" (today, live)" if live else ""))
        self.roam_feed.append(dict(kind="sep", text=("Exam day" if exam else "Day replay") + f" on {day:%b %d %Y}" + (" (today)" if live else "")
                                                    + f": {len(allg)} scalpers x {len(trade)} tickers (1-minute candles)"))
        job = dict(bars=trade, genes=allg, day=day.isoformat(), ctx=ctx, feed=True, sspread=E.SCALP_SPREAD[0],
                   slip=E.cost_model(self.settings)[1])
        res = self._scan_run(job)
        self.roam_check_on(t)
        for x in sorted(res.get("feed") or [], key=lambda x: x["t"]):  # its trades scroll by in the live list
            self._roam_feed(who.get(x["uid"], x["who"]), x["c"], dict(x), x["t"], "scalp")

        def apply():
            if self.tourn is not t:
                return
            if live:
                self.scan_done(res)  # today's live results also fill the Scalp scan tab
            by_uid = {v.uid: v for v in t.vars}
            rows = []
            for g in allg:
                b = res["bots"].get(g["uid"]) or [0, 0, 0.0, 0.0]
                pct = b[2] / b[3] * 100 if b[3] else 0.0
                rows.append((g, b, pct))
                v = by_uid.get(g["uid"]) if g["uid"] > 0 else None
                if v is not None and not live:  # its record across past days (older days slowly count for less)
                    rs = v.roam_scan or [0, 0, 0.0, 0.0]
                    v.roam_scan = [round(rs[0] * 0.9 + b[0], 2), round(rs[1] * 0.9 + b[1], 2), rs[2] * 0.9 + b[2], rs[3] * 0.9 + b[3]]
                    if exam and not E.is_kami(v):  # a held-back day: an exam, and nothing is learned from it
                        if b[0]:
                            E.add_exam(v, "day", pct, b[0], day.isoformat(), 60)
                        continue
                    if b[0] >= 8 and b[2] > 0:  # a scalper that paid on a day it never saw: an elder for future eggs
                        t.elders.append(dict(name=v.name, params=dict(v.params), tf=v.tf, edge=b[2] / b[3]))
                        del t.elders[:-40]
            mine = [(g, b, pct) for g, b, pct in rows if g["uid"] >= 0 and b[0] >= 10]
            best_mine = max((pct for _, _, pct in mine), default=0.0)
            new = sorted(((g, b, pct) for g, b, pct in rows if g["uid"] < 0 and b[0] >= 10 and pct > 0 and pct > best_mine),
                         key=lambda x: -x[2])
            grad = ""
            if not live and not exam:
                self.roam_days.add(day.isoformat())
            if new and self.seeded and len(t.vars) < t.cap + 2 and not exam and not t.nursery_full():
                g = new[0][0]
                v = t.add_variant(g["name"], g["params"], {c: self.hist[c] for c in self.session_coins}, 1.0,
                                  note="Free roam scalp graduate:", tf=g["tf"], origin="free roam", risk=g["risk"],
                                  traits=g["traits"], vetoes=g["vetoes"], layers=g["layers"])
                if v:
                    grad = E.bot_nickname(v.key())
                    b = new[0][1]
                    v.roam_scan = [b[0], b[1], b[2], b[3]]
                    self.refresh_board()
            tot = [sum(b[k] for _, b, _ in rows) for k in range(4)]
            best_t = max(res["tickers"].items(), key=lambda kv: kv[1]["pl"] / kv[1]["paid"] if kv[1]["paid"] else -1e9, default=None)
            top = sorted(rows, key=lambda x: (not x[1][0], -x[2]))[:6]  # bots that traded first
            rec = dict(time=time.time(), kind="Scalp roam", day=day.isoformat(), live=live, exam=exam, start=day0, end=close, gran=60,
                       bars=n_, hold=round(hold * 100, 2), mood=mood, pl=tot[2], paid=tot[3], graduate=grad, passed=1 if grad else 0,
                       results=[(who[g["uid"]], round(pct, 2), int(b[0]), round(pct, 2), int(b[0]), int(b[1]),
                                 b[4] if len(b) > 4 else None, b[5] if len(b) > 5 else None) for g, b, pct in top if b[0]],
                       summary=(("Exam on a held-back day: " if exam else "")
                                + f"{int(tot[0])} scalps, {tot[1] / tot[0] * 100 if tot[0] else 0:.0f}% won, "
                                f"{tot[2] / tot[3] * 100 if tot[3] else 0:+.2f}% on what they paid"
                                + (f"; best ticker {best_t[0]} ({best_t[1]['pl'] / best_t[1]['paid'] * 100:+.1f}%)"
                                   if best_t and best_t[1]["paid"] else "")
                                + (f"; {grad} joined your farm" if grad else "")))
            self._roam_finish(rec, cur)
        self.q.put(("call", apply))

    def _roam_camp(self, t):
        """A practice farm (seeded with the farm's leaders) lives through the chosen stretch; your farm learns its
        lessons and adopts its best graduate."""
        import random as _r
        rng = _r.Random()
        closes, extra, info = self._roam_pick(rng, 700, prefer=self.farm_gran())
        lead = t.active[:4] or t.vars[:4]
        seeds = [E._genes(v) for v in lead]
        self.roam_now = dict(info, kind="Practice farm", phase="study", text="A practice farm is living through this stretch",
                             bots=[E.bot_nickname(v.key()) for v in lead], done=0, total=1, t0=time.time())
        cur = self.roam_now  # this study's own status (a newer study replaces self.roam_now)
        self.roam_feed.append(dict(kind="sep", text=f"Practice farm on {self._when(info['start'], True)} to "
                                                    f"{self._when(info['end'], True)}  ({E.fmt_bar(info['gran'] / 60)} candles)"))
        fee, slip = E.cost_model(self.settings)
        names = {}

        def prog(done, total, text):
            cur.update(done=done, total=total, text=f"Practice farm: {text}")

        def on_trade(v, c, tr, ts):
            nm = names.get(id(v)) or names.setdefault(id(v), E.bot_nickname(v.key()))
            self._roam_feed(nm, c, tr, ts, "farm")
        res = E.train_farm(closes, extra, E.opts_for_bars(E.tournament_opts(self.settings), info["gran"]), cap=20,
                           passes=1, exam_frac=0.25,
                           seed_genes=seeds, start_cash=self.start_cash, fee=fee, pct=self.settings["trade_pct"] / 100,
                           evolve=int(self.settings["evolve_every"]), params=self.settings["params"], progress=prog,
                           stop=lambda: self.tourn is not t or not self.cfg.get("roam_on"), on_trade=on_trade)

        def apply():
            if self.tourn is not t:
                return
            E.teach_from(t, res["tourn"])
            passed = [r for r in res["results"] if r["passed"]]
            grad = ""
            if passed and self.seeded and len(t.vars) < t.cap + 2 and not info.get("revisit"):  # (a revisit only teaches)
                g = passed[0]["genes"]
                v = t.add_variant(g["name"], g["params"], {c: self.hist[c] for c in self.session_coins}, 1.0,
                                  note="Free roam graduate:", tf=g["tf"], origin="free roam", risk=g["risk"],
                                  traits=g["traits"], vetoes=g["vetoes"], layers=g["layers"])
                if v:
                    v.layer_stats = {k: list(x) for k, x in passed[0]["layer_stats"].items()}
                    grad = E.bot_nickname(v.key())
                    self.refresh_board()
            gen = max((v.gen for v in res["tourn"].vars), default=0)
            top = sorted(res["results"], key=lambda r: -((r["exam_edge"] if r["exam_edge"] is not None else r["train_edge"]) or 0))[:5]

            def vs_hold(r):  # (vs holding as money, its own return) on the exam if it sat one, else on training
                if r["exam_ret"] is not None and res.get("exam_hold") is not None:
                    return E.rel_edge(r["exam_ret"], res["exam_hold"]), r["exam_ret"]
                hold_ = r["train_ret"] - r["train_edge"] if r["train_edge"] is not None else None
                return E.rel_edge(r["train_ret"], hold_), r["train_ret"]
            rec = dict(time=time.time(), kind="Practice farm", revisit=bool(info.get("revisit")), start=info["start"], end=info["end"], gran=info["gran"],
                       bars=info["bars"], hold=round(info["hold"] * 100, 2), mood=info["mood"],
                       results=[(E.bot_nickname(r["v"].key()), round(vs_hold(r)[1] * 100, 2), r["trades"],
                                 round(vs_hold(r)[1] * 100, 2), r.get("closed", 0), r.get("wins", 0), r.get("best_trade"),
                                 r.get("worst_trade")) for r in top],
                       passed=len(passed), graduate=grad,
                       summary=f"{gen} generations, {len(passed)} passed the exam" + (f"; {grad} joined your farm" if grad else ""))
            self._roam_finish(rec, cur)
            if False:
                self.write(f"◇ Free roam: a practice farm lived through {self._when(info['start'], True)} to "
                           f"{self._when(info['end'], True)}. {rec['summary']}.")
        self.q.put(("call", apply))

    def toggle_roam(self, on=None):
        on = (not self.cfg.get("roam_on")) if on is None else on
        if on and self.essentials_block("Free roam"):
            return
        self.cfg["roam_on"] = bool(on)
        E.save_json(CONFIG_PATH, self.cfg)
        self._roam_last = 0
        if on and not self.tourn:
            self.write("◇ Free roam mode ON. It starts as soon as there's a farm: press Start once (you can pause it "
                       "straight away) and Free roam keeps learning.", "warn")
        else:
            self.write("◇ Free roam mode ON: the farm is going back in time to learn on its own." if on else
                       "◇ Free roam mode OFF: it stops the study in progress, and nothing learns in the background.", "good" if on else None)
        if hasattr(self, "roam_btn"):
            self.roam_btn.config(text="◇ Free roam: ON" if on else "◇ Free roam")
        self.start_free_roam()

    @staticmethod
    def roam_result(r, x):
        """One Free roam result as a dict: name, ret (the bot's own return %, or the scalps' profit on what they paid),
        trades, closed, wins, best and worst trade (% of what it paid; None when not known). Entries from older versions
        are read too (best / worst trade and wins weren't kept then)."""
        nm, e, tr = x[0], x[1], x[2]
        if len(x) >= 4:
            ret = x[3]
        elif r.get("kind") in ("Self-test", "Practice farm") and r.get("hold") is not None:
            ret = e + r["hold"]  # before 1.16 the stored number was the return minus the market's
        else:
            ret = e
        out = dict(name=nm, ret=ret, trades=tr, closed=None, wins=None, best=None, worst=None)
        if len(x) >= 8:
            out.update(closed=x[4], wins=x[5], best=x[6], worst=x[7])
        return out

    def roam_bots(self):
        """Every bot Free roam has tested, best first: its tests, how many made money, its average return, trades,
        win rate and its single best and worst trade."""
        agg = {}
        for r in getattr(self, "roam_log", []) or []:
            if r.get("kind") == "Hindsight":
                continue
            for x in (self.roam_result(r, x) for x in r.get("results", [])):
                nm = x["name"]
                a = agg.setdefault(nm, dict(name=nm, n=0, made=0, tot=0.0, trades=0, closed=0, wins=0, best=None, worst=None,
                                            last=0, kinds=set()))
                a["n"] += 1
                a["made"] += x["ret"] > 0
                a["tot"] += x["ret"]
                a["trades"] += x["trades"] or 0
                if x["closed"] is not None:
                    a["closed"] += x["closed"]
                    a["wins"] += x["wins"] or 0
                if x["best"] is not None:
                    a["best"] = x["best"] if a["best"] is None else max(a["best"], x["best"])
                    a["worst"] = x["worst"] if a["worst"] is None else min(a["worst"], x["worst"])
                a["last"] = max(a["last"], r["time"])
                a["kinds"].add(r["kind"])
                if r.get("graduate") == nm:
                    a["grad"] = True
        alive = {E.bot_nickname(v.key()) for v in (self.tourn.vars if self.tourn else [])}
        out = []
        for a in agg.values():
            a["avg"] = a["tot"] / a["n"]
            a["farm"] = a["name"] in alive
            out.append(a)
        return sorted(out, key=lambda a: (-(a["avg"] > 0 and a["made"] * 2 >= a["n"]), -a["avg"]))

    def roam_book(self):
        """Everything Free roam has learned, as plain data: its findings per bot, the farm's hindsight book, exams,
        elders, days studied, and the best bots it found (as a bot file other SBF copies can import)."""
        t = self.tourn
        good = [a["name"] for a in self.roam_bots() if a["farm"] and a["avg"] > 0 and a["made"] * 2 >= a["n"]]
        rows = [r for r in (t.board() if t else []) if E.bot_nickname(r["v"].key()) in set(good)]
        data = E.export_bots([dict(r, bar_minutes=self.session_bar) for r in rows], self.version,
                             note="Free roam: made money in at least half its tests") if rows else dict(bots=[])
        data["free_roam"] = dict(
            made=datetime.now().isoformat(timespec="seconds"), studies=getattr(self, "roam_n", 0),
            findings=[{k: (sorted(x) if isinstance(x, set) else x) for k, x in a.items()} for a in self.roam_bots()],
            hindsight=getattr(t, "hindsight", {}) if t else {}, elders=list(getattr(t, "elders", []) if t else []),
            exams={E.bot_nickname(v.key()): dict(verdict=E.exam_verdict(v, t.serial)[0], exams=v.exams)
                   for v in (t.vars if t else []) if v.exams},
            days_trained=sorted(getattr(self, "roam_days", set()) or []), log=list(getattr(self, "roam_log", []))[-150:])
        return data

    def export_roam(self, parent=None):
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(parent=parent, title="Export what Free roam learned", defaultextension=".json",
                                            initialfile=f"SBF-free-roam-{datetime.now():%Y-%m-%d}.json",
                                            filetypes=[("SBF bot file", "*.json")])
        if not path:
            return
        try:
            data = self.roam_book()
            E.save_json(path, data)
            self.write(f"Free roam's lessons saved to {path} ({len(data['bots'])} of its best bots inside; File > Import "
                       "bots... loads them).", "good")
        except Exception as e:
            self._record_error("exporting Free roam")
            messagebox.showerror("Export", f"Couldn't save it: {e}", parent=parent)

    def open_roam(self, parent=None):
        """The Free roam screen: an in-session banner, where in the past it's learning, the trades as they happen,
        and its findings sorted best to worst."""
        if parent is None:  # it lives as a tab in the learning window
            return self.open_hub('learning', 'Free roam')
        self.start_free_roam()
        w = self.roam_win = parent
        w.configure(bg=BG)
        top = tk.Frame(w, bg=BG, padx=12, pady=10)
        top.pack(fill="x")
        tk.Label(top, text="◇ Free roam", bg=BG, fg=TEXT, font=("", 15, "bold")).pack(side="left")
        tog = tk.Button(top, relief="flat", bd=0, padx=16, pady=5, cursor="hand2", font=("", 10, "bold"), fg="white",
                        activeforeground="white")
        tog.pack(side="left", padx=14)
        scalp_var = tk.BooleanVar(value=bool(self.cfg.get("roam_scalp", True)))
        sc_cb = ttk.Checkbutton(top, text="\u26a1 Scalp roams", variable=scalp_var,
                                command=lambda: (self.cfg.__setitem__("roam_scalp", bool(scalp_var.get())),
                                                 E.save_json(CONFIG_PATH, self.cfg)))
        sc_cb.pack(side="left", padx=(0, 12))
        Tip(sc_cb, "Every fourth study, your scalpers (and a few new ones) trade a whole day on 1-minute prices across "
                   "many tickers: today while the market is open, a past day otherwise. Needs scalpers on the farm.")
        state_lbl = tk.Label(top, text="", bg=BG, fg=MUTED, font=("", 9), justify="left", anchor="w")
        state_lbl.pack(side="left", fill="x", expand=True)

        # --- the session banner (animated while it studies)
        ban = tk.Canvas(w, height=78, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        ban.pack(fill="x", padx=12)

        # --- middle: where/what on the left, live trades on the right
        mid = tk.Frame(w, bg=BG)
        mid.pack(fill="x", padx=12, pady=(8, 0))
        left = tk.Frame(mid, bg=BG)
        left.pack(side="left", fill="both", expand=True)
        right = tk.Frame(mid, bg=BG, width=470)
        right.pack(side="right", fill="y", padx=(10, 0))
        right.pack_propagate(False)
        tk.Label(left, text="WHERE IT IS LEARNING  (2016 to now; blue = already studied, orange = right now)",
                 bg=BG, fg=ACCENT, font=("", 8, "bold"), anchor="w").pack(fill="x")
        tl = tk.Canvas(left, height=70, bg=CARD, highlightthickness=0)
        tl.pack(fill="x")
        tk.Label(left, text="THE STRETCH BEING STUDIED  (average of your tickers; orange ticks = big news days)",
                 bg=BG, fg=ACCENT, font=("", 8, "bold"), anchor="w").pack(fill="x", pady=(8, 0))
        pc = tk.Canvas(left, height=150, bg=CARD, highlightthickness=0)
        pc.pack(fill="x")
        feed_h = tk.Label(right, text="LIVE TRADES", bg=BG, fg=ACCENT, font=("", 8, "bold"), anchor="w")
        feed_h.pack(fill="x")
        ff = tk.Frame(right, bg=BG)
        ff.pack(fill="both", expand=True)
        feed = ttk.Treeview(ff, columns=("when", "who", "what", "res"), show="headings", height=9)
        for c_, lab, wd, an in (("when", "Market time", 112, "w"), ("who", "Bot", 110, "w"), ("what", "Trade", 150, "w"),
                                ("res", "Result", 70, "e")):
            feed.heading(c_, text=lab)
            feed.column(c_, width=wd, anchor=an, stretch=c_ == "what")
        fsb = ttk.Scrollbar(ff, orient="vertical", command=feed.yview)
        feed.configure(yscrollcommand=fsb.set)
        fsb.pack(side="right", fill="y")
        feed.pack(side="left", fill="both", expand=True)
        feed.tag_configure("win", foreground=GREEN)
        feed.tag_configure("loss", foreground=RED)
        feed.tag_configure("open", foreground=TEXT)
        feed.tag_configure("sep", foreground=ACCENT)

        stats = tk.Label(w, text="", bg=BG, fg=TEXT, anchor="w", justify="left", padx=12, font=("", 9))
        stats.pack(fill="x", pady=(8, 0))

        # --- results, organised
        nb = ttk.Notebook(w)
        nb.pack(fill="both", expand=True, padx=12, pady=(4, 10))
        t1 = tk.Frame(nb, bg=BG)
        t2 = tk.Frame(nb, bg=BG)
        t3 = tk.Frame(nb, bg=BG)
        nb.add(t1, text="  Bots it tested: best to worst  ")
        nb.add(t2, text="  Study log  ")
        nb.add(t3, text="  What the results do  ")
        t4 = tk.Frame(nb, bg=BG)
        nb.add(t4, text="  What learning is worth  ")
        fill_feats = self.build_feature_report(t4)
        brow = tk.Frame(t1, bg=BG)
        brow.pack(fill="x", pady=(4, 4))

        def catch_picked():
            names = [bots.item(i, "values")[0] for i in bots.selection()]
            if not names:  # nothing picked: the green ones (made money in at least half their tests) still on the farm
                names = [a["name"] for a in self.roam_bots() if a["farm"] and a["avg"] > 0 and a["made"] * 2 >= a["n"]]
            rows = [r for r in (self.tourn.board() if self.tourn else []) if E.bot_nickname(r["v"].key()) in set(names)]
            if not rows:
                return messagebox.showinfo("Catch", "None of those bots are on your farm any more (only bots still on the "
                                                    "farm can be caught). Pick one marked 'on your farm'.", parent=w)
            caught = self.catch_rows(rows, "Free roam")
            messagebox.showinfo("Catch", f"{len(caught) or len(rows)} bot(s) saved to your Library.", parent=w)
        ttk.Button(brow, text="Catch to Library (the picked bots, or every green one on your farm)",
                   command=catch_picked).pack(side="left")
        ttk.Button(brow, text="Export what Free roam learned...", command=lambda: self.export_roam(w)).pack(side="left", padx=8)
        bots = self.make_tree(t1, ("who", "n", "wins", "avg", "best", "worst", "mode", "why"),
                              (130, 50, 90, 90, 70, 70, 90, 420),
                              ("Bot", "Tests", "Made money", "Avg return", "Trades", "Win rate", "Best trade", "Worst trade", "Now",
                               "What it means"))
        bots.tag_configure("good", foreground=GREEN)
        bots.tag_configure("mixed", foreground="#f59e0b")
        bots.tag_configure("bad", foreground=RED)
        fr = tk.Frame(t2, bg=BG)
        fr.pack(fill="x", pady=(4, 2))
        tk.Label(fr, text="Show", bg=BG, fg=MUTED).pack(side="left")
        flt = tk.StringVar(value="All")
        ttk.Combobox(fr, textvariable=flt, values=["All", "Good finds", "Bad results", "Self-tests", "Practice farms", "Scalp roams", "Hindsight",
                                                 "Feature tests"],
                     state="readonly", width=14).pack(side="left", padx=6)
        lb = tk.Frame(t2, bg=BG)
        lb.pack(fill="both", expand=True)
        log = self.make_tree(lb, ("time", "kind", "strategy", "candles", "status", "why"), (100, 95, 230, 60, 200, 420),
                             ("Studied", "Study", "Stretch of the past", "Candles", "Market then", "Result"))
        log.tag_configure("good", foreground=GREEN)
        log.tag_configure("bad", foreground=RED)
        log.tag_configure("mixed", foreground="#f59e0b")
        tk.Label(t3, bg=BG, fg=TEXT, justify="left", anchor="nw", wraplength=1100, padx=8, pady=8, font=("", 9), text=(
            "Free roam never trades your account. It works on your farm's bots and memory:\n\n"
            "SELF-TEST  A few of your bots (always including the leader) trade a stretch of the past on their own.\n"
            "   • Did well (with 4+ trades): the bot becomes an ELDER, so new eggs are more likely to copy it, and "
            "its rules gain evidence and spread to its children.\n"
            "   • Did badly: nothing is deleted, but its costly rules and the tickers it struggles with are "
            "written into its memory, so its children drop those rules, and the farm's post-trade review learns which "
            "never-rules would have stopped its losing buys.\n"
            "   • Every result is also kept in the bot's memory (its card shows its tests).\n\n"
            "PRACTICE FARM  A small farm, started from your leaders, lives through a stretch, breeding as it goes, then "
            "sits an exam on the last quarter it never trained on.\n"
            "   • Bots that do well in training AND the exam pass. The best one joins your farm as a 'Free roam "
            "graduate' (it still has to prove itself on live prices).\n"
            "   • Your farm also learns the practice farm's memory: its elders, season champions, post-trade review and "
            "which kinds of mutation made winners.\n\n"
            "DAY REPLAY / SCALP ROAM  (\u26a1, every fourth study when your farm has scalpers or kamikazes)  Your best "
            "scalpers, four new ones (two tweaked from your best, two brand new) and your best kamikazes trade a whole day "
            "on 1-minute prices across up to 10 tickers, with VIXY and SPY for their volatility gates. While the market is "
            "open the day is today; otherwise it's a past day they haven't studied. Every fill pays half the bid/ask gap "
            "(Settings > Orders > Spread), as a real share order would.\n"
            "   • Each scalper's results across past days build up (older days count a little less): ones that keep paying "
            "on tickers and days they never saw get a bigger say on your farm, ones that don't get less.\n"
            "   • A scalper with 8+ winning scalps on a day becomes an ELDER, nudging future scalp eggs.\n"
            "   • A new scalper that beats all of yours there (10+ scalps, in profit) joins your farm as a 'Free roam scalp "
            "graduate'. For scalp roams, the % shown is the scalps' profit on what they paid.\n\n"
            "HINDSIGHT  (every fifth study)  Looking back at a stretch, about 50 instincts and settings are tried on its "
            "first 70%; the 12 most promising are then checked on the last 30% they never saw. The ones that made money on "
            "both 'would have worked': they go into your farm's hindsight book, the best one hatches on your farm as a "
            "'Hindsight egg', and from then on most new eggs start from the book instead of at random. What worked before "
            "is tried again, tweaked, so good finds get refined. (Settings > Learning > Hindsight.)\n\n"
            "KAMIKAZES  Throwaway bots with all-or-nothing bets. Each one carries one or two rules the farm knows least about; "
            "when a kamikaze leaves, what its rules did goes into the farm's trial book (shown in Farm knowledge), and the "
            "never-rules that kept saving money become more likely in ordinary eggs.\n\n"
            "THE NUMBERS  Avg return: what a bot made or lost on average per test (for scalp roams, the scalps' profit on "
            "what they paid). Best / Worst trade: its single best and worst trade, as % of what it paid. Win rate: the share "
            "of its trades that made money. Made money: in how many of its tests it ended up.\n\n"
            "HOW TO READ THE 'BEST TO WORST' TAB  Green bots made money in at least half their tests and on average: "
            "they're your strongest candidates for the Hall of Fame. Orange = mixed (could be luck). Red = usually loses "
            "money. 'Now' shows whether the bot is still in your farm. One good test can be luck; look for bots "
            "that win across many different stretches and market moods.")).pack(fill="both", expand=True)

        st = dict(n=-1, flt="", feed=0, phase=0.0, fb=None)

        def paint():
            if not w.winfo_exists():
                return
            try:
                if w.winfo_toplevel().state() != "iconic":
                    self._roam_seen = time.time()
            except tk.TclError:
                pass
            on = bool(self.cfg.get("roam_on"))
            tog.config(text="ON  —  click to stop" if on else "OFF  —  click to start", bg=GREEN if on else "#4b5563",
                       activebackground=GREEN if on else "#4b5563")
            if not self.tourn:
                state_lbl.config(text="There's no farm yet: press Start once so there are bots to study with (pause it "
                                      "straight away if you like). Free roam then keeps learning, even when the market is closed.",
                                 fg="#f59e0b")
            elif on:
                state_lbl.config(text="Studying back to back" + (" (alongside live prices)" if self.running else
                                                                 " (the farm is paused, so it has the computer to itself)"), fg=MUTED)
            else:
                state_lbl.config(text="Off. Nothing studies in the background: Free roam only learns while this is ON.", fg=MUTED)
            cur = self.roam_now
            draw_timeline(cur)
            draw_price(cur)
            fill_feed()
            L_ = self.roam_log
            res = [self.roam_result(r, x) for r in L_ for x in r.get("results", [])]
            cl_ = sum(x["closed"] or 0 for x in res)
            wn_ = sum(x["wins"] or 0 for x in res)
            grads = [r["graduate"] for r in L_ if r.get("graduate")]
            stats.config(text=(f"Studies: {getattr(self, 'roam_n', 0)}   |   past covered: {covered_days():.0f} days   |   "
                               f"bot tests: {len(res)} ({sum(1 for x in res if x['ret'] > 0)} made money)   |   "
                               + (f"trades won: {wn_ / cl_ * 100:.0f}%   |   " if cl_ else "") +
                               f"graduates adopted: {len(grads)}   |   trades this session: {getattr(self, 'roam_trades', 0):,}"))
            if len(L_) != st["n"] or flt.get() != st["flt"]:
                st["n"], st["flt"] = len(L_), flt.get()
                fill_log()
                fill_bots()
                fill_feats()
            w.after(500, paint)

        def verdict(r):
            res = r.get("results") or []
            if r["kind"] == "Practice farm":
                return "good" if r.get("passed") else "bad"
            if r["kind"] == "Feature test":
                return "mixed"  # a measurement, not a find
            if r["kind"] == "Scalp roam":
                return "good" if r.get("pl", 0) > 0 else "bad"
            if r["kind"] == "Hindsight":
                return "good" if r.get("passed") else "bad"
            good = sum(1 for x in res if self.roam_result(r, x)["ret"] > 0)
            return "good" if good * 2 > len(res) else "bad" if good == 0 else "mixed"

        def fill_log():
            log.delete(*log.get_children())
            f_ = flt.get()
            for r in reversed(self.roam_log[-300:]):
                v_ = verdict(r)
                if (f_ == "Good finds" and v_ != "good") or (f_ == "Bad results" and v_ != "bad") or \
                        (f_ == "Self-tests" and r["kind"] != "Self-test") or (f_ == "Practice farms" and r["kind"] != "Practice farm") or \
                        (f_ == "Scalp roams" and r["kind"] != "Scalp roam") or (f_ == "Hindsight" and r["kind"] != "Hindsight") or \
                        (f_ == "Feature tests" and r["kind"] != "Feature test"):
                    continue
                ex = r["summary"] + (":  " + ",  ".join(f"{x['name']} {x['ret']:+.1f}%" for x in (self.roam_result(r, x) for x in r.get("results", [])[:4]))
                                     if r.get("results") else "")
                log.insert("", "end", tags=(v_,), values=(
                    datetime.fromtimestamp(r["time"]).strftime("%b %d %H:%M"), r["kind"] + (" (again)" if r.get("revisit") else ""),
                    f"{self._when(r['start'], True)} to {self._when(r['end'], True)}", E.fmt_bar(r["gran"] / 60),
                    f"{MOOD_TEXT.get(r['mood'], r['mood'])} ({r['hold']:+.1f}%)", ex))

        def fill_bots():
            bots.delete(*bots.get_children())
            for a in self.roam_bots():
                good = a["avg"] > 0 and a["made"] * 2 >= a["n"]
                tag = "good" if good else "bad" if a["made"] == 0 or a["avg"] < -1 else "mixed"
                means = ("Strong: makes money across many stretches. A Hall of Fame candidate." if good and a["n"] >= 3 else
                         "Promising: made money, but needs more tests to rule out luck." if good else
                         "Mixed: makes money sometimes. Its weak rules are being weeded out." if tag == "mixed" else
                         "Usually loses money. Its costly rules fade in its children.")
                if a.get("grad"):
                    means = "Graduate. " + means
                bots.insert("", "end", tags=(tag,), values=(
                    a["name"], a["n"], f"{a['made']} of {a['n']}", f"{a['avg']:+.1f}%", a["trades"],
                    f"{a['wins'] / a['closed'] * 100:.0f}%" if a["closed"] else "-",
                    "-" if a["best"] is None else f"{a['best']:+.1f}%", "-" if a["worst"] is None else f"{a['worst']:+.1f}%",
                    "in farm" if a["farm"] else "gone", means))

        def fill_feed():
            items = list(self.roam_feed)
            n = getattr(self, "roam_trades", 0) + len(items)
            if n == st["feed"]:
                return
            st["feed"] = n
            feed.delete(*feed.get_children())
            for x in reversed(items[-150:]):
                if x.get("kind") == "sep":
                    feed.insert("", "end", tags=("sep",), values=("", "", x["text"], ""))
                    continue
                pl = x.get("pl")
                if x["side"] == "buy":
                    res_, tag = "opened", "open"
                else:
                    paid = (x.get("usd") or 0) - (pl or 0)
                    res_ = f"{(pl / paid * 100 if paid > 0 else 0):+.0f}%" if pl is not None else "closed"
                    tag = "win" if (pl or 0) > 0 else "loss"
                feed.insert("", "end", tags=(tag,), values=(
                    datetime.fromtimestamp(x["t"]).strftime("%b %d %Y %H:%M") if x.get("t") else "",
                    x["who"], ("buy " if x["side"] == "buy" else "sell ") + x["what"], res_))

        def animate():
            """The session banner: a spinning ring and a sweeping beam while it studies."""
            if not w.winfo_exists():
                return
            ban.delete("all")
            W, H = ban.winfo_width(), ban.winfo_height()
            cur = self.roam_now
            err = self.roam_err
            on = bool(self.cfg.get("roam_on"))
            st["phase"] = (st["phase"] + 0.06) % 1.0
            ph = st["phase"]
            if cur and cur.get("phase") == "study":
                colr, big = "#f59e0b", "IN SESSION"
            elif cur:
                colr, big = ACCENT, "GETTING READY"
            elif err and time.time() - err[0] < 120:
                colr, big = RED, "PROBLEM"
            elif self.tourn and on:
                colr, big = MUTED, "RESTING"
            else:
                colr, big = MUTED, "OFF" if not self.tourn or not on else "WAITING"
            cx, cy, R = 40, H // 2, 22
            ban.create_oval(cx - R, cy - R, cx + R, cy + R, outline=LINE, width=4)
            if cur:
                ban.create_arc(cx - R, cy - R, cx + R, cy + R, start=-ph * 360, extent=110, style="arc", outline=colr, width=4)
                ban.create_arc(cx - R, cy - R, cx + R, cy + R, start=-ph * 360 + 180, extent=50, style="arc", outline=colr, width=4)
                r2 = 6 + 3 * math.sin(ph * 2 * math.pi)
                ban.create_oval(cx - r2, cy - r2, cx + r2, cy + r2, fill=colr, outline="")
            else:
                ban.create_oval(cx - 6, cy - 6, cx + 6, cy + 6, fill=colr, outline="")
            ban.create_text(76, 18, anchor="w", text=big, fill=colr, font=("", 14, "bold"))
            if cur:
                if cur.get("start"):
                    sub = (f"{cur.get('kind', '')}:  {self._when(cur['start'], True)}  to  {self._when(cur['end'], True)}"
                           f"   ·   {E.fmt_bar(cur['gran'] / 60)} candles" + (f"   ·   {cur['bars']:,} of them" if cur.get("bars") else ""))
                else:
                    sub = cur.get("kind", "")
                ban.create_text(220, 18, anchor="w", text=sub, fill=TEXT, font=("", 10, "bold"))
                ban.create_text(76, 42, anchor="w", text=cur.get("text", "") + f"   ·   {int(time.time() - cur.get('t0', time.time()))} s",
                                fill=MUTED, font=("", 9))
                x0, x1, y = 76, W - 16, 62  # progress track with a sweeping highlight
                ban.create_rectangle(x0, y - 3, x1, y + 3, fill=BG, outline="")
                if cur.get("phase") == "study":
                    frac = min(1.0, cur.get("done", 0) / max(1, cur.get("total", 1)))
                    ban.create_rectangle(x0, y - 3, x0 + (x1 - x0) * frac, y + 3, fill=colr, outline="")
                bx = x0 + (x1 - x0) * ph
                ban.create_rectangle(max(x0, bx - 40), y - 3, min(x1, bx + 40), y + 3, fill="#fde68a" if cur.get("phase") == "study" else "#93c5fd",
                                     outline="", stipple="gray50")
                if cur.get("bots"):
                    ban.create_text(W - 16, 42, anchor="e", text="Bots: " + ", ".join(cur["bots"][:5]), fill=MUTED, font=("", 8))
            elif err and time.time() - err[0] < 120:
                ban.create_text(220, 18, anchor="w", text=err[1][:150], fill=RED, font=("", 9, "bold"))
                ban.create_text(76, 46, anchor="w", text="It tries again in about a minute. Details are in the activity log "
                                                         "and crash.log.", fill=MUTED, font=("", 9))
            elif big == "RESTING":
                left_s = max(0, int(self.roam_wait() - (time.time() - getattr(self, "_roam_last", 0))))
                ban.create_text(220, 18, anchor="w", text=(f"Next study in about {left_s} s" if left_s > 2 else "Starting the next study..."),
                                fill=TEXT, font=("", 10))
                ban.create_text(76, 46, anchor="w", text="Saving what it learned before the next study.", fill=MUTED, font=("", 9))
            else:
                ban.create_text(220, 18, anchor="w", text="Not studying right now.", fill=TEXT, font=("", 10))
                ban.create_text(76, 46, anchor="w", text=state_lbl.cget("text")[:160], fill=MUTED, font=("", 9))
            w.after(70 if cur else 400, animate)

        def covered_days():
            iv = sorted((r["start"], r["end"]) for r in self.roam_log)
            tot, cs, ce = 0.0, None, None
            for a, b in iv:
                if ce is None or a > ce:
                    if ce is not None:
                        tot += ce - cs
                    cs, ce = a, b
                else:
                    ce = max(ce, b)
            if ce is not None:
                tot += ce - cs
            return tot / 86400

        def draw_timeline(cur):
            tl.delete("all")
            W, H = tl.winfo_width(), tl.winfo_height()
            now = time.time()
            oldest = max(E.HIST_START, datetime(int(self.settings.get("roam_from", 2016)), 1, 4).timestamp())
            X = lambda ts: 10 + (max(ts, oldest) - oldest) / max(1, now - oldest) * (W - 20)
            tl.create_rectangle(10, 22, W - 10, 42, fill=BG, outline=LINE)
            for r in self.roam_log:
                tl.create_rectangle(X(r["start"]), 23, max(X(r["start"]) + 2, X(r["end"])), 41,
                                    fill="#1d4ed8" if DARK else "#93c5fd", outline="")
            if cur and cur.get("start"):
                x0, x1 = X(cur["start"]), max(X(cur["start"]) + 3, X(cur["end"]))
                tl.create_rectangle(x0, 18, x1, 46, fill="#f59e0b", outline="")
                tl.create_text(min(max((x0 + x1) / 2, 50), W - 50), 9, text="studying now", fill="#f59e0b", font=("", 8, "bold"))
            y = datetime.fromtimestamp(oldest).year
            while True:
                ts = datetime(y, 1, 1).timestamp()
                if ts > now:
                    break
                if ts >= oldest:
                    tl.create_line(X(ts), 42, X(ts), 48, fill=MUTED)
                    tl.create_text(X(ts), 58, text=str(y), fill=MUTED, font=("", 7))
                y += 1
            tl.create_text(W - 12, 58, text="now", anchor="e", fill=MUTED, font=("", 7))

        def draw_price(cur):
            pc.delete("all")
            W, H = pc.winfo_width(), pc.winfo_height()
            held = False
            if cur and cur.get("preview"):
                st["last"] = dict(cur)
            elif st.get("last"):  # keep the last study's chart up until the next one is ready (no flashing)
                cur, held = st["last"], True
            if not cur or not cur.get("preview"):
                pc.create_text(W // 2, H // 2, text="The stretch appears here while the farm studies it.", fill=MUTED)
                return
            pv, pt = cur["preview"], cur.get("preview_t") or []
            lo, hi = min(pv), max(pv)
            pad = (hi - lo) * 0.1 or 0.01
            lo, hi = lo - pad, hi + pad
            X = lambda i: 50 + i * (W - 60) / max(1, len(pv) - 1)
            Y = lambda y: H - 18 - (y - lo) / (hi - lo) * (H - 34)
            pc.create_line(50, Y(1.0), W - 10, Y(1.0), fill=LINE, dash=(3, 3))
            if pt and E.events():
                for kind in E.BIG_EVENTS:
                    for et in E.events().get(kind) or []:
                        if pt[0] <= et <= pt[-1]:
                            x = X(bisect.bisect_left(pt, et))
                            pc.create_line(x, H - 16, x, H - 10, fill="#f59e0b", width=2)
            pc.create_line(*[z for i, y in enumerate(pv) for z in (X(i), Y(y))], fill=GREEN if pv[-1] >= 1 else RED, width=2)
            for y in (lo + pad, hi - pad):
                pc.create_text(46, Y(y), text=f"{(y - 1) * 100:+.1f}%", anchor="e", fill=MUTED, font=("", 7))
            pc.create_text(52, 6, anchor="nw", text=self._when(cur["start"], True), fill=MUTED, font=("", 7))
            pc.create_text(W - 12, 6, anchor="ne", text=self._when(cur["end"], True), fill=MUTED, font=("", 7))
            if held:
                pc.create_text(W // 2, 6, anchor="n", text="last study (the next one appears here when it's ready)",
                               fill=MUTED, font=("", 7, "italic"))
            elif cur.get("phase") == "study" and cur.get("total"):
                frac = cur.get("done", 0) / max(1, cur["total"])
                if cur.get("kind") == "Practice farm":
                    frac = min(1.0, frac / 0.75)
                    lab = "the practice farm is here"
                else:
                    d_ = cur.get("done", 0)
                    frac = d_ - int(d_)
                    lab = "this bot is here"
                x = X(int((len(pv) - 1) * min(1.0, frac)))
                pc.create_line(x, 4, x, H - 4, fill="#f59e0b", width=2)
                pc.create_text(x + 4, H - 4, anchor="sw", text=lab, fill="#f59e0b", font=("", 7, "bold"))

        tog.config(command=lambda: (self.toggle_roam(), paint()))
        paint()
        animate()

    def slow_path(self):
        return os.path.join(E.HOME_DIR, "farm-slow.json")

    def build_slow_tab(self, parent):
        self.slow = dict(t=None, wallet=None, curve=[], hist={}, hx={}, last_ts=0, busy=False, coins=[], status="")
        top = tk.Frame(parent, bg=BG)
        top.pack(fill="x", padx=8, pady=(8, 2))
        self.slow_btn = ttk.Button(top, text="▶ Start the slow farm", style="Accent.TButton", command=self.slow_toggle)
        self.slow_btn.pack(side="left")
        tb = ttk.Button(top, text="⚔ Train it on the past", style="Plain.TButton", command=self.slow_train)
        tb.pack(side="left", padx=6)
        Tip(tb, "Sends the slow farm through its months of hourly history twice more (keeping what it learned), then "
                "tests the best on the most recent weeks. Graduates join it.")
        for txt, cmd in (("◉ Save selected to Library", self.slow_catch), ("★ Add selected to Hall of Fame", self.slow_hof),
                         ("Reset slow farm", self.slow_reset)):
            ttk.Button(top, text=txt, style="Plain.TButton", command=cmd).pack(side="left", padx=(0, 6))
        self.slow_trade_var = tk.BooleanVar(value=bool(self.settings.get("slow_trades_account", False)))
        cb = ttk.Checkbutton(top, text="Let it trade my account too", variable=self.slow_trade_var, command=self._slow_trade_setting)
        cb.pack(side="right")
        Tip(cb, "Off: the slow farm only uses its own pretend wallet. On: when its winners agree on a new hourly candle, "
                "your account follows them too (while the bot is running), with the reason 'slow farm'.")
        tk.Label(parent, bg=BG, fg=MUTED, anchor="w", justify="left", padx=10, wraplength=1000, text=(
            "A second, separate farm of slow dragons. They decide on hourly candles, from every hour up to every two and a "
            "half days, hold for days to weeks, and can aim for much bigger moves (targets up to 100%, stops up to 30%), so "
            "fees matter far less. They learn from about four months of history first, then carry on with each new hour. "
            "They never mix with your fast farm, and only trade your account if you tick the box.")).pack(fill="x")
        self.slow_lbl = tk.Label(parent, text="", bg=CARD, fg=TEXT, anchor="w", justify="left", padx=10, pady=5, font=("", 9, "bold"))
        self.slow_lbl.pack(fill="x", padx=8, pady=(6, 4))
        self.slow_cv = tk.Canvas(parent, height=150, bg=CARD, highlightthickness=0)
        self.slow_cv.pack(fill="x", padx=8)
        self.slow_cv.bind("<Configure>", lambda e: self.later(self.slow_draw, 200))
        box = tk.Frame(parent, bg=BG)
        box.pack(fill="both", expand=True, padx=8, pady=(6, 6))
        self.slow_tree = self.make_tree(box, ("name", "strategy", "pace", "status", "recent", "edge", "life", "trades", "holds", "stops"),
                                        (130, 170, 90, 120, 70, 80, 80, 55, 90, 190),
                                        ("Name", "Strategy", "Decides", "Status", "Recent", "vs the market", "Lifetime", "Trades",
                                         "Typical hold", "Own stop / target"))
        self.slow_tree.configure(selectmode="extended")
        for tag, col in (("win", GREEN), ("shaky", "#f59e0b"), ("stopped", MUTED), ("learning", MUTED)):
            self.slow_tree.tag_configure(tag, foreground=col)
        if self.cfg.get("slow_on"):
            self.root.after(6000, lambda: self.slow_start(quiet=True))
        self.slow_refresh()

    def _slow_trade_setting(self):
        self.settings["slow_trades_account"] = bool(self.slow_trade_var.get())
        self.cfg["settings"] = self.settings
        E.save_json(CONFIG_PATH, self.cfg)

    def slow_toggle(self):
        if self.cfg.get("slow_on"):
            self.cfg["slow_on"] = False
            E.save_json(CONFIG_PATH, self.cfg)
            self.slow_save()
            self.write("◇ Slow farm paused. It keeps everything it learned.")
            self.slow_refresh()
        else:
            self.slow_start()

    def slow_coins(self):
        return list(self.session_coins or [c for c, v in self.coin_vars.items() if v.get()] or ["SPY", "QQQ"])

    def slow_start(self, quiet=False):
        """Loads (or hatches and warms up) the slow farm, then follows each new hourly candle."""
        if self.cfg.get("essentials") and (quiet or self.essentials_block("The Slow farm")):
            return
        sl = self.slow
        self.cfg["slow_on"] = True
        E.save_json(CONFIG_PATH, self.cfg)
        if sl["busy"]:
            return
        sl["busy"] = True
        sl["status"] = "Loading hourly history..."
        self.slow_refresh()
        saved = E.load_json(self.slow_path(), {}) or {}
        coins = list(saved.get("coins") or []) if sl["t"] is None and saved.get("farm") else (sl["coins"] or self.slow_coins())
        quote = self.cur_quote()
        fee, slip = E.cost_model(self.settings)
        pct, cash = self.settings["trade_pct"] / 100, self.start_cash or 1000.0
        opts = dict(E.tournament_opts(self.settings), risk_caps=dict(E.SLOW_RISK), window=72, scalp_share=0.0)
        cap = int(self.settings.get("slow_size", 30))

        def make():
            return E.Tournament(coins, cash, fee, pct, 24, window=72, params=self.settings["params"], opts=opts, cap=cap,
                                retire_steps=24 * 14, risk_caps=E.SLOW_RISK)

        def work():
            try:
                hist, hx, ts = {}, {}, {}
                for c in coins:
                    d = E.fetch_ohlcv(c, self.SLOW_GRAN, self.SLOW_HISTORY, quote)
                    n = len(d["c"])
                    t = list(d.get("t") or [])
                    if len(t) != n:  # older engine without candle times: assume one per hour up to now
                        top = int(time.time()) // self.SLOW_GRAN * self.SLOW_GRAN
                        t = [top - (n - 1 - i) * self.SLOW_GRAN for i in range(n)]
                    done = [i for i, x in enumerate(t) if x + self.SLOW_GRAN <= time.time()]  # finished candles only
                    k = (done[-1] + 1) if done else n
                    hist[c], ts[c] = d["c"][:k], t[:k]
                    hx[c] = {x: list(d[x][:k]) for x in ("h", "l", "v")}
                    hx[c]["t"] = list(t[:k])
                L = min(len(v) for v in hist.values())
                hist = {c: v[-L:] for c, v in hist.items()}
                hx = {c: {x: y[-L:] for x, y in d.items()} for c, d in hx.items()}
                if self.settings.get("ctx_studies", True):
                    try:
                        self.add_ctx(hx, self.SLOW_GRAN, self.SLOW_HISTORY, quote)
                    except Exception:
                        E.note_error("work")
                last_ts = min(ts[c][-1] for c in coins) if L else 0
                t = sl["t"]
                if t is None and saved.get("farm"):
                    t = make()
                    if not t.restore(saved["farm"]):
                        t = None
                    else:
                        t.xfull = None
                        self.q.put(("log", (f"◇ Slow farm: carrying on with its {len(t.vars)} bots.", "good")))
                if t is None:
                    self.q.put(("log", f"◇ Slow farm: hatching and warming up on {L} hours of history (this can take a minute)..."))
                    sl["status"] = f"Warming up on {L} hours of history..."
                    t = None
                    if self.settings.get("multicore", True) and opts.get("islands", 1) > 1:
                        t = E.warm_islands(coins, cash, fee, pct, 24, self.settings["params"], dict(opts), cap, 24 * 14, hist, hx, workers=self.bg_workers())
                        if t is not None:
                            t.risk_caps = dict(E.SLOW_RISK)
                            t.W = 72
                    if t is None:
                        t = make()
                        t.warmup(hist, extra=hx)
                    last_ts = min(ts[c][-1] for c in coins)
                elif saved.get("last_ts") and saved["last_ts"] < last_ts:  # catch up on the hours it missed
                    miss = [i for i, x in enumerate(ts[coins[0]][-L:]) if x > saved["last_ts"]]
                    for i in miss:
                        t.step({c: E.View(hist[c], i + 1) for c in coins}, E.slice_extra(hx, i + 1))
                t.recording = False

                def done_():
                    sl.update(t=t, hist=hist, hx=hx, last_ts=last_ts, coins=coins, busy=False, status="")
                    if sl["wallet"] is None:
                        w = saved.get("wallet") or {}
                        sl["wallet"] = E.Sim(cash, fee, pct, slip=slip)
                        if w:
                            sl["wallet"].cash = float(w.get("cash", cash))
                            sl["wallet"].pos = {c: float(q) for c, q in (w.get("pos") or {}).items()}
                            sl["wallet"].cost = {c: float(q) for c, q in (w.get("cost") or {}).items()}
                        sl["curve"] = [tuple(x) for x in saved.get("curve") or []]
                        sl["hold0"] = saved.get("hold0") or {c: hist[c][-1] for c in coins}
                    self.slow_save()
                    self.write(f"◇ Slow farm ready: {len(t.vars)} bots on {', '.join(coins)}, deciding on hourly candles.", "good")
                    self.slow_refresh()
                self.q.put(("call", done_))
            except Exception as e:
                def fail(e=e):
                    sl.update(busy=False, status=f"Couldn't start: {e}")
                    self.slow_refresh()
                self.q.put(("call", fail))
        threading.Thread(target=work, daemon=True).start()
        if not getattr(self, "_slow_loop", None):
            self._slow_loop = threading.Thread(target=self._slow_feed, daemon=True)
            self._slow_loop.start()

    def _slow_feed(self):
        """Every few minutes: has a new hourly candle finished? Then the slow farm takes its next step."""
        while True:
            time.sleep(60 if self.slow.get("t") else 15)
            sl = self.slow
            if not self.cfg.get("slow_on") or sl["t"] is None or sl["busy"]:
                continue
            if time.time() < sl["last_ts"] + 2 * self.SLOW_GRAN + 20:
                continue  # the next candle hasn't finished yet
            try:
                new = {}
                for c in sl["coins"]:
                    d = E.fetch_ohlcv(c, self.SLOW_GRAN, 60, self.cur_quote())
                    t = list(d.get("t") or [])
                    if len(t) != len(d["c"]):
                        continue
                    new[c] = [(t[i], d["c"][i], d["h"][i], d["l"][i], d["v"][i]) for i in range(len(t))
                              if t[i] > sl["last_ts"] and t[i] + self.SLOW_GRAN <= time.time()]
                if new and all(new.get(c) for c in sl["coins"]):
                    k = min(len(new[c]) for c in sl["coins"])
                    self.q.put(("call", lambda new=new, k=k: self._slow_step(new, k)))
            except Exception:
                E.note_error("_slow_feed")

    def _slow_step(self, new, k):
        sl, t = self.slow, self.slow["t"]
        if t is None:
            return
        for i in range(k):
            for c in sl["coins"]:
                ts, cl, hi, lo, vol = new[c][i]
                sl["hist"][c].append(cl)
                for x, val in (("h", hi), ("l", lo), ("v", vol), ("t", ts)):
                    sl["hx"][c].setdefault(x, []).append(val)
                del sl["hist"][c][:-6000]
                for x in ("h", "l", "v", "t"):
                    del sl["hx"][c][x][:-6000]
            sl["last_ts"] = new[sl["coins"][0]][i][0]
            cx = sl["hx"].get(E.CTX_KEY)
            if cx:  # the watched tickers move on too (latest known price)
                for sym, q in cx.items():
                    px_ = self.last.get(sym) or (q[-1] if q else None)
                    if px_:
                        q.append(px_)
                        del q[:-6000]
            t.step({c: sl["hist"][c] for c in sl["coins"]}, dict(sl["hx"]))
            px = {c: sl["hist"][c][-1] for c in sl["coins"]}
            for c in sl["coins"]:
                sig, mult = t.vote(c)
                if sig:
                    sl["wallet"].buy(c, px[c], 0, mult) if sig == "buy" else sl["wallet"].sell(c, px[c])
                    if self.settings.get("slow_trades_account") and self.running and self.session and c in self.session_coins \
                            and c not in self.pending:
                        self.pending_detail[c] = "The slow farm's winners agreed on the new hourly candle."
                        (self.buy(c, self.last.get(c, px[c]), "slow farm", mult) if sig == "buy"
                         else self.sell(c, self.last.get(c, px[c]), "slow farm"))
            sl["curve"].append((time.time(), sl["wallet"].equity(px)))
        for e in t.pop_events()[-3:]:
            self.write("◇ Slow farm: " + friendly_text(e[0].lower() + e[1:]))
        self.slow_save()
        self.slow_refresh()

    def slow_save(self):
        sl = self.slow
        if sl.get("t") is None:
            return
        w = sl["wallet"]
        try:
            E.save_json(self.slow_path(), dict(coins=sl["coins"], last_ts=sl["last_ts"], farm=sl["t"].to_state(),
                                              wallet=None if w is None else dict(cash=w.cash, pos=w.pos, cost=w.cost),
                                              curve=sl["curve"][-3000:], hold0=sl.get("hold0")))
        except OSError:
            pass

    def slow_reset(self):
        if not messagebox.askyesno("Reset slow farm", "Clear the slow farm and its wallet? (Your Library and Hall of Fame are kept.)"):
            return
        self.slow.update(t=None, wallet=None, curve=[], hist={}, hx={}, last_ts=0, coins=[], status="")
        try:
            os.remove(self.slow_path())
        except OSError:
            pass
        self.cfg["slow_on"] = False
        E.save_json(CONFIG_PATH, self.cfg)
        self.slow_refresh()
        self.write("◇ Slow farm reset. Start it again to hatch a fresh one on your current coins.")

    def _slow_selected(self):
        t = self.slow.get("t")
        if not t:
            return []
        ids = set(self.slow_tree.selection())
        return [v for v in t.vars if str(id(v)) in ids]

    def _slow_entries(self, vs):
        t = self.slow["t"]
        ids = {id(v) for v in vs}
        rows = [dict(r, bar_minutes=60) for r in t.board() if id(r["v"]) in ids]
        out = E.export_bots(rows, self.version, note="slow farm bot (decides on hourly candles)")["bots"]
        return out

    def slow_catch(self):
        vs = self._slow_selected()
        if not vs:
            return messagebox.showinfo("Slow farm", "Select bots in the list first (Ctrl+click for several).")
        self.catch_rows(self._slow_entries(vs), "slow farm")

    def slow_hof(self):
        vs = self._slow_selected()
        if not vs:
            return messagebox.showinfo("Slow farm", "Select bots in the list first.")
        entries = self._slow_entries(vs)
        self.catch_rows(entries, "slow farm")
        E.induct(entries, self.version)
        self.after_hof_change()
        self.write(f"★ Added {len(entries)} slow-farm bot(s) to the Hall of Fame.", "good")

    def slow_train(self):
        sl = self.slow
        if sl.get("t") is None or sl["busy"]:
            return messagebox.showinfo("Slow farm", "Start the slow farm first (it needs its history loaded).")
        sl["busy"] = True
        sl["status"] = "Training on the past..."
        self.slow_refresh()
        hist = {c: list(v) for c, v in sl["hist"].items()}
        hx = {c: {x: list(y) for x, y in d.items()} for c, d in sl["hx"].items()}
        t = sl["t"]
        seeds = [E._genes(v) for v in t.vars]
        fee, slip = E.cost_model(self.settings)

        def prog(done, total, text):
            sl["status"] = f"Training on the past: {done * 100 // max(1, total)}% ({text})"

        def work():
            try:
                res = E.train_farm(hist, hx, dict(E.tournament_opts(self.settings), risk_caps=dict(E.SLOW_RISK), window=72,
                                                  scalp_share=0.0),
                                   cap=len(t.vars), passes=2, exam_frac=0.2,
                                   seed_genes=seeds, start_cash=t.start_cash, fee=fee, pct=t.pct, evolve=24,
                                   params=self.settings["params"], retire_steps=24 * 14, progress=prog)

                def done_():
                    E.teach_from(t, res["tourn"])
                    n = 0
                    for r in res["results"]:
                        if r["passed"] and len(t.vars) < t.cap + 6:
                            g = r["genes"]
                            v = t.add_variant(g["name"], g["params"], hist, 1.0, note="Slow farm graduate:", tf=g["tf"],
                                              origin="training camp", risk=g["risk"], traits=g["traits"], vetoes=g["vetoes"],
                                              layers=g["layers"])
                            if v:
                                v.layer_stats = {k: list(x) for k, x in r["layer_stats"].items()}
                                n += 1
                    t.cap = max(t.cap, len(t.vars))
                    sl.update(busy=False, status="")
                    self.slow_save()
                    self.write(f"◇ Slow farm trained on {res['train_bars']} hours of history; "
                               f"{sum(1 for r in res['results'] if r['passed'])} passed the exam, {n} joined it.", "good")
                    self.slow_refresh()
                self.q.put(("call", done_))
            except Exception as e:
                self.q.put(("call", lambda e=e: (sl.update(busy=False, status=f"Training stopped: {e}"), self.slow_refresh())))
        threading.Thread(target=work, daemon=True).start()

    def slow_refresh(self):
        if not hasattr(self, "slow_tree"):
            return
        sl, t = self.slow, self.slow.get("t")
        on = bool(self.cfg.get("slow_on"))
        self.slow_btn.config(text="⏸ Pause the slow farm" if on and t else "▶ Start the slow farm")
        try:
            if "Slow farm" not in self.cur_tab():
                return  # redrawn when you open the tab
        except Exception:
            E.note_error("slow_refresh")
        if sl.get("status"):
            txt = sl["status"]
        elif t is None:
            txt = "Not started. Press Start: it hatches on your ticked coins and learns from about four months of hourly history."
        else:
            px = {c: sl["hist"][c][-1] for c in sl["coins"]}
            w = sl["wallet"].equity(px) if sl["wallet"] else t.start_cash
            h0 = sl.get("hold0") or px
            hold = sum(px[c] / h0[c] for c in sl["coins"] if h0.get(c)) / len(sl["coins"]) - 1
            nxt = max(0, int(sl["last_ts"] + 2 * self.SLOW_GRAN - time.time()))
            txt = (f"{'Running' if on else 'Paused'}  |  {len(t.vars)} bots on {', '.join(sl['coins'])}  |  generation "
                   f"{max((v.gen for v in t.vars), default=0)}  |  {len(t.active)} winning  |  last candle "
                   f"{datetime.fromtimestamp(sl['last_ts']).strftime('%b %d %H:%M') if sl['last_ts'] else '-'}"
                   + (f", next in about {nxt // 60} min" if on else "")
                   + f"  |  its wallet ${w:,.2f} ({(w / t.start_cash - 1) * 100:+.1f}%, the market {hold * 100:+.1f}%)")
        self.slow_lbl.config(text=txt)
        self.slow_draw()
        tr = self.slow_tree
        sel = tr.selection()
        tr.delete(*tr.get_children())
        if t is None:
            return
        for r in t.board():
            v, lf = r["v"], r["life"]
            holds = [b - a for a, b in self._hold_spans(v)]  # in hourly candles
            typ = sum(holds) / len(holds) if holds else None
            tr.insert("", "end", iid=str(id(v)), tags=(STATUS_TAG.get(r["status"], "learning"),), values=(
                E.bot_nickname(v.key()), STRAT_LABELS.get(v.name, v.name), E.fmt_bar(v.tf * 60),
                STATUS_TEXT.get(r["status"], r["status"]), pct_txt(r["score"]), pct_txt(r.get("edge")), pct_txt(lf["ret"]),
                lf["trades"], "-" if typ is None else (f"{typ / 24:.1f} days" if typ >= 24 else f"{typ:.0f} h"),
                E.risk_text(v.risk) or "none"))
        keep = [i for i in sel if tr.exists(i)]
        if keep:
            tr.selection_set(keep)

    @staticmethod
    def _hold_spans(v):
        """(bought, sold) decision numbers of the bot's recent round trips."""
        out, open_ = [], {}
        for n, c, side, _ in v.log:
            if side == "buy":
                open_.setdefault(c, n)
            elif c in open_:
                out.append((open_.pop(c), n))
        return out[-20:]

    def slow_draw(self):
        cv, sl = self.slow_cv, self.slow
        cv.delete("all")
        W, H = cv.winfo_width(), cv.winfo_height()
        pts = sl.get("curve") or []
        t = sl.get("t")
        if t is not None and len(pts) < 2:  # nothing live yet: show what its leader did over the history
            lead = (t.active or [v for v, _ in t.ranking()] or [None])[0]
            if lead is not None and len(lead.eq) > 2:
                pts = [(i, x) for i, x in enumerate(lead.eq)]
                cv.create_text(W - 10, 8, anchor="ne", fill=MUTED, font=("", 8),
                               text=f"its leader {E.bot_nickname(lead.key())} over its scoring window (hours)")
        if len(pts) < 2:
            cv.create_text(W // 2, H // 2, fill=MUTED, text="Its wallet appears here as the slow farm trades.")
            return
        ys = [p[1] for p in pts]
        lo, hi = min(ys), max(ys)
        pad = (hi - lo) * 0.1 or 1
        lo, hi = lo - pad, hi + pad
        X = lambda i: 60 + i * (W - 70) / (len(pts) - 1)
        Y = lambda y: H - 12 - (y - lo) / (hi - lo) * (H - 24)
        for k in range(3):
            yv = lo + (hi - lo) * k / 2
            cv.create_line(60, Y(yv), W - 10, Y(yv), fill=LINE)
            cv.create_text(54, Y(yv), text=f"{yv:,.0f}", anchor="e", fill=MUTED, font=("", 8))
        cv.create_line(*[z for i, (_, y) in enumerate(pts) for z in (X(i), Y(y))], fill="#a78bfa", width=2)

    # ---------------- compare ----------------
    def compare_bots(self, a=None, b=None):
        if a is None or b is None:
            sel = self.selected_variants()
            if len(sel) != 2:
                return messagebox.showinfo("Compare", "Select exactly two bots (Ctrl+click on the farm), then compare.")
            a, b = sel
        w = tk.Toplevel(self.root)
        w.title("Compare bots")
        w.configure(bg=BG)
        f = tk.Frame(w, bg=BG, padx=14, pady=10)
        f.pack(fill="both", expand=True)

        def facts(v):
            lf = self.tourn.life(v)
            d = self.tourn.info.get(id(v), {})
            wr = "-" if lf["winrate"] is None else f"{lf['winrate'] * 100:.0f}%"
            return [E.bot_nickname(v.key()), STRAT_LABELS.get(v.name, v.name),
                    ", ".join(f"{k}={x}" for k, x in v.params.items()), f"every {E.fmt_bar(v.tf * self.session_bar)}",
                    STATUS_TEXT.get(d.get("status"), "-"), pct_txt(d.get("score")), pct_txt(lf["ret"]), pct_txt(lf["edge"]),
                    wr, str(lf["trades"]), f"{lf['maxdd'] * 100:.1f}%", f"{self.tourn.health(v) * 100:.0f}% / {v.power:.1f}",
                    E.risk_text(v.risk) or "none", E.traits_text(v.traits) or "none", E.vetoes_text(v.vetoes, v.veto_stats) or "none",
                    E.layers_text(v.layers, v.layer_stats) or "none",
                    "-" if v.twin is None else f"{v.twin * 100:+.1f}%", f"gen {v.gen}, island {v.island + 1}",
                    ", ".join(n for n, _ in self.achievements(v, lf)) or "-"]
        rows = ["Name", "Strategy", "Settings", "Decides", "Status", "Recent", "Lifetime", "vs the market", "Win rate", "Trades",
                "Worst drop", "Health / power", "Own stops", "Traits", "Never-rules", "Studies", "Twin test", "Family", "Badges"]
        fa, fb = facts(a), facts(b)
        for i, name in enumerate(rows):
            tk.Label(f, text=name, bg=BG, fg=MUTED, anchor="nw").grid(row=i, column=0, sticky="nw", pady=2, padx=(0, 12))
            for j, val in enumerate((fa[i], fb[i])):
                diff = fa[i] != fb[i]
                tk.Label(f, text=val, bg=CARD if diff else BG, fg=TEXT, anchor="nw", justify="left", wraplength=330,
                         font=("", 9, "bold" if i == 0 else "normal"), padx=6).grid(row=i, column=1 + j, sticky="nwe", pady=2, padx=2)
        tk.Label(f, text="Highlighted rows are where they differ.", bg=BG, fg=MUTED).grid(row=len(rows), column=0, columnspan=3, sticky="w", pady=(8, 0))

    # ---------------- bloodlines (drawn tree) ----------------
    def open_bloodlines(self, parent=None):
        if parent is None:  # it lives as a tab in the family window
            return self.open_hub('family', 'Bloodlines')
        if not self.tourn:
            return self._nothing(parent, "Bloodlines", "Start the farm first.")
        w = parent
        w.configure(bg=BG)
        tk.Label(w, text="Each row is a generation. Lines run from parent to child; bright = alive, gold = strong line "
                         "(a living descendant beating the market). Click a living dragon to open it.", bg=BG, fg=MUTED,
                 padx=10, pady=6, anchor="w").pack(fill="x")
        area = tk.Frame(w, bg=BG)
        area.pack(fill="both", expand=True)
        cv = tk.Canvas(area, bg=CARD, highlightthickness=0)
        xs = ttk.Scrollbar(area, orient="horizontal", command=cv.xview)
        ys = ttk.Scrollbar(area, orient="vertical", command=cv.yview)
        cv.configure(xscrollcommand=xs.set, yscrollcommand=ys.set)
        xs.pack(side="bottom", fill="x")
        ys.pack(side="right", fill="y")
        cv.pack(fill="both", expand=True)
        recs = {r["uid"]: r for r in self.tourn.family()}
        alive = {v.uid: v for v in self.tourn.vars}
        keep = set()
        for u in alive:  # each living bot and its ancestors (keeps the picture readable)
            stack = [u]
            while stack:
                x = stack.pop()
                if x in recs and x not in keep:
                    keep.add(x)
                    stack.extend(recs[x]["parents"])
        rows = {}
        for u in sorted(keep):
            rows.setdefault(recs[u]["gen"], []).append(u)
        strong = set()
        for u, v in alive.items():
            lf = self.tourn.life(v)
            if (lf.get("edge") or 0) > 0:
                stack = [u]
                while stack:
                    x = stack.pop()
                    if x in recs and x not in strong:
                        strong.add(x)
                        stack.extend(recs[x]["parents"])
        pos, gx, gy = {}, 96, 92
        for g, us in sorted(rows.items()):
            for i, u in enumerate(us):
                pos[u] = (60 + i * gx, 50 + g * gy)
        for u in keep:
            for pnt in recs[u]["parents"]:
                if pnt in pos:
                    (x0, y0), (x1, y1) = pos[pnt], pos[u]
                    cv.create_line(x0, y0 + 20, x0, (y0 + y1) / 2, x1, (y0 + y1) / 2, x1, y1 - 22, smooth=True,
                                   fill="#f59e0b" if (u in strong and pnt in strong) else LINE, width=2 if u in strong else 1)
        for u, (x, y) in pos.items():
            r = recs[u]
            v = alive.get(u)
            if v is not None:
                st = self.tourn.info.get(id(v), {}).get("status", "warming up")
                draw_creature(cv, v.name, x - 18, y - 22, 36, self.GRID_STATUS.get(st, ("", "#8b5cf6"))[1], status=st,
                              awake=True, phase=0, tags=(f"u{u}",))
                cv.tag_bind(f"u{u}", "<Button-1>", lambda e, v=v: self.bot_detail(v, quiet=True))
                name = E.bot_nickname(v.key())
            else:
                cv.create_oval(x - 7, y - 7, x + 7, y + 7, fill=LINE, outline="")
                name = f"#{u}"
            cv.create_text(x, y + 26, text=name, fill=TEXT if v is not None else MUTED, font=("", 7, "bold" if v else "normal"))
        for g in rows:
            cv.create_text(8, 50 + g * gy, text=f"G{g}", anchor="w", fill=MUTED, font=("", 8, "bold"))
        bb = cv.bbox("all") or (0, 0, 100, 100)
        cv.configure(scrollregion=(0, 0, bb[2] + 40, bb[3] + 40))

    # ---------------- farm replay ----------------
    def record_farm(self):
        if not self.tourn:
            return
        rows = self.tourn.board()
        tiers = [self.tier(r) for r in rows]
        ev = [e for e in self.tourn.events if any(w in e for w in ("Bred", "Retired", "Season", "Migration", "Classic", "Twin"))][-3:]
        self.farm_hist.append((time.time(), tiers.count("egg"), tiers.count("rising"), tiers.count("beast"), ev))

    def open_replay(self, parent=None):
        if parent is None:  # it lives as a tab in the insights window
            return self.open_hub('insights', 'Farm replay')
        data = list(self.farm_hist)
        if len(data) < 5:
            return self._nothing(parent, "Farm replay", "Not enough farm history yet. Let the farm run for a while.")
        w = parent
        w.configure(bg=BG)
        cv = tk.Canvas(w, bg=CARD, highlightthickness=0, height=300)
        cv.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        log = tk.Label(w, text="", bg=BG, fg=TEXT, justify="left", anchor="nw", height=5, font=("", 9))
        log.pack(fill="x", padx=12)
        ctl = ttk.Frame(w, padding=8)
        ctl.pack(fill="x")
        st = dict(i=0, playing=True, speed=8)
        pb = ttk.Button(ctl, text="Pause")
        pb.pack(side="left")
        sp = tk.StringVar(value="8x")
        ttk.Combobox(ctl, textvariable=sp, values=["2x", "8x", "32x"], state="readonly", width=5).pack(side="left", padx=6)
        when = ttk.Label(ctl, text="")
        when.pack(side="left", padx=10)
        recent = deque(maxlen=5)

        def toggle():
            st["playing"] = not st["playing"]
            pb.config(text="Pause" if st["playing"] else "Play")
            if st["playing"]:
                tick()
        pb.config(command=toggle)

        def draw(i):
            cv.delete("all")
            W, H = cv.winfo_width(), cv.winfo_height()
            mx = max(max(d[1] + d[2] + d[3] for d in data), 1)
            n = len(data)
            X = lambda k: 40 + k * (W - 60) / max(1, n - 1)
            Y = lambda y: H - 20 - y / mx * (H - 40)
            for idx, col, name in ((1, "#22c55e", "eggs"), (2, "#3b82f6", "rising"), (3, "#f97316", "BEASTS")):
                cv.create_line(*[z for k in range(0, i + 1, max(1, n // 800)) for z in (X(k), Y(data[k][idx]))] + [X(i), Y(data[i][idx])],
                               fill=col, width=2)
                cv.create_text(W - 12, 14 + idx * 14, anchor="ne", text=f"{name}: {data[i][idx]}", fill=col, font=("", 9, "bold"))
            cv.create_line(X(i), 10, X(i), H - 14, fill=MUTED, dash=(2, 3))
            when.config(text=datetime.fromtimestamp(data[i][0]).strftime("%a %H:%M:%S"))
            for e in data[i][4]:
                recent.append(e)
            log.config(text="\n".join(friendly_text(e)[:140] for e in recent))

        def tick():
            if not w.winfo_exists() or not cv.winfo_exists() or not st["playing"]:
                return
            step = int(sp.get().rstrip("x")) // 2 or 1
            st["i"] = min(len(data) - 1, st["i"] + step)
            draw(st["i"])
            if st["i"] >= len(data) - 1:
                st["playing"] = False
                pb.config(text="Play")
                st["i"] = 0
                return
            w.after(60, tick)
        w.after(200, tick)

    # ---------------- settings profiles ----------------
    def fill_profiles(self, menu):
        menu.delete(0, "end")
        menu.add_command(label="Save current settings as...", command=self.save_profile)
        profs = self.cfg.get("profiles") or {}
        if profs:
            menu.add_separator()
            for name in sorted(profs):
                menu.add_command(label=f"Load: {name}", command=lambda n=name: self.load_profile(n))
            menu.add_separator()
            for name in sorted(profs):
                menu.add_command(label=f"Delete: {name}", command=lambda n=name: self.delete_profile(n))

    PRIVATE = ("alert_discord", "alert_tg_token", "alert_tg_chat")

    def save_profile(self):
        name = simpledialog.askstring("Settings profile", "Name this setup (for example 'Weekday calm live'):")
        if not name or not name.strip():
            return
        snap = {k: v for k, v in json.loads(json.dumps(self.settings)).items() if k not in self.PRIVATE}
        self.cfg.setdefault("profiles", {})[name.strip()] = snap
        E.save_json(CONFIG_PATH, self.cfg)
        self.write(f"Saved settings profile '{name.strip()}'.", "good")

    def load_profile(self, name):
        snap = (self.cfg.get("profiles") or {}).get(name)
        if not snap:
            return
        keep = {k: self.settings.get(k) for k in self.PRIVATE}
        self.settings = E.merge_settings(dict(snap, **keep))
        self.cfg["settings"] = self.settings
        E.save_json(CONFIG_PATH, self.cfg)
        if self.tourn:
            self.tourn.o.update(E.tournament_opts(self.settings))
            self.tourn.cap = int(self.settings["max_bots"])
        if hasattr(self, "farm_size"):
            self.farm_size.set(str(int(self.settings["max_bots"])))
        self.write(f"Loaded settings profile '{name}'." + (" A new decision speed starts with a new session." if self.session else ""), "good")

    def delete_profile(self, name):
        if messagebox.askyesno("Settings profiles", f"Delete the profile '{name}'?"):
            (self.cfg.get("profiles") or {}).pop(name, None)
            E.save_json(CONFIG_PATH, self.cfg)

    # ---------------- backup / restore ----------------
    def backup_all(self):
        p = filedialog.asksaveasfilename(title="Back up SBF", defaultextension=".zip",
                                         initialfile=f"sbf-backup-{datetime.now():%Y%m%d-%H%M}.zip", filetypes=[("Zip", "*.zip")])
        if not p:
            return
        self.save_state()
        self.save_farm(force=True)
        n = self.write_backup(p)
        self.write(f"Backed up {n} files (Library, Hall of Fame, farms, sessions, settings, trades) to {p}. "
                   f"Your broker logins are not in it.", "good")
        return p

    def write_backup(self, p, skip=("cache", "backups", "auto-backups")):
        """Zips the data folder to p, without any keys. Returns the number of files."""
        n = 0
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
            for root_, dirs, files in os.walk(E.HOME_DIR):
                dirs[:] = [d for d in dirs if d not in skip]
                for fn in files:
                    full = os.path.join(root_, fn)
                    if fn == "config.json":  # never put keys in a backup, even when the keychain isn't available
                        try:
                            c_ = json.load(open(full, encoding="utf-8"))
                            for k in E.SECRET_CFG:
                                c_.pop(k, None)
                            for k in E.SECRET_SETTINGS + self.PRIVATE:
                                (c_.get("settings") or {}).pop(k, None)
                            for pr in (c_.get("profiles") or {}).values():
                                for k in E.SECRET_SETTINGS + self.PRIVATE:
                                    pr.pop(k, None)
                            z.writestr("config.json", json.dumps(c_))
                            n += 1
                        except Exception:
                            E.note_error("write_backup")
                        continue
                    if fn.endswith(".lock") or fn.endswith(".flag"):
                        continue
                    z.write(full, os.path.relpath(full, E.HOME_DIR))
                    n += 1
        return n

    def auto_backup(self):
        """Once a week: a copy of the bots, farms, sessions and settings in auto-backups/ (the last 4 are kept).
        Price history is left out (it downloads again by itself), so each copy stays small."""
        if not self.settings.get("auto_backup", True) or self.cfg.get("essentials"):
            return
        if time.time() - float(self.cfg.get("last_auto_backup", 0) or 0) < 7 * 86400:
            return
        folder = os.path.join(E.HOME_DIR, "auto-backups")
        try:
            os.makedirs(folder, exist_ok=True)
            self.save_farm(force=True)
            self.save_state()
            p = os.path.join(folder, f"{APP_SHORT.lower()}-auto-{datetime.now():%Y%m%d-%H%M}.zip")
            n = self.write_backup(p, skip=("cache", "backups", "auto-backups", "history", "saved-farms"))
            old = sorted(f for f in os.listdir(folder) if f.endswith(".zip"))
            for f in old[:-4]:
                os.remove(os.path.join(folder, f))
            self.cfg["last_auto_backup"] = time.time()
            E.save_json(CONFIG_PATH, self.cfg)
            self.write(f"Weekly backup saved ({n} files, no keys) in your data folder under auto-backups.", "good")
        except Exception as e:
            self.write(f"The weekly backup couldn't be saved ({e}).", "warn")

    def restore_all(self):
        p = filedialog.askopenfilename(title="Restore an SBF backup", filetypes=[("Zip", "*.zip")])
        if not p:
            return
        if self.running:
            return messagebox.showinfo("Restore", "Stop the bot first.")
        if not messagebox.askyesno("Restore", "Replace your Library, Hall of Fame, farms, sessions and settings with the "
                                              "backup? Your current ones are kept in a safety copy first."):
            return
        safety = os.path.join(E.HOME_DIR, "backups", f"before-restore-{datetime.now():%Y%m%d-%H%M%S}.zip")
        os.makedirs(os.path.dirname(safety), exist_ok=True)
        with zipfile.ZipFile(safety, "w", zipfile.ZIP_DEFLATED) as z:
            for root_, dirs, files in os.walk(E.HOME_DIR):
                dirs[:] = [d for d in dirs if d not in ("cache", "backups")]
                for fn in files:
                    full = os.path.join(root_, fn)
                    z.write(full, os.path.relpath(full, E.HOME_DIR))
        # say it now: once the files are written nothing may run (a delayed autosave would undo the restore)
        messagebox.showinfo("Restore", f"SBF will now restore the backup and restart.\n(Safety copy: {safety})")
        with zipfile.ZipFile(p) as z:
            for m in z.namelist():
                dest = os.path.normpath(os.path.join(E.HOME_DIR, m))
                if not dest.startswith(os.path.normpath(E.HOME_DIR) + os.sep):
                    continue  # never write outside the data folder
                if m.endswith("/") or not re.search(r"\.(json|csv|log|txt)$", m, re.I):
                    continue  # only SBF's own data files (never programs)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(m) as src, open(dest, "wb") as out:
                    out.write(src.read())
        self.restart_app(save=False)

    def release_lock(self):
        if self.tray is not None:
            self.tray.stop()
        T = self.tray_lib()
        if T:
            T.release_single()

    # ---------------- phone remote (Telegram) ----------------
    def start_tg_remote(self):
        """Listens for commands from your Telegram chat: /status /pause /resume /report /kill."""
        if getattr(self, "_tg_thread", None) and self._tg_thread.is_alive():
            return

        def loop():
            offset, primed = None, False
            while True:
                s_ = self.settings
                if not s_.get("tg_remote") or not s_.get("alert_tg_token") or not s_.get("alert_tg_chat"):
                    time.sleep(15)
                    primed = False
                    continue
                try:
                    if not primed:  # skip anything sent while SBF was closed (an old "/kill confirm" must not fire now)
                        _, offset = E.tg_updates(s_["alert_tg_token"], offset, timeout=0)
                        primed = True
                    ups, offset = E.tg_updates(s_["alert_tg_token"], offset)
                    for chat, text, sent, private, sender in ups:
                        # only your own private chat with the bot, and only fresh messages
                        if (str(chat) == str(s_["alert_tg_chat"]).strip() and private and str(sender) == str(chat)
                                and time.time() - sent < 120):
                            self.q.put(("call", lambda t=text: self.tg_command(t)))
                except Exception:
                    time.sleep(20)
                time.sleep(3)
        self._tg_thread = threading.Thread(target=loop, daemon=True)
        self._tg_thread.start()

    def tg_command(self, text):
        cmd = text.strip().split()[0].lower().split("@")[0] if text.strip() else ""
        reply = None
        if cmd in ("/status", "/start"):
            eq = self.equity() if self.session else 0
            reply = (f"SBF {'LIVE' if self.live else 'Practice'}: {'running' if self.running else 'stopped'}. "
                     + (f"Value ${eq:,.2f} ({(eq / self.start_cash - 1) * 100:+.1f}%). " if self.session else "")
                     + self.pulse_text())
        elif cmd == "/pause":
            if self.running:
                self.toggle()
            reply = "Paused."
        elif cmd == "/resume":
            if self.live:
                reply = "LIVE can't be resumed from the phone (it needs you to type LIVE at the computer)."
            elif not self.running and self.session:
                self.toggle()
                reply = "Resumed (Practice)."
            else:
                reply = "Already running." if self.running else "No session to resume."
        elif cmd == "/report":
            reply = self.weekly_report(force=True)
        elif cmd == "/kill":
            if text.strip().lower().endswith("confirm"):
                reply = self.kill_switch(ask=False)
            else:
                reply = "Send '/kill confirm' to stop, sell everything and lock LIVE."
        elif cmd:
            reply = "Commands: /status /pause /resume /report /kill confirm"
        if reply:
            s_ = dict(self.settings, alert_discord="")
            threading.Thread(target=lambda: E.send_alert(s_, reply), daemon=True).start()

    # ---------------- market context ----------------
    def ctx_card_build(self):
        for w in self.ctx_box.winfo_children():
            w.destroy()
        self.ctx_labels = {}
        syms = [x for x in (self.settings.get("ctx_tickers") or []) if E.valid_coin(x)]
        if not syms:
            tk.Label(self.ctx_box, text="None. Click Edit to add some (VIXY, SPY, TLT...).", bg=CARD, fg=MUTED,
                     font=("", 8), wraplength=220, justify="left").pack(anchor="w")
        for sym in syms:
            row = tk.Frame(self.ctx_box, bg=CARD)
            row.pack(fill="x")
            tk.Label(row, text=sym + (" (VIX)" if sym in ("VIXY", "UVXY", "VXX") else ""), bg=CARD, fg=TEXT,
                     font=("", 9, "bold"), width=11, anchor="w").pack(side="left")
            l = tk.Label(row, text="-", bg=CARD, fg=MUTED, font=("", 9), anchor="w")
            l.pack(side="left")
            self.ctx_labels[sym] = l

    def ctx_card_refresh(self):
        for sym, l in getattr(self, "ctx_labels", {}).items():
            q = self.ctx_hist.get(sym) or self.hist.get(sym)
            px = self.last.get(sym) or (q[-1] if q else None)
            if not px:
                l.config(text="-", fg=MUTED)
                continue
            base = self.start_px.get(sym) or (q[0] if q else None)
            ch = (px / base - 1) * 100 if base else 0
            l.config(text=f"{fmt_px(px)}  {ch:+.2f}%", fg=GREEN if ch > 0 else RED if ch < 0 else MUTED)

    def edit_context(self):
        cur = ", ".join(self.settings.get("ctx_tickers") or [])
        ans = simpledialog.askstring("Market context", "Tickers the bots WATCH but never trade, separated by commas.\n"
                                                       "VIXY stands in for the VIX (fear gauge); TLT is long-term bonds; SPY "
                                                       "the whole market. Up to 8:", initialvalue=cur)
        if ans is None:
            return
        syms = [x.strip().upper() for x in ans.replace(";", ",").split(",") if E.valid_coin(x.strip().upper())][:8]
        self.settings["ctx_tickers"] = syms
        self.cfg["settings"] = self.settings
        E.save_json(CONFIG_PATH, self.cfg)
        if self.tourn:
            self.tourn.ctx_syms = [c for c in syms if c not in self.tourn.coins]
            self.tourn.o["ctx"] = syms
        self.ctx_card_build()
        self.write("Market context: " + (", ".join(syms) or "none") + ". New tickers join the live data when you start "
                   "a new session; studies on them start appearing in new eggs.")

    def ctx_report(self):
        """Per watched ticker: how many bots study it, and what those studies did for them (from the farm and the slow farm)."""
        out = {}
        for t in (self.tourn, (getattr(self, "slow", {}) or {}).get("t")):
            if not t:
                continue
            for v in t.vars:
                for e in v.layers.get("ctx", []):
                    d = out.setdefault(e["sym"], dict(bots=0, n=0.0, saved=0.0, rules=[]))
                    d["bots"] += 1
                    st = v.layer_stats.get(f"ctx:{e['sym']}")
                    if st:
                        d["n"] += st[0]
                        d["saved"] += st[1]
                        d["rules"].append((st[1] / st[0] if st[0] else 0, E.ctx_text(e), E.bot_nickname(v.key())))
        return out

    # ---------------- day-trade guard ----------------
    SAFETY_EXITS = ("stop-loss", "trailing-stop", "manual", "kill switch", "daily-loss", "scalp-flat")

    def managed_guard(self):
        """Runs every minute while SBF is open, bot running or not: checks the Alpaca positions SBF manages (handed
        over by you, or kept from before a practice reset) against your account's rules."""
        try:
            self.guard_kept_copies()
        except Exception:
            E.note_error("guard_kept_copies")
        finally:
            self.root.after(60000, self.managed_guard)

    def is_day_trade(self, c):
        """Was this position bought today (New York date)? Selling it today would be a day trade."""
        t = self.bought_at.get(c)
        return bool(t) and E.ny_date(t) == E.ny_date(time.time())

    def pdt_info(self):
        """(day trades in the last 5 business days, account equity or None, who said so)."""
        a = getattr(self, "acct_info", None) or {}
        if a.get("daytrade_count") is not None:
            try:
                return int(a["daytrade_count"]), float(a.get("equity") or 0) or None, "Alpaca"
            except (TypeError, ValueError):
                pass
        # our own count: live buys and sells of the same stock on the same day, last 5 business days
        days = set()
        d = E.ny_date(time.time())
        while len(days) < 5:
            if d.weekday() < 5:
                days.add(d.isoformat())
            d -= timedelta(days=1)
        opened = {}
        n = 0
        for t in self.trades:
            if t.get("mode") not in ("live", "alpaca") or str(t.get("time", ""))[:10] not in days:
                continue
            k = (t["coin"], str(t["time"])[:10])
            if t["side"] == "buy":
                opened[k] = True
            elif opened.get(k):
                n += 1
        return n, self.equity() if self.live else None, "SBF's own count"

    def pdt_blocked(self):
        if not self.settings.get("pdt_guard", True) or not self.live:
            return False
        n, eq, _ = self.pdt_info()
        return n >= 3 and (eq is None or eq < 25000)

    # ---------------- security check ----------------
    def security_items(self):
        """[(ok: True / False / None for info, title, detail)] describing how SBF is locked down right now."""
        out = []
        kc = E.keychain_ok()
        out.append((True, "No open ports", "SBF never listens for connections. It only makes outgoing, encrypted (HTTPS) "
                    "connections: Alpaca for prices and orders, GitHub for updates, FRED for the calendar, Telegram if "
                    "you turned on alerts or the phone remote."))
        out.append((kc, "Keys kept in your system keychain" if kc else "System keychain not available",
                    "Your Alpaca keys, Telegram token, Discord webhook and FRED key are stored in Windows Credential "
                    "Manager / macOS Keychain, not in SBF's files, backups or exports." if kc else
                    "Install the 'keyring' package (it comes with SBF's requirements) so keys aren't saved in plain files."))
        try:
            raw = open(CONFIG_PATH, encoding="utf-8").read()
            leak = [k for k in E.SECRET_SETTINGS + E.SECRET_CFG if re.search(rf'"{k}":\s*"[^"]+"', raw)]
        except OSError:
            leak = []
        if leak:
            out.append((False, "Some keys are still in config.json", ", ".join(leak) + ": they move to the keychain the "
                        "next time settings are saved (or the keychain isn't available)."))
        out.append((True, "Logs hide keys", "Anything that looks like a key, token or webhook is blanked before it's "
                    "written to activity.log or crash.log."))
        if os.name == "posix":
            try:
                mode = os.stat(E.HOME_DIR).st_mode & 0o777
                out.append((mode & 0o077 == 0, "Data folder private" if mode & 0o077 == 0 else "Data folder readable by others",
                            f"{E.HOME_DIR} (permissions {oct(mode)})"))
            except OSError:
                pass
        else:
            out.append((True, "Data folder in your user profile", f"{E.HOME_DIR}: other Windows users can't open it."))
        out.append((True, "Trading lock", "Buy shares, sell only shares held, checked against the broker's own count: "
                    "nothing is ever sold short. LIVE needs you at the computer typing LIVE; the kill switch locks it "
                    "again. Day-trade guard: " + ("on" if self.settings.get("pdt_guard", True) else "OFF")))
        if self.settings.get("tg_remote"):
            out.append((None, "Telegram phone remote is ON", "Only your own private chat with your bot is obeyed, only "
                        "messages under 2 minutes old, and anything sent while SBF was closed is ignored. It can pause, "
                        "report and kill, never start LIVE. Keep the bot token secret: anyone with it could read the chat."))
        else:
            out.append((True, "Telegram phone remote is off", "No commands are accepted from outside this computer."))
        out.append((bool(self.settings.get("pdt_guard", True)), "Day-trade guard" + ("" if self.settings.get("pdt_guard", True) else " is OFF"),
                    "Under $25,000, SBF won't make a 4th day trade in 5 business days (Settings > Basics)."))
        out.append((None, "Updates", f"Only when you click Update, from github.com/{UPDATE_REPO} over HTTPS; only SBF's own "
                    "files are replaced, each must compile first, and the old version is backed up. Anyone who can push "
                    "to that GitHub repository can ship an update, so keep two-factor sign-in on your GitHub account."))
        out.append((None, "Your broker accounts", "Turn on two-factor sign-in at Alpaca, and use paper keys "
                    "until you're ready. SBF never needs your broker password, only API keys you can revoke any time."))
        return out

    def open_security(self, parent=None):
        if parent is None:  # it lives as a tab in the system window
            return self.open_hub('system', 'Security')
        w = parent
        w.configure(bg=BG)
        tk.Label(w, text="Security check", bg=BG, fg=TEXT, font=("", 14, "bold"), anchor="w", padx=14, pady=10).pack(fill="x")
        t = tk.Text(w, bg=BG, fg=TEXT, relief="flat", wrap="word", padx=14, font=("", 10), highlightthickness=0)
        t.pack(fill="both", expand=True)
        t.tag_configure("ok", foreground=GREEN, font=("", 11, "bold"))
        t.tag_configure("bad", foreground=RED, font=("", 11, "bold"))
        t.tag_configure("info", foreground=ACCENT, font=("", 11, "bold"))
        t.tag_configure("d", foreground=MUTED, lmargin1=26, lmargin2=26, spacing3=8)
        for ok, title, detail in self.security_items():
            mark, tag = ("OK   ", "ok") if ok else ("NOTE ", "info") if ok is None else ("FIX  ", "bad")
            t.insert("end", mark + title + "\n", tag)
            t.insert("end", detail + "\n", "d")
        t.config(state="disabled")
        ttk.Button(w, text="Close", command=lambda: w.winfo_toplevel().destroy()).pack(side="bottom", anchor="e", padx=14, pady=10)

    # ---------------- disk use ----------------
    def open_disk(self, parent=None):
        """What SBF keeps on disk and how much it has written since it started."""
        if parent is None:  # it lives as a tab in the system window
            return self.open_hub('system', 'Disk use')
        w = parent
        w.configure(bg=BG)
        tk.Label(w, bg=BG, fg=MUTED, justify="left", anchor="w", wraplength=690, padx=12, pady=8, text=(
            "Everything SBF keeps is in one folder (File > Open data folder). It only writes when something changes: your "
            "account and farm are saved as they trade (routine saves at most every 30 seconds and 5 minutes), and price "
            "history is saved once when a backtest, Training camp or Free roam first downloads it. Nothing learns or "
            "downloads in the background unless you switch it on. For scale: a modern SSD is rated for hundreds of "
            "terabytes of writes, so even a busy day of trading is a tiny fraction of that.")).pack(fill="x")
        tk.Label(w, text="STORED ON DISK", bg=BG, fg=ACCENT, font=("", 8, "bold"), anchor="w", padx=12).pack(fill="x")
        f1 = tk.Frame(w, bg=BG, padx=12)
        f1.pack(fill="x")
        t1 = self.make_tree(f1, ("who", "size", "files"), (360, 140, 120), ("What", "Size", "Files"))
        t1.configure(height=7)
        tk.Label(w, text="WRITTEN SINCE SBF STARTED", bg=BG, fg=ACCENT, font=("", 8, "bold"), anchor="w",
                 padx=12).pack(fill="x", pady=(10, 0))
        f2 = tk.Frame(w, bg=BG, padx=12)
        f2.pack(fill="x")
        t2 = self.make_tree(f2, ("who", "n", "size", "rate"), (260, 100, 120, 160), ("What", "Times", "Written", "Per hour"))
        t2.configure(height=5)
        bb = tk.Frame(w, bg=BG, padx=12, pady=10)
        bb.pack(side="bottom", fill="x")

        def mb(b):
            return f"{b / 1e6:,.1f} MB" if b >= 1e5 else f"{b / 1e3:,.0f} KB"

        def walk(sub):
            n = b = 0
            for r_, _, fs in os.walk(os.path.join(E.HOME_DIR, sub)):
                for f_ in fs:
                    n += 1
                    try:
                        b += os.path.getsize(os.path.join(r_, f_))
                    except OSError:
                        pass
            return n, b

        def fill():
            t1.delete(*t1.get_children())
            groups = {"Farms (your bots and their memory)": [], "Bot Library and Hall of Fame": [],
                      "Sessions, settings, logs, trades": []}
            try:
                for f_ in os.listdir(E.HOME_DIR):
                    p_ = os.path.join(E.HOME_DIR, f_)
                    if os.path.isfile(p_):
                        k = ("Farms (your bots and their memory)" if f_.startswith("farm-") else
                             "Bot Library and Hall of Fame" if any(x in f_.lower() for x in ("library", "hof", "hall", "collection"))
                             else "Sessions, settings, logs, trades")
                        groups[k].append(os.path.getsize(p_))
            except OSError:
                pass
            for k, v in groups.items():
                t1.insert("", "end", values=(k, mb(sum(v)), len(v)))
            for lab, sub in (("Price history (downloaded once, by year)", "history"), ("Recent prices and calendar", "cache")):
                n, b = walk(sub)
                if n:
                    t1.insert("", "end", values=(lab, mb(b), n))
            t2.delete(*t2.get_children())
            hrs = max(1 / 60, (time.time() - E.DISK_T0) / 3600)
            for k, (n, b) in sorted(E.DISK_WRITES.items(), key=lambda kv: -kv[1][1]):
                t2.insert("", "end", values=(k, n, mb(b), mb(b / hrs) if hrs >= 0.25 else "after 15 min"))
            if w.winfo_exists():
                w.after(5000, fill)

        def clear(sub, what):
            if not messagebox.askyesno("Disk use", f"Delete the {what}? It's downloaded again when it's needed.", parent=w):
                return
            if sub == "cache":  # keep the economic calendar
                for f_ in os.listdir(os.path.join(E.HOME_DIR, sub)) if os.path.isdir(os.path.join(E.HOME_DIR, sub)) else []:
                    if f_ != "events.json":
                        try:
                            os.remove(os.path.join(E.HOME_DIR, sub, f_))
                        except OSError:
                            pass
            else:
                shutil.rmtree(os.path.join(E.HOME_DIR, sub), ignore_errors=True)
            self.write(f"Deleted the {what}.")
        ttk.Button(bb, text="Delete downloaded history", command=lambda: clear("history", "downloaded price history")).pack(side="left")
        ttk.Button(bb, text="Delete price cache", command=lambda: clear("cache", "recent price cache")).pack(side="left", padx=6)
        ttk.Button(bb, text="Close", command=lambda: w.winfo_toplevel().destroy()).pack(side="right")
        fill()

    # ---------------- economic calendar ----------------
    def news_card_refresh(self):
        try:
            for w in self.news_box.winfo_children():
                w.destroy()
            up = E.upcoming_events(days=21)[:4]
            if not E.events():
                tk.Label(self.news_box, text="No calendar yet. Click Calendar... to download it (free).", bg=CARD, fg=MUTED,
                         font=("", 8), wraplength=220, justify="left").pack(anchor="w")
            elif not up:
                tk.Label(self.news_box, text="Nothing big in the next 3 weeks.", bg=CARD, fg=MUTED, font=("", 8)).pack(anchor="w")
            for t, k in up:
                row = tk.Frame(self.news_box, bg=CARD)
                row.pack(fill="x")
                soon = t - time.time() < 36 * 3600
                tk.Label(row, text=datetime.fromtimestamp(t).strftime("%a %b %d %H:%M"), bg=CARD, fg=TEXT if soon else MUTED,
                         font=("", 8, "bold" if soon else ""), anchor="w").pack(side="left", padx=(0, 8))
                tk.Label(row, text=E.EVENT_KINDS[k][1], bg=CARD, fg="#f59e0b" if soon and k in E.BIG_EVENTS else TEXT,
                         font=("", 8), anchor="w").pack(side="left")
        except tk.TclError:
            return
        self.root.after(15 * 60 * 1000, self.news_card_refresh)

    def fetch_calendar(self, key=None, done=None, quiet=False):
        if quiet and self.cfg.get("essentials"):
            return  # the automatic weekly refresh is an extra download
        key = (key or self.cfg.get("fred_key") or "").strip()
        if not key:
            if done:
                done(False, "Enter your FRED key first.")
            return

        def work():
            try:
                ev, errs = E.fetch_events(key)
                n = sum(len(x) for x in ev.values())
                msg = f"Economic calendar downloaded: {n} releases since 2015, plus the scheduled ones." + (
                    f" (Skipped: {'; '.join(errs)})" if errs else "")
                ok = True
            except Exception as e:
                msg, ok = f"Couldn't download the economic calendar: {e}", False

            def fin():
                if not quiet or not ok:
                    self.write(msg, "good" if ok else "warn")
                self.news_card_refresh()
                if done:
                    done(ok, msg)
            self.q.put(("call", fin))
        threading.Thread(target=work, daemon=True).start()

    def event_report(self):
        """Per event never-rule: bots using it and its graded record."""
        out = {k: dict(bots=0, n=0, saved=0.0) for k in E.EVENT_VETOES}
        pools = [self.tourn.vars] if self.tourn else []
        if self.slow.get("t"):
            pools.append(self.slow["t"].vars)
        for vs in pools:
            for v in vs:
                for k in E.EVENT_VETOES:
                    if k in (v.vetoes or {}):
                        out[k]["bots"] += 1
                    st = (v.veto_stats or {}).get(k)
                    if st:
                        out[k]["n"] += st[0]
                        out[k]["saved"] += st[1]
        return out

    def open_calendar(self, parent=None):
        if parent is None:  # it lives as a tab in the insights window
            return self.open_hub('insights', 'Economic calendar')
        w = parent
        w.configure(bg=BG)
        tk.Label(w, bg=BG, fg=MUTED, justify="left", anchor="w", wraplength=730, padx=12, pady=8, text=(
            "Scheduled news moves stocks more than almost anything: prices jump around CPI, jobs reports and Fed "
            "decisions, and often reverse right after. With the calendar downloaded, bots can grow three never-rules "
            "around these days (quiet before big news, quiet before the Fed, let the news settle), and every trade those "
            "rules skip is graded afterwards, in live trading, the farms, backtests, Training camp and Free roam, all the "
            "way back to 2016. Release dates come from FRED, the St. Louis Fed's free data service.")
                 ).pack(fill="x")
        top = tk.Frame(w, bg=BG, padx=12)
        top.pack(fill="x")
        tk.Label(top, text="FRED API key:", bg=BG, fg=TEXT).pack(side="left")
        key = tk.StringVar(value=self.cfg.get("fred_key", ""))
        ttk.Entry(top, textvariable=key, width=36, show="*").pack(side="left", padx=6)
        res = tk.Label(w, text="", bg=BG, fg=MUTED, anchor="w", padx=12, wraplength=730, justify="left")

        def go():
            k = key.get().strip()
            if not re.fullmatch(r"[0-9a-zA-Z]{32}", k):
                return messagebox.showerror("Economic calendar", "A FRED key is 32 letters and numbers.", parent=w)
            self.cfg["fred_key"] = k
            E.save_json(CONFIG_PATH, self.cfg)
            res.config(text="Downloading...", fg=MUTED)
            self.fetch_calendar(k, lambda ok, msg: (res.config(text=msg, fg=GREEN if ok else RED), fill())
                                if w.winfo_exists() else None)
        ttk.Button(top, text="Download", style="Accent.TButton", command=go).pack(side="left")
        ttk.Button(top, text="Get a free key", style="Plain.TButton",
                   command=lambda: webbrowser.open("https://fred.stlouisfed.org/docs/api/api_key.html")).pack(side="left", padx=6)
        res.pack(fill="x", pady=(4, 0))
        fe = E.events_fetched()
        if fe:
            res.config(text=f"Last downloaded {datetime.fromtimestamp(fe).strftime('%b %d %Y')}. It refreshes itself weekly.")
        tk.Label(w, text="Next 45 days", bg=BG, fg=TEXT, font=("", 10, "bold"), anchor="w", padx=12).pack(fill="x", pady=(8, 0))
        f1 = tk.Frame(w, bg=BG, padx=12)
        f1.pack(fill="both", expand=True)
        up = self.make_tree(f1, ("time", "who", "in"), (210, 260, 200), ("When (your time)", "Release", "In"))
        tk.Label(w, text="What the bots learned about news days", bg=BG, fg=TEXT, font=("", 10, "bold"), anchor="w",
                 padx=12).pack(fill="x", pady=(8, 0))
        f2 = tk.Frame(w, bg=BG, padx=12, pady=(0))
        f2.pack(fill="x", pady=(0, 12))
        lr = self.make_tree(f2, ("who", "bots", "n", "saved"), (330, 100, 130, 160), ("Never-rule", "Bots using", "Trades skipped", "Saved / cost"))
        lr.configure(height=3)

        def fill():
            for t_ in (up, lr):
                t_.delete(*t_.get_children())
            for t, k in E.upcoming_events(days=45):
                d = (t - time.time()) / 86400
                up.insert("", "end", values=(datetime.fromtimestamp(t).strftime("%a %b %d  %H:%M"), E.EVENT_KINDS[k][1],
                                             "now" if d < 0 else f"{d * 24:.0f} hours" if d < 2 else f"{d:.0f} days"))
            for k, d in self.event_report().items():
                lr.insert("", "end", values=(E.VETOES[k]["name"], d["bots"], int(d["n"]),
                                             f"{'saved' if d['saved'] >= 0 else 'cost'} {abs(d['saved']):.1f}%" if d["n"] else "-"))
        fill()

    def open_context(self, parent=None):
        if parent is None:  # it lives as a tab in the insights window
            return self.open_hub('insights', 'Market context')
        w = parent
        w.configure(bg=BG)
        tk.Label(w, bg=BG, fg=MUTED, justify="left", anchor="w", wraplength=900, padx=12, pady=8, text=(
            "The bots watch these tickers without trading them. A context study votes for or against a trade depending on "
            "what the watched ticker is doing, and every trade it held back is checked afterwards: 'saved' means skipping "
            "those trades avoided losses, 'cost' means it blocked winners. Good studies spread to children, costly ones die "
            "out. VIXY stands in for the VIX.")).pack(fill="x")
        t = self.make_tree(w, ("sym", "now", "bots", "n", "saved", "best"), (90, 110, 70, 90, 110, 440),
                           ("Ticker", "Now", "Bots using", "Trades judged", "Saved / cost", "Best rule so far (and who)"))
        rep_ = self.ctx_report()
        for sym in list(dict.fromkeys(list(self.settings.get("ctx_tickers") or []) + list(rep_))):
            d = rep_.get(sym, dict(bots=0, n=0, saved=0, rules=[]))
            q = self.ctx_hist.get(sym) or []
            px = self.last.get(sym) or (q[-1] if q else None)
            best = max(d["rules"]) if d["rules"] else None
            t.insert("", "end", values=(sym, fmt_px(px) if px else "-", d["bots"], int(d["n"]),
                                        f"{'saved' if d['saved'] >= 0 else 'cost'} {abs(d['saved']):.1f}%" if d["n"] else "-",
                                        f"{best[1]}  ({best[2]})" if best else
                                        "traded this session, so its own chart is already studied" if sym in self.session_coins
                                        else "no evidence yet"))
        tk.Label(w, bg=BG, fg=MUTED, anchor="w", padx=12, pady=6,
                 text="Tip: Free roam and the Training camp let the farm learn these reactions from months of history.").pack(fill="x")


    # ---------------- compact top bar ----------------
    def apply_compact(self):
        """Compact: the account tiles shrink to one slim strip (title and number side by side), giving the farm more room."""
        on = bool(self.cfg.get("compact", False))
        for key, lbl in getattr(self, "tiles", {}).items():
            t = self.tile_titles.get(key)
            if not t:
                continue
            if on:
                t.pack_configure(side="left", anchor="w", pady=0)
                lbl.pack_configure(side="left", anchor="w", padx=(8, 0))
                lbl.config(font=("", 11, "bold"))
                t.master.config(pady=3)
                if key == "day" and hasattr(self, "day_sub"):
                    self.day_sub.pack_configure(side="left", padx=(8, 0))
            else:
                t.pack_configure(side="top", anchor="w", pady=(0, 4))
                lbl.pack_configure(side="top", anchor="w", padx=0)
                lbl.config(font=("", 15, "bold"))
                t.master.config(pady=8)
                if key == "day" and hasattr(self, "day_sub"):
                    self.day_sub.pack_configure(side="top", padx=0)

    def toggle_compact(self):
        self.cfg["compact"] = bool(self.compact_var.get())
        E.save_json(CONFIG_PATH, self.cfg)
        self.apply_compact()

    # ---------------- background mode ----------------
    def _on_root_configure(self, e):
        """While the window is being dragged or resized, animations and chart redraws pause so it moves smoothly."""
        if e.widget is self.root:
            geo = (e.x, e.y, e.width, e.height)
            if geo != getattr(self, "_last_geo", None):
                self._last_geo = geo
                self._moving_until = time.time() + 0.25

    def moving(self):
        return time.time() < getattr(self, "_moving_until", 0)

    def in_bg(self):
        """True while minimized with Background mode on: screens skip their redraws until you come back."""
        try:
            return (bool(self.settings.get("bg_mode", True)) and self.root.state() == "iconic") or self.tray_hidden()
        except Exception:
            return False

    def on_restore(self, e=None):
        if e is not None and e.widget is not self.root:
            return
        if getattr(self, "_bg_dirty", False):
            self._bg_dirty = False
            for fn in (self.refresh_board, self.draw_chart, self.refresh_view):
                try:
                    fn()
                except Exception:
                    pass

    # ---------------- undo (Library / Hall of Fame) ----------------
    def lib_snapshot(self):
        return (json.loads(json.dumps(E.load_collection())), json.loads(json.dumps(E.load_hof())))

    def offer_undo(self, msg, snap):
        """A small bar with an Undo button for 15 seconds after releasing bots or removing them from the Hall of Fame."""
        old = getattr(self, "_undo_win", None)
        if old is not None:
            try:
                old.destroy()
            except Exception:
                pass
        try:
            w = tk.Toplevel(self.root)
            w.overrideredirect(True)
            w.attributes("-topmost", True)
            f = tk.Frame(w, bg=CARD, highlightthickness=1, highlightbackground=ACCENT, padx=12, pady=6)
            f.pack()
            tk.Label(f, text=msg, bg=CARD, fg=TEXT).pack(side="left")

            def undo():
                E.save_collection(snap[0])
                E.save_hof(snap[1], self.version)
                self.__dict__.pop("_file_cache", None)
                w.destroy()
                self.write("Undone: your Library and Hall of Fame are back as they were.", "good")
                self.after_hof_change()
                if getattr(self, "col_win", None) is not None and self.col_win.winfo_exists():
                    self.col_refresh()
            tk.Button(f, text="Undo", command=undo, bg=ACCENT, fg="white", relief="flat", padx=10,
                      activebackground=ACCENT, cursor="hand2").pack(side="left", padx=(12, 0))
            tk.Label(f, text="\u2715", bg=CARD, fg=MUTED, cursor="hand2", padx=6).pack(side="left")
            f.winfo_children()[-1].bind("<Button-1>", lambda e: w.destroy())
            w.update_idletasks()
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            w.geometry(f"+{rx + (rw - w.winfo_reqwidth()) // 2}+{ry + rh - w.winfo_reqheight() - 30}")
            w.after(15000, lambda: w.winfo_exists() and w.destroy())
            self._undo_win = w
        except Exception:
            pass

    # ---------------- farm presets ----------------
    PRESETS = {
        "Calm": dict(max_bots=24, evolve_every=36, winner_boost=1.5, fail_drop=2.0, retire_hours=24.0, classic_rate=15,
                     long_holds=True),
        "Balanced": dict(max_bots=30, evolve_every=24, winner_boost=2.5, fail_drop=3.0, retire_hours=12.0, classic_rate=25,
                         long_holds=True),
        "Wild": dict(max_bots=80, evolve_every=12, winner_boost=3.0, fail_drop=4.0, retire_hours=6.0, classic_rate=35,
                     long_holds=True),
    }
    PRESET_TIPS = {"Calm": "fewer bots, slower evolution, small trade sizes for winners, patient",
                   "Balanced": "the default farm",
                   "Wild": "a big farm that evolves fast, bolder winners, quick turnover"}

    def preset_menu(self, widget):
        m = self._menu()
        m.add_command(label="Farm preset", state="disabled")
        m.add_separator()
        for name in self.PRESETS:
            m.add_command(label=f"{name}: {self.PRESET_TIPS[name]}", command=lambda n=name: self.apply_preset(n))
        try:
            m.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())
        finally:
            m.grab_release()

    def apply_preset(self, name):
        self.settings.update(self.PRESETS[name])
        self.cfg["settings"] = self.settings
        self.cfg["preset"] = name
        E.save_json(CONFIG_PATH, self.cfg)
        if hasattr(self, "farm_size"):
            self.farm_size.set(str(self.settings["max_bots"]))
        t = self.tourn
        if t:
            t.o.update(E.tournament_opts(self.settings))
            t.cap = int(self.settings["max_bots"])
            t.family_cap = max(5, -(-t.cap // 4))
            t.evolve_every = int(self.settings["evolve_every"])
            t.retire_steps = self.retire_steps(self.session_bar)
            while len(t.vars) > t.cap:
                worst = [v for v, _ in t.weakest() if v not in t.active]
                if not worst:
                    break
                t._remove(worst[0], "farm made smaller")
            t._rate()
            self.refresh_board()
        self.write(f"Farm preset: {name} ({self.PRESET_TIPS[name]}).", "good")

    # ---------------- first-run tour ----------------
    def maybe_tour(self):
        if not self.cfg.get("toured"):
            self.start_tour()

    def start_tour(self):
        steps = [
            (lambda: self.grid_chips["Eggs"], "The dens",
             "Every dragon is a bot. New ones start as Eggs, grow into Rising, and the dominant ones become BEASTS. "
             "Click a den to see who lives there."),
            (lambda: self.catch_btn, "Catch!",
             "Select a dragon you like and press Catch! to keep it forever in your Library (it gets a name and a rarity)."),
            (lambda: self.grid_chips["Hall of Fame"], "Hall of Fame",
             "Your hand-picked champions. Only you decide who gets in: right-click any dragon, or drag its card onto this tab."),
            (lambda: self.seg_paper, "Practice and Live",
             "Green PRACTICE uses pretend money. Blue LIVE is real money and always asks you to type LIVE first."),
            (lambda: self.btn, "Start",
             "Press Start and the farm wakes up: dragons hatch, compete, breed and evolve while you watch. "
             "Help > Take the tour shows this again."),
        ]
        self.cfg["toured"] = True
        E.save_json(CONFIG_PATH, self.cfg)
        self._tour_step(steps, 0)

    def _tour_step(self, steps, i):
        old = getattr(self, "_tour_win", None)
        if old is not None:
            try:
                old.destroy()
            except Exception:
                E.note_error("_tour_step")
        if i >= len(steps):
            return
        try:
            widget = steps[i][0]()
            if not widget.winfo_ismapped():
                return self._tour_step(steps, i + 1)
        except Exception:
            return self._tour_step(steps, i + 1)
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        f = tk.Frame(w, bg=CARD, highlightthickness=2, highlightbackground=ACCENT, padx=14, pady=10)
        f.pack()
        tk.Label(f, text=f"\u25b2  {steps[i][1]}   ({i + 1}/{len(steps)})", bg=CARD, fg=ACCENT, font=("", 10, "bold")).pack(anchor="w")
        tk.Label(f, text=steps[i][2], bg=CARD, fg=TEXT, wraplength=320, justify="left").pack(anchor="w", pady=(4, 8))
        row = tk.Frame(f, bg=CARD)
        row.pack(fill="x")
        tk.Button(row, text="Next" if i < len(steps) - 1 else "Done", bg=ACCENT, fg="white", relief="flat", padx=12,
                  cursor="hand2", command=lambda: self._tour_step(steps, i + 1)).pack(side="right")
        if i < len(steps) - 1:
            tk.Button(row, text="Skip tour", bg=CARD, fg=MUTED, relief="flat", cursor="hand2",
                      command=w.destroy).pack(side="right", padx=(0, 6))
        w.update_idletasks()
        x = min(widget.winfo_rootx(), self.root.winfo_rootx() + self.root.winfo_width() - w.winfo_reqwidth() - 10)
        y = widget.winfo_rooty() + widget.winfo_height() + 6
        if y + w.winfo_reqheight() > self.root.winfo_rooty() + self.root.winfo_height():
            y = widget.winfo_rooty() - w.winfo_reqheight() - 6
        w.geometry(f"+{max(0, x)}+{max(0, y)}")
        self._tour_win = w

    def allowance_mult(self, c, voters, mult):
        """Allowance per bot: shrinks (or skips) a buy when the bots voting for it already have their share of the
        account tied up. Returns the new size multiplier (0 = skip)."""
        pct = float(self.settings.get("bot_allowance", 25) or 0)
        if pct <= 0 or not voters:
            return mult
        cap = self.start_cash * pct / 100
        used = {}
        for alloc in self.bot_alloc.values():
            for uid, usd in alloc.items():
                used[uid] = used.get(uid, 0.0) + usd
        room = sum(max(0.0, cap - used.get(v.uid, 0.0)) for v in voters)
        want = self.trade_size(c, mult)
        if want <= 0 or room >= want:
            return mult
        if room < 1:
            if time.time() - getattr(self, "_allow_note", 0) > 900:
                self._allow_note = time.time()
                self.write(f"Allowance: the bots voting to buy {tick_of(c)} already have their share of the account tied up, "
                           f"so the buy was skipped. (Settings > Basics > Allowance per bot)", "warn")
            return 0.0
        return mult * room / want

    def why_text(self, c, sig, voters, who):
        """Plain-English 'why did it trade?' for the account's trade on coin c."""
        lines = []
        kind = self.tourn.kind.get(c) if (self.wild and self.tourn) else None
        if kind == "add":
            lines.append(f"Adding to {c}: the winning bots holding most of the weight are still in it.")
        elif kind == "slow":
            lines.append("Only shaky bots wanted this, so it's a small, slowed-down trade.")
        for v in sorted(voters, key=lambda x: -self.tourn.info.get(id(x), {}).get("weight", 0))[:5]:
            st = STATUS_TEXT.get(self.tourn.info.get(id(v), {}).get("status"), "")
            lines.append(f"- {E.bot_nickname(v.key())} ({STRAT_LABELS.get(v.name, v.name)}, {st}): "
                         f"{v.why.get(c) or v.name + ' said ' + sig}")
        if len(voters) > 5:
            lines.append(f"...and {len(voters) - 5} more bot{'s' if len(voters) > 6 else ''} agreed.")
        if not voters and who:
            lines.append("Your picked strategies voted " + sig + ": " + ", ".join(STRAT_LABELS.get(n, n) for n in dict.fromkeys(who)))
        return "\n".join(lines)

    def show_trade_why(self, row):
        """The 'why did it trade?' window for one trade."""
        w = tk.Toplevel(self.root)
        w.title("Why did it trade?")
        w.configure(bg=BG)
        f = tk.Frame(w, bg=BG, padx=16, pady=12)
        f.pack(fill="both", expand=True)
        side = str(row.get("side", "")).upper()
        tk.Label(f, text=f"{side} {row.get('coin', '')}  at {row.get('price', '')}  ({str(row.get('time', '')).replace('T', ' ')})",
                 bg=BG, fg=GREEN if side == "BUY" else RED, font=("", 12, "bold")).pack(anchor="w")
        reason = REASON_TEXT.get(row.get("reason"), row.get("reason", ""))
        tk.Label(f, text=f"Reason: {reason}", bg=BG, fg=TEXT).pack(anchor="w", pady=(4, 0))
        if row.get("who"):
            tk.Label(f, text=f"Strategies behind it: {row['who']}", bg=BG, fg=MUTED, wraplength=560, justify="left").pack(anchor="w")
        detail = row.get("detail") or ("Only trades made from now on carry the full story of which bots voted and why."
                                       if row.get("mode") != "bot" else "This is one bot's own practice trade inside the farm.")
        tk.Label(f, text=detail, bg=BG, fg=TEXT, wraplength=560, justify="left", font=("", 10)).pack(anchor="w", pady=(8, 0))
        ttk.Button(f, text="Close", command=w.destroy).pack(anchor="e", pady=(12, 0))

    # ---------------- weekly report ----------------
    def weekly_report(self, force=False):
        """Once a week (or on demand): what the farm learned and how the account did. Saved to the data folder
        and sent to Discord / Telegram if set up. Returns the report text."""
        last = float(self.cfg.get("last_report", 0) or 0)
        if not force and (not self.settings.get("weekly_report", True) or time.time() - last < 7 * 86400):
            return None
        if not force and not last:  # first run: start the clock instead of reporting on nothing
            self.cfg["last_report"] = time.time()
            E.save_json(CONFIG_PATH, self.cfg)
            return None
        since = datetime.now().timestamp() - 7 * 86400
        lines = [f"SBF weekly report, {datetime.now():%Y-%m-%d}", ""]
        for mode in ("live", "alpaca", "paper"):
            rows = [r for r in self.trades + list(getattr(self, "mirror_trades", [])) if r.get("mode") == mode
                    and (self._ts(r.get("time")) or 0) >= since]
            if rows:
                pl = sum(float(r["pl"]) for r in rows if r.get("pl") not in ("", None))
                lines.append(f"{({'live': 'LIVE', 'alpaca': 'ALPACA PAPER'}).get(mode, 'PRACTICE')} account: {len(rows)} trades this week, "
                             f"closed profit/loss ${pl:+,.2f}.")
        if self.session:
            eq = self.equity()
            lines.append(f"Current session ({'LIVE' if self.live else 'Practice'}): total value ${eq:,.2f} "
                         f"({(eq / self.start_cash - 1) * 100:+.1f}% since it began).")
        t = self.tourn
        if t:
            rows = t.board()
            lines += ["", f"Farm: {len(rows)} bots, generation {max((r['gen'] for r in rows), default=0)}, "
                          f"{sum(1 for r in rows if r['status'] == 'active')} winning right now."]
            if t.season:
                lines.append(f"Season: {t.season}. Champions known for: {', '.join(sorted(t.champions)) or 'none yet'}.")
            twins = [r for r in rows if r.get("twin") is not None]
            if twins:
                lines.append(f"Twin tests: {sum(1 for r in twins if r['twin'] > 0)} passed, {sum(1 for r in twins if r['twin'] <= 0)} failed.")
            lines.append(f"Elders remembered: {len(t.elders)}.")
            best = sorted((r for r in rows if r["life"]["edge"] is not None), key=lambda r: -r["life"]["edge"])[:5]
            if best:
                lines += ["", "Top dragons (lifetime, vs the market):"]
                for r in best:
                    lines.append(f"  {E.bot_nickname(r['v'].key())} ({STRAT_LABELS.get(r['name'], r['name'])}): "
                                 f"{r['life']['ret'] * 100:+.1f}%, {r['life']['edge'] * 100:+.1f}% vs the market")
        lines += ["", f"Library: {len(E.load_collection())} bots saved, {len(E.load_hof())} in the Hall of Fame."]
        text = "\n".join(lines)
        try:
            d = os.path.join(E.HOME_DIR, "reports")
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, f"report-{datetime.now():%Y-%m-%d}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self.write(f"Weekly report saved: {path}", "good")
        except OSError:
            pass
        if E.alerts_configured(self.settings):
            s = dict(self.settings)
            threading.Thread(target=lambda: E.send_alert(s, text), daemon=True).start()
        self.cfg["last_report"] = time.time()
        E.save_json(CONFIG_PATH, self.cfg)
        return text

    def show_report(self, parent=None):
        if parent is None:  # it lives as a tab in the insights window
            return self.open_hub('insights', 'Weekly report')
        text = self.weekly_report(force=True)
        w = parent
        w.configure(bg=BG)
        box = tk.Text(w, width=90, height=28, bg=CARD, fg=TEXT, relief="flat", padx=10, pady=8, font=("", 10))
        box.pack(fill="both", expand=True)
        box.insert("1.0", text or "")
        box.config(state="disabled")

    def check_hof(self):
        """Saves long-term winners to the Hall of Fame."""
        try:
            need = int(float(self.settings.get("hof_hours", 6)) * 60 / max(self.session_bar, 0.25))
            cands = self.tourn.hall_of_fame_candidates(max(1, need), int(self.settings.get("hof_min_trades", 4)))
            if not cands:
                return
            rows = [dict(r, bar_minutes=self.session_bar) for r in self.tourn.board() if r["v"] in cands]
            entries = E.export_bots(rows, self.version, note=f"long-term winner, {', '.join(self.session_coins)}")["bots"]
            to_hof = self.settings.get("auto_hof", False)
            if self.settings.get("auto_catch", True):
                self.catch_rows(entries, "long-term winner")
            new = E.induct(entries, self.version) if to_hof else 0
            for v in cands:
                v.inducted = True  # remembered so it is only saved once
                lf = self.tourn.life(v)
                where = "the Hall of Fame" if to_hof else "your Bot Library (promote it to the Hall of Fame if you trust it)"
                msg = (f"Long-term winner: {E.bot_nickname(v.key())} ({friendly_label(v.name, v.params, v.tf, self.session_bar)}) "
                       f"made {lf['ret'] * 100:+.1f}% ({pct_txt(lf['edge'])} vs the market). Saved to {where}.")
                self.write(msg, "good")
                self.alert("hof", msg)
            self.notify("SBF", f"{len(cands)} long-term winner{'s' if len(cands) != 1 else ''} saved"
                        + (" to the Hall of Fame" if new else " to your Library"), sound="hof")
        except Exception:
            note = self._record_error("updating the Hall of Fame")
            if note:
                self.write(note, "bad")

    def collect_shadow(self):
        """Adds every bot's pretend trades from this bar to the Trades tab (they never touch the account)."""
        try:
            stamp = datetime.now().isoformat(timespec="seconds")
            new = self.tourn.pop_shadow()
            for t in new:
                self.shadow_trades.append(dict(
                    time=stamp, mode="bot", side=t["side"], coin=t["coin"], qty=round(t["qty"], 8),
                    price=round(t["price"], 6), usd=round(t["usd"], 2),
                    pl="" if t["pl"] is None else round(t["pl"], 2), reason="bot-test",
                    who=friendly_label(t["name"], t["params"], t.get("tf", 1), self.session_bar),
                    bot=t.get("uid"), strategy=t.get("name"), under_px=t.get("under_px"), vol=t.get("vol"),
                    vol_src=t.get("vol_src"), held=t.get("held"), why=str(t.get("why") or "")[:160],
                    contract={k: t["contract"].get(k) for k in ("type", "K", "exp", "symbol")} if t.get("contract") else None))
            del self.shadow_trades[:-4000]
            if new:
                if self.cur_tab() == "Trades":
                    self.refresh_trades()
                else:
                    self._trades_dirty = True  # redrawn when you open the Trades tab
        except Exception:
            note = self._record_error("listing the bots' trades")
            if note:
                self.write(note, "bad")

    def update_wild_msg(self):
        n, top, scored = self.tourn.summary()
        if self.settings.get("proven_only", True):
            t = self.tourn
            proven = t.proven_bots()
            leading = [v for v in t.active if v in proven]
            need = f"{int(self.settings.get('proven_trades', 8) or 0)}+ finished trades, made money over its lifetime, " \
                   f"beat just holding" + (", passed its exams" if self.settings.get("proven_exam", True) else "")
            if leading:
                best = max(leading, key=lambda v: t.life(v)["ret"])
                msg = (f"The account follows {len(leading)} proven winner{'s' if len(leading) != 1 else ''} "
                       f"({len(proven)} proven in all). Best lifetime record: {E.bot_nickname(best.key())} "
                       f"({t.life(best)['ret'] * 100:+.1f}% since it hatched).")
            elif proven:
                msg = (f"{len(proven)} bot{'s are' if len(proven) != 1 else ' is'} proven, but none is winning right now, "
                       "so the account waits.")
            else:
                msg = (f"No bot is proven yet ({need}), so the account doesn't trade. The farm keeps training on its own "
                       "pretend money.")
            self.wild_msg = msg
            if msg != self.last_wild_msg:
                self.write(msg)
                self.last_wild_msg = msg
            return
        if n:
            info = self.tourn.info[id(top)]
            slow = len(self.tourn.slowed)
            msg = (f"Wildcard is following {n} winning strateg{'y' if n == 1 else 'ies'}. "
                   f"Best right now: {friendly_label(top.name, top.params)} at {info['stake']:.1f}x trade size."
                   + (f" {slow} shaky one{'s are' if slow != 1 else ' is'} slowed down." if slow else ""))
        elif scored:
            slow = len(self.tourn.slowed)
            msg = ("No strategy is winning consistently right now, so the bot is " +
                   (f"only making small, slow trades with {slow} shaky one{'s' if slow != 1 else ''}." if slow
                    else "standing aside."))
        else:
            msg = "Wildcard is still learning from recent prices."
        self.wild_msg = msg
        if msg != self.last_wild_msg:
            self.write(msg)
            self.last_wild_msg = msg

    def account_stops(self):
        """(stop, take, who decided) as fractions. With 'bot_stops' on in Wildcard, the account uses the
        weighted average stop-loss / take-profit that the current winners evolved, as long as winners holding
        most of the weight actually use one. Otherwise your own Safety stop / Lock in profit settings."""
        stop, take = self.settings["stop_loss"] / 100, self.settings["take_profit"] / 100
        src = ["you", "you"]
        t = self.tourn
        longs = [v for v in (t.active if t else []) if not E.is_scalp(v.name)]  # scalps carry their own exits
        if self.settings.get("bot_stops", True) and self.wild and t and longs:
            tot = sum(t.info[id(v)]["weight"] for v in longs) or 0
            for i, k in enumerate(("sl", "tp")):
                using = [(t.info[id(v)]["weight"], v.risk[k]) for v in longs if v.risk.get(k)]
                w = sum(x for x, _ in using)
                if tot > 0 and w * 2 >= tot:
                    val = sum(x * r for x, r in using) / w / 100
                    if i == 0:
                        stop = val
                    else:
                        take = val
                    src[i] = "bots"
        return stop, take, src

    def risk_checks(self):
        """Safety stop / lock-in profit / trailing stop on each position's share price, and scalps' own exits."""
        if not self.live:  # "Both": a copy whose practice trade is gone (closed while the copy was on its way) is closed too
            for c in [k for k, m in getattr(self, "mirror", {}).items() if not m.get("pending") and self.pos.get(k, 0) <= 0]:
                self.mirror_close(c, "sync")
        stop, take, _ = self.account_stops()
        trail = float(self.settings.get("trail_stop", 0) or 0) / 100
        for c, q in list(self.pos.items()):
            if q <= 0 or c in self.pending:
                continue
            px = self.last.get(tick_of(c), 0)
            sc = (self.opt.get(c) or {}).get("scalp")
            if sc and E.minutes_to_close(time.time()) <= sc.get("flatby", 15):
                self.sell(c, px, "scalp-flat")  # scalps are never held overnight
                continue
            val = self.position_price(c)
            if val <= 0 or not self.cost.get(c):
                continue
            self.high[c] = max(self.high.get(c, val), val)
            r = val / (self.cost[c] / q) - 1
            if sc:  # a scalp: the scalpers' own tight stop and target, and their time limit
                if r <= -sc["sl"] / 100:
                    self.sell(c, px, "stop-loss")
                elif r >= sc["tp"] / 100:
                    self.sell(c, px, "take-profit")
                elif time.time() - sc.get("at", time.time()) >= sc.get("mins", 10) * 60:
                    self.sell(c, px, "scalp-time")
                elif trail > 0 and val <= self.high[c] * (1 - trail):
                    self.sell(c, px, "trailing-stop")
                continue
            if stop > 0 and r <= -stop:
                self.sell(c, px, "stop-loss")
            elif take > 0 and r >= take:
                self.sell(c, px, "take-profit")
            elif trail > 0 and val <= self.high[c] * (1 - trail):
                self.sell(c, px, "trailing-stop")

    def check_daily(self):
        today, eq = date.today(), self.equity()
        if self.day != today:
            self.day, self.day_eq = today, eq
            return
        lim = self.settings["daily_loss"] / 100
        if lim > 0 and self.running and eq < self.day_eq * (1 - lim):
            self.running = False
            self.halted_day = today
            self.ui_running(False)
            self.write(f"HALTED: daily loss limit ({self.settings['daily_loss']}%) reached. Positions are still open.")
            self.alert("problems", f"HALTED: daily loss limit ({self.settings['daily_loss']}%) reached. Positions are still open.")
            self.notify("SBF", "Daily loss limit reached. Trading halted.", sound="problem")
            self.save_state()

    # ---------------- trading ----------------
    def trade_size(self, c, mult=1.0):
        spend = min(self.start_cash * self.settings["trade_pct"] / 100 * mult, self.cash)
        room = self.start_cash * self.settings["max_coin_pct"] / 100 - sum(x for k, x in self.cost.items() if tick_of(k) == tick_of(c))
        return max(min(spend, room), 0)

    def buy(self, c, px, reason="signal", mult=1.0, genes=None):
        """A buy signal: buy shares, unless this book already holds the stock."""
        if is_scalp_book(c) and not (genes or {}).get("_scalp"):
            return
        if self.pos.get(c, 0) > 0:
            return
        self.open_position(c, px, reason, mult, genes)

    def sell_signal(self, c, px, reason="signal", mult=1.0, genes=None):
        """A sell signal: sell the shares this book holds. With nothing held there's nothing to do (SBF never sells
        short)."""
        if self.pos.get(c, 0) > 0:
            self.sell(c, px, reason)

    def driver_genes(self, c, voters):
        """The habits of the strongest bot voting. When scalpers carry the vote, the trade is a scalp with the
        scalpers' own exits (weighted by their say)."""
        if not voters or not self.tourn:
            return {}
        wt = lambda v: self.tourn.info.get(id(v), {}).get("weight", 0) or 1e-9
        sc = [v for v in voters if E.is_scalp(v.name)]
        tot, sw = sum(wt(v) for v in voters), sum(wt(v) for v in sc)
        if sc and sw * 2 > tot:
            top = max(sc, key=wt)
            bar = float(getattr(self, "session_bar", 0) or self.settings.get("bar_minutes", 1))
            g = dict(top.traits)
            g["_scalp"] = dict(sl=sum(wt(v) * v.risk["sl"] for v in sc) / sw,
                               tp=sum(wt(v) * v.risk["tp"] for v in sc) / sw,
                               mins=max(1.0, sum(wt(v) * v.traits.get("maxhold", 10) * v.tf for v in sc) / sw * bar),
                               flatby=max(v.traits.get("flatby", 15) for v in sc))
            return g
        top = max(voters, key=wt)
        return {k: x for k, x in top.traits.items() if k != "flatby"}

    def group_room(self, c, spend):
        """Tickers that move together: the account never puts more than max_group_pct of its money into a group of
        stocks whose prices have been moving in step (SPY and QQQ often move as one)."""
        pct = float(self.settings.get("max_group_pct", 0) or 0)
        if pct <= 0 or spend <= 0:
            return spend

        def rets(k, n=120):
            h = (self.hist.get(k) or [])[-n - 1:]
            return [h[i] / h[i - 1] - 1 for i in range(1, len(h)) if h[i - 1]]

        def corr(a, b):
            n = min(len(a), len(b))
            if n < 30:
                return 0.0
            a, b = a[-n:], b[-n:]
            ma, mb = sum(a) / n, sum(b) / n
            va, vb = sum((x - ma) ** 2 for x in a), sum((y - mb) ** 2 for y in b)
            return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (va * vb) ** 0.5 if va > 0 and vb > 0 else 0.0
        mine = rets(tick_of(c))
        group = [k for k in self.pos if tick_of(k) != tick_of(c) and self.pos.get(k, 0) > 0
                 and corr(mine, rets(tick_of(k))) >= 0.6]
        if not group:
            return spend
        room = self.start_cash * pct / 100 - sum(self.cost.get(k, 0) for k in group)
        if room < 1:
            if time.time() - getattr(self, "_group_note", 0) > 900:
                self._group_note = time.time()
                self.write(f"Skipped a buy of {tick_of(c)}: it has been moving together with "
                           f"{', '.join(dict.fromkeys(tick_of(k) for k in group))}, and that group already uses {pct:g}% "
                           f"of your money. (Settings > Basics > Tickers that move together)", "warn")
            return 0.0
        return min(spend, room)

    def open_position(self, c, S, reason="signal", mult=1.0, genes=None):
        """Buys shares for this book: in Practice at the live price plus half the spread, in LIVE through the broker.
        Fractional shares are fine, so any trade size works on any stock."""
        g = genes or {}
        t_ = tick_of(c)  # c is the account's book ("SPY" or "SPY~scalp"), t_ the ticker
        if not S or S <= 0:
            return
        spend = self.group_room(c, self.trade_size(c, mult))
        if spend < 1:
            return
        scalp = g.get("_scalp")
        if scalp and not (scalp["flatby"] < E.minutes_to_close(time.time()) <= 390):
            return  # scalps only open during the trading day, never in the scalpers' last minutes
        if self.live:
            if spend < MIN_LIVE_ORDER:
                return
            if self.pdt_blocked():
                if time.time() - self._pdt_note.get("_open", 0) > 1800:
                    self._pdt_note["_open"] = time.time()
                    self.write("No new buys today: the account has used its 3 day trades for this 5-day window and is "
                               "under $25,000, so a new position couldn't be sold today if it went wrong.", "warn")
                return
            self.cash -= spend  # reserved until the fill comes back
            self.pending.add(c)
            if not hasattr(self, "scalp_next"):
                self.scalp_next = {}
            if scalp:
                self.scalp_next[c] = dict(scalp)
            else:
                self.scalp_next.pop(c, None)
            self.executor.submit(self.live_open, c, S, spend, reason)
            return
        fee, slip = E.cost_model(self.settings)
        fill = S * (1 + slip)
        qty = spend * (1 - fee) / fill
        self.fees_paid += spend * fee + qty * S * slip
        self.cash -= spend
        if scalp:
            self.opt[c] = dict(scalp=dict(scalp, at=time.time()))
        else:
            self.opt.pop(c, None)
        self.pos[c] = qty
        self.cost[c] = spend
        self.log_trade("buy", c, qty, fill, spend, None, reason)
        self.mirror_open(c, qty, reason)

    def sell(self, c, px, reason="signal"):
        """Sell everything this book holds."""
        qty = self.pos.get(c, 0)
        if qty <= 0 or c in self.pending:
            return
        if self.live and reason not in self.SAFETY_EXITS and self.is_day_trade(c) and self.pdt_blocked():
            if time.time() - self._pdt_note.get(c, 0) > 1800:
                self._pdt_note[c] = time.time()
                self.write(f"Holding {self.contract_label(c)} until tomorrow: selling it today would be a 4th day trade "
                           f"in 5 business days, and the account is under $25,000 (Settings > Basics > Day-trade guard). "
                           f"Safety stops still sell it.", "warn")
            return
        if self.live:
            self.pending.add(c)
            self.executor.submit(self.live_close, c, qty, reason)
            return
        value = self.position_price(c)
        fee, slip = E.cost_model(self.settings)
        fill = value * (1 - slip)
        proceeds = qty * fill * (1 - fee)
        self.fees_paid += qty * value * slip + qty * fill * fee
        pl = proceeds - self.cost.get(c, 0)
        self.cash += proceeds
        label = self.contract_label(c)
        self.pos[c], self.cost[c] = 0, 0
        self._closing_label = label
        self.log_trade("sell", c, qty, fill, proceeds, pl, reason)
        self.opt.pop(c, None)
        self.mirror_close(c, reason)

    def log_trade(self, side, c, qty, px, usd, pl, reason):
        acct_ = self.acct_name()
        row = dict(time=datetime.now().isoformat(timespec="seconds"), mode={"PRACTICE": "paper", "ALPACA PAPER": "alpaca"}.get(acct_, "live"),
                   acct=acct_,
                   side=side, coin=c, qty=round(qty, 8), price=round(px, 6), usd=round(usd, 2),
                   pl="" if pl is None else round(pl, 2), reason=reason, who=self.pending_who.pop(c, ""))
        row["coin"] = tick_of(c)
        if is_scalp_book(c):
            row["coin"] += " (scalp)"
        detail = self.pending_detail.pop(c, "")
        if not detail and reason not in ("signal", "add-on"):
            detail = {"stop-loss": "Your account's safety stop was hit.", "take-profit": "Your account locked in its profit target.",
                      "trailing-stop": "Your trailing stop was hit.", "manual": "You placed this trade yourself."}.get(reason, "")
        if detail:
            row["detail"] = detail
        uids = self.pending_voters.pop(c, [])
        if side == "buy" and uids:
            alloc = self.bot_alloc.setdefault(c, {})
            for u in uids:
                alloc[u] = alloc.get(u, 0.0) + usd / len(uids)
        elif side == "sell" and self.pos.get(c, 0) <= 0:
            self.bot_alloc.pop(c, None)
        self.trades.append(row)
        if side == "buy":
            self.high[c] = max(self.high.get(c, px), px)
            self.bought_at.setdefault(c, time.time())
        elif self.pos.get(c, 0) <= 0:
            self.high.pop(c, None)
            self.bought_at.pop(c, None)
        try:
            E.append_trade_csv(row)
        except OSError:
            pass
        self.save_state()  # cash and positions have already changed, so persist them before any screen code runs
        # Each screen update is isolated: one failing widget must not hide the trade or freeze the balances.
        def trades_tab():
            try:
                visible = self.cur_tab() == "Trades"
            except Exception:
                visible = True
            if visible:
                self.refresh_trades()
            else:
                self._trades_dirty = True  # rebuilt when you open the Trades tab
        for step, name in ((trades_tab, "updating the Trades tab"),
                           (self.refresh_view, "updating the balances")):
            try:
                step()
            except Exception:
                note = self._record_error(name)
                if note:
                    self.write(note, "bad")
        why = REASON_TEXT.get(reason, reason)
        label = self.contract_label(c) if side == "buy" else (getattr(self, "_closing_label", None) or self.contract_label(c))
        self._closing_label = None
        row["contract"] = label
        if side == "buy":
            msg = f"Bought {qty:g} x {label} at ${px:,.2f} a share (${usd:,.2f} in all) ({why})"
        else:
            msg = f"Sold {qty:g} x {label} at ${px:,.2f} for ${usd:,.2f}, {'made' if (pl or 0) >= 0 else 'lost'} ${abs(pl or 0):,.2f} ({why})"
        self.write(f"{self.acct_name()}: " + msg)
        self.notify("SBF", f"{side.upper()} {label} @ ${px:,.2f} ({why})", important=False, sound="trade")
        if self.live and self.settings.get("why_popup", True):
            self.toast(f"{self.acct_name()} {side.upper()} {label}: why?", (detail or why)[:400], ms=20000)
        self.alert("trades", f"{self.acct_name()} " + msg)

    def sell_all(self):
        if not any(q > 0 for q in self.pos.values()):
            return messagebox.showinfo("Sell all", "No positions held.")
        if self.live and not messagebox.askyesno("Sell all", "Place REAL sell orders for all positions?"):
            return
        for c, q in list(self.pos.items()):
            if q > 0 and tick_of(c) in self.last and c not in self.pending:
                self.sell(c, self.last[tick_of(c)], "manual")
        self.refresh_view()

    # ---------------- live trading (Alpaca) ----------------
    def account_action(self):
        if self.client:
            self.logout()
        else:
            self.open_login()

    def auto_login(self):
        creds = E.load_login()
        try:
            if creds and E.migrate_paper_sessions(creds[2]):
                if self.cfg.get("last_mode") == "live" and not self.cfg.get("last_tag"):
                    self.cfg["last_tag"] = "alpaca"
                self.write("Your saved Alpaca paper session now lives under PRACTICE > 'My Alpaca paper account' "
                           "(it used to sit behind LIVE). LIVE is for real money only.", "good")
        except Exception:
            E.note_error("auto_login")
        if creds and not self.client:
            self.write("Saved Alpaca login found, connecting...")
            self.start_login(*creds, remember=False, saved=True)

    def open_login(self):
        if self.client:
            return messagebox.showinfo("Login", "Already logged in. Use Account > Log out to switch keys.")
        w = tk.Toplevel(self.root)
        w.title("Log in to Alpaca")
        w.resizable(False, False)
        f = ttk.Frame(w, padding=16)
        f.pack()
        ttk.Label(f, text="Log in with an Alpaca API key", font=("", 12, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(f, wraplength=500, foreground=MUTED, text=(
            "SBF uses Alpaca for stock prices and (in LIVE mode) orders. A free Alpaca account comes with "
            "a PAPER account full of pretend money: on alpaca.markets, open the Paper account, find 'API Keys' and "
            "generate a key. Copy the Key ID and the Secret here (the secret is only shown once). Practice mode needs "
            "the key too, for prices. Never share your secret with anyone.")).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 10))
        kind = tk.StringVar(value="paper")
        kr = ttk.Frame(f)
        kr.grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(kr, text="Paper account (pretend money at Alpaca, keys start with PK)", value="paper",
                        variable=kind).pack(anchor="w")
        ttk.Radiobutton(kr, text="Live account (REAL money, keys start with AK)", value="live", variable=kind).pack(anchor="w")
        ttk.Label(f, text="API Key ID").grid(row=3, column=0, sticky="w", pady=(10, 0))
        key_e = ttk.Entry(f, width=60)
        key_e.grid(row=4, column=0, columnspan=2, sticky="ew")
        ttk.Label(f, text="Secret key").grid(row=5, column=0, sticky="w", pady=(8, 0))
        sec_e = ttk.Entry(f, width=60, show="*")
        sec_e.grid(row=6, column=0, columnspan=2, sticky="ew")
        remember = tk.BooleanVar(value=E.keyring_available())
        can_keep = E.keyring_available()
        ttk.Checkbutton(f, text="Remember on this computer (OS keychain)" if can_keep else
                        "Remember (needs the secure-storage add-on: Tools > Install add-ons)", variable=remember,
                        state="normal" if can_keep else "disabled").grid(row=7, column=0, sticky="w", pady=(8, 0))

        def go():
            key, secret = key_e.get().strip(), sec_e.get().strip()
            ok, msg = E.check_credentials(key, secret)
            if not ok:
                return messagebox.showerror("Login", msg, parent=w)
            if kind.get() == "paper" and key.startswith("AK") and not messagebox.askyesno(
                    "Login", "This looks like a LIVE key (AK...) but you chose Paper. Continue anyway?", parent=w):
                return
            w.destroy()
            self.start_login(key, secret, kind.get() == "paper", remember.get())
        row = ttk.Frame(f)
        row.grid(row=8, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(row, text="Cancel", command=w.destroy).pack(side="left", padx=4)
        ttk.Button(row, text="Log in", style="Accent.TButton", command=go).pack(side="left")

    def start_login(self, key, secret, paper=True, remember=False, saved=False):
        self.logging_in = True
        self.update_status()
        threading.Thread(target=self._connect, args=(key, secret, paper, remember, saved), daemon=True).start()

    def _connect(self, key, secret, paper, remember, saved=False):
        creds = dict(key=key, secret=secret, paper=bool(paper))
        try:
            acct = E.alp_account(creds)
        except Exception as e:
            low = str(e).lower()
            if saved and ("401" in low or "403" in low or "unauthorized" in low or "forbidden" in low):
                E.clear_login()  # a dead key: forget it, so the next start doesn't fail the same way
                return self.q.put(("saved_login_rejected", E.explain_login_error(e, secret)))
            if saved:  # no answer yet (often right after the computer starts): try again shortly
                return self.q.put(("saved_login_retry", E.explain_login_error(e, secret)))
            return self.q.put(("login_failed", E.explain_login_error(e, secret)))
        E.ALPACA.update(creds)
        if remember and not E.save_login(key, secret, paper):
            self.q.put(("log", "No OS keychain available, so the login was not saved."))
        self.q.put(("connected", (creds, self.read_balances(acct))))

    def logout(self):
        if self.running and self.live:
            return messagebox.showinfo("Log out", "Stop trading first.")
        E.clear_login()
        E.ALPACA.update(key="", secret="")
        self.client, self.usd_avail = None, 0.0
        self.update_status()
        self.write("Logged out. Saved login removed from this computer.")

    def confirm_live(self, resume):
        if not self.client:
            messagebox.showerror("Live mode", "Log in to Alpaca first (Account > Log in).")
            return False
        min_budget = MIN_LIVE_ORDER / (self.settings["trade_pct"] / 100)
        if self.cash > self.usd_avail or (not resume and self.cash < min_budget):
            messagebox.showerror(
                "Live mode", f"Budget must be at least ${min_budget:.0f} (so one trade is ${MIN_LIVE_ORDER:.0f}+) "
                             f"and no more than your available {self.quote} (${self.usd_avail:,.2f}).")
            return False
        warn = ("\nWildcard picks strategies automatically based on recent results. "
                "Recent winners often stop winning" + (", and with 'Winners trade more' on it trades more often, "
                "which means more fees" if self.more_var.get() else "") + ".\n" if self.wild_var.get() else "")
        if self.cfg.get("live_locked"):
            ans = simpledialog.askstring("LIVE is locked", "The kill switch locked LIVE mode.\n\nType UNLOCK to allow live "
                                                           "trading again:")
            if (ans or "").strip() != "UNLOCK":
                return False
            self.cfg["live_locked"] = False
            E.save_json(CONFIG_PATH, self.cfg)
        roster = self.roster("live")
        self.grad_only = False
        if self.wild_var.get() and roster in ("hof", "team") and self.settings.get("graduation", True):
            squad = E.team() if roster == "team" else self.hof()
            fails = [(b, self.graduated(b)[1]) for b in squad if not self.graduated(b)[0]]
            if fails:
                lines = "\n".join(f"- {b.get('nickname') or E.bot_nickname(E.bot_key(b))}: {', '.join(m)}" for b, m in fails[:8])
                more = f"\n...and {len(fails) - 8} more" if len(fails) > 8 else ""
                ans_g = messagebox.askyesnocancel(
                    "Graduation check", f"{len(fails)} of {len(squad)} bots haven't graduated to real money yet:\n\n"
                    f"{lines}{more}\n\nYes = trade only with the {len(squad) - len(fails)} that graduated\n"
                    f"No = trade with all of them anyway\nCancel = don't go live")
                if ans_g is None:
                    return False
                self.grad_only = bool(ans_g)
        who = ({"farm": "the WHOLE FARM (any winning bot, including brand-new ones). Tip: choose 'Hall of Fame only' "
                        "under Strategy to trade real money with only the bots you picked",
                "team": f"only YOUR TEAM ({len(E.team())} bots)",
                "hof": f"only YOUR HALL OF FAME ({len(self.hof())} bots you picked)"}[roster]
               if self.wild_var.get() else "the strategies you ticked")
        try:
            self.acct_info = E.alp_account() or {}
            if self.acct_info.get("trading_blocked") or self.acct_info.get("account_blocked"):
                messagebox.showerror("Live mode", "Alpaca says this account can't trade right now (trading is blocked). "
                                                  "Check the Alpaca dashboard.")
                return False
        except Exception:
            E.note_error("confirm_live")
        if E.ALPACA.get("paper", True):
            return messagebox.askyesno(
                "Trade your Alpaca PAPER account", f"This places share orders on your Alpaca PAPER account (pretend money "
                f"at Alpaca), using up to ${self.cash:,.2f}.\nBots allowed to trade: {who}.\n\nGo ahead?")
        ans = simpledialog.askstring(
            "Confirm LIVE trading",
            f"This will place REAL share orders on your Alpaca account using up to ${self.cash:,.2f}.\n"
            f"Bots allowed to trade: {who}.\n"
            f"Stocks can fall fast, and automated strategies can lose money.{warn}\nType LIVE to continue:")
        return (ans or "").strip() == "LIVE"

    def _stuck_order(self, oid, status):
        """An order the broker wouldn't confirm as cancelled: it could still fill, so say so loudly."""
        self.__dict__.setdefault("open_oids", set()).add(oid)  # the kill switch still cancels it
        name = self.brk().name
        self.q.put(("log", (f"Couldn't confirm order {oid} was cancelled ({name} says {status or '?'}). "
                            f"Please check your {name} account: it could still fill.", "warn")))
        self.q.put(("call", lambda: self.notify("SBF order check", "An order couldn't be confirmed cancelled. "
                                                                 f"Please check your {name} account.", sound="problem")))

    def brk(self):
        """The broker LIVE mode and the paper copies trade through (broker.py)."""
        return B.get(self.broker())

    def live_open(self, c, S, spend, reason):
        """Buys shares through the broker: whole shares with a limit at the ask, part of a share at market."""
        try:
            t_ = tick_of(c)
            bk = self.brk()
            q = bk.quotes([t_]).get(t_)
            if q and q[0] > 0 and q[1] > 0:
                gap = (q[1] - q[0]) / ((q[0] + q[1]) / 2) * 100
                cap = float(self.settings.get("max_spread_pct", 0) or 0)
                if c in getattr(self, "scalp_next", {}):  # a scalp: only a tight bid/ask gap will do
                    cap = min(cap or E.SCALP_SPREAD[0], E.SCALP_SPREAD[0])
                if cap and gap > cap:
                    raise RuntimeError(f"{t_}'s bid/ask gap is {gap:.2f}% right now, wider than your {cap:g}% limit "
                                       f"(Settings > Basics > LIVE bid/ask gap)")
            got, avg = bk.buy_shares(t_, spend, self.settings.get("limit_wait", 20), self.settings.get("limit_fallback", True),
                                     self._stuck_order)
            if got <= 0:
                return self.q.put(("unfilled", ("buy", c, spend)))
            self.q.put(("fill", ("buy", c, got, got * avg, spend, reason)))
        except Exception as e:
            self.q.put(("fail", ("buy", c, spend, str(e))))

    def live_close(self, c, qty, reason):
        """Sells this book's shares through the broker: a limit at the bid for normal exits, market for stops."""
        try:
            urgent = reason in ("stop-loss", "manual", "trailing-stop", "scalp-flat", "kill switch", "daily-loss")
            got, avg = self.brk().sell_shares(tick_of(c), qty, urgent, self.settings.get("limit_wait", 20),
                                              self.settings.get("limit_fallback", True), self._stuck_order)
            if got <= 0:
                return self.q.put(("unfilled", ("sell", c, 0)))
            self.q.put(("fill", ("sell", c, got, got * avg, 0, reason)))
        except Exception as e:
            self.q.put(("fail", ("sell", c, 0, str(e))))

    def broker_held(self, sym):
        """Shares of this stock the broker says the account holds (the broker's own count)."""
        return self.brk().held(sym)

    # ---------------- broker choice ----------------

    def broker(self):
        """Which broker LIVE mode trades through. Alpaca for now; broker.py is where Schwab or IBKR would be added."""
        return "alpaca"

    def on_unfilled(self, side, c, reserved):
        """A limit order that found no taker in time and had no market fallback: not an error."""
        self.pending.discard(c)
        if side == "buy":
            getattr(self, "scalp_next", {}).pop(c, None)
        self.pending_who.pop(c, None)
        self.cash += reserved
        self.write(f"Limit {side} for {c} didn't fill in time, so nothing was traded "
                   f"(turn on the market fallback in Settings > Orders to always trade).", "warn")
        self.refresh_view()

    # ---------------- connection light / alerts ----------------
    def on_net(self, got, want, secs):
        if got == want:
            col, txt = (GREEN if secs < 1.5 else "#fbbf24"), f"Prices OK, {secs * 1000:.0f} ms for {want} coin{'s' if want != 1 else ''}"
        elif got:
            col, txt = "#fbbf24", f"Only {got} of {want} prices came back ({secs:.1f} s)"
        else:
            col, txt = RED, "No prices: Alpaca can't be reached right now"
        self.net_lbl.config(fg=col)
        self.net_tip.text = txt
        if not got and self.running:
            self.alert("problems", "SBF: can't reach Alpaca for prices.", once_key="net")

    def alert(self, kind, text, once_key=None):
        """Sends a phone alert (Discord/Telegram) in the background, if set up and wanted for this kind."""
        if not E.alerts_configured(self.settings) or not self.settings.get(f"alert_{kind}", True):
            return
        if once_key:
            last = getattr(self, "_alert_once", {}).get(once_key, 0)
            if time.time() - last < 1800:
                return
            self.__dict__.setdefault("_alert_once", {})[once_key] = time.time()
        s = dict(self.settings)

        def work():
            for p in E.send_alert(s, "SBF: " + text if not text.startswith("SBF") else text):
                self.q.put(("log", (f"Alert not sent: {p}", "warn")))
        threading.Thread(target=work, daemon=True).start()

    def on_fill(self, side, c, qty, amount, reserved, reason):
        """A broker order filled: qty shares for amount dollars in all."""
        self.fees_paid += amount * float(self.settings.get("fee_pct", 0.005)) / 100  # (the broker's own fees are tiny)
        self.pending.discard(c)
        self.fails = 0
        px = amount / qty if qty else 0
        if side == "buy":
            self.cash += reserved - amount
            sc_ = getattr(self, "scalp_next", {}).pop(c, None)
            if sc_:
                self.opt[c] = dict(scalp=dict(sc_, at=time.time()))
            self.pos[c] = self.pos.get(c, 0) + qty
            self.cost[c] = self.cost.get(c, 0) + amount
            self.log_trade("buy", c, qty, px, amount, None, reason)
        else:
            held = self.pos.get(c, 0)
            basis = self.cost.get(c, 0) * (min(qty, held) / held if held else 1)
            self.cash += amount
            self.pos[c] = max(held - qty, 0)
            self.cost[c] = max(self.cost.get(c, 0) - basis, 0)
            if self.pos[c] < 1e-9:
                self.pos[c], self.cost[c] = 0, 0
            self._closing_label = self.contract_label(c)
            self.log_trade("sell", c, qty, px, amount, amount - basis, reason)
            if self.pos[c] <= 0:
                self.opt.pop(c, None)
        self.refresh_view()

    def on_fail(self, side, c, reserved, err):
        self.pending.discard(c)
        if side == "buy":
            getattr(self, "scalp_next", {}).pop(c, None)
        self.pending_who.pop(c, None)
        self.cash += reserved
        self.fails += 1
        self.write(f"Order failed ({side} {c}): {err}")
        self.alert("problems", f"Order failed ({side} {c}): {err}")
        if self.fails >= MAX_FAILS and self.running:
            self.running = False
            self.ui_running(False)
            self.write(f"Stopped after {MAX_FAILS} consecutive order failures.")
            self.notify("SBF", "Stopped after repeated order failures.", sound="problem")
        self.refresh_view()

    # ---------------- add-on setup ----------------
    def maybe_setup(self):
        if E.missing_deps() and not self.cfg.get("skip_setup_prompt"):
            self.open_setup(auto=True)

    def open_setup(self, auto=False):
        if getattr(self, "setup_win", None) is not None and self.setup_win.winfo_exists():
            self.setup_win.lift()
            return
        w = self.setup_win = tk.Toplevel(self.root)
        w.title("Install add-ons")
        w.resizable(False, False)
        f = ttk.Frame(w, padding=16)
        f.pack()
        ttk.Label(f, text="Add-ons", font=("", 13, "bold")).pack(anchor="w")
        ttk.Label(f, wraplength=470, foreground=MUTED, text=(
            "Practice mode works without anything extra. These add-ons only make logging in more convenient. "
            "Installing them takes about a minute and needs an internet connection.")).pack(anchor="w", pady=(2, 10))
        self.setup_rows = {}
        for d in E.DEPENDENCIES:
            row = ttk.Frame(f)
            row.pack(fill="x", pady=2)
            mark = tk.Label(row, text="", width=3, font=("", 12, "bold"))
            mark.pack(side="left")
            col = ttk.Frame(row)
            col.pack(side="left", anchor="w")
            ttk.Label(col, text=d["label"], font=("", 10, "bold")).pack(anchor="w")
            ttk.Label(col, text=d["why"], foreground=MUTED, wraplength=420).pack(anchor="w")
            self.setup_rows[d["module"]] = mark
        self.setup_text = tk.Text(f, width=64, height=7, state="disabled", relief="flat", bg=CARD, font=("Courier", 8))
        self.setup_msg = ttk.Label(f, text="", wraplength=470, justify="left")
        self.setup_msg.pack(anchor="w", pady=(10, 2))
        self.setup_text.pack(pady=(0, 8))
        btns = ttk.Frame(f)
        btns.pack(fill="x")
        if auto:
            skip = tk.BooleanVar(value=False)

            def close_auto():
                if skip.get():
                    self.cfg["skip_setup_prompt"] = True
                    E.save_json(CONFIG_PATH, self.cfg)
                w.destroy()
            ttk.Checkbutton(btns, text="Don't remind me", variable=skip).pack(side="left")
            ttk.Button(btns, text="Not now", command=close_auto).pack(side="right", padx=(6, 0))
        else:
            ttk.Button(btns, text="Close", command=w.destroy).pack(side="right", padx=(6, 0))
        self.setup_btn = ttk.Button(btns, text="Install now", style="Accent.TButton", command=self.start_install)
        self.setup_btn.pack(side="right")
        self.refresh_setup()

    def refresh_setup(self):
        status = E.dep_status()
        for d, ok in status:
            self.setup_rows[d["module"]].config(text="\u2713" if ok else "\u2717", fg=GREEN if ok else RED)
        gone = [d for d, ok in status if not ok]
        self.setup_btn.config(state="normal" if gone else "disabled", text="Install now" if gone else "All installed")
        if not gone and not self.setup_msg.cget("text"):
            self.setup_msg.config(text="Everything is installed. You're ready for Live mode.")

    def start_install(self):
        specs = [d["pip"] for d in E.missing_deps()]
        if not specs:
            return self.refresh_setup()
        self.setup_btn.config(state="disabled", text="Installing...")
        self.setup_msg.config(text="Installing, please wait. This can take a minute...")
        self.setup_text.config(state="normal")
        self.setup_text.delete("1.0", "end")
        self.setup_text.config(state="disabled")
        self.write("Installing add-ons...")

        def work():
            ok, msg = E.install_packages(specs, lambda line: self.q.put(("dep_log", line)))
            self.q.put(("dep_done", (ok, msg)))
        threading.Thread(target=work, daemon=True).start()

    def setup_log(self, line):
        if getattr(self, "setup_win", None) is None or not self.setup_win.winfo_exists():
            return
        self.setup_text.config(state="normal")
        self.setup_text.insert("end", line + "\n")
        self.setup_text.see("end")
        self.setup_text.config(state="disabled")

    def setup_done(self, ok, msg):
        self.write(msg, "good" if ok else "bad")
        if getattr(self, "setup_win", None) is not None and self.setup_win.winfo_exists():
            self.setup_msg.config(text=msg, foreground=GREEN if ok else RED)
            self.refresh_setup()
        self.update_status()
        if ok and not self.client and self.mode_var.get() == "live":
            self.write("Add-ons ready. Click 'Log in to Alpaca' to continue.")

    # ---------------- backtest ----------------
    # ---------------- Bot Farm grid (default tab): eggs, rising, BEASTS, Hall of Fame ----------------
    GRID_STATUS = {"active": ("WINNING", "#22c55e"), "slowed": ("SHAKY", "#f59e0b"), "standby": ("STANDBY", "#3b82f6"),
                   "benched": ("RESTING", "#6b7280"), "warming up": ("LEARNING", "#8b5cf6")}
    DEN_COLORS = {"egg": ("#22c55e", "#14532d"), "rising": ("#3b82f6", "#1e3a8a"), "beast": ("#f97316", "#7c2d12")}
    DEN_PORTRAIT = {"egg": "#0c2416", "rising": "#0c1a36", "beast": "#2a1406"}
    SECTIONS = (("Eggs", "\u25d2 Eggs"), ("Rising", "\u25b2 Rising"), ("BEASTS", "\u265b BEASTS"),
                ("Hall of Fame", "\u2605 Hall of Fame"), ("All", "All"), ("Caught", "\u25c9 Library"))
    SECTION_TIPS = {
        "Eggs": "New bots still learning. They hatch once they've seen enough prices and made a few trades.",
        "Rising": "Hatched bots doing OK: shaky, on standby, or winning but not dominant yet. Resting ones are at the end.",
        "BEASTS": "The dominant ones: winning, steady (3 of the last 4 stretches or more) and ahead of the market. "
                  "The account follows these the most.",
        "Hall of Fame": "The bots YOU picked as champions. Only you add or remove them (right-click any bot). "
                        "'Bots trading: Hall of Fame only' trades with just these.",
        "All": "Every bot on the farm.", "Caught": "Bots on the farm that are saved in your Bot Library."}

    def tier(self, r):
        """egg / rising / beast for a scoreboard row."""
        v = r["v"]
        if r["status"] == "warming up" or v.age < (self.tourn.W if self.tourn else 100):
            return "egg"
        if r["status"] == "active" and ((r.get("cons") or 0) >= 0.75 or v.streak >= 30) and (r.get("edge") is None or r["edge"] > 0):
            return "beast"
        return "rising"

    def build_grid_tab(self, parent):
        top = tk.Frame(parent, bg=BG)
        top.pack(fill="x", padx=6, pady=(6, 2))
        chips = tk.Frame(top, bg=BG)
        chips.pack(side="left")
        self.grid_filter = tk.StringVar(value=self.cfg.get("grid_filter", "BEASTS"))
        if self.grid_filter.get() not in dict(self.SECTIONS):
            self.grid_filter.set("BEASTS")
        self.grid_chips = {}
        for key, label in self.SECTIONS:
            l = tk.Label(chips, text=label, bg=CARD, fg=MUTED, padx=12, pady=4, cursor="hand2", font=("", 9, "bold"))
            l.pack(side="left", padx=(0, 3))
            l.bind("<Button-1>", lambda e, n=key: self._grid_set_filter(n))
            Tip(l, self.SECTION_TIPS[key])
            self.grid_chips[key] = l
        self.catch_btn = ttk.Button(top, text="Catch!", style="Accent.TButton", command=self.catch_selected)
        self.catch_btn.pack(side="right")
        # second row: actions on the left, view controls on the right
        tools = tk.Frame(parent, bg=BG)
        tools.pack(fill="x", padx=6, pady=(2, 2))
        db = ttk.Button(tools, text="\u270e Design an egg", style="Plain.TButton", command=self.open_designer)
        eb = ttk.Button(tools, text="+ Add eggs", style="Plain.TButton", command=self.add_eggs)
        tb = ttk.Button(tools, text="\u2694 Train", style="Plain.TButton", command=self.open_training)
        self.roam_btn = ttk.Button(tools, text="\u25c7 Free roam: ON" if self.cfg.get("roam_on") else "\u25c7 Free roam",
                                   style="Plain.TButton", command=self.open_roam)
        self.camp_btn = ttk.Button(tools, text="\u2694 Auto camp: ON" if self.cfg.get("auto_camp") else "\u2694 Auto camp",
                                   style="Plain.TButton", command=self.toggle_auto_camp)
        Tip(self.camp_btn, "Auto training camp: while your farm runs, it keeps picking stretches of past prices by itself, "
                           "runs a practice farm through each (at most 1,500 decisions per stretch), then teaches your "
                           "farm what it learned and brings over the best bot that passed its exam. Starts OFF each "
                           "time the app opens; Essentials only keeps it off.")
        kb = ttk.Button(tools, text="\U0001f4d6 Knowledge", style="Plain.TButton", command=self.open_knowledge)
        Tip(kb, "Everything your farm has learned: see it, save it as a book, and add any of it to another farm.")
        xb = ttk.Button(tools, text="\u21e9 Export", style="Plain.TButton", command=self.export_bots)
        pb = ttk.Button(tools, text="Preset \u25be", style="Plain.TButton")
        pb.configure(command=lambda: self.preset_menu(pb))
        for b in (db, eb, tb, self.roam_btn, self.camp_btn, kb, xb, pb):
            b.pack(side="left", padx=(0, 6))
        Tip(db, "Write your own bot: strategy, numbers (like RSI buy below 28 / sell above 72), pace, exits and habits. (Ctrl+E)")
        Tip(eb, "Hatch brand-new random bots now. They learn on recent prices and have to earn their place.")
        Tip(tb, "Training camp: run a whole farm through days or months of past prices, several times over, then test "
                "the best on recent prices they never saw. Bring the graduates to your farm or Library.")
        Tip(self.roam_btn, "Free roam: switch it on and the farm goes back in time by itself, choosing its own stretches "
                           "of the past and candle sizes to learn from. Opens a screen showing where it's learning.")
        Tip(xb, "Export bots to a file: the whole farm, a den, the Hall of Fame, the Library or the ones you selected.")
        Tip(pb, "Farm presets: Calm (few bots, patient), Balanced (default) or Wild (big, fast-evolving farm).")
        self.farm_size = tk.StringVar(value=str(int(self.settings.get("max_bots", 30))))
        fs = ttk.Spinbox(tools, from_=14, to=200, increment=2, width=5, textvariable=self.farm_size, command=self.set_farm_size)
        fs.pack(side="right")
        fs.bind("<Return>", lambda e: self.set_farm_size())
        fs.bind("<FocusOut>", lambda e: self.set_farm_size())
        tk.Label(tools, text="Farm size", bg=BG, fg=MUTED, font=("", 8)).pack(side="right", padx=(0, 4))
        Tip(fs, "Most bots on the farm at once (14 to 200). More bots explore more ideas but use more computer power; "
                "the farm grows toward this number as it evolves, or press '+ Add eggs'.")
        self.farm_style = tk.StringVar(value=self.cfg.get("farm_style", "Dragons"))
        vb = ttk.Combobox(tools, textvariable=self.farm_style, values=["Dragons", "Simple cards", "List"], state="readonly", width=12)
        vb.pack(side="right", padx=(0, 10))
        vb.bind("<<ComboboxSelected>>", lambda e: self.set_farm_style())
        tk.Label(tools, text="View", bg=BG, fg=MUTED, font=("", 8)).pack(side="right", padx=(0, 4))
        self.motion_var = tk.StringVar(value=self.settings.get("animations", "Full"))
        mb = ttk.Combobox(tools, textvariable=self.motion_var, values=["Full", "Light", "Off"], state="readonly", width=5)
        mb.pack(side="right", padx=(0, 10))
        mb.bind("<<ComboboxSelected>>", lambda e: self._set_motion())
        tk.Label(tools, text="Motion", bg=BG, fg=MUTED, font=("", 8)).pack(side="right", padx=(0, 4))
        Tip(mb, "Full: smooth animation. Light: about 2 frames a second, uses very little computer power. Off: still pictures.")
        Tip(vb, "Dragons: animated cards. Simple cards: the same cards without animation. List: a plain table.")
        srow = tk.Frame(parent, bg=BG)
        srow.pack(fill="x", padx=8)
        self.grid_search = tk.StringVar()
        se = tk.Entry(srow, textvariable=self.grid_search, width=24, bg=CARD, fg=TEXT, insertbackground=TEXT,
                      relief="flat", highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT)
        se.pack(side="right", ipady=2)
        tk.Label(srow, text="\u2315 Search", bg=BG, fg=MUTED, font=("", 8)).pack(side="right", padx=(0, 4))
        Tip(se, "Find dragons by name, strategy, trait, never-rule or study. Esc clears it.")
        self.grid_search.trace_add("write", lambda *a: self.soon(self.grid_refresh, 150))
        se.bind("<Escape>", lambda e: self.grid_search.set(""))
        self.grid_sum = tk.Label(srow, text="", bg=BG, fg=MUTED, font=("", 9), anchor="w")
        self.grid_sum.pack(side="left", fill="x", expand=True)
        self.pulse_lbl = tk.Label(parent, text="", bg=CARD, fg=TEXT, font=("", 8, "bold"), anchor="w", padx=8, pady=3)
        self.pulse_lbl.pack(fill="x", padx=8, pady=(2, 0))
        Tip(self.pulse_lbl, "Farm pulse: births and retirements in the last hour, the current season, the best bot of the "
                            "hour, and how your Hall of Fame would be doing on its own (Shadow Live).")
        self.grid_area = tk.Frame(parent, bg=BG)
        self.grid_area.pack(fill="both", expand=True)
        self.grid = tk.Canvas(self.grid_area, bg=BG, highlightthickness=0)
        ys = ttk.Scrollbar(self.grid_area, orient="vertical", command=self.grid.yview)
        self.grid.configure(yscrollcommand=lambda a, b: (ys.set(a, b), self._grid_scrolled()))
        self.grid_ys = ys
        ys.pack(side="right", fill="y")
        self.grid.pack(fill="both", expand=True, padx=(6, 0), pady=(2, 4))
        self.grid_list = None
        self.grid.bind("<Configure>", lambda e: self.later(self.grid_refresh, 180))
        self.grid.bind("<Button-1>", self._grid_click)
        self.grid.bind("<Double-1>", lambda e: (self._grid_click(e), self.bot_detail() if self.selected_variant() else None))
        self.grid.bind("<Motion>", self._grid_motion)
        self.grid.bind("<B1-Motion>", self._grid_drag)
        self.grid.bind("<ButtonRelease-1>", self._grid_drop)
        for btn in ("<Button-3>", "<Button-2>"):
            self.grid.bind(btn, lambda e: (lambda card: self.bot_menu(e, card[4]) if card else None)(self._grid_at(e)))
        self.grid.bind("<Leave>", lambda e: setattr(self, "grid_hover", None))
        for ev, d in (("<Button-4>", -1), ("<Button-5>", 1)):
            self.grid.bind(ev, lambda e, d=d: self.grid.yview_scroll(d, "units"))
        self.grid.bind("<MouseWheel>", lambda e: self.grid.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.grid_cards, self.grid_hover, self.beasts_seen = [], None, set()
        self._grid_paint_chips()
        self.root.after(300, self.animate_all)

    def _set_motion(self):
        self.settings["animations"] = self.motion_var.get()
        self.cfg["settings"] = self.settings
        E.save_json(CONFIG_PATH, self.cfg)

    def set_farm_size(self):
        try:
            n = int(float(self.farm_size.get()))
        except ValueError:
            return
        n = min(max(n, 14), 200)
        self.farm_size.set(str(n))
        if n == int(self.settings.get("max_bots", 30)):
            return
        self.settings["max_bots"] = n
        self.cfg["settings"] = self.settings
        E.save_json(CONFIG_PATH, self.cfg)
        if self.tourn:
            self.tourn.cap = n
            self.tourn.family_cap = max(5, -(-n // 4))
            while len(self.tourn.vars) > n:  # shrink: let the weakest go
                worst = [v for v, _ in reversed(self.tourn.ranking()) if v not in self.tourn.active] or \
                        [v for v in self.tourn.vars if self.tourn.info.get(id(v), {}).get("status") == "warming up"]
                if not worst:
                    break
                self.tourn._remove(worst[0], "farm made smaller")
            self.tourn._rate()
            self.refresh_board()
        self.write(f"Farm size: up to {n} bots." + (" It grows toward this as it evolves, or press '+ Add eggs'." if self.tourn else ""))

    def add_eggs(self):
        if not self.tourn or not self.seeded:
            return messagebox.showinfo("Add eggs", "Start the bot with 'Let the bot choose (Wildcard)' ticked first.")
        room = self.tourn.cap - len(self.tourn.vars)
        n = simpledialog.askinteger("Add eggs", f"How many new eggs? (room for {max(room, 0)} more; raise Farm size for more)",
                                    initialvalue=max(1, min(5, room)) if room > 0 else 0, minvalue=0, maxvalue=200)
        if not n:
            return
        if n > room:
            self.tourn.cap = len(self.tourn.vars) + n
            self.settings["max_bots"] = min(200, self.tourn.cap)
            self.farm_size.set(str(self.settings["max_bots"]))
            self.cfg["settings"] = self.settings
            E.save_json(CONFIG_PATH, self.cfg)
        closes = {c: self.hist[c] for c in self.session_coins}
        made = 0
        for _ in range(n):
            if self.tourn.spawn_random(closes):
                made += 1
        self.flush_events()
        self.refresh_board()
        self.write(f"\u25d2 {made} new egg{'s' if made != 1 else ''} added to the farm.", "good")

    def set_farm_style(self):
        st = self.farm_style.get()
        self.cfg["farm_style"] = st
        E.save_json(CONFIG_PATH, self.cfg)
        if st == "List":
            self.grid.pack_forget()
            self.grid_ys.pack_forget()
            if self.grid_list is None:
                box = tk.Frame(self.grid_area, bg=BG)
                self.grid_list_box = box
                self.grid_list = self.make_tree(box, ("tier", "name", "strategy", "status", "recent", "edge", "winrate", "trades", "saved"),
                                                (80, 140, 190, 140, 70, 75, 65, 55, 110),
                                                ("Section", "Name", "Strategy", "Status", "Recent", "vs the market", "Win rate",
                                                 "Trades", "Saved"))
                self.grid_list.bind("<Double-1>", lambda e: self.bot_detail(self._list_variant(), quiet=True)
                                    if self.on_row(self.grid_list, e) else None)
                self.grid_list.bind("<<TreeviewSelect>>", lambda e: self._list_select())
                for btn in ("<Button-3>", "<Button-2>"):
                    self.grid_list.bind(btn, lambda e: (self.grid_list.selection_set(self.grid_list.identify_row(e.y))
                                                        if self.grid_list.identify_row(e.y) else None,
                                                        self.bot_menu(e, self._list_variant())))
            self.grid_list_box.pack(fill="both", expand=True, padx=6, pady=(2, 4))
        else:
            if self.grid_list is not None:
                self.grid_list_box.pack_forget()
            self.grid_ys.pack(side="right", fill="y")
            self.grid.pack(fill="both", expand=True, padx=(6, 0), pady=(2, 4))
        if hasattr(self, "farm"):
            if st == "Dragons":
                if not self.farm.winfo_ismapped():
                    self.farm.pack(fill="x", padx=4, pady=(0, 4), after=self.best_lbl)
            else:
                self.farm.pack_forget()
        self.grid_refresh()

    def _list_variant(self):
        sel = self.grid_list.selection() if self.grid_list is not None else ()
        if not sel or not self.tourn:
            return None
        return next((v for v in self.tourn.vars if str(id(v)) == sel[0]), None)

    def _list_select(self):
        v = self._list_variant()
        if v and self.board.exists(str(id(v))):
            self.board.selection_set(str(id(v)))

    def _fill_grid_list(self, items):
        t = self.grid_list
        sel = t.selection()
        t.delete(*t.get_children())
        lib, hofk = self.library_keys(), self.hof_keys()
        names = {"egg": "Egg", "rising": "Rising", "beast": "BEAST"}
        for kind, data, extra in items:
            if kind == "hof":
                b = data
                lt = b.get("lifetime") or {}
                t.insert("", "end", values=("Hall of Fame", E.bot_nickname(E.bot_key(b)), STRAT_LABELS.get(b["name"], b["name"]),
                                            "on the farm" if extra else "resting",
                                            "-", "-" if lt.get("vs_holding_pct") is None else f"{lt['vs_holding_pct']:+.1f}%",
                                            "-" if lt.get("win_rate_pct") is None else f"{lt['win_rate_pct']:.0f}%",
                                            lt.get("trades", "-"), "\u2605 HoF"))
                continue
            r, tier = data, extra
            v, lf = r["v"], r["life"]
            saved = ("\u2605 HoF" if v.key() in hofk else "") + (" \u25c9 Library" if v.key() in lib else "")
            t.insert("", "end", iid=str(id(v)), tags=(STATUS_TAG.get(r["status"], "learning"),), values=(
                names[tier], E.bot_nickname(v.key()), friendly_label(v.name, v.params, v.tf, self.session_bar),
                STATUS_TEXT.get(r["status"], r["status"]), pct_txt(r["score"]), pct_txt(r.get("edge")),
                "-" if lf["winrate"] is None else f"{lf['winrate'] * 100:.0f}%", lf["trades"], saved.strip()))
        amber = "#fbbf24" if DARK else "#b45309"
        for tag, color in (("win", GREEN), ("shaky", amber), ("standby", TEXT), ("stopped", MUTED), ("learning", MUTED)):
            t.tag_configure(tag, foreground=color)
        keep = [i for i in sel if t.exists(i)]
        if keep:
            t.selection_set(keep)
        resort(t)

    def _grid_scrolled(self):
        """Scrolling the farm: draw the newly visible cards once you get near the edge of what's drawn."""
        band = getattr(self, "_grid_drawn", None)
        if not band:
            return
        top, bot = self.grid.canvasy(0), self.grid.canvasy(self.grid.winfo_height())
        if top < band[0] + 60 or bot > band[1] - 60:
            self.soon(self.grid_refresh, 30)

    def _grid_set_filter(self, n):
        self.grid_filter.set(n)
        self.cfg["grid_filter"] = n
        self._grid_paint_chips()
        self.grid.yview_moveto(0)
        self.grid_refresh()

    def _grid_paint_chips(self):
        counts = getattr(self, "grid_counts", {})
        for n, l in self.grid_chips.items():
            on = n == self.grid_filter.get()
            base = dict(self.SECTIONS)[n]
            l.config(text=base + (f"  {counts[n]}" if n in counts else ""),
                     bg=({"BEASTS": "#c2410c", "Hall of Fame": "#a16207", "Eggs": "#15803d", "Rising": "#1d4ed8"}.get(n, ACCENT))
                     if on else CARD,
                     fg="white" if on else (("#f59e0b" if n in ("BEASTS", "Hall of Fame") else MUTED)))

    def _grid_items(self):
        """Cards to show: ('farm', row, tier) for live bots or ('hof', entry, in_farm) for Hall of Fame bots."""
        f = self.grid_filter.get()
        rows = self.tourn.board() if self.tourn else []
        tiers = [(r, self.tier(r)) for r in rows]
        hof = self.hof()
        self._hof_n = len(hof)
        self.grid_counts = {"Eggs": sum(1 for _, t in tiers if t == "egg"),
                            "Rising": sum(1 for _, t in tiers if t == "rising"),
                            "BEASTS": sum(1 for _, t in tiers if t == "beast"),
                            "Hall of Fame": getattr(self, "_hof_n", 0), "All": len(rows)}
        # promotions (egg -> rising -> BEAST) get a little animation
        rank = {"egg": 0, "rising": 1, "beast": 2}
        seen = self.__dict__.setdefault("tier_seen", {})
        promos = self.__dict__.setdefault("promos", {})
        for r, t in tiers:
            v = r["v"]
            old = seen.get(id(v))
            if old is not None and rank[t] > rank[old] and self.running and self.seeded:
                promos[id(v)] = ("roar" if t == "beast" else "hatch", getattr(self, "farm_phase", 0))
            seen[id(v)] = t
        # new BEASTS get announced
        for r, t in tiers:
            k = r["v"].key()
            if t == "beast" and k not in self.beasts_seen and self.running and self.seeded:
                self.beasts_seen.add(k)
                msg = (f"{E.bot_nickname(k)} became a BEAST! ({friendly_label(r['name'], r['params'], r.get('tf', 1), self.session_bar)}, "
                       f"{pct_txt(r['score'])} recently, {pct_txt(r.get('edge'))} vs the market)")
                self.write("\u265b " + msg, "good")
                self.notify("SBF", msg, sound="hof")
        q = self.grid_search.get().strip().lower() if hasattr(self, "grid_search") else ""
        if q:
            tiers = [(r, t) for r, t in tiers if q in self._search_text(r)]
        if f == "Hall of Fame":
            live = {r["v"].key(): r for r in rows}
            return [("hof", b, live.get(E.bot_key(b))) for b in hof
                    if not q or q in (E.bot_nickname(E.bot_key(b)) + " " + b["name"] + " "
                                      + STRAT_LABELS.get(b["name"], "")).lower()]
        if f == "Caught":
            have = self.library_keys()
            return [("farm", r, t) for r, t in tiers if r["v"].key() in have]
        want = {"Eggs": "egg", "Rising": "rising", "BEASTS": "beast"}.get(f)
        items = [("farm", r, t) for r, t in tiers if want is None or t == want]
        if f == "Rising":  # resting ones last
            items.sort(key=lambda it: it[1]["status"] == "benched")
        if f == "All":
            order = {"beast": 0, "rising": 1, "egg": 2}
            items.sort(key=lambda it: order[it[2]])
        return items

    def _search_text(self, r):
        v = r["v"]
        L = v.layers or {}
        studies = " ".join(sd["name"] + " " + STRAT_LABELS.get(sd["name"], "") for k in ("plus", "minus", "exits")
                           for sd in L.get(k, []))
        return " ".join((E.bot_nickname(v.key()), v.name, STRAT_LABELS.get(v.name, ""), E.traits_text(v.traits),
                         E.vetoes_text(v.vetoes), E.risk_text(v.risk), v.classic or "", studies, v.origin or "",
                         STATUS_TEXT.get(r["status"], ""), f"#{v.uid}")).lower()

    # ---- drag a card onto a den tab: Hall of Fame (promote) or Library (catch) ----
    def _grid_drag(self, e):
        card = self._grid_at(e) if not getattr(self, "_drag", None) else None
        if card and card[4] is not None and not getattr(self, "_drag", None):
            self._drag_start = getattr(self, "_drag_start", None) or (e.x, e.y, card[4])
        st = getattr(self, "_drag_start", None)
        if st and not getattr(self, "_drag", None) and abs(e.x - st[0]) + abs(e.y - st[1]) > 14:
            self._drag = st[2]
            self.grid.config(cursor="fleur")
            for k in ("Hall of Fame", "Caught"):
                self.grid_chips[k].config(bg="#16a34a", fg="white")
        if getattr(self, "_drag", None):
            g = getattr(self, "_drag_ghost", None)
            if g is None:
                g = self._drag_ghost = tk.Toplevel(self.root)
                g.overrideredirect(True)
                g.attributes("-topmost", True)
                tk.Label(g, text="\u265b " + E.bot_nickname(self._drag.key()) + "  \u2192 drop on Hall of Fame or Library",
                         bg=ACCENT, fg="white", padx=8, pady=3, font=("", 9, "bold")).pack()
            g.geometry(f"+{e.x_root + 14}+{e.y_root + 10}")

    def _grid_drop(self, e):
        v = getattr(self, "_drag", None)
        self._drag = self._drag_start = None
        g = getattr(self, "_drag_ghost", None)
        if g is not None:
            g.destroy()
            self._drag_ghost = None
        if v is None:
            return
        self.grid.config(cursor="")
        self._grid_paint_chips()
        target = self.root.winfo_containing(e.x_root, e.y_root)
        if target is self.grid_chips.get("Hall of Fame"):
            self.promote_variants([v])
        elif target is self.grid_chips.get("Caught"):
            rows = self.board_rows_of([v])
            self.catch_rows(E.export_bots(rows, self.version, note="caught by you")["bots"], "caught")
            self.refresh_board()

    @staticmethod
    def round_rect(c, x0, y0, x1, y1, r=12, **kw):
        pts = (x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1, x0, y1,
               x0, y1 - r, x0, y0 + r, x0, y0)
        return c.create_polygon(*pts, smooth=True, **kw)

    def _bar(self, c, x, y, w, name, frac, col):
        c.create_text(x, y + 4, anchor="w", text=name, fill=MUTED, font=("", 7, "bold"), tags="card")
        self.round_rect(c, x + 20, y, x + w, y + 8, 4, fill=LINE, outline="", tags="card")
        if frac > 0.02:
            self.round_rect(c, x + 20, y, x + 20 + (w - 20) * min(frac, 1), y + 8, 4, fill=col, outline="", tags="card")

    def _chips(self, c, x, y, xmax, chips):
        cx = x
        for txt, tc in chips:
            t = c.create_text(cx + 5, y, anchor="w", text=txt, fill=tc, font=("", 7, "bold"), tags="card")
            bb = c.bbox(t)
            if bb[2] > xmax:
                c.delete(t)
                break
            bgc = self.round_rect(c, bb[0] - 5, bb[1] - 2, bb[2] + 5, bb[3] + 2, 6, fill=BG, outline="", tags="card")
            c.tag_lower(bgc, t)
            cx = bb[2] + 9

    def grid_refresh(self):
        """Draws the cards (the still parts). Dragons and eggs are animated separately by grid_animate."""
        if not hasattr(self, "grid"):
            return
        if self.moving():  # resizing / dragging the window: draw once it settles
            return self.later(self.grid_refresh, 250)
        c = self.grid
        W = max(c.winfo_width(), 200)
        items = self._grid_items()
        self._grid_paint_chips()
        c.delete("card")
        self.grid_cards = []
        awake = self.running and self.seeded
        gc = self.grid_counts
        self.grid_sum.config(text=(f"\u25d2 {gc['Eggs']} eggs   \u25b2 {gc['Rising']} rising   "
                                   f"\u265b {gc['BEASTS']} BEASTS   \u2605 {gc['Hall of Fame']} in the Hall of Fame"
                                   + (f"   |   generation {max(v.gen for v in self.tourn.vars)}" if self.tourn and self.tourn.vars else "")
                                   + ("" if awake or not self.tourn else "   |   the farm is asleep (press Start)")
                                   + "   |   goal: make money after fees, and more than the market"))
        try:
            self.pulse_lbl.config(text=self.pulse_text())
        except Exception:
            pass
        f = self.grid_filter.get()
        style = self.farm_style.get()
        try:
            if "Bot Farm" not in self.cur_tab():
                return  # not visible: drawn when you open the tab
        except Exception:
            pass
        if style == "List" and self.grid_list is not None:
            self._fill_grid_list(items)
            return
        if not items:
            msgs = {"Eggs": "No eggs right now. New bots appear here while they learn.",
                    "Rising": "Nobody in the middle right now.",
                    "BEASTS": "No BEASTS yet. A bot becomes one when it keeps winning steadily and beats the market.\n"
                              "It can take a while, and that's the point: they have to earn it.",
                    "Hall of Fame": "The Hall of Fame is empty so far. Bots that stay winners for hours are saved here.",
                    "Caught": "None of the bots on the farm are in your collection yet. Select some and press Catch!"}
            msg = msgs.get(f) if self.tourn or f == "Hall of Fame" else (
                "Your dragons hatch here when the bot runs with 'Let the bot choose (Wildcard)' ticked.\n"
                "Press Start the bot. Each dragon is one trading bot.")
            c.create_text(W // 2, 90, text=msg or "", fill=MUTED, justify="center", tags="card", font=("", 10))
            c.configure(scrollregion=(0, 0, W, 200))
            return
        big = f == "BEASTS"
        cw, ch, g = (300, 180, 14) if big else (236, 160, 12)
        per = max(1, (W - g) // (cw + g))
        caught = self.library_keys()
        hofk = self.hof_keys()
        sel = set(self.board.selection())
        gold = "#f59e0b"
        vtop, vbot = c.canvasy(0), c.canvasy(max(c.winfo_height(), 300))
        self._grid_drawn = (vtop - (ch + g) * 2, vbot + (ch + g) * 2)  # drawn band (cards outside it are skipped)
        for i, (kind, data, extra) in enumerate(items):
            x = g + (i % per) * (cw + g)
            y = g + (i // per) * (ch + g)
            if y + ch < self._grid_drawn[0] or y > self._grid_drawn[1]:
                continue  # off screen: drawn when you scroll to it
            if kind == "hof":
                self._hof_card(c, x, y, cw, ch, data, extra)
                self.grid_cards.append((x, y, x + cw, y + ch, extra["v"] if extra else None, "hof", "hof", data["name"]))
                continue
            r, t = data, extra
            v, lf = r["v"], r["life"]
            label, col = self.GRID_STATUS.get(r["status"], ("?", MUTED))
            if t == "egg":
                label, col = f"EGG {int(min(1, len(v.eq) / max(1, self.tourn.W)) * 100)}%", "#8b5cf6"
            elif t == "beast":
                label, col = "\u265b BEAST", "#dc2626"
            if not awake:
                label, col = "ASLEEP", "#6b7280"
            rec = E.lifetime_record(lf, self.session_bar)
            rar = E.rarity_for(rec)
            rc = gold if t == "beast" else E.RARITY_COLORS[rar]
            xp = E.xp_for(rec)
            lvl = E.level_for(xp)
            lo_xp, hi_xp = 10 * (lvl - 1) ** 2, 10 * lvl ** 2
            is_sel = str(id(v)) in sel
            den, glow = self.DEN_COLORS[t]
            self.round_rect(c, x - 4, y - 4, x + cw + 4, y + ch + 4, 18, fill="", outline=glow,
                            width=4 if t == "beast" else 2, tags="card")  # den glow: green eggs, blue rising, orange BEASTS
            self.round_rect(c, x, y, x + cw, y + ch, 14, fill=CARD, outline=TEXT if is_sel else rc,
                            width=3 if (is_sel or t == "beast") else (2 if rar != "Common" else 1), tags="card")
            port = 92 if big else 76
            self.round_rect(c, x + 3, y + 3, x + port, y + port, 10, fill=self.DEN_PORTRAIT[t] if DARK else BG, outline="", tags="card")
            c.create_line(x + 16, y + 1, x + cw - 16, y + 1, fill=den, width=3, capstyle="round", tags="card")
            c.create_rectangle(x + cw - 92, y + 10, x + cw - 10, y + 28, fill=col, outline="", tags="card")
            c.create_text(x + cw - 51, y + 19, text=label, fill="white", font=("", 8, "bold"), tags="card")
            nick = E.bot_nickname(v.key())
            if style != "Dragons":  # plain badge instead of an animated dragon
                pc = col if awake else "#6b7280"
                c.create_oval(x + 14, y + 14, x + port - 10, y + port - 10, fill=pc, outline="", tags="card")
                c.create_text((x + 2 + port) / 2, (y + 2 + port) / 2, tags="card", fill="white", font=("", 13, "bold"),
                              text="".join(w[0] for w in nick.split()[:2]) if t != "egg" else "...")
            star = "★ " if v.key() in hofk else ""
            ball = " ◉" if v.key() in caught else ""
            tx = x + port + 8
            c.create_text(tx, y + 40, anchor="w", text=star + nick + ball, fill=gold if t == "beast" else TEXT,
                          font=("", 12 if big else 10, "bold"), tags="card")
            c.create_text(tx, y + 58, anchor="w", text=("\u2604 Kamikaze \u00b7 " if E.is_kami(v) else "") + STRAT_LABELS.get(v.name, v.name),
                          fill="#f97316" if E.is_kami(v) else MUTED, font=("", 8), tags="card")
            sub = "hatching..." if t == "egg" else f"every {E.fmt_bar(v.tf * self.session_bar)}  |  {rar}"
            if v.classic:
                sub = f"\u272a Classic: {v.classic}" + ("  \u26a0" if any(cl.get("controversial") and cl["title"] == v.classic
                                                                        for cl in E.CLASSICS) else "")
            c.create_text(tx, y + 72, anchor="w", text=sub, fill=rc, font=("", 8, "bold"), tags="card")
            c.create_text(x + 10, y + 15, anchor="w", text=f"LV {lvl}", fill=rc, font=("", 9, "bold"), tags="card")
            by = y + (port + 12)
            if t == "egg":
                prog = min(1, len(v.eq) / max(1, self.tourn.W))
                self._bar(c, x + 10, by, cw - 10, "HATCH", prog, "#8b5cf6")
                self._bar(c, x + 10, by + 13, cw - 10, "TRADES", min(1, len(v.sim.tbars) / max(1, self.tourn.min_trades)), "#a78bfa")
            else:
                hp = r.get("health", 1.0)
                self._bar(c, x + 10, by, cw - 10, "XP", (xp - lo_xp) / max(1, hi_xp - lo_xp), "#a855f7")
                self._bar(c, x + 10, by + 13, cw - 10, "HP", hp, GREEN if hp > 0.5 else "#f59e0b" if hp > 0.2 else RED)
                self._bar(c, x + 10, by + 26, cw - 10, "PWR", r.get("power", 0) / 10, "#f97316")
            chips = [(pct_txt(r["score"]), GREEN if (r["score"] or 0) > 0 else RED if r["score"] else MUTED),
                     ("vs mkt " + pct_txt(r.get("edge")), GREEN if (r.get("edge") or 0) > 0 else MUTED),
                     ("WR " + ("-" if lf["winrate"] is None else f"{lf['winrate'] * 100:.0f}%"), TEXT),
                     (f"{lf['trades']} trades", MUTED)]
            if t == "beast":
                chips.insert(0, (f"{r['stake']:.1f}x size", gold))
            if r.get("layers"):
                L_ = r["layers"]
                bits = [f"+{1 + len(L_.get('plus', []))}" if L_.get("plus") else "", f"-{len(L_['minus'])}" if L_.get("minus") else "",
                        f"x{len(L_['exits'])}" if L_.get("exits") else ""]
                if any(bits):
                    chips.append(("studies " + " ".join(b for b in bits if b), "#a78bfa"))
            badges = self.achievements(v, lf)
            if badges:
                chips.insert(0, (f"\u2726 {len(badges)}", "#facc15"))
            if self.tourn and int(self.tourn.o.get("islands", 1) or 1) > 1:
                chips.append((f"isle {v.island + 1}", MUTED))
            if r.get("vetoes"):
                good = sum(1 for k, s in (r.get("veto_stats") or {}).items() if s[0] >= 3 and s[1] > 0)
                chips.append((f"{len(r['vetoes'])} never-rule{'s' if len(r['vetoes']) != 1 else ''}"
                              + (f", {good} proven" if good else ""), "#38bdf8"))
            self._chips(c, x + 10, y + ch - 17, x + cw - 8, chips)
            self.grid_cards.append((x, y, x + cw, y + ch, v, r["status"], t, (KAMI_MARK + v.name) if E.is_kami(v) else v.name))
        nrows = (len(items) + per - 1) // per
        c.configure(scrollregion=(0, 0, W, nrows * (ch + g) + g))
        c.tag_raise("anim")  # keep the animated layer on top so nothing flickers when the cards are redrawn

    def _hof_card(self, c, x, y, cw, ch, b, live):
        gold = "#f59e0b"
        if self.farm_style.get() != "Dragons":
            c.create_oval(x + 14, y + 14, x + 66, y + 66, fill="#a16207", outline="", tags="card")
            c.create_text(x + 40, y + 40, text="\u2605", fill="white", font=("", 18, "bold"), tags="card")
        lt = b.get("lifetime") or {}
        self.round_rect(c, x - 3, y - 3, x + cw + 3, y + ch + 3, 17, fill="", outline="#713f12", width=3, tags="card")
        self.round_rect(c, x, y, x + cw, y + ch, 14, fill=CARD, outline=gold, width=2, tags="card")
        self.round_rect(c, x + 3, y + 3, x + 76, y + 76, 10, fill="#2a2006", outline="", tags="card")
        pill, pc = ("ON THE FARM", "#16a34a") if live else ("RESTING", "#a16207")
        rt = b.get("retest") or {}
        if rt.get("rusty"):
            pill, pc = "RUSTY", "#78350f"
        ok, _miss = self.graduated(b)
        c.create_text(x + cw - 10, y + ch - 12, anchor="e", text="\u2713 graduated" if ok else "not graduated",
                      fill=GREEN if ok else MUTED, font=("", 7, "bold"), tags="card")
        c.create_rectangle(x + cw - 92, y + 10, x + cw - 10, y + 28, fill=pc, outline="", tags="card")
        c.create_text(x + cw - 51, y + 19, text=pill, fill="white", font=("", 8, "bold"), tags="card")
        c.create_text(x + 84, y + 40, anchor="w", text="\u2605 " + E.bot_nickname(E.bot_key(b)), fill=gold,
                      font=("", 10, "bold"), tags="card")
        c.create_text(x + 84, y + 57, anchor="w", text=STRAT_LABELS.get(b["name"], b["name"]), fill=MUTED, font=("", 8), tags="card")
        when = str(b.get("inducted_at", ""))[:10]
        c.create_text(x + 84, y + 71, anchor="w", text=f"since {when}" if when else "champion", fill=gold, font=("", 8, "bold"), tags="card")

        def p(k):
            v = lt.get(k)
            return "-" if v is None else f"{v:+.1f}%"
        c.create_text(x + 10, y + 92, anchor="w", fill=TEXT, font=("", 8), tags="card",
                      text=f"Lifetime {p('return_pct')}   vs the market {p('vs_holding_pct')}")
        hrs = lt.get("hours_winning")
        c.create_text(x + 10, y + 108, anchor="w", fill=MUTED, font=("", 8), tags="card",
                      text=(f"won for {hrs:.1f} h" if hrs is not None else lt.get("source", "")) +
                           (f"   |   win rate {lt['win_rate_pct']:.0f}%" if lt.get("win_rate_pct") is not None else ""))
        extras = ", ".join(x for x in (E.risk_text(b.get("risk")), E.traits_text(b.get("traits"))) if x)
        if extras:
            c.create_text(x + 10, y + 124, anchor="w", fill=MUTED, font=("", 7), tags="card", text=extras[:48])

    def egg_color(self, v):
        if v.royal:
            return "#f59e0b"  # royal egg: descends from bots you saved
        if v.classic:
            return "#94a3b8"  # silver egg: a famous strategy
        if self.settings.get("egg_colors", "By dragon type") == "Purple":
            return "#8b5cf6"
        th = theme_for(v.name)
        return THEMES[th]["body"] if th else "#8b5cf6"

    def _grid_anim_frame(self):
        c = self.grid
        c.delete("anim")
        if not c.winfo_ismapped() or not self.grid_cards or self.farm_style.get() != "Dragons":
            return
        ph = getattr(self, "farm_phase", 0)
        top, bottom = c.canvasy(0), c.canvasy(c.winfo_height())
        awake = self.running and self.seeded
        big = self.grid_filter.get() == "BEASTS"
        for i, (x0, y0, x1, y1, v, st, t, strat) in enumerate(self.grid_cards):
            if y1 < top or y0 > bottom:
                continue  # off screen: skip
            if t == "hof":
                draw_creature(c, strat, x0 + 14, y0 + 22 + math.sin(ph / 3) * 1.5, 50, "#eab308", awake=True,
                            phase=ph // 3, mood="happy", gold=True, crown=True, tags="anim")
                continue
            if t == "egg":
                prog = min(1, len(v.eq) / max(1, self.tourn.W)) if self.tourn else 0
                draw_egg(c, x0 + 20, y0 + 12, 40, self.egg_color(v), prog, ph if awake else 0, tags="anim",
                         royal=v.royal, classic=bool(v.classic))
                continue
            label, col = self.GRID_STATUS.get(st, ("?", MUTED))
            working = awake and st != "benched"
            just = awake and bool(v.log) and v.log[-1][0] == (self.tourn.n if self.tourn else -1)
            bob = 2 * math.sin(ph / 2.4) if working else 0
            if t == "beast":
                fire = working and (just or ph % 30 < 4)
                draw_creature(c, strat, x0 + 12, y0 + 22 + bob, 64 if big else 50, "#dc2626" if awake else "#6b7280", status=st, awake=working,
                            phase=ph, mood="happy", fire=fire, flash=just and ph % 2 == 0, crown=True, tags="anim")
            else:
                draw_creature(c, strat, x0 + 14, y0 + 22 + bob, 50, col if awake else "#6b7280", status=st, awake=working, phase=ph,
                            mood="happy" if st == "active" else "plain", fire=just and v.log[-1][2] == "buy",
                            flash=just and ph % 2 == 0, tags="anim")
            if not working:
                k = ph % 18
                if k < 12:
                    c.create_text(x0 + 66 + k / 3, y0 + 26 - k, text="z" if k < 6 else "Z", fill=MUTED,
                                  font=("", 8 + k // 6, "bold"), tags="anim")
            elif just:
                side = v.log[-1][2]
                typ_ = getattr(v, "log_ct", {}).get(tuple(v.log[-1][:2]), "")
                c.create_text(x0 + 66, y0 + 24, text=("BUY" if side == "buy" else "SELL") + (f" {typ_.upper()}" if typ_ else "") + "!",
                              tags="anim",
                              fill=GREEN if side == "buy" else "#f59e0b", font=("", 8, "bold"))
        promos = getattr(self, "promos", {})
        if promos:
            for x0, y0, x1, y1, v, st, t, strat in self.grid_cards:
                if v is None or id(v) not in promos or y1 < top or y0 > bottom or t == "hof":
                    continue
                kind, start = promos[id(v)]
                age = ph - start
                if age > 28 or age < 0:
                    promos.pop(id(v), None)
                    continue
                cx, cy = x0 + 40, y0 + 42
                if kind == "hatch":  # shell shards fly out, "HATCHED!" floats up
                    col = self.egg_color(v)
                    for k in range(8):
                        a = k * math.pi / 4 + 0.3
                        d = age * 3
                        sx, sy = cx + math.cos(a) * d, cy + math.sin(a) * d + age * age * 0.06
                        c.create_polygon(sx, sy, sx + 6, sy + 2, sx + 2, sy + 7, fill=col, outline="white", tags="anim")
                    self._promo_text(c, (x0 + x1) / 2, y0 + 14 - min(age, 10) * 0.6, "HATCHED!", "#22c55e")
                else:  # roar: fiery rings burst out of the dragon
                    for k in range(3):
                        r_ = min(40, (age - k * 4) * 3)
                        if 0 < r_ < 40:
                            c.create_oval(cx - r_, cy - r_, cx + r_, cy + r_, outline=("#f97316", "#dc2626", "#facc15")[k],
                                          width=3, tags="anim")
                    self._promo_text(c, (x0 + x1) / 2, y0 + 14, "ROAR!", "#f97316", 11 + min(age, 6))
        sel = set(self.board.selection())
        for x0, y0, x1, y1, v, st, t, strat in self.grid_cards:
            if v is not None and str(id(v)) in sel and not (y1 < top or y0 > bottom):
                self.round_rect(c, x0 - 3, y0 - 3, x1 + 3, y1 + 3, 16, fill="", outline=TEXT, width=3, tags="anim")
        hv = self.grid_hover
        if hv is not None:
            x0, y0, x1, y1 = hv
            self.round_rect(c, x0 - 2, y0 - 2, x1 + 2, y1 + 2, 15, fill="", outline=ACCENT, width=2, tags="anim")

    def _promo_text(self, c, x, y, text, color, size=11):
        t = c.create_text(x, y, text=text, fill=color, font=("", size, "bold"), tags="anim")
        bb = c.bbox(t)
        if bb:
            bg = self.round_rect(c, bb[0] - 8, bb[1] - 3, bb[2] + 8, bb[3] + 3, 8, fill=BG, outline=color, width=2, tags="anim")
            c.tag_lower(bg, t)

    def _grid_at(self, e):
        x, y = self.grid.canvasx(e.x), self.grid.canvasy(e.y)
        for card in self.grid_cards:
            if card[0] <= x <= card[2] and card[1] <= y <= card[3]:
                return card
        return None

    def _grid_motion(self, e):
        card = self._grid_at(e)
        self.grid_hover = card[:4] if card else None

    def _grid_click(self, e):
        card = self._grid_at(e)
        if not card or card[4] is None:
            return
        iid = str(id(card[4]))
        if self.board.exists(iid):
            if e.state & 0x0004 or e.state & 0x0001:  # ctrl/shift: several (for Catch! or Export)
                cur = set(self.board.selection())
                cur ^= {iid}
                self.board.selection_set(list(cur))
            else:
                self.board.selection_set(iid)
        self.soon(self._after_select)

    def later(self, fn, ms=150):
        """Runs fn once, ms after the LAST time it was asked for (resizing fires hundreds of events: draw once at the end)."""
        pend = self.__dict__.setdefault("_later", {})
        old = pend.pop(fn.__name__, None)
        if old:
            try:
                self.root.after_cancel(old)
            except Exception:
                E.note_error("later")
        pend[fn.__name__] = self.root.after(ms, lambda: (pend.pop(fn.__name__, None), fn()))

    def soon(self, fn, ms=40):
        """Runs fn once a moment from now, however many times it is asked for meanwhile (keeps clicks snappy)."""
        pend = self.__dict__.setdefault("_soon", {})
        if fn.__name__ in pend:
            return
        pend[fn.__name__] = self.root.after(ms, lambda: (pend.pop(fn.__name__, None), fn()))

    def _after_select(self):
        self.draw_chart()
        if self.farm_style.get() != "Dragons":  # dragons show the selection on their own animated layer
            self.grid_refresh()

    # ---------------- custom egg designer ----------------
    def edit_rules(self, v=None, entry=None):
        """See and change a bot's learned rules. Farm bots: hatch the edited version as its child, or replace it.
        Library bots: save the edited version as a new Library bot."""
        if v is not None:
            g = dict(name=v.name, params=dict(v.params), tf=v.tf, risk=dict(v.risk), traits=dict(v.traits),
                     vetoes=dict(v.vetoes), layers=json.loads(json.dumps(v.layers)), size=v.size_mult,
                     layer_stats=v.layer_stats, veto_stats=v.veto_stats)
            self.open_designer(g, edit=v)
        elif entry is not None:
            self.open_designer(dict(entry), edit=entry)

    def open_designer(self, preset=None, edit=None):
        """Design a bot by hand: strategy and its numbers, pace, own exits and traits. Hatch it on the farm
        (a royal egg) and/or save it to the Library."""
        w = tk.Toplevel(self.root)
        editing = edit is not None
        ed_name = (E.bot_nickname(edit.key()) if hasattr(edit, "key") else
                   (edit.get("nickname") or E.bot_nickname(E.bot_key(edit)))) if editing else ""
        w.title(f"Edit rules: {ed_name}" if editing else "Design an egg")
        w.geometry(f"900x{min(860, self.root.winfo_screenheight() - 120)}")
        sf = ScrollFrame(w, BG, width=880)  # tall form: it scrolls on small screens
        sf.pack(fill="both", expand=True)
        f = ttk.Frame(sf.inner, padding=14)
        f.pack()
        ttk.Label(f, text=f"Edit rules: {ed_name}" if editing else "Design an egg", font=("", 13, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w")
        if editing:
            learned = []
            for title_, txt in E.rule_stack(preset):
                learned.append(f"{title_}: {txt}")
            ttk.Label(f, foreground=MUTED, wraplength=820, justify="left", text=(
                "What it has learned so far (with the evidence for each rule):\n" + "\n".join(learned) +
                "\n\nChange anything below. Its own record stays as it earned it: the edited version hatches as its child "
                "(or replaces it), keeps its bloodline, and has to prove itself.")).grid(row=1, column=0, columnspan=4,
                                                                                      sticky="w", pady=(2, 10))
        else:
            ttk.Label(f, foreground=MUTED, wraplength=560, text=(
                "Write your own rules: pick a strategy and its numbers (for example RSI: buy below 28, sell above 72), how "
                "often it decides, its own exits and habits. It hatches as a royal egg and competes like any other bot, "
                "and its children keep your bloodline.")).grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 10))
        names = [n for n in E.WILDCARD_STRATS]
        labels = {STRAT_LABELS.get(n, n): n for n in names}
        strat = tk.StringVar(value=STRAT_LABELS.get((preset or {}).get("name", "RSI"), "Buy dips / sell peaks"))
        ttk.Label(f, text="Strategy").grid(row=2, column=0, sticky="w")
        sb = ttk.Combobox(f, textvariable=strat, values=list(labels), state="readonly", width=28)
        sb.grid(row=2, column=1, columnspan=2, sticky="w", padx=6)
        tip = ttk.Label(f, foreground=MUTED, wraplength=560, justify="left")
        tip.grid(row=3, column=0, columnspan=4, sticky="w", pady=(2, 6))
        pbox = ttk.LabelFrame(f, text=" Its numbers ", padding=8)
        pbox.grid(row=4, column=0, columnspan=4, sticky="ew")
        pvars = {}

        def build_params(*_):
            for ch in pbox.winfo_children():
                ch.destroy()
            pvars.clear()
            n = labels[strat.get()]
            tip.config(text=STRAT_TIPS.get(n, ""))
            given = (preset or {}).get("params", {}) if (preset or {}).get("name") == n else {}
            for r, (k, dv) in enumerate(E.STRATS[n]["params"].items()):
                lo, hi = E.STRATS[n]["bounds"][k]
                ttk.Label(pbox, text=PARAM_NAMES.get(k, k)).grid(row=r, column=0, sticky="w")
                var = tk.StringVar(value=str(given.get(k, dv)))
                ttk.Entry(pbox, textvariable=var, width=8).grid(row=r, column=1, padx=6)
                ttk.Label(pbox, text=f"{lo:g} to {hi:g}", foreground=MUTED).grid(row=r, column=2, sticky="w")
                pvars[k] = (var, type(dv), lo, hi)
        sb.bind("<<ComboboxSelected>>", build_params)
        build_params()
        row = 5
        ttk.Label(f, text="Decides every").grid(row=row, column=0, sticky="w", pady=(8, 0))
        paces = {E.fmt_bar(tf * self.session_bar) + (f"  ({tf} bars)" if tf > 1 else "  (every bar)"): tf for tf in E.TIMEFRAMES}
        pace = tk.StringVar(value=next(k for k, v in paces.items() if v == E.clamp_tf((preset or {}).get("tf", 1))))
        ttk.Combobox(f, textvariable=pace, values=list(paces), state="readonly", width=22).grid(
            row=row, column=1, columnspan=2, sticky="w", padx=6, pady=(8, 0))
        row += 1
        risk = (preset or {}).get("risk") or {}
        sl, tp = tk.StringVar(value=str(risk.get("sl", 0) or "")), tk.StringVar(value=str(risk.get("tp", 0) or ""))
        ttk.Label(f, text="Own safety stop %").grid(row=row, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(f, textvariable=sl, width=8).grid(row=row, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(f, text="Own profit target %").grid(row=row, column=2, sticky="w", pady=(6, 0))
        ttk.Entry(f, textvariable=tp, width=8).grid(row=row, column=3, sticky="w", padx=6, pady=(6, 0))
        row += 1
        tbox = ttk.LabelFrame(f, text=" Habits (traits), tick to use ", padding=8)
        tbox.grid(row=row, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        given_t = (preset or {}).get("traits") or {}
        tvars = {}
        for r, k in enumerate(k for k in E.TRAITS if k not in ("exit", "moods")):
            spec = E.TRAITS[k]
            on = tk.BooleanVar(value=k in given_t)
            start = spec["lo"] + (spec["hi"] - spec["lo"]) / 4  # a sensible starting value inside the allowed range
            val = tk.StringVar(value=str(given_t.get(k, round(start, 2) if spec["kind"] is float else int(round(start)))))
            ttk.Checkbutton(tbox, text=E.TRAIT_NAMES.get(k, k), variable=on).grid(row=r // 2, column=(r % 2) * 3, sticky="w")
            ttk.Entry(tbox, textvariable=val, width=6).grid(row=r // 2, column=(r % 2) * 3 + 1, padx=4)
            ttk.Label(tbox, text=f"{spec['lo']:g}-{spec['hi']:g}", foreground=MUTED).grid(row=r // 2, column=(r % 2) * 3 + 2,
                                                                                          sticky="w", padx=(0, 12))
            tvars[k] = (on, val)
        rr = (len(tvars) + 1) // 2
        exit_only = tk.BooleanVar(value=bool(given_t.get("exit")))
        ttk.Checkbutton(tbox, text="Sell only through its own exits (stop / target / trailing / max hold)",
                        variable=exit_only).grid(row=rr, column=0, columnspan=6, sticky="w", pady=(4, 0))
        mood_vars = []
        mrow = ttk.Frame(tbox)
        mrow.grid(row=rr + 1, column=0, columnspan=6, sticky="w", pady=(4, 0))
        ttk.Label(mrow, text="Only trade when the market is:").pack(side="left")
        gm = int(given_t.get("moods") or 0)
        for i, m in enumerate(E.MOODS):
            var = tk.BooleanVar(value=bool(gm & (1 << i)) if gm else True)
            ttk.Checkbutton(mrow, text=m, variable=var).pack(side="left", padx=(6, 0))
            mood_vars.append(var)
        row += 1
        lbox = ttk.LabelFrame(f, text=" Studies: several reasons to buy, several reasons not to (optional) ", padding=8)
        lbox.grid(row=row, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        row += 1
        gl = E.clean_layers((preset or {}).get("layers") or {})
        fams = [STRAT_LABELS.get(n, n) for n in E.WILDCARD_STRATS]
        to_name = {STRAT_LABELS.get(n, n): n for n in E.WILDCARD_STRATS}
        lists = {}
        for col, (key, title, cap) in enumerate((("plus", "Positive studies (also say buy)", E.MAX_PLUS),
                                                 ("minus", "Negative studies (a sell counts against)", E.MAX_MINUS),
                                                 ("exits", "Exit studies (any sell = sell)", E.MAX_EXITS))):
            ttk.Label(lbox, text=f"{title}, up to {cap}").grid(row=0, column=col, sticky="w", padx=(0, 10))
            lb = tk.Listbox(lbox, selectmode="multiple", height=7, exportselection=False, width=26,
                            bg=CARD, fg=TEXT, selectbackground=ACCENT, highlightthickness=0)
            for fam in fams:
                lb.insert("end", fam)
            for sub in gl.get(key, []):
                lbl = STRAT_LABELS.get(sub["name"], sub["name"])
                if lbl in fams:
                    lb.selection_set(fams.index(lbl))
            lb.grid(row=1, column=col, sticky="w", padx=(0, 10))
            lists[key] = (lb, cap)
        need_v = tk.StringVar(value=str(gl.get("need", 1)))
        nrow = ttk.Frame(lbox)
        nrow.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(nrow, text="Buy when at least").pack(side="left")
        ttk.Spinbox(nrow, from_=1, to=1 + E.MAX_PLUS, width=3, textvariable=need_v).pack(side="left", padx=4)
        ttk.Label(nrow, text="positive studies (the main strategy counts as one) say buy, minus one for each negative "
                             "study saying sell.", foreground=MUTED).pack(side="left")
        size_on = tk.BooleanVar(value=bool(gl.get("size")))
        size_t = tk.StringVar(value=str((gl.get("size") or {}).get("target", 0.5)))
        srow = ttk.Frame(lbox)
        srow.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Checkbutton(srow, text="Size trades to the market's swing, aiming for this % move per decision:",
                        variable=size_on).pack(side="left")
        ttk.Entry(srow, textvariable=size_t, width=6).pack(side="left", padx=4)
        hrow = ttk.Frame(lbox)
        hrow.grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(hrow, text="Bigger trend must agree:").pack(side="left")
        htf_v = tk.StringVar(value=STRAT_LABELS.get(gl["htf"]["name"], gl["htf"]["name"]) if gl.get("htf") else "(none)")
        ttk.Combobox(hrow, textvariable=htf_v, values=["(none)"] + fams, state="readonly", width=24).pack(side="left", padx=4)
        ttk.Label(hrow, text="on candles").pack(side="left")
        htf_m = tk.StringVar(value=f"{gl['htf']['mult']}x" if gl.get("htf") else "6x")
        ttk.Combobox(hrow, textvariable=htf_m, values=[f"{m}x" for m in E.HTF_MULTS], state="readonly", width=4).pack(side="left", padx=4)
        ttk.Label(hrow, text="longer than its own", foreground=MUTED).pack(side="left")
        lrow = ttk.Frame(lbox)
        lrow.grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 0))
        lead_on = tk.BooleanVar(value=bool(gl.get("lead")))
        ttk.Checkbutton(lrow, text="The market leads: SPY moving", variable=lead_on).pack(side="left")
        lead_th = tk.StringVar(value=str((gl.get("lead") or {}).get("th", 0.8)))
        ttk.Entry(lrow, textvariable=lead_th, width=5).pack(side="left", padx=4)
        ttk.Label(lrow, text="% over").pack(side="left")
        lead_lb = tk.StringVar(value=str((gl.get("lead") or {}).get("lb", 10)))
        ttk.Entry(lrow, textvariable=lead_lb, width=4).pack(side="left", padx=4)
        ttk.Label(lrow, text="decisions votes for (up) or against (down) buying other coins", foreground=MUTED).pack(side="left")
        nbox = ttk.LabelFrame(f, text=f" Never-rules (up to {E.MAX_VETOES}), tick to use ", padding=8)
        nbox.grid(row=row, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        given_v = (preset or {}).get("vetoes") or {}
        vvars = {}
        for r_, (k, spec) in enumerate(E.VETOES.items()):
            on = tk.BooleanVar(value=k in given_v)
            start = given_v.get(k, spec["lo"] + (spec["hi"] - spec["lo"]) / 3)
            val = tk.StringVar(value=str(int(start) if k == "revenge" else round(start, 2)))
            ttk.Checkbutton(nbox, text=spec["name"], variable=on).grid(row=r_ // 2, column=(r_ % 2) * 3, sticky="w")
            ttk.Entry(nbox, textvariable=val, width=6).grid(row=r_ // 2, column=(r_ % 2) * 3 + 1, padx=4)
            ttk.Label(nbox, text=f"{spec['lo']:g}-{spec['hi']:g}", foreground=MUTED).grid(row=r_ // 2, column=(r_ % 2) * 3 + 2,
                                                                                          sticky="w", padx=(0, 12))
            vvars[k] = (on, val)
        ttk.Label(nbox, foreground=MUTED, wraplength=540, text=(
            "Each never-rule blocks some trades. The farm checks what every blocked trade would have done and keeps "
            "score: rules that save money are passed on to children, rules that keep blocking winners die out.")).grid(
            row=(len(vvars) + 1) // 2, column=0, columnspan=6, sticky="w", pady=(4, 0))
        size = tk.StringVar(value=str((preset or {}).get("size", 1.0)))
        row += 1
        ttk.Label(f, text="Trade size (x normal)").grid(row=row, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(f, textvariable=size, width=8).grid(row=row, column=1, sticky="w", padx=6, pady=(8, 0))

        def build():
            n = labels[strat.get()]
            try:
                params = {}
                for k, (var, typ, lo, hi) in pvars.items():
                    v = float(var.get())
                    if not lo <= v <= hi:
                        raise ValueError(f"{PARAM_NAMES.get(k, k)} must be {lo:g} to {hi:g}")
                    params[k] = int(round(v)) if typ is int else v
                if E.fix_params(n, dict(params)) != params:
                    raise ValueError("short averages must be below long ones, and buy levels well below sell levels")
                traits = {}
                for k, (on, val) in tvars.items():
                    if on.get():
                        traits[k] = float(val.get())
                if exit_only.get():
                    traits["exit"] = 1
                mask = sum(1 << i for i, v in enumerate(mood_vars) if v.get())
                if 0 < mask < 15:
                    traits["moods"] = mask
                vetoes = {k: float(val.get()) for k, (on, val) in vvars.items() if on.get()}
                if len(vetoes) > E.MAX_VETOES:
                    raise ValueError(f"pick at most {E.MAX_VETOES} never-rules")
                layers = {}
                for key, (lb, cap) in lists.items():
                    picks = [to_name[lb.get(i_)] for i_ in lb.curselection()]
                    if len(picks) > cap:
                        raise ValueError(f"pick at most {cap} {'positive' if key == 'plus' else 'negative' if key == 'minus' else 'exit'} studies")
                    if picks:  # a study it already had keeps the settings it learned
                        had = {sd["name"]: sd for sd in gl.get(key, [])}
                        layers[key] = [dict(name=pn, params=dict(had[pn]["params"] if pn in had else E.STRATS[pn]["params"]))
                                       for pn in picks]
                if layers.get("plus") or layers.get("minus"):
                    layers["need"] = int(float(need_v.get() or 1))
                if size_on.get():
                    layers["size"] = dict(target=float(size_t.get()))
                if htf_v.get() != "(none)":
                    hn = to_name[htf_v.get()]
                    layers["htf"] = dict(name=hn, params=dict(gl["htf"]["params"] if gl.get("htf") and gl["htf"]["name"] == hn
                                                             else E.STRATS[hn]["params"]), mult=int(htf_m.get().rstrip("x")))
                if lead_on.get():
                    layers["lead"] = dict(lb=int(float(lead_lb.get())), th=float(lead_th.get()))
                entry = dict(name=n, params=params, tf=paces[pace.get()], size=min(max(float(size.get() or 1), 0.25), 3.0),
                             risk=E.clean_risk({"sl": float(sl.get() or 0), "tp": float(tp.get() or 0)}),
                             traits=E.clean_traits(traits), vetoes=E.clean_vetoes(vetoes), layers=E.clean_layers(layers),
                             stats={}, notes="edited by you" if editing else "designed by you")
                return entry
            except ValueError as ex:
                messagebox.showerror("Design an egg", f"Please check the numbers: {ex}", parent=w)
                return None

        def hatch(save=False, farm=True):
            entry = build()
            if not entry:
                return
            if farm:
                if not self.tourn or not self.seeded:
                    return messagebox.showinfo("Design an egg", "Start the bot with Wildcard ticked first, or use "
                                                                "'Save to Library' to keep it for later.", parent=w)
                v = self.tourn.add_variant(entry["name"], entry["params"], {c: self.hist[c] for c in self.session_coins},
                                           entry["size"], note="Designed egg:", tf=entry["tf"], risk=entry["risk"], vetoes=entry["vetoes"],
                                           layers=entry["layers"],
                                           traits=entry["traits"], origin="designed")
                if v:
                    if len(self.tourn.vars) > self.tourn.cap:
                        self.tourn.cap = len(self.tourn.vars)
                    self.flush_events()
                    self.refresh_board()
                    self.write(f"♛ Your egg {E.bot_nickname(v.key())} is on the farm: {E.describe_bot(entry, self.session_bar)}", "good")
            if save:
                self.catch_rows([dict(entry, lifetime=dict(bar_minutes=self.session_bar, source="designed by you"))], "designed")
            w.destroy()
        def apply_edit(replace):
            entry = build()
            if not entry:
                return
            src = edit
            if not self.tourn or not self.seeded:
                return messagebox.showinfo("Edit rules", "Start the farm first, or use 'Save to Library'.", parent=w)
            v = self.tourn.add_variant(entry["name"], entry["params"], {c: self.hist[c] for c in self.session_coins},
                                       entry["size"], note="Edited by you:", tf=entry["tf"], risk=entry["risk"],
                                       traits=entry["traits"], vetoes=entry["vetoes"], layers=entry["layers"], origin="edited by you")
            if not v:
                return
            if hasattr(src, "uid"):  # keep the bloodline: the edited bot is the original's child, on its island
                v.parents, v.gen, v.island, v.royal = [src.uid], src.gen + 1, src.island, True
                rec = self.tourn.lineage.get(v.uid)
                if rec:
                    rec.update(parents=[src.uid], gen=v.gen)
                v.layer_stats = {k: [x[0] / 2, x[1] / 2] for k, x in src.layer_stats.items()}
                v.veto_stats = {k: [x[0] / 2, x[1] / 2] for k, x in src.veto_stats.items() if k in v.vetoes}
                if replace and src in self.tourn.vars:
                    self.tourn._remove(src, "replaced by your edit")
            if len(self.tourn.vars) > self.tourn.cap:
                self.tourn.cap = len(self.tourn.vars)
            self.flush_events()
            self.refresh_board()
            self.write(f"\u270e {'Replaced' if replace else 'Hatched an edited child of'} {ed_name}: "
                       f"{E.bot_nickname(v.key())} is on the farm and has to prove itself.", "good")
            w.destroy()
        b = ttk.Frame(f)
        b.grid(row=row + 1, column=0, columnspan=4, sticky="e", pady=(14, 0))
        ttk.Button(b, text="Cancel", command=w.destroy).pack(side="left", padx=4)
        ttk.Button(b, text="Save to Library", command=lambda: hatch(save=True, farm=False)).pack(side="left", padx=4)
        if editing and hasattr(edit, "uid"):
            ttk.Button(b, text="Replace this bot", command=lambda: apply_edit(True)).pack(side="left", padx=4)
            ttk.Button(b, text="Hatch as its child", style="Accent.TButton", command=lambda: apply_edit(False)).pack(side="left", padx=4)
        else:
            ttk.Button(b, text="Hatch & save", command=lambda: hatch(save=True)).pack(side="left", padx=4)
            ttk.Button(b, text="Hatch on the farm", style="Accent.TButton", command=lambda: hatch()).pack(side="left", padx=4)

    # ---------------- auto-resume ----------------
    def auto_resume(self):
        """If the bot was running when SBF last closed, pick that session back up."""
        if not self.settings.get("auto_resume", True) or not self.cfg.get("was_running") or self.running:
            return
        mode = self.cfg.get("last_mode", "paper")
        tag_ = self.cfg.get("last_tag")
        if not tag_ and mode == "live":  # sessions from before 1.17: a paper key's session belongs to ALPACA PAPER
            lg = E.load_login()
            tag_ = "alpaca" if (lg and lg[2] and os.path.exists(E.state_path("alpaca"))) else "live"
        saved = E.load_json(E.state_path(tag_ or (mode == "live")))
        if not saved:
            return
        if mode == "live":
            if not self.client:  # wait for the saved login to connect (up to about 30 seconds)
                self._resume_tries = getattr(self, "_resume_tries", 0) + 1
                if self._resume_tries < 30 and E.load_login():
                    self.root.after(1000, self.auto_resume)
                elif self._resume_tries >= 30:
                    self.write("Your LIVE session was running when SBF closed, but Alpaca didn't connect, so it "
                               "was not resumed. Log in and press Start to resume it.", "warn")
                return
            if not messagebox.askyesno("Resume LIVE session?", "Your LIVE session was running when SBF closed.\n\n"
                                                               "Resume it now? (You'll be asked to type LIVE.)"):
                return
        if not self.session:
            self.mode_var.set(mode)
            self.wild_var.set(bool(self.cfg.get("last_wild", True)))
            self.toggle_wild()
        self._auto_resume = True
        self.write(f"Picking up where you left off ({'LIVE' if mode == 'live' else 'Practice'} session).", "good")
        self.toggle()
        self._auto_resume = False

    # ---------------- only redraw what you can see ----------------
    def cur_tab(self):
        """The name of the tab you're looking at (the inner one when Markets or Trades is open)."""
        try:
            t = self.nb.tab(self.nb.select(), "text").strip()
            sn = getattr(self, "subnb", {}).get(t)
            return sn.tab(sn.select(), "text").strip() if sn is not None else t
        except Exception:
            return ""

    def show_tab(self, name):
        """Brings a tab forward by name, inner tabs included."""
        for t_ in self.nb.tabs():
            if name in self.nb.tab(t_, "text"):
                return self.nb.select(t_)
        for outer, sn in getattr(self, "subnb", {}).items():
            for t_ in sn.tabs():
                if name in sn.tab(t_, "text"):
                    sn.select(t_)
                    return self.show_tab(outer)

    def refresh_visible(self):
        """Catches up the tab you just opened (hidden tabs skip their redraws to save effort)."""
        try:
            tab = self.cur_tab()
        except Exception:
            return
        if tab == "Trades" and getattr(self, "_trades_dirty", False):
            self._trades_dirty = False
            self.refresh_trades()
        elif tab == "Strategy scoreboard" and getattr(self, "_board_dirty", False):
            self._board_dirty = False
            self.refresh_board()
        elif "Bot Farm" in tab:
            self.grid_refresh()
        elif "Slow farm" in tab:
            self.slow_refresh()
        elif tab == "Bot positions":
            self.refresh_botpos()
        elif tab == "Alpaca account":
            self.refresh_alpaca(quiet=True)
        elif "Overview" in tab:
            self.refresh_features()
        elif "Scalp scan" in tab:
            self.refresh_scan()
        elif tab == "Scalp lab":
            self.refresh_lab()

    # ---------------- Features: every learning feature in one place ----------------
    def _set_flag(self, where, key, on):
        """Switches a feature setting (where = 'settings' or 'cfg'), saves it and tells the farms."""
        if where == "settings":
            self.settings[key] = bool(on)
            self.cfg["settings"] = self.settings
        else:
            self.cfg[key] = bool(on)
        E.save_json(CONFIG_PATH, self.cfg)
        if self.tourn:
            self.tourn.o.update(E.tournament_opts(self.settings))
        sl_ = getattr(self, "slow", {}).get("t")
        if sl_:
            sl_.o.update(E.tournament_opts(self.settings), scalp_share=0.0)  # the slow farm never keeps scalpers

    def _settings_tab(self, name):
        self.open_settings()
        try:
            nb = self.settings_nb
            for t_ in nb.tabs():
                if name in nb.tab(t_, "text"):
                    nb.select(t_)
        except Exception:
            pass

    def features(self):
        """[(icon, name, what it is, status fn, switch (get, set) or None, open fn)] for the Features tab."""
        st = self.settings

        def roam_status():
            cur = getattr(self, "roam_now", None)
            n = getattr(self, "roam_n", 0)
            if not self.cfg.get("roam_on"):
                return f"Off.  {n} studies done so far."
            if cur:
                return f"Now: {cur.get('kind', '')}: {cur.get('text', '')}"[:150]
            last = (getattr(self, "roam_log", None) or [None])[-1]
            return f"{n} studies done." + (f"  Last: {last['kind']}: {last['summary']}"[:140] if last else "")

        def hind_status():
            t = self.tourn
            book = getattr(t, "hindsight", {}) if t else {}
            if not book:
                return "Nothing recorded yet. Its first study runs inside Free roam."
            worked = sorted(((e["wins"], n) for n, e in book.items() if e.get("best")), reverse=True)
            return (f"{len(book)} instincts looked at; {len(worked)} would have worked"
                    + (f". Best so far: {', '.join(STRAT_LABELS.get(n, n) for _, n in worked[:3])}" if worked else "") + ".")

        def breed_status(pick, what):
            t = self.tourn
            if not t:
                return "No farm yet: press Start once."
            vs = [v for v in t.vars if pick(v)]
            win = sum(1 for v in vs if t.info.get(id(v), {}).get("status") == "active")
            return f"{len(vs)} {what} on your farm, {win} winning."

        def kami_status():
            base = breed_status(E.is_kami, "kamikazes")
            t = self.tourn
            book = getattr(t, "trial_book", {}) if t else {}
            tested = [k for k, x in book.items() if x[0] >= 3]
            return base + (f"  {len(tested)} rules tested for the farm so far." if book else "")

        def lab_status():
            lab = getattr(self, "lab", {})
            lt = lab.get("t")
            if lt:
                return (f"Learning live on {', '.join(lab.get('coins', []))}: {len(lt.scalpers())} scalpers, "
                        f"{lab.get('trades', 0)} scalps today, {len(lab.get('grads', []))} graduated to your farm.")
            return (lab.get("status") or "Off.") + f"  ({len(lab.get('grads', []))} graduated so far)"

        def scan_status():
            sc = getattr(self, "scan", {})
            res = sc.get("res") or {}
            hot = [k for k, x in (res.get("tickers") or {}).items() if x.get("paid") and x["pl"] > 0]
            return (sc.get("status") or "Ready") + (f".  Paying today: {', '.join(hot[:5])}" if hot else "")

        def slow_status():
            sl = getattr(self, "slow", {})
            if sl.get("t"):
                return f"{len(sl['t'].vars)} slow dragons on {', '.join(sl.get('coins') or [])}. " + (sl.get("status") or "")
            return sl.get("status") or "Off."

        def know_status():
            t = self.tourn
            if not t:
                return "No farm yet."
            items = E.farm_knowledge(t)
            return f"{len(items)} things learned, {sum(1 for x in items if x.get('strong'))} proven by their own evidence."

        def shadow_status():
            cur = getattr(self, "shadow_curve", None)
            return (f"Following your Hall of Fame with pretend money: {cur[-1][1] - self.start_cash:+,.2f} so far."
                    if cur else "Waiting for Hall of Fame bots to follow.")

        def mirror_status():
            if not self.cfg.get("mirror_paper"):
                return "Off: practice trades stay inside SBF."
            if not self.is_paper_key():
                return "On, but waiting for your Alpaca paper key (Account > Log in)."
            return f"On: {len(getattr(self, 'mirror_trades', []))} copies placed on your Alpaca paper account this session."

        s_ = lambda k: (lambda: bool(st.get(k, True)), lambda on: self._set_flag("settings", k, on))
        c_ = lambda k, d=False: (lambda: bool(self.cfg.get(k, d)), lambda on: self._set_flag("cfg", k, on))
        def think_status():
            t = self.tourn
            n = getattr(t, "thinks", 0) if t else 0
            on = st.get("think_between", True)
            return (f"{'On' if on else 'Off'}: {n:,} looks at the forming candle this run (each bot gets about "
                    f"{60 // (self.THINK_EVERY * 2) - 1} extra decisions a minute while the market is open).")

        def succ_status():
            t = self.tourn
            if not t:
                return "No farm yet."
            live = self.success_rows(t, "live")
            up = sum(1 for x in live if x[1] > 0)
            return f"{up} of {len(live)} bots are up on fresh prices" + (
                f"; best: {E.bot_nickname(live[0][0].key())} {live[0][1]:+.1f}%" if live else "") + "."
        return [
            ("\u2605", "Success", "How every bot is doing, five ways: live, lifetime, Free roam tests, exams and day replays.",
             succ_status, None, lambda: self.show_tab("Success")),
            ("\u23f1", "Think between candles", "A few times a minute the bots also decide on the candle that's still "
                                                "forming, so they can get in or out without waiting for the minute to end.",
             think_status, s_("think_between"), lambda: self.open_settings() if hasattr(self, "open_settings") else None),
            ("\u25c7", "Free roam", "The farm goes back in time by itself: bots test themselves on stretches of the past, "
                                   "practice farms live through them, and every lesson comes home.",
             roam_status, (lambda: bool(self.cfg.get("roam_on")), lambda on: self.toggle_roam(on)), self.open_roam),
            ("\U0001f52e", "Hindsight", "Looks back at what WOULD have worked: many instincts and settings tried on the past, "
                                       "checked on prices they didn't see. New eggs start from what worked.",
             hind_status, s_("hindsight"), lambda: self.open_knowledge()),
            ("\u26a1", "Scalp lab", "A farm of scalpers only, learning from live prices minute by minute while the market is "
                                   "open, and sending its best to your farm.",
             lab_status, (lambda: bool(self.cfg.get("lab_on")), lambda on: on != bool(self.cfg.get("lab_on")) and self.toggle_lab()),
             lambda: self.show_tab("Scalp lab")),
            ("\U0001f50d", "Scalp scan", "Every 5 minutes during the trading day, your best scalpers are tried on today's "
                                        "prices of many tickers, to see where scalps are paying.",
             scan_status, c_("scalp_scan", True), lambda: self.show_tab("Scalp scan")),
            ("\U0001f32a", "Scalper breed", "Wind dragons: quick in-and-out bots with tight exits, a time limit and flat "
                                           "before the close.",
             lambda: breed_status(lambda v: E.is_scalp(v.name), "scalpers"), s_("scalpers"),
             lambda: (self.show_tab("Bot Farm"), self.grid_search.set("scalp"), self._grid_set_filter("All"))),
            ("\u2604", "Kamikaze breed", "Big bets with wide stops and no say in your account; each one tests risky "
                                        "rules so your good bots don't pay for them.",
             kami_status, s_("kamikazes"), lambda: self.open_knowledge()),
            ("\u25c7", "Slow farm", "A separate farm of patient dragons that decide on hourly candles and hold for days.",
             slow_status, (lambda: bool(self.cfg.get("slow_on")), lambda on: on != bool(self.cfg.get("slow_on")) and self.slow_toggle()),
             lambda: self.show_tab("Slow farm")),
            ("\u2605", "Shadow Live", "Follows only your Hall of Fame bots with pretend money in real time, so you see how "
                                    "your champions would do on their own.",
             shadow_status, s_("shadow_live"), lambda: (self.show_tab("Bot Farm"), self._grid_set_filter("Hall of Fame"))),
            ("\U0001f4d6", "Farm knowledge", "Everything the farm has learned, with the evidence: save it as a book and teach "
                                           "it to another farm.",
             know_status, None, lambda: self.open_knowledge()),
            ("\u2694", "Training camp", "Send a whole farm through days or months of history, then give the best a final "
                                       "exam on prices they never saw.",
             lambda: "Runs when you start it.", None, self.open_training),
            ("\u21c4", "Practice on Alpaca paper too", "Every practice trade is also copied to your Alpaca paper account "
                                                      "(Practice trades go to: Both).",
             mirror_status, (lambda: bool(self.cfg.get("mirror_paper")),
                             lambda on: (self.dest_var.set("Both: SBF and my Alpaca paper account" if on else "Stay in SBF  (PRACTICE)"),
                                         self.pick_practice_dest())),
             lambda: self._settings_tab("Basics")),
            ("\u2699", "Learning switches", "Every rule the bots can grow (never-rules, studies, seasons, elders, islands...), "
                                          "each one on or off.",
             lambda: f"{sum(1 for k, v in st.items() if isinstance(v, bool) and v)} learning options on.", None,
             lambda: self._settings_tab("Learning")),
        ]

    SUCCESS_MODES = (
        ("live", "Live", "On fresh prices since each bot was born (what it's doing right now)"),
        ("life", "Lifetime", "Everything since it was born, including the recent past it replayed when it hatched"),
        ("roam", "Free roam tests", "Its self-tests on stretches of the past (average return per test)"),
        ("exam", "Exams", "Days and stretches it never trained on (average result; passing needs to beat luck)"),
        ("day", "Day replays", "Scalpers and kamikazes: whole past trading days on 1-minute prices (profit on what they paid)"))

    def success_rows(self, t, mode):
        """[(bot, result %, how many (tests / decisions), trades, win rate, best, worst, detail)] for one view, best first."""
        out = []
        for v in list(t.vars):
            best, worst = v.best_tr, v.worst_tr
            closed = v.wins + v.losses
            wr = v.wins / closed if closed else None
            if mode == "live":
                if not v.live_n or not v.live_eq0 or not v.eq:
                    continue
                out.append((v, (v.eq[-1] / v.live_eq0 - 1) * 100, f"{v.live_n} decisions", v.ntrades, wr, best, worst,
                            "on prices that came in after it hatched"))
            elif mode == "life":
                lf = t.life(v)
                out.append((v, lf["ret"] * 100, f"{v.age} decisions", lf["trades"], lf["winrate"], best, worst,
                            "" if lf["edge"] is None else f"{lf['edge'] * 100:+.1f}% vs the market"))
            elif mode == "roam":
                bts = [b for b in (v.backtests or []) if b.get("trades")]
                if not bts:
                    continue
                rets = [b["ret"] for b in bts]
                wrs = [b["winrate"] for b in bts if b.get("winrate") is not None]
                out.append((v, sum(rets) / len(rets), f"{len(bts)} tests, {sum(1 for x in rets if x > 0)} made money",
                            sum(b["trades"] for b in bts), (sum(wrs) / len(wrs) / 100) if wrs else None, max(rets), min(rets),
                            f"{sum(b['edge'] for b in bts) / len(bts):+.1f}% vs the market on average"))
            elif mode == "exam":
                ex = [x for x in (v.exams or []) if x.get("n", 0) > 0]
                if not ex:
                    continue
                e = [x["edge"] for x in ex]
                out.append((v, sum(e) / len(e), f"{len(ex)} exams, {sum(1 for x in e if x > 0)} won", sum(x["n"] for x in ex),
                            None, max(e), min(e), E.exam_verdict(v, t.serial)[0]))
            elif mode == "day":
                rs = getattr(v, "roam_scan", None)
                if not rs or not rs[0] or not rs[3]:
                    continue
                out.append((v, rs[2] / rs[3] * 100, f"{rs[0]:.0f} scalps", int(round(rs[0])), rs[1] / rs[0] if rs[0] else None,
                            best, worst, "older days count a little less"))
        return sorted(out, key=lambda x: -x[1])

    def build_success_tab(self, parent):
        """How well each bot is doing, in different views: live, lifetime, Free roam tests, exams and day replays."""
        top = tk.Frame(parent, bg=BG)
        top.pack(fill="x", padx=6, pady=(6, 2))
        tk.Label(top, text="\u2605 Success", bg=BG, fg=TEXT, font=("", 12, "bold")).pack(side="left")
        self.succ_farm = tk.StringVar(value="Bot Farm")
        ttk.Combobox(top, textvariable=self.succ_farm, values=["Bot Farm", "Slow farm", "Scalp lab"], state="readonly",
                     width=11).pack(side="left", padx=(12, 6))
        self.succ_mode = tk.StringVar(value=self.cfg.get("succ_mode", "live"))
        self.succ_btns = {}
        for key, lab, tip in self.SUCCESS_MODES:
            b = tk.Button(top, text=lab, relief="flat", bd=0, padx=10, pady=3, cursor="hand2", font=("", 9, "bold"),
                          command=lambda k=key: (self.succ_mode.set(k), self.cfg.__setitem__("succ_mode", k), self.refresh_success()))
            b.pack(side="left", padx=2)
            Tip(b, tip)
            self.succ_btns[key] = b
        self.succ_farm.trace_add("write", lambda *a: self.refresh_success())
        self.succ_note = tk.Label(parent, text="", bg=BG, fg=MUTED, anchor="w", font=("", 8), padx=8)
        self.succ_note.pack(fill="x")
        self.succ_cv = tk.Canvas(parent, bg=CARD, height=170, highlightthickness=0)
        self.succ_cv.pack(fill="x", padx=6, pady=(2, 4))
        self.succ_cv.bind("<Configure>", lambda e: self.later(self.refresh_success, 200))
        fr = tk.Frame(parent, bg=BG)
        fr.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.succ_tree = self.make_tree(fr, ("bot", "breed", "status", "res", "n", "trades", "wr", "best", "worst", "det"),
                                        (130, 150, 80, 80, 170, 60, 70, 80, 80, 260),
                                        ("Bot", "Breed", "Status", "Result", "Based on", "Trades", "Win rate", "Best", "Worst",
                                         "Notes"))
        self.succ_tree.tag_configure("good", foreground=GREEN)
        self.succ_tree.tag_configure("bad", foreground=RED)
        self.succ_tree.configure(selectmode="extended")
        self.succ_vs = {}
        bar = tk.Frame(parent, bg=BG)
        bar.pack(fill="x", padx=6, pady=(0, 6), before=fr)
        tk.Label(bar, text="Picked bots:", bg=BG, fg=MUTED, font=("", 8)).pack(side="left")
        for lab_, fn in (("\u25c9 Save to Library", lambda vs: self.catch_rows(self.board_rows_of(vs), "success tab")),
                         ("\u2605 Add to Hall of Fame", self.promote_variants),
                         ("Add to my Bot Farm", self.add_to_main_farm),
                         ("Details...", lambda vs: self.bot_detail(vs[0]))):
            ttk.Button(bar, text=lab_, style="Plain.TButton",
                       command=lambda fn=fn: self._succ_act(fn)).pack(side="left", padx=(6, 0))
        self.succ_tree.bind("<Double-1>", lambda e: self._succ_act(lambda vs: self.bot_detail(vs[0])))
        self._succ_tick()

    def _succ_act(self, fn):
        vs = [self.succ_vs[i] for i in self.succ_tree.selection() if i in self.succ_vs]
        if not vs:
            return messagebox.showinfo("Success", "Pick one or more bots in the list first (Ctrl+click picks several).")
        fn(vs)

    def _succ_tick(self):
        try:
            if "Success" in self.cur_tab() and not self.in_bg():
                self.refresh_success()
        except Exception:
            E.note_error("_succ_tick")
        self.root.after(5000, self._succ_tick)

    def refresh_success(self):
        if not hasattr(self, "succ_tree"):
            return
        mode = self.succ_mode.get()
        for k, b in self.succ_btns.items():
            on = k == mode
            b.config(bg=ACCENT if on else CARD, fg="white" if on else TEXT, activebackground=ACCENT)
        t = {"Bot Farm": self.tourn, "Slow farm": getattr(self, "slow", {}).get("t"),
             "Scalp lab": getattr(self, "lab", {}).get("t")}.get(self.succ_farm.get())
        tree, cv = self.succ_tree, self.succ_cv
        keep = {self.succ_vs[i].uid for i in tree.selection() if i in getattr(self, "succ_vs", {})}
        tree.delete(*tree.get_children())
        self.succ_vs = {}
        cv.delete("all")
        lab = dict((k, (l, tip)) for k, l, tip in self.SUCCESS_MODES)[mode]
        if t is None:
            self.succ_note.config(text=f"{self.succ_farm.get()}: not running yet.")
            return
        rows = self.success_rows(t, mode)
        self.succ_note.config(text=f"{lab[1]}. {len(rows)} of {len(t.vars)} bots have results here."
                                   + ("" if rows else "  (Nothing yet: Free roam fills the test, exam and replay views.)"))
        pc = lambda x: "-" if x is None else f"{x:+.1f}%"
        for v, res, n, trades, wr, best, worst, det in rows:
            d = t.info.get(id(v), {})
            iid = tree.insert("", "end", tags=("good" if res > 0 else "bad" if res < 0 else "",), values=(
                E.bot_nickname(v.key()), STRAT_LABELS.get(v.name, v.name), STATUS_TEXT.get(d.get("status"), d.get("status") or "-"),
                pc(res), n, trades, "-" if wr is None else f"{wr * 100:.0f}%", pc(best), pc(worst), det))
            self.succ_vs[iid] = v
            if v.uid in keep:
                tree.selection_add(iid)  # (it refreshes every few seconds: what you picked stays picked)
        W, H = max(200, cv.winfo_width()), int(cv.cget("height"))
        top = rows[:12]
        if not top:
            return
        hi = max(abs(x[1]) for x in top) or 1
        mid = W * 0.45
        bh = (H - 10) / len(top)
        cv.create_line(mid, 4, mid, H - 4, fill=LINE)
        for i, (v, res, *_x) in enumerate(top):
            y = 6 + i * bh
            L = (W * 0.5 - 70) * abs(res) / hi
            x0, x1 = (mid, mid + L) if res >= 0 else (mid - L, mid)
            cv.create_rectangle(x0, y + 1, x1, y + bh - 2, fill=GREEN if res >= 0 else RED, outline="")
            cv.create_text(mid - 6 if res >= 0 else x0 - 6, y + bh / 2, anchor="e", fill=TEXT, font=("", 8),
                           text=E.bot_nickname(v.key()))
            cv.create_text((x1 + 6) if res >= 0 else (mid + 6), y + bh / 2, anchor="w", fill=MUTED, font=("", 8),
                           text=f"{res:+.1f}%")

    def build_features_tab(self, parent):
        sf = ScrollFrame(parent, BG, width=900)
        sf.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.feat_rows = []
        # --- every farm at a glance
        ov = tk.Frame(sf.inner, bg=BG, padx=4, pady=4)
        ov.pack(fill="x")
        top_ = tk.Frame(ov, bg=BG)
        top_.pack(fill="x")
        tk.Label(top_, text="\u2630 Overview", bg=BG, fg=TEXT, font=("", 13, "bold")).pack(side="left")
        eb = ttk.Button(top_, text="Export a report for Claude...", style="Plain.TButton", command=self.export_report)
        eb.pack(side="right")
        Tip(eb, "Saves a .zip of what every farm is doing and learning (no keys, passwords or account numbers) to attach "
                "in a chat with Claude for analysis.")
        tk.Label(ov, text="Every farm and everything they're learning, in one place. Updated every few seconds.", bg=BG,
                 fg=MUTED, font=("", 9)).pack(anchor="w")
        ff = tk.Frame(ov, bg=BG)
        ff.pack(fill="x", pady=(6, 4))
        cols = ("farm", "bots", "winning", "gen", "best", "breeds", "learned", "state")
        self.ov_tree = ttk.Treeview(ff, columns=cols, show="headings", height=4)
        for c_, lab, wd in (("farm", "Farm", 120), ("bots", "Dragons", 70), ("winning", "Winning", 70), ("gen", "Generation", 80),
                            ("best", "Best right now", 230), ("breeds", "Breeds", 230), ("learned", "Learned (proven)", 120),
                            ("state", "Doing now", 330)):
            self.ov_tree.heading(c_, text=lab)
            self.ov_tree.column(c_, width=wd, anchor="w", stretch=c_ in ("best", "state"))
        self.ov_tree.pack(fill="x")
        self.ov_tree.bind("<Double-1>", lambda e: (lambda sel: self.show_tab({"Bot Farm": "Bot Farm", "Slow farm": "Slow farm",
                                                                             "Scalp lab": "Scalp lab"}.get(sel[0], "Bot Farm"))
                                                  if sel else None)(self.ov_tree.selection()))
        prog = tk.Frame(ov, bg=BG)
        prog.pack(fill="x", pady=(4, 8))
        self.ov_stats = {}
        for i, (key, lab) in enumerate((("roam", "Free roam studies"), ("replay", "Days replayed"), ("real", "Proven bots with a vote"),
                                        ("hind", "What would have worked"), ("trial", "Rules tested by kamikazes"),
                                        ("grads", "Graduates adopted"), ("lib", "Library / Hall of Fame"), ("know", "Things learned"),
                                        ("exam", "Exams on unseen days"), ("order", "Days trained, in order"),
                                        ("hsfwd", "Hindsight lessons that held up"), ("cond", "Bots that know their moment"))):
            card = tk.Frame(prog, bg=CARD, highlightthickness=1, highlightbackground=LINE, padx=10, pady=6)
            card.grid(row=i // 4, column=i % 4, sticky="nsew", padx=3, pady=3)
            prog.columnconfigure(i % 4, weight=1, uniform="ov")
            tk.Label(card, text=lab.upper(), bg=CARD, fg=MUTED, font=("", 7, "bold")).pack(anchor="w")
            big = tk.Label(card, text="-", bg=CARD, fg=TEXT, font=("", 13, "bold"))
            big.pack(anchor="w")
            sub = tk.Label(card, text="", bg=CARD, fg=MUTED, font=("", 7), wraplength=330, justify="left", anchor="nw", height=3)
            sub.pack(anchor="w", fill="x")
            card.bind("<Configure>", lambda e, l=sub: self._fit_wrap(l, e.width - 24))
            self.ov_stats[key] = (big, sub)
        tk.Label(sf.inner, text="FEATURES  \u2014  what each one is, what it's learning right now, a switch, and a way to jump to it",
                 bg=BG, fg=ACCENT, font=("", 8, "bold"), padx=8).pack(anchor="w", pady=(4, 0))
        grid = tk.Frame(sf.inner, bg=BG)
        grid.pack(fill="x")
        for col in (0, 1):
            grid.columnconfigure(col, weight=1, uniform="feat")
        for i, (icon, name, what, status, switch, opener) in enumerate(self.features()):
            card = tk.Frame(grid, bg=CARD, highlightthickness=1, highlightbackground=LINE, padx=12, pady=8)
            card.grid(row=i // 2, column=i % 2, sticky="nsew", padx=5, pady=5)
            head = tk.Frame(card, bg=CARD)
            head.pack(fill="x")
            tk.Label(head, text=f"{icon}  {name}", bg=CARD, fg=TEXT, font=("", 11, "bold")).pack(side="left")
            btn = None
            if switch:
                btn = tk.Button(head, relief="flat", bd=0, padx=10, pady=2, cursor="hand2", font=("", 8, "bold"), fg="white",
                                activeforeground="white")
                btn.pack(side="right")

                def flip(sw=switch, b=None):
                    try:
                        sw[1](not sw[0]())
                    except Exception:
                        note = self._record_error("switching a feature")
                        if note:
                            self.write(note, "bad")
                    self.refresh_features()
                btn.config(command=flip)
            ttk.Button(head, text="Open \u203a", style="Plain.TButton", command=opener).pack(side="right", padx=(0, 8))
            tk.Label(card, text=what, bg=CARD, fg=MUTED, font=("", 8), wraplength=520, justify="left", anchor="w").pack(fill="x", pady=(4, 2))
            # always three lines tall, so the card doesn't jump as the news changes
            lbl = tk.Label(card, text="", bg=CARD, fg=ACCENT, font=("", 8, "bold"), wraplength=520, justify="left", anchor="nw",
                           height=3)
            lbl.pack(fill="x")
            card.bind("<Configure>", lambda e, l=lbl, w=card.winfo_children()[1]: (self._fit_wrap(l, e.width - 30),
                                                                                 self._fit_wrap(w, e.width - 30)))
            self.feat_rows.append((btn, switch, lbl, status))
        self._feat_tick()

    @staticmethod
    def _fit_wrap(lbl, w):
        """Wraps a label's text to the width it really has (only when that changes, so resizing stays light)."""
        w = max(120, int(w))
        if abs(int(lbl.cget("wraplength") or 0) - w) > 8:
            lbl.config(wraplength=w)

    def _feat_tick(self):
        try:
            if "Overview" in self.cur_tab() and not self.in_bg():
                self.refresh_features()
        except Exception:
            E.note_error("_feat_tick")
        self.root.after(3000, self._feat_tick)

    def overview_rows(self):
        """One row per farm: (name, dragons, winning, generation, best now, breeds, learned, doing now)."""
        out = []
        lab = getattr(self, "lab", {})
        for name, t, state in (("Bot Farm", self.tourn, ("running" if self.running else "paused") if self.session else "not started"),
                               ("Slow farm", getattr(self, "slow", {}).get("t"), (getattr(self, "slow", {}).get("status") or
                                                                                   ("running" if self.cfg.get("slow_on") else "paused"))),
                               ("Scalp lab", lab.get("t"), lab.get("status") or "")):
            if t is None:
                out.append((name, "-", "-", "-", "-", "-", "-", state or "off"))
                continue
            vs = list(t.vars)
            win = sum(1 for v in vs if t.info.get(id(v), {}).get("status") == "active")
            gen = max((v.gen for v in vs), default=0)
            r = [x for x in t.ranking() if x[1] is not None]
            best = f"{E.bot_nickname(r[0][0].key())} ({STRAT_LABELS.get(r[0][0].name, r[0][0].name)}) {r[0][1] * 100:+.1f}%" if r else "-"
            sc = sum(1 for v in vs if E.is_scalp(v.name))
            ka = sum(1 for v in vs if E.is_kami(v))
            breeds = f"{len(vs) - sc - ka} regular, {sc} scalpers, {ka} kamikazes"
            try:
                items = E.farm_knowledge(t)
                learned = f"{len(items)} ({sum(1 for x in items if x.get('strong'))})"
            except Exception:
                learned = "-"
            out.append((name, len(vs), win, gen, best, breeds, learned, str(state)[:120]))
        return out

    def refresh_overview(self):
        if not hasattr(self, "ov_tree"):
            return
        t = self.ov_tree
        t.delete(*t.get_children())
        for row in self.overview_rows():
            t.insert("", "end", iid=row[0], values=row)
        log = list(getattr(self, "roam_log", []) or [])
        kinds = collections.Counter(r.get("kind") for r in log)
        day = time.time() - 86400
        clip = lambda x: x if len(x) <= 140 else x[:137].rstrip() + "..."
        set_ = lambda k, big, sub="": (self.ov_stats[k][0].config(text=big), self.ov_stats[k][1].config(text=clip(" ".join(str(sub).split()))))
        set_("roam", f"{getattr(self, 'roam_n', 0):,}", "  |  ".join(x for x in (
            ", ".join(f"{n} {k.lower()}" for k, n in kinds.most_common()),
            f"{sum(1 for r in log if r.get('time', 0) >= day)} in the last 24 h") if x))
        reps = [r for r in log if r.get("kind") == "Scalp roam"]
        set_("replay", f"{len(reps)}", f"last: {reps[-1].get('day', '')}  {reps[-1].get('summary', '')[:90]}" if reps else "none yet")
        t_pv = self.tourn
        proven = t_pv.proven_bots() if t_pv else []
        if not t_pv:
            sub = "the farm hasn't started yet"
        elif not t_pv.o.get("proven_only", True):
            sub = "the gate is off: every winning bot may vote (Settings > Wildcard)"
        elif proven:
            lead = max(proven, key=lambda v: t_pv.life(v)["ret"])
            sub = (f"leader: {E.bot_nickname(lead.key())} ({t_pv.life(lead)['ret'] * 100:+.1f}% lifetime). "
                   f"Only these decide the account's trades")
        else:
            sub = "none yet: the account waits until bots prove themselves"
        set_("real", f"{len(proven)} of {len(t_pv.vars)}" if t_pv else "-", sub)
        tf = self.tourn
        hb = getattr(tf, "hindsight", {}) if tf else {}
        worked = [n for n, e in hb.items() if e.get("best")]
        set_("hind", f"{len(worked)} instincts", ("best: " + ", ".join(STRAT_LABELS.get(n, n) for n in sorted(
            worked, key=lambda n: -hb[n]["wins"])[:4])) if worked else "nothing recorded yet")
        tb = getattr(tf, "trial_book", {}) if tf else {}
        good = [k for k, x in tb.items() if x[0] >= 5 and x[1] > 0]
        set_("trial", f"{sum(1 for x in tb.values() if x[0] >= 3)}", f"{len(good)} proved useful" if tb else "none yet")
        g_roam = sum(1 for r in log if r.get("graduate"))
        g_lab = len((getattr(self, "lab", {}) or {}).get("grads", []))
        set_("grads", f"{g_roam + g_lab}", f"{g_roam} from Free roam, {g_lab} from the Scalp lab")
        try:
            set_("lib", f"{len(self.library())} / {len(self.hof())}", "caught dragons / your champions")
        except Exception:
            pass
        try:
            items = E.farm_knowledge(tf) if tf else []
            set_("know", f"{len(items)}", f"{sum(1 for x in items if x.get('strong'))} proven by their own evidence (Bot Farm)")
        except Exception:
            pass
        if tf is not None:
            vs = [v for v in tf.vars if not E.is_kami(v)]
            ver = collections.Counter(E.exam_verdict(v, tf.serial)[0] for v in vs)
            set_("exam", f"{ver['passed']} passed", f"{ver['failed']} failed, {ver['untested']} not proven yet; the bar for "
                                                    f"beating luck is {E.exam_bar(tf.serial):.1f} after {tf.serial:,} bots tried")
            n_cond = sum(1 for v in vs if E.good_at(v))
            here = ", ".join(E.COND_TEXT.get(c, c) for c in getattr(tf, "conds", []))
            set_("cond", f"{n_cond}", "have a record for each kind of market" + (f"; right now: {here}" if here else ""))
            fw = [(e.get("fwd") or [0, 0]) for e in (getattr(tf, "hindsight", {}) or {}).values()]
            fn, fw_ = sum(x[0] for x in fw), sum(x[1] for x in fw)
            set_("hsfwd", f"{fw_} of {fn}" if fn else "-",
                 f"hindsight eggs that did well later; {tf.hindsight_share() * 100:.0f}% of new eggs come from hindsight"
                 if fn else "graded once its eggs take their exams")
        days = sorted(getattr(self, "roam_days", set()) or [])
        exams = [r for r in reps if r.get("exam")]
        set_("order", f"{len(days)}", (f"up to {days[-1]}; " if days else "") + f"{len(exams)} exams on held-back days "
             "(1 day in 5 is never trained on)")

    def refresh_features(self):
        try:
            self.refresh_overview()
        except Exception:
            self._record_error("updating the overview")
        for btn, switch, lbl, status in getattr(self, "feat_rows", []):
            try:
                if btn is not None:
                    on = bool(switch[0]())
                    btn.config(text="ON" if on else "OFF", bg=GREEN if on else "#4b5563", activebackground=GREEN if on else "#4b5563")
                txt = " ".join(str(status() or "").split())
                lbl.config(text=txt if len(txt) <= 260 else txt[:257].rstrip() + "...")
            except Exception:
                lbl.config(text="")

    # ---------------- bot positions: every bot's open pretend positions ----------------
    def build_botpos_tab(self, parent):
        bar = tk.Frame(parent, bg=CARD)
        bar.pack(fill="x", padx=6, pady=(6, 2))
        self.bp_farm = tk.StringVar(value="Both farms")
        ttk.Combobox(bar, textvariable=self.bp_farm, values=("Both farms", "Bot Farm", "Slow farm"), width=12,
                     state="readonly").pack(side="left")
        self.bp_breed = tk.StringVar(value="All bots")
        ttk.Combobox(bar, textvariable=self.bp_breed, values=("All bots", "Scalpers", "Long-term"), width=11,
                     state="readonly").pack(side="left", padx=6)
        for v_ in (self.bp_farm, self.bp_breed):
            v_.trace_add("write", lambda *a: self.refresh_botpos())
        self.bp_sum = tk.Label(bar, text="", bg=CARD, fg=MUTED, anchor="w", font=("", 9, "bold"))
        self.bp_sum.pack(side="left", padx=10)
        tk.Label(parent, bg=CARD, fg=MUTED, anchor="w", padx=8, font=("", 8),
                 text="Each bot's own pretend share positions right now. These never touch your balance; double-click a row to see "
                      "its ticker on the chart.").pack(fill="x")
        box = tk.Frame(parent, bg=CARD)
        box.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        cols = ("bot", "kind", "status", "ticker", "contract", "qty", "paid", "worth", "pl", "plpct", "held", "exits", "life")
        heads = ("Bot", "Kind", "Status", "Ticker", "Avg cost / now", "Shares", "Paid", "Worth now", "P/L $", "P/L %", "Held",
                 "Exits", "Bot overall")
        t = self.bp_tree = self.make_tree(box, cols, (130, 150, 110, 60, 150, 70, 80, 80, 80, 60, 70, 230, 80), heads)
        for c_ in ("bot", "kind", "status", "ticker", "contract", "exits", "held"):
            t.column(c_, anchor="w")
        t.tag_configure("up", foreground=GREEN)
        t.tag_configure("down", foreground=RED)
        t.bind("<Double-1>", lambda e: (lambda sel: self.show_on_chart(t.set(sel[0], "ticker")) if sel else None)(t.selection()))
        self._bp_tick()

    def _bp_tick(self):
        """Keeps the open tab current (every few seconds, only while you're looking at it)."""
        try:
            if self.cur_tab() == "Bot positions" and not self.in_bg():
                self.refresh_botpos()
        except Exception:
            E.note_error("_bp_tick")
        self.root.after(3000, self._bp_tick)

    def refresh_botpos(self):
        t = getattr(self, "bp_tree", None)
        if t is None:
            return
        farms = []
        if self.bp_farm.get() in ("Both farms", "Bot Farm") and self.tourn:
            farms.append(("Bot Farm", self.tourn, float(self.session_bar or self.settings.get("bar_minutes", 1))))
        sl = getattr(self, "slow", {}).get("t")
        if self.bp_farm.get() in ("Both farms", "Slow farm") and sl:
            farms.append(("Slow farm", sl, self.SLOW_GRAN / 60))
        breed = self.bp_breed.get()
        rows, paid_all, worth_all, n_bots, unpriced = [], 0.0, 0.0, set(), 0
        live_px = dict(getattr(self, "last", None) or {})
        for farm, tr, bar_min in farms:
            for v in list(tr.vars):
                sc = E.is_scalp(v.name)
                if (breed == "Scalpers" and not sc) or (breed == "Long-term" and sc):
                    continue
                for c, q in list(v.sim.pos.items()):
                    if q <= 0:
                        continue
                    S = live_px.get(c) or tr.last_px.get(c)
                    paid = v.sim.cost.get(c, 0.0)
                    avg = paid / q
                    if not S:  # no price for this ticker yet: don't show it as worthless
                        unpriced += 1
                        n_bots.add((farm, v.uid))
                        rows.append(((farm, 0.0), (E.bot_nickname(v.key()), STRAT_LABELS.get(v.name, v.name),
                                     STATUS_TEXT.get(tr.info.get(id(v), {}).get("status"), ""), c,
                                     f"{fmt_px(avg)} / -", f"{q:,.4g}", f"${paid:,.2f}", "no price yet", "-", "-",
                                     "-", "-", f"{tr.life(v)['ret'] * 100:+.1f}%"), ""))
                        continue
                    worth = v.sim.value_of(c, S)
                    pl = worth - paid
                    bars = max(0, tr.n - v.since.get(c, tr.n))
                    mins = bars * bar_min
                    held = f"{mins:.0f} min" if mins < 120 else f"{mins / 60:.1f} hr" if mins < 2880 else f"{mins / 1440:.1f} days"
                    exits = ", ".join(x for x in (E.risk_text(v.risk),
                                                  f"max {v.traits['maxhold'] * v.tf} bars" if v.traits.get("maxhold") else "",
                                                  f"flat {v.traits['flatby']} min before close" if v.traits.get("flatby") else "")
                                      if x) or "its strategy decides"
                    st = STATUS_TEXT.get(tr.info.get(id(v), {}).get("status"), "")
                    life = tr.life(v)["ret"] * 100
                    rows.append(((farm, -pl), (E.bot_nickname(v.key()), STRAT_LABELS.get(v.name, v.name), st, c,
                                 f"{fmt_px(avg)} / {fmt_px(S)}", f"{q:,.4g}", f"${paid:,.2f}", f"${worth:,.2f}",
                                 f"{pl:+,.2f}", f"{(pl / paid * 100) if paid else 0:+.2f}%", held, exits, f"{life:+.1f}%"),
                                 "up" if pl >= 0 else "down"))
                    paid_all += paid
                    worth_all += worth
                    n_bots.add((farm, v.uid))
        rows.sort(key=lambda r: r[0])
        t.delete(*t.get_children())
        for _, vals, tag in rows:
            t.insert("", "end", values=vals, tags=(tag,))
        pl_all = worth_all - paid_all
        self.bp_sum.config(text=(f"{len(n_bots)} bot{'s' if len(n_bots) != 1 else ''} holding {len(rows)} position"
                                 f"{'s' if len(rows) != 1 else ''}   |   all bots' pretend money together: paid "
                                 f"${paid_all:,.2f}, worth ${worth_all:,.2f}, P/L {pl_all:+,.2f} "
                                 f"({(pl_all / paid_all * 100) if paid_all else 0:+.1f}%)"
                                 + (f"   |   {unpriced} waiting for a price" if unpriced else ""))
                           if rows else ("No bot holds a position right now." if farms else "The farms haven't started yet."),
                           fg=(GREEN if pl_all >= 0 else RED) if rows else MUTED)

    # ---------------- Alpaca account: what's really there, straight from Alpaca ----------------
    @staticmethod
    def occ_text(sym):
        """'SPY250321C00500000' -> ('SPY', 'SPY 500C Mar 21 2025 (option)', expiry timestamp, 'call', 500.0) for an
        option the account may hold from elsewhere; a stock -> ('SPY', 'SPY', None, None, None)."""
        m = re.match(r"^([A-Z.]+?)(\d{6})([CP])(\d{8})$", str(sym or ""))
        if not m:
            return str(sym), str(sym), None, None, None
        root, ymd, cp, k = m.groups()
        exp = datetime.strptime(ymd, "%y%m%d").replace(hour=16)
        K = int(k) / 1000
        return root, f"{root} {K:g}{cp} {exp:%b %d %Y} (option)", exp.timestamp(), "call" if cp == "C" else "put", K

    def alpaca_owner(self, sym):
        """Who in SBF looks after this Alpaca position: (text, can it be sold from the Alpaca tab?)."""
        if self.occ_text(sym)[2]:
            return "An option (SBF trades shares only, so it leaves options alone)", True
        for c, m in (getattr(self, "mirror", None) or {}).items():
            if m.get("symbol") == sym:
                return "Copy of a PRACTICE trade (sold when that trade closes)", False
        rec = (self.cfg.get("alpaca_kept") or {}).get(sym)
        if rec:
            if rec.get("acct", "paper") != self.alpaca_acct():
                return f"Managed by SBF on your {rec.get('acct', 'paper')} account (log in with that key to manage it)", False
            return "Managed by SBF: " + self.managed_rules_text() + (" (kept from before a practice reset)"
                                                                      if rec.get("how") != "adopted" else ""), True
        cur = self.sess_tag() if self.session else None
        if cur in ("alpaca", "live") and self.broker() == "alpaca":
            n = sum(q for c, q in self.pos.items() if tick_of(c) == sym and q > 0)
            if n > 0:
                return f"This {self.acct_name()} session holds {n:,.4g} (sell it from Portfolio)", False
        for tag_, nm_ in (("alpaca", "ALPACA PAPER"), ("live", "LIVE")):
            if tag_ == cur:
                continue
            st = E.load_json(E.state_path(tag_), {}) or {}
            if any(tick_of(c) == sym and q > 0 for c, q in (st.get("pos") or {}).items()):
                return f"Your saved {nm_} session (open it to manage it)", False
        return "Not tracked by SBF", True

    def build_alpaca_tab(self, parent):
        bar = tk.Frame(parent, bg=CARD)
        bar.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Button(bar, text="\u27f3 Refresh", command=lambda: self.refresh_alpaca()).pack(side="left")
        self.al_sell = ttk.Button(bar, text="Sell selected...", style="Plain.TButton", command=self.alpaca_sell_selected)
        self.al_sell.pack(side="left", padx=6)
        ab_ = ttk.Button(bar, text="Let SBF manage selected", style="Plain.TButton", command=self.alpaca_adopt_selected)
        ab_.pack(side="left")
        Tip(ab_, "SBF looks after these share positions with your account's rules (Settings): the safety stop, "
                 "lock-in profit and trailing stop, using the prices Alpaca shows. Works for any stock, even ones no "
                 "bot traded.")
        ttk.Button(bar, text="Stop managing", style="Plain.TButton", command=self.alpaca_release_selected).pack(
            side="left", padx=6)
        self.al_sum = tk.Label(bar, text="", bg=CARD, fg=MUTED, anchor="w", font=("", 9, "bold"))
        self.al_sum.pack(side="left", padx=10)
        tk.Label(parent, bg=CARD, fg=MUTED, anchor="w", padx=8, font=("", 8), justify="left", wraplength=1100, text=(
            "What's really in your Alpaca account (the paper or live one your key belongs to), read straight from Alpaca. "
            "Resetting SBF never closes these. 'Tracked by SBF' says which part of SBF looks after each one.")).pack(fill="x")
        box = tk.Frame(parent, bg=CARD)
        box.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        cols = ("contract", "qty", "avg", "paid", "worth", "pl", "plpct", "who")
        heads = ("Stock", "Shares", "Avg price", "Paid", "Worth now", "P/L $", "P/L %", "Tracked by SBF")
        t = self.al_tree = self.make_tree(box, cols, (200, 80, 90, 90, 90, 90, 70, 420), heads)
        for c_ in ("contract", "who"):
            t.column(c_, anchor="w")
        t.configure(selectmode="extended")
        t.tag_configure("up", foreground=GREEN)
        t.tag_configure("down", foreground=RED)
        self.al_rows = {}
        self._al_tick()

    def _al_tick(self):
        """While you're looking at the tab, it refreshes itself every 30 seconds."""
        try:
            if self.cur_tab() == "Alpaca account" and not self.in_bg():
                self.refresh_alpaca(quiet=True)
        except Exception:
            E.note_error("_al_tick")
        self.root.after(30000, self._al_tick)

    def refresh_alpaca(self, quiet=False):
        if getattr(self, "al_tree", None) is None or getattr(self, "_al_busy", False):
            return
        if not E.ALPACA.get("key"):
            self.al_tree.delete(*self.al_tree.get_children())
            self.al_rows = {}
            return self.al_sum.config(text="Log in to Alpaca (Account > Log in) to see your Alpaca positions.", fg=MUTED)
        self._al_busy = True
        if not quiet:
            self.al_sum.config(text="Asking Alpaca...", fg=MUTED)

        def work():
            try:
                rows = [p_ for p_ in (E.alp_positions() or []) if float(p_.get("qty") or 0) != 0]
                self.q.put(("call", lambda: self._alpaca_fill(rows, None)))
            except Exception as e:
                self.q.put(("call", lambda e=e: self._alpaca_fill(None, str(e))))
        threading.Thread(target=work, daemon=True).start()

    def _alpaca_fill(self, rows, err):
        self._al_busy = False
        if getattr(self, "al_tree", None) is None:
            return
        acct = "ALPACA PAPER" if E.ALPACA.get("paper", True) else "ALPACA LIVE (real money)"
        if rows is None:
            return self.al_sum.config(text=f"Couldn't read your {acct} account: {err}", fg=RED)
        t = self.al_tree
        t.delete(*t.get_children())
        self.al_rows = {}
        paid_all = worth_all = 0.0
        for p_ in sorted(rows, key=lambda r: str(r.get("symbol"))):
            sym = str(p_.get("symbol"))
            _, label, exp, _, _ = self.occ_text(sym)
            qty = float(p_.get("qty") or 0)
            paid = float(p_.get("cost_basis") or 0)
            worth = float(p_.get("market_value") or 0)
            avg = float(p_.get("avg_entry_price") or 0)
            pl = float(p_.get("unrealized_pl") if p_.get("unrealized_pl") not in (None, "") else worth - paid)
            who, can_sell = self.alpaca_owner(sym)
            stock = not exp and p_.get("asset_class", "us_equity") == "us_equity"
            iid = t.insert("", "end", values=(label + ("" if p_.get("side", "long") == "long" else "  (short)"), f"{qty:,.4g}",
                                              f"${avg:,.2f}", f"${paid:,.2f}", f"${worth:,.2f}", f"{pl:+,.2f}",
                                              f"{(pl / abs(paid) * 100) if paid else 0:+.1f}%", who),
                           tags=("up" if pl >= 0 else "down",))
            self.al_rows[iid] = dict(symbol=sym, qty=qty, label=label, can_sell=can_sell and stock,
                                     side=p_.get("side", "long"), paid=paid, stock=stock)
            paid_all += paid
            worth_all += worth
        pl_all = worth_all - paid_all
        stamp = datetime.now().strftime("%H:%M:%S")
        self.al_sum.config(text=(f"{acct}: {len(rows)} position{'s' if len(rows) != 1 else ''}   |   paid ${paid_all:,.2f}   |   "
                                 f"worth ${worth_all:,.2f}   |   P/L {pl_all:+,.2f}   |   as of {stamp}") if rows else
                           f"{acct}: no open positions (as of {stamp}).", fg=(GREEN if pl_all >= 0 else RED) if rows else MUTED)

    def alpaca_sell_selected(self):
        sel = [self.al_rows[i] for i in self.al_tree.selection() if i in getattr(self, "al_rows", {})]
        if not sel:
            return messagebox.showinfo("Sell", "Pick one or more positions in the list first.")
        locked = [r for r in sel if not r["can_sell"] or r["side"] != "long" or r["qty"] <= 0]
        if locked:
            return messagebox.showinfo("Sell", "SBF can't sell these from here: positions an SBF session looks after are "
                                               "sold from that session (that keeps its records right), and SBF only sells "
                                               "long stock positions:\n\n" + "\n".join(r["label"] for r in locked))
        live = not E.ALPACA.get("paper", True)
        msg = (("LIVE ACCOUNT, REAL MONEY.\n\n" if live else "") + "Sell these on your "
               + ("LIVE" if live else "paper") + " Alpaca account now (at the bid, then at market if that doesn't fill)?\n\n"
               + "\n".join(f"{r['qty']:,.4g} {r['label']}" for r in sel))
        if not messagebox.askyesno("Sell on Alpaca", msg, icon="warning" if live else "question"):
            return
        self.al_sum.config(text=f"Selling {len(sel)} position{'s' if len(sel) != 1 else ''} on Alpaca...", fg=MUTED)
        for r in sel:
            self.executor.submit(self._alpaca_close, r["symbol"], r["qty"], "sold from the Alpaca tab")

    def _alpaca_close(self, sym, qty, reason):
        """Sells an Alpaca stock position SBF isn't following (never more than Alpaca says is held). Runs off the UI
        thread."""
        try:
            bk = self.brk()
            if bk.held(sym) <= 0:
                return self.q.put(("call", lambda: self._alpaca_closed(sym, 0, 0.0, reason, "Alpaca no longer holds it")))
            got, avg = bk.sell_shares(sym, qty, False, self.settings.get("limit_wait", 20), True, self._stuck_order)
            self.q.put(("call", lambda: self._alpaca_closed(sym, got, avg, reason, "" if got > 0 else "the order didn't fill")))
        except Exception as e:
            self.q.put(("call", lambda e=e: self._alpaca_closed(sym, 0, 0.0, reason, str(e))))

    def _alpaca_closed(self, sym, got, avg, reason, err=""):
        kept = self.cfg.get("alpaca_kept") or {}
        rec = kept.get(sym)
        _, label, _, _, _ = self.occ_text(sym)
        if got > 0:
            proceeds = got * avg
            basis = (rec["cost"] * min(1.0, got / rec["qty"])) if rec and rec.get("qty") else None
            paper = E.ALPACA.get("paper", True)
            row = dict(time=datetime.now().isoformat(timespec="seconds"), mode="alpaca", acct="ALPACA PAPER" if paper else "LIVE",
                       side="sell", coin=label, qty=got, price=round(avg, 6), usd=round(proceeds, 2),
                       pl="" if basis is None else round(proceeds - basis, 2), reason=reason,
                       who=("managed by SBF (you handed it over)" if rec.get("how") == "adopted" else
                            "kept copy of a PRACTICE trade") if rec else "position SBF wasn't following")
            if paper:
                self.mirror_trades.append(row)
                del self.mirror_trades[:-5000]
                self._trades_dirty = True
            try:
                E.append_trade_csv(row)
            except OSError:
                pass
            self.write(f"ALPACA: sold {got:,.4g} {label} at ${avg:,.2f} ({reason}).", "good")
            if rec:
                rec["qty"] -= got
                rec["cost"] = rec.get("cost", 0) - (basis or 0)
        elif err == "Alpaca no longer holds it" and rec:
            rec["qty"] = 0
        else:
            self.write(f"ALPACA: couldn't sell {label}: {err}", "warn")
        if rec is not None and rec.get("qty", 0) <= 1e-9:
            kept.pop(sym, None)
        if rec is not None:
            self.cfg["alpaca_kept"] = kept
            try:
                E.save_json(CONFIG_PATH, self.cfg)
            except OSError:
                pass
        self.root.after(1500, lambda: self.refresh_alpaca(quiet=True))

    def keep_mirror_copies(self):
        """Practice reset: the copies on the Alpaca paper account stay open there, and SBF keeps looking after them
        with your account's rules (the Alpaca account tab) instead of forgetting them. -> how many."""
        kept = dict(self.cfg.get("alpaca_kept") or {})
        n = 0
        for c, m in (getattr(self, "mirror", None) or {}).items():
            if m.get("symbol") and not m.get("pending") and m.get("qty", 0) > 0:
                old = kept.get(m["symbol"])
                if old:  # two books on the same stock (long-term and scalp): one record for the shares at Alpaca
                    old["qty"] = old.get("qty", 0) + m["qty"]
                    old["cost"] = old.get("cost", 0) + m.get("cost", 0)
                else:
                    kept[m["symbol"]] = {k: v for k, v in dict(m, c=tick_of(c), kept_at=time.time(), acct="paper").items()
                                         if k != "closing"}
                n += 1
        self.mirror = {}
        if n:
            self.cfg["alpaca_kept"] = kept
            try:
                E.save_json(CONFIG_PATH, self.cfg)
            except OSError:
                pass
        return n

    def alpaca_acct(self):
        """'paper' or 'live': which Alpaca account the logged-in key belongs to."""
        return "paper" if E.ALPACA.get("paper", True) else "live"

    def managed_rules_text(self):
        st = self.settings
        bits = [f"stop -{st.get('stop_loss', 0):g}%" if st.get("stop_loss") else "",
                f"target +{st.get('take_profit', 0):g}%" if st.get("take_profit") else "",
                f"trailing {st.get('trail_stop', 0):g}%" if st.get("trail_stop") else ""]
        return ", ".join(b for b in bits if b) or "no rules set yet (Settings > Basics)"

    def alpaca_adopt_selected(self):
        sel = [self.al_rows[i] for i in self.al_tree.selection() if i in getattr(self, "al_rows", {})]
        if not sel:
            return messagebox.showinfo("Let SBF manage", "Pick one or more positions in the list first.")
        ok = [r for r in sel if r["can_sell"] and r["side"] == "long" and r["qty"] > 0 and r.get("stock")]
        if not ok:
            return messagebox.showinfo("Let SBF manage", "Only long stock positions that no SBF session is already "
                                                          "looking after can be handed over.")
        live = self.alpaca_acct() == "live"
        if not messagebox.askyesno("Let SBF manage", ("LIVE ACCOUNT, REAL MONEY.\n\n" if live else "")
                                   + "SBF will sell these by itself when your rules say so (" + self.managed_rules_text()
                                   + "), using the prices Alpaca shows:\n\n" + "\n".join(f"{r['qty']:,.4g} {r['label']}" for r in ok)
                                   + "\n\nYou can stop that any time with 'Stop managing'.", icon="warning" if live else "question"):
            return
        n = self.adopt_alpaca(ok)
        self.write(f"SBF now manages {n} Alpaca position{'s' if n != 1 else ''}: {self.managed_rules_text()}.", "good")
        self.refresh_alpaca(quiet=True)

    def adopt_alpaca(self, rows, how="adopted"):
        """Hands Alpaca positions to SBF's care (rows from the Alpaca tab or from Alpaca itself). -> how many."""
        kept = dict(self.cfg.get("alpaca_kept") or {})
        n = 0
        for r in rows:
            sym = r["symbol"]
            if sym in kept:
                continue
            kept[sym] = dict(symbol=sym, c=sym, qty=float(r["qty"]), cost=float(r.get("paid") or 0),
                             how=how, acct=self.alpaca_acct(), kept_at=time.time())
            n += 1
        self.cfg["alpaca_kept"] = kept
        try:
            E.save_json(CONFIG_PATH, self.cfg)
        except OSError:
            E.note_error("adopt_alpaca")
        return n

    def alpaca_release_selected(self):
        sel = [self.al_rows[i] for i in self.al_tree.selection() if i in getattr(self, "al_rows", {})]
        kept = dict(self.cfg.get("alpaca_kept") or {})
        gone = [r["label"] for r in sel if kept.pop(r["symbol"], None)]
        if not gone:
            return messagebox.showinfo("Stop managing", "Pick positions marked 'Managed by SBF' first.")
        self.cfg["alpaca_kept"] = kept
        try:
            E.save_json(CONFIG_PATH, self.cfg)
        except OSError:
            E.note_error("alpaca_release_selected")
        self.write("SBF no longer manages " + ", ".join(gone) + ". They stay open on Alpaca.")
        self.refresh_alpaca(quiet=True)

    def guard_kept_copies(self):
        """Every minute while the market is open: Alpaca positions SBF manages (handed over by you, or kept from before
        a practice reset) are checked against your account's rules with the prices Alpaca shows, and sold when one
        says so. With 'Let SBF manage every untracked Alpaca stock' on, stocks nothing in SBF follows are picked up too."""
        kept = self.cfg.get("alpaca_kept") or {}
        auto = bool(self.settings.get("alpaca_adopt_all", False))
        if (not kept and not auto) or not E.ALPACA.get("key") or not self.client or getattr(self, "_kept_busy", False):
            return
        if not (0 < E.minutes_to_close(time.time()) <= 390) or datetime.now().weekday() >= 5:
            return
        if time.time() - getattr(self, "_kept_try", 0) < 55:
            return
        self._kept_try, self._kept_busy = time.time(), True

        def work():
            try:
                rows = [p_ for p_ in (E.alp_positions() or []) if float(p_.get("qty") or 0) != 0
                        and p_.get("asset_class", "us_equity") == "us_equity"]
                self.q.put(("call", lambda: self._manage_alpaca(rows)))
            except Exception:
                E.note_error("guard_kept_copies")
            finally:
                self._kept_busy = False
        threading.Thread(target=work, daemon=True).start()

    def _manage_alpaca(self, rows):
        acct = self.alpaca_acct()
        kept = dict(self.cfg.get("alpaca_kept") or {})
        held = {str(p_.get("symbol")): p_ for p_ in rows}
        if self.settings.get("alpaca_adopt_all", False):
            new = []
            for sym, p_ in held.items():
                if sym in kept or p_.get("side", "long") != "long" or self.occ_text(sym)[2]:
                    continue
                if self.alpaca_owner(sym)[0] == "Not tracked by SBF":
                    new.append(dict(symbol=sym, qty=float(p_.get("qty") or 0), paid=float(p_.get("cost_basis") or 0)))
            if new and self.adopt_alpaca(new, how="adopted"):
                kept = dict(self.cfg.get("alpaca_kept") or {})
                self.write(f"SBF picked up {len(new)} Alpaca stock position{'s' if len(new) != 1 else ''} no bot was "
                           f"following ({', '.join(x['symbol'] for x in new)}): {self.managed_rules_text()}.")
        st = self.settings
        stop = float(st.get("stop_loss", 0) or 0) / 100
        take = float(st.get("take_profit", 0) or 0) / 100
        trail = float(st.get("trail_stop", 0) or 0) / 100
        changed = False
        for sym, rec in list(kept.items()):
            if rec.get("acct", "paper") != acct:
                continue  # it's on the other account: only that key can see or sell it
            p_ = held.get(sym)
            if p_ is None:  # sold on Alpaca (or by hand): nothing left to manage
                kept.pop(sym)
                changed = True
                continue
            cost, worth = float(p_.get("cost_basis") or 0), float(p_.get("market_value") or 0)
            if cost <= 0 or time.time() - rec.get("closing", 0) < 120:
                continue
            pl = worth / cost - 1
            if pl > rec.get("peak", -1):
                rec["peak"] = pl
                changed = True
            why = ("safety stop" if stop and pl <= -stop else
                   "lock in profit" if take and pl >= take else
                   "trailing stop" if trail and rec.get("peak", 0) > 0 and rec["peak"] - pl >= trail else None)
            if why:
                rec["closing"] = time.time()
                changed = True
                self.write(f"ALPACA: selling {sym} ({why}, {pl * 100:+.1f}%).", "warn")
                self.executor.submit(self._alpaca_close, sym, min(float(p_.get("qty") or 0), float(rec.get("qty") or 0)),
                                     why + " (managed by SBF)")
        if changed:
            self.cfg["alpaca_kept"] = kept
            try:
                E.save_json(CONFIG_PATH, self.cfg)
            except OSError:
                E.note_error("_manage_alpaca")

    # ---------------- live scalp lab: a farm of scalpers that learns on its own from live 1-minute prices ----------------
    LAB_CAP = 24          # scalpers in the lab
    LAB_TICKERS = 8       # tickers it learns on each day
    LAB_GRAD_EVERY = 30   # minutes between graduations to your farm

    def lab_path(self):
        return os.path.join(E.HOME_DIR, "scalp-lab.json")

    def build_lab_tab(self, parent):
        saved = E.load_json(self.lab_path(), {}) or {}
        self.lab = dict(t=None, day=None, coins=[], status="", last_t=0, bars=0, grads=list(saved.get("grads") or [])[-200:],
                        sent=set(saved.get("sent") or []), best=list(saved.get("best") or []), trades=0, busy=False,
                        days=int(saved.get("days", 0)), last_grad=0)
        self._lab_lock = threading.Lock()
        self.lab_feed = deque(maxlen=400)
        self.lab_log = deque(list(saved.get("log") or [])[-300:], maxlen=300)
        w = parent
        top = tk.Frame(w, bg=BG, padx=8, pady=6)
        top.pack(fill="x")
        tk.Label(top, text="\u26a1 Scalp lab", bg=BG, fg=TEXT, font=("", 13, "bold")).pack(side="left")
        self.lab_tog = tk.Button(top, relief="flat", bd=0, padx=14, pady=4, cursor="hand2", font=("", 9, "bold"), fg="white",
                                 activeforeground="white", command=self.toggle_lab)
        self.lab_tog.pack(side="left", padx=12)
        self.lab_state = tk.Label(top, text="", bg=BG, fg=MUTED, font=("", 9), anchor="w", justify="left")
        self.lab_state.pack(side="left", fill="x", expand=True)
        b = tk.Frame(top, bg=BG)
        b.pack(side="right")
        ttk.Button(b, text="Send selected to my farm", style="Plain.TButton", command=self.lab_send).pack(side="left")
        self.lab_stats = tk.Label(w, text="", bg=BG, fg=TEXT, anchor="w", padx=10, font=("", 9))
        self.lab_stats.pack(fill="x")
        mid = tk.PanedWindow(w, orient="horizontal", bg=BG, sashwidth=5, bd=0)
        mid.pack(fill="both", expand=True, padx=6, pady=(4, 0))
        left, right = tk.Frame(mid, bg=CARD), tk.Frame(mid, bg=CARD)
        mid.add(left, minsize=300, stretch="always")
        mid.add(right, minsize=300, stretch="always")
        tk.Label(left, text="SCALPERS: BEST TO WORST (live, pretend money)", bg=CARD, fg=ACCENT, font=("", 8, "bold"),
                 anchor="w").pack(fill="x", padx=4)
        lf = tk.Frame(left, bg=CARD)
        lf.pack(fill="both", expand=True)
        cols = ("who", "kind", "status", "score", "trades", "winrate", "gate", "gen", "origin")
        self.lab_tree = self.make_tree(lf, cols, (120, 140, 110, 70, 55, 65, 170, 40, 110),
                                       ("Scalper", "Instinct", "Status", "Last scalps", "Scalps", "Win rate", "Volatility gate",
                                        "Gen", "Born"))
        for c_ in ("who", "kind", "status", "gate", "origin"):
            self.lab_tree.column(c_, anchor="w")
        self.lab_tree.tag_configure("up", foreground=GREEN)
        self.lab_tree.tag_configure("down", foreground=RED)
        tk.Label(right, text="LIVE TRADES", bg=CARD, fg=ACCENT, font=("", 8, "bold"), anchor="w").pack(fill="x", padx=4)
        rf = tk.Frame(right, bg=CARD)
        rf.pack(fill="both", expand=True)
        self.lab_feed_tree = self.make_tree(rf, ("when", "who", "what", "res"), (60, 110, 190, 70),
                                            ("Time", "Scalper", "Trade", "Result"))
        for c_ in ("when", "who", "what"):
            self.lab_feed_tree.column(c_, anchor="w")
        self.lab_feed_tree.tag_configure("win", foreground=GREEN)
        self.lab_feed_tree.tag_configure("loss", foreground=RED)
        lo = ttk.Notebook(w, height=96)
        lo.pack(side="bottom", fill="x", padx=6, pady=(4, 6), before=mid)  # always visible under the live lists
        l1, l2, l3 = tk.Frame(lo, bg=BG), tk.Frame(lo, bg=BG), tk.Frame(lo, bg=BG)
        lo.add(l1, text="  Tickers it's learning on  ")
        lo.add(l2, text="  Lab log  ")
        lo.add(l3, text="  How it works  ")
        self.lab_tk_tree = self.make_tree(l1, ("ticker", "price", "move", "vol"), (80, 90, 80, 120),
                                          ("Ticker", "Price", "Today", "Volatility"))
        self.lab_tk_tree.column("vol", anchor="w")
        self.lab_log_tree = self.make_tree(l2, ("time", "what"), (110, 900), ("Time", "What happened"))
        self.lab_log_tree.column("what", anchor="w")
        for t_ in (self.lab_tk_tree, self.lab_log_tree):
            t_.configure(height=3)
        tk.Label(l3, bg=BG, fg=TEXT, justify="left", anchor="nw", wraplength=1100, padx=8, pady=8, font=("", 9), text=(
            "The Scalp lab is a farm of scalpers only, learning on its own from LIVE prices while the market is open. It "
            "never trades your account.\n\n"
            "EACH MORNING it picks up to 8 tickers (yours, then the busiest ones and the ones the Scalp scan found paying), "
            "starts from yesterday's best scalpers plus new ones, and warms up on the day so far.\n"
            "EVERY MINUTE a new 1-minute bar arrives and every scalper decides: in, out, or wait, with real bid/ask costs, "
            "its own stop, target and time limit, and its volatility gate reading VIXY, SPY and the live VIX-like reading.\n"
            "EVERY 15 MINUTES the lab evolves: scalpers that keep losing retire, winners lay eggs (tweaked copies), two winners "
            "can crossbreed, and brand new scalpers are tried, all judged on their last 30 scalps.\n"
            "EVERY 30 MINUTES its best proven scalper (15+ scalps, in profit, winning) graduates to your farm as a "
            "'Scalp lab graduate', where it still has to prove itself. 'Send selected to my farm' does it by hand.\n"
            "AT THE CLOSE its 12 best scalpers are kept, so tomorrow's lab starts from them.")).pack(fill="both", expand=True)
        threading.Thread(target=self._lab_loop, daemon=True).start()
        self.refresh_lab()
        self._lab_tick()

    def toggle_lab(self):
        on = not self.cfg.get("lab_on", False)
        if on and self.essentials_block("The Scalp lab"):
            return
        self.cfg["lab_on"] = on
        E.save_json(CONFIG_PATH, self.cfg)
        self.write("\u26a1 Scalp lab ON: scalpers learn on their own from live prices while the market is open." if on else
                   "\u26a1 Scalp lab OFF: nothing learns in the background (its best scalpers are kept for next time).",
                   "good" if on else None)
        if not on:
            self.lab_save()
        self.refresh_lab()

    def _lab_tick(self):
        try:
            if self.cur_tab() == "Scalp lab" and not self.in_bg():
                self.refresh_lab()
        except Exception:
            E.note_error("_lab_tick")
        self.root.after(2000, self._lab_tick)

    def lab_note(self, text):
        self.lab_log.appendleft((time.time(), text))

    def lab_pick(self):
        """Today's tickers: yours, the ones the Scalp scan found paying, then the busiest ones."""
        res = (getattr(self, "scan", None) or {}).get("res") or {}
        hot = [k for k, v in sorted((res.get("tickers") or {}).items(), key=lambda kv: -(kv[1]["pl"] / kv[1]["paid"] if kv[1]["paid"] else -1))
               if v.get("trades", 0) >= 5 and v["pl"] > 0]
        busy = ["SPY", "QQQ", "NVDA", "TSLA", "AAPL", "AMD", "META", "AMZN", "MSFT", "IWM"]
        return [c for c in dict.fromkeys(list(self.session_coins or []) + hot[:4] + busy) if E.valid_coin(c)][:self.LAB_TICKERS]

    def _lab_bars(self, coins):
        """Finished 1-minute bars for the lab's tickers, lined up on SPY's minutes. -> (closes, extra) or None"""
        raw = {}
        for sym in list(dict.fromkeys(coins + ["SPY", "VIXY"])):
            try:
                d = E.fetch_ohlcv(sym, 60, 470, self.quote)
            except Exception:
                continue
            keep = [i for i, tt in enumerate(d.get("t") or []) if tt + 60 <= time.time()]  # finished bars only
            if len(keep) > 40:
                raw[sym] = {k: [d[k][i] for i in keep] for k in ("c", "h", "l", "v", "t")}
        if "SPY" not in raw:
            return None
        clock = raw["SPY"]["t"]  # SPY trades every minute: the lab's clock
        closes, extra = {}, {}
        for c in coins:
            r = raw.get(c)
            if not r:
                continue
            vol = dict(zip(r["t"], r["v"]))
            closes[c] = E.align_ctx(clock, r["t"], r["c"])
            extra[c] = dict(h=E.align_ctx(clock, r["t"], r["h"]), l=E.align_ctx(clock, r["t"], r["l"]),
                            v=[vol.get(tt, 0.0) for tt in clock], t=list(clock))
        ctx = {"SPY": list(raw["SPY"]["c"])}
        if "VIXY" in raw:
            ctx["VIXY"] = E.align_ctx(clock, raw["VIXY"]["t"], raw["VIXY"]["c"])
        iv = E.ivx_today()
        if iv:
            ctx[E.IVX_KEY] = E.align_ctx(clock, [x[0] for x in iv], [x[1] for x in iv])
        extra[E.CTX_KEY] = ctx
        return closes, extra

    def _lab_start(self, day):
        coins = self.lab_pick()
        self.lab["status"] = f"Starting today's lab on {', '.join(coins)}: reading the day so far..."
        got = self._lab_bars(coins)
        if not got:
            raise RuntimeError("no live 1-minute prices yet")
        closes, extra = got
        coins = [c for c in coins if c in closes]
        if len(coins) < 2:
            raise RuntimeError("too few tickers with live prices")
        opts = dict(E.tournament_opts(self.settings), scalp_share=1.0, scalp_only=True, islands=1, classics=False, window=60)
        t = E.Tournament(coins, E.SCAN_CASH, 0.0, 0.05, 15, opts=opts, cap=self.LAB_CAP, retire_after=4, retire_steps=120)
        for g in self.lab["best"][:12]:  # yesterday's best scalpers come back
            try:
                if len(t.vars) >= t.cap:
                    t._remove(t.vars[-1], "made room for yesterday's best")
                t.add_variant(g["name"], g["params"], None, note="Back from yesterday:", tf=g.get("tf", 1), origin="lab: yesterday",
                              risk=g.get("risk"), traits=g.get("traits"), vetoes=g.get("vetoes"), layers=g.get("layers"))
            except Exception:
                continue
        L = len(next(iter(closes.values())))
        self.lab["status"] = f"Warming up on today's {L} minutes so far..."
        t.replaying = True
        t.warmup(closes, start=min(E.WARM, max(1, L - 2)), extra=extra)
        t.replaying = False
        t.pop_events()
        t.on_trade = lambda v, c, tr, ts: self._lab_trade(v, c, tr, ts)  # from here on: live trades only
        self.lab.update(t=t, day=day, coins=coins, last_t=extra[coins[0]]["t"][-1], bars=L, trades=0, last_grad=time.time())
        self._lab_tk({c: closes[c] for c in coins}, extra)
        self.lab["days"] += 1
        self.lab_note(f"Started the lab for {day}: {len(t.vars)} scalpers on {', '.join(coins)}"
                      + (f" ({min(12, len(self.lab['best']))} back from yesterday)" if self.lab["best"] else "") + ".")

    def _lab_tk(self, closes, extra):
        """Price, move since today's open and volatility state of each lab ticker (for its list)."""
        day0 = E.ny_time(E.ny_date(time.time()).isoformat(), 9, 30)
        out = {}
        for c, p in closes.items():
            tt = extra[c]["t"]
            i0 = next((i for i, x in enumerate(tt) if x >= day0), 0)
            out[c] = dict(px=p[-1], move=(p[-1] / p[i0] - 1) * 100 if p[i0] else 0.0,
                          reg=E.vol_regime(closes, extra, c)[0])
        self.lab["tk"] = out

    def _lab_trade(self, v, c, tr, ts):
        self.lab["trades"] += 1
        o = tr.get("contract")
        self.lab_feed.appendleft(dict(t=ts or time.time(), who=E.bot_nickname(v.key()), side=tr["side"], pl=tr.get("pl"),
                                      usd=tr.get("usd"), what=E.contract_text(dict(o, under=c)) if o else c))

    def _lab_step(self):
        lab, t = self.lab, self.lab["t"]
        got = self._lab_bars(lab["coins"])
        if not got:
            return
        closes, extra = got
        times = extra[lab["coins"][0]]["t"]
        new = [i for i, tt in enumerate(times) if tt > lab["last_t"]]
        for i in new:  # usually one new minute (more after a hiccup)
            t.step({c: E.View(closes[c], i + 1) for c in lab["coins"]}, E.slice_extra(extra, i + 1))
            lab["last_t"] = times[i]
            lab["bars"] += 1
        if new:
            self._lab_tk({c: closes[c] for c in lab["coins"]}, extra)
        for e in t.pop_events():
            if not e.startswith(("Spawned", "Dropped", "Bred", "Random new")):
                self.lab_note(e)
        if new and time.time() - lab["last_grad"] >= self.LAB_GRAD_EVERY * 60:
            lab["last_grad"] = time.time()
            self.q.put(("call", self.lab_graduate))

    def _lab_loop(self):
        while True:
            time.sleep(5)
            lab = self.lab
            try:
                if not self.cfg.get("lab_on", False):
                    lab["status"] = "Off. Nothing learns in the background: the lab only learns while this is ON."
                    continue
                if not self.client:
                    lab["status"] = "Log in to Alpaca (Account menu): the lab learns from live prices."
                    continue
                clk = E.market_clock()
                if not clk["is_open"]:
                    if lab["t"] is not None:
                        self.lab_save(close=True)
                    lab["status"] = ("Market closed. The lab wakes up at the open, "
                                     + datetime.fromtimestamp(clk["next_open"]).strftime("%a %I:%M %p").replace(" 0", " ")
                                     + ". (Free roam's scalp roams can keep learning from past days.)")
                    continue
                day = E.ny_date(time.time()).isoformat()
                if lab["t"] is None or lab["day"] != day:
                    if lab["t"] is not None:
                        self.lab_save(close=True)
                    with self._lab_lock:
                        self._lab_start(day)
                    continue
                if time.time() - lab["last_t"] < 120:  # the next minute isn't finished yet
                    left = int(lab["last_t"] + 120 - time.time())
                    lab["status"] = (f"Learning live on {len(lab['coins'])} tickers   |   minute {lab['bars']}   |   "
                                     f"next bar in {max(0, left)} s")
                    continue
                with self._lab_lock:
                    self._lab_step()
                if lab["bars"] % 10 == 0:
                    self.lab_save()
            except Exception as e:
                if isinstance(e, E.NeedLogin) or getattr(e, "code", None) in (401, 403):
                    # not logged in (or the key isn't accepted): nothing to record, just wait for a login
                    lab["status"] = "Waiting for an Alpaca login (Account > Log in)."
                    time.sleep(300)
                    continue
                lab["status"] = f"The lab hit a problem and tries again in a minute: {str(e)[:120]}"
                self._record_error("scalp lab")
                time.sleep(55)

    def lab_best(self, n=12):
        t = self.lab.get("t")
        if not t:
            return []
        ranked = [v for v, _ in t.ranking() if E.scalp_record(v)] or [v for v, _ in t.ranking()]
        return ranked[:n]

    def lab_save(self, close=False):
        """Keeps the lab's best scalpers (tomorrow starts from them), its log and its graduates."""
        lab = self.lab
        best = [E._genes(v) for v in self.lab_best()] or lab["best"]
        lab["best"] = best
        if close and lab.get("t") is not None:
            self.lab_note(f"Closed the lab for {lab['day']}: {lab['trades']} scalps; kept its {len(best)} best scalpers for next time.")
            lab["t"] = None
        try:
            E.save_json(self.lab_path(), dict(best=best, grads=lab["grads"][-200:], sent=sorted(lab["sent"])[-500:],
                                              log=list(self.lab_log)[:300], days=lab["days"]))
        except Exception:
            E.note_error("lab_save")

    def lab_graduate(self, v=None, by_hand=False):
        """The lab's best proven scalper joins your farm (main thread)."""
        lab, t, main = self.lab, self.lab.get("t"), self.tourn
        if not t:
            return
        if v is None:
            cands = [x for x in self.lab_best(6) if x in t.active and (E.scalp_record(x) or ())
                     and len(E.scalp_record(x)) > 15 and (t.score(x) or 0) > 0]
            v = next((x for x in cands if E.bot_nickname(x.key()) not in lab["sent"]), None)
            if v is None:
                return
        nm = E.bot_nickname(v.key())
        if not main or not self.seeded:
            if by_hand:
                messagebox.showinfo("Scalp lab", "Start your farm first (press Start once), then send scalpers to it.")
            return
        if not main.scalpers() and not by_hand:
            return  # your farm has scalpers switched off
        g = E._genes(v)
        nv = main.add_variant(g["name"], g["params"], {c: self.hist[c] for c in self.session_coins}, 1.0,
                              note="Scalp lab graduate:", tf=g["tf"], origin="scalp lab", risk=g["risk"], traits=g["traits"],
                              vetoes=g["vetoes"], layers=g["layers"])
        if nv:
            lab["sent"].add(nm)
            te = E.scalp_record(v) or []
            sc = t.score(v)
            lab["grads"].append(dict(time=time.time(), name=nm, kind=v.name, score=sc))
            self.lab_note(f"{nm} ({STRAT_LABELS.get(v.name, v.name)}) graduated to your farm"
                          + (" (sent by you)" if by_hand else "")
                          + (f": {sc * 100:+.1f}% over its last {len(te) - 1} scalps." if sc is not None and te else "."))
            self.write(f"\u26a1 Scalp lab: {nm} graduated to your farm. It still has to prove itself there.", "good")
            self.refresh_board()
            self.lab_save()

    def lab_send(self):
        t = self.lab.get("t")
        sel = self.lab_tree.selection() if hasattr(self, "lab_tree") else ()
        if not t or not sel:
            return messagebox.showinfo("Scalp lab", "Click a scalper in the list first (the lab must be running).")
        by_name = {E.bot_nickname(v.key()): v for v in t.vars}
        v = by_name.get(sel[0])
        if v:
            self.lab_graduate(v, by_hand=True)
            self.refresh_lab()

    def refresh_lab(self):
        if not hasattr(self, "lab_tree"):
            return
        lab, t = self.lab, self.lab.get("t")
        on = bool(self.cfg.get("lab_on", False))
        self.lab_tog.config(text="ON  —  click to stop" if on else "OFF  —  click to start", bg=GREEN if on else "#4b5563",
                            activebackground=GREEN if on else "#4b5563")
        self.lab_state.config(text=lab.get("status") or "")
        if t:
            sc = t.scalpers()
            win = sum(1 for v in sc if t.info.get(id(v), {}).get("status") == "active")
            self.lab_stats.config(text=(f"{len(sc)} scalpers ({win} winning)   |   generations: {t.evolves}   |   "
                                        f"born today: {len(t.births)}   retired: {len(t.deaths)}   |   scalps today: {lab['trades']:,}   |   "
                                        f"graduated to your farm: {len(lab['grads'])}   |   days learned: {lab['days']}   |   "
                                        f"{self.vol_text()}"))
        else:
            self.lab_stats.config(text=f"Graduated to your farm so far: {len(lab['grads'])}   |   days learned: {lab['days']}   |   "
                                       f"best scalpers kept for next time: {len(lab['best'])}")
        tr = self.lab_tree
        sel = tr.selection()
        try:  # the lab thread may be mid-step: a list that changed underneath just waits for the next refresh
            rows_ = ([v for v, _ in t.ranking()] + [v for v in list(t.vars) if t.score(v) is None]) if t else []
        except (RuntimeError, ValueError, KeyError):
            return
        tr.delete(*tr.get_children())
        if t:
            for v in rows_:
                d = t.info.get(id(v), {})
                te = E.scalp_record(v)
                sc = t.score(v)
                nm = E.bot_nickname(v.key())
                if tr.exists(nm):
                    continue
                tr.insert("", "end", iid=nm, tags=("up" if (sc or 0) > 0 else "down" if sc is not None else "",), values=(
                    nm, STRAT_LABELS.get(v.name, v.name), STATUS_TEXT.get(d.get("status"), ""),
                    f"{sc * 100:+.1f}%" if sc is not None else "-", v.wins + v.losses,
                    f"{v.wins / (v.wins + v.losses) * 100:.0f}%" if v.wins + v.losses else "-",
                    E.VIX_MODES.get(int(v.traits.get("vixmode", 5)), ""), v.gen,
                    {"scalp hatch": "egg", "random scalper": "new", "tweaked": "egg", "bred": "crossbred",
                     "lab: yesterday": "yesterday"}.get(v.origin, v.origin)))
            for x in sel:
                if tr.exists(x):
                    tr.selection_add(x)
        f = self.lab_feed_tree
        f.delete(*f.get_children())
        for x in list(self.lab_feed)[:150]:
            if x["side"] == "buy":
                res_, tag = "opened", ""
            else:
                paid = (x.get("usd") or 0) - (x.get("pl") or 0)
                res_ = f"{(x['pl'] / paid * 100 if paid > 0 and x.get('pl') is not None else 0):+.0f}%"
                tag = "win" if (x.get("pl") or 0) > 0 else "loss"
            f.insert("", "end", tags=(tag,), values=(datetime.fromtimestamp(x["t"]).strftime("%H:%M"), x["who"],
                                                     ("buy " if x["side"] == "buy" else "sell ") + x["what"], res_))
        k = self.lab_tk_tree
        k.delete(*k.get_children())
        for c, x in (lab.get("tk") or {}).items():
            k.insert("", "end", values=(c, fmt_px(x["px"]), f"{x['move']:+.2f}%", x["reg"]))
        g = self.lab_log_tree
        g.delete(*g.get_children())
        for ts, text in list(self.lab_log)[:200]:
            g.insert("", "end", values=(datetime.fromtimestamp(ts).strftime("%b %d %H:%M"), text))

    # ---------------- scalp scan: the scalpers roam the market during the trading day ----------------
    SCAN_EVERY = 300  # seconds between scans while the market is open

    def build_scan_tab(self, parent):
        self.scan = dict(at=0, res=None, status="", busy=False, now=False)
        self.scan_feed = deque(maxlen=200)
        bar = tk.Frame(parent, bg=CARD)
        bar.pack(fill="x", padx=6, pady=(6, 2))
        self.scan_var = tk.BooleanVar(value=bool(self.cfg.get("scalp_scan", True)))
        ttk.Checkbutton(bar, text="Scan the market for scalps during trading hours", variable=self.scan_var,
                        command=lambda: (self.cfg.__setitem__("scalp_scan", bool(self.scan_var.get())),
                                         E.save_json(CONFIG_PATH, self.cfg), self.refresh_scan())).pack(side="left")
        ttk.Button(bar, text="Scan now", style="Plain.TButton", command=self.scan_now).pack(side="left", padx=6)
        ttk.Button(bar, text="Add selected to my tickers", style="Plain.TButton", command=self.scan_add).pack(side="left")
        self.scan_alert_var = tk.BooleanVar(value=bool(self.cfg.get("scan_alerts", True)))
        al = ttk.Checkbutton(bar, text="Alert me", variable=self.scan_alert_var,
                             command=lambda: (self.cfg.__setitem__("scan_alerts", bool(self.scan_alert_var.get())),
                                              E.save_json(CONFIG_PATH, self.cfg)))
        al.pack(side="left", padx=(8, 0))
        Tip(al, "A notification when a setup fires on a ticker where the scalpers are already up today (at least 5 closed scalps).")
        self.scan_lbl = tk.Label(bar, text="", bg=CARD, fg=MUTED, anchor="w", font=("", 9, "bold"))
        self.scan_lbl.pack(side="left", padx=10)
        tk.Label(parent, bg=CARD, fg=MUTED, anchor="w", justify="left", padx=8, font=("", 8), wraplength=1100,
                 text="Every 5 minutes while the market is open, your farm's best scalpers are tried on today's 1-minute "
                      "prices of many tickers (yours, your watchlist and the most-traded ones) with pretend money, real "
                      "bid/ask costs and their own exits. Scalpers that also win here get a bigger say on your farm; ones "
                      "that only win on your own tickers get less. Nothing here trades your account. Free roam can also take your "
                      "scalpers back to past trading days: switch on Free roam with \u26a1 Scalp roams ticked.").pack(fill="x")
        sp = tk.PanedWindow(parent, orient="vertical", bg=BG, sashwidth=5, bd=0)
        sp.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        top, low = tk.Frame(sp, bg=CARD), tk.Frame(sp, bg=CARD)
        sp.add(top, minsize=120, stretch="always")
        sp.add(low, minsize=90, stretch="always")
        tk.Label(top, text="Where scalps are paying today", bg=CARD, fg=TEXT, anchor="w", font=("", 9, "bold")).pack(fill="x", padx=4)
        tb = tk.Frame(top, bg=CARD)
        tb.pack(fill="both", expand=True)
        cols = ("ticker", "move", "vol", "setups", "trades", "winrate", "pl", "plpct", "best", "now")
        self.scan_tree = self.make_tree(tb, cols, (70, 70, 80, 60, 60, 70, 90, 70, 170, 180),
                                        ("Ticker", "Today", "Volatility", "Scalps", "Closed", "Win rate", "P/L $", "P/L %",
                                         "Best scalper here", "Setup right now"))
        self.scan_tree.column("vol", anchor="w")
        for c_ in ("ticker", "best", "now"):
            self.scan_tree.column(c_, anchor="w")
        self.scan_tree.tag_configure("up", foreground=GREEN)
        self.scan_tree.tag_configure("down", foreground=RED)
        self.scan_tree.bind("<Double-1>", lambda e: (lambda sel: self.show_on_chart(sel[0]) if sel and sel[0] in (self.session_coins or [])
                                                     else None)(self.scan_tree.selection()))
        tk.Label(low, text="Setups firing (newest first)", bg=CARD, fg=TEXT, anchor="w", font=("", 9, "bold")).pack(fill="x", padx=4)
        fb = tk.Frame(low, bg=CARD)
        fb.pack(fill="both", expand=True)
        self.scan_feed_tree = self.make_tree(fb, ("time", "ticker", "kind", "who", "price", "why"), (70, 70, 60, 170, 80, 420),
                                             ("Time", "Ticker", "Side", "Scalper", "Stock price", "Why"))
        for c_ in ("time", "ticker", "kind", "who", "why"):
            self.scan_feed_tree.column(c_, anchor="w")
        threading.Thread(target=self._scan_loop, daemon=True).start()
        self.refresh_scan()

    def vol_text(self):
        """What the scalpers' volatility gates are reading right now, in one line."""
        parts = []
        try:
            if self.session_coins and self.hist.get(self.session_coins[0]):
                closes = {c: self.hist[c] for c in self.session_coins if self.hist.get(c)}
                reg, vm, sr, im = E.vol_regime(closes, self.extra_now(), self.session_coins[0])
                parts.append(f"Volatility now: {reg}")
                if vm is not None:
                    parts.append(f"VIXY {vm:+.2f}% in 15 bars")
                if sr is not None:
                    parts.append(f"SPY swinging {sr:.1f}x its usual")
            iv = E.ivx_now()
            if iv:
                path = E.ivx_today()
                old = next((x[1] for x in reversed(path) if x[0] <= time.time() - 900), None)
                parts.append(f"SPY implied vol {iv * 100:.1f}%" + (f" ({(iv / old - 1) * 100:+.1f}% in 15 min)" if old else ""))
        except Exception:
            pass
        return "   ".join(parts) or "Volatility: waiting for live prices"

    def scan_now(self):
        self.scan["now"] = True
        self.scan["status"] = "Scan requested..."
        self.refresh_scan()

    def scan_add(self):
        sel = [x for x in self.scan_tree.selection() if E.valid_coin(x)]
        if not sel:
            return messagebox.showinfo("Scalp scan", "Click one or more tickers in the list first.")
        added = self.add_coins(sel)
        self.write(f"Added {', '.join(sel)} to your tickers" + (" (they join the farm when you next press Start)."
                                                                 if self.running else "."), "good")
        return added

    def scan_universe(self):
        """The tickers the scan roams: yours, your watchlist, then the most-traded ones (30 at most)."""
        out = list(dict.fromkeys(list(self.session_coins or []) + list(self.watchlist()) + list(E.POPULAR)))
        return [c for c in out if E.valid_coin(c)][:int(self.settings.get("scan_size", 30) or 30)]

    def scan_genes(self):
        """The scalpers sent out: the farm's best scalpers (up to 8), or the five scalp instincts when there are none yet."""
        t = self.tourn
        if t and t.scalpers():
            ranked = [v for v, _ in t.ranking() if E.is_scalp(v.name)]
            pick = (ranked + [v for v in t.scalpers() if v not in ranked])[:8]
            return [dict(E._genes(v), uid=v.uid) for v in pick]
        out = []
        for i, n in enumerate(E.SCALP_STRATS):
            tf, r, tr = E.scalp_shape(random.Random(i), 1, None, None)
            out.append(dict(uid=0, name=n, params=dict(E.STRATS[n]["params"]), tf=tf, risk=r, traits=tr, vetoes={}, layers={}))
        return out

    def ivx_tick(self):
        """About once a minute while the market is open: SPY's ~30-day at-the-money implied volatility from its live
        option quotes. It moves with the VIX itself, minute by minute (VIXY, the VIX fund, follows it slowly)."""
        if not self.client or time.time() - getattr(self, "_ivx_at", 0) < 55 or not E.scalp_share(self.settings):
            return
        if not (0 < E.minutes_to_close(time.time()) <= 390) or datetime.now().weekday() >= 5:
            return
        self._ivx_at = time.time()
        S = self.last.get("SPY") or (E.spot_many(["SPY"]) or {}).get("SPY")
        if not S:
            return
        now = datetime.now()
        rows = E.option_chain("SPY", (now + timedelta(days=20)).strftime("%Y-%m-%d"), (now + timedelta(days=45)).strftime("%Y-%m-%d"),
                              None, S * 0.98, S * 1.02)
        iv = E.ivx_from_chain(rows, S)
        if iv:
            E.ivx_add(time.time(), iv)

    def _scan_loop(self):
        while True:
            time.sleep(5 if self.scan.get("now") else 20)
            try:
                self.ivx_tick()
            except Exception:
                E.note_error("_scan_loop")
            try:
                if not self.cfg.get("scalp_scan", True) and not self.scan.get("now"):
                    self.scan["status"] = "Off"
                    continue
                if not self.client:
                    self.scan["status"] = "Log in to Alpaca (Account menu): the scan needs its price data"
                    continue
                if not E.scalp_share(self.settings):
                    self.scan["status"] = "Scalpers are off (Settings > Learning, and a decision speed of 5 min or faster)"
                    continue
                clk = E.market_clock()
                if not clk["is_open"]:
                    self.scan["now"] = False
                    self.scan["status"] = ("Market closed. Scanning starts when it opens "
                                           + datetime.fromtimestamp(clk["next_open"]).strftime("%a %I:%M %p").replace(" 0", " "))
                    continue
                if time.time() - self.scan["at"] < self.SCAN_EVERY and not self.scan.get("now"):
                    left = int(self.SCAN_EVERY - (time.time() - self.scan["at"]))
                    self.scan["status"] = f"Next scan in {left // 60}:{left % 60:02d}"
                    continue
                self.scan.update(now=False, busy=True)
                self._scan_once()
            except Exception as e:
                self.scan["status"] = f"Scan paused: {e}"
            finally:
                self.scan["busy"] = False
                self.q.put(("call", self.refresh_scan))

    def _scan_once(self):
        uni, genes = self.scan_universe(), self.scan_genes()
        ctx = {}
        for sym in ("VIXY", "SPY"):  # the volatility gate and market studies need these lined up with every ticker
            try:
                d = E.fetch_ohlcv(sym, 60, 470, self.quote)
                if d.get("c") and len(d["c"]) == len(d.get("t") or ()):
                    ctx[sym] = dict(c=d["c"], t=d["t"])
            except Exception:
                E.note_error("_scan_once")
        bars = {}
        for i, sym in enumerate(uni):
            self.scan["status"] = f"Reading today's prices: {sym} ({i + 1} of {len(uni)})"
            try:
                d = E.fetch_ohlcv(sym, 60, 470, self.quote)
                if d.get("c") and len(d["c"]) == len(d.get("t") or ()):
                    bars[sym] = dict(c=d["c"], h=d["h"], l=d["l"], v=d["v"], t=d["t"])
            except Exception:
                continue
        self.scan["status"] = f"Trying {len(genes)} scalpers on {len(bars)} tickers..."
        job = dict(bars=bars, genes=genes, day=E.ny_date(time.time()).isoformat(), sspread=E.SCALP_SPREAD[0],
                   slip=E.cost_model(self.settings)[1], ctx=dict(ctx, **({E.IVX_KEY: dict(c=[x[1] for x in E.ivx_today()], t=[x[0] for x in E.ivx_today()])}
                                                         if E.ivx_today() else {})))
        res = self._scan_run(job)
        self.q.put(("call", lambda: self.scan_done(res)))

    @staticmethod
    def _scan_run(job):
        """Runs a scalp scan on its own processor core (so the window never stutters), or here if that isn't possible."""
        try:
            import concurrent.futures as cf
            import multiprocessing as mp
            with cf.ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn")) as ex:
                return ex.submit(E.scan_scalps, job).result(timeout=900)
        except Exception:
            return E.scan_scalps(job)

    def scan_done(self, res):
        self.scan.update(at=time.time(), res=res, status="")
        t = self.tourn
        if t:  # each scalper's results on the wider market count toward how much the farm trusts it
            for v in t.vars:
                b = res["bots"].get(v.uid)
                if b and v.uid:
                    v.scan = list(b)
        stamp = datetime.now().strftime("%H:%M")
        fresh = []
        for x in res.get("now", []):
            key = (x["ticker"], x["bot"], stamp)
            if any((f["ticker"], f["bot"], f["time"]) == key for f in self.scan_feed):
                continue
            row = dict(x, time=stamp)
            self.scan_feed.appendleft(row)
            fresh.append(row)
        hot = [f for f in fresh if (res["tickers"].get(f["ticker"]) or {}).get("trades", 0) >= 5
               and (res["tickers"].get(f["ticker"]) or {}).get("pl", 0) > 0]
        if hot and self.cfg.get("scan_alerts", True):
            r0 = res["tickers"][hot[0]["ticker"]]
            self.notify("SBF scalp setup", f"{hot[0]['ticker']} {(hot[0]['kind'] or 'setup').upper()} "
                                           f"({STRAT_LABELS.get(hot[0]['bot'], hot[0]['bot'])}): scalps there are "
                                           f"{r0['pl'] / r0['paid'] * 100 if r0['paid'] else 0:+.1f}% today"
                                           + (f" (+{len(hot) - 1} more)" if len(hot) > 1 else ""), important=True)
        if fresh:
            self.write("Scalp scan: " + ", ".join(f"{f['ticker']} {f['kind'] or 'setup'} ({STRAT_LABELS.get(f['bot'], f['bot'])})"
                                                  for f in fresh[:6]) + (f" and {len(fresh) - 6} more" if len(fresh) > 6 else "")
                       + " firing right now (scan only, not traded).")
        self.refresh_scan()

    def refresh_scan(self):
        if not hasattr(self, "scan_tree"):
            return
        sc = self.scan
        res = sc.get("res")
        when = (f"last scan {datetime.fromtimestamp(sc['at']).strftime('%I:%M %p').lstrip('0')}" if sc.get("at") else "no scan yet")
        self.scan_lbl.config(text=f"{sc.get('status') or 'Ready'}   |   {when}   |   {self.vol_text()}")
        if self.cur_tab().find("Scalp scan") < 0 and res is not None and getattr(self, "_scan_drawn", None) is res:
            return
        self._scan_drawn = res
        t = self.scan_tree
        t.delete(*t.get_children())
        if res:
            now = {}
            for x in res.get("now", []):
                now.setdefault(x["ticker"], []).append(f"{x['kind'] or 'setup'} ({STRAT_LABELS.get(x['bot'], x['bot'])})")
            rows = sorted(res["tickers"].items(), key=lambda kv: -(kv[1]["pl"] / kv[1]["paid"] if kv[1]["paid"] else -1))
            for sym, r in rows:
                pct = r["pl"] / r["paid"] * 100 if r["paid"] else 0.0
                t.insert("", "end", iid=sym, tags=("up" if r["pl"] >= 0 else "down",), values=(
                    sym, f"{r.get('move', 0):+.2f}%", r.get("regime") or "-", r["setups"], r["trades"],
                    f"{r['wins'] / r['trades'] * 100:.0f}%" if r["trades"] else "-", f"{r['pl']:+,.0f}", f"{pct:+.1f}%",
                    STRAT_LABELS.get(r["best"], r["best"] or "-"), "; ".join(now.get(sym, [])) or ""))
        f = self.scan_feed_tree
        f.delete(*f.get_children())
        for x in self.scan_feed:
            f.insert("", "end", values=(x["time"], x["ticker"], (x.get("kind") or "").upper(), STRAT_LABELS.get(x["bot"], x["bot"]),
                                        fmt_px(x.get("price")), x.get("why", "")))

    # ---------------- watchlist ----------------
    def build_watchlist(self, parent):
        bar = tk.Frame(parent, bg=CARD)
        bar.pack(fill="x", padx=6, pady=6)
        self.watch_entry = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.watch_entry, width=10)
        e.pack(side="left")
        e.bind("<Return>", lambda ev: self.watch_add())
        ttk.Button(bar, text="Add", style="Accent.TButton", command=self.watch_add).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="Browse tickers...", style="Plain.TButton", command=self.open_coin_browser).pack(side="left", padx=4)
        for txt, cmd, tip in (("Buy...", lambda: self.trade_dialog("buy", self._watch_sel()), "Buy the selected coin by hand."),
                              ("Sell...", lambda: self.trade_dialog("sell", self._watch_sel()), "Sell the selected coin by hand."),
                              ("Price alert...", self.watch_alert, "Get notified when the selected coin goes above or below a price."),
                              ("Let the bots trade it", self.watch_to_bots, "Ticks the coin in COINS so the bots trade it next session."),
                              ("Remove", self.watch_remove, "Take the selected coins off the watchlist.")):
            b = ttk.Button(bar, text=txt, style="Plain.TButton", command=cmd)
            b.pack(side="left", padx=(6 if txt == "Buy..." else 2, 0))
            Tip(b, tip)
        self.watch_note = tk.Label(bar, text="", bg=CARD, fg=MUTED, font=("", 8))
        self.watch_note.pack(side="right")
        self.watch_tree = self.make_tree(parent, ("coin", "price", "chg", "d24", "vol", "own", "alert", "bots"),
                                         (70, 120, 110, 90, 130, 120, 150, 90),
                                         ("Ticker", "Price", "Since added", "Today", "Volume today ($)", "You own", "Alert", "Bots trade it"))
        self.watch_tree.configure(selectmode="extended")
        self.watch_tree.bind("<Double-1>", lambda ev: self.show_on_chart(self._watch_sel())
                             if self.on_row(self.watch_tree, ev) and self._watch_sel() else None)
        for btn in ("<Button-3>", "<Button-2>"):
            self.watch_tree.bind(btn, self._watch_menu)
        self.watch_stats = {}
        self.watch_base = dict(self.cfg.get("watch_base") or {})
        self.refresh_watchlist()
        self.root.after(1500, self.start_watch_feed)

    def watchlist(self):
        w = self.cfg.get("watchlist")
        if not isinstance(w, list):
            w = self.cfg["watchlist"] = ["SPY", "QQQ", "NVDA"]
        return [c for c in w if E.valid_coin(c)]

    def _watch_sel(self):
        sel = self.watch_tree.selection() if hasattr(self, "watch_tree") else ()
        return sel[0] if sel else None

    def watch_add(self, coin=None):
        c = (coin or self.watch_entry.get()).strip().upper()
        if not E.valid_coin(c):
            return messagebox.showinfo("Watchlist", "Type a ticker, like SPY or NVDA (or use Browse tickers).")
        w = self.watchlist()
        if c not in w:
            w.append(c)
            self.cfg["watchlist"] = w
            E.save_json(CONFIG_PATH, self.cfg)
            self.write(f"Watching {c}.")
        self.watch_entry.set("")
        self.refresh_watchlist()

    def watch_remove(self):
        sel = list(self.watch_tree.selection())
        if not sel:
            return messagebox.showinfo("Watchlist", "Select coins on the watchlist first.")
        self.cfg["watchlist"] = [c for c in self.watchlist() if c not in sel]
        alerts = self.cfg.get("watch_alerts") or {}
        for c in sel:
            alerts.pop(c, None)
        self.cfg["watch_alerts"] = alerts
        E.save_json(CONFIG_PATH, self.cfg)
        self.refresh_watchlist()

    def watch_to_bots(self):
        sel = list(self.watch_tree.selection())
        if not sel:
            return messagebox.showinfo("Watchlist", "Select a coin first.")
        self.add_coins(sel)
        self.write(f"Ticked {', '.join(sel)} for the bots." + (" They join when you start a new session." if self.session else ""))
        self.refresh_watchlist()

    def watch_alert(self):
        c = self._watch_sel()
        if not c:
            return messagebox.showinfo("Price alert", "Select a coin on the watchlist first.")
        px = self.last.get(c)
        cur = (self.cfg.get("watch_alerts") or {}).get(c, {})
        above = simpledialog.askstring("Price alert", f"{c} is at {fmt_px(px)}.\n\nAlert me when it goes ABOVE this price "
                                                      "(leave empty for none):", initialvalue=str(cur.get("above", "")))
        if above is None:
            return
        below = simpledialog.askstring("Price alert", f"Alert me when {c} goes BELOW this price (leave empty for none):",
                                       initialvalue=str(cur.get("below", "")))
        if below is None:
            return
        try:
            a = {k: float(v.replace(",", "").replace("$", "")) for k, v in (("above", above), ("below", below)) if v.strip()}
        except ValueError:
            return messagebox.showerror("Price alert", "Type plain numbers, like 70000.")
        alerts = self.cfg.get("watch_alerts") or {}
        if a:
            alerts[c] = a
        else:
            alerts.pop(c, None)
        self.cfg["watch_alerts"] = alerts
        E.save_json(CONFIG_PATH, self.cfg)
        self.refresh_watchlist()

    def _watch_menu(self, ev):
        iid = self.watch_tree.identify_row(ev.y)
        if not iid:
            return
        self.watch_tree.selection_set(iid)
        m = self._menu()
        m.add_command(label=iid, state="disabled")
        m.add_separator()
        m.add_command(label="Show on chart", command=lambda: self.show_on_chart(iid))
        m.add_command(label="Buy...", command=lambda: self.trade_dialog("buy", iid))
        m.add_command(label="Sell...", command=lambda: self.trade_dialog("sell", iid))
        m.add_command(label="Price alert...", command=self.watch_alert)
        m.add_command(label="Let the bots trade it", command=self.watch_to_bots)
        m.add_separator()
        m.add_command(label="Remove from watchlist", command=self.watch_remove)
        try:
            m.tk_popup(ev.x_root, ev.y_root)
        finally:
            m.grab_release()

    def start_watch_feed(self):
        """A light background price check for the watchlist and anything you own, so prices (and your balances)
        stay live even while the bot is stopped. One request for all coins every few seconds."""
        if getattr(self, "_watch_thread", None) and self._watch_thread.is_alive():
            return
        self.sync_thread_values()

        def loop():
            last_stats = 0.0
            while True:
                try:
                    # plain values only: Tk must never be touched from this thread
                    coins = list(dict.fromkeys(list(self._watch_coins) + [tick_of(c) for c, q in list(self.pos.items()) if q > 0]))
                    if self.running:  # the main feed already covers the session coins
                        coins = [c for c in coins if c not in self.session_coins]
                    prices = E.spot_many(coins, self._quote_now) if coins else {}
                    stats = None
                    if time.time() - last_stats > 300 and self._watch_coins:
                        last_stats = time.time()
                        try:
                            stats = {d["coin"]: d for d in E.list_products(self._quote_now)}
                        except Exception:
                            stats = None
                    if prices or stats:
                        self.q.put(("watch", (prices, stats)))
                except Exception:
                    E.note_error("loop")
                time.sleep(20 if self._iconic else 5)
        self._watch_thread = threading.Thread(target=loop, daemon=True)
        self._watch_thread.start()

    def sync_thread_values(self):
        """Copies what background threads need into plain attributes (they must not call Tk)."""
        self._watch_coins = tuple(self.watchlist())
        self._quote_now = self.cur_quote()
        try:
            self._iconic = self.root.state() == "iconic"
        except tk.TclError:
            self._iconic = False

    def on_watch(self, prices, stats):
        ts = time.time()
        for c, px in prices.items():
            self.last[c] = px
            self.tick_px.setdefault(c, deque(maxlen=8000)).append((ts, px))
            if c not in self.watch_base:
                self.watch_base[c] = px
                self.cfg["watch_base"] = self.watch_base
        if stats:
            self.watch_stats = stats
        alerts = self.cfg.get("watch_alerts") or {}
        for c, a in list(alerts.items()):
            px = self.last.get(c)
            if not px:
                continue
            for k, hit in (("above", lambda lim: px >= lim), ("below", lambda lim: px <= lim)):
                if k in a and hit(a[k]):
                    msg = f"{c} is {'above' if k == 'above' else 'below'} {fmt_px(a[k])}: now {fmt_px(px)}"
                    self.write("♪ Price alert: " + msg, "good" if k == "above" else "warn")
                    self.notify("SBF price alert", msg, sound="notify")
                    self.alert("trades", "Price alert: " + msg)
                    a.pop(k)
            if not a:
                alerts.pop(c, None)
        self.cfg["watch_alerts"] = alerts
        self.refresh_watchlist()
        if not self.running and self.session:
            self.refresh_view()  # keep Total value live while the bot is stopped

    def refresh_watchlist(self):
        if not hasattr(self, "watch_tree"):
            return
        t = self.watch_tree
        sel = t.selection()
        t.delete(*t.get_children())
        alerts = self.cfg.get("watch_alerts") or {}
        ticked = {c for c, v in self.coin_vars.items() if v.get()}
        for c in self.watchlist():
            px = self.last.get(c)
            st = self.watch_stats.get(c, {})
            base = self.watch_base.get(c)
            q = self.pos.get(c, 0) + self.pos.get(scalp_key(c), 0)
            a = alerts.get(c, {})
            at = " / ".join(f"{'>' if k == 'above' else '<'} {fmt_px(v)}" for k, v in a.items())
            d24 = st.get("change_24h")
            t.insert("", "end", iid=c, tags=(("up",) if (d24 or 0) > 0 else ("down",) if d24 else ()), values=(
                c, fmt_px(px), "-" if not (px and base) else f"{(px / base - 1) * 100:+.2f}%",
                "-" if d24 is None else f"{d24:+.2f}%",
                "-" if not st.get("volume_usd") else f"{st['volume_usd']:,.0f}",
                "-" if q <= 0 else f"{q:,.6f}".rstrip("0").rstrip(".") + (f" (${q * px:,.0f})" if px else ""),
                at or "-", "yes" if c in (self.session_coins if self.session else ticked) else ""))
        t.tag_configure("up", foreground=GREEN)
        t.tag_configure("down", foreground=RED)
        keep = [i for i in sel if t.exists(i)]
        if keep:
            t.selection_set(keep)
        self.watch_note.config(text=f"{len(self.watchlist())} coins  |  prices every 5 s, even while the bot is stopped")

    # ---------------- portfolio tab ----------------
    def build_portfolio(self, parent):
        top = tk.Frame(parent, bg=CARD)
        top.pack(fill="x", padx=6, pady=(6, 2))
        self.pf_stats = {}
        for i, (k, t) in enumerate((("value", "Total value"), ("invested", "In stocks"), ("cash", "Cash"),
                                    ("unreal", "Open profit / loss"), ("real", "Closed profit / loss"), ("fees", "Fees paid"),
                                    ("n", "Positions"))):
            f = tk.Frame(top, bg=CARD)
            f.grid(row=0, column=i, sticky="w", padx=(0, 22))
            tk.Label(f, text=t.upper(), bg=CARD, fg=MUTED, font=("", 7, "bold")).pack(anchor="w")
            l = tk.Label(f, text="-", bg=CARD, fg=TEXT, font=("", 11, "bold"))
            l.pack(anchor="w")
            self.pf_stats[k] = l
        btns = tk.Frame(top, bg=CARD)
        btns.grid(row=0, column=8, sticky="e")
        top.columnconfigure(7, weight=1)
        ttk.Button(btns, text="Buy...", style="Accent.TButton", command=lambda: self.trade_dialog("buy")).pack(side="left")
        ttk.Button(btns, text="Sell...", style="Plain.TButton", command=lambda: self.trade_dialog("sell")).pack(side="left", padx=4)
        self.pf_alloc = tk.Canvas(parent, height=26, bg=CARD, highlightthickness=0)
        self.pf_alloc.pack(fill="x", padx=6, pady=(2, 4))
        self.pf_alloc.bind("<Configure>", lambda e: self.later(self.draw_alloc, 180))
        self.pf_stops = tk.Label(parent, text="", bg=CARD, fg=MUTED, anchor="w", font=("", 8))
        self.pf_stops.pack(fill="x", padx=8)
        self.show_watch = tk.BooleanVar(value=bool(self.cfg.get("pf_watch", True)))
        bar = tk.Frame(parent, bg=CARD)
        bar.pack(fill="x", padx=6)
        ttk.Checkbutton(bar, text="Also show tickers I'm watching but don't hold", variable=self.show_watch,
                        style="Card.TCheckbutton", command=lambda: (self.cfg.__setitem__("pf_watch", self.show_watch.get()),
                                                                    self.refresh_portfolio(force=True))).pack(side="left")
        tk.Label(bar, text="Click a ticker to see its chart.", bg=CARD, fg=MUTED, font=("", 8)).pack(side="right")
        self.pf_scroll = ScrollFrame(parent, CARD, width=600)
        self.pf_scroll.pack(fill="both", expand=True, padx=2, pady=(2, 2))
        self.pf_rows, self.pf_order = {}, []

    def _pf_row(self, c):
        box = self.pf_scroll.inner
        r = tk.Frame(box, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        badge = tk.Canvas(r, width=40, height=40, bg=CARD, highlightthickness=0)
        badge.grid(row=0, column=0, rowspan=2, padx=8, pady=6)
        draw_coin_badge(badge, 4, 4, 32, tick_of(c))
        name = tk.Label(r, text=tick_of(c) + ("  · scalp" if is_scalp_book(c) else ""), bg=CARD, fg=TEXT,
                        font=("", 11, "bold"), cursor="hand2")
        name.grid(row=0, column=1, sticky="w")
        name.bind("<Button-1>", lambda e: self.show_on_chart(tick_of(c)))
        sub = tk.Label(r, text="", bg=CARD, fg=MUTED, font=("", 8))
        sub.grid(row=1, column=1, sticky="w")
        cols = {}
        for j, (k, t) in enumerate((("value", "Worth now"), ("pl", "Profit / loss"), ("avg", "Avg cost / now"),
                                    ("day", "Today"), ("contract", "Shares"))):
            tk.Label(r, text=t, bg=CARD, fg=MUTED, font=("", 7, "bold")).grid(row=0, column=2 + j, sticky="w", padx=(14, 0))
            l = tk.Label(r, text="-", bg=CARD, fg=TEXT, font=("", 9, "bold") if k in ("value", "pl") else ("", 9))
            l.grid(row=1, column=2 + j, sticky="w", padx=(14, 0))
            cols[k] = l
        cols["contract"].config(cursor="hand2")
        cols["contract"].bind("<Button-1>", lambda e: self.contract_info(c))
        share = tk.Canvas(r, width=90, height=8, bg=CARD, highlightthickness=0)
        share.grid(row=1, column=7, padx=(14, 0))
        b = tk.Frame(r, bg=CARD)
        b.grid(row=0, column=8, rowspan=2, sticky="e", padx=8)
        ttk.Button(b, text="Buy", width=5, command=lambda: self.trade_dialog("buy", tick_of(c))).pack(side="left")
        ttk.Button(b, text="Sell", width=5, command=lambda: self.trade_dialog("sell", c)).pack(side="left", padx=(4, 0))
        r.columnconfigure(7, weight=1)
        return dict(frame=r, sub=sub, share=share, **cols)

    def refresh_portfolio(self, force=False):
        if not hasattr(self, "pf_rows"):
            return
        held = [c for c, q in self.pos.items() if q > 0]
        watch = [c for c in (self.session_coins or [c for c, v in self.coin_vars.items() if v.get()]) if c not in held]
        held.sort(key=lambda c: -self.pos[c] * self.position_price(c))
        order = held + (watch if self.show_watch.get() else [])
        if order != self.pf_order or force:
            for c in list(self.pf_rows):
                if c not in order:
                    self.pf_rows.pop(c)["frame"].destroy()
            for c in order:
                if c not in self.pf_rows:
                    self.pf_rows[c] = self._pf_row(c)
                self.pf_rows[c]["frame"].pack_forget()
            for c in order:
                self.pf_rows[c]["frame"].pack(fill="x", padx=4, pady=3)
            self.pf_order = order
            if not order:
                if not hasattr(self, "pf_empty"):
                    self.pf_empty = tk.Label(self.pf_scroll.inner, bg=CARD, fg=MUTED, pady=18, text=(
                        "You don't own any coins right now.\nThe bot buys when a strategy sees a good moment, or press Buy."))
                self.pf_empty.pack(fill="x")
            elif hasattr(self, "pf_empty"):
                self.pf_empty.pack_forget()
        eq = self.equity()
        invested = sum(q * self.position_price(c) for c, q in self.pos.items() if q > 0)
        unreal = sum(q * self.position_price(c) - self.cost.get(c, 0) for c, q in self.pos.items() if q > 0)
        real = 0.0
        for t in self.trades:
            try:
                real += float(t.get("pl") or 0)
            except (TypeError, ValueError):
                pass
        st = self.pf_stats
        st["value"].config(text=f"${eq:,.2f}")
        st["invested"].config(text=f"${invested:,.2f}")
        st["cash"].config(text=f"${self.cash:,.2f}")
        st["unreal"].config(text=f"{unreal:+,.2f}", fg=GREEN if unreal >= 0 else RED)
        st["real"].config(text=f"{real:+,.2f}", fg=GREEN if real >= 0 else RED)
        st["n"].config(text=str(len(held)))
        st["fees"].config(text=f"${self.fees_paid:,.2f}", fg=MUTED)
        stop, take, src = self.account_stops()
        trail = float(self.settings.get("trail_stop", 0) or 0)
        self.pf_stops.config(text=(f"Safety stop: {'off' if not stop else f'-{stop * 100:.1f}%'} (set by {src[0]})   |   "
                                   f"Lock in profit: {'off' if not take else f'+{take * 100:.1f}%'} (set by {src[1]})"
                                   + (f"   |   Trailing stop: {trail:g}%" if trail else "")))
        n24 = int(1440 // self.session_bar)
        for c in order:
            w = self.pf_rows[c]
            q = self.pos.get(c, 0)
            px = self.position_price(c)  # one share's price
            h = self.hist.get(tick_of(c), [])
            S = self.last.get(tick_of(c)) or (h[-1] if h else None)
            day = f"{(h[-1] / h[-n24 - 1] - 1) * 100:+.2f}%" if len(h) > n24 else "-"
            w["day"].config(text=day, fg=(GREEN if day.startswith("+") else RED) if day != "-" else MUTED)
            if q > 0:
                val, basis = q * px, self.cost.get(c, 0)
                pl = val - basis
                w["sub"].config(text=f"holding  |  stock {fmt_px(S)}  |  " + MOOD_TEXT.get(E.market_mood(h)[0], "-"))
                w["value"].config(text=f"${val:,.2f}")
                w["pl"].config(text=f"{pl:+,.2f} ({pl / basis * 100 if basis else 0:+.2f}%)", fg=GREEN if pl >= 0 else RED)
                w["avg"].config(text=f"{fmt_px(basis / q)} / {fmt_px(px)}")
                stake = f"{q:,.4g} (details)"
                share = val / eq if eq else 0
            else:
                w["sub"].config(text=f"not held  |  stock {fmt_px(S)}  |  " + MOOD_TEXT.get(E.market_mood(h)[0], "-"))
                w["value"].config(text="-")
                w["pl"].config(text="-", fg=MUTED)
                w["avg"].config(text="-")
                stake = "-"
                share = 0
            w["contract"].config(text=stake, fg=GREEN if q > 0 else MUTED)
            cv = w["share"]
            cv.delete("all")
            cv.create_rectangle(0, 0, 90, 8, fill=LINE, outline="")
            cv.create_rectangle(0, 0, 90 * min(share, 1), 8, fill=coin_color(c), outline="")
        self.draw_alloc()

    def draw_alloc(self):
        c = self.pf_alloc
        c.delete("all")
        W = c.winfo_width()
        eq = self.equity()
        if W < 50 or eq <= 0:
            return
        parts = [(tick_of(k) + (" scalp" if is_scalp_book(k) else ""), q * self.position_price(k)) for k, q in self.pos.items() if q > 0]
        parts.sort(key=lambda x: -x[1])
        parts.append(("Cash", self.cash))
        x = 0
        for name, v in parts:
            wd = W * max(v, 0) / eq
            if wd <= 0:
                continue
            col = LINE if name == "Cash" else coin_color(name.split(" ")[0])
            c.create_rectangle(x, 2, x + wd, 14, fill=col, outline=CARD)
            if wd > 48:
                c.create_text(x + 4, 20, anchor="w", text=f"{name} {v / eq * 100:.0f}%", fill=MUTED, font=("", 7))
            x += wd

    def contract_info(self, c):
        """Details of the shares the account holds in this book: how many, what they cost, what they're worth, and
        where the account's own exits would sell them."""
        q = self.pos.get(c, 0)
        if q <= 0:
            return messagebox.showinfo("Position", f"No {tick_of(c)} shares held right now.")
        S = self.position_price(c)
        avg = self.cost[c] / q
        stop, take, src = self.account_stops()
        trail = float(self.settings.get("trail_stop", 0) or 0)
        sc = (self.opt.get(c) or {}).get("scalp")
        lines = [f"{q:,.4g} shares of {self.contract_label(c)}", "",
                 f"Price now: {fmt_px(S)}    Average cost: {fmt_px(avg)}",
                 f"Paid: ${self.cost[c]:,.2f}    Worth now: ${q * S:,.2f}    Profit / loss: {q * S - self.cost[c]:+,.2f} "
                 f"({(S / avg - 1) * 100 if avg else 0:+.2f}%)", ""]
        if sc:
            lines += [f"A scalp: the scalpers' own exits sell it at -{sc['sl']:.2f}% ({fmt_px(avg * (1 - sc['sl'] / 100))}) or "
                      f"+{sc['tp']:.2f}% ({fmt_px(avg * (1 + sc['tp'] / 100))}), after {sc.get('mins', 10):.0f} minutes, "
                      f"and always {sc.get('flatby', 15)} minutes before the close."]
        else:
            lines += ["Safety stop: " + (f"sells at {fmt_px(avg * (1 - stop))} (-{stop * 100:.1f}%, set by {src[0]})" if stop else "off"),
                      "Lock in profit: " + (f"sells at {fmt_px(avg * (1 + take))} (+{take * 100:.1f}%, set by {src[1]})" if take else "off")]
        if trail:
            lines.append(f"Trailing stop: {trail:g}% below its high since buying ({fmt_px(self.high.get(c, S) * (1 - trail / 100))} now)")
        self.info_window(f"Position: {self.contract_label(c)}", "\n".join(lines))

    def trade_dialog(self, side, coin=None):
        """Buy shares by hand, or sell the shares you hold."""
        if not self.session:
            live = self.mode_var.get() == "live"
            if not messagebox.askyesno("Trade by hand", f"Open a {'LIVE' if live else 'Practice'} session to trade by hand? "
                                                        "The bot stays off until you press Start."):
                return
            ticked = [c for c, v in self.coin_vars.items() if v.get()] or ["SPY"]
            if coin and coin not in ticked:
                ticked.append(coin)
            if not self.begin_session(ticked):
                return
            self.update_status()
        tickers = list(dict.fromkeys(list(self.session_coins) + self.watchlist() + ([coin] if coin else [])))
        if side == "sell":
            held = [c for c in tickers + list(self.pos) if self.pos.get(c, 0) > 0]
            if not held:
                return messagebox.showinfo("Sell", "You don't hold any shares right now.")
            c = coin if coin in held else held[0]
            if not messagebox.askyesno("Sell", f"Sell your {self.pos[c]:,.4g} shares of {self.contract_label(c)}"
                                               + (" with a REAL order" if self.live else "") + "?"):
                return
            self.pending_who[c] = "you (by hand)"
            return self.sell(c, self.last.get(tick_of(c), 0), "manual")
        coin = coin if coin in tickers else tickers[0]
        w = tk.Toplevel(self.root)
        w.title("Buy shares")
        w.resizable(False, False)
        f = ttk.Frame(w, padding=16)
        f.pack()
        mode = "LIVE: your Alpaca account" if self.live else "Practice: pretend money"
        ttk.Label(f, text=f"Buy shares  ({mode})", font=("", 12, "bold"),
                  foreground=RED if self.live else TEXT).grid(row=0, column=0, columnspan=4, sticky="w")
        cvar, avar = tk.StringVar(value=coin), tk.StringVar(value=f"{self.trade_size(coin):.0f}")
        ttk.Label(f, text="Ticker").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(f, textvariable=cvar, values=tickers, width=8).grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Label(f, text="Spend ($)").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(f, textvariable=avar, width=10).grid(row=2, column=1, sticky="w", pady=(6, 0))
        est = ttk.Label(f, text="", wraplength=440, justify="left")
        est.grid(row=3, column=0, columnspan=4, sticky="w", pady=(10, 0))

        def update(*_):
            c = cvar.get().strip().upper()
            S = self.last.get(c) or (self.hist.get(c) or [0])[-1]
            try:
                a = float(avar.get())
            except ValueError:
                return est.config(text="")
            if not S:
                return est.config(text=f"No price for {c} yet. Start the bot (during market hours) so prices come in.")
            n = a / S
            est.config(text=(f"{c} at {fmt_px(S)}: ${a:,.2f} buys about {n:,.4g} share{'s' if abs(n - 1) > 1e-9 else ''}."
                             + (" Whole shares go in as a limit order at the ask; a part share at market." if self.live else "")))
        for var in (cvar, avar):
            var.trace_add("write", update)
        update()

        def go():
            c = cvar.get().strip().upper()
            S = self.last.get(c) or (self.hist.get(c) or [0])[-1]
            if not S:
                return messagebox.showerror("Buy", f"No price for {c} yet.", parent=w)
            if self.pos.get(c, 0) > 0:
                return messagebox.showinfo("Buy", f"You already hold {c}. Sell it first.", parent=w)
            try:
                a = float(avar.get())
                assert a >= 1
            except Exception:
                return messagebox.showerror("Buy", "Type an amount of at least $1.", parent=w)
            if a > self.cash + 1e-9:
                return messagebox.showerror("Buy", f"That's more than your cash (${self.cash:,.2f}).", parent=w)
            if self.live and not messagebox.askyesno("Buy", f"Place a REAL order for about ${a:,.2f} of {c}?", parent=w):
                return
            self.pending_who[c] = "you (by hand)"
            pct = self.settings["trade_pct"]
            self.settings["trade_pct"] = a / self.start_cash * 100  # this one trade spends what you typed
            try:
                self.open_position(c, S, "manual buy", 1.0, {})
            finally:
                self.settings["trade_pct"] = pct
            w.destroy()
        b = ttk.Frame(f)
        b.grid(row=4, column=0, columnspan=4, sticky="e", pady=(14, 0))
        ttk.Button(b, text="Cancel", command=w.destroy).pack(side="left", padx=6)
        ttk.Button(b, text="Buy", style="Accent.TButton", command=go).pack(side="left")

    def build_farm_strip(self, parent):
        self.farm = tk.Canvas(parent, height=46, bg=CARD, highlightthickness=0)
        self.farm.pack(fill="x", padx=4, pady=(0, 4))
        self.farm_phase = 0
        self.farm_hits = []  # (x0, y0, x1, y1, variant) for clicks
        self.farm_hover = None
        self.farm.bind("<Motion>", self._farm_motion)
        self.farm.bind("<Leave>", lambda e: setattr(self, "farm_hover", None))
        self.farm.bind("<Button-1>", self._farm_click)
        self.farm.bind("<Double-1>", lambda e: (self._farm_click(e), self.bot_detail(quiet=True)) if self._farm_at(e.x, e.y) else None)
        for btn in ("<Button-3>", "<Button-2>"):
            self.farm.bind(btn, lambda e: self.bot_menu(e, self._farm_at(e.x, e.y)))
        # (animated by the shared clock in animate_all)

    def _farm_at(self, x, y):
        for x0, y0, x1, y1, v in self.farm_hits:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return v
        return None

    def _farm_motion(self, e):
        self.farm_hover = self._farm_at(e.x, e.y)

    def _farm_click(self, e):
        v = self._farm_at(e.x, e.y)
        if v and self.board.exists(str(id(v))):
            self.board.selection_set(str(id(v)))
            self.board.see(str(id(v)))

    def animate_all(self):
        """One clock for every animation, so all the dragons move in step. Only the view you can see is drawn,
        nothing is drawn while the window is minimised, and Settings > Animations can slow it or switch it off."""
        mode = self.settings.get("animations", "Full")
        delay = {"Full": 160, "Light": 480}.get(mode, 1000)
        self.sync_thread_values()
        try:
            if mode != "Off" and self.root.state() != "iconic" and not self.moving():
                self.farm_phase = getattr(self, "farm_phase", 0) + 1
                tab = self.cur_tab() if hasattr(self, "nb") else "Bot Farm"
                if "Bot Farm" in tab:
                    self._grid_anim_frame()
                elif tab == "Strategy scoreboard":
                    self._draw_farm()
        except Exception:
            pass
        self.root.after(delay, self.animate_all)

    def _draw_farm(self):
        c = self.farm
        if not c.winfo_ismapped() or self.farm_style.get() != "Dragons":
            return
        c.delete("all")
        ph = self.farm_phase
        W = max(c.winfo_width(), 100)
        self.farm_hits = []
        if not self.tourn:
            msg = "Your bot farm appears here when Wildcard is running." if not self.running else "Waking the bots up..."
            c.create_text(W // 2, 22, text=msg, fill=MUTED)
            c.config(height=46)
            return
        rows = self.tourn.board()
        size, gap = (26, 6) if len(rows) <= 60 else (18, 4)
        per_row = max(1, (W - 8) // (size + gap))
        nrows = (len(rows) + per_row - 1) // per_row
        want_h = nrows * (size + 18) + 6
        if int(c.cget("height")) != want_h:
            c.config(height=want_h)
        amber = "#fbbf24" if DARK else "#d97706"
        hofk = self.hof_keys()
        colors = {"active": GREEN, "slowed": amber, "standby": ACCENT, "benched": "#6b7280", "warming up": "#8b5cf6"}
        awake_now = self.running and self.seeded
        for i, r in enumerate(rows):
            v = r["v"]
            st = r["status"]
            col = colors.get(st, MUTED)
            x0 = 6 + (i % per_row) * (size + gap)
            y0 = 12 + (i // per_row) * (size + 18)
            working = awake_now and st in ("active", "slowed", "standby", "warming up")
            just_traded = bool(v.log) and v.log[-1][0] == self.tourn.n and awake_now
            bob = round(2 * math.sin(ph / 2.2)) if working else 0
            y = y0 + bob
            mood = "happy" if st == "active" else "plain"
            draw_creature(c, v.name, x0, y, size, col, status=st, awake=working, phase=ph, mood=mood,
                        fire=just_traded and v.log[-1][2] == "buy", flash=just_traded and ph % 2 == 0)
            if not working:
                k = ph % 18
                if k < 12:
                    c.create_text(x0 + size + 1 + k / 3, y + 2 - k, text="z" if k < 6 else "Z", fill=MUTED,
                                  font=("", 7 + k // 6, "bold"))
            if just_traded:
                side = v.log[-1][2]
                c.create_text(x0 + size / 2, y - 9, text="$" if side == "sell" else "+", fill=GREEN if side == "buy" else amber,
                              font=("", 9, "bold"))
            if v.key() in hofk:
                c.create_text(x0 + size - 1, y + 5, text="★", fill="#f59e0b", font=("", 8))
            sel = self.board.selection()
            if sel and sel[0] == str(id(v)):
                c.create_rectangle(x0 - 3, y0 - 8, x0 + size + 3, y0 + size + 5, outline=TEXT, dash=(2, 2))
            self.farm_hits.append((x0 - 2, y0 - 8, x0 + size + 2, y0 + size + 4, v))
        hv = self.farm_hover
        if hv is not None and hv in self.tourn.vars:
            d = self.tourn.info.get(id(hv), {})
            nick = E.bot_nickname(hv.key())
            txt = (f"{nick}: {friendly_label(hv.name, hv.params, hv.tf, self.session_bar)}  |  "
                   f"{STATUS_TEXT.get(d.get('status'), d.get('status', ''))}  |  recent {pct_txt(d.get('score'))}  |  "
                   f"{'working' if (awake_now and d.get('status') != 'benched') else 'sleeping'}")
            t = c.create_text(6, 1, text=txt, anchor="nw", fill=TEXT, font=("", 8))
            bb = c.bbox(t)
            c.create_rectangle(bb[0] - 3, bb[1] - 1, bb[2] + 3, bb[3] + 1, fill=CARD, outline=LINE)
            c.tag_raise(t)

    # ---------------- cached look-ups (no file reading on every frame) ----------------
    def _cached(self, path, loader):
        try:
            m = os.path.getmtime(path)
        except OSError:
            m = None
        cache = self.__dict__.setdefault("_file_cache", {})
        if path not in cache or cache[path][0] != m:
            cache[path] = (m, loader())
        return cache[path][1]

    def library(self):
        return self._cached(E.collection_path(), E.load_collection)

    def library_keys(self):
        return {E.bot_key(b) for b in self.library()}

    def hof(self):
        return self._cached(E.hof_path(), E.load_hof)

    def hof_keys(self):
        return {E.bot_key(b) for b in self.hof()}

    # ---------------- Bot Collection ----------------
    def catch_rows(self, rows, source):
        """rows: tournament board rows or bot-file entries. Returns the caught entries."""
        entries = []
        for r in rows:
            if "v" in r:
                entries += E.export_bots([dict(r, bar_minutes=self.session_bar)], self.version)["bots"]
            else:
                entries += E.export_bots([r], self.version)["bots"]
        new, ups, caught = E.catch(entries, source)
        for b in caught:
            shiny = " ✨ SHINY" if b.get("shiny") else ""
            self.write(f"Caught {b['nickname']}{shiny}: {b['rarity']}, level {b['level']} "
                       f"({friendly_label(b['name'], b['params'])}).", "good")
        if ups:
            self.write(f"{ups} bot{'s' if ups != 1 else ''} in your collection levelled up.", "good")
        if getattr(self, "col_win", None) is not None and self.col_win.winfo_exists():
            self.col_refresh()
        return caught

    def catch_selected(self):
        vs = self.selected_variants()
        if not vs:
            return messagebox.showinfo("Catch", "Select one or more bots on the scoreboard (or in the farm above it) first.")
        ids = {id(v) for v in vs}
        self.catch_rows([r for r in self.tourn.board() if id(r["v"]) in ids], "scoreboard")

    ROSTERS = ("farm", "team", "hof")

    def roster_mode(self):
        return "live" if (self.live if self.session else self.mode_var.get() == "live") else "paper"

    def roster(self, mode=None):
        mode = mode or self.roster_mode()
        r = self.cfg.get(f"roster_{mode}", "farm")
        return r if r in self.ROSTERS else "farm"

    def roster_labels(self):
        return {"farm": "Whole farm", "team": f"My team ({len(E.team())}/{E.TEAM_MAX})",
                "hof": f"Hall of Fame only ({len(self.hof())})"}

    def set_roster(self):
        lab = {v: k for k, v in self.roster_labels().items()}
        r = lab.get(self.roster_var.get(), "farm")
        if r == "team" and not E.team():
            messagebox.showinfo("Team", "Your team is empty. Open the Bot Library (Ctrl+B), select up to "
                                        f"{E.TEAM_MAX} bots and press 'Add to team' (or right-click a bot).")
            r = self.roster()
        elif r == "hof" and not self.hof():
            messagebox.showinfo("Hall of Fame", "Your Hall of Fame is empty. Right-click any bot (farm, scoreboard or "
                                                "Library) and choose 'Add to Hall of Fame'.")
            r = self.roster()
        self.cfg[f"roster_{self.roster_mode()}"] = r
        E.save_json(CONFIG_PATH, self.cfg)
        if r != "farm" and not self.wild_var.get():
            self.wild_var.set(True)
            self.toggle_wild()
        self.update_team_label()
        if self.session and self.tourn:
            self.write("The change of who trades applies when Wildcard next starts (File > Reset session).", "warn")

    def update_team_label(self):
        if not hasattr(self, "roster_box"):
            return
        labels = self.roster_labels()
        self.roster_box.config(values=[labels[k] for k in self.ROSTERS])
        self.roster_var.set(labels[self.roster()])

    # ---------------- bots on any farm ----------------
    def farms(self):
        """[(name, farm, minutes per decision bar)] for every farm that's running."""
        out = []
        if self.tourn is not None:
            out.append(("Bot Farm", self.tourn, float(self.session_bar or self.settings.get("bar_minutes", 1))))
        sl = (getattr(self, "slow", None) or {}).get("t")
        if sl is not None:
            out.append(("Slow farm", sl, self.SLOW_GRAN / 60))
        lb = (getattr(self, "lab", None) or {}).get("t")
        if lb is not None:
            out.append(("Scalp lab", lb, 1.0))
        return out

    def farm_of(self, v):
        """(name, farm, minutes per bar) of the farm this bot lives on, or (None, None, None)."""
        return next(((n, t, b) for n, t, b in self.farms() if v in t.vars), (None, None, None))

    def board_rows_of(self, vs):
        """Scoreboard rows for these bots, from whichever farm each lives on (ready for export_bots / catch_rows).
        Before, only the Bot Farm was looked at, so a Slow farm or Scalp lab bot quietly came back as nothing."""
        want = {id(v) for v in vs}
        rows = []
        for _, t, bar in self.farms():
            rows += [dict(r, bar_minutes=bar) for r in t.board() if id(r["v"]) in want]
        return rows

    # ---------------- Hall of Fame: only what you pick ----------------
    def promote_variants(self, vs):
        rows = self.board_rows_of(vs or [])
        if not rows:
            return messagebox.showinfo("Hall of Fame", "That bot isn't on any farm any more, so it couldn't be added.")
        entries = E.export_bots(rows, self.version, note="picked by you")["bots"]
        self.catch_rows(entries, "hall of fame")  # Hall of Fame bots are always in the Library too
        E.induct(entries, self.version)
        self.write(f"\u2605 Added {', '.join(E.bot_nickname(r['v'].key()) for r in rows)} to the Hall of Fame.", "good")
        self.after_hof_change()

    def add_to_main_farm(self, vs):
        """Copies bots from the Slow farm or the Scalp lab into the Bot Farm as new eggs, genes and rule evidence and
        all. A Slow farm bot's pace is turned from hourly candles into the nearest pace on your farm's candles."""
        if not self.tourn or not self.seeded:
            return messagebox.showinfo("Add to farm", "Start your Bot Farm first (press Start once), then add bots to it.")
        added, skipped = [], []
        for v in vs:
            name, t, bar = self.farm_of(v)
            nm = E.bot_nickname(v.key())
            if t is None or t is self.tourn:
                skipped.append(nm)
                continue
            g = E._genes(v)
            tf = g["tf"]
            if name == "Slow farm":
                want = g["tf"] * bar / max(1e-9, float(self.session_bar or 1))
                tf = min(E.TIMEFRAMES, key=lambda x: abs(x - want))
            nv = self.tourn.add_variant(g["name"], g["params"], {c: self.hist[c] for c in self.session_coins}, 1.0,
                                        note=f"From the {name}:", tf=tf, origin=name.lower(), risk=g["risk"],
                                        traits=g["traits"], vetoes=g["vetoes"], layers=g["layers"])
            if nv:
                nv.layer_stats = {k: list(x) for k, x in (v.layer_stats or {}).items()}
                nv.veto_stats = {k: list(x) for k, x in (v.veto_stats or {}).items()}
                added.append(nm)
        if added:
            self.write(f"Added {', '.join(added)} to your Bot Farm. They start as eggs there and have to prove themselves.",
                       "good")
            self.refresh_board()
        if skipped and not added:
            messagebox.showinfo("Add to farm", "Those bots are already on your Bot Farm.")

    def promote_entries(self, entries):
        if not entries:
            return
        E.induct(E.export_bots(entries, self.version)["bots"], self.version)
        self.write(f"\u2605 Added {', '.join(b.get('nickname') or E.bot_nickname(E.bot_key(b)) for b in entries)} "
                   f"to the Hall of Fame.", "good")
        self.after_hof_change()

    def demote_keys(self, keys, names=""):
        keys = set(keys)
        if not keys:
            return
        snap = self.lib_snapshot()
        E.remove_from_hof(keys)
        self.write(f"Removed {names or str(len(keys)) + ' bots'} from the Hall of Fame (still in your Library).")
        self.after_hof_change()
        self.offer_undo(f"Removed {names or str(len(keys)) + ' bots'} from the Hall of Fame.", snap)

    def after_hof_change(self):
        self.update_team_label()
        self.refresh_board()
        if getattr(self, "col_win", None) is not None and self.col_win.winfo_exists():
            self.col_refresh()
        if getattr(self, "hof_win", None) is not None and self.hof_win.winfo_exists():
            self.hof_win.destroy()
            self.open_hof()

    # ---------------- right-click menus ----------------
    def _menu(self):
        return tk.Menu(self.root, tearoff=0, bg=CARD, fg=TEXT, activebackground=ACCENT, activeforeground="white")

    def bot_menu(self, e, v):
        """Right-click menu for a bot on the farm."""
        if v is None or not self.tourn or v not in self.tourn.vars:
            return
        if self.board.exists(str(id(v))) and str(id(v)) not in self.board.selection():
            self.board.selection_set(str(id(v)))
        chosen = self.selected_variants() or [v]
        many = len(chosen) > 1
        name = f"{len(chosen)} bots" if many else E.bot_nickname(v.key())
        in_hof = v.key() in self.hof_keys()
        in_lib = v.key() in self.library_keys()
        m = self._menu()
        m.add_command(label=name, state="disabled")
        m.add_separator()
        if not many:
            m.add_command(label="Details...", command=lambda: self.bot_detail(v))
            m.add_command(label="Show on chart", command=lambda: (self.board.selection_set(str(id(v))) if self.board.exists(str(id(v))) else None,
                self.chart_bot.set(True), self.show_on_chart(
                next((c for n, c, _, _ in reversed(v.log)), self.session_coins[0] if self.session_coins else "Account value"))))
        m.add_command(label="\u25c9 Save to Library" if not in_lib or many else "\u25c9 Update in Library",
                      command=lambda: self.catch_rows([r for r in self.tourn.board() if r["v"] in chosen], "right-click"))
        if in_hof and not many:
            m.add_command(label="\u2715 Remove from Hall of Fame", command=lambda: self.demote_keys([v.key()], E.bot_nickname(v.key())))
        else:
            m.add_command(label="\u2605 Add to Hall of Fame", command=lambda: self.promote_variants(chosen))
        m.add_command(label="Add to my team", command=lambda: self._team_from_variants(chosen))
        m.add_separator()
        if not many:
            m.add_command(label="\u270e See / edit its rules...", command=lambda: self.edit_rules(v))
            m.add_command(label="Backtest this bot...", command=lambda: self.open_backtest(v))
        if len(chosen) == 2:
            m.add_command(label="Compare these two...", command=lambda: self.compare_bots(chosen[0], chosen[1]))
        elif not many and self.selected_variants() and self.selected_variants()[0] is not v:
            other = self.selected_variants()[0]
            m.add_command(label=f"Compare with {E.bot_nickname(other.key())}...", command=lambda: self.compare_bots(v, other))
        m.add_separator()
        m.add_command(label="Trade size +", command=lambda: [self._size(x, 0.25) for x in chosen])
        m.add_command(label="Trade size -", command=lambda: [self._size(x, -0.25) for x in chosen])
        m.add_command(label="Export...", command=self.export_bots)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _size(self, v, d):
        v.size_mult = round(min(max(v.size_mult + d, 0.25), 3.0), 2)
        self.tourn._rate()
        self.refresh_board()

    def _team_from_variants(self, vs):
        caught = self.catch_rows(self.board_rows_of(vs), "team")
        size, full = E.set_team([E.bot_key(b) for b in caught], True)
        self.write(f"Team: {size}/{E.TEAM_MAX}." + (f" {full} didn't fit (the team is full)." if full else ""))
        self.update_team_label()

    def entry_menu(self, e, entries, where):
        """Right-click menu for saved bots (Library cards or Hall of Fame rows)."""
        if not entries:
            return
        hofk = self.hof_keys()
        m = self._menu()
        name = f"{len(entries)} bots" if len(entries) > 1 else (entries[0].get("nickname") or E.bot_nickname(E.bot_key(entries[0])))
        m.add_command(label=name, state="disabled")
        m.add_separator()
        out = [b for b in entries if E.bot_key(b) not in hofk]
        inn = [b for b in entries if E.bot_key(b) in hofk]
        if out:
            m.add_command(label="\u2605 Add to Hall of Fame", command=lambda: self.promote_entries(out))
        if inn:
            m.add_command(label="\u2715 Remove from Hall of Fame", command=lambda: self.demote_keys(
                [E.bot_key(b) for b in inn], ", ".join(b.get("nickname") or E.bot_nickname(E.bot_key(b)) for b in inn)))
        if where == "library":
            m.add_command(label="Add to team", command=lambda: (E.set_team([E.bot_key(b) for b in entries], True), self.col_refresh()))
            m.add_command(label="Remove from team", command=lambda: (E.set_team([E.bot_key(b) for b in entries], False), self.col_refresh()))
        m.add_command(label="Add to farm (they compete and breed)", command=lambda: self._add_entries_to_run(entries))
        if len(entries) == 1:
            m.add_command(label="\u270e See / edit its rules...", command=lambda: self.edit_rules(entry=entries[0]))
            m.add_command(label="Backtest it...", command=lambda: self.open_backtest(None))
        if where == "library" and len(entries) == 2:
            m.add_command(label="\u265b Breed these two", command=self._col_breed)
        m.add_separator()
        if where == "library":
            m.add_command(label="Release from Library", command=self._col_release)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _add_entries_to_run(self, entries):
        if not self.tourn or not self.seeded:
            return messagebox.showinfo("Add bots", "Start the bot with Wildcard ticked first.")
        closes = {c: self.hist[c] for c in self.session_coins}
        have, n = {v.key() for v in self.tourn.vars}, 0
        for b in entries:
            if E.bot_key(b) not in have and self.tourn.add_variant(
                    b["name"], b["params"], closes, b.get("size", 1.0), note="Added:", tf=b.get("tf", 1), risk=b.get("risk"),
                    traits=b.get("traits"), vetoes=b.get("vetoes"), layers=b.get("layers"), origin="library"):
                n += 1
        self.flush_events()
        self.refresh_board()
        self.write(f"Added {n} bots to this run." if n else "Those bots are already on the farm.")

    def open_collection(self):
        if getattr(self, "col_win", None) is not None and self.col_win.winfo_exists():
            return self.col_win.lift()
        w = self.col_win = tk.Toplevel(self.root)
        w.title("Bot Library")
        w.geometry("1020x680")
        top = ttk.Frame(w, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        self.col_head = ttk.Label(top, text="Bot Library", font=("", 13, "bold"))
        self.col_head.pack(anchor="w")
        ttk.Label(top, foreground=MUTED, wraplength=980, text=(
            "Catch bots from the farm, Training camp or the Hall of Fame. Rarity comes from how much they beat "
            "the market, their win rate and how long they kept winning; catching a bot again levels it up. Pick a team "
            f"of up to {E.TEAM_MAX} and tick 'Trade with my team' to run Wildcard with just them.")).pack(anchor="w", pady=(2, 6))
        bar = ttk.Frame(top)
        bar.pack(fill="x")
        ttk.Label(bar, text="Show").pack(side="left")
        self.col_filter = tk.StringVar(value="All")
        cb = ttk.Combobox(bar, textvariable=self.col_filter, values=["All", "Team"] + list(reversed(E.RARITIES)),
                          state="readonly", width=10)
        cb.pack(side="left", padx=(4, 12))
        ttk.Label(bar, text="Sort by").pack(side="left")
        self.col_sort = tk.StringVar(value="Rarity")
        sb = ttk.Combobox(bar, textvariable=self.col_sort, values=["Rarity", "Level", "vs the market", "Newest", "Name"],
                          state="readonly", width=10)
        sb.pack(side="left", padx=4)
        for box in (cb, sb):
            box.bind("<<ComboboxSelected>>", lambda e: self.col_refresh())
        self.col_canvas = tk.Canvas(w, bg=BG, highlightthickness=0)
        ys = ttk.Scrollbar(w, orient="vertical", command=self.col_canvas.yview)
        self.col_canvas.configure(yscrollcommand=ys.set)
        btns = ttk.Frame(w, padding=10)
        btns.pack(side="bottom", fill="x")
        ys.pack(side="right", fill="y")
        self.col_canvas.pack(fill="both", expand=True, padx=(12, 0))
        self.col_canvas.bind("<Configure>", lambda e: self.later(self.col_draw, 180))
        self.col_canvas.bind("<Button-1>", self._col_click)
        self.col_canvas.bind("<Double-1>", lambda e: (self._col_click(e), self._col_team(None)))
        for btn in ("<Button-3>", "<Button-2>"):
            self.col_canvas.bind(btn, self._col_right)
        self.col_canvas.bind("<MouseWheel>", lambda e: self.col_canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.col_canvas.bind("<Button-4>", lambda e: self.col_canvas.yview_scroll(-1, "units"))
        self.col_canvas.bind("<Button-5>", lambda e: self.col_canvas.yview_scroll(1, "units"))
        self.col_sel = set()
        for txt, cmd, tip in (("Add to team", lambda: self._col_team(True), "Your team trades together when "
                               "'Trade with my team' is ticked. Double-click a card to add or remove it."),
                              ("Remove from team", lambda: self._col_team(False), ""),
                              ("\u2605 Add to Hall of Fame", lambda: self.promote_entries(self._col_chosen()) if self._col_chosen()
                               else messagebox.showinfo("Hall of Fame", "Click a card first.", parent=self.col_win),
                               "Your Hall of Fame holds only the bots you pick. 'Bots trading: Hall of Fame only' "
                               "trades with just them."),
                              ("Remove from Hall of Fame", lambda: self.demote_keys([E.bot_key(b) for b in self._col_chosen()]),
                               "They stay in your Library."),
                              ("Add to farm", self._col_add_run, "Drop the selected bots onto the farm: they compete and "
                               "breed, and their children hatch from royal eggs."),
                              ("Breed pair", self._col_breed, "Select exactly two bots and breed a child from them right "
                               "now. It starts as a royal egg on the farm."),
                              ("Export...", self._col_export, "Save the selected bots (or all) to a bot file to share."),
                              ("Release", self._col_release, "Remove the selected bots from your collection.")):
            b = ttk.Button(btns, text=txt, command=cmd, style="Accent.TButton" if txt == "Add to team" else "TButton")
            b.pack(side="left", padx=(0, 6))
            if tip:
                Tip(b, tip)
        self.col_team_lbl = ttk.Label(btns, foreground=MUTED)
        self.col_team_lbl.pack(side="left", padx=10)
        ttk.Button(btns, text="Close", command=w.destroy).pack(side="right")
        self.col_refresh()

    def col_refresh(self):
        self.col_bots = E.load_collection()
        keys = {E.bot_key(b) for b in self.col_bots}
        self.col_sel &= keys
        counts = {r: sum(1 for b in self.col_bots if b.get("rarity") == r) for r in E.RARITIES}
        shiny = sum(1 for b in self.col_bots if b.get("shiny"))
        self.col_head.config(text=f"Bot Library: {len(self.col_bots)} saved  |  {len(self.hof())} in the Hall of Fame  |  "
                                  + "  ".join(f"{n} {r}" for r, n in reversed(list(counts.items())) if n)
                                  + (f"  |  ✨ {shiny} shiny" if shiny else ""))
        n_team = sum(1 for b in self.col_bots if b.get("team"))
        self.col_team_lbl.config(text=f"Team: {n_team}/{E.TEAM_MAX}")
        if hasattr(self, "roster_box"):
            self.update_team_label()
        self.col_draw()

    def col_draw(self):
        c = self.col_canvas
        c.delete("all")
        bots = list(self.col_bots)
        f = self.col_filter.get()
        if f == "Team":
            bots = [b for b in bots if b.get("team")]
        elif f in E.RARITIES:
            bots = [b for b in bots if b.get("rarity") == f]
        srt = self.col_sort.get()
        rk = {r: i for i, r in enumerate(E.RARITIES)}
        if srt == "Rarity":
            bots.sort(key=lambda b: (-rk.get(b.get("rarity"), 0), -int(b.get("level") or 1)))
        elif srt == "Level":
            bots.sort(key=lambda b: -int(b.get("level") or 1))
        elif srt == "vs the market":
            bots.sort(key=lambda b: -float((b.get("lifetime") or {}).get("vs_holding_pct") or -999))
        elif srt == "Newest":
            bots.sort(key=lambda b: str(b.get("caught_at", "")), reverse=True)
        else:
            bots.sort(key=lambda b: str(b.get("nickname", "")))
        W = max(c.winfo_width(), 300)
        cw, ch, g = 230, 150, 12
        per = max(1, (W - g) // (cw + g))
        self.col_cards = []
        if not bots:
            c.create_text(W // 2, 80, fill=MUTED, justify="center", text=(
                "No bots here yet.\nSelect bots on the scoreboard and press 'Catch!', or catch Training camp graduates and "
                "Hall of Fame bots.\nHall of Fame bots are caught automatically."))
        for i, b in enumerate(bots):
            x = g + (i % per) * (cw + g)
            y = g + (i // per) * (ch + g)
            k = E.bot_key(b)
            rc = E.RARITY_COLORS.get(b.get("rarity"), "#9ca3af")
            sel = k in self.col_sel
            c.create_rectangle(x, y, x + cw, y + ch, fill=CARD, outline=TEXT if sel else rc, width=3 if sel else 2)
            c.create_rectangle(x, y, x + cw, y + 26, fill=rc, outline=rc)
            c.create_text(x + 8, y + 13, anchor="w", fill="white", font=("", 10, "bold"),
                          text=("✨ " if b.get("shiny") else "") + b.get("nickname", "?"))
            c.create_text(x + cw - 8, y + 13, anchor="e", fill="white", font=("", 9, "bold"), text=f"Lv {b.get('level', 1)}")
            draw_creature(c, b["name"], x + 8, y + 40, 42, rc, awake=True, phase=0, mood="happy", gold=bool(b.get("shiny")))
            lt = b.get("lifetime") or {}

            def p(key):
                v = lt.get(key)
                return "-" if v is None else f"{v:+.1f}%"
            lines = [f"{STRAT_LABELS.get(b['name'], b['name'])}",
                     f"{b.get('rarity', 'Common')}  |  XP {b.get('xp', 0)}",
                     f"vs the market {p('vs_holding_pct')}",
                     f"win rate {'-' if lt.get('win_rate_pct') is None else str(round(lt['win_rate_pct'])) + '%'}",
                     (E.risk_text(b.get("risk")) or "no own stops")
                     + (f" +{len(b.get('traits') or {})} traits" if b.get("traits") else "")]
            for j, line in enumerate(lines):
                c.create_text(x + 56, y + 36 + j * 16, anchor="w", text=line, fill=TEXT if j == 0 else MUTED,
                              font=("", 9, "bold") if j == 0 else ("", 8))
            settings = "/".join(f"{v:g}" for v in b["params"].values()) + (f", pace x{b.get('tf', 1)}" if b.get("tf", 1) > 1 else "")
            c.create_text(x + 8, y + ch - 12, anchor="w", text=settings, fill=MUTED, font=("", 7))
            tags_txt = (["TEAM"] if b.get("team") else []) + (["\u2605 HALL OF FAME"] if k in self.hof_keys() else [])
            if tags_txt:
                c.create_text(x + cw - 8, y + ch - 12, anchor="e", text="  ".join(tags_txt), fill="#f59e0b", font=("", 8, "bold"))
            self.col_cards.append((x, y, x + cw, y + ch, k))
        rows = (len(bots) + per - 1) // per
        c.configure(scrollregion=(0, 0, W, rows * (ch + g) + g))

    def _col_click(self, e):
        x, y = self.col_canvas.canvasx(e.x), self.col_canvas.canvasy(e.y)
        for x0, y0, x1, y1, k in self.col_cards:
            if x0 <= x <= x1 and y0 <= y <= y1:
                if e.state & 0x0004 or e.state & 0x0001:  # ctrl / shift: add to the selection
                    self.col_sel ^= {k}
                else:
                    self.col_sel = {k}
                break
        else:
            self.col_sel = set()
        self.col_draw()

    def _col_right(self, e):
        x, y = self.col_canvas.canvasx(e.x), self.col_canvas.canvasy(e.y)
        for x0, y0, x1, y1, k in self.col_cards:
            if x0 <= x <= x1 and y0 <= y <= y1:
                if k not in self.col_sel:
                    self.col_sel = {k}
                    self.col_draw()
                return self.entry_menu(e, self._col_chosen(), "library")

    def _col_chosen(self, all_if_none=False):
        bots = [b for b in self.col_bots if E.bot_key(b) in self.col_sel]
        return bots or (list(self.col_bots) if all_if_none else [])

    def _col_team(self, on):
        bots = self._col_chosen()
        if not bots:
            return messagebox.showinfo("Team", "Click a card first (Ctrl-click for several).", parent=self.col_win)
        if on is None:  # double-click toggles
            on = not bots[0].get("team")
        size, full = E.set_team([E.bot_key(b) for b in bots], on)
        if full:
            messagebox.showinfo("Team", f"A team has at most {E.TEAM_MAX} bots. Remove one first.", parent=self.col_win)
        self.col_refresh()

    def _col_add_run(self):
        if not self.tourn or not self.seeded:
            return messagebox.showinfo("Bot Collection", "Start the bot with Wildcard ticked first.", parent=self.col_win)
        bots = self._col_chosen()
        if not bots:
            return messagebox.showinfo("Bot Collection", "Click a card first.", parent=self.col_win)
        closes = {c: self.hist[c] for c in self.session_coins}
        have, n = {v.key() for v in self.tourn.vars}, 0
        for b in bots:
            if E.bot_key(b) in have:
                continue
            if self.tourn.add_variant(b["name"], b["params"], closes, b.get("size", 1.0), note=f"{b.get('nickname')} joined:",
                                      tf=b.get("tf", 1), risk=b.get("risk"), traits=b.get("traits"), vetoes=b.get("vetoes"), layers=b.get("layers"), origin="collection"):
                n += 1
        self.flush_events()
        self.refresh_board()
        self.write(f"Added {n} bots from your collection to this run." if n else "Those bots are already competing.")

    def _col_breed(self):
        bots = self._col_chosen()
        if len(bots) != 2:
            return messagebox.showinfo("Breed", "Select exactly two bots (Ctrl-click) to breed them.", parent=self.col_win)
        if not self.tourn or not self.seeded:
            return messagebox.showinfo("Breed", "Start the bot with Wildcard ticked first.", parent=self.col_win)
        closes = {c: self.hist[c] for c in self.session_coins}
        parents = []
        for b in bots:
            v = next((x for x in self.tourn.vars if x.key() == E.bot_key(b)), None) or self.tourn.add_variant(
                b["name"], b["params"], closes, b.get("size", 1.0), note="Parent joined:", tf=b.get("tf", 1),
                risk=b.get("risk"), traits=b.get("traits"), vetoes=b.get("vetoes"), layers=b.get("layers"), origin="library")
            if v:
                parents.append(v)
        if len(parents) != 2:
            return
        child = self.tourn.breed_pair(parents[0], parents[1], closes)
        self.flush_events()
        self.refresh_board()
        self.write(f"♛ A royal egg joined the farm: {E.bot_nickname(child.key())}, child of "
                   f"{bots[0].get('nickname', '?')} and {bots[1].get('nickname', '?')}.", "good")

    def _col_export(self):
        bots = self._col_chosen(all_if_none=True)
        if not bots:
            return
        p = filedialog.asksaveasfilename(title="Export bots", defaultextension=".json", parent=self.col_win,
                                         initialfile="my-bot-collection.json", filetypes=[("Bot file", "*.json")])
        if p:
            payload = E.export_bots(bots, self.version)
            for out, b in zip(payload["bots"], bots):
                out.update({k: b[k] for k in E.COLLECT_FIELDS if k in b and k != "team"})
            E.save_bots_file(p, payload)
            self.write(f"Exported {len(bots)} collected bots to {p}")

    def _col_release(self):
        bots = self._col_chosen()
        if not bots:
            return messagebox.showinfo("Release", "Click a card first.", parent=self.col_win)
        names = ", ".join(b.get("nickname", "?") for b in bots[:5]) + ("..." if len(bots) > 5 else "")
        if messagebox.askyesno("Release", f"Release {names}? They leave your collection.", parent=self.col_win):
            snap = self.lib_snapshot()
            E.release([E.bot_key(b) for b in bots])
            self.col_sel = set()
            self.col_refresh()
            self.offer_undo(f"Released {names}.", snap)

    # ---------------- bot detail card ----------------
    @staticmethod
    def on_row(tree, e):
        """True when a double-click landed on a row (not on a column heading, which sorts, or empty space)."""
        return tree.identify_region(e.x, e.y) in ("cell", "tree") and bool(tree.identify_row(e.y))

    def _family_strip(self, parent, v, win):
        """Parents and children as little clickable portraits."""
        box = ttk.LabelFrame(parent, text=" Family (click to open) ", padding=(8, 4))
        box.pack(fill="x", pady=(8, 0))
        alive = {x.uid: x for x in self.tourn.vars}
        kids = [x for x in self.tourn.vars if v.uid in x.parents][:6]
        people = [("parent", u) for u in v.parents] + [("child", x.uid) for x in kids]
        if not people:
            ttk.Label(box, text="A founder: no parents on record, no children yet.", foreground=MUTED).pack(anchor="w")
            return
        for role, uid in people:
            x = alive.get(uid)
            cell = tk.Frame(box, bg=CARD, padx=4, pady=2)
            cell.pack(side="left", padx=(0, 6))
            cv = tk.Canvas(cell, width=38, height=38, bg=CARD, highlightthickness=0)
            cv.pack(side="left")
            if x is not None:
                st = self.tourn.info.get(id(x), {}).get("status", "warming up")
                draw_creature(cv, x.name, 4, 6, 30, self.GRID_STATUS.get(st, ("", "#8b5cf6"))[1], status=st, awake=True,
                              phase=0, tags=())
                name = E.bot_nickname(x.key())
            else:
                cv.create_text(19, 19, text="\u2620", fill=MUTED, font=("", 16))
                name = f"#{uid} (gone)"
            head = "\u25b2 Parent" if role == "parent" else "\u25bc Child"
            lab = tk.Label(cell, text=head + "\n" + name, bg=CARD,
                           fg=TEXT if x is not None else MUTED, justify="left", font=("", 8, "bold"),
                           cursor="hand2" if x is not None else "")
            lab.pack(side="left")
            if x is not None:
                for wd in (cell, cv, lab):
                    wd.bind("<Button-1>", lambda e, x=x: self.bot_detail(x, quiet=True))

    def bot_detail(self, v=None, quiet=False):
        v = v or self.selected_variant()
        T = self.farm_of(v)[1] if v else None  # the farm this bot lives on (the Slow farm and Scalp lab too)
        T = T or self.tourn
        if not v or not T:
            if quiet:
                return None
            return messagebox.showinfo("Bot details", "Click a bot on the farm or the scoreboard first.")
        w = tk.Toplevel(self.root)
        w.title(STRAT_LABELS.get(v.name, v.name))
        w.geometry(f"820x{min(880, self.root.winfo_screenheight() - 80)}")
        f = ttk.Frame(w, padding=12)
        f.pack(fill="both", expand=True)
        title = friendly_label(v.name, v.params, v.tf, self.session_bar)
        ttk.Label(f, text=("★ " if v.key() in self.hof_keys() else "") + E.bot_nickname(v.key()) + ":  " + title,
                  font=("", 12, "bold")).pack(anchor="w")
        ttk.Label(f, text=STRAT_TIPS.get(v.name, ""), foreground=MUTED, wraplength=720).pack(anchor="w", pady=(2, 8))
        stats = ttk.Frame(f)
        stats.pack(fill="x")
        info_lbls = {}
        for i, key in enumerate(("Status", "Recent gain", "vs the market (recent)", "Lifetime return", "Lifetime vs the market",
                                 "Win rate", "Trades", "Worst drop", "Time winning", "Best market", "Own stops",
                                 "Family", "Traits", "Fees paid", "Health / power", "Never-rules", "Twin test", "Skips coins",
                                 "Badges", "Island", "Backtests", "Exams", "Good at")):
            ttk.Label(stats, text=key, foreground=MUTED).grid(row=i // 3, column=(i % 3) * 2, sticky="w", padx=(0, 6), pady=2)
            l = ttk.Label(stats, text="-", font=("", 10, "bold"), wraplength=230)
            l.grid(row=i // 3, column=(i % 3) * 2 + 1, sticky="w", padx=(0, 22), pady=2)
            info_lbls[key] = l
        self._family_strip(f, v, w)
        stack = ttk.LabelFrame(f, text=" Rule stack (each layer evolves on its own) ", padding=(8, 4))
        stack.pack(fill="x", pady=(8, 0))
        stack_lbls = []
        for i in range(5):
            a = ttk.Label(stack, text="", font=("", 9, "bold"), width=19)
            a.grid(row=i, column=0, sticky="nw")
            b = ttk.Label(stack, text="", wraplength=600, justify="left")
            b.grid(row=i, column=1, sticky="w", pady=1)
            stack_lbls.append((a, b))
        crow = ttk.Frame(f)
        crow.pack(fill="x", pady=(10, 2))
        ttk.Label(crow, text="Entries and exits on").pack(side="left")
        coin = tk.StringVar(value=self.session_coins[0] if self.session_coins else "")
        cb = ttk.Combobox(crow, textvariable=coin, values=self.session_coins, state="readonly", width=8)
        cb.pack(side="left", padx=6)
        ttk.Label(crow, text="▲ buy   ▼ sell   (last 300 decisions)", foreground=MUTED).pack(side="left")
        cv = tk.Canvas(f, height=140, bg=CARD, highlightthickness=0)
        cv.pack(fill="both", expand=True)

        def paint(_=None):
            if not w.winfo_exists():
                return
            lf = T.life(v)
            d = T.info.get(id(v), {})
            st = STATUS_TEXT.get(d.get("status"), d.get("status", "removed")) if v in T.vars else "Removed"
            hrs = v.win_steps * self.session_bar / 60
            parents = T.lineage.get(v.uid, {}).get("parents", [])
            fam = {"starter": "starter bot", "classic": f"famous strategy: {v.classic}", "bred": f"bred from #{' and #'.join(map(str, parents))}",
                   "tweaked": f"tweaked copy of #{parents[0]}" if parents else "tweaked copy"}.get(v.origin, v.origin or "-")
            vals = {"Status": st, "Recent gain": pct_txt(d.get("score")), "vs the market (recent)": pct_txt(d.get("edge")),
                    "Lifetime return": pct_txt(lf["ret"]), "Lifetime vs the market": pct_txt(lf["edge"]),
                    "Win rate": "-" if lf["winrate"] is None else f"{lf['winrate'] * 100:.0f}% ({lf['wins']}W/{lf['losses']}L)",
                    "Trades": str(lf["trades"]), "Worst drop": f"{lf['maxdd'] * 100:.1f}%",
                    "Time winning": f"{hrs:.1f} h" if hrs >= 1 else f"{hrs * 60:.0f} min",
                    "Best market": MOOD_TEXT.get(lf["best_mood"], "not sure yet") if lf["best_mood"] else "not sure yet",
                    "Own stops": E.risk_text(v.risk) or "none", "Family": f"#{v.uid}, {fam}, generation {v.gen}",
                    "Health / power": f"{T.health(v) * 100:.0f}% health, power {v.power:.1f} / 10",
                    "Never-rules": E.vetoes_text(v.vetoes, v.veto_stats) or "none",
                    "Twin test": "not run yet" if v.twin is None else
                    (f"passed ({v.twin * 100:+.1f}% vs the market on older prices)" if v.twin > 0 else
                     f"failed ({v.twin * 100:+.1f}%): maybe just lucky, counts for less"),
                    "Skips coins": ", ".join(c_ for c_, x in v.coin_pl.items() if x[0] >= 3 and x[1] < -1.5) or "none",
                    "Exams": E.exam_text(v, T.serial),
                    "Good at": ("; ".join(f"{n_} ({x_:+.3f}% a decision)" for n_, x_ in E.good_at(v)[:3])
                                or "still learning when it does best"),
                    "Badges": ", ".join(f"{n}" for n, _ in self.achievements(v, lf)) or "none yet",
                    "Island": f"{v.island + 1} of {int(T.o.get('islands', 1) or 1)}",
                    "Backtests": ("; ".join(f"{b_['ret']:+.1f}% ({b_['edge']:+.1f}% vs market, {b_['segments_passed']}/{b_['segments']} parts)"
                                            for b_ in v.backtests[-2:]) if v.backtests else "none saved"),
                    "Traits": (E.traits_text(v.traits) or "none yet") + (
                        "  |  " + next((cl["note"] for cl in E.CLASSICS if cl["title"] == v.classic), "") if v.classic else ""), "Fees paid": f"${v.sim.fees:,.2f}"}
            for k, l in info_lbls.items():
                l.config(text=vals[k])
            for (a, b), (title, txt) in zip(stack_lbls, E.rule_stack(v)):
                a.config(text=title)
                b.config(text=txt)
            cv.delete("all")
            c = coin.get()
            h = self.hist.get(c, [])[-300:]
            W, H = cv.winfo_width(), cv.winfo_height()
            if len(h) < 2 or W < 60:
                cv.create_text(W // 2, H // 2, text="No price history yet", fill=MUTED)
                return
            L = len(self.hist.get(c, []))
            first_n = T.n - (len(h) - 1)
            marks = [(n - first_n, side, px) for n, cc, side, px in v.log if cc == c and n >= first_n]
            lo, hi = min(h + [m[2] for m in marks]), max(h + [m[2] for m in marks])
            pad = (hi - lo) * 0.08 or hi * 0.001 or 1
            lo, hi = lo - pad, hi + pad
            left = 64

            def X(i):
                return left + i * (W - left - 8) / (len(h) - 1)

            def Y(p):
                return H - 14 - (p - lo) / (hi - lo) * (H - 28)
            for k in range(4):
                p = lo + (hi - lo) * k / 3
                cv.create_line(left, Y(p), W - 8, Y(p), fill=LINE)
                cv.create_text(left - 6, Y(p), text=f"{p:,.2f}" if p >= 1 else f"{p:,.5f}", anchor="e", fill=MUTED, font=("", 8))
            cv.create_line(*[z for i, p in enumerate(h) for z in (X(i), Y(p))], fill=ACCENT, width=2)
            for i, side, px in marks:
                x, y = X(i), Y(px)
                pts = (x, y + 3, x - 6, y + 13, x + 6, y + 13) if side == "buy" else (x, y - 3, x - 6, y - 13, x + 6, y - 13)
                cv.create_polygon(*pts, fill=GREEN if side == "buy" else RED, outline="")
            cv.create_text(W - 10, 4, anchor="ne", fill=MUTED, font=("", 8),
                           text=f"{len(marks)} trades on {c} shown, one bar = {E.fmt_bar(self.session_bar)}")
            _ = L
        cb.bind("<<ComboboxSelected>>", paint)
        cv.bind("<Configure>", paint)
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(8, 0))

        def export_one():
            p = filedialog.asksaveasfilename(title="Export bot", defaultextension=".json", parent=w,
                                             initialfile=f"{v.name.lower().replace(' ', '-')}-bot.json", filetypes=[("Bot file", "*.json")])
            if p:
                rows = self.board_rows_of([v])
                E.save_bots_file(p, E.export_bots(rows, self.version))
                self.write(f"Exported {title} to {p}")

        def to_hof():
            self.promote_variants([v])
            paint()

        def size(d):
            v.size_mult = round(min(max(v.size_mult + d, 0.25), 3.0), 2)
            T._rate()
            self.refresh_board()
            self.write(f"{title} trade size is now {v.size_mult:.2f}x.")
        ttk.Button(b, text="Export this bot...", command=export_one).pack(side="left")
        ttk.Button(b, text="\u270e Edit rules...", command=lambda: self.edit_rules(v)).pack(side="left", padx=(6, 0))
        ttk.Button(b, text="Backtest...", command=lambda: self.open_backtest(v)).pack(side="left", padx=(6, 0))
        ttk.Button(b, text="Add to Hall of Fame", command=to_hof).pack(side="left", padx=6)
        ttk.Button(b, text="Catch!", style="Accent.TButton", command=lambda: self.catch_rows(
            self.board_rows_of([v]), "detail card")).pack(side="left", padx=(0, 6))
        ttk.Button(b, text="Size -", command=lambda: size(-0.25)).pack(side="left")
        ttk.Button(b, text="Size +", command=lambda: size(0.25)).pack(side="left", padx=(4, 0))
        ttk.Button(b, text="Show on main chart", command=lambda: (
            self.board.selection_set(str(id(v))) if self.board.exists(str(id(v))) else None,
            self.chart_bot.set(True), self.show_on_chart(coin.get() or "Account value"))).pack(side="left", padx=6)
        ttk.Button(b, text="Close", command=w.destroy).pack(side="right")

        def tick():
            if w.winfo_exists():
                paint()
                w.after(5000, tick)
        w.after(100, tick)

    # ---------------- family tree ----------------
    def open_family(self, parent=None):
        if parent is None:  # it lives as a tab in the family window
            return self.open_hub('family', 'Family tree')
        if not self.tourn:
            return self._nothing(parent, "Family tree", "Start the bot with Wildcard ticked first.")
        w = parent
        ttk.Label(w, padding=(10, 8, 10, 2), wraplength=860, foreground=MUTED, text=(
            "Every bot's family: starters at the top, their tweaked and bred children underneath. A bred bot has two "
            "parents; it is listed under the first and names the second. Bold = alive, ★ = Hall of Fame. "
            "Removed bots are kept for a while so you can see which lines died out.")).pack(anchor="w")
        top = ttk.Frame(w, padding=(10, 0))
        top.pack(fill="x")
        alive_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Only families with living bots", variable=alive_only, command=lambda: fill()).pack(side="left")
        mid = ttk.Frame(w, padding=10)
        mid.pack(fill="both", expand=True)
        t = ttk.Treeview(mid, columns=("status", "ret", "origin", "gen"), show="tree headings")
        sb = ttk.Scrollbar(mid, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        t.pack(fill="both", expand=True)
        t.heading("#0", text="Bot")
        t.column("#0", width=430)
        for col, txt, wd in (("status", "Status", 200), ("ret", "Return", 80), ("origin", "How it was made", 140), ("gen", "Gen", 40)):
            t.heading(col, text=txt)
            t.column(col, width=wd, anchor="w" if col in ("status", "origin") else "e")
        t.tag_configure("alive", font=("", 9, "bold"))
        t.tag_configure("dead", foreground=MUTED)

        def fill():
            t.delete(*t.get_children())
            recs = {r["uid"]: r for r in self.tourn.family()}
            kids = {}
            for r in recs.values():
                p = r["parents"][0] if r["parents"] and r["parents"][0] in recs else None
                kids.setdefault(p, []).append(r)

            def has_alive(r):
                return r["alive"] or any(has_alive(k) for k in kids.get(r["uid"], []))

            def add(parent_iid, r):
                if alive_only.get() and not has_alive(r):
                    return
                lab = friendly_label(r["name"], {}, r["tf"], self.session_bar).replace(" ()", "")
                extra = f"  (+ #{r['parents'][1]})" if len(r["parents"]) > 1 else ""
                st = STATUS_TEXT.get(r["status"], r["status"]) if r["alive"] else r["status"]
                iid = t.insert(parent_iid, "end", text=f"#{r['uid']}  " + ("★ " if r.get("inducted") else "") + r["label"] + extra,
                               values=(st, pct_txt(r.get("ret")), r["origin"], r["gen"]),
                               tags=("alive" if r["alive"] else "dead",), open=r["alive"] or has_alive(r))
                _ = lab
                for k in sorted(kids.get(r["uid"], []), key=lambda x: x["uid"]):
                    add(iid, k)
            for r in sorted(kids.get(None, []), key=lambda x: x["uid"]):
                add("", r)
        fill()
        b = ttk.Frame(w, padding=10)
        b.pack(fill="x")
        ttk.Button(b, text="Refresh", command=fill).pack(side="left")
        ttk.Button(b, text="Close", command=lambda: w.winfo_toplevel().destroy()).pack(side="right")

    # ---------------- Hall of Fame ----------------
    def open_hof(self):
        if getattr(self, "hof_win", None) is not None and self.hof_win.winfo_exists():
            return self.hof_win.lift()
        w = self.hof_win = tk.Toplevel(self.root)
        w.title("Hall of Fame")
        w.geometry("940x520")
        top = ttk.Frame(w, padding=(10, 10, 10, 4))
        top.pack(fill="x")
        ttk.Label(top, text="Hall of Fame", font=("", 13, "bold")).pack(anchor="w")
        ttk.Label(top, foreground=MUTED, wraplength=900, text=(
            "Only the bots YOU pick are here: right-click any bot on the farm, scoreboard or in the Library and choose "
            "'Add to Hall of Fame' (right-click here to remove). Choose 'Bots trading: Hall of Fame only' to trade "
            "with just these, for example on your real account. Past wins don't guarantee future ones.")).pack(anchor="w", pady=(2, 0))
        mid = ttk.Frame(w, padding=(10, 4))
        mid.pack(fill="both", expand=True)
        t = self.make_tree(mid, ("strategy", "edge", "ret", "win", "hours", "best", "stops", "when"),
                           (250, 85, 80, 70, 70, 110, 120, 120),
                           ("Strategy", "vs the market", "Return", "Win rate", "Hrs winning", "Best market", "Own stops", "Added"))
        t.configure(selectmode="extended")
        state = {"bots": []}

        def right(e):
            iid = t.identify_row(e.y)
            if not iid:
                return
            if iid not in t.selection():
                t.selection_set(iid)
            self.entry_menu(e, [state["bots"][int(i)] for i in t.selection()], "hof")
        for btn in ("<Button-3>", "<Button-2>"):
            t.bind(btn, right)

        def fill():
            t.delete(*t.get_children())
            state["bots"] = E.load_hof()
            for i, b in enumerate(state["bots"]):
                lf = b.get("lifetime") or {}

                def p(k):
                    v = lf.get(k)
                    return "-" if v is None else f"{v:+.1f}%"
                bar = lf.get("bar_minutes")
                t.insert("", "end", iid=str(i), values=(
                    friendly_label(b["name"], b["params"], b.get("tf", 1), bar), p("vs_holding_pct"), p("return_pct"),
                    "-" if lf.get("win_rate_pct") is None else f"{lf['win_rate_pct']:.0f}%",
                    "-" if lf.get("hours_winning") is None else f"{lf['hours_winning']:.1f}",
                    MOOD_TEXT.get(lf.get("best_market"), lf.get("best_market") or "-"),
                    E.risk_text(b.get("risk")) or "-", str(b.get("inducted_at", ""))[:16].replace("T", " ")))
            status.config(text=f"{len(state['bots'])} bots  |  {E.hof_path()}")

        def chosen():
            sel = [state["bots"][int(i)] for i in t.selection()]
            return sel or state["bots"]

        def export():
            bots = chosen()
            if not bots:
                return messagebox.showinfo("Hall of Fame", "The Hall of Fame is empty so far.", parent=w)
            p = filedialog.asksaveasfilename(title="Export Hall of Fame bots", defaultextension=".json", parent=w,
                                             initialfile="hall-of-fame-bots.json", filetypes=[("Bot file", "*.json")])
            if p:
                E.save_bots_file(p, E.export_bots(bots, self.version))
                self.write(f"Exported {len(bots)} Hall of Fame bots to {p}")

        def add_run():
            if not self.tourn or not self.seeded:
                return messagebox.showinfo("Hall of Fame", "Start the bot with Wildcard ticked first.", parent=w)
            closes = {c: self.hist[c] for c in self.session_coins}
            have, n = {v.key() for v in self.tourn.vars}, 0
            for b in [state["bots"][int(i)] for i in t.selection()] or state["bots"][:10]:
                if E.bot_key(b) in have:
                    continue
                v = self.tourn.add_variant(b["name"], b["params"], closes, b.get("size", 1.0), note="Hall of Fame:",
                                           tf=b.get("tf", 1), risk=b.get("risk"), traits=b.get("traits"), vetoes=b.get("vetoes"), layers=b.get("layers"), origin="hall of fame")
                if v:
                    v.inducted, n = True, n + 1
            self.flush_events()
            self.refresh_board()
            self.write(f"Added {n} Hall of Fame bots to this run." if n else "Those bots are already competing.")

        def remove():
            sel = [state["bots"][int(i)] for i in t.selection()]
            if not sel or not messagebox.askyesno("Hall of Fame", f"Remove {len(sel)} bots from the Hall of Fame?", parent=w):
                return
            snap = self.lib_snapshot()
            E.remove_from_hof([E.bot_key(b) for b in sel])
            fill()
            self.offer_undo(f"Removed {len(sel)} bots from the Hall of Fame.", snap)
        b = ttk.Frame(w, padding=10)
        b.pack(fill="x")
        ttk.Button(b, text="Export...", style="Accent.TButton", command=export).pack(side="left")
        ttk.Button(b, text="Add to this run", command=add_run).pack(side="left", padx=6)
        ttk.Button(b, text="Remove", command=remove).pack(side="left")
        ttk.Button(b, text="Catch", command=lambda: self.catch_rows(chosen(), "hall of fame")).pack(side="left", padx=(6, 0))
        ttk.Button(b, text="Refresh", command=fill).pack(side="left", padx=6)
        ttk.Button(b, text="Close", command=w.destroy).pack(side="right")
        status = ttk.Label(b, foreground=MUTED)
        status.pack(side="right", padx=10)
        fill()

    def scout_then_start(self):
        self.write(f"Scouting for {self.settings['auto_coins']} busy tickers that are moving...")
        self.btn.config(state="disabled")

        def work():
            try:
                try:
                    size = float(self.cash_var.get()) * float(self.settings.get("trade_pct", 10)) / 100
                except (TypeError, ValueError):
                    size = None
                why = {}
                picks = E.scout_coins(E.list_products(self.cur_quote()), int(self.settings["auto_coins"]), trade_size=size,
                                      why=why)
                self._scout_why = why
                self.q.put(("scouted", (picks, None)))
            except Exception as e:
                self.q.put(("scouted", ([], str(e))))
        threading.Thread(target=work, daemon=True).start()

    def on_scouted(self, picks, err):
        self.btn.config(state="normal")
        if err or not picks:
            self.write(f"Couldn't scout coins ({err or 'nothing liquid enough'}). Using your ticked coins.", "warn")
        else:
            for c, v in self.coin_vars.items():
                v.set(False)
            self.add_coins(picks)
            why = getattr(self, "_scout_why", {}) or {}
            self.write(f"The bot picked {', '.join(picks)}." + "".join(f" {c}: {why[c]}." for c in picks if c in why), "good")
        self._scouted = True
        self.toggle()

    def show_shortcuts(self, parent=None):
        if parent is None:
            return self.open_hub("help", 'Shortcuts')
        self.info_window("Keyboard shortcuts", (
            "Ctrl+Enter    Start / stop the bot\n"
            "Ctrl+L        Learning (backtests, Training camp, Free roam)\n"
            "Ctrl+H        Hall of Fame\n"
            "Ctrl+K        Browse tickers\n"
            "Ctrl+B        Bot Library\n"
            "Ctrl+E        Design an egg\n"
            "Ctrl+,        Settings\n"
            "F5            Refresh the Trades tab\n\n"
            "Scoreboard: double-click a strategy for its detail card and chart. Select a strategy and pick a coin in "
            "the chart menu to see that bot's entries and exits on the main chart.\n"
            "Chart: hover for exact values, click the range buttons (30 ticks to All), drag the bar under the chart "
            "to make it taller."), parent=parent)

    # ---------------- settings / export / updates ----------------
    EVERYDAY = ("trade_pct", "stop_loss", "take_profit", "trail_stop", "daily_loss", "max_coin_pct", "proven_only",
                "proven_trades", "pdt_guard", "max_bots", "bar_minutes", "ctx_tickers", "animations")
    HIDDEN_SETTINGS = ("maker_fee_pct",)  # nothing in share trading uses these any more

    def settings_groups(self):
        """Everyday settings first; the full groups (minus those) are the advanced tabs."""
        fields = {k: (k, lab, typ) for _, _, fs in SETTING_GROUPS for (k, lab, typ) in fs if typ != "head"}
        out = [("Everyday", "The settings most people change. Tick 'Show advanced settings' below for everything else.",
                [fields[k] for k in self.EVERYDAY if k in fields])]
        for title, blurb, fs in SETTING_GROUPS:
            keep = [f for f in fs if f[0] not in self.EVERYDAY and f[0] not in self.HIDDEN_SETTINGS]
            while keep and keep[-1][2] == "head":
                keep.pop()
            keep = [f for i_, f in enumerate(keep) if not (f[2] == "head" and i_ + 1 < len(keep) and keep[i_ + 1][2] == "head")]
            if any(f[2] != "head" for f in keep):
                out.append((title, blurb, keep))
        return out

    def open_settings(self):
        if getattr(self, "settings_win", None) is not None and self.settings_win.winfo_exists():
            return self.settings_win.lift()
        w = self.settings_win = tk.Toplevel(self.root)
        w.title("Settings")
        w.resizable(False, False)
        outer = ttk.Frame(w, padding=10)
        outer.pack()
        nb = ttk.Notebook(outer)
        nb.pack()
        self.settings_nb = nb
        ents = {}
        adv_tabs = []
        for title, blurb, fields in self.settings_groups():
            f = ttk.Frame(nb, padding=10)
            nb.add(f, text=f"  {title}  ")
            if title != "Everyday":
                adv_tabs.append(f)
            ttk.Label(f, text=blurb, foreground=MUTED, wraplength=620).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
            lpos, lr, lc = {}, 1, 0  # Learning tab: switches two per row
            for r, (key, label, typ) in enumerate(fields, start=1):
                if typ is bool:
                    lpos[r] = (lr, lc)
                    lr, lc = (lr + 1, 0) if lc else (lr, 1)
                else:
                    if lc:
                        lr, lc = lr + 1, 0
                    lpos[r] = (lr, 0)
                    lr += 1
            if title != "Learning":
                lpos = {r: (r, 0) for r in range(1, len(fields) + 1)}
            for r, (key, label, typ) in enumerate(fields, start=1):
                r_ = r
                r = lpos[r_][0]
                if typ == "head":
                    hf = tk.Frame(f, bg=BG)
                    hf.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(10 if r > 1 else 0, 2))
                    tk.Label(hf, text=label.upper(), bg=BG, fg=ACCENT, font=("", 8, "bold")).pack(side="left")
                    tk.Frame(hf, bg=LINE, height=1).pack(side="left", fill="x", expand=True, padx=(8, 0), pady=(2, 0))
                    continue
                cur = self.settings.get(key)
                if typ is bool:
                    var = tk.BooleanVar(value=bool(cur))
                    if title == "Learning":
                        ttk.Checkbutton(f, text=label, variable=var).grid(row=r, column=lpos[r_][1], sticky="w", pady=2,
                                                                          padx=(0, 16))
                    else:  # long switch labels wrap instead of stretching the window
                        cf = ttk.Frame(f)
                        cf.grid(row=r, column=0, columnspan=2, sticky="w", pady=3)
                        ttk.Checkbutton(cf, variable=var).pack(side="left", anchor="n")
                        lb = ttk.Label(cf, text=label, wraplength=560, justify="left", cursor="hand2")
                        lb.pack(side="left", anchor="w")
                        lb.bind("<Button-1>", lambda e, v_=var: v_.set(not v_.get()))
                    ents[key] = (var, bool)
                    continue
                ttk.Label(f, text=label, wraplength=520 if title == "Everyday" else 360, justify="left").grid(
                    row=r, column=0, sticky="w", pady=2 if title == "Everyday" else 3)
                if typ == "bar":
                    var = tk.StringVar(value=BAR_LABELS.get(float(cur), "1 min"))
                    ttk.Combobox(f, textvariable=var, values=list(BAR_LABELS.values()), state="readonly", width=12).grid(
                        row=r, column=1, padx=(10, 0), sticky="w")
                elif typ == "eggs":
                    var = tk.StringVar(value=cur if cur in ("By dragon type", "Purple") else "By dragon type")
                    ttk.Combobox(f, textvariable=var, values=["By dragon type", "Purple"], state="readonly", width=15).grid(
                        row=r, column=1, padx=(10, 0), sticky="w")
                elif typ == "chars":
                    var = tk.StringVar(value=cur if cur in ("Themed dragons", "Classic dragons") else "Themed dragons")
                    ttk.Combobox(f, textvariable=var, values=["Themed dragons", "Classic dragons"], state="readonly", width=15).grid(
                        row=r, column=1, padx=(10, 0), sticky="w")
                elif typ == "anim":
                    var = tk.StringVar(value=cur if cur in ("Full", "Light", "Off") else "Full")
                    ttk.Combobox(f, textvariable=var, values=["Full", "Light", "Off"], state="readonly", width=8).grid(
                        row=r, column=1, padx=(10, 0), sticky="w")
                elif typ == "order":
                    var = tk.StringVar(value=ORDER_LABELS.get(cur, ORDER_LABELS["market"]))
                    ttk.Combobox(f, textvariable=var, values=list(ORDER_LABELS.values()), state="readonly", width=34).grid(
                        row=r, column=1, padx=(10, 0), sticky="w")
                elif typ == "tickers":
                    var = tk.StringVar(value=", ".join(cur or []))
                    ttk.Entry(f, textvariable=var, width=30).grid(row=r, column=1, padx=(10, 0), sticky="w")
                else:
                    var = tk.StringVar(value="" if cur is None else str(cur))
                    ttk.Entry(f, textvariable=var, width=44 if typ is str else 8,
                              show="*" if key == "alert_tg_token" else "").grid(row=r, column=1, padx=(10, 0), sticky="w")
                ents[key] = (var, typ)
            if title == "Learning":
                learn = [k for k, _, t_ in fields if t_ is bool]

                def set_all(on, ents=ents, learn=learn):
                    for k in learn:
                        ents[k][0].set(on if on is not None else bool(E.DEFAULT_SETTINGS.get(k, True)))
                    if on is None:
                        for k in ("islands", "classic_rate"):
                            ents[k][0].set(str(E.DEFAULT_SETTINGS[k]))
                qb = ttk.Frame(f)
                qb.grid(row=lr + 1, column=0, columnspan=2, sticky="w", pady=(12, 0))
                ttk.Button(qb, text="All on", command=lambda: set_all(True)).pack(side="left")
                ttk.Button(qb, text="All off", command=lambda: set_all(False)).pack(side="left", padx=6)
                ttk.Button(qb, text="Recommended", style="Accent.TButton", command=lambda: set_all(None)).pack(side="left")
                ttk.Label(qb, text="  All off = the bots only tweak their numbers and pace.", foreground=MUTED).pack(side="left")
            if title == "Alerts":
                def test(ents=ents):
                    trial = dict(self.settings)
                    for k in ("alert_discord", "alert_tg_token", "alert_tg_chat"):
                        trial[k] = ents[k][0].get().strip()
                    if not E.alerts_configured(trial):
                        return messagebox.showinfo("Alerts", "Fill in a Discord webhook, or a Telegram token and chat ID, first.", parent=w)

                    def work():
                        probs = E.send_alert(trial, "SBF test alert: alerts are working.")
                        self.q.put(("call", lambda: messagebox.showinfo(
                            "Alerts", "\n".join(probs) if probs else "Sent. Check your phone.", parent=w)))
                    threading.Thread(target=work, daemon=True).start()
                ttk.Button(f, text="Send a test alert", command=test).grid(row=len(fields) + 1, column=0, sticky="w", pady=(10, 0))
            if title == "Orders":
                ttk.Label(f, foreground=MUTED, wraplength=520, justify="left", text=(
                    "LIVE buys whole shares with a limit order at the ask and sells with a limit at the bid; if nothing "
                    "fills in time it goes to market (Limit fallback). Part shares always trade at market. Safety stops, "
                    "the kill switch and Sell everything sell at market right away.")
                          ).grid(row=len(fields) + 1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        adv = tk.BooleanVar(value=bool(self.cfg.get("settings_advanced", False)))

        def show_adv():
            self.cfg["settings_advanced"] = bool(adv.get())
            for f_ in adv_tabs:
                nb.tab(f_, state="normal" if adv.get() else "hidden")
        ttk.Checkbutton(outer, text="Show advanced settings (how the farm breeds and learns, orders, timing, alerts, "
                                    "Library and Hall of Fame)", variable=adv, command=show_adv).pack(anchor="w", pady=(6, 0))
        show_adv()
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="  Strategy details  ")
        ttk.Label(f, text="Starting numbers for each strategy. Wildcard tweaks these on its own.",
                  foreground=MUTED).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        pents, items = {}, [(name, k, dv) for name, spec in E.STRATS.items() for k, dv in spec["params"].items()]
        half = (len(items) + 1) // 2
        for i, (name, k, dv) in enumerate(items):
            r, col = 1 + i % half, 0 if i < half else 2
            ttk.Label(f, text=f"{STRAT_LABELS[name]}: {PARAM_NAMES.get(k, k)}").grid(row=r, column=col, sticky="w", pady=1,
                                                                                    padx=(0 if col == 0 else 18, 0))
            e = ttk.Entry(f, width=7)
            e.insert(0, str(self.settings["params"][name][k]))
            e.grid(row=r, column=col + 1, padx=(8, 0))
            pents[(name, k)] = (e, type(dv))
        misc = ttk.Frame(outer)
        misc.pack(fill="x", pady=(8, 0))
        notify = tk.BooleanVar(value=bool(self.settings["notify"]))
        ttk.Checkbutton(misc, text="Notifications (important events)", variable=notify).pack(side="left")
        ntr = tk.BooleanVar(value=bool(self.settings.get("notify_trades", False)))
        ttk.Checkbutton(misc, text="...also every trade", variable=ntr).pack(side="left", padx=(8, 0))
        snd = tk.BooleanVar(value=bool(self.settings.get("chime", True)))
        ttk.Checkbutton(misc, text="SBF sounds", variable=snd).pack(side="left", padx=(8, 0))
        vol = tk.IntVar(value=int(self.settings.get("chime_volume", 60)))
        ttk.Scale(misc, from_=5, to=100, variable=vol, length=80).pack(side="left", padx=(4, 0))
        sb = ttk.Button(misc, text="\u25b6", width=3, command=lambda: E.play_sound(
            ("trade", "notify", "hof", "problem")[getattr(self, "_demo", 0) % 4], vol.get()) or
            setattr(self, "_demo", getattr(self, "_demo", 0) + 1))
        sb.pack(side="left", padx=(4, 0))
        Tip(sb, "Hear the sounds: each click plays the next one (trade, notice, Hall of Fame, problem).")
        srow = ttk.Frame(outer)
        srow.pack(fill="x", pady=(4, 0))
        ttk.Label(srow, text="Play a sound for:", foreground=MUTED).pack(side="left")
        off = set(self.settings.get("sounds_off") or [])
        snd_vars = {}
        for kind, txt in (("trade", "trades (blip)"), ("notify", "notices (chime)"), ("hof", "Hall of Fame (fanfare)"),
                          ("problem", "problems (low tone)")):
            snd_vars[kind] = tk.BooleanVar(value=kind not in off)
            ttk.Checkbutton(srow, text=txt, variable=snd_vars[kind]).pack(side="left", padx=(8, 0))
            ttk.Button(srow, text="\u25b6", width=2, command=lambda k=kind: E.play_sound(k, vol.get())).pack(side="left")
        keep = tk.BooleanVar(value=bool(self.settings.get("keep_awake", True)))
        ttk.Checkbutton(misc, text="Keep the computer awake while the bot runs", variable=keep).pack(side="left", padx=12)

        def save():
            try:
                new = {}
                for k, (var, typ) in ents.items():
                    v = var.get()
                    if typ is bool:
                        new[k] = bool(v)
                    elif typ == "bar":
                        new[k] = next(b for b, l in BAR_LABELS.items() if l == v)
                    elif typ == "order":
                        new[k] = next(o for o, l in ORDER_LABELS.items() if l == v)
                    elif typ == "tickers":
                        new[k] = [x.strip().upper() for x in v.replace(";", ",").split(",") if E.valid_coin(x.strip().upper())][:8]
                    elif typ is str or typ in ("anim", "chars", "eggs"):
                        new[k] = v.strip()
                    else:
                        new[k] = typ(float(v)) if typ is int else float(v)
                params = {n: dict(p) for n, p in self.settings["params"].items()}
                for (n, k), (e, typ) in pents.items():
                    params[n][k] = typ(float(e.get())) if typ is int else float(e.get())
                    lo, hi = E.STRATS[n]["bounds"][k]
                    assert lo <= params[n][k] <= hi
                assert 0 < new["trade_pct"] <= 100 and new["refresh"] >= 1 and new["evolve_every"] >= 1 and 0 < new["max_coin_pct"] <= 100
                assert new["bar_minutes"] in BAR_CHOICES and min(new["stop_loss"], new["take_profit"], new["daily_loss"]) >= 0
                assert 1.0 <= new["winner_boost"] <= 3.0 and new["add_every"] >= 1 and new["slow_every"] >= 1 and new["fail_drop"] >= 0
                assert 14 <= new["max_bots"] <= 200 and new["retire_hours"] >= 0 and 0 <= new["auto_coins"] <= 15
                assert 1 <= new["islands"] <= 6 and new["grad_hours"] >= 0 and 0 <= new["classic_rate"] <= 100
                assert new["trade_hours"] == "" or parse_hours(new["trade_hours"]) is not None
                assert new["hof_hours"] > 0 and new["hof_min_trades"] >= 0 and new["max_trades_hour"] >= 0
                assert 0 <= new["trail_stop"] < 100 and 0 <= new["slippage"] < 5 and 0 <= new["maker_fee_pct"] < 5
                assert 3 <= new["limit_wait"] <= 600 and 0 <= new["fee_pct"] < 5
                for n, p in params.items():
                    assert E.fix_params(n, dict(p)) == p
            except Exception:
                return messagebox.showerror("Settings", "One of the numbers isn't valid. Reminders: islands 1 to 6; trading "
                                                        "hours like 9-17 or 22-6 (or empty); winner trade size must be "
                                                        "between 1 and 3; farm size must be 14 to 200; the bot can pick 0 to 15 "
                                                        "coins; limit wait is 3 to 600 seconds; percentages can't be negative; "
                                                        "strategy numbers must stay in their normal range, short averages below "
                                                        "long ones and buy levels well below sell levels.", parent=w)
            if self.session and new["bar_minutes"] != self.session_bar:
                messagebox.showinfo("Settings", "The new decision speed starts after you reset the session (File > Reset session).", parent=w)
            if new["bar_minutes"] < 1 and float(self.settings["bar_minutes"]) >= 1:
                messagebox.showinfo("Fast bars", "Short decision speeds mean many more trades. Each round trip costs about "
                                                 f"{2 * E.cost_model(dict(self.settings, **new))[0] * 100:.1f}% in fees, so "
                                                 "a move has to beat that to make money. Consider Limit orders and the "
                                                 "trades-per-hour brake (Settings > Orders).", parent=w)
            self.settings.update(new)
            self.settings["params"], self.settings["notify"] = params, notify.get()
            self.settings["keep_awake"] = keep.get()
            self.settings["notify_trades"], self.settings["chime"] = ntr.get(), snd.get()
            self.settings["chime_volume"] = int(vol.get())
            self.settings["sounds_off"] = [k for k, v in snd_vars.items() if not v.get()]
            CHARACTER_MODE["mode"] = self.settings.get("characters", "Themed dragons")
            self.grid_refresh()
            self.apply_awake()
            self.auto_coin_var.set(int(self.settings.get("auto_coins", 0)) > 0)
            match = [n for n, p in E.STYLES.items() if all(abs(self.settings[k] - v) < 1e-9 for k, v in p.items())]
            self.settings["style"] = match[0] if match else "Custom"
            self.style_var.set(self.settings["style"])
            if self.tourn:
                self.tourn.o.update(E.tournament_opts(self.settings))
                self.tourn.cap = int(self.settings["max_bots"])
                self.tourn.family_cap = max(5, -(-self.tourn.cap // 4))
                self.tourn.retire_steps = self.retire_steps(self.session_bar)
            self.cfg["settings"] = self.settings
            E.save_json(CONFIG_PATH, self.cfg)
            self.write("Settings saved.")
            w.destroy()
        row = ttk.Frame(outer)
        row.pack(anchor="e", pady=(10, 0))
        ttk.Button(row, text="Cancel", command=w.destroy).pack(side="left", padx=6)
        ttk.Button(row, text="Save", style="Accent.TButton", command=save).pack(side="left")

    def open_data_folder(self):
        os.makedirs(E.HOME_DIR, exist_ok=True)
        try:
            if sys.platform.startswith("win"):
                os.startfile(E.HOME_DIR)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", E.HOME_DIR])
        except Exception:
            messagebox.showinfo("Data folder", E.HOME_DIR)

    def export_csv(self):
        if not self.trades and not self.shadow_trades:
            return messagebox.showinfo("Export", "No trades yet.")
        rows = list(self.trades)
        if self.shadow_trades and messagebox.askyesno(
                "Export", f"Also include the bots' {len(self.shadow_trades)} test trades?\n\n"
                          "They are marked BOT in the mode column and never touched your account."):
            rows = sorted(rows + self.shadow_trades, key=lambda r: str(r.get("time", "")))
        p = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="trades.csv")
        if p:
            E.export_trades_csv(p, rows)
            self.write(f"Exported {len(rows)} trades to {p}")

    def check_update(self, silent=False):
        if getattr(self, "_upd_busy", False):
            return
        self._upd_busy = True
        if not silent:
            self.write("Checking GitHub for a newer version...")

        def work():
            try:
                rel = E.latest_release(UPDATE_REPO)
                remote = rel["version"]
                if not remote:
                    raise RuntimeError("no release found on GitHub")
                notes = ""
                try:  # the changelog at that release's tag: every version since yours, not just the newest
                    raw = f"https://raw.githubusercontent.com/{UPDATE_REPO}/{rel['tag']}/CHANGELOG.md"
                    notes = E.changelog_since(E.http_get(raw, 10).decode("utf-8", "replace"), self.version)
                except Exception:
                    E.note_error("work")
                self.update_rel = rel
                self.q.put(("update", ("found" if E.version_tuple(remote) > E.version_tuple(self.version) else "none",
                                       (remote, notes or rel["notes"], silent))))
            except Exception as e:
                self.q.put(("update", ("error", (str(e), silent))))
        threading.Thread(target=work, daemon=True).start()

    def auto_check_update(self):
        if time.time() - float(self.cfg.get("last_update_check", 0)) > 12 * 3600:
            self.cfg["last_update_check"] = time.time()
            try:
                E.save_json(CONFIG_PATH, self.cfg)
            except OSError:
                pass
            self.check_update(silent=True)

    def on_update_msg(self, kind, data):
        self._upd_busy = False
        if kind in ("error", "none") and not getattr(self, "update_info", None):
            self.upd_btn.config(text="\u27f3 Check for updates")
        if kind == "error":
            msg, silent = data
            if not silent:
                messagebox.showerror("Update", f"Couldn't check for updates: {msg}")
            return
        if kind == "none":
            if not data[2]:
                messagebox.showinfo("Update", f"You have the latest version ({self.version}).")
            return
        if kind == "found":
            remote, notes, silent = data
            self.update_info = (remote, notes)
            self.upd_btn.config(text=f"\u2b06 Update to {remote}", bg=GREEN, fg="white", activebackground=GREEN,
                                activeforeground="white")
            self.write(f"Version {remote} is available. Click 'Update' at the top to see what's new.", "good")
            if self.tray_hidden() and getattr(self, "_tray_upd_told", None) != remote:
                self._tray_upd_told = remote
                self.tray.notify(f"Version {remote} is available. Open {APP_SHORT} and click Update to see what's new "
                                 "(nothing installs until you say yes).")
            if not silent:
                self.show_update()
            return
        if kind == "installed":
            written, backup, added, problem = data
            self.upd_btn.config(text="\u27f3 Restart to finish updating", bg=ACCENT, fg="white")
            self.write(f"Updated {len(written)} files. The old version is kept in {backup}.", "good")
            if added and not problem:
                self.write("Installed what the new version needs: " + ", ".join(added), "good")
            if problem:
                self.write(f"The new version needs {', '.join(added)}, which didn't install: {problem}", "warn")
                messagebox.showwarning("Update", "The update is installed, but an add-on it needs didn't install:\n\n"
                                                 f"{', '.join(added)}\n\n{problem}\n\nIf the app has trouble after "
                                                 "restarting, Tools > Undo last update goes back.")
            if messagebox.askyesno("Update installed", "The update is installed. Restart SBF now to use it?\n\n"
                                                       "(Your session is saved first. Tools > Undo last update goes back.)"):
                self.restart_app()
            return
        if kind == "install_fail":
            messagebox.showerror("Update", f"The update didn't install, nothing was changed: {data}")

    def update_clicked(self):
        if getattr(self, "update_info", None):
            return self.show_update()
        self.upd_btn.config(text="\u27f3 Checking...")
        self.check_update()

    def show_update(self):
        if not getattr(self, "update_info", None):
            return self.check_update()
        remote, notes = self.update_info
        w = tk.Toplevel(self.root)
        w.title("Update available")
        f = ttk.Frame(w, padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=f"SBF {remote} is available", font=("", 13, "bold")).pack(anchor="w")
        ttk.Label(f, text=f"You have {self.version}. Here is what's new:", foreground=MUTED).pack(anchor="w", pady=(2, 6))
        t = tk.Text(f, width=86, height=18, wrap="word", relief="flat", padx=8, pady=6)
        t.insert("1.0", notes or "(No release notes found.)")
        t.config(state="disabled")
        t.pack(fill="both", expand=True)
        ttk.Label(f, foreground=MUTED, wraplength=640, text=(
            f"Downloads from github.com/{UPDATE_REPO}. Your settings, sessions, trades, saved bots and login are not "
            "touched. The current version is backed up first, and Tools > Undo last update puts it back.")).pack(anchor="w", pady=(8, 0))
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(10, 0))

        def accept():
            if self.running and not messagebox.askyesno("Update", "The bot is running. It keeps running while the update "
                                                                  "downloads, and stops when the app restarts. Continue?", parent=w):
                return
            w.destroy()
            self.write("Downloading the update...")

            def work():
                try:
                    rel = getattr(self, "update_rel", None) or E.latest_release(UPDATE_REPO)
                    data = E.download_release(rel)  # the release zip, checked against its published checksum
                    written, backup = E.install_update(data, HERE, self.version, expect_version=rel["version"])
                except Exception as e:
                    return self.q.put(("update", ("install_fail", str(e))))
                added, problem = E.new_requirements(backup, HERE), ""
                if added:  # the new version needs add-ons this computer may not have yet
                    try:
                        ok, msg = E.install_packages(added, lambda line: None)
                        problem = "" if ok else msg
                    except Exception as e:
                        problem = str(e)
                self.q.put(("update", ("installed", (written, backup, added, problem))))
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(b, text="Accept and install", style="Accent.TButton", command=accept).pack(side="right")
        ttk.Button(b, text="Later", command=w.destroy).pack(side="right", padx=6)

    def undo_update(self):
        bk = E.latest_backup(HERE)
        if not bk:
            return messagebox.showinfo("Undo update", "There is no earlier version saved.")
        if not messagebox.askyesno("Undo update", f"Put back the version saved in\n{bk}?\n\nThe app restarts afterwards."):
            return
        try:
            E.restore_backup(HERE, bk)
            shutil.move(bk, bk + "-restored")
        except Exception as e:
            return messagebox.showerror("Undo update", f"Couldn't restore: {e}")
        self.restart_app()

    def restart_app(self, save=True):
        """save=False after a restore: saving now would write the old in-memory state over the files just restored."""
        self.running = False
        self.awake.off()
        if save:
            try:
                self.save_state()
                E.save_json(CONFIG_PATH, self.cfg)
            except Exception:
                pass
        try:
            self._gone = True
            self.root.destroy()
        except Exception:
            pass
        self.release_lock()
        # a fresh process (not os.execv: on Windows that doesn't quote paths, so a space in a folder name broke restarts)
        args = [sys.executable, os.path.join(HERE, "main.py")] + [a for a in sys.argv[1:] if a == "--tray"]
        try:
            subprocess.Popen(args, cwd=HERE, close_fds=True)
        except Exception:
            os.execv(sys.executable, [f'"{a}"' if " " in a and sys.platform.startswith("win") else a for a in args])
        os._exit(0)


def _report_crash():
    import traceback
    text = traceback.format_exc()
    path = os.path.join(E.HOME_DIR, "crash.log")
    try:
        os.makedirs(E.HOME_DIR, exist_ok=True)
        with open(path, "a") as f:
            f.write(f"\n--- {datetime.now():%Y-%m-%d %H:%M:%S} ---\n{text}")
    except OSError:
        path = "(could not save a log file)"
    try:
        messagebox.showerror("SBF hit an error", f"Something went wrong:\n\n{text[-900:]}\n\nSaved to: {path}")
    except Exception:
        pass
    _stay_open(f"{text}\nThis was also saved to: {path}")


if __name__ == "__main__":
    try:
        tray_start = "--tray" in sys.argv
        try:
            import tray as _T
        except Exception:
            _T = None
        if _T and not _T.acquire_single(E.HOME_DIR):  # SBF is already open (maybe as the icon by the clock)
            if not tray_start:
                _T.ask_running_copy_to_show(E.HOME_DIR)
            sys.exit(0)
        root = tk.Tk()
        root.report_callback_exception = lambda *a: __import__("traceback").print_exception(*a)
        app = App(root, tray=tray_start)
        if _T:
            root.after(2000, app.poll_show)
        root.mainloop()
    except SystemExit:
        raise
    except BaseException:
        _report_crash()
