# Changelog

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
