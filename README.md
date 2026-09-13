# HeatShield

**An autonomous heat-stress protection agent that uses the mobile network itself as the sensing layer.**

MENA Ignite Hackathon 2026 · Theme 6 (Climate Resilience) · Nokia Network as Code (CAMARA)

**Live demo:** https://heatshield-demo.onrender.com/demo — the six one-click scenarios below, running in fixture mode with no operator credentials on the instance. Free tier: the first load after idle can take up to a minute.

---

Outdoor work in the Gulf is lethal for months of the year, and the law already forbids it: Qatar's Ministerial
Decision No. 17 of 2021 stops all work whenever the Wet Bulb Globe Temperature exceeds **32.1 °C**, year-round and
at any hour. The law is strong; enforcement is blind — no employer, inspector or insurer can answer in real time
which worker is still outdoors while the threshold is breached, because every existing safety product assumes an
app the worker opens, a wearable the employer buys, or a foreman who reports honestly. HeatShield removes all three
assumptions. It treats the operator network as the sensor: a geofence subscription per site perimeter costs nothing
and needs nothing running on the device, so while no threshold is breached the agent spends zero queries. The moment
WBGT crosses the legal limit the site enters alert state and the agent earns the authority to verify — ranking every
worker with no recorded exit by `severity × exposure_minutes × staleness × vulnerability`, spending a fixed
per-sweep query budget from the top of that list, climbing the cost ladder only when the cheap signal is ambiguous,
and stopping when the budget runs out rather than when the list does — then reporting how many workers it could not
reach instead of assuming they are safe. Every decision, with the signal that triggered it, lands in an evidence
ledger: the compliance audit trail is itself the product.

**What this claims, precisely.** Every input here is best-effort. A phone can be left in a hut, a battery can die, the
network can miss a crossing, and our own measurement puts the platform's location uncertainty at **1000 m against a
500 m site perimeter**. So HeatShield does not claim a safety guarantee and would not be put behind an SLA. What a
mobile network *can* carry is **evidence**: a timestamped, per-worker, auditable record of who was outdoors, at what
temperature, at what hour, what was decided and why — plus an honest count of who could not be seen. This is a
compliance-evidence system that improves safety as a consequence, not a safety system that produces paperwork. The
distinction decides the architecture, the claims, and who buys it.

## The six CAMARA APIs

All six are called by the prototype through a single entry point (`packages/nac_client`).

| API | Role in HeatShield |
|---|---|
| **Geofencing Subscriptions** | The backbone. Entry/exit at the site perimeter arrives as a network notification — nothing on the device, and **no query budget spent**. This is what makes passive watch free. |
| **Location Verification** | For a worker with no recorded exit: are they still inside the zone? Returns a **verdict, never coordinates** — the first rung of the cost ladder. |
| **Device Reachability Status** | A device unreachable inside a breached zone is a possible collapse. Queried only for workers already confirmed inside. |
| **Congestion Insights** | Separates a medical event from a network event when several devices in one micro-zone go silent together. Cached 5 minutes per micro-zone to protect the budget. |
| **Quality on Demand** | Guaranteed bandwidth for the medic-to-physician video assessment once a collapse is escalated. |
| **Location Retrieval** | Where to send the medic — **after escalation only**, once, coarsened to three decimals before it enters the ledger. The only API here that returns a coordinate, so the most tightly gated. |

**Network Slicing** is a separate Nokia product on the roadmap. It is marked `# MOCK:` in `packages/agent/policy.py`
and is **not** presented as live. Deliberately unused: SIM/Device Swap, the KYC family, Number Verification —
HeatShield does not establish identity, it measures exposure, and calling those would violate data minimisation.

## Running it

```bash
python -m pip install -r requirements.txt
python -m pytest -q                  # 130 tests
```

Three modes, selected by `NAC_MODE`:

