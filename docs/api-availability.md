# API availability — what HeatShield calls, and what the live platform answered

Six CAMARA APIs on Nokia Network as Code, all called through `packages/nac_client`. The numbers below come from the
probe of **9 September 2026** against the live platform (`evidence/probe-20260909-124244.json`, committed unaltered) and
from the follow-up scans in the same folder. Nothing here is asserted from documentation alone.

| API | Path family | Live result, 9 Sep 2026 | How HeatShield uses it |
|---|---|---|---|
| Geofencing Subscriptions | `geofencing-subscriptions/v0.3` | **200** · subscription created, read back, deleted (one event type per subscription — two types in one request returns 400) · no CloudEvent ever arrived, because the sandbox device does not move | Passive presence; two subscriptions per worker (entered / left); zero query budget |
| Location Verification | `location-verification/v1` | **200** · well-formed verdict and `lastLocationTime` · `maxAge` is mandatory (422 without it) · in the sandbox the verdict is `FALSE` for every area, including a 200 km circle around the device's own coordinate — put to Nokia | First rung of the cost ladder: a verdict, never a coordinate; loose `maxAge` = cached, strict `maxAge` = fresh fix and liveness probe |
| Device Reachability Status | `device-reachability-status/v1` | **200** · `reachable: true`, `connectivity: ["SMS"]`, `lastStatusTime` | Silent-device detection inside a breached zone; the status can lag, so a strict-freshness fix overrules it |
| Congestion Insights | `congestion-insights/v0` | **200** · four-interval forecast, latest `High` at confidence 56 | Cluster test: several devices dark together in a congested cell is a network event, not a medical one |
| Quality on Demand | `quality-on-demand/v0` | **200** · session opened with `QOS_E`, 600 s, application server `203.0.113.10`; read back from the session list; deleted · `qosStatus` came back `REQUESTED`, not `AVAILABLE` | Guaranteed bandwidth for the medic-to-physician assessment, after escalation |
| Location Retrieval | `location-retrieval/v0` | **200** · coordinate returned (the sandbox device sits in Budapest) · platform-reported uncertainty radius **1000 m** | Once, after escalation, coarsened to three decimals before it enters the ledger |

**Not called, on purpose:** Network Slicing is a separate Nokia product and appears in the code only as `# MOCK`. SIM and
Device Swap, KYC and Number Verification establish identity; HeatShield measures exposure and does not need them.

**Not integrated:** a weather provider. The WBGT value is an input to `/v1/sites/{id}/wbgt`; the demos feed it from the
scenario. Wiring a meteorological source is roadmap.

**What the platform taught us that no document did:** one event type per geofence subscription; `maxAge` mandatory on
Location Retrieval; a 1000 m uncertainty radius, which is the same order as a 500 m site perimeter — the reason the
`PARTIAL` verdict exists in the rules.

Reproduce with your own key: copy `.env.example` to `.env`, set `NAC_RAPIDAPI_KEY`, run `python tools/verify_live.py`.
The script writes its report under `evidence/`.
