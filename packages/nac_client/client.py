"""NacClient — tek Nokia NaC giriş noktası.

Modlar (NacConfig.mode):
  fixture    → FixtureBackend (bellek içi), HTTP yok. Testler ve "API çöktü" yedek modu.
  simulator  → yerel apps/simulator HTTP mock'u (aynı path'ler, aynı fixture'lar). Client'ın HTTP katmanını kanıtlar.
  live       → RapidAPI (https://network-as-code.p-eu.rapidapi.com), gerçek anahtarlarla. SIMULATOR/DEFAULT planı.

Her çağrı NacResult döner: data (CAMARA gövdesi), api adı, kaynak modu, gecikme. explain[] için kullanılır.
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

# --- Doğrulanmış path'ler (docs/nac-api-reference.md) ---------------------------------------
P = {
    "number_recycling": "passthrough/camara/v1/number-recycling/number-recycling/v0.2/check",
    "call_forwardings": "passthrough/camara/v1/call-forwarding-signal/call-forwarding-signal/v0.3/call-forwardings",
    "unconditional_cf": "passthrough/camara/v1/call-forwarding-signal/call-forwarding-signal/v0.3/unconditional-call-forwardings",
    "kyc_tenure": "passthrough/camara/v1/kyc-tenure/kyc-tenure/v0.1/check-tenure",
    "kyc_age": "passthrough/camara/v1/kyc-age-verification/kyc-age-verification/v0.1/verify",
    "kyc_match": "passthrough/camara/v1/kyc-match/kyc-match/v0.3/match",
    "number_verify": "passthrough/camara/v1/number-verification/number-verification/v2/verify",
    "device_phone_number": "passthrough/camara/v1/number-verification/number-verification/v2/device-phone-number",
    "sim_swap_check": "passthrough/camara/v1/sim-swap/sim-swap/v0/check",
    "sim_swap_date": "passthrough/camara/v1/sim-swap/sim-swap/v0/retrieve-date",
    "device_swap_check": "passthrough/camara/v1/device-swap/device-swap/v1/check",
    "device_swap_date": "passthrough/camara/v1/device-swap/device-swap/v1/retrieve-date",
    "consent": "passthrough/camara/v1/consent-info/consent-info/v0.1/retrieve",
    "reachability": "device-status/device-reachability-status/v1/retrieve",
    "roaming": "device-status/device-roaming-status/v1/retrieve",
    "reachability_subs": "device-status/device-reachability-status-subscriptions/v0.8/subscriptions",
    "roaming_subs": "device-status/device-roaming-status-subscriptions/v0.8/subscriptions",
    "geofencing_subs": "geofencing-subscriptions/v0.3/subscriptions",
    "location_retrieve": "location-retrieval/v0/retrieve",
    "location_verify": "location-verification/v1/verify",
    "congestion_query": "congestion-insights/v0/query",
    "congestion_subs": "congestion-insights/v0/subscriptions",
    "qod_sessions": "quality-on-demand/v1/sessions",
    "qod_retrieve": "quality-on-demand/v1/retrieve-sessions",
}
PASSTHROUGH_PREFIX = "passthrough/"  # bu path'ler Bearer token ister


@dataclass
class NacConfig:
    mode: str = "fixture"  # fixture | simulator | live
    base_url: str = "https://network-as-code.p-eu.rapidapi.com"
    rapidapi_key: str = ""
    rapidapi_host: str = "network-as-code.nokia.rapidapi.com"
    oauth_token: str = ""  # passthrough API'ler için Bearer (client-credentials ile alınır)
    timeout_s: float = 4.0
    retries: int = 2
    breaker_threshold: int = 3
    breaker_cooldown_s: float = 20.0
    fixture_path: str | None = None  # fixture modunda yüklenecek JSON

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
    request: dict = field(default_factory=dict)  # maskelenmiş istek özeti (explain için)

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
            self.fx = fixtures  # opsiyonel: HTTP çökerse yedek
            self.http = http or httpx.Client(base_url=self.cfg.base_url.rstrip("/") + "/", timeout=self.cfg.timeout_s)
        self.calls: list[NacResult] = []  # son çağrılar (debug paneli)

    # ---------------------------------------------------------------- çekirdek
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
            except httpx.TimeoutException:
                raise NacTimeout(api, self.cfg.timeout_s)
            except httpx.HTTPError as e:
                raise NacError("network", api, str(e), retryable=True)
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

    # ---------------------------------------------------------------- kimlik / dolandırıcılık
    def number_recycling(self, phone: str, specified_date: str) -> NacResult:
        p = normalize_phone(phone)
        return self._call("number-recycling", "POST", P["number_recycling"], json={"phoneNumber": p, "specifiedDate": specified_date}, phone=p,
                          fixture_fn=lambda: self.fx.number_recycling(p, specified_date))

    def call_forwardings(self, phone: str) -> NacResult:
        p = normalize_phone(phone)
        return self._call("call-forwarding-signal", "POST", P["call_forwardings"], json={"phoneNumber": p}, phone=p,
                          fixture_fn=lambda: self.fx.call_forwardings(p))

    def unconditional_call_forwarding(self, phone: str) -> NacResult:
        p = normalize_phone(phone)
        return self._call("call-forwarding-signal", "POST", P["unconditional_cf"], json={"phoneNumber": p}, phone=p,
                          fixture_fn=lambda: self.fx.unconditional_call_forwarding(p))

    def kyc_tenure(self, phone: str, tenure_date: str) -> NacResult:
        p = normalize_phone(phone)
        return self._call("kyc-tenure", "POST", P["kyc_tenure"], json={"phoneNumber": p, "tenureDate": tenure_date}, phone=p,
                          fixture_fn=lambda: self.fx.kyc_tenure(p, tenure_date))

    def kyc_age(self, phone: str, age_threshold: int, **fields) -> NacResult:
        p = normalize_phone(phone)
        body = {"phoneNumber": p, "ageThreshold": age_threshold, **{_camel(k): v for k, v in fields.items() if v is not None}}
        return self._call("kyc-age-verification", "POST", P["kyc_age"], json=body, phone=p,
                          fixture_fn=lambda: self.fx.kyc_age(p, age_threshold, **fields))

    def kyc_match(self, phone: str, **fields) -> NacResult:
        p = normalize_phone(phone)
        body = {"phoneNumber": p, **{_camel(k): v for k, v in fields.items() if v is not None}}
        return self._call("kyc-match", "POST", P["kyc_match"], json=body, phone=p,
                          fixture_fn=lambda: self.fx.kyc_match(p, **fields))

    def number_verify(self, phone: str) -> NacResult:
        """Canlıda: cihazın mobil veri üzerinden aldığı OIDC token ile çağrılır (3-legged). Prototipte SIMULATOR/fixture."""
        p = normalize_phone(phone)
        return self._call("number-verification", "POST", P["number_verify"], json={"phoneNumber": p}, phone=p,
                          fixture_fn=lambda: self.fx.number_verify(p))

    def sim_swap_check(self, phone: str, max_age_h: int | None = None) -> NacResult:
        p = normalize_phone(phone)
        body = {"phoneNumber": p, **({"maxAge": max_age_h} if max_age_h is not None else {})}
        return self._call("sim-swap", "POST", P["sim_swap_check"], json=body, phone=p, fixture_fn=lambda: self.fx.sim_swap_check(p, max_age_h))

    def sim_swap_date(self, phone: str) -> NacResult:
        p = normalize_phone(phone)
        return self._call("sim-swap", "POST", P["sim_swap_date"], json={"phoneNumber": p}, phone=p, fixture_fn=lambda: self.fx.sim_swap_date(p))

    def device_swap_check(self, phone: str, max_age_h: int | None = None) -> NacResult:
        p = normalize_phone(phone)
        body = {"phoneNumber": p, **({"maxAge": max_age_h} if max_age_h is not None else {})}
        return self._call("device-swap", "POST", P["device_swap_check"], json=body, phone=p, fixture_fn=lambda: self.fx.device_swap_check(p, max_age_h))

    def device_swap_date(self, phone: str) -> NacResult:
        p = normalize_phone(phone)
        return self._call("device-swap", "POST", P["device_swap_date"], json={"phoneNumber": p}, phone=p, fixture_fn=lambda: self.fx.device_swap_date(p))

    def consent(self, phone: str, scopes: list[str], purpose: str) -> NacResult:
        p = normalize_phone(phone)
        return self._call("consent-info", "POST", P["consent"], json={"phoneNumber": p, "scopes": scopes, "purpose": purpose}, phone=p,
                          fixture_fn=lambda: self.fx.consent(p, scopes, purpose))

    # ---------------------------------------------------------------- cihaz / konum
    def reachability(self, phone: str) -> NacResult:
        p = normalize_phone(phone)
        return self._call("device-reachability-status", "POST", P["reachability"], json={"device": {"phoneNumber": p}}, phone=p, fixture_fn=lambda: self.fx.reachability(p))

    def roaming(self, phone: str) -> NacResult:
        p = normalize_phone(phone)
        return self._call("device-roaming-status", "POST", P["roaming"], json={"device": {"phoneNumber": p}}, phone=p, fixture_fn=lambda: self.fx.roaming(p))

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

    # ---------------------------------------------------------------- abonelikler (webhook)
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

    # CAMARA geofencing: ABONELIK BASINA TEK OLAY TIPI. Nokia SDK bunu acikca zorluyor
    # (network_as_code/geofencing/raw_client.py) — iki tipi tek govdede gondermek 400 dondurur.
    # area-entered ve area-left icin AYRI abonelik acilir; bkz. geofence_subscribe_both().
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

    def reachability_subscribe(self, phone: str, sink: str, *, types: list[str] | None = None, sink_token: str | None = None, expire: datetime | None = None, initial_event: bool = True) -> NacResult:
        p = normalize_phone(phone)
        types = types or [
            "org.camaraproject.device-reachability-status-subscriptions.v0.reachability-data",
            "org.camaraproject.device-reachability-status-subscriptions.v0.reachability-disconnected",
        ]
        body = self._sub_body(p, sink, types, sink_token, expire, None, initial_event)
        return self._call("device-reachability-status-subscriptions", "POST", P["reachability_subs"], json=body, phone=p, fixture_fn=lambda: self.fx.create_subscription("reachability", body))

    def reachability_unsubscribe(self, subscription_id: str) -> NacResult:
        return self._call("device-reachability-status-subscriptions", "DELETE", f"{P['reachability_subs']}/{subscription_id}", fixture_fn=lambda: self.fx.delete_subscription(subscription_id))

    def roaming_subscribe(self, phone: str, sink: str, *, types: list[str] | None = None, sink_token: str | None = None, expire: datetime | None = None, initial_event: bool = True) -> NacResult:
        p = normalize_phone(phone)
        types = types or [
            "org.camaraproject.device-roaming-status-subscriptions.v0.roaming-on",
            "org.camaraproject.device-roaming-status-subscriptions.v0.roaming-off",
            "org.camaraproject.device-roaming-status-subscriptions.v0.roaming-change-country",
        ]
        body = self._sub_body(p, sink, types, sink_token, expire, None, initial_event)
        return self._call("device-roaming-status-subscriptions", "POST", P["roaming_subs"], json=body, phone=p, fixture_fn=lambda: self.fx.create_subscription("roaming", body))

    def roaming_unsubscribe(self, subscription_id: str) -> NacResult:
        return self._call("device-roaming-status-subscriptions", "DELETE", f"{P['roaming_subs']}/{subscription_id}", fixture_fn=lambda: self.fx.delete_subscription(subscription_id))

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


# ---------------------------------------------------------------- yardımcılar
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
