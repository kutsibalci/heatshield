"""nac_client core tests: fixture mode, retry/circuit breaker, masking, simulator HTTP path."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from nac_client import FixtureBackend, NacClient, NacConfig, NacError, NacCircuitOpen, mask_phone, hash_phone
from nac_client.privacy import normalize_phone

NOW = datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)

PROFILES = {
    "+999991110001": {"location": {"lat": 41.0, "lng": 29.0, "radius": 300}},
    "+999991110002": {"fail": 500},
    "+999991110003": {"latency_ms": 50},
}


@pytest.fixture
def client():
    return NacClient(NacConfig(mode="fixture", retries=1, breaker_threshold=2, breaker_cooldown_s=60), fixtures=FixtureBackend(PROFILES, now=lambda: NOW))


def test_mask_and_hash():
    assert mask_phone("+999991234567") == "+99999***4567"
    assert mask_phone("+99 999 123 45 67") == "+99999***4567"
    assert hash_phone("+999991234567", "s") == hash_phone("+99 999 123 45 67", "s")
    assert hash_phone("+999991234567", "s") != hash_phone("+999991234568", "s")
    with pytest.raises(ValueError):
        normalize_phone("abc")


def test_location_verify(client):
    assert client.location_verify("+999991110001", 41.0, 29.0, 1000).data["verificationResult"] == "TRUE"
    assert client.location_verify("+999991110001", 41.05, 29.0, 1000).data["verificationResult"] == "FALSE"
    assert client.location_verify("+999999999999", 41.0, 29.0, 1000).data["verificationResult"] == "UNKNOWN"


def test_result_masks_phone_in_request_summary(client):
    r = client.reachability("+999991110001")
    assert r.request["device"]["phoneNumber"] == "+99999***0001"
    assert r.source == "fixture" and r.latency_ms >= 0


def test_retry_then_circuit_opens(client):
    with pytest.raises(NacError) as e:
        client.reachability("+999991110002")
    assert e.value.kind == "server"
    # breaker_threshold=2 → first call 1+1 retry = 2 failures → circuit open
    with pytest.raises(NacCircuitOpen):
        client.reachability("+999991110002")
    assert client.health()["breaker"]["device-reachability-status"]["open"] is True


def test_simulator_http_roundtrip():
    """NacClient (simulator mode) → local simulator (TestClient) → same fixture; the real paths are verified."""
    import simulator.main as sim
    sim.fx = FixtureBackend(PROFILES, now=lambda: NOW)
    http = TestClient(sim.app, base_url="http://sim")
    c = NacClient(NacConfig(mode="simulator", base_url="http://sim", rapidapi_key="k"), http=http)
    # the six APIs the product calls, over real HTTP on the real CAMARA paths
    assert c.reachability("+999991110001").data["reachable"] is True
    assert c.location_verify("+999991110001", 41.0, 29.0, 1000, 600).data["verificationResult"] == "TRUE"
    assert c.location_retrieve("+999991110001", 60).data["area"]["center"]["latitude"] == 41.0
    assert isinstance(c.congestion_query("+999991110001").data, list)
    qod = c.qod_create("+999991110001", "203.0.113.10", "QOS_E", 600).data
    assert qod["sessionId"] and c.qod_get(qod["sessionId"]).data["sessionId"] == qod["sessionId"]
    c.qod_delete(qod["sessionId"])
    sub = c.geofence_subscribe("+999991110001", 41.0, 29.0, 500, "http://sink.local/webhook", sink_token="abc").data
    assert sub["status"] == "ACTIVE" and sub["id"]
    assert c.geofence_get(sub["id"]).data["id"] == sub["id"]
    assert c.geofence_delete(sub["id"]).data["id"] == sub["id"]
    with pytest.raises(NacError) as e:
        c.reachability("+999991110002")
    assert e.value.kind == "server"


def test_bad_input_does_not_open_the_circuit_for_everyone():
    """A single bad number (400) is not an outage; the circuit must not open and blind everyone."""
    profiles = {"+99999910001": {"fail": 400}, "+99999910002": {}}
    c = NacClient(NacConfig(mode="fixture", retries=0, breaker_threshold=2, breaker_cooldown_s=60),
                  fixtures=FixtureBackend(profiles, now=lambda: NOW))
    for _ in range(3):
        with pytest.raises(NacError) as e:
            c.reachability("+99999910001")
        assert e.value.kind == "bad_request"
    assert c.reachability("+99999910002").data is not None


def test_half_open_lets_exactly_one_probe_through():
    from nac_client.resilience import CircuitBreaker
    br = CircuitBreaker(failure_threshold=1, cooldown_s=60)
    br.failure("x")
    with pytest.raises(NacCircuitOpen):
        br.before("x")                      # cooldown not yet elapsed
    br._states["x"].opened_at -= 61         # cooldown elapsed
    br.before("x")                          # a single probe allowed
    with pytest.raises(NacCircuitOpen):
        br.before("x")                      # a second call does not pass before the probe resolves
    br._states["x"].probe_at -= 61
    br.before("x")                          # a probe that never resolves does not lock the circuit forever
    br.success("x")
    br.before("x")                          # a successful probe closes the circuit