| Mode | What it talks to | Use |
|---|---|---|
| `fixture` | Canned responses from `fixtures/profiles.json` | Fully offline; the demos run here |
| `simulator` | A local FastAPI mock on `:8081` serving the **real CAMARA paths** with the same request/response schemas | Exercises the whole HTTP layer offline |
| `live` | Nokia Network as Code over RapidAPI | Requires your own key (see Configuration) |

```bash
python -m uvicorn apps.api.main:app --port 8000    # fixture mode: no key, no second server — what the public demo runs
```

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 -Mode simulator   # Windows - mock on :8081, API on :8000
```

```bash
./run.sh simulator             # Linux / macOS / Git Bash
```

Then open **http://127.0.0.1:8000/demo** for the six one-click demos:

| Demo | Endpoint | What it proves |
|---|---|---|
| Hot day (full cycle) | `POST /v1/demo/heat-day` | Silent watch → breach → budgeted sweep → collapse → QoD → breach ends |
| **Collapse, or a dead battery?** | `POST /v1/demo/collapse` | Six devices read as unreachable. One was a stale network reading — a fresh-fix probe proved it alive. Five verdicts of four kinds (collapse, network event, dead battery, left site), one escalation — with the evidence for each |
| No breach → zero queries | `POST /v1/demo/no-breach` | Two sweeps, zero API calls: purpose limitation, enforced |
| Three jurisdictions | `POST /v1/demo/jurisdiction` | Same minute, same temperature — Qatar bans, Saudi Arabia permits, the UAE bans |
| Nokia API down | `POST /v1/demo/api-down` | Circuit breaker opens; **nobody is counted safe**, the ledger records "unknown" |
| **Planner guard — the model is overruled** | `POST /v1/demo/planner-guard` | The same breach swept twice: a valid re-ranking is accepted, then the model tries to slip in a worker who is not in the plan and the guard rejects the whole proposal. The provider call is simulated here and the response says so (`simulated_model: true`) — it is not presented as a live model |

Switch jurisdiction with `HS_JURISDICTION=SA` (or `AE`); the default `QA` is the reference implementation.

Code comments and docstrings are partly in Turkish, the author's working language; every user-facing string, the docs,
the tests and the evidence are in English.
**Three jurisdictions ship today** (`QA`, `SA`, `AE` in `packages/rules/config.py`). One assumption is stated in
the config and repeated here: only Qatar publishes a numeric WBGT limit, so **Saudi Arabia and the UAE carry the
Qatari 32.1 °C figure as a placeholder** while their own midday-ban clocks are their real, published rule. Their
WBGT number is our assumption, not their law.

## Architecture

```
apps/api (FastAPI)          HTTP surface, webhook sink, the six demos, SafeFacade
   └── packages/agent       sweep engine: SiteRuntime, WorkerRuntime, step() - the agent policy
        └── packages/rules  deterministic PURE functions, each returning explain[]
             └── packages/nac_client   the single Nokia / CAMARA entry point
