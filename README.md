<p align="center"><b>SBF, the Stock Bot Farm. Hatch a farm of little dragons that trade stocks. Let them compete, breed and evolve.
Only the proven ones get a say.</b></p>

SBF is the stock-market sibling of [OBF](https://github.com/kefirman9000/obf) (options) and
[CBBF](https://github.com/kefirman9000/cbbf) (crypto). Same farm, same dragons, same learning; the bots buy and sell
**shares** instead of option contracts or coins.

## Getting started
- **Windows:** download the latest release zip, extract the whole folder, double-click **Run SBF.bat** (or **SBF.pyw**)
- **Mac / Linux:** `python3 main.py` (Python 3.8 or newer with Tkinter)
- Prices come from **Alpaca**. A free Alpaca account with a **paper** key is all you need: Account > Log in to Alpaca.

New versions appear inside the app as an **Update** button. Nothing installs until you say yes, and
**Tools > Undo last update** always takes you back.

---

## Only proven bots trade your account
Every bot trains on its own pretend money. Your account (Practice, Alpaca paper or LIVE) only follows bots with a
**real track record**:

- at least **8 finished trades** (Settings > Wildcard)
- at least one full scoring window on the farm
- money made over their **lifetime**, and more than just holding the same stocks
- **passed exams** on days they never trained on (one day in five is held back for exams)

Among the proven bots, the best lifetime records get the **biggest say** (up to 3x). A bot whose record slips loses its
vote again. Kamikazes never vote. Bots you pick yourself (My team, Hall of Fame only) aren't held back.

Expect the account to trade little at first: it waits until bots have proven themselves. The Overview tab shows how
many bots have a vote right now and who's leading.

## From egg to legend
Every dragon climbs the same ladder, and the farm sorts them into dens:

| Den | Who lives there |
|---|---|
| **Eggs** | Newborns still hatching and learning the ropes |
| **Rising** | Young dragons starting to prove themselves |
| **BEASTS** | The dominant ones, winning again and again |
| **Hall of Fame** | Your hand-picked champions. Only you decide who gets in |

Every dragon has **XP, health and power**, and earns **badges** along the way.

## A dragon's genes
- **A main instinct**: more than twenty breeds, from patient trend riders to dip buyers and breakout jumpers
- **Studies** that vote for or against buying, a **bigger-picture** trend that has to agree first, and an optional
  **follow-the-leader** study (SPY's move votes on other stocks)
- **Market context**: studies on tickers it watches but never trades (VIXY for the VIX, TLT, ...)
- **Never-rules**: things it refuses to do (no buys before the Fed, none in the first minutes after the open...), each
  graded in hindsight; useful rules spread, costly ones fade
- **Stops, targets and trailing stops** sized for share prices, **patience**, and exits that adapt to calm or wild times
- **Memory**: which stocks it's bad at, which seasons suit it, and every lesson from its tests

## Breeds
- **Long-term dragons** ride trends and dips over minutes to days.
- **Scalpers** (wind dragons) are quick in and out: a tight stop and target (0.2% nips up to 4% runs), a time limit,
  only tight bid/ask gaps, and always **flat before the close**.
- **Kamikazes** make big bets with wide stops and test risky rules so your good bots don't have to. No say in your
  account.
- **The slow farm**: a second farm of patient dragons on hourly candles that hold for days.

## Breeding and learning
- **Eggs from winners**, **crossbreeding**, royal and silver eggs, **islands**, **elders**, **seasons** and
  **mutation memory**, exactly as on OBF and CBBF
- **Free roam**: the farm goes back in time by itself, sends practice farms through stretches of history, replays
  whole days minute by minute for the scalpers, and brings every lesson home
- **Training camp**, **Trials** and **Backtests** on any stretch since 2016
- **Exams** on held-back days: only bots that clearly beat luck can become BEASTS, or get a vote

## Your account
- **Practice**: pretend money on live prices, simulated inside SBF. Every fill pays half the bid/ask gap, and the
  small regulatory fees on sells are counted.
- **Practice + Alpaca paper**: every practice trade is also copied to your Alpaca paper account.
- **Alpaca paper / LIVE**: orders go to Alpaca. Whole shares buy with a limit at the ask and sell with a limit at the
  bid, then at market if nobody takes it. Trades smaller than one share buy **part of a share** (fractional shares).
- Safety stop, lock-in profit, trailing stop, daily loss limit, a limit per stock and per group of stocks that move
  together, and an hourly trade brake
- **Day-trade guard**: under $25,000 it never makes a 4th day trade in 5 business days
- **Kill switch** (Account menu): stop, cancel waiting orders, sell everything and lock LIVE

**Safety lock:** SBF only buys shares and only sells shares the broker says the account holds. It never sells short and
never borrows on margin, so no trade can lose more than it paid.

## Brokers
Alpaca is built in. Everything SBF needs from a broker (account, quotes, orders, positions) sits in `broker.py`, so
adding Schwab or Interactive Brokers later means writing one new class there; the farm and the bots don't change.

## Collect them all
Press **Catch!** on any dragon to keep it in your **Library**. Every caught dragon gets a name, a rarity from
**Common** to **Legendary**, and the occasional one is **shiny**. Build a team of up to 6, compare two side by side,
and export or import dragons. Dragons from OBF and CBBF can be imported too, to see if they survive on stocks.

## Your data
Everything stays on your computer in `~/.sbf/` (Windows: `C:\Users\<you>\.sbf`). **File > Open data folder** opens
it. Your Alpaca keys are kept in your system keychain, not in SBF's files.

## Support the farm
SBF is free and made in spare time. If you'd like to help the farm grow, you can support it on
**[GitHub Sponsors](https://github.com/sponsors/kefirman9000)**. Starring the repo and sharing your best dragons helps too.

## License and disclaimer
Copyright © 2026 kefirman9000. All rights reserved. SBF is free to use; see [LICENSE](LICENSE) for what is and isn't
allowed.

- **Not financial advice.** SBF is a learning and experimentation tool. Nothing it shows is advice or a recommendation.
- You can lose money. You alone are responsible for every action taken through your account. Always test in paper
  mode first and never use money you can't afford to lose.
- Software can have bugs, connections can fail, and data can be late or wrong. Check your account directly.

SBF is provided **as is**, without warranty of any kind. Found a security problem? Please report it privately; see
[SECURITY.md](SECURITY.md).

Past results never guarantee future ones, even for a Legendary dragon.
