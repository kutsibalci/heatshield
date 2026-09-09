"""Normalize edilmiş hata tipleri. Üst katman sadece bunları görür."""
from __future__ import annotations


class NacError(Exception):
    """Tüm NaC hatalarının tabanı.

    kind: timeout | auth | rate_limit | not_found | bad_request | server | network | circuit_open | unknown
    """

    def __init__(self, kind: str, api: str, message: str = "", status: int | None = None, retryable: bool = False):
        self.kind = kind
        self.api = api
        self.status = status
        self.retryable = retryable
        super().__init__(f"[{api}] {kind}" + (f" ({status})" if status else "") + (f": {message}" if message else ""))

    def to_dict(self) -> dict:
        return {"kind": self.kind, "api": self.api, "status": self.status, "retryable": self.retryable, "message": str(self)}


class NacTimeout(NacError):
    def __init__(self, api: str, timeout_s: float):
        super().__init__("timeout", api, f"{timeout_s}s aşıldı", retryable=True)


class NacCircuitOpen(NacError):
    def __init__(self, api: str, retry_after_s: float):
        self.retry_after_s = retry_after_s
        super().__init__("circuit_open", api, f"circuit open, retry in {retry_after_s:.0f}s", retryable=False)


def error_from_status(api: str, status: int, body: str = "") -> NacError:
    if status in (401, 403):
        return NacError("auth", api, body[:200], status)
    if status == 404:
        return NacError("not_found", api, body[:200], status)
    if status == 429:
        return NacError("rate_limit", api, body[:200], status, retryable=True)
    if status in (400, 422):
        return NacError("bad_request", api, body[:200], status)
    if status >= 500:
        return NacError("server", api, body[:200], status, retryable=True)
    return NacError("unknown", api, body[:200], status)