apps/simulator              local Nokia mock on the real CAMARA paths (simulator mode)
```

The split is the safety argument. **`packages/rules` is deterministic pure functions** — jurisdiction thresholds,
the exposure score, the budget plan, the collapse classification — each returning an `explain[]` trace, so every
verdict is testable, reproducible and auditable. **`packages/agent/policy.py`** holds the agent's policy: what to
query, in what order, until the budget is gone. A reasoning model sits *under* these rules, never above them — it
may re-rank a plan the rules produced, but it cannot invent an API call, exceed the budget, or overturn a safety
verdict. When an operator API fails, `SafeFacade` returns an explicit error, never fabricated data: no worker is
ever marked "cleared" on a response the agent does not trust.

Detail: [`docs/architecture.md`](docs/architecture.md) · [`docs/api-availability.md`](docs/api-availability.md) ·
[`docs/demo-script.md`](docs/demo-script.md)

Tests: `tests/test_rules.py` · `tests/test_policy.py` · `tests/test_planner.py` · `tests/test_api.py` ·
`tests/test_nac_client.py` — **130 total** (parametrised cases included), all offline.

## Why this is not the GSMA catalogue use case

GSMA Open Gateway already lists a *Worker Safety Monitoring* use case built on Device Geofencing Subscriptions, Device
Location Retrieval and SMS: it tells an employer who is where. HeatShield shares the API family and departs from it in
three places. **The trigger is a legal number, not a schedule** — below the WBGT limit the agent has no authority to
query anyone, and that is enforced in code. **The default query is Location Verification**, a yes/no verdict, not
Retrieval — a coordinate is requested once, after an escalation, and coarsened before it is stored. **The hard part is
not the calls but their allocation** — a per-sweep budget spent in risk order with a starvation guard, and the
collapse-versus-dead-battery classification that keeps a medic from being paged for every flat phone. The catalogue
scenario is a location tool; this is a compliance-evidence producer that spends location queries as sparingly as the law
allows. Wearables (Kenzen at EGA, viAct's watch) measure the body better than a network ever will and cost a charged,
worn device per worker; HeatShield measures presence under a legal threshold with zero hardware. Complementary, not
substitutes.

## Honesty: what is verified live, and what is not

*This section is the point. Read it before you read anything else we claim.*

On **9 September 2026** a server-side probe called the live Nokia Network as Code platform with a publicly reachable
HTTPS sink standing. **All six of our APIs answered 200.** The two gaps left open by the 22 August run — a live
geofencing subscription, and a QoD session actually opened — are closed. Three of the six results are narrower than
the status code makes them look, and those are stated here unvarnished.

| API | Live status, 9 Sep 2026 | Detail |
|---|---|---|
| Geofencing Subscriptions | ✅ **200** (228 ms) | Subscription **created on the live platform**, returned with its id, then deleted (`f402f71d-a5c1-4061-847f-d25fbcf79e2f`). Public HTTPS sink, one event type, 300 m circle, one-day expiry |
| Quality on Demand | ✅ **200** (174 ms) | Session **genuinely opened** — `QOS_E`, 600 s, `applicationServer.ipv4Address 203.0.113.10` — read back by the session-list endpoint (200, 127 ms), then deleted. **`qosStatus: REQUESTED`, not AVAILABLE** |
| Device Reachability Status | ✅ **200** (145 ms) | `reachable: true`, `connectivity: ["SMS"]`, `lastStatusTime` returned |
| Location Verification | ✅ **200** (152 ms) | A well-formed verdict and `lastLocationTime`. **In the sandbox the verdict does not vary with the area asked** — see below |
| Location Retrieval | ✅ **200** (163 ms) | Coordinate returned, then coarsened — Budapest, the simulated position. Platform-reported uncertainty radius **1000 m** |
| Congestion Insights | ✅ **200** (139 ms) | Four-interval forecast; most recent interval `High` at `confidenceLevel: 56` |
| Network Slicing | — **mock / roadmap** | Marked `# MOCK:` in the code; a ledger entry is written, no call is made |

**Three things we do not round up.**

**Nokia sent nothing to our sink.** A simulated device does not physically move, so no crossing event was ever born,
and the subscription was deleted seconds later — our sink log holds our own connectivity test and nothing else. What
is proven is the **subscription lifecycle**, not the crossing. Live: the creation, confirmation and deletion of a real
subscription on the operator's platform, and our own public HTTPS endpoint. Simulated: only the network actually
detecting a boundary crossing. The one link we cannot prove sits on Nokia's side; everything on our side of that line
is live. Nokia's own open-source integration tests skip every geofencing case for the same reason. Architecturally
this is survivable — Geofencing is a *cost optimisation*, Location Verification is *the decision* — so if crossing
events cannot be shown live the agent does not get dumber, it just works the expensive way.

