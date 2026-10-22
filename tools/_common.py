"""Shared start-up for the tools: repo root, package path, .env and the evidence folder.

Previously every tool carried its own `.env` reader, and three of them built the path tied to the
repo folder's NAME — they broke when the repo was cloned under another name, and they wrote their
output under `tools/` instead of `evidence/`.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
for _p in (ROOT / "apps", ROOT / "packages"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def require_live_flag(what: str) -> None:
    """Tools that hit a live API run only with an explicit `--live` flag.

    If a `.env` is found, the tool makes real calls with the real key and spends quota. A "trial
    run" could do this without noticing; without the flag, nothing is read and nothing is called.
    """
    if "--live" not in sys.argv[1:]:
        print(f"{what}\nThis tool calls a live API and spends quota. Re-run with --live to proceed.")
        sys.exit(2)


def load_env() -> Path | None:
    """Loads the repo's `.env` (or, if absent, the one in the parent folder); values set in the shell win."""
    from dotenv import load_dotenv

    for candidate in (ROOT / ".env", ROOT.parent / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=False)
            return candidate
    return None


def evidence_file(prefix: str, suffix: str = "json") -> Path:
    """`evidence/<prefix>-YYYYmmdd-HHMMSS.<suffix>` — tool output is written here unedited."""
    EVIDENCE.mkdir(exist_ok=True)
    return EVIDENCE / f"{prefix}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.{suffix}"
