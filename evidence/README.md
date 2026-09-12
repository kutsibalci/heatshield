# Evidence

Every claim in this repository that says "verified live" points at a file in here. The files are
machine-generated output from probes in [`../tools/`](../tools), not transcriptions, and they are
committed **unaltered**.

| File | What it proves |
|---|---|
| `probe-20260909-124244.md` / `.json` | 9 September 2026 probe against the live Nokia Network as Code platform. All six APIs this product uses returned 200 — including the two that had never been attempted before: a **geofencing subscription created, verified and deleted** against a public HTTPS sink, and a **Quality on Demand session actually opened** (`qosStatus: REQUESTED`). |
| `lv-discrimination-20260909-130240.json` | Location Verification returns `FALSE` regardless of the area requested — including a **200 km circle centred on the coordinate Location Retrieval had just returned**. Also records that `maxAge` is mandatory: the call without it returns 422. |
| `lv-diagnosis-20260909-143413.json` | Elimination of two explanations for the above: coordinate order and sign (five variants, all `FALSE`) and device identifier mismatch (byte-identical in both calls). What remains is a sandbox question, put to Nokia. |
| `partial-scan-20260909-124602.json` | Verification across radii from 100 m to 5 km, at both loose and strict freshness. The platform reports an uncertainty radius of **1000 m** — the same order as a 500 m site perimeter. |
| `live-planner-20260909-*.json` (6 runs) | The planner pointed at the **live** model chain (Gemini → Groq) on 9 September, with the CAMARA calls answered from the local fixture so that what is measured is the model itself. Each file records the rules' order, the model's proposed order, the guard's verdict (accepted / which rule it broke), latency and the model's one-line rationale per worker. The prompt carries **no phone number, no name and no coordinate**. |
| `planner-reliability-20260909-*.json` (2 runs) | Availability of the model chain on the providers' free tiers: **2 of 8 calls succeeded**, the rest returned HTTP 429 or 503; median latency when a model does answer, **3.2 s**. This is why the chain ends in the deterministic rules and why the ledger records every fallback. |

## Why the coordinates in here are not coarsened

The product coarsens every coordinate it stores to three decimals. These files do not, and that is
deliberate: **evidence is not edited to match policy.** Rounding a measurement after the fact to
make it agree with a claim is the opposite of what this folder is for.

The coordinates belong to a Nokia sandbox test device that sits at a fixed point in Budapest. It is
not a subscriber and not a person.

## What is still not proven

No CloudEvent ever arrived at the sink. The simulated device does not physically move, so no
crossing was ever generated. What is proven is the **subscription lifecycle** — created, verified,
deleted on the real platform, against our own publicly reachable endpoint. The crossing itself, the
network's own detection, is the one link we cannot demonstrate. Everything on our side of that
boundary is live.

Quality on Demand returned `REQUESTED`, not `AVAILABLE`. The session was accepted; that the
guaranteed bandwidth actually activated is not something we verified.
