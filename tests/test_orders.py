"""Live-order safety (broker.py): SBF never sells what it doesn't hold, never sends the same order twice, and knows
which orders are still open so the kill switch can cancel them."""
import urllib.error

import pytest

import broker as B
import engine as E


class FakeAlpaca:
    """Pretends to be Alpaca's order, quote, asset and position endpoints. `lose_answer` makes the first POST arrive
    but its reply get lost; `fill` decides what an order looks like when it's checked."""

    def __init__(self, lose_answer=False, fail_first=None, held=None, quote=(99.9, 100.1), fractionable=True,
                 fill="filled"):
        self.orders, self.by_id = {}, {}
        self.posts = 0
        self.lose_answer = lose_answer
        self.fail_first = fail_first
        self.held = dict(held or {})
        self.quote = quote
        self.fractionable = fractionable
        self.fill = fill
        self.cancelled = []

    def __call__(self, method, url, body=None, timeout=12, retries=1, creds=None):
        if method == "POST":
            self.posts += 1
            if self.fail_first is not None and self.posts == 1:
                raise self.fail_first
            cid = body["client_order_id"]
            if cid in self.orders:
                raise E.AlpacaError(422, "Alpaca said 422: client_order_id must be unique")
            o = dict(body, id=f"ord-{len(self.orders)}")
            self.orders[cid] = self.by_id[o["id"]] = o
            if self.lose_answer and self.posts == 1:
                raise urllib.error.URLError("timed out")
            return o
        if method == "GET" and "by_client_order_id" in url:
            cid = url.split("client_order_id=")[1]
            if cid not in self.orders:
                raise E.AlpacaError(404, "Alpaca said 404: order not found")
            return self.orders[cid]
        if method == "GET" and "/v2/orders/" in url:
            o = self.by_id[url.rsplit("/", 1)[1]]
            if self.fill != "filled":
                return dict(o, status=self.fill, filled_qty="0")
            px = float(o.get("limit_price") or self.quote[1])
            qty = float(o["qty"]) if "qty" in o else float(o["notional"]) / px
            return dict(o, status="filled", filled_qty=str(qty), filled_avg_price=str(px))
        if method == "DELETE" and "/v2/orders/" in url:
            oid = url.rsplit("/", 1)[1]
            self.cancelled.append(oid)
            self.fill = "canceled"
            return {}
        if method == "GET" and "/quotes/latest" in url:
            sym = url.split("symbols=")[1].split("&")[0]
            return {"quotes": {sym: {"bp": self.quote[0], "ap": self.quote[1]}}}
        if method == "GET" and "/v2/assets/" in url:
            return {"tradable": True, "fractionable": self.fractionable}
        if method == "GET" and url.endswith("/v2/positions"):
            return [dict(symbol=s, qty=str(q), side="long", asset_class="us_equity", avg_entry_price="100",
                         market_value=str(q * 100)) for s, q in self.held.items()]
        raise AssertionError(f"unexpected call {method} {url}")


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(B.time, "sleep", lambda s: None)
    monkeypatch.setitem(E.ALPACA, "key", "PKTEST")
    monkeypatch.setitem(E.ALPACA, "secret", "secret")

    def make(**kw):
        f = FakeAlpaca(**kw)
        monkeypatch.setattr(E, "alp", f)
        return f
    return make


def test_lost_reply_does_not_place_a_second_order(fake):
    f = fake(lose_answer=True)
    r = B.Alpaca().place("SPY", "buy", qty=2, limit=1.23)
    assert len(f.orders) == 1
    assert r["id"] == "ord-0"


def test_order_that_never_arrived_is_sent_again(fake):
    f = fake(fail_first=urllib.error.URLError("connection reset"))
    r = B.Alpaca().place("SPY", "buy", qty=1)
    assert len(f.orders) == 1 and f.posts == 2
    assert r["qty"] == "1"


def test_server_error_checks_before_resending(fake):
    f = fake(fail_first=E.AlpacaError(503, "Alpaca said 503"))
    B.Alpaca().place("SPY", "buy", qty=1)
    assert len(f.orders) == 1


def test_rejected_order_is_not_retried(fake):
    f = fake(fail_first=E.AlpacaError(403, "Alpaca said 403: insufficient buying power"))
    with pytest.raises(E.AlpacaError):
        B.Alpaca().place("SPY", "buy", qty=1)
    assert f.posts == 1


