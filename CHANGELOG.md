# Changelog







## 1.9.0
- **Stand-ins while no bot is proven.** Until a proven bot is winning, the account no longer just waits: the 3 best
  winners trade it at half the normal size. Each must be winning right now, have 3+ finished trades, be ahead over
  its whole life and not have failed its exams; kamikazes never stand in. As soon as a proven bot is winning, it
  takes over and the stand-ins step back. The bar under the top line, the farm's status line and Home name the
  stand-ins. Settings (under "Only proven bots trade the account"): how many may stand in (0 = off, the old
  behaviour) and their size (0.5 = half).

## 1.8.0
- **A new layout: Home, plus four sections on a rail at the left.**
  - **Home** answers three questions at a glance: *Is it running?* (with Start / Stop), *Is anyone proven?* (the
    leading bot, and what the closest one still needs) and *What needs you?* (the farm doctor's findings, with their
    fix buttons). Below: your latest trades and what's learning right now.
  - **Trading**: Portfolio, Options, Trades, Alpaca account. **Bots**: Bot Farm, Slow farm, Scalpers, Success, Bot
    positions, and the Library & Hall of Fame. **Learning**: the Overview of every feature, plus Free roam, the
    Training camp and Farm knowledge. **Markets**.
  - Each section shows its screens as buttons along the top. The account panel and the charts stay in Home, Trading
    and Markets; Bots and Learning get the whole window.
  - Nothing was removed: every tab, button and window is where its section says. Links that jump to a tab (like
    'See the bots') move the rail along. Settings > New layout (off) brings the old row of tabs back after a restart.

## 1.7.1
- **Fix: Free roam's self-tests failed** ("backtest_bot() got an unexpected keyword argument 'stop'"). A study that
  stays in the app (because it reports each trade to the Free roam screen) was handed a stop option backtests don't
  take. It now gets only the options it was given.

## 1.7.0
- **Studies on their own processor cores.** Training camps, Auto camp, Free roam's studies, hindsight and backtests now
  run in separate worker processes, so they use other cores instead of sharing one with the window. Stopping them
  and their progress bars work as before, and if a worker can't start, the study simply runs inside SBF as it used
  to. Settings > Studies on their own cores (on). (The farm's own decisions were measured too: about 6 ms per
  decision even with 175 bots, so they stay where they are.)
- **Fix: double-clicking SBF.pyw could open a second copy of the app** in the background whenever a study ran on
  another core (the scalp scan did this already); the launcher now only starts the app itself.
- **One shared farm engine.** 39 farm methods that were identical in CBBF, OBF and SBF (ranking, farm size, families,
  replays, seasons, health...) now live once in farmcore.py, about 360 lines per app, so a fix there reaches all
  three apps.

## 1.6.0
- **Practice learns what real fills cost.** With 'Both' on, every practice trade is also placed on Alpaca paper. SBF now
  compares the two prices and, after 10 priced copies, practice charges the difference on buys and sells, so practice
  results stop being rosier (or harsher) than real ones. The Alpaca account tab shows the comparison, and the farm
  doctor warns when many paper copies don't fill at all. Settings > Learn from paper fills (on).
- **Earnings awareness.** SBF keeps the US earnings calendar for the next six weeks (from Nasdaq, refreshed every 6
  hours; off with Essentials only):
  - **Earnings guard** (Settings, default 1): the account opens nothing in a stock the day before or the day it
    reports.
  - A new never-rule bots can learn, **Stays away from earnings**, graded like the others.
  - The farm doctor warns when you're holding something into an earnings report.

## 1.5.0
- **Farm doctor** (top bar, next to Essentials only): checks the farm and the app every minute and offers a fix
  where there is one: a farm far past its size (one click trims it), most bots unjudged, no bot proven for hours,
  few exams, prices that stopped coming, Free roam stuck or idle, errors piling up, one strategy taking over, and
  today's loss limit or a losing-streak pause. The button turns amber or red when something needs you, and says
  so once in the log.
- **Safety limits for the account**, enforced whatever the bots decide (Settings, after the daily loss limit):
  **Most open positions** (default 10), and a **Losing-streak pause** (after 5 losing trades in a row, nothing new
  opens for 30 minutes). Sells and safety exits are never blocked. Together with the daily loss limit and the limit
  per ticker you already had.
- **Silent bots get loosened instead of just dropped.** Your reports showed farms where most bots never traded
  (100 of 120 on one): they carried so many conditions before buying (skip quiet markets, only some kinds of market,
  several signals in a row, the bigger trend agreeing...) that the moment never came, so they could never be judged
  or learn. A bot that goes a whole scoring window without a trade now loses its most restrictive condition and gets
  a fresh window, up to 4 times. In a test farm built like yours, bots stuck without trades went from 35 to 0 and
  more bots could be judged (29 instead of 23).
- **Fix: "not logged in" no longer fills the error log.** The price watcher logged an error every few seconds while
  you weren't logged in (26 of the last 27 errors in one report); it now just waits.
