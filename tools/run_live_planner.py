"""Gemini'yi CANLI cagirip ne onerdigini ve kapinin ne yaptigini KAYDEDER.

Neden: "model bagli" demek ile "model calistirildi" demek ayni sey degil. Bu betik gercek bir
tarama kosturur, modelin onerdigi sirayi ve guvenlik kapisinin verdigi karari `evidence/` altina
makine uretimi bir dosya olarak yazar. Deck'teki her "canli" iddiasinin arkasinda bir dosya var.

CAMARA cagrilari fixture modunda kalir — Nokia kotasini harcamadan modelin kendisini olceriz.
Modele giden istemde ham telefon numarasi, isim veya koordinat YOKTUR (bkz. llm_adapter._payload).

Calistirma (heatshield/ icinden):
    python tools/run_live_planner.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages"), str(ROOT / "apps")]
logging.disable(logging.CRITICAL)

UTC = timezone.utc
T0 = datetime(2026, 8, 17, 3, 0, tzinfo=UTC)
NOON = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)


def load_env() -> None:
    for candidate in (ROOT / ".env", ROOT.parent / ".env"):
        if candidate.exists():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
            return


class Facade:
    def __init__(self, nac):
        self.nac, self.fx = nac, getattr(nac, "fx", None)

    def call(self, name, *a, **k):
        return getattr(self.nac, name)(*a, **k)


def main() -> None:
    load_env()
    os.environ["NAC_MODE"] = "fixture"          # Nokia kotasini harcama; olculen sey MODEL
    os.environ.setdefault("HS_PLANNER", "auto")

    from nac_client import FixtureBackend, NacClient, NacConfig
    from agent import SiteRuntime, apply_geofence_event, new_worker, step
    from agent import policy as P
    from agent.llm_adapter import ChainPlanner, LLMPlanner, get_planner
    from rules import Config

    P.reset_planner()
    planner = get_planner()
    if not isinstance(planner, (LLMPlanner, ChainPlanner)):
        print("Canli planlayici DEVREDE DEGIL. .env icinde GEMINI_API_KEY (ya da GROQ_API_KEY)")
        print("ve HS_PLANNER=auto olmali. Su anki planlayici:", type(planner).__name__)
        sys.exit(1)
    print(f"Planlayici: {planner.name}  (saglayici: {planner.provider})\n")

    fx = FixtureBackend.from_json(ROOT / "fixtures" / "profiles.json")
    cfg = Config()
    site = SiteRuntime(site_id="live-1", name="Lusail Construction Site",
                       lat=25.38, lng=51.49, radius_m=500, cfg=cfg)
    for i in range(1, 13):
        phone = f"+9999991{i:04d}"
        #  acikca temizlenir: demo profilleri ayni numara araliginda ve biri hata
        # enjeksiyonu tasiyor. Burada olculen sey MODEL, CAMARA hata yolu degil.
        fx.update_profile(phone, {"location": {"lat": 25.3804, "lng": 51.4896, "radius": 120},
                                  "reachable": i != 2, "connectivity": ["DATA", "SMS"],
                                  "congestion": "Low", "fail": None, "latency_ms": 0,
                                  "stale_reachability": False})
        w = new_worker(f"W-{i:03d}", phone, name=f"Worker {i}", micro_zone=f"z{i % 3}",
                       prior_incident=(i == 8),
                       first_day_on_site=(T0 - timedelta(days=2 if i in (2, 5) else 400)).date().isoformat())
        site.workers[w.worker_id] = w
        apply_geofence_event(site, w.worker_id, "enter", T0)

    nac = Facade(NacClient(NacConfig(mode="fixture"), fixtures=fx))
    sweeps = []
    for k in (1, 2, 3):
        r = step(site, NOON + timedelta(minutes=2 * k), nac, cfg, 35.6)
        pl = r.get("planner", {})
        sweeps.append({
            "sweep": k,
            "used": pl.get("used"),
            "accepted": pl.get("accepted"),
            "violation": pl.get("violation"),
            "reordered": pl.get("reordered"),
            "moved_positions": pl.get("moved"),
            "latency_ms": pl.get("latency_ms"),
            "model_note": pl.get("note"),
            "order_before": pl.get("order_before"),
            "order_after": pl.get("order_after"),
            "model_reasons": pl.get("why"),
        })
        used = pl.get("used")
        head = "KABUL" if used else f"RED ({pl.get('violation') or pl.get('reason')})"
        print(f"Tarama {k}: {head}   gecikme={pl.get('latency_ms')} ms")
        if pl.get("note"):
            print(f"  modelin notu : {pl['note']}")
        if pl.get("order_before") != pl.get("order_after"):
            print(f"  kurallarin sirasi: {pl.get('order_before')}")
            print(f"  modelin sirasi   : {pl.get('order_after')}")
        for wid, why in list((pl.get("why") or {}).items())[:4]:
            print(f"    {wid}: {why}")
        print()

    out = ROOT / "evidence" / f"live-planner-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({
        "at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "planner": planner.name,
        "provider": planner.provider,
        "note": ("CAMARA calls run against the local fixture; what is measured here is the model "
                 "itself. The prompt carries no raw phone number, no worker name and no coordinate."),
        "sweeps": sweeps,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("kayit:", out)


if __name__ == "__main__":
    main()