**QoD returned `qosStatus: REQUESTED`, not AVAILABLE.** The session request was accepted; guaranteed bandwidth was not
verified as active. A session **can be opened** — that is the claim, and it is the whole claim.

**Location Verification answers, but in the sandbox it does not discriminate.** We centred the query on the device's
own coordinate — the one Location Retrieval had just returned — and grew the radius: 100 m, 250, 500, 750, 1000,
1500, 2000, 3000, 5000, each at loose freshness (`maxAge` 3600) and again at strict (`maxAge` 60). Eighteen calls,
**FALSE every one**, including circles that contain the device by kilometres. The transport, the request/response
schema and the freshness parameter are proven; the verdict logic is not. That is a limit of the simulated
environment, not of our code — and it means **the PARTIAL rate cannot be measured in the sandbox**, so no PARTIAL
statistic in this repository rests on live data. It is the first question we are taking to Nokia.

**Two things the live platform taught us that no document did.** Sending two event types in one subscription returns
**400** — CAMARA requires one event type per subscription — so the code now opens two subscriptions per worker, entry
and exit (`apps/api/main.py`). And `maxAge` is **mandatory** on Location Retrieval; without it the platform returns
422. The freshness axis the cost ladder rides on is the platform's requirement, not our design preference.

What is not in doubt: a working prototype runs today, offline, with 130 automated tests and six one-click demos.
Nothing in this repository is claimed as live that is not. Raw probe output (credentials and MSISDN masked) is in
[`evidence/probe-20260909-124244.json`](evidence/probe-20260909-124244.json), the
Location Verification radius scan in
[`evidence/partial-scan-20260909-124602.json`](evidence/partial-scan-20260909-124602.json), and the
endpoint-by-endpoint record in [`docs/api-availability.md`](docs/api-availability.md).

**On WBGT — and this is the biggest gap in the prototype.** The product does not measure WBGT, it consumes it.
Today it consumes it **from outside**: the value is pushed in by `POST /v1/sites/{site_id}/wbgt` and the ledger
records it as `source: meteorology-feed`. **No weather provider is integrated.** There is no code in this
repository that fetches, subscribes to or computes a WBGT value — the trigger of the entire product is, at this
stage, a number someone hands us. Ingestion from a site sensor, and a computed fallback for sites without one,
are **roadmap**; when they land the UI will have to label which of the two a reading came from, because Qatari law
ties the threshold to a value measured **at the workplace**, so a national meteorological figure would not be a
legal substitute for a site reading. We state that limit rather than hide it.

## Measured limits

Claims about scale are easy to make and easy to check, so we checked ours.

| What | Measured |
|---|---|
| 400-worker sweep | 47 ms |
| 20,000 workers in memory | 90 MB (~3 KB per worker) |
| **Full coverage ceiling** | **~110 workers per site** at 20 queries/sweep, 2-minute cadence |
| Silence → medic notified and a QoD session opened | **within the same sweep** — ≤ 2 min cadence in a severe breach, ≤ 10 min marginal (heat-day scenario). The paper log it replaces is signed at the end of the shift |
| 8-hour shift, 400 workers | 7,341 ledger rows / 13.9 MB — the ledger is now bounded and counts what it drops |

CPU and memory are not the limit; the budget policy is. Beyond ~110 workers a site should be
modelled as several sub-sites — a 400-person project is four sites of a hundred, not one of four
hundred. We would rather state that ceiling than be asked about it.

Finding it cost us an assumption: in a two-hour unbroken breach at 400 workers, **80% were never
queried once**. Staleness saturated at 30 minutes, so ranking fell to vulnerability alone and the
same 80 people won every sweep — while our own text promised the opposite. A worker not yet queried
in this breach now takes absolute priority over anyone already checked. Coverage: 20% → 100%.

## What the model adds, measured

The reasoning model may propose a different order for the plan the rules produced. We measured what
that is worth: the best any re-ranker can do is put the worker at risk first, so we compared the
rules' order against a perfect oracle.

