"""Brokers SBF can trade shares through. No GUI code lives here.

Everything the app needs from a broker is on the Broker class: the account's money, live quotes, placing and
checking orders, and what the account holds. Alpaca is the first one. To add another broker (Schwab, Interactive
Brokers...), subclass Broker, fill in the same methods, and add it to BROKERS; the farm, the bots and the account
logic never talk to a broker directly.

Safety lock (every broker): SBF only BUYS shares to open and SELLS shares it holds, never more than the broker says
the account holds. Nothing is ever sold short, so no trade can lose more than what was paid for it."""
import math
import time
import urllib.error
import urllib.parse
import uuid

import engine as E

UnsafeOrder = E.UnsafeOrder
DONE = ("filled", "canceled", "expired", "rejected")  # an order in one of these states will never fill any further
QTY_EPS = 1e-9


def fmt_qty(q):
    """A share quantity as brokers want it: whole numbers plain, fractions to 9 decimals (rounded DOWN, so a sell
    never asks for a sliver more than the account holds)."""
    q = float(q)
    if abs(q - round(q)) < QTY_EPS:
        return str(int(round(q)))
    return f"{math.floor(q * 1e9) / 1e9:.9f}".rstrip("0").rstrip(".")


def check_order(side, qty=None, held=None, notional=None):
    """The safety lock. Buys are always allowed; a sell must be for more than nothing and no more than is held."""
    if side == "buy":
        if (qty is None or float(qty) <= 0) and (notional is None or float(notional) <= 0):
            raise UnsafeOrder("blocked: a buy needs a number of shares or a dollar amount above zero")
        return
    if side != "sell":
        raise UnsafeOrder(f"unknown order side {side!r}")
    if notional is not None:
        raise UnsafeOrder("blocked: sells are always for a number of shares the account holds, never a dollar amount")
    q, h = float(qty or 0), float(held or 0)
    if held is None or q <= 0 or q > h + QTY_EPS:
        raise UnsafeOrder(f"blocked: selling {fmt_qty(q)} share(s) while holding {fmt_qty(h)} would sell short. "
                          f"SBF only sells shares it owns.")


