"""HeatShield API testleri — TestClient, NAC_MODE=fixture (HTTP yok)."""
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("NAC_MODE", "fixture")
os.environ["WEBHOOK_TOKEN"] = "test-token"

import api.main as m  # noqa: E402

AUTH = {"Authorization": "Bearer test-token"}
UTC = timezone.utc
T0 = datetime(2026, 8, 17, 3, 0, tzinfo=UTC)     # 06:00 Doha
NOON = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)   # 12:00 Doha
GEO_ENTERED = "org.camaraproject.geofencing-subscriptions.v0.area-entered"
GEO_LEFT = "org.camaraproject.geofencing-subscriptions.v0.area-left"


@pytest.fixture
def client():
    m.store.reset()
    m.WEBHOOK_TOKEN = "test-token"
    m.nac.breaker._states.clear()
    for _, phone, *_ in m.NAMED:
        m.nac.fx.update_profile(phone, {"fail": None})
    return TestClient(m.app)


def mk_site(client, jur="QA"):
    r = client.post("/v1/sites", json={"name": "Lusail", "jurisdiction": jur, "lat": 25.38, "lng": 51.49, "radius_m": 500})
    assert r.status_code == 201, r.text
    return r.json()


def add(client, site_id, wid, phone, **kw):
    r = client.post(f"/v1/sites/{site_id}/workers", json={"worker_id": wid, "phone": phone, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def ce(sub_id, etype, phone, at=T0, ce_id="ce-1"):
    return {"id": ce_id, "source": "test", "specversion": "1.0", "type": etype, "time": at.isoformat(),
            "data": {"subscriptionId": sub_id, "device": {"phoneNumber": phone}}}


# ------------------------------------------------------------------ kurulum
def test_site_and_worker_registration_subscribes_geofence(client):
    s = mk_site(client)
    assert s["area"]["radius_m"] == 500 and s["jurisdiction"]["code"] == "QA"
    w = add(client, s["site_id"], "W-001", "+99999910001", name="Rajan")
    assert w["phone_masked"] == "+99999***0001" and "+99999910001" not in str(w)
    assert len(w["phone_hash"]) == 64
    calls = client.get("/v1/_debug/calls").json()
    sub = [c for c in calls if c["api"] == "geofencing-subscriptions"][-1]
    assert sub["request"]["config"]["subscriptionDetail"]["device"]["phoneNumber"] == "+99999***0001"
    assert sub["request"]["sink"].endswith("/webhooks/geofence")
    # Kimlik bilgisi debug panelinde ASLA duz metin gorunmemeli: bir gizlilik denetimi bu token'i
    # yanitta 106 kez duz metin bulmus ve saldiri zincirini dogrulamisti (token hasat -> sahte olay).
    assert sub["request"]["sinkCredential"] == "***redacted***"
    assert "test-token" not in client.get("/v1/_debug/calls").text
    assert sub["request"]["config"]["subscriptionDetail"]["area"]["radius"] == 500
    # CAMARA abonelik başına TEK olay tipi ister → işçi başına İKİ abonelik (entered + left)
    types = [c["request"]["types"] for c in calls if c["api"] == "geofencing-subscriptions"]
    assert types == [[GEO_ENTERED], [GEO_LEFT]], "iki tipi tek gövdede göndermek canlıda 400 döndürür"
    st = client.get("/v1/state").json()
    assert len(st["subscriptions"]) == 2
    # ESKIDEN burada `location_history_size == 0` vardi ve bu YANLIS bir iddiaydi: defter
    # isci bazinda zaman damgali bir varlik kaydi tutuyor. Artik gizlemek yerine SAYIYORUZ.
    assert "presence_record" in st
    # debug paneli jüri ekranında açık kalır: YANIT gövdesindeki numara da maskeli olmalı
    assert "+99999910001" not in client.get("/v1/_debug/calls").text
    assert sub["response"]["config"]["subscriptionDetail"]["device"]["phoneNumber"] == "+99999***0001"


def test_small_perimeter_is_raised_to_minimum(client):
    s = client.post("/v1/sites", json={"name": "X", "radius_m": 50}).json()
    assert s["area"]["radius_m"] == 500


def test_invalid_phone_rejected(client):
    s = mk_site(client)
    r = client.post(f"/v1/sites/{s['site_id']}/workers", json={"worker_id": "W-x", "phone": "abc"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "INVALID_ARGUMENT"


# ------------------------------------------------------------------ webhook (ücretsiz sinyal)
def test_geofence_webhook_requires_token_and_costs_nothing(client):
    s = mk_site(client)
    add(client, s["site_id"], "W-001", "+99999910001")
    sub = client.get(f"/v1/sites/{s['site_id']}").json()["subscriptions"]["W-001"]
    body = ce(sub, GEO_ENTERED, "+99999910001")
    assert client.post("/webhooks/geofence", json=body).status_code == 401
    r = client.post("/webhooks/geofence", json=body, headers=AUTH).json()
    assert r["kind"] == "enter" and r["cost"] == 0
    assert client.get(f"/v1/sites/{s['site_id']}").json()["queries_total"] == 0
    assert client.post("/webhooks/geofence", json=body, headers=AUTH).json()["duplicate"] is True


def test_exit_event_clears_worker(client):
    s = mk_site(client)
    add(client, s["site_id"], "W-001", "+99999910001")
    sub = client.get(f"/v1/sites/{s['site_id']}").json()["subscriptions"]["W-001"]
    client.post("/webhooks/geofence", json=ce(sub, GEO_ENTERED, "+99999910001", ce_id="a"), headers=AUTH)
    client.post("/webhooks/geofence", json=ce(sub, GEO_LEFT, "+99999910001", ce_id="b"), headers=AUTH)
    w = client.get(f"/v1/sites/{s['site_id']}").json()["workers"][0]
    assert w["inside"] is False and w["cleared"] is True


def test_unknown_subscription_404_and_bad_type_400(client):
    s = mk_site(client)
    add(client, s["site_id"], "W-001", "+99999910001")
    assert client.post("/webhooks/geofence", json=ce("nope", GEO_ENTERED, "+99900000000"), headers=AUTH).status_code == 404
    sub = client.get(f"/v1/sites/{s['site_id']}").json()["subscriptions"]["W-001"]
    assert client.post("/webhooks/geofence", json=ce(sub, "org.example.x", "+99999910001", ce_id="z"), headers=AUTH).status_code == 400


# ------------------------------------------------------------------ tarama
def test_sweep_below_threshold_makes_no_calls(client):
    s = mk_site(client)
    add(client, s["site_id"], "W-001", "+99999910001")
    r = client.post(f"/v1/sites/{s['site_id']}/sweep", json={"now": T0.isoformat(), "wbgt_c": 30.0}).json()
    assert r["breached"] is False and r["budget"]["spent"] == 0 and r["calls"] == []


def test_sweep_respects_cadence_when_not_forced(client):
    s = mk_site(client)
    add(client, s["site_id"], "W-001", "+99999910001")
    client.post(f"/v1/sites/{s['site_id']}/sweep", json={"now": NOON.isoformat(), "wbgt_c": 35.0})
    r = client.post(f"/v1/sites/{s['site_id']}/sweep",
                    json={"now": (NOON + timedelta(minutes=1)).isoformat(), "force": False}).json()
    assert r["skipped"] is True


def test_wbgt_endpoint_writes_to_ledger(client):
    s = mk_site(client)
    r = client.post(f"/v1/sites/{s['site_id']}/wbgt", json={"wbgt_c": 34.2, "now": NOON.isoformat()}).json()
    assert r["wbgt_c"] == 34.2 and r["limit_c"] == 32.1
    led = client.get(f"/v1/sites/{s['site_id']}/ledger?event=wbgt_reading").json()
    assert led["count"] == 1 and "34.2" in led["items"][0]["detail"]


def test_ledger_filters_by_worker(client):
    d = client.post("/v1/demo/collapse", json={}).json()
    sid = d["site"]["site_id"]
    led = client.get(f"/v1/sites/{sid}/ledger?worker_id=W-002").json()
    assert led["count"] and all(e["worker_id"] == "W-002" for e in led["items"])
    assert any(e["event"] == "distress_verdict" for e in led["items"])


def test_jurisdictions_endpoint(client):
    j = client.get("/v1/jurisdictions").json()
    assert set(j) == {"QA", "SA", "AE"} and j["SA"]["summer_ban"]["from"] == "12:00"


def test_state_and_reset(client):
    mk_site(client)
    assert len(client.get("/v1/state").json()["sites"]) == 1
    # Kanit defterini silmek kimlik dogrulamasi ister — uyum kaydi urunun kendisi.
    assert client.post("/v1/reset").status_code == 401
    assert client.post("/v1/reset", headers=AUTH).json()["ok"] is True
    assert client.get("/v1/state").json()["sites"] == []
    assert client.get("/v1/sites/yok").status_code == 404


# ------------------------------------------------------------------ demolar
def test_demo_no_breach_spends_nothing(client):
    d = client.post("/v1/demo/no-breach", json={}).json()
    assert d["totals"]["queries"] == 0
    assert all(s["breached"] is False and s["calls"] == [] for s in d["sweeps"])


def test_demo_heat_day_full_cycle(client):
    d = client.post("/v1/demo/heat-day", json={}).json()
    sweeps = d["sweeps"]
    assert [s["breached"] for s in sweeps] == [False, True, True, True, True, False]
    assert [s["level"] for s in sweeps][1] == "marginal" and sweeps[3]["level"] == "severe"
    assert sweeps[1]["sweep_interval_min"] == 10 and sweeps[3]["sweep_interval_min"] == 2
    assert sweeps[1]["budget"]["skipped_for_budget"] > 0          # bütçe listeyi kesti
    assert d["totals"]["queries"] < d["totals"]["queries_if_polled_everyone"]
    assert d["totals"]["escalations"] == 1
    assert sweeps[-1]["state"] == "passive_watch" and sweeps[-1]["calls"] == []
    assert d["coverage"]["missing"] == ["W-012"]                  # rozetli ama şebekede görünmeyen
    assert "+99999910002" not in str(d)


def test_demo_api_down_marks_signals_unknown_not_safe(client):
    d = client.post("/v1/demo/api-down", json={}).json()
    assert any(x["kind"] in ("server", "circuit_open") for x in d["degraded"])
    calls = d["sweeps"][0]["calls"]
    assert calls and all(str(c["source"]).startswith("error(") for c in calls)
    assert all(c["result"] is None for c in calls)
    # hiç kimse 'doğrulandı' ya da 'temizlendi' sayılmadı — kuyrukta kaldılar
    assert all(w["verified_inside"] is None for w in d["site"]["workers"] if w["inside"])
    assert not [w for w in d["site"]["workers"] if w["cleared"]]
    assert len(d["unknown_workers"]) >= 10 and d["breaker"]["location-verification"]["open"] is True


def test_demo_collapse_separates_four_causes(client):
    d = client.post("/v1/demo/collapse", json={}).json()
    matrix = {v["worker_id"]: v["verdict"] for v in d["verdict_matrix"]}
    assert matrix["W-002"] == "probable_collapse"
    assert matrix["W-003"] == matrix["W-004"] == "probable_network"
    assert matrix["W-005"] == "probable_battery"
    assert matrix["W-006"] == "probable_left"
    assert d["totals"]["escalations"] == 1                        # yalnızca biri insana gitti
    assert "W-002" in d["site"]["qod_sessions"]
    w2 = next(w for w in d["site"]["workers"] if w["worker_id"] == "W-002")
    assert w2["state"] == "distress" and w2["last_location_masked"]["purpose"].startswith("where the medic")
    collapse = next(v for v in d["verdict_matrix"] if v["verdict"] == "probable_collapse")
    assert {e["signal"] for e in collapse["explain"]} >= {"cluster_test", "congestion", "device_history", "trajectory"}


def test_demo_jurisdiction_same_instant_different_law(client):
    d = client.post("/v1/demo/jurisdiction", json={}).json()
    by = {r["code"]: r for r in d["results"]}
    assert by["QA"]["breached"] is True and by["SA"]["breached"] is False and by["AE"]["breached"] is True
    assert by["AE"]["local_time"] == "12:45" and by["QA"]["local_time"] == "11:45"
    assert by["SA"]["jurisdiction"]["legal_ref"]


def test_health_and_demo_page(client):
    assert client.get("/health").json()["ok"] is True
    r = client.get("/demo")
    assert r.status_code == 200 and "Verification budget" in r.text
    assert client.get("/").json()["scenarios"][0] == "heat-day"


# ------------------------------------------------------------------ zamanlayıcı (ajanın kendi saati)
# Deck'in iddiası: "The agent decides. Nobody presses a button." Aşağıdaki testler o cümleyi
# doğrular: kapalıyken hiçbir şey olmaz, açıkken kadansa uyulur, ve otomatik tarama defterde
# dışarıdan tetiklenenden ayırt edilebilir.
def test_scheduler_is_off_by_default_in_fixture_mode(client, monkeypatch):
    monkeypatch.delenv("HS_SCHEDULER", raising=False)
    s = mk_site(client)
    add(client, s["site_id"], "W-001", "+99999910001")
    client.post(f"/v1/sites/{s['site_id']}/wbgt", json={"wbgt_c": 35.5})
    with TestClient(m.app) as c:                       # lifespan çalışır → zamanlayıcı karar verir
        st = c.get("/v1/scheduler").json()
        assert st["enabled"] is False and st["running"] is False
        time.sleep(0.25)
        assert m.store.sites[s["site_id"]].sweeps == 0      # kimse taramadı: düğmeye basan yok, saat de kapalı
        assert c.get("/v1/state").json()["scheduler"]["enabled"] is False


def test_scheduler_sweeps_on_its_own_when_enabled(client, monkeypatch):
    monkeypatch.setenv("HS_SCHEDULER", "1")
    monkeypatch.setenv("HS_SCHEDULER_TICK_S", "0.05")
    with TestClient(m.app) as c:
        assert c.get("/v1/scheduler").json()["enabled"] is True
        s = mk_site(c)
        add(c, s["site_id"], "W-001", "+99999910001")
        c.post(f"/v1/sites/{s['site_id']}/wbgt", json={"wbgt_c": 35.5})
        deadline = time.time() + 10
        while time.time() < deadline and m.store.sites[s["site_id"]].sweeps == 0:
            time.sleep(0.05)
        assert m.store.sites[s["site_id"]].sweeps >= 1, "zamanlayıcı hiç tarama yapmadı"
        st = c.get("/v1/scheduler").json()
        assert st["sweeps_run"] >= 1 and st["ticks"] >= 1 and st["last_tick_at"] and st["errors"] == 0
        assert any(e["event"] == "sweep_scheduled" for e in c.get(f"/v1/sites/{s['site_id']}/ledger").json()["items"])
    assert m.scheduler.to_dict()["running"] is False   # kapanışta görev iptal edildi, asılı kalmadı


def test_scheduler_respects_the_sweep_cadence(client):
    s = mk_site(client)
    add(client, s["site_id"], "W-001", "+99999910001")
    client.post(f"/v1/sites/{s['site_id']}/wbgt", json={"wbgt_c": 33.0, "now": NOON.isoformat()})
    site = m.store.sites[s["site_id"]]
    assert m.scheduler.tick(NOON)                                  # hiç taranmamış → hemen
    assert site.sweeps == 1
    assert m.scheduler.tick(NOON + timedelta(minutes=1)) == []     # kadans dolmadı
    assert site.sweeps == 1
    assert m.scheduler.tick(NOON + timedelta(minutes=11))          # kadans doldu
    assert site.sweeps == 2


def test_scheduler_skips_sites_without_a_wbgt_reading(client):
    s = mk_site(client)
    add(client, s["site_id"], "W-001", "+99999910001")
    assert m.scheduler.tick(NOON) == []                            # ölçüm yok → karar da yok
    assert m.store.sites[s["site_id"]].sweeps == 0 and m.store.sites[s["site_id"]].queries_total == 0


def test_automatic_sweeps_are_distinguishable_in_the_ledger(client):
    s = mk_site(client)
    sid = s["site_id"]
    add(client, sid, "W-001", "+99999910001")
    client.post(f"/v1/sites/{sid}/wbgt", json={"wbgt_c": 35.5, "now": NOON.isoformat()})
    r = client.post(f"/v1/sites/{sid}/sweep", json={"now": NOON.isoformat(), "wbgt_c": 35.5}).json()
    assert r["trigger"] == "api"
    m.scheduler.tick(NOON + timedelta(minutes=30))
    items = client.get(f"/v1/sites/{sid}/ledger").json()["items"]
    auto = [e for e in items if e.get("trigger") == "scheduler"]
    manual = [e for e in items if e.get("trigger") == "api"]
    assert auto and manual
    marker = [e for e in auto if e["event"] == "sweep_scheduled"]
    assert len(marker) == 1 and marker[0]["source"] == "scheduler" and "Nobody pressed a button" in marker[0]["detail"]
    assert not [e for e in manual if e["event"] == "sweep_scheduled"]


def test_scheduler_survives_a_failing_sweep(client, monkeypatch):
    s = mk_site(client)
    client.post(f"/v1/sites/{s['site_id']}/wbgt", json={"wbgt_c": 35.5, "now": NOON.isoformat()})

    def boom(*a, **kw):
        raise RuntimeError("nac exploded")

    monkeypatch.setattr(m, "step", boom)
    before = m.scheduler.errors
    assert m.scheduler.tick(NOON) == []                            # patlar ama görev ölmez
    assert m.scheduler.errors == before + 1 and "nac exploded" in m.scheduler.last_error


# ------------------------------------------------------------------ maliyet: kaynaksız rakam yok
def test_no_dollar_figure_is_invented(client):
    d = client.post("/v1/demo/collapse", json={}).json()
    cost = d["totals"]["cost"]
    assert cost["priced"] is False and cost["estimated_cost_usd"] is None and cost["unit_cost_usd"] is None
    assert "no unit price" in cost["basis"].lower()
    assert "estimated_cost_usd" not in d["totals"]                 # eski `queries * 0.02` kalktı


def test_unit_cost_only_when_configured_and_labelled_illustrative(client, monkeypatch):
    monkeypatch.setenv("HS_QUERY_UNIT_COST_USD", "0.05")
    d = client.post("/v1/demo/collapse", json={}).json()
    cost = d["totals"]["cost"]
    assert cost["priced"] is True and cost["unit_cost_usd"] == 0.05
    assert cost["estimated_cost_usd"] == round(d["totals"]["queries"] * 0.05, 2)
    assert "illustrative" in cost["basis"].lower() and "not a nokia price" in cost["basis"].lower()


def test_naive_baseline_is_counted_not_assumed(client):
    d = client.post("/v1/demo/heat-day", json={}).json()
    nb = d["totals"]["naive_baseline"]
    breached = [s for s in d["sweeps"] if s["breached"]]
    expected = [len([w for w in s["workers"] if w["badge_in"] and not w["exited_at"]]) for s in breached]
    assert nb["per_breached_sweep"] == expected and nb["queries"] == sum(expected) == d["totals"]["queries_if_polled_everyone"]
    # elmayla elma: taban çizgisi doğrulama basamaklarını sayar, biz de doğrulama basamaklarını
    assert nb["verification_queries_made"] < nb["queries"]
    assert nb["verification_queries_made"] + nb["escalation_queries_made"] == sum(len(s["calls"]) for s in d["sweeps"])
