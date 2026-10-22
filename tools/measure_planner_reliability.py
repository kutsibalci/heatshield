"""The model's REAL latency and success rate. A measurement, not an estimate.

Our safety budget is 6 seconds: if a re-ranker takes longer than that, it is abandoned and the rules
run. To know whether this budget is right, we need to measure how long the model actually takes and
how often it answers.
"""
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone

from _common import evidence_file, load_env, require_live_flag

require_live_flag("Planner latency / success-rate measurement against Gemini / Groq.")
load_env()
os.environ["HS_PLANNER_TIMEOUT_S"] = "60"        # generous for measurement; the production budget is separate

from agent.llm_adapter import get_planner          # noqa: E402
from rules.heatshield import Action               # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 10
plan = [Action({"worker_id": f"W-{i:03d}", "masked": f"+99999***{i:04d}", "score": 40 - i,
                "vulnerability": 2.0 if i % 4 == 0 else 1.0, "micro_zone": f"z{i % 3}",
                "prior_incident": i == 8, "first_day_on_site": "2026-08-15",
                "last_signal_at": "2026-08-17T09:00:00Z"},
               "location_verify", "no exit event", i) for i in range(1, 13)]
snap = {"wbgt_c": 35.6, "legal_limit_c": 32.1, "workers_in_plan": len(plan), "budget_per_sweep": 20}

p = get_planner()   # if there is a chain we measure the chain — that is what runs in production
print(f"Model: {p.name}   sample: {N} calls\n")
rows = []
for i in range(1, N + 1):
    t = time.monotonic()
    p.propose(snap, plan)
    v = p.last_verdict()
    ms = int((time.monotonic() - t) * 1000)
    ok = bool(v.get("used"))
    rows.append({"call": i, "ok": ok, "ms": ms, "reason": None if ok else v.get("reason") or v.get("violation")})
    print(f"  {i:>2}. {'OK    ' if ok else 'FAILED'}  {ms:>6} ms   {rows[-1]['reason'] or ''}")

good = [r["ms"] for r in rows if r["ok"]]
print(f"\nsuccess: {len(good)}/{N}")
if good:
    print(f"latency  median {statistics.median(good):.0f} ms · min {min(good)} · max {max(good)}")
    for budget in (6, 10, 15, 20):
        within = sum(1 for m in good if m <= budget * 1000)
        print(f"  within a {budget:>2} s budget: {within}/{N}")
out = evidence_file("planner-reliability")
out.write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                           "model": p.name, "calls": rows,
                           "success_rate": f"{len(good)}/{N}",
                           "median_ms": statistics.median(good) if good else None},
                          ensure_ascii=False, indent=2), encoding="utf-8")
print("saved:", out)
