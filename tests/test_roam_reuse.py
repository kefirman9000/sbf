"""Free roam reuses what it downloaded: each stretch is studied a few times, and only the first visit can promote."""
import engine as E


def stretch(start, gran=60, bars=1000):
    closes = {"A": [100.0 + i for i in range(bars)]}
    extra = {"A": {"t": [start + i * gran for i in range(bars)], "v": [1.0] * bars}}
    return closes, extra, dict(start=start, end=start + bars * gran, gran=gran, bars=bars)


def test_each_stretch_is_studied_reuse_times_then_a_new_one_is_needed():
    pool = E.StretchPool()
    info = pool.add(("A",), 3, *stretch(0))
    assert info["revisit"] is False and info["visit"] == 1
    visits = [pool.take(("A",), 3) for _ in range(3)]
    assert [v[2]["visit"] for v in visits[:2]] == [2, 3]
    assert all(v[2]["revisit"] for v in visits[:2])
    assert visits[2] is None  # used up: time to download a new stretch


def test_reuse_of_one_means_always_new():
    pool = E.StretchPool()
    pool.add(("A",), 1, *stretch(0))
    assert pool.take(("A",), 1) is None


def test_least_visited_stretch_goes_first():
    pool = E.StretchPool()
    pool.add(("A",), 5, *stretch(0))
    pool.take(("A",), 5)
    pool.add(("A",), 5, *stretch(10 ** 6))
    assert pool.take(("A",), 5)[2]["start"] == 10 ** 6


def test_new_session_does_not_reuse_other_tickers():
    pool = E.StretchPool()
    pool.add(("A",), 3, *stretch(0))
    assert pool.take(("A", "B"), 3) is None
    assert not pool.items


def test_candle_size_and_length_must_fit():
    pool = E.StretchPool()
    pool.add(("A",), 3, *stretch(0, gran=60, bars=300))
    assert pool.take(("A",), 3, gran=300) is None  # a farm on 5-minute candles studies 5-minute candles
    assert pool.take(("A",), 3, min_bars=350) is None
    assert pool.take(("A",), 3, min_bars=200, gran=60) is not None


def test_each_visit_gets_its_own_copy():
    pool = E.StretchPool()
    pool.add(("A",), 4, *stretch(0))
    closes, extra, _ = pool.take(("A",), 4)
    closes["A"][0] = -1.0
    extra["A"]["t"].clear()
    closes2, extra2, _ = pool.take(("A",), 4)
    assert closes2["A"][0] == 100.0 and extra2["A"]["t"]


def test_only_a_few_stretches_are_kept_in_memory():
    pool = E.StretchPool(keep=2)
    for k in range(5):
        pool.add(("A",), 3, *stretch(k * 10 ** 6))
    assert len(pool.items) == 2
