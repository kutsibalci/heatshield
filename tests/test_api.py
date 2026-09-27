"""HeatShield API tests — TestClient, NAC_MODE=fixture (no HTTP)."""
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("NAC_MODE", "fixture")
os.environ["WEBHOOK_TOKEN"] = "test-token"

import api.main as m  # noqa: E402
from api import demo, runtime, scheduler, security  # noqa: E402

AUTH = {"Authorization": "Bearer test-token"}          # webhook (operator notifications)
ADMIN = {"Authorization": "Bearer test-api-token"}     # admin: write endpoints and personal data
UTC = timezone.utc
T0 = datetime(2026, 8, 17, 3, 0, tzinfo=UTC)     # 06:00 Doha
NOON = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)   # 12:00 Doha
GEO_ENTERED = "org.camaraproject.geofencing-subscriptions.v0.area-entered"
GEO_LEFT = "org.camaraproject.geofencing-subscriptions.v0.area-left"


@pytest.fixture
def client():
    m.store.reset()
    security.WEBHOOK_TOKEN = "test-token"
    security.API_TOKEN = "test-api-token"
    m.DEMO_LIMITER.reset()
    m.nac.breaker._states.clear()
    for _, phone, *_ in m.NAMED:
        m.nac.fx.update_profile(phone, {"fail": None})
    return TestClient(m.app, headers=ADMIN)


def mk_site(client, jur="QA"):
    r = client.post("/v1/sites", json={"name": "Lusail", "jurisdiction": jur, "lat": 25.3917, "lng": 51.5299, "radius_m": 500})
    assert r.status_code == 201, r.text
    return r.json()


