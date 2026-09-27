"""Access control: secrets, bearer tokens and the per-client rate limiter.

Module globals (`API_TOKEN`, `WEBHOOK_TOKEN`) are read at call time, so tests and callers must refer to
them as `security.API_TOKEN`, not import the value.
"""
from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from collections import OrderedDict, deque
from typing import Optional

from fastapi import Header, HTTPException


def _secret(name: str) -> tuple[str, bool]:
    """The secret from the environment; otherwise a random per-process value. (value, came from env?)"""
    v = (os.environ.get(name) or "").strip()
    return (v, True) if v else (secrets.token_urlsafe(32), False)


# SECRETS HAVE NO DEFAULT IN THE REPO. Previously a fixed development token was written here: anyone reading the repo
# could send fake enter/exit events to the webhook and wipe the evidence ledger.
#   HS_API_TOKEN   admin: site/worker/WBGT/sweep/reset and reading personal data
#   WEBHOOK_TOKEN  only the operator's geofence notifications (given to the subscription as sinkCredential)
API_TOKEN, _API_TOKEN_FROM_ENV = _secret("HS_API_TOKEN")
WEBHOOK_TOKEN, _ = _secret("WEBHOOK_TOKEN")
PRODUCTION_SECRETS = ("HS_API_TOKEN", "WEBHOOK_TOKEN", "PHONE_HASH_SALT")


def missing_production_secrets(mode: str, environ) -> list[str]:
    """An instance connected to the live operator does NOT START with random secrets: on restart the hashes and
    the subscription key would change and the evidence ledger would no longer match up."""
    if mode != "live":
        return []
    return [k for k in PRODUCTION_SECRETS if not (environ.get(k) or "").strip()]


def _bearer_ok(authorization: str | None, token: str) -> bool:
    if not token or not authorization or not authorization.startswith("Bearer "):
        return False
    return hmac.compare_digest(authorization[7:].encode(), token.encode())   # constant-time


def demo_mode() -> bool:
    return (os.environ.get("HS_DEMO_MODE") or "").strip().lower() in ("1", "true", "yes", "on")


def require_api_token(authorization: Optional[str] = Header(None)) -> None:
    if not _bearer_ok(authorization, API_TOKEN):
        raise HTTPException(401, {"code": "UNAUTHENTICATED",
                                  "message": "this endpoint needs the API token: Authorization: Bearer <HS_API_TOKEN>"})


def require_read_access(authorization: Optional[str] = Header(None)) -> None:
    """Personal data (name, crew, nationality, evidence ledger) requires the token.
    In demo mode the data is fictional and stays open."""
    if not demo_mode():
        require_api_token(authorization)


class RateLimiter:
    """At most `limit` requests per client (IP) within the window; memory is bounded by `max_clients`.

    If `limit` is None, `HS_DEMO_RATE_PER_MIN` is read on every request (default 20, 0 = unlimited).
    Behind a proxy, uvicorn must be given `FORWARDED_ALLOW_IPS` to see the real IP (render.yaml).
    """

    def __init__(self, window_s: float = 60.0, max_clients: int = 10_000) -> None:
        self.limit: int | None = None
        self.window_s, self.max_clients = window_s, max_clients
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self._hits: OrderedDict[str, deque] = OrderedDict()

    def hit(self, key: str) -> int:
        """0 if allowed; otherwise how many seconds until it may be retried."""
        limit = self.limit if self.limit is not None else int(os.environ.get("HS_DEMO_RATE_PER_MIN") or 20)
        if limit <= 0:
            return 0
        now = time.monotonic()
        with self._lock:
            q = self._hits.pop(key, None) or deque()
            while q and now - q[0] >= self.window_s:
                q.popleft()
            self._hits[key] = q
            if len(q) >= limit:
                return max(1, int(self.window_s - (now - q[0])) + 1)
            q.append(now)
            while len(self._hits) > self.max_clients:
                self._hits.popitem(last=False)
            return 0