- **Fix: an overfull saved farm now really shrinks back to its size when it loads.** Loading stretched the size to
  fit every bot, so the trim did nothing and a 60-bot farm could stay at 125.

## 1.4.1
- Training camp Auto: **Stop now works while it waits** between camps (it cancels the next one), the wait shows a
  countdown, and a **wait** choice sets the pause between camps (0, 5, 10, 30 or 60 seconds; default 10). Unticking
  Auto during the wait stops it too.

## 1.4.0
- **Training camp: Randomize, Auto and Max decisions.**
  - **Max decisions** (default 5,000): the most farm decisions one run may take (candles x passes). Longer periods are
    cut to their latest candles, so a run can't grow huge.
  - **Randomize** picks a period, candle size, passes, farm size and starting bots for you, always under Max decisions,
    and mostly on your farm's own candle size so its graduates fit your farm.
  - **Auto** runs one camp after another with random settings. After each one your farm learns from it and up to 3
    bots that passed the exam join your farm. 30 seconds between runs; it runs while the Training camp window is open,
    stops with Stop (or untick Auto), gives up after 5 runs in a row that can't finish, and Essentials only keeps it off.

## 1.3.1
- Fixed: Auto camp's (or the exam pass's) first stretch after the app started could fail with "no attribute roam_log"
  when Free roam hadn't loaded its study log yet; it then waited 5 minutes before trying again. The log now loads
  whenever any of them needs it.
- Auto camp says when a stretch is a repeat visit. Each stretch is studied 3 times (Settings > Learning > Free roam
  reuse); repeat visits teach your farm, but only a first visit can bring an exam-passing bot over.

## 1.3.0
- **Auto camp** (the farm toolbar, next to Free roam): while your farm runs, it keeps picking stretches of past prices
  by itself and runs a practice farm through each one, at most 1,500 farm decisions per stretch (longer stretches are cut
  to their latest 1,500 candles). After each stretch your farm learns its lessons and the best bot that passed the
  exam joins your farm. It pauses a minute between stretches, starts OFF each time the app opens, waits while Free
  roam is on (Free roam runs its own camps), and Essentials only keeps it off.

## 1.2.1
- **Essentials only button** (top bar, next to Check for updates): one click switches off everything that downloads
  extra history or works the disk in the background: Free roam, the Slow farm, the Scalp lab, background exams, the automatic
  economic calendar refresh and weekly backups. Normal trading and its prices keep running. While it's on, those modes
  can't start by themselves; starting one by hand asks whether to switch Essentials only off. Click again to allow them.

## 1.2.0
- **Exams with Free roam off**: most bots were never examined, because exams only happened inside Free roam. Now,
  every 20 minutes while the farm runs, up to 3 bots that still need exams are tested on a stretch of history none of
  them trained on, at their own candle size. It's only an exam: the bots learn nothing from it. Each stretch downloads
  once and is kept; with no connection it waits for the next round. Settings > "Exams with Free roam off".
- **Backup strategies stay alive**: when one strategy fills over half the farm, half the new eggs try other
  strategies, and the last two bots of any strategy are dropped last. If the market stops suiting the favourite, there
  are alternatives ready. Settings > "Keep backup strategies".
- **Thin-market warning**: once a day, a ticker that went without trades in a fifth or more of its recent bars gets a
  warning in the log (and on the icon), with well-traded tickers to consider instead.
- **Shared core**: about 800 lines that were identical in CBBF, OBF and SBF now live in one shared file, farmcore.py,
  so fixes to that code reach all three apps at once. tools/sync_core.py copies it between the apps, and a test fails
  if one app's copy drifts.

## 1.1.1
- **Successful bots stay.** A bot with 8+ finished trades that made money over its whole life and beat just holding
  is now protected from retirement and from being dropped to make room. A bad patch benches it (no say in trades)
  but no longer kills it; it only goes once its lifetime record turns. Weaker bots still die out as before.
  Settings > "Keep proven veterans".
- **No more exact copies.** Copies of the same bot on the same island (from migration, training camps or starter
  sets) wasted farm places and voted twice for the same idea. The best-scoring copy stays, the rest are removed.
- Fixed: a bot that learned a rule from its own losses kept its old identity, so the copy check and the Hall of Fame
  could mix it up with its parent.
- **Daily recap fixed**: it no longer reports gains while prices are stale or across a reset (it once showed
  +83% for a day with no trades). It waits for fresh prices and starts over when the session changes.
- **Background start logs in reliably.** When the computer had just started, the saved Alpaca login could time out
  and was never tried again, so the bots trained all day without prices. It now retries after 30 seconds, then 1, 2,
  4 and 8 minutes, then every 10 minutes, and loads the missed history once it connects.
- The scalp lab no longer fills the error log every minute while logged out; it waits quietly for a login.

