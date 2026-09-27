"""The live probe's logic, exercised offline against the fixture backend.

Only the probe functions are called — never `main()`, which would read `.env`. No network is touched.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import verify_live as VL  # noqa: E402

from nac_client import FixtureBackend, NacClient, NacConfig  # noqa: E402

PHONE = VL.DEFAULT_PROBE_PHONE


@pytest.fixture
def client():
    fx = FixtureBackend({PHONE: {"location": {"lat": 25.2854, "lng": 51.5310, "radius": 300}}})
    return NacClient(NacConfig(mode="fixture"), fixtures=fx)


def test_the_probe_covers_exactly_the_apis_the_product_calls():
    apis = {p.paths[0].split("/")[0] if not p.paths[0].startswith("device-status") else p.paths[0].split("/")[1]
            for p in VL.PROBES}
    assert apis == {"device-reachability-status", "location-retrieval", "location-verification",
                    "congestion-insights", "geofencing-subscriptions", "quality-on-demand"}
    assert VL.DEFAULT_PROBE_PHONE.startswith("+99999")      # a sandbox test device, never a real-format number


def test_every_probe_succeeds_and_everything_created_is_deleted(client, tmp_path):
    ctx = VL.ProbeCtx(phone=PHONE, sink="http://127.0.0.1:8000/webhooks/probe")
    results = VL.run_probes(client, ctx, include_write=True, only=None)
    assert [r["key"] for r in results if not r.get("ok")] == []
    assert {k for k, _ in ctx.created} == {"geofencing", "qod"}
    cleaned = VL.cleanup(client, ctx)
    assert cleaned and all(c["deleted"] for c in cleaned)

    meta = {"started": "2026-09-27T12:00:00+00:00", "mode": "fixture", "base_url": "-", "phone_masked": "x",
            "sink": ctx.sink, "has_key": False, "include_write": True}
    j, md = VL.write_report(results, meta, cleaned, out_dir=tmp_path)
    assert j.exists() and md.exists()
    assert PHONE not in j.read_text(encoding="utf-8") and PHONE not in md.read_text(encoding="utf-8")


def test_write_probes_are_skipped_without_include_write(client):
    ctx = VL.ProbeCtx(phone=PHONE, sink="http://127.0.0.1:8000/webhooks/probe")
    results = VL.run_probes(client, ctx, include_write=False, only=None)
    assert {r["key"] for r in results if r.get("skipped")} == {"geofencing_subs", "qod_sessions"}
    assert ctx.created == []
