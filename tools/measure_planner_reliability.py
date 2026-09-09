"""Modelin GERCEK gecikmesi ve basari orani. Tahmin degil, olcum.

Guvenlik butcemiz 6 saniye: bir yeniden siralayici bundan uzun surerse terk edilir ve kurallar
calisir. Bu butcenin dogru olup olmadigini bilmek icin modelin gercekte ne kadar surdugunu ve
ne siklikta cevap verdigini olcmek gerekir.
"""
import json, os, statistics, sys, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages")]
for c in (ROOT / ".env", ROOT.parent / ".env"):
    if c.exists():
        for line in c.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip())
        break
os.environ["HS_PLANNER_TIMEOUT_S"] = "60"        # olcum icin genis; uretim butcesi ayri

from agent.llm_adapter import LLMPlanner          # noqa: E402
from rules.heatshield import Action               # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 10
plan = [Action({"worker_id": f"W-{i:03d}", "masked": f"+99999***{i:04d}", "score": 40 - i,
                "vulnerability": 2.0 if i % 4 == 0 else 1.0, "micro_zone": f"z{i % 3}",
                "prior_incident": i == 8, "first_day_on_site": "2026-08-15",
                "last_signal_at": "2026-08-17T09:00:00Z"},
               "location_verify", "no exit event", i) for i in range(1, 13)]
snap = {"wbgt_c": 35.6, "legal_limit_c": 32.1, "workers_in_plan": len(plan), "budget_per_sweep": 20}

p = LLMPlanner("gemini", os.environ["GEMINI_API_KEY"])
print(f"Model: {p.name}   ornek: {N} cagri\n")
rows = []
for i in range(1, N + 1):
    t = time.monotonic()
    p.propose(snap, plan)
    v = p.last_verdict()
    ms = int((time.monotonic() - t) * 1000)
    ok = bool(v.get("used"))
    rows.append({"call": i, "ok": ok, "ms": ms, "reason": None if ok else v.get("reason") or v.get("violation")})
    print(f"  {i:>2}. {'BASARILI' if ok else 'DUSTU   '}  {ms:>6} ms   {rows[-1]['reason'] or ''}")

good = [r["ms"] for r in rows if r["ok"]]
print(f"\nbasari: {len(good)}/{N}")
if good:
    print(f"gecikme  medyan {statistics.median(good):.0f} ms · en dusuk {min(good)} · en yuksek {max(good)}")
    for budget in (6, 10, 15, 20):
        within = sum(1 for m in good if m <= budget * 1000)
        print(f"  {budget:>2} sn butcede yetisen: {within}/{N}")
out = ROOT / "evidence" / f"planner-reliability-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json"
out.write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                           "model": p.name, "calls": rows,
                           "success_rate": f"{len(good)}/{N}",
                           "median_ms": statistics.median(good) if good else None},
                          ensure_ascii=False, indent=2), encoding="utf-8")
print("kayit:", out)
