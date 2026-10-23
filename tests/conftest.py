# Lets the tests import orbbot no matter which folder pytest is started from.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_wall_clock(monkeypatch):
    """A test must never depend on the real date/time: anything that falls back to the wall clock
    (a StateStore or PaperClient built without the test's clock) sees a date far in the future,
    so such a test fails every time instead of only on some days."""
    from datetime import datetime, timezone
    import orbbot.state as st
    far = datetime(2099, 1, 1, tzinfo=timezone.utc)
    orig = st.StateStore.__init__

    def init(self, path, clock=None):
        orig(self, path, clock=clock or (lambda: far))
    monkeypatch.setattr(st.StateStore, "__init__", init)
