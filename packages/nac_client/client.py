"""NacClient — the single Nokia NaC entry point.

Modes (NacConfig.mode):
  fixture    → FixtureBackend (in-memory), no HTTP. Tests and the "API is down" fallback mode.
  simulator  → the local apps/simulator HTTP mock (same paths, same fixtures). Proves the client's HTTP layer.
  live       → RapidAPI (https://network-as-code.p-eu.rapidapi.com), with real keys. SIMULATOR/DEFAULT plan.

Every call returns a NacResult: data (CAMARA body), api name, source mode, latency. Used for explain[].
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .errors import NacError, NacTimeout, error_from_status
from .fixtures import FixtureBackend
from .privacy import MaskingFilter, mask_phone, normalize_phone
from .resilience import CircuitBreaker, with_retry

log = logging.getLogger("nac_client")
log.addFilter(MaskingFilter())

# --- Verified paths (docs/nac-api-reference.md) ---------------------------------------------
P = {
    "reachability": "device-status/device-reachability-status/v1/retrieve",
    "geofencing_subs": "geofencing-subscriptions/v0.3/subscriptions",
    "location_retrieve": "location-retrieval/v0/retrieve",
    "location_verify": "location-verification/v1/verify",
    "congestion_query": "congestion-insights/v0/query",
    "qod_sessions": "quality-on-demand/v1/sessions",
    "qod_retrieve": "quality-on-demand/v1/retrieve-sessions",
}
PASSTHROUGH_PREFIX = "passthrough/"  # these paths require a Bearer token


@dataclass
class NacConfig:
    mode: str = "fixture"  # fixture | simulator | live
    base_url: str = "https://network-as-code.p-eu.rapidapi.com"
    rapidapi_key: str = ""
    rapidapi_host: str = "network-as-code.nokia.rapidapi.com"
    oauth_token: str = ""  # Bearer for passthrough APIs (obtained via client-credentials)
    timeout_s: float = 4.0
    retries: int = 2
    breaker_threshold: int = 3
    breaker_cooldown_s: float = 20.0
    fixture_path: str | None = None  # JSON to load in fixture mode

    @classmethod
    def from_env(cls) -> "NacConfig":
        mode = os.environ.get("NAC_MODE", "fixture")
        base = os.environ.get("NAC_BASE_URL") or (
            "http://127.0.0.1:8081" if mode == "simulator" else "https://network-as-code.p-eu.rapidapi.com"
        )
        return cls(
            mode=mode,
            base_url=base,
            rapidapi_key=os.environ.get("NAC_RAPIDAPI_KEY", ""),
            rapidapi_host=os.environ.get("NAC_RAPIDAPI_HOST", "network-as-code.nokia.rapidapi.com"),
            oauth_token=os.environ.get("NAC_OAUTH_TOKEN", ""),
            timeout_s=float(os.environ.get("NAC_TIMEOUT_S", "4")),
            retries=int(os.environ.get("NAC_RETRIES", "2")),
            fixture_path=os.environ.get("NAC_FIXTURE_PATH"),
        )


@dataclass
class NacResult:
    api: str
    data: Any
    source: str  # fixture | simulator | live
    latency_ms: int
    correlator: str
    request: dict = field(default_factory=dict)  # masked request summary (for explain)

    def to_dict(self) -> dict:
        return {"api": self.api, "source": self.source, "latency_ms": self.latency_ms, "correlator": self.correlator, "request": self.request, "response": self.data}


class NacClient:
    def __init__(self, config: NacConfig | None = None, fixtures: FixtureBackend | None = None, http: httpx.Client | None = None):
        self.cfg = config or NacConfig.from_env()
        self.breaker = CircuitBreaker(self.cfg.breaker_threshold, self.cfg.breaker_cooldown_s)
        if self.cfg.mode == "fixture":
            self.fx = fixtures or (FixtureBackend.from_json(self.cfg.fixture_path) if self.cfg.fixture_path else FixtureBackend())
            self.http = None
        else:
            self.fx = fixtures  # optional: fallback if HTTP fails
            self.http = http or httpx.Client(base_url=self.cfg.base_url.rstrip("/") + "/", timeout=self.cfg.timeout_s)
        self.calls: list[NacResult] = []  # recent calls (debug panel)

    # ---------------------------------------------------------------- core
    def _headers(self, path: str) -> dict:
        h = {"content-type": "application/json", "x-rapidapi-key": self.cfg.rapidapi_key, "x-correlator": str(uuid.uuid4())}
        if self.cfg.rapidapi_host:
            h["x-rapidapi-host"] = self.cfg.rapidapi_host
        if path.startswith(PASSTHROUGH_PREFIX) and self.cfg.oauth_token:
            h["authorization"] = f"Bearer {self.cfg.oauth_token}"
        return h

    def _call(self, api: str, method: str, path: str, *, json: dict | None = None, phone: str | None = None, fixture_fn=None) -> NacResult:
        corr = str(uuid.uuid4())
        req_summary = _mask_body(json or {})
        t0 = time.perf_counter()

        def do_fixture() -> Any:
            assert self.fx is not None
            self.fx.preflight(api, phone)
            lat = self.fx.get(phone).get("latency_ms", 0) if phone else 0
            if lat:
                time.sleep(min(lat, self.cfg.timeout_s * 1000) / 1000)
                if lat / 1000 > self.cfg.timeout_s:
                    raise NacTimeout(api, self.cfg.timeout_s)
            return fixture_fn()

        def do_http() -> Any:
            assert self.http is not None
            headers = self._headers(path)
            headers["x-correlator"] = corr
            try:
                r = self.http.request(method, path, json=json, headers=headers)
            except httpx.TimeoutException as e:
                raise NacTimeout(api, self.cfg.timeout_s) from e
            except httpx.HTTPError as e:
                raise NacError("network", api, str(e), retryable=True) from e
            if r.status_code >= 400:
                raise error_from_status(api, r.status_code, r.text)
            if r.status_code == 204 or not r.content:
                return {}
            return r.json()

        fn = do_fixture if self.cfg.mode == "fixture" else do_http
        try:
            data = with_retry(api, fn, retries=self.cfg.retries, breaker=self.breaker)
        except NacError as e:
            log.warning("nac call failed api=%s kind=%s phone=%s", api, e.kind, mask_phone(phone) if phone else "-")
            raise
        res = NacResult(api=api, data=data, source=self.cfg.mode, latency_ms=int((time.perf_counter() - t0) * 1000), correlator=corr, request=req_summary)
        self.calls.append(res)
        if len(self.calls) > 200:
            del self.calls[:-200]
        log.info("nac call api=%s source=%s ms=%d phone=%s", api, res.source, res.latency_ms, mask_phone(phone) if phone else "-")
        return res

    def health(self) -> dict:
        return {"mode": self.cfg.mode, "base_url": self.cfg.base_url if self.cfg.mode != "fixture" else None, "breaker": self.breaker.snapshot(), "recent_calls": len(self.calls)}

    # ---------------------------------------------------------------- device / location
    def reachability(self, phone: str) -> NacResult:
        p = normalize_phone(phone)
        return self._call("device-reachability-status", "POST", P["reachability"], json={"device": {"phoneNumber": p}}, phone=p, fixture_fn=lambda: self.fx.reachability(p))

    def location_retrieve(self, phone: str, max_age_s: int | None = None) -> NacResult:
        p = normalize_phone(phone)
        body = {"device": {"phoneNumber": p}, **({"maxAge": max_age_s} if max_age_s is not None else {})}
        return self._call("location-retrieval", "POST", P["location_retrieve"], json=body, phone=p, fixture_fn=lambda: self.fx.location_retrieve(p, max_age_s))

    def location_verify(self, phone: str, lat: float, lng: float, radius_m: float, max_age_s: int | None = None) -> NacResult:
        p = normalize_phone(phone)
        body = {"device": {"phoneNumber": p}, "area": _circle(lat, lng, radius_m), **({"maxAge": max_age_s} if max_age_s is not None else {})}
        return self._call("location-verification", "POST", P["location_verify"], json=body, phone=p, fixture_fn=lambda: self.fx.location_verify(p, lat, lng, radius_m, max_age_s))

    def congestion_query(self, phone: str, start: str | None = None, end: str | None = None) -> NacResult:
        p = normalize_phone(phone)
        body = {"device": {"phoneNumber": p}, **({"start": start} if start else {}), **({"end": end} if end else {})}
        return self._call("congestion-insights", "POST", P["congestion_query"], json=body, phone=p, fixture_fn=lambda: self.fx.congestion_query(p, start, end))

    # ---------------------------------------------------------------- subscriptions (webhook)
    def _sub_body(self, phone: str, sink: str, types: list[str], sink_token: str | None, expire: datetime | None, extra_detail: dict | None = None, initial_event: bool = False, max_events: int | None = None) -> dict:
        exp = expire or (datetime.now(timezone.utc) + timedelta(days=1))
        body: dict[str, Any] = {
            "protocol": "HTTP",
            "sink": sink,
            "types": types,
            "config": {
                "subscriptionDetail": {"device": {"phoneNumber": phone}, **(extra_detail or {})},
                "subscriptionExpireTime": exp.isoformat(),
                "initialEvent": initial_event,
            },
        }
        if max_events:
            body["config"]["subscriptionMaxEvents"] = max_events
        if sink_token:
            body["sinkCredential"] = {"credentialType": "ACCESSTOKEN", "accessToken": sink_token, "accessTokenType": "bearer"}
        return body

    # CAMARA geofencing: ONE EVENT TYPE PER SUBSCRIPTION. The Nokia SDK enforces this explicitly
    # (network_as_code/geofencing/raw_client.py) — sending both types in one body returns 400.
    # SEPARATE subscriptions are opened for area-entered and area-left; see geofence_subscribe_both().
    GEO_ENTERED = "org.camaraproject.geofencing-subscriptions.v0.area-entered"
    GEO_LEFT = "org.camaraproject.geofencing-subscriptions.v0.area-left"

    def geofence_subscribe(self, phone: str, lat: float, lng: float, radius_m: float, sink: str, *, types: list[str] | None = None, sink_token: str | None = None, expire: datetime | None = None, initial_event: bool = False) -> NacResult:
        p = normalize_phone(phone)
        types = types or [self.GEO_ENTERED]
        body = self._sub_body(p, sink, types, sink_token, expire, {"area": _circle(lat, lng, radius_m)}, initial_event)
        return self._call("geofencing-subscriptions", "POST", P["geofencing_subs"], json=body, phone=p, fixture_fn=lambda: self.fx.create_subscription("geofencing", body))

    def geofence_get(self, subscription_id: str) -> NacResult:
        return self._call("geofencing-subscriptions", "GET", f"{P['geofencing_subs']}/{subscription_id}", fixture_fn=lambda: self.fx.get_subscription(subscription_id))

    def geofence_delete(self, subscription_id: str) -> NacResult:
        return self._call("geofencing-subscriptions", "DELETE", f"{P['geofencing_subs']}/{subscription_id}", fixture_fn=lambda: self.fx.delete_subscription(subscription_id))

    # ---------------------------------------------------------------- QoD
    def qod_create(self, phone: str, app_server_ipv4: str, qos_profile: str, duration_s: int = 3600, sink: str | None = None) -> NacResult:
        p = normalize_phone(phone)
        body: dict[str, Any] = {"device": {"phoneNumber": p}, "applicationServer": {"ipv4Address": app_server_ipv4}, "qosProfile": qos_profile, "duration": duration_s}
        if sink:
            body["sink"] = sink
        return self._call("quality-on-demand", "POST", P["qod_sessions"], json=body, phone=p, fixture_fn=lambda: self.fx.qod_create(p, body))

    def qod_get(self, session_id: str) -> NacResult:
        return self._call("quality-on-demand", "GET", f"{P['qod_sessions']}/{session_id}", fixture_fn=lambda: self.fx.qod_get(session_id))

    def qod_delete(self, session_id: str) -> NacResult:
        return self._call("quality-on-demand", "DELETE", f"{P['qod_sessions']}/{session_id}", fixture_fn=lambda: self.fx.qod_delete(session_id))

    def qod_list(self, phone: str) -> NacResult:
        p = normalize_phone(phone)
        return self._call("quality-on-demand", "POST", P["qod_retrieve"], json={"device": {"phoneNumber": p}}, phone=p, fixture_fn=lambda: self.fx.qod_list(p))


# ---------------------------------------------------------------- helpers
def _circle(lat: float, lng: float, radius_m: float) -> dict:
    return {"areaType": "CIRCLE", "center": {"latitude": lat, "longitude": lng}, "radius": radius_m}


def _camel(snake: str) -> str:
    parts = snake.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _mask_body(body: dict) -> dict:
    out = {}
    for k, v in body.items():
        if isinstance(v, dict):
            out[k] = _mask_body(v)
        elif k in ("phoneNumber", "hashedPhoneNumber") and isinstance(v, str):
            out[k] = mask_phone(v)
        else:
            out[k] = v
    return out