class Broker:
    """What SBF needs from a broker. Every method may raise; callers run them off the screen's thread."""
    key = ""                  # short id saved in the config ("alpaca")
    name = ""                 # shown to people ("Alpaca")
    fractional = False        # can it buy part of a share?

    def __init__(self):
        self.open = set()     # ids of orders sent and not yet known to be done: the kill switch cancels these

    def paper(self):
        """True when the logged-in account trades pretend money."""
        raise NotImplementedError

    def account(self):
        """-> dict(cash, equity, last_equity, buying_power, daytrades, pdt) in dollars / counts."""
        raise NotImplementedError

    def quotes(self, symbols):
        """-> {symbol: (bid, ask, time)} for the symbols that have a live quote."""
        raise NotImplementedError

    def asset(self, symbol):
        """-> dict(tradable, fractionable) for one symbol."""
        raise NotImplementedError

    def place(self, symbol, side, qty=None, notional=None, limit=None, held=None):
        """Sends one order (qty in shares, or notional in dollars for a fractional buy). -> order dict with 'id'."""
        raise NotImplementedError

    def order(self, oid):
        """-> dict(status, filled_qty, filled_avg_price) of an order sent earlier."""
        raise NotImplementedError

    def cancel(self, oid):
        raise NotImplementedError

    def positions(self):
        """-> {symbol: dict(qty, avg, value)} for every long share position in the account."""
        raise NotImplementedError

    def held(self, symbol):
        """Shares of this symbol the account holds long (0 if none)."""
        return float((self.positions().get(symbol) or {}).get("qty") or 0)

    # ---- built on the methods above ----
    def wait(self, oid, secs, on_stuck=None):
        """Polls an order until it is done or secs pass, then cancels the rest and makes sure it's really gone (an
        order left open could still fill later, unseen). -> (shares filled, average price per share, done?)."""
        o, until = {}, time.time() + secs
        self.open.add(oid)
        while time.time() < until:
            o = self.order(oid)
            if o.get("status") in DONE:
                self.open.discard(oid)
                return float(o.get("filled_qty") or 0), float(o.get("filled_avg_price") or 0), True
            time.sleep(1.0)
        for _ in range(3):
            try:
                self.cancel(oid)
            except Exception:
                E.note_error("cancel order")
            for _ in range(4):
                time.sleep(1.0)
                try:
                    o = self.order(oid)
                except Exception:
                    E.note_error("check cancelled order")
                    continue
                if o.get("status") in DONE:
                    self.open.discard(oid)
                    return float(o.get("filled_qty") or 0), float(o.get("filled_avg_price") or 0), True
        if on_stuck:  # (it stays in self.open, so the kill switch still cancels it)
            on_stuck(oid, o.get("status"))
        return float(o.get("filled_qty") or 0), float(o.get("filled_avg_price") or 0), False

    def buy_shares(self, symbol, spend, limit_wait=20, fallback=True, on_stuck=None):
        """Spends about `spend` dollars on a stock. Whole shares go in as a limit order at the ask (then at market if
        nobody takes it and fallback is on); a trade too small for one share buys part of one at market (fractional
        shares only fill at market prices). -> (shares, average price)."""
        q = self.quotes([symbol]).get(symbol)
        ask = q[1] if q and q[1] > 0 else 0.0
        whole = int(spend // ask) if ask else 0
        if whole >= 1:
            r = self.place(symbol, "buy", qty=whole, limit=round(ask, 2))
            got, avg, _ = self.wait(r["id"], max(5, int(limit_wait)), on_stuck)
            if got <= 0 and fallback:
                r = self.place(symbol, "buy", qty=whole)
                got, avg, _ = self.wait(r["id"], 30, on_stuck)
            return got, avg
        if not self.fractional or not self.asset(symbol).get("fractionable"):
            raise RuntimeError(f"one share of {symbol} costs about ${ask:,.2f}, more than this trade's ${spend:,.2f}, and "
                               f"{self.name} can't buy part of a share of it")
        r = self.place(symbol, "buy", notional=round(spend, 2))
        got, avg, _ = self.wait(r["id"], 30, on_stuck)
        return got, avg

    def sell_shares(self, symbol, qty, urgent=False, limit_wait=20, fallback=True, on_stuck=None):
        """Sells up to qty shares, never more than the broker says are held. Normal exits try a limit at the bid
        first; urgent ones (stops, kill switch) and part-shares go at market. -> (shares sold, average price)."""
        held = self.held(symbol)
        qty = min(float(qty), held)
        if qty <= QTY_EPS:
            raise UnsafeOrder(f"{self.name} shows no {symbol} shares in the account, so nothing was sold "
                              "(selling would open a short position).")
        limit = None
        if not urgent and abs(qty - round(qty)) < QTY_EPS:
            q = self.quotes([symbol]).get(symbol)
            if q and q[0] > 0:
                limit = round(q[0], 2)
        r = self.place(symbol, "sell", qty=qty, limit=limit, held=held)
        got, avg, _ = self.wait(r["id"], 30 if not limit else max(5, int(limit_wait)), on_stuck)
        if got <= 0 and limit and (fallback or urgent):
            r = self.place(symbol, "sell", qty=qty, held=held)
            got, avg, _ = self.wait(r["id"], 30, on_stuck)
        return got, avg


class Alpaca(Broker):
    """Alpaca's trading API (paper or live, chosen by the key you log in with). Stocks are commission-free and can be
    bought in fractions."""
    key, name, fractional = "alpaca", "Alpaca", True

    def paper(self):
        return bool(E.ALPACA.get("paper", True))

    def _url(self, path):
        return f"{E.trade_url()}{path}"

    def account(self):
        a = E.alp_account() or {}
        f = lambda k: float(a.get(k) or 0)
        return dict(cash=f("cash"), equity=f("equity"), last_equity=f("last_equity"), buying_power=f("buying_power"),
                    daytrades=int(a.get("daytrade_count") or 0), pdt=bool(a.get("pattern_day_trader")),
                    blocked=bool(a.get("trading_blocked") or a.get("account_blocked")))

    def quotes(self, symbols):
        out = {}
        syms = [s for s in dict.fromkeys(symbols or []) if s]
        for k in range(0, len(syms), 100):
            q = urllib.parse.quote(",".join(syms[k:k + 100]))
            d = E.alp("GET", f"{E.DATA_URL}/v2/stocks/quotes/latest?symbols={q}&feed={E.FEED}")
            for sym, x in (d.get("quotes") or {}).items():
                try:
                    bid, ask = float(x.get("bp") or 0), float(x.get("ap") or 0)
                except (TypeError, ValueError):
                    continue
                if bid > 0 or ask > 0:
                    out[sym] = (bid, ask, time.time())
        return out

    def asset(self, symbol):
        a = E.alp("GET", self._url(f"/v2/assets/{urllib.parse.quote(symbol)}")) or {}
        return dict(tradable=bool(a.get("tradable")), fractionable=bool(a.get("fractionable")))

    def _by_client_id(self, cid):
        try:
            return E.alp("GET", self._url(f"/v2/orders:by_client_order_id?client_order_id={urllib.parse.quote(cid)}"))
        except E.AlpacaError as ex:
            if ex.code == 404:
                return None
            raise

    def place(self, symbol, side, qty=None, notional=None, limit=None, held=None):
        """Never sent twice: every order carries its own client_order_id, and when the answer is lost (timeout,
        dropped connection, server error) Alpaca is asked whether it arrived before trying again. A second send with
        the same id is refused by Alpaca, so even a race can't place a duplicate."""
        check_order(side, qty, held, notional)
        cid = f"sbf-{uuid.uuid4().hex}"
        body = dict(symbol=symbol, side=side, type="limit" if limit else "market", time_in_force="day",
                    client_order_id=cid)
        if notional is not None:
            body["notional"] = f"{float(notional):.2f}"
        else:
            body["qty"] = fmt_qty(qty)
        if limit:
            body["limit_price"] = f"{float(limit):.2f}"
        url = self._url("/v2/orders")
        try:
            return E.alp("POST", url, body, retries=0)
        except E.AlpacaError as ex:
            if ex.code != 429 and ex.code < 500:
                raise  # Alpaca looked at it and said no: nothing was placed
            first = ex
        except (urllib.error.URLError, OSError) as ex:
            first = ex
        time.sleep(1.5)
        found = self._by_client_id(cid)  # did it arrive even though the answer didn't?
        if found:
            return found
        try:
            return E.alp("POST", url, body, retries=0)
        except E.AlpacaError as ex:
            if ex.code == 422:  # "client_order_id must be unique": the first send did arrive after all
                found = self._by_client_id(cid)
                if found:
                    return found
            raise ex from first

    def order(self, oid):
        return E.alp("GET", self._url(f"/v2/orders/{oid}"))

    def cancel(self, oid):
        return E.alp("DELETE", self._url(f"/v2/orders/{oid}"))

    def positions(self):
        out = {}
        for p in E.alp("GET", self._url("/v2/positions")) or []:
            if p.get("asset_class", "us_equity") != "us_equity" or p.get("side", "long") != "long":
                continue
            try:
                q = float(p.get("qty") or 0)
            except (TypeError, ValueError):
                continue
            if q > 0:
                out[p["symbol"]] = dict(qty=q, avg=float(p.get("avg_entry_price") or 0),
                                        value=float(p.get("market_value") or 0))
        return out


BROKERS = {"alpaca": Alpaca}
_ACTIVE = {}


def get(key="alpaca"):
    """The broker object for this id (one shared instance each)."""
    k = key if key in BROKERS else "alpaca"
    if k not in _ACTIVE:
        _ACTIVE[k] = BROKERS[k]()
    return _ACTIVE[k]
