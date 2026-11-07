"""farmcore.py is shared by CBBF, OBF and SBF and must be identical in all three (see tools/sync_core.py)."""
import hashlib
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_shared_core_matches_its_fingerprint():
    with open(os.path.join(ROOT, "farmcore.py"), "rb") as f:
        fp = hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()
    with open(os.path.join(ROOT, "tests", "farmcore.sha256")) as f:
        want = f.read().strip()
    assert fp == want, ("farmcore.py differs from the shared copy. Change it in one app and run "
                        "`python tools/sync_core.py <other app folders>` to copy it everywhere.")


def test_engine_uses_the_shared_core():
    import engine
    import farmcore
    for name in ("View", "TrainingStopped", "exam_verdict", "market_mood", "slice_extra"):
        assert getattr(engine, name) is getattr(farmcore, name)
