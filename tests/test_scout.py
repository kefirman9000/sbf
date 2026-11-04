"""Bot picks: busy and moving (up to a point), with one place for an index ETF. Shares can be bought in fractions, so a
stock's price never rules it out."""
import engine as E

SNAP = {"SPY": (680, 38, 0.4), "QQQ": (600, 24, 0.6), "IWM": (240, 7, 0.9), "NVDA": (190, 40, 2.1), "TSLA": (450, 33, 3.2),
        "AMD": (250, 12, 4.0), "PLTR": (190, 9, 4.5), "INTC": (38, 3, 5.0), "RIVN": (14, 0.8, 6.0), "SOFI": (28, 1.5, 4.8)}


def products(snap=SNAP):
    return [dict(coin=c, price=p, volume_usd=v * 1e9, change_24h=m) for c, (p, v, m) in snap.items()]


def test_an_index_etf_always_gets_a_place():
    for size in (100, 1000, None):
        picks = E.scout_coins(products(), 4, trade_size=size)
        assert len(picks) == 4 and any(c in E.INDEX_ETFS for c in picks)


def test_the_trade_size_does_not_change_the_picks():
    assert E.scout_coins(products(), 4, trade_size=50) == E.scout_coins(products(), 4, trade_size=5000)


def test_busy_moving_names_beat_quiet_thin_ones():
    picks = E.scout_coins(products(), 4, trade_size=100)
    assert "NVDA" in picks and "TSLA" in picks
    assert "RIVN" not in picks and "SOFI" not in picks


def test_a_share_dearer_than_a_trade_is_explained_not_skipped():
    why = {}
    picks = E.scout_coins(products(), 4, trade_size=100, why=why)
    assert "TSLA" in picks and "part of one" in why["TSLA"]


def test_a_huge_move_cannot_outweigh_everything():
    snap = dict(SNAP, RIVN=(14, 0.8, 40.0))  # +40% on thin volume
    assert E.scout_coins(products(snap), 2, trade_size=1000)[0] != "RIVN"
