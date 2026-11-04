import os
import sys
import tempfile

# keep tests away from the real ~/.sbf data folder
os.environ["HOME"] = tempfile.mkdtemp(prefix="sbf-test-home-")
os.environ["USERPROFILE"] = os.environ["HOME"]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


import random  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _repeatable(monkeypatch):
    """Every test sees the same 'random' farm: a generator made without a seed gets a fixed one, so a test can't pass
    or fail on the luck of the draw."""
    import engine
    real = random.Random
    counter = iter(range(10 ** 6))
    monkeypatch.setattr(engine.random, "Random", lambda *a: real(*a) if a else real(1000 + next(counter)))
    random.seed(12345)
    yield
