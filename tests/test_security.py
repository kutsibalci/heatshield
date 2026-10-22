"""Security foundation (phase 3): authentication, demo mode, input limits, default secrets, headers."""
import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

import api.main as m
from api import security
from nac_client.privacy import hash_phone

API = {"Authorization": "Bearer test-api-token"}
HOOK = {"Authorization": "Bearer test-token"}


@pytest.fixture
def anon(monkeypatch):
    """Unauthenticated client. Demo mode is ON by default (conftest); tests turn it off when needed."""
    m.store.reset()
    monkeypatch.setattr(security, "API_TOKEN", "test-api-token")
    monkeypatch.setattr(security, "WEBHOOK_TOKEN", "test-token")
    m.DEMO_LIMITER.reset()
    return TestClient(m.app)


def _site(c):
    r = c.post("/v1/sites", json={"name": "X"}, headers=API)
    assert r.status_code == 201, r.text
    return r.json()["site_id"]


# ------------------------------------------------------------------ write endpoints
@pytest.mark.parametrize("path,body", [
    ("/v1/sites", {"name": "X"}),
    ("/v1/sites/{sid}/workers", {"worker_id": "W-1", "phone": "+99999910001"}),
    ("/v1/sites/{sid}/wbgt", {"wbgt_c": 35.0}),
    ("/v1/sites/{sid}/sweep", {}),
    ("/v1/reset", None),
])
def test_every_write_needs_the_api_token(anon, path, body):
    sid = _site(anon)
    url = path.format(sid=sid)
    assert anon.post(url, json=body).status_code == 401
    assert anon.post(url, json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert anon.post(url, json=body, headers=HOOK).status_code == 401      # the webhook key is not an admin
    assert anon.post(url, json=body, headers=API).status_code in (200, 201)


# ------------------------------------------------------------------ read endpoints (personal data)
PII_READS = ["/v1/state", "/v1/sites", "/v1/sites/{sid}", "/v1/sites/{sid}/ledger", "/v1/_debug/calls", "/v1/_debug/profiles"]


@pytest.mark.parametrize("path", PII_READS)
def test_personal_data_needs_the_token_outside_demo_mode(anon, monkeypatch, path):
    sid = _site(anon)
    anon.post(f"/v1/sites/{sid}/workers", json={"worker_id": "W-1", "phone": "+99999910001", "name": "Jane Roe",
                                                "nationality": "NP", "subscribe": False}, headers=API)
    monkeypatch.delenv("HS_DEMO_MODE", raising=False)
    url = path.format(sid=sid)
    r = anon.get(url)
    assert r.status_code == 401 and "Jane Roe" not in r.text
    assert anon.get(url, headers=API).status_code == 200


@pytest.mark.parametrize("path", PII_READS)
def test_demo_mode_keeps_reads_open_because_the_data_is_fictional(anon, path):
    sid = _site(anon)
    assert anon.get(path.format(sid=sid)).status_code == 200


def test_the_console_endpoints_stay_public(anon, monkeypatch):
    monkeypatch.delenv("HS_DEMO_MODE", raising=False)
    for path in ("/health", "/v1/config", "/v1/scheduler", "/v1/jurisdictions", "/v1/about"):
        assert anon.get(path).status_code == 200, path


# ------------------------------------------------------------------ demo mode
def test_demo_endpoints_are_off_unless_demo_mode_is_on(anon, monkeypatch):
    monkeypatch.delenv("HS_DEMO_MODE", raising=False)
    sid = _site(anon)
    assert anon.post("/v1/demo/no-breach").status_code == 404
    assert sid in m.store.sites, "a disabled demo endpoint must not wipe the store"
    monkeypatch.setenv("HS_DEMO_MODE", "1")
    assert anon.post("/v1/demo/no-breach").status_code == 200


def test_demo_mode_refuses_to_run_against_the_live_operator(anon, monkeypatch):
    monkeypatch.setattr(m.nac.cfg, "mode", "live")
    r = anon.post("/v1/demo/no-breach")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "DEMO_NOT_ALLOWED_LIVE"


def test_demo_endpoints_are_rate_limited(anon, monkeypatch):
    monkeypatch.setattr(m.DEMO_LIMITER, "limit", 1)
    assert anon.post("/v1/demo/no-breach").status_code == 200
    r = anon.post("/v1/demo/no-breach")
    assert r.status_code == 429 and r.headers.get("retry-after")


# ------------------------------------------------------------------ input limits
@pytest.mark.parametrize("value", [-500, 99.0, float("nan")])
def test_implausible_wbgt_is_rejected(anon, value):
    sid = _site(anon)
    assert anon.post(f"/v1/sites/{sid}/wbgt", content=f'{{"wbgt_c": {value}}}'.replace("nan", "NaN"),
                     headers={**API, "content-type": "application/json"}).status_code == 422
    assert anon.post(f"/v1/sites/{sid}/sweep", content=f'{{"wbgt_c": {value}}}'.replace("nan", "NaN"),
                     headers={**API, "content-type": "application/json"}).status_code == 422


def test_worker_text_fields_are_bounded(anon):
    sid = _site(anon)
    r = anon.post(f"/v1/sites/{sid}/workers", json={"worker_id": "W-1", "phone": "+99999910001", "name": "x" * 10_000},
                  headers=API)
    assert r.status_code == 422


# ------------------------------------------------------------------ default secrets
def test_no_public_default_secrets(monkeypatch):
    monkeypatch.delenv("PHONE_HASH_SALT", raising=False)
    known = hmac.new(b"dev-salt-change-me", b"+99999910001", hashlib.sha256).hexdigest()
    assert hash_phone("+99999910001") != known
    assert hash_phone("+99999910001") == hash_phone("+99999910001")   # stable within the process
    from pathlib import Path
    for source in Path(m.__file__).parent.glob("*.py"):
        assert "heatshield-dev-token" not in source.read_text(encoding="utf-8"), source.name


def test_live_mode_refuses_to_start_without_its_secrets():
    missing = m.missing_production_secrets("live", {})
    assert set(missing) == {"HS_API_TOKEN", "WEBHOOK_TOKEN", "PHONE_HASH_SALT"}
    full = {"HS_API_TOKEN": "a", "WEBHOOK_TOKEN": "b", "PHONE_HASH_SALT": "c"}
    assert m.missing_production_secrets("live", full) == []
    assert m.missing_production_secrets("fixture", {}) == []


# ------------------------------------------------------------------ headers and providers
def test_security_headers_are_set(anon):
    r = anon.get("/demo")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "access-control-allow-origin" not in r.headers


def test_gemini_key_travels_in_a_header_not_the_url(monkeypatch):
    from agent import llm_adapter as LA
    seen = {}

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}

    def fake_post(url, **kw):
        seen.update(url=url, **kw)
        return R()

    monkeypatch.setattr(LA.httpx, "post", fake_post)
    LA.LLMPlanner("gemini", "secret-key", model="m")._call_gemini("{}")
    assert "secret-key" not in seen["url"] and "params" not in seen
    assert seen["headers"]["x-goog-api-key"] == "secret-key"


