"""Timeout + retry + circuit breaker. If the network API is slow, the product does not lock up."""
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
    probe_at: float | None = None      # moment the half-open probe started


# Input-specific errors (bad number, unknown device) are NOT an OUTAGE: a single broken record
# must not open the circuit and blind everyone. The service answered — that is proof it is up.
INPUT_ERROR_KINDS = frozenset({"bad_request", "not_found"})


@dataclass
class CircuitBreaker:
    """Circuit breaker per API name.

    The circuit opens after failure_threshold consecutive failures; for cooldown_s, calls raise NacCircuitOpen at once.
    When the time is up, a SINGLE probe (half-open) goes through; until it resolves, other calls do not wait, they are
    rejected. If it succeeds the circuit closes; if it fails it reopens. If the probe never resolves (an unexpected
    exception), a new probe is allowed after one cooldown — the circuit does not stay locked forever.
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
                    st.probe_at = now      # stuck probe: allow a new one
                    return
                raise NacCircuitOpen(api, self.cooldown_s - (now - (st.probe_at or now)))
            elapsed = now - st.opened_at
            if elapsed >= self.cooldown_s:
                st.half_open, st.probe_at = True, now  # allow a single probe
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
    """Retries fn on retryable errors; updates the breaker."""
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
