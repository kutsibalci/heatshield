# Developer guide

Short orientation for anyone opening this repository cold. The product story is in [README.md](README.md); the design is
in [docs/architecture.md](docs/architecture.md).

## Layout

```
apps/api/main.py        FastAPI service: routes, Scheduler, webhook sink, six demo scenarios
apps/api/common.py      masking of secrets and phone numbers in every response, shared facade
apps/web/               the console (index.html, base.js, base.css) — no build step, no CDN
apps/simulator/         a local mock that serves the real CAMARA paths (NAC_MODE=simulator)
packages/rules/         pure functions: thresholds, exposure score, budget, cost ladder, verdicts, jurisdictions
packages/agent/         SiteRuntime / WorkerRuntime state machine, sweep execution, ledger, liveness probe,
                        roster reconciliation, and the model adapter with guard_plan()
packages/nac_client/    the single entry point to Nokia Network as Code: auth, retry, circuit breaker, masking,
                        fixture / simulator / live backends
fixtures/profiles.json  device profiles the fixture backend answers from (all numbers +99999…, none real)
tests/                  130 tests, offline by construction (conftest.py strips every key from the environment)
tools/                  probes and measurements; their output is committed unaltered under evidence/
evidence/               machine-generated proof for every "verified live" claim
```

## Rules of the house

1. **No breach, no query.** Any code path that reaches the operator must sit behind a breached site state.
2. **Rules decide, the model may only re-order.** New behaviour goes into `packages/rules` as a pure function with a
   test; the model adapter never gains a new power.
3. **Every operator call goes through `packages/nac_client`.** Never call an endpoint that is not in its verified path
   table; if something is not available on the platform, mark it `# MOCK:` and say so in the docs.
4. **Every decision lands in the ledger** with its trigger (`scheduler` / `api` / `demo`), its source and its
   `explain[]`. If it is not in the ledger, it did not happen.
5. **Phone numbers are masked at the boundary** and secrets are redacted (`_mask_deep`). Add new secret key names to
   `_SECRET_KEYS`, not to individual handlers.
6. **Evidence is not edited to match policy.** Files under `evidence/` are committed as the tools wrote them.

## Running

```bash
python -m pip install -r requirements.txt
python -m pytest -q                                   # 130 tests, ~60 s, no network
python -m uvicorn apps.api.main:app --port 8000       # fixture mode → http://127.0.0.1:8000/demo
NAC_MODE=simulator ./run.sh simulator                 # mock operator on :8081 with the real CAMARA paths
```

Live mode needs your own Nokia Network as Code key: copy `.env.example` to `.env`, set `NAC_RAPIDAPI_KEY`, and run
`python tools/verify_live.py` — it probes the six APIs this product uses and writes the result under `evidence/`.

## Using the client

```python
from packages.nac_client import NacClient, NacConfig

nac = NacClient(NacConfig.from_env())            # NAC_MODE decides fixture / simulator / live
nac.location_verify("+99999000001000", lat=25.38, lng=51.49, radius_m=500, max_age_s=600)
nac.reachability("+99999000001000")
nac.congestion_query("+99999000001000")
nac.geofence_subscribe("+99999000001000", lat=25.38, lng=51.49, radius_m=500,
                       sink="https://example.com/webhooks/geofence", types=["org.camaraproject.geofencing-subscriptions.v0.area-entered"])
nac.qod_create("+99999000001000", profile="QOS_E", duration_s=600, app_server_ipv4="203.0.113.10")
nac.location_retrieve("+99999000001000", max_age_s=60)   # after escalation only — the one call that returns a coordinate
```

Deliberately unused: SIM/Device Swap, the KYC family, Number Verification. HeatShield measures exposure; it does not
establish identity, and calling those would violate data minimisation.