def test_every_order_has_its_own_client_id(fake):
    f = fake()
    bk = B.Alpaca()
    bk.place("A", "buy", qty=1)
    bk.place("A", "buy", qty=1)
    assert len(f.orders) == 2


@pytest.mark.parametrize("qty,held", [(1, None), (2, 1), (0, 5), (-1, 5), (1.5000001, 1.5)])
def test_never_sells_more_than_it_holds(qty, held):
    with pytest.raises(B.UnsafeOrder):
        B.check_order("sell", qty, held)


def test_unknown_side_is_blocked():
    with pytest.raises(B.UnsafeOrder):
        B.check_order("sell_short", 1, 1)


def test_a_sell_is_never_a_dollar_amount():
    with pytest.raises(B.UnsafeOrder):
        B.check_order("sell", None, 5, notional=100)


def test_closing_what_it_holds_is_allowed():
    B.check_order("sell", 2, 2)
    B.check_order("sell", 0.3333333333, 0.3333333333)
    B.check_order("buy", 3)
    B.check_order("buy", notional=12.5)


@pytest.mark.parametrize("q,txt", [(3, "3"), (3.0000000001, "3"), (0.123456789987, "0.123456789"), (2.5, "2.5")])
def test_quantities_are_written_the_way_brokers_want(q, txt):
    assert B.fmt_qty(q) == txt


def test_whole_shares_buy_with_a_limit_at_the_ask(fake):
    f = fake(quote=(99.9, 100.1))
    got, avg = B.Alpaca().buy_shares("SPY", 450)
    o = next(iter(f.orders.values()))
    assert o["type"] == "limit" and o["qty"] == "4" and o["limit_price"] == "100.10"
    assert got == 4 and avg == pytest.approx(100.1)


def test_a_trade_smaller_than_one_share_buys_part_of_one(fake):
    f = fake(quote=(499.9, 500.1))
    got, avg = B.Alpaca().buy_shares("SPY", 100)
    o = next(iter(f.orders.values()))
    assert o["type"] == "market" and o["notional"] == "100.00" and "qty" not in o
    assert got == pytest.approx(100 / 500.1)


def test_part_shares_need_a_fractionable_stock(fake):
    fake(quote=(499.9, 500.1), fractionable=False)
    with pytest.raises(RuntimeError, match="part of a share"):
        B.Alpaca().buy_shares("XYZ", 100)


def test_selling_never_asks_for_more_than_the_broker_holds(fake):
    f = fake(held={"SPY": 2.5})
    got, _ = B.Alpaca().sell_shares("SPY", 10)
    o = next(iter(f.orders.values()))
    assert o["side"] == "sell" and float(o["qty"]) == 2.5 and got == 2.5
    assert o["type"] == "market"  # a part share goes at market


def test_whole_shares_sell_with_a_limit_at_the_bid_unless_urgent(fake):
    f = fake(held={"SPY": 3}, quote=(99.9, 100.1))
    B.Alpaca().sell_shares("SPY", 3)
    assert next(iter(f.orders.values()))["limit_price"] == "99.90"
    f2 = fake(held={"SPY": 3})
    B.Alpaca().sell_shares("SPY", 3, urgent=True)
    assert next(iter(f2.orders.values()))["type"] == "market"


def test_nothing_held_means_nothing_is_sold(fake):
    f = fake(held={})
    with pytest.raises(B.UnsafeOrder):
        B.Alpaca().sell_shares("SPY", 1)
    assert f.posts == 0


def test_an_unfilled_order_is_cancelled_and_forgotten(fake, monkeypatch):
    f = fake(fill="new")
    clock = iter(range(0, 10 ** 6, 10))
    monkeypatch.setattr(B.time, "time", lambda: next(clock))
    bk = B.Alpaca()
    r = bk.place("SPY", "buy", qty=1, limit=100)
    got, _, done = bk.wait(r["id"], 5)
    assert got == 0 and done and f.cancelled == [r["id"]]
    assert r["id"] not in bk.open


def test_options_and_shorts_in_the_account_are_not_counted_as_held(fake, monkeypatch):
    fake()
    monkeypatch.setattr(E, "alp", lambda *a, **k: [
        dict(symbol="SPY", qty="2", side="long", asset_class="us_equity"),
        dict(symbol="SPY250101C00500000", qty="1", side="long", asset_class="us_option"),
        dict(symbol="TSLA", qty="-3", side="short", asset_class="us_equity")])
    assert B.Alpaca().positions() == {"SPY": dict(qty=2.0, avg=0.0, value=0.0)}
