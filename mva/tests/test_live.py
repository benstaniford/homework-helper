"""
Opt-in live smoke tests against the real Canvas instance.

These are skipped unless CANVAS_TOKEN is set (e.g. via a .env file). They only READ
data and never modify anything. Run just these with:  pytest -m live
"""
import os

import pytest

from mva_canvas import MvaCanvas
from mva_canvas.api import DEFAULT_COOKIES_PATH

pytestmark = pytest.mark.live

# Live tests run if we have EITHER a token OR an exported session cookies file.
_cookies_path = os.environ.get("CANVAS_COOKIES", DEFAULT_COOKIES_PATH)
HAVE_CREDS = bool(os.environ.get("CANVAS_TOKEN")) or os.path.exists(_cookies_path)
skip_reason = "No CANVAS_TOKEN and no session cookies; skipping live Canvas tests"


@pytest.fixture(scope="module")
def live():
    if not HAVE_CREDS:
        pytest.skip(skip_reason)
    return MvaCanvas.from_env()


@pytest.mark.skipif(not HAVE_CREDS, reason=skip_reason)
def test_live_self(live):
    me = live.get_self()
    assert me.id and me.name


@pytest.mark.skipif(not HAVE_CREDS, reason=skip_reason)
def test_live_courses(live):
    courses = live.get_courses()
    assert len(courses) > 0
    assert all(c.name for c in courses)


@pytest.mark.skipif(not HAVE_CREDS, reason=skip_reason)
def test_live_upcoming_homework(live):
    # Should not raise; may legitimately be empty.
    hw = live.get_upcoming_homework()
    assert isinstance(hw, list)
