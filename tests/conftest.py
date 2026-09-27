"""Test environment — path setup AND environment isolation.

A mutation audit found that `conftest.py` only set `sys.path` and did not clean the
environment. On a machine with `GEMINI_API_KEY` defined, the scan tests **made network
calls to the real Gemini**. This is exactly what happens for a judge who clones the repo: tests slow down,
burn quota, and even give different results than on a machine without keys.

Tests do NOT reach the outside world. This file guarantees that.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "packages", ROOT / "apps", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# Every environment variable that could change the tests' behavior
_ISOLATED = (
    "GEMINI_API_KEY", "GROQ_API_KEY", "HS_PLANNER", "HS_GEMINI_MODEL", "HS_GROQ_MODEL",
    "HS_SCHEDULER", "HS_SCHEDULER_TICK_S", "HS_QUERY_UNIT_COST_USD",
    "NAC_RAPIDAPI_KEY", "NAC_OAUTH_TOKEN", "NAC_BASE_URL",
)


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch):
    """Make the environment deterministic before every test.

    `NAC_MODE` and `NAC_FALLBACK_TO_FIXTURE` are pinned explicitly: previously, `NAC_MODE=live`
    in the environment was NOT overridden because of `setdefault` — so on a machine with a live key
    the tests could hit the real Nokia platform.
    """
    for name in _ISOLATED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NAC_MODE", "fixture")
    # Demo scenarios are the input of most tests; the disabled case is tested separately in test_security.py.
    monkeypatch.setenv("HS_DEMO_MODE", "1")
    monkeypatch.setenv("NAC_FALLBACK_TO_FIXTURE", "0")
    monkeypatch.setenv("NAC_FIXTURE_PATH", str(ROOT / "fixtures" / "profiles.json"))
    yield