def test_simulator_only_delivers_to_local_sinks(monkeypatch):
    import simulator.main as sim
    c = TestClient(sim.app)
    delivered = []
    monkeypatch.setattr(sim.httpx, "post", lambda url, **kw: delivered.append(url))
    sub = {"id": "s1", "sink": "http://169.254.169.254/latest/meta-data",
           "config": {"subscriptionDetail": {"device": {"phoneNumber": "+99999910001"}}}}
    monkeypatch.setitem(sim.fx.subscriptions, "s1", sub)
    r = c.post("/_sim/emit", json={"subscription_id": "s1", "type": "org.camaraproject.geofencing-subscriptions.v0.area-left"})
    assert r.status_code == 400 and delivered == []


def test_simulator_sends_the_sink_credential_back_as_bearer(monkeypatch):
    """CAMARA: the notification arrives with the accessToken given in the subscription. The simulator was not
    sending it; our token-requiring webhook endpoint was rejecting simulator events with 401."""
    import simulator.main as sim
    c = TestClient(sim.app)
    sub = c.post("/geofencing-subscriptions/v0.3/subscriptions", json={
        "protocol": "HTTP", "sink": "http://127.0.0.1:8000/webhooks/geofence",
        "types": ["org.camaraproject.geofencing-subscriptions.v0.area-left"],
        "sinkCredential": {"credentialType": "ACCESSTOKEN", "accessToken": "sink-secret", "accessTokenType": "bearer"},
        "config": {"subscriptionDetail": {"device": {"phoneNumber": "+99999910001"},
                                          "area": {"areaType": "CIRCLE", "center": {"latitude": 25.0, "longitude": 51.0}, "radius": 500}}}})
    assert sub.status_code in (200, 201), sub.text
    assert "sink-secret" not in sub.text
    sent = {}
    monkeypatch.setattr(sim.httpx, "post", lambda url, **kw: sent.update(kw) or type("R", (), {"status_code": 204})())
    c.post("/_sim/emit", json={"subscription_id": sub.json()["id"], "type": "org.camaraproject.geofencing-subscriptions.v0.area-left"})
    assert sent["headers"]["authorization"] == "Bearer sink-secret"
