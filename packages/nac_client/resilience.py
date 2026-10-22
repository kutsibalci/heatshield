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
    probe_at: float | None = None      # yari acik denemenin basladigi an


# Girdiye ozgu hatalar (hatali numara, bilinmeyen cihaz) bir KESINTI degildir: tek bir bozuk kayit
# devreyi acip herkesi kor etmemeli. Servis cevap verdi — bu, ayakta oldugunun kanitidir.
INPUT_ERROR_KINDS = frozenset({"bad_request", "not_found"})


@dataclass
class CircuitBreaker:
    """API adı başına devre kesici.

    failure_threshold ardışık hatadan sonra devre açılır; cooldown_s boyunca çağrılar anında NacCircuitOpen fırlatır.
    Süre dolunca TEK bir deneme (half-open) geçer; o sonuçlanana kadar diğer çağrılar beklemez, reddedilir.
    Başarılıysa devre kapanır, başarısızsa yeniden açılır. Deneme hiç sonuçlanmazsa (beklenmedik bir
    istisna) bir cooldown sonra yeni bir denemeye izin verilir — devre sonsuza kadar kilitli kalmaz.
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
            now = time.monotonic()
            if st.half_open:
                if st.probe_at is not None and now - st.probe_at >= self.cooldown_s:
                    st.probe_at = now      # takili kalmis deneme: yenisine izin ver
                    return
                raise NacCircuitOpen(api, self.cooldown_s - (now - (st.probe_at or now)))
            elapsed = now - st.opened_at
            if elapsed >= self.cooldown_s:
                st.half_open, st.probe_at = True, now  # tek deneme izni
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
                st.half_open, st.probe_at = False, None

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
            if breaker and e.kind in INPUT_ERROR_KINDS:
                breaker.success(api)
            elif breaker:
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
