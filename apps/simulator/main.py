"""Yerel Nokia NaC simülatörü.

Gerçek path'leri (docs/nac-api-reference.md) aynı gövde/yanıt şemasıyla sunar; veriyi FixtureBackend üretir.
NAC_MODE=simulator iken NacClient buraya (http://127.0.0.1:8081) bağlanır → HTTP katmanı uçtan uca test edilir.

Ek kontrol uçları (gerçek API'de YOK, sadece demo):
  GET  /_sim/profiles                 tüm profiller
  PUT  /_sim/profiles/{phone}         profil yaz/güncelle (patch)
  POST /_sim/emit                     {subscription_id | phone, type, extra} → sink'e CloudEvent POST'lar
  POST /_sim/reset                    fixture dosyasını yeniden yükle
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from nac_client.errors import NacError  # noqa: E402
from nac_client.fixtures import FixtureBackend  # noqa: E402

FIXTURE_PATH = os.environ.get("NAC_FIXTURE_PATH", str(ROOT / "fixtures" / "profiles.json"))

app = FastAPI(title="Local NaC Simulator", version="0.1")
fx = FixtureBackend.from_json(FIXTURE_PATH) if Path(FIXTURE_PATH).exists() else FixtureBackend()


@app.exception_handler(NacError)
def _nac_err(_: Request, e: NacError):
    return JSONResponse(status_code=e.status or 500, content={"status": e.status or 500, "code": e.kind.upper(), "message": str(e)})


def _phone(body: dict) -> str:
    p = body.get("phoneNumber") or (body.get("device") or {}).get("phoneNumber")
    if not p:
        raise HTTPException(400, {"code": "INVALID_ARGUMENT", "message": "phoneNumber gerekli"})
    fail = fx.get(p).get("fail")
    if fail == "timeout":
        import time
        time.sleep(30)
    if isinstance(fail, int):
        raise HTTPException(fail, {"code": "INJECTED", "message": "simülatör: enjekte edilmiş hata"})
    lat = fx.get(p).get("latency_ms", 0)
    if lat:
        import time
        time.sleep(lat / 1000)
    return p


def _require_bearer(request: Request):
    # Passthrough API'ler canlıda Bearer ister; simülatörde sadece uyarı üretiriz (SIM_STRICT_AUTH=1 ile zorunlu).
    if os.environ.get("SIM_STRICT_AUTH") == "1" and not request.headers.get("authorization", "").startswith("Bearer "):
        raise HTTPException(401, {"code": "UNAUTHENTICATED", "message": "Bearer token gerekli"})


PT = "/passthrough/camara/v1"


@app.post(f"{PT}/number-recycling/number-recycling/v0.2/check")
def number_recycling(request: Request, body: dict = Body(...)):
    _require_bearer(request)
    return fx.number_recycling(_phone(body), body.get("specifiedDate"))


@app.post(f"{PT}/call-forwarding-signal/call-forwarding-signal/v0.3/call-forwardings")
def call_forwardings(request: Request, body: dict = Body(...)):
    _require_bearer(request)
    return fx.call_forwardings(_phone(body))


@app.post(f"{PT}/call-forwarding-signal/call-forwarding-signal/v0.3/unconditional-call-forwardings")
def unconditional_cf(request: Request, body: dict = Body(...)):
    _require_bearer(request)
    return fx.unconditional_call_forwarding(_phone(body))


@app.post(f"{PT}/kyc-tenure/kyc-tenure/v0.1/check-tenure")
def kyc_tenure(request: Request, body: dict = Body(...)):
    _require_bearer(request)
    return fx.kyc_tenure(_phone(body), body.get("tenureDate"))


def _snake_fields(body: dict, exclude=("phoneNumber", "ageThreshold", "includeContentLock", "includeParentalControl")) -> dict:
    import re
    return {re.sub(r"(?<!^)(?=[A-Z])", "_", k).lower(): v for k, v in body.items() if k not in exclude}


@app.post(f"{PT}/kyc-age-verification/kyc-age-verification/v0.1/verify")
def kyc_age(request: Request, body: dict = Body(...)):
    _require_bearer(request)
    return fx.kyc_age(_phone(body), int(body.get("ageThreshold", 18)), **_snake_fields(body))


@app.post(f"{PT}/kyc-match/kyc-match/v0.3/match")
def kyc_match(request: Request, body: dict = Body(...)):
    _require_bearer(request)
    return fx.kyc_match(_phone(body), **_snake_fields(body))


@app.post(f"{PT}/number-verification/number-verification/v2/verify")
def number_verify(request: Request, body: dict = Body(...)):
    _require_bearer(request)
    return fx.number_verify(_phone(body))


@app.post(f"{PT}/sim-swap/sim-swap/v0/check")
def sim_swap_check(request: Request, body: dict = Body(...)):
    _require_bearer(request)
    return fx.sim_swap_check(_phone(body), body.get("maxAge"))


@app.post(f"{PT}/sim-swap/sim-swap/v0/retrieve-date")
def sim_swap_date(request: Request, body: dict = Body(...)):
    _require_bearer(request)
    return fx.sim_swap_date(_phone(body))


@app.post(f"{PT}/device-swap/device-swap/v1/check")
def device_swap_check(request: Request, body: dict = Body(...)):
    _require_bearer(request)
    return fx.device_swap_check(_phone(body), body.get("maxAge"))


@app.post(f"{PT}/device-swap/device-swap/v1/retrieve-date")
def device_swap_date(request: Request, body: dict = Body(...)):
    _require_bearer(request)
    return fx.device_swap_date(_phone(body))


@app.post(f"{PT}/consent-info/consent-info/v0.1/retrieve")
def consent(request: Request, body: dict = Body(...)):
    _require_bearer(request)
    return fx.consent(_phone(body), body.get("scopes") or [], body.get("purpose", ""))


@app.post("/device-status/device-reachability-status/v1/retrieve")
def reachability(body: dict = Body(...)):
    return fx.reachability(_phone(body))


@app.post("/device-status/device-roaming-status/v1/retrieve")
def roaming(body: dict = Body(...)):
    return fx.roaming(_phone(body))


@app.post("/location-retrieval/v0/retrieve")
def location_retrieve(body: dict = Body(...)):
    return fx.location_retrieve(_phone(body), body.get("maxAge"))


@app.post("/location-verification/v1/verify")
def location_verify(body: dict = Body(...)):
    area = body.get("area") or {}
    c = area.get("center") or {}
    return fx.location_verify(_phone(body), float(c.get("latitude")), float(c.get("longitude")), float(area.get("radius", 500)), body.get("maxAge"))


@app.post("/congestion-insights/v0/query")
def congestion_query(body: dict = Body(...)):
    return fx.congestion_query(_phone(body), body.get("start"), body.get("end"))


# ---- abonelikler -------------------------------------------------------------
def _sub_routes(prefix: str, kind: str):
    @app.post(prefix, name=f"create_{kind}")
    def create(body: dict = Body(...)):
        return fx.create_subscription(kind, body)

    @app.get(prefix, name=f"list_{kind}")
    def list_():
        return fx.list_subscriptions(kind)

    @app.get(prefix + "/{sid}", name=f"get_{kind}")
    def get(sid: str):
        return fx.get_subscription(sid)

    @app.delete(prefix + "/{sid}", name=f"delete_{kind}")
    def delete(sid: str):
        return fx.delete_subscription(sid)


_sub_routes("/geofencing-subscriptions/v0.3/subscriptions", "geofencing")
_sub_routes("/device-status/device-reachability-status-subscriptions/v0.8/subscriptions", "reachability")
_sub_routes("/device-status/device-roaming-status-subscriptions/v0.8/subscriptions", "roaming")


# ---- QoD ---------------------------------------------------------------------
@app.post("/quality-on-demand/v1/sessions")
def qod_create(body: dict = Body(...)):
    return fx.qod_create(_phone(body), body)


@app.get("/quality-on-demand/v1/sessions/{sid}")
def qod_get(sid: str):
    return fx.qod_get(sid)


@app.delete("/quality-on-demand/v1/sessions/{sid}")
def qod_delete(sid: str):
    return fx.qod_delete(sid)


@app.post("/quality-on-demand/v1/retrieve-sessions")
def qod_list(body: dict = Body(...)):
    return fx.qod_list(_phone(body))


# ---- simülatör kontrol uçları (gerçek API'de yok) ------------------------------
@app.get("/_sim/profiles")
def sim_profiles():
    return fx.profiles


@app.put("/_sim/profiles/{phone}")
def sim_put_profile(phone: str, patch: dict = Body(...)):
    return fx.update_profile(phone, patch)


@app.post("/_sim/reset")
def sim_reset():
    global fx
    fx = FixtureBackend.from_json(FIXTURE_PATH) if Path(FIXTURE_PATH).exists() else FixtureBackend()
    return {"ok": True, "profiles": len(fx.profiles)}


@app.post("/_sim/emit")
def sim_emit(body: dict = Body(...)):
    """Bir aboneliğin sink'ine CloudEvent gönderir. body: {subscription_id?, phone?, type, extra?}"""
    sid = body.get("subscription_id")
    subs = [fx.subscriptions[sid]] if sid and sid in fx.subscriptions else [
        s for s in fx.subscriptions.values() if (s.get("config") or {}).get("subscriptionDetail", {}).get("device", {}).get("phoneNumber") == body.get("phone")
    ]
    if not subs:
        raise HTTPException(404, "abonelik bulunamadı")
    delivered = []
    for s in subs:
        phone = (s.get("config") or {}).get("subscriptionDetail", {}).get("device", {}).get("phoneNumber")
        ev = fx.cloud_event(body["type"], s["id"], phone, body.get("extra"))
        headers = {"content-type": "application/cloudevents+json"}
        try:
            r = httpx.post(s["sink"], json=ev, headers=headers, timeout=5)
            delivered.append({"subscription_id": s["id"], "status": r.status_code})
        except Exception as e:  # noqa: BLE001
            delivered.append({"subscription_id": s["id"], "error": str(e)})
    return {"delivered": delivered}


@app.get("/")
def root():
    return {"simulator": "local-nac", "profiles": len(fx.profiles), "fixture": FIXTURE_PATH}
