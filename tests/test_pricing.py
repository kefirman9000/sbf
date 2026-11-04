"""Practice option prices: steady volatility and a realistic minimum price, so a few % move can't turn a contract
into a 400x win that only exists in the model (the farm learned to chase those)."""
import random

import engine as E


def calm_then_burst(n_calm=2000, n_burst=15, seed=4):
    rng = random.Random(seed)
    p = [14.0]
    for _ in range(n_calm):
        p.append(p[-1] * (1 + rng.gauss(0, 0.0004)))
    calm = list(p)
    for _ in range(n_burst):
        p.append(p[-1] * (1 + 0.002 + rng.gauss(0, 0.003)))
    t = [1.77e9 + 60 * i for i in range(len(p))]
    return calm, p, t


def test_pricing_volatility_barely_moves_on_a_short_burst():
    calm, run, t = calm_then_burst()
    old_jump = E.realized_vol(run, t) / E.realized_vol(calm, t[:len(calm)])
    new_jump = E.pricing_vol(run, t) / E.pricing_vol(calm, t[:len(calm)])
    assert old_jump > 2.0  # the last-120-candles measure nearly triples...
    assert new_jump < 1.3  # ...the pricing one hardly moves


def test_daily_candles_use_a_few_months():
    rng = random.Random(1)
    p = [100.0]
    for _ in range(300):
        p.append(p[-1] * (1 + rng.gauss(0, 0.01)))
    t = [1.6e9 + 86400 * i for i in range(len(p))]
    assert abs(E.pricing_vol(p, t) - E.realized_vol(p, t, 120)) < 0.01  # (same few months, one day either way)


def test_pricing_volatility_has_a_floor():
    p = [100.0] * 500
    t = [1.77e9 + 60 * i for i in range(500)]
    assert E.pricing_vol(p, t) >= E.PRICING_VOL_FLOOR


def test_overnight_gaps_do_not_inflate_pricing_volatility():
    """A jump over a night (or an earnings gap) isn't a one-minute move: counted as one, INTC read 226% vs 92%."""
    import math
    rng = random.Random(7)
    sd_min = 0.50 / math.sqrt(252 * 390)
    p, t, t0 = [57.0], [1.77e9], 1.77e9
    for day in range(6):
        if day:
            p.append(p[-1] * (1 + (0.10 if day == 3 else rng.gauss(0, 0.02))))
            t.append(t0 + day * 86400)
        for m in range(1, 390):
            p.append(p[-1] * (1 + rng.gauss(0, sd_min)))
            t.append(t0 + day * 86400 + m * 60)
    assert E.realized_vol(p, t, 1950) > 0.75  # the old way
    assert 0.42 < E.pricing_vol(p, t) < 0.58  # close to the true 50%