def add(client, site_id, wid, phone, **kw):
    r = client.post(f"/v1/sites/{site_id}/workers", json={"worker_id": wid, "phone": phone, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def ce(sub_id, etype, phone, at=T0, ce_id="ce-1"):
    return {"id": ce_id, "source": "test", "specversion": "1.0", "type": etype, "time": at.isoformat(),
            "data": {"subscriptionId": sub_id, "device": {"phoneNumber": phone}}}


# ------------------------------------------------------------------ setup
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
    # The credential must NEVER appear as plain text in the debug panel: a privacy audit found this token
    # in plain text 106 times in the response and confirmed the attack chain (token harvest -> fake event).
    assert sub["request"]["sinkCredential"] == "***redacted***"
    assert "test-token" not in client.get("/v1/_debug/calls").text
    assert sub["request"]["config"]["subscriptionDetail"]["area"]["radius"] == 500
    # CAMARA wants ONE event type per subscription → TWO subscriptions per worker (entered + left)
    types = [c["request"]["types"] for c in calls if c["api"] == "geofencing-subscriptions"]
    assert types == [[GEO_ENTERED], [GEO_LEFT]], "sending both types in one body returns 400 on the live platform"
    st = client.get("/v1/state").json()
    assert len(st["subscriptions"]) == 2
    # PREVIOUSLY there was `location_history_size == 0` here and that was a FALSE claim: the ledger
    # keeps a timestamped presence record per worker. Now, instead of hiding it, we COUNT it.
    assert "presence_record" in st
    # the debug panel stays open on the judges' screen: the number in the RESPONSE body must be masked too
    assert "+99999910001" not in client.get("/v1/_debug/calls").text
    assert sub["response"]["config"]["subscriptionDetail"]["device"]["phoneNumber"] == "+99999***0001"


def test_small_perimeter_is_raised_to_minimum(client):
    s = client.post("/v1/sites", json={"name": "X", "radius_m": 50}).json()
    assert s["area"]["radius_m"] == 500


def test_invalid_phone_rejected(client):
    s = mk_site(client)
    r = client.post(f"/v1/sites/{s['site_id']}/workers", json={"worker_id": "W-x", "phone": "abc"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "INVALID_ARGUMENT"


# ------------------------------------------------------------------ webhook (free signal)
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


# ------------------------------------------------------------------ sweep
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
    # Wiping the evidence ledger needs the admin token — the compliance record IS the product; the webhook key won't do.
    assert client.post("/v1/reset", headers={"Authorization": ""}).status_code == 401
    assert client.post("/v1/reset", headers=AUTH).status_code == 401
    assert client.post("/v1/reset").json()["ok"] is True
    assert client.get("/v1/state").json()["sites"] == []
    assert client.get("/v1/sites/missing").status_code == 404


# ------------------------------------------------------------------ demos
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
    assert sweeps[1]["budget"]["skipped_for_budget"] > 0          # the budget cut the list
    assert d["totals"]["queries"] < d["totals"]["queries_if_polled_everyone"]
    assert d["totals"]["escalations"] == 1
    assert sweeps[-1]["state"] == "passive_watch" and sweeps[-1]["calls"] == []
    assert d["coverage"]["missing"] == ["W-012"]                  # badged in but not visible on the network
    assert "+99999910002" not in str(d)


def test_demo_api_down_marks_signals_unknown_not_safe(client):
    d = client.post("/v1/demo/api-down", json={}).json()
    assert any(x["kind"] in ("server", "circuit_open") for x in d["degraded"])
    calls = d["sweeps"][0]["calls"]
    assert calls and all(str(c["source"]).startswith("error(") for c in calls)
    assert all(c["result"] is None for c in calls)
    # nobody was counted as 'verified' or 'cleared' — they stayed in the queue
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
    assert d["totals"]["escalations"] == 1                        # only one went to a human
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
    assert client.get("/v1/about").json()["scenarios"][0] == "heat-day"
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/demo"  # the bare address goes to the UI


# ------------------------------------------------------------------ scheduler (the agent's own clock)
# The deck's claim: "The agent decides. Nobody presses a button." The tests below verify that sentence:
# when off, nothing happens; when on, the cadence is respected; and an automatic sweep can be told apart
# in the ledger from an externally triggered one.
def test_scheduler_is_off_by_default_in_fixture_mode(client, monkeypatch):
    monkeypatch.delenv("HS_SCHEDULER", raising=False)
    s = mk_site(client)
    add(client, s["site_id"], "W-001", "+99999910001")
    client.post(f"/v1/sites/{s['site_id']}/wbgt", json={"wbgt_c": 35.5})
    with TestClient(m.app) as c:                       # lifespan runs → the scheduler decides
        st = c.get("/v1/scheduler").json()
        assert st["enabled"] is False and st["running"] is False
        time.sleep(0.25)
        assert m.store.sites[s["site_id"]].sweeps == 0      # nobody swept: no one pressed a button, and the clock is off
        assert c.get("/v1/state").json()["scheduler"]["enabled"] is False


def test_scheduler_sweeps_on_its_own_when_enabled(client, monkeypatch):
    monkeypatch.setenv("HS_SCHEDULER", "1")
    monkeypatch.setenv("HS_SCHEDULER_TICK_S", "0.05")
    with TestClient(m.app, headers=ADMIN) as c:
        assert c.get("/v1/scheduler").json()["enabled"] is True
        s = mk_site(c)
        add(c, s["site_id"], "W-001", "+99999910001")
        c.post(f"/v1/sites/{s['site_id']}/wbgt", json={"wbgt_c": 35.5})
        deadline = time.time() + 10
        while time.time() < deadline and m.store.sites[s["site_id"]].sweeps == 0:
            time.sleep(0.05)
        assert m.store.sites[s["site_id"]].sweeps >= 1, "the scheduler never ran a sweep"
        st = c.get("/v1/scheduler").json()
        assert st["sweeps_run"] >= 1 and st["ticks"] >= 1 and st["last_tick_at"] and st["errors"] == 0
        assert any(e["event"] == "sweep_scheduled" for e in c.get(f"/v1/sites/{s['site_id']}/ledger").json()["items"])
    assert m.scheduler.to_dict()["running"] is False   # the task was cancelled on shutdown, not left hanging


def test_scheduler_respects_the_sweep_cadence(client):
    s = mk_site(client)
    add(client, s["site_id"], "W-001", "+99999910001")
    client.post(f"/v1/sites/{s['site_id']}/wbgt", json={"wbgt_c": 33.0, "now": NOON.isoformat()})
    site = m.store.sites[s["site_id"]]
    assert m.scheduler.tick(NOON)                                  # never swept → right away
    assert site.sweeps == 1
    assert m.scheduler.tick(NOON + timedelta(minutes=1)) == []     # cadence not elapsed
    assert site.sweeps == 1
    assert m.scheduler.tick(NOON + timedelta(minutes=11))          # cadence elapsed
    assert site.sweeps == 2


def test_scheduler_skips_sites_without_a_wbgt_reading(client):
    s = mk_site(client)
    add(client, s["site_id"], "W-001", "+99999910001")
    assert m.scheduler.tick(NOON) == []                            # no reading → no decision either
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

    monkeypatch.setattr(scheduler, "step", boom)
    before = m.scheduler.errors
    assert m.scheduler.tick(NOON) == []                            # it blows up but the task does not die
    assert m.scheduler.errors == before + 1 and "nac exploded" in m.scheduler.last_error


# ------------------------------------------------------------------ cost: no unsourced figures
def test_no_dollar_figure_is_invented(client):
    d = client.post("/v1/demo/collapse", json={}).json()
    cost = d["totals"]["cost"]
    assert cost["priced"] is False and cost["estimated_cost_usd"] is None and cost["unit_cost_usd"] is None
    assert "no unit price" in cost["basis"].lower()
    assert "estimated_cost_usd" not in d["totals"]                 # the old `queries * 0.02` is gone


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
    # apples to apples: the baseline counts verification rungs, and so do we
    assert nb["verification_queries_made"] < nb["queries"]
    assert nb["verification_queries_made"] + nb["escalation_queries_made"] == sum(len(s["calls"]) for s in d["sweeps"])


# ------------------------------------------------------------------ shift roster (240 workers, Lusail)
# These tests lock in the contract of the demo DATA: the UI reads these fields, the scale claim rests on
# these numbers, and the phone numbers deliberately stay in the +99999 range.
WORKER_FIELDS = {"worker_id", "name", "nationality", "trade", "crew", "micro_zone", "zone_label",
                 "first_day_on_site", "prior_incident", "shift", "badge_in", "phone_masked", "phone_hash",
                 "inside", "cleared", "state", "verdict", "confidence", "score", "vulnerability",
                 "queries_used", "reachable", "reachable_via_operator", "presence_prior", "last_signal_at"}


def test_demo_site_is_a_240_worker_lusail_roster(client):
    d = client.post("/v1/demo/heat-day", json={}).json()
    ws = d["site"]["workers"]
    assert len(ws) == 240                                  # 13 named scenario workers + 227 crowd
    assert d["site"]["area"] == {"lat": 25.3917, "lng": 51.5299, "radius_m": 500}   # the real Lusail Marina
    for w in ws:
        assert WORKER_FIELDS <= set(w), f"{w['worker_id']} missing fields: {WORKER_FIELDS - set(w)}"
        assert w["name"] and w["nationality"] and w["trade"] and w["zone_label"]
        # numbers stay in the RESERVED +99999 range: no real Qatari number is used
        assert w["phone_masked"].startswith("+99999")
    assert {w["nationality"] for w in ws} >= {"Nepal", "India", "Bangladesh", "Pakistan",
                                              "Sri Lanka", "Philippines", "Kenya", "Uganda"}
    # the named scenario workers and their behaviours are in place
    named = {w["worker_id"]: w for w in ws}
    assert named["W-002"]["name"].startswith("Bikash") and named["W-001"]["name"].startswith("Rajan")
    assert named["W-008"]["prior_incident"] is True and named["W-009"]["shift"] == "night_to_day"


def test_budget_ceiling_is_felt_at_240_workers(client):
    d = client.post("/v1/demo/heat-day", json={}).json()
    sc = d["scale"]
    assert sc["roster"] == 240 and sc["over_ceiling"] is True
    assert sc["full_coverage_ceiling_workers"] == 110 and sc["usable_per_sweep"] == 18
    # in every breached sweep hundreds of workers REMAIN in line — the ceiling is not an abstract sentence
    assert len(sc["skipped_for_budget_per_sweep"]) == 4
    assert all(n > 150 for n in sc["skipped_for_budget_per_sweep"])
    assert sc["never_queried_in_this_breach"] > 100
    # and the queue depth matches the number of workers on site, not the budget
    assert all(depth > sc["usable_per_sweep"] for depth in sc["queue_depth_per_breached_sweep"])


def test_ban_hour_rotation_is_free_and_does_not_clear_anyone(client):
    d = client.post("/v1/demo/heat-day", json={}).json()
    r = d["ban_hour_rotation"]
    assert r["queries"] == 0 and r["free_events"] == 2 * r["returned_inside"] + r["welfare_compound"]
    entry = next(e for e in d["ledger"] if e["event"] == "ban_hour_rotation")
    assert entry["source"] == "geofencing-subscriptions" and entry["cost"] == 0
    # those who re-enter are NOT DROPPED FROM THE QUEUE: a free event declares nobody safe
    inside = [w for w in d["site"]["workers"] if w["inside"] and not w["cleared"]]
    assert len(inside) > r["returned_inside"]
    # those in the welfare compound are "presumed safe" → the reserve re-verifies them
    assert any(s["budget"]["reserve_rechecks"] > 0 for s in d["sweeps"] if s["breached"])


def test_zone_labels_are_real_names_but_the_perimeter_stays_single(client):
    d = client.post("/v1/demo/heat-day", json={}).json()
    labels = {z["label"] for z in d["zones"]["list"]}
    assert labels >= {"Marina District", "Energy City", "Fox Hills", "Al Erkyah"}
    assert "single" in d["zones"]["perimeter"]
    assert "1000 m" in d["zones"]["why_labels_only"]        # a sub-zone cannot be verified from the network
    # single perimeter: ONE site subscription per worker, NO subscription per sub-zone
    assert len(d["site"]["subscriptions"]) == len(d["site"]["workers"])
    assert d["site"]["area"]["radius_m"] == 500
    # the sub-zone totals add up to the shift roster — a label, not a separate perimeter
    assert sum(z["workers"] for z in d["zones"]["list"]) == len(d["site"]["workers"])


# ------------------------------------------------------------------ phase 1: correctness
def test_wbgt_posted_via_the_api_goes_stale(client):
    """A reading from the feed must carry a timestamp; otherwise sweeps keep running on a value from days ago."""
    s = mk_site(client)
    add(client, s["site_id"], "W-001", "+99999910001")
    client.post(f"/v1/sites/{s['site_id']}/wbgt", json={"wbgt_c": 35.0, "now": T0.isoformat()})
    r = client.post(f"/v1/sites/{s['site_id']}/sweep", json={"now": (T0 + timedelta(days=3)).isoformat()}).json()
    assert r["wbgt_stale"] is True and r["breached"] is False and r["calls"] == []


def test_new_sites_default_to_the_configured_jurisdiction(client, monkeypatch):
    monkeypatch.setattr(runtime, "CFG", m.Config.from_env("SA"))
    r = client.post("/v1/sites", json={"name": "Riyadh"})
    assert r.status_code == 201
    assert m.store.sites[r.json()["site_id"]].cfg.jurisdiction.code == "SA"


def test_duplicate_worker_id_is_rejected_not_overwritten(client):
    s = mk_site(client)
    add(client, s["site_id"], "W-001", "+99999910001")
    subs_before = len(m.store.subs)
    r = client.post(f"/v1/sites/{s['site_id']}/workers", json={"worker_id": "W-001", "phone": "+99999910002"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ALREADY_EXISTS"
    assert len(m.store.subs) == subs_before
    assert client.get(f"/v1/sites/{s['site_id']}").json()["workers"][0]["masked"].endswith("0001")


def test_webhook_events_without_an_id_are_still_deduplicated(client):
    s = mk_site(client)
    add(client, s["site_id"], "W-001", "+99999910001")
    sub = client.get(f"/v1/sites/{s['site_id']}").json()["subscriptions"]["W-001"]
    body = ce(sub, GEO_ENTERED, "+99999910001")
    body.pop("id")
    assert client.post("/webhooks/geofence", json=body, headers=AUTH).json().get("duplicate") is None
    assert client.post("/webhooks/geofence", json=body, headers=AUTH).json()["duplicate"] is True


def test_webhook_dedupe_window_is_bounded_without_forgetting_recent_ids(client, monkeypatch):
    monkeypatch.setattr(runtime, "DEDUPE_WINDOW", 3)
    s = mk_site(client)
    add(client, s["site_id"], "W-001", "+99999910001")
    sub = client.get(f"/v1/sites/{s['site_id']}").json()["subscriptions"]["W-001"]
    for i in range(5):
        client.post("/webhooks/geofence", json=ce(sub, GEO_ENTERED, "+99999910001", at=T0 + timedelta(minutes=i), ce_id=f"e{i}"), headers=AUTH)
    assert len(m.store.seen_cloudevent_ids) == 3
    again = ce(sub, GEO_ENTERED, "+99999910001", at=T0 + timedelta(minutes=4), ce_id="e4")
    assert client.post("/webhooks/geofence", json=again, headers=AUTH).json()["duplicate"] is True


# ------------------------------------------------------------------ phase 2: concurrency
def test_reset_keeps_the_same_locks(client):
    """Reset MUST NOT replace the lock: waiters on the old lock and holders of the new one would write at once."""
    lock = m.store.lock
    client.post("/v1/demo/no-breach")
    m.store.reset()
    assert m.store.lock is lock


def test_readers_never_see_a_container_change_under_them(client):
    """If a writer grew a dict while a lock-free reader was iterating it, we got `dictionary changed size during
    iteration` (measured 390 times with two threads at 400 workers). Rule: containers that grow outside a
    sweep are not mutated in place, they are copied and reassigned — what the reader holds never changes."""
    s = mk_site(client)
    site = m.store.sites[s["site_id"]]
    held = (m.store.sites, m.store.subs, site.workers, site.subscriptions, site.subscriptions_left)
    sizes = [len(c) for c in held]
    add(client, s["site_id"], "W-001", "+99999910001")
    mk_site(client)
    assert [len(c) for c in held] == sizes
    assert len(site.workers) == 1 and len(m.store.sites) == 2


def test_a_slow_operator_on_one_site_does_not_stall_another(client, monkeypatch):
    """While a Nokia call was slow, a single global lock was stalling the whole API."""
    import threading
    slow_site, other = mk_site(client), mk_site(client)
    add(client, slow_site["site_id"], "W-001", "+99999910001", subscribe=False)
    started = threading.Event()
    real_call = m.facade.call

    def slow_call(name, *a, **kw):
        if name == "location_verify":
            started.set()
            time.sleep(1.5)
        return real_call(name, *a, **kw)

    monkeypatch.setattr(m.facade, "call", slow_call)
    t = threading.Thread(target=lambda: client.post(f"/v1/sites/{slow_site['site_id']}/sweep",
                                                    json={"now": NOON.isoformat(), "wbgt_c": 35.0}))
    t.start()
    try:
        assert started.wait(5)
        t0 = time.monotonic()
        assert client.post(f"/v1/sites/{other['site_id']}/wbgt", json={"wbgt_c": 30.0}).status_code == 200
        assert client.get("/v1/sites").status_code == 200
        assert time.monotonic() - t0 < 0.75
    finally:
        t.join()


def test_planner_guard_demo_does_not_touch_process_state(client, monkeypatch):
    """The demo was changing the class method and environment variables process-wide: a real sweep
    running at the same time could have gone to the fake model."""
    from agent import llm_adapter as LA

    class RecordingEnviron(dict):
        writes: list = []

        def __setitem__(self, k, v):
            self.writes.append(k)
            super().__setitem__(k, v)

        def pop(self, k, *d):
            self.writes.append(k)
            return super().pop(k, *d)

    env = RecordingEnviron(os.environ)
    monkeypatch.setattr(m.os, "environ", env)
    method = LA.LLMPlanner.__dict__["_call_gemini"]
    seen = []
    monkeypatch.setattr(demo, "_sweep", lambda *a, **kw: (seen.append(LA.LLMPlanner.__dict__["_call_gemini"] is method),
                                                         _real_sweep(*a, **kw))[1])
    r = client.post("/v1/demo/planner-guard", json={})
    assert r.status_code == 200 and r.json()["simulated_model"] is True
    assert r.json()["rejected"]["violation"] and r.json()["accepted"]["used"]
    assert env.writes == [] and seen and all(seen)


_real_sweep = demo._sweep