| Workers | Budget | Rules | Perfect oracle | Difference |
|---|---|---|---|---|
| 400 | 20 | 5th sweep | 5th sweep | **0** |
| 400 | 8 | 13th sweep | 13th sweep | **0** |
| 1000 | 20 | 12th sweep | 12th sweep | **0** |

The result is structural, not luck: the plan is capped by the budget **before** the model sees it,
the guard forbids changing its membership, and every action in the plan is executed — so ordering
cannot change *who* is contacted, only the sequence.

The agency that matters lives in the allocation policy: the budget, the cost ladder, staleness, the
starvation guard. Those are deterministic and tested. The model stays because ordering does matter
when a sweep is cut short by a rate limit or a degraded API — **we have not quantified that case, so
we do not claim it.**

### And we ran it live

On 9 September 2026 the planner was pointed at real providers. It works, and it reasons the way you
would want:

> *"Prioritise unacclimatised, unseen, and prior incident workers first."*
> `W-002: First week on site, unacclimatised, high heat risk` ·
> `W-008: Prior heat incident, unseen long time, high risk`

Then we measured how often it is actually there. On the free tiers of both providers, **2 of 8 calls
succeeded**; the rest returned HTTP 429 (`evidence/planner-reliability-20260909-175855.json`). Earlier the same
evening ten consecutive Gemini-only calls all failed (`…-172205.json`; the adapter logged 503, the file records only the exception class)
earlier the same evening. Latency, when it answers, has a median of 3.2 s — inside the 6-second
budget the agent allows a re-ranker before abandoning it.

So the provider chain is not decoration either. Gemini is tried first and usually fails in about
400 ms; Groq answers in about 2 s; if both are gone the deterministic order runs. Every one of those
outcomes is written to the evidence ledger with the HTTP status, because *"the model was
unavailable"* is not something a safety system should record vaguely.

This is the honest shape of it: **a model that is often absent, behind a guard that makes its
absence a non-event.** We would rather show that than a screenshot of one good response.

## Privacy

- **No breach, no query authority.** The system cannot become productivity surveillance.
- The default query returns a **verdict, not a coordinate**. Coordinates only after escalation, once.
- **Location history is counted, not denied.** The ledger is a bounded per-worker presence record (cap 5,000 entries,
  `presence_record` in `/v1/state`); post-escalation coordinates are retained as evidence, coarsened to three decimals,
  and the count is shown on screen. An earlier draft claimed "never retained"; the audit showed that was wrong.
- Raw MSISDNs appear in no response: masked (`+99999***0001`) plus HMAC-SHA256 hash; logs pass through `MaskingFilter`.
- **Honest coverage metric:** workers who badge in but never appear on the network are reported as
  `coverage.missing`. A worker without a phone is invisible to us, and we say so rather than hiding it.

## Configuration

```bash
cp .env.example .env      # then put YOUR OWN key in .env
```

`.env` is listed in `.gitignore` and **must never be committed.** `.env.example` ships placeholders only. For `live`
mode you need your own Nokia Network as Code account (https://networkascode.nokia.io — the free SIMULATOR plan is
enough to reproduce the probe above) and your own RapidAPI key in `NAC_RAPIDAPI_KEY`. Also change `PHONE_HASH_SALT`
and `WEBHOOK_TOKEN` from their placeholder values before any deployment. `fixture` and `simulator` modes need no
credentials at all — the demos and the full test suite run with an empty `.env`.


## A note on the phone numbers in this repository

Every MSISDN in the fixtures, demos and tests is in the `+99999…` range: the same range Nokia uses
for its sandbox test devices. It is not assignable to a subscriber in any country.

This was not the first choice. The fixtures originally used numbers in Qatar’s real mobile format,
which are fictional but *plausible* — and a product built on privacy discipline should not publish
plausible subscriber numbers for the country it is aimed at. They were moved.
