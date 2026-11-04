"""The farm: it keeps to its size without dropping bots too young to judge, and a saved farm loads back intact."""
import json

import engine as E

ASSET = "SPY" if E.KEYRING_SERVICE in ("obf", "sbf") else "BTC"


def farm(cap=20):
    return E.Tournament([ASSET], 1000.0, 0.006, 0.1, 50, cap=cap)


def test_saved_farm_loads_back_the_same():
    t = farm()
    st = json.loads(json.dumps(t.to_state()))  # through JSON, like the real save file
    t2 = farm()
    assert t2.restore(st)
    assert sorted(v.key() for v in t2.vars) == sorted(v.key() for v in t.vars)


def test_young_bots_are_never_dropped_to_make_room():
    t = farm(cap=10)
    assert len(t.vars) > t.cap and all(t.young(v) for v in t.vars)  # a fresh farm starts a bit over a small size
    assert t.trim_to_cap() == 0  # but nobody can be judged yet, so nobody is dropped
    assert not t.weakest()


def test_overfull_farm_trims_matured_unjudged_bots_first():
    t = farm(cap=10)
    for v in t.vars:
        v.age = 10 ** 6  # had a full window, never earned a score
    n_before = len(t.vars)
    gone = t.trim_to_cap()
    assert len(t.vars) == min(n_before, t.cap) and gone == max(0, n_before - t.cap)


def test_nursery_full_when_many_bots_are_young():
    t = farm(cap=10)
    assert t.nursery_full()
    for v in t.vars:
        v.age = 10 ** 6
    assert not t.nursery_full()


def test_note_error_writes_errors_log():
    import os
    try:
        raise ValueError("boom")
    except ValueError:
        E.note_error("test")
    with open(os.path.join(E.HOME_DIR, "errors.log")) as f:
        assert "ValueError: boom" in f.read()


def test_fresh_farm_can_step_through_prices():
    """A new farm's bots can remember more decisions than its buy-and-hold record; rating them must not crash."""
    import random
    rng = random.Random(1)
    syms = ["SPY", "QQQ", "IWM"] if E.KEYRING_SERVICE in ("obf", "sbf") else ["BTC", "ETH", "SOL"]
    hist = {}
    for s in syms:
        p, hist[s] = 100.0, []
        for _ in range(1600):
            p *= 1 + rng.gauss(0, 0.003)
            hist[s].append(p)
    t = E.Tournament(syms, 1000.0, 0.006, 0.1, 24, cap=60)
    for i in range(1500, 1560):
        t.step({s: h[:i] for s, h in hist.items()})
    assert t.vars