## 1.1.0
- **Proven bots are reachable now.** Almost no bot ever earned a vote, so the account never traded. Two reasons:
  bots were replaced faster than they could finish a record (about half the farm in two hours), and trades from Free
  roam's exams didn't count. Now:
  - Trades a bot makes in its exams (days it never trained on) count toward the finished trades it needs.
  - Bots on course to earn a vote (passed exams, or exams so far positive and not behind holding) aren't retired or
    dropped to make room, up to a third of the farm, so they live long enough to finish their record.
  - The rules themselves are unchanged: exams, lifetime profit and beating just holding are still required.
- **Leveraged and inverse ETFs**: SPYU (4x S&P 500), UPRO, SPXL, SSO, TQQQ, QLD, SOXL, TNA, and the inverse SPXU, SPXS,
  SDS, SH, SQQQ, PSQ, SOXS and TZA. SPYU, TQQQ and SQQQ are in the ticker list; the rest are in More tickers, which now
  says what each one is. Inverse ETFs rise when their index falls, so the buy-only bots can profit on down days too.
  Bot picks tries them but keeps them to half its picks at most. They reset daily: held for days they drift from
  their multiple.
- **A rejected saved key no longer costs you the session.** When Alpaca rejects the key saved on this computer, OBF
  forgets it and opens the login (tick Remember with your current key). Loading history now waits for a login that's
  still connecting, and logging in after a start without history reloads it for the same farm.

## 1.0.1
- **Donate button**: a pink **Donate** button in the top right (and Tools > Donate) opens SBF's GitHub Sponsors page.
  SBF stays free; donations pay for new breeds, new tricks and more time to build them.
- **Fix: Free roam no longer logs errors when you aren't logged in.** It needs Alpaca's history, so it now waits
  quietly, says once in the log that it's waiting for a login, and starts studying as soon as you log in.
- **Start asks you to log in when you haven't**: Practice uses pretend money but real prices, and those come from
  Alpaca, so without a login the bots just waited. Start now offers to open the login (or start anyway).
- **Fix: "no history" isn't a crash any more.** When Free roam finds nothing to study (a holiday, a gap in the
  data) it notes it and moves on, instead of adding an entry to crash.log and the error counter.
- **New README and screenshots**: the page now explains how the farm trains, breeds and tests its dragons, with
  fresh dark-mode screenshots of the farm, the BEASTS den, Overview, a dragon's card, Free roam, Farm knowledge, the Library,
  Portfolio, Bot positions and Success.

## 1.0.0
- **SBF, the Stock Bot Farm: the farm from OBF and CBBF, trading shares.** Built from OBF 1.24.0 (the options farm),
  with its learning, breeding, Free roam, exams, Library and Hall of Fame. The bots and your account buy and sell
  **shares** of stocks and ETFs instead of option contracts.
- **Only proven bots trade your account** (from OBF 1.24): at least 8 finished trades, a full scoring window, money made
  over their lifetime and more than just holding, and passed exams on days they never trained on. The best lifetime
  records get the biggest say (up to 3x); kamikazes never vote. The Overview tab now shows how many bots have a vote
  and who's leading.
- **Shares, whole or in part.** Practice buys exactly the trade size, part shares included. LIVE and Alpaca paper buy
  whole shares with a limit at the ask (then market if it doesn't fill) and smaller trades as a fractional market order.
  Sells use a limit at the bid; stops, the kill switch and part shares sell at market.
- **Costs for stocks**: Alpaca charges no commission on stocks, so Practice counts half the bid/ask gap on each fill
  (0.03%) and the small regulatory fees on sells (0.005%). LIVE skips a buy when a stock's bid/ask gap is wider than
  0.5% (Settings > Basics), scalps when it's wider than 0.15%.
- **Genes sized for stocks**: bots evolve stops from 0.15% to 25% and targets from 0.2% to 60% (options needed 2% to
  500%). Scalpers aim for 0.2% to 4%, kamikazes ride 20% to 60% targets with 12% to 25% stops. The option genes (calls
  or puts, days to expiry, out of the money, exit before expiry) are gone; bots imported from OBF simply drop them.
- **Scalpers are flat before the close on shares too**, and their trading-day and volatility gates work on any account
  (before, those only ran for option accounts).
- **Risk styles for stocks**: Cautious (5% trades, 3% stop, 6% target), Balanced (10%, 5%, 10%), Adventurous
  (15%, 8%, 20%).
- **Brokers live in `broker.py`**: account, quotes, orders and positions behind one small interface, with Alpaca built
  in. Schwab or Interactive Brokers can be added there later without touching the farm. The kill switch cancels every
  order the broker is still waiting on.
- **The Alpaca account tab** lists your stock positions (shares, average price, paid, worth, P/L) and can sell them or
  let SBF manage them with your stop, target and trailing rules. Options in the account are shown but left alone.
- **Removed (options only)**: the Options chain tab, the contract price sliders, the expiry guard, option pricing and
  option quotes, and the IBKR option bridge.
- Data lives in `~/.sbf/`. Dragons exported from OBF and CBBF can be imported.
