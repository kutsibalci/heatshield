"""Timeout + retry + circuit breaker. Ağ API'si yavaşsa ürün kilitlenmez."""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, TypeVar

from .errors import NacCircuitOpen, NacError

T = TypeVar("T")


@dataclass
class CircuitState:
    failures: int = 0
    opened_at: float | None = None
    half_open: bool = False


@dataclass
class CircuitBreaker:
    """API adı başına devre kesici.

    failure_threshold ardışık hatadan sonra devre açılır; cooldown_s boyunca çağrılar anında NacCircuitOpen fırlatır.
    Süre dolunca tek bir deneme (half-open) geçer; başarılıysa devre kapanır.
    """

    failure_threshold: int = 3
    cooldown_s: float = 20.0
    _states: dict[str, CircuitState] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _state(self, api: str) -> CircuitState:
        return self._states.setdefault(api, CircuitState())

    def before(self, api: str) -> None:
        with self._lock:
            st = self._state(api)
            if st.opened_at is None:
                return
            elapsed = time.monotonic() - st.opened_at
            if elapsed >= self.cooldown_s and not st.half_open:
                st.half_open = True  # tek deneme izni
                return
            if st.half_open:
                return
            raise NacCircuitOpen(api, self.cooldown_s - elapsed)

    def success(self, api: str) -> None:
        with self._lock:
            self._states[api] = CircuitState()

    def failure(self, api: str) -> None:
        with self._lock:
            st = self._state(api)
            st.failures += 1
            if st.half_open or st.failures >= self.failure_threshold:
                st.opened_at = time.monotonic()
                st.half_open = False

    def snapshot(self) -> dict:
        with self._lock:
            return {
                api: {"failures": s.failures, "open": s.opened_at is not None, "half_open": s.half_open}
                for api, s in self._states.items()
            }


def with_retry(
    api: str,
    fn: Callable[[], T],
    *,
    retries: int = 2,
    backoff_s: float = 0.2,
    breaker: CircuitBreaker | None = None,
) -> T:
    """fn'i retryable hatalarda tekrar dener; breaker'ı günceller."""
    attempt = 0
    while True:
        if breaker:
            breaker.before(api)
        try:
            result = fn()
        except NacError as e:
            if breaker:
                breaker.failure(api)
            if e.retryable and attempt < retries:
                attempt += 1
                time.sleep(backoff_s * (2 ** (attempt - 1)) + random.uniform(0, 0.05))
                continue
            raise
        else:
            if breaker:
                breaker.success(api)
            return result
