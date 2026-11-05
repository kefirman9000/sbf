# Changelog

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
