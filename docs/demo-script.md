# HeatShield — 3-minute video script

**Length:** 3:00 · **Screen:** `https://heatshield-demo.onrender.com/demo` (the public instance — the address bar is part
of the evidence) or `http://127.0.0.1:8000/demo` locally · **Narration:** English · **Pace:** open the page with
`?pace=5000` so each sweep stays on screen for five seconds; the default 1.5 s is too fast to narrate.

**Before recording:** open the page once and press *Hot day* so the free instance is awake · press **Clear screen** ·
open the *Under the hood* panel at the bottom right and leave it open · browser zoom 100 %, F11 · notifications off.
Buttons are disabled while a scenario replays; wait for "✓ Replay finished" before pressing the next one.

Every scene names the criterion it is there for (Innovation · Impact · Scalability & Commercial · Technical
Feasibility & API Usage · Agentic AI & Multi-API Orchestration · Presentation).

---

## 0:00–0:12 · Opening — *Impact*

Page open, nothing pressed, address bar visible.

> "Qatar stops all outdoor work above 32.1 degrees wet-bulb. The law is strong; enforcement is blind. Nobody can
> show who was outdoors, at what temperature, at what hour — so causation is never established and liability never
> arises. HeatShield produces that record from the mobile network. No app, no wearable, nothing the worker does."

## 0:12–0:25 · `No breach → zero queries` — *Innovation*

Two sweeps, budget boxes empty, total queries 0.

> "Below the threshold the agent has no authority to query anyone. Two sweeps, zero API calls. That is law in code,
> not an optimisation."

## 0:25–0:40 · `Hot day (full cycle)`, sweep 10:15 — *Agentic AI*

Perimeter turns red, budget boxes fill, dashed red boxes appear.

> "Breach. Twenty ranked verifications a sweep. We spend the budget, not the list — and the report names the workers
> who had to wait."

## 0:40–0:55 · sweep 10:25 — *Multi-API Orchestration*

Ranking table changes; the *Under the hood → calls in this sweep* panel shows `location-verification`,
`device-reachability-status`, `congestion-insights`, `quality-on-demand` side by side.

> "Second sweep. Whoever was never queried in this breach goes first — a starvation bug we found by measurement. One
> device went silent: reachability, then congestion for the cell, then guaranteed bandwidth for the medic. Four CAMARA
> APIs in one sweep, each chosen by the rules."

## 0:55–1:12 · sweeps 10:35 → 10:37 — *Agentic AI · Impact · Privacy*

WBGT 35.4, state *emergency*, cadence 2 min; red dot with a white ring; ledger row `location_retrieve`.

> "Severe: the cadence tightens to two minutes. Only now a coordinate — once, coarsened, for the place the medic has to
> run to. From silence to medic: the same sweep."

## 1:12–1:45 · `Collapse, or a dead battery?` — *Innovation · Multi-API*

Verdict cards stay on screen for the whole breach. Point at each:

| Card | Say |
|---|---|
| W-002 — **probable collapse 0.95** | "Alone in its zone, neighbours reachable, congestion low, online for eight hours straight. This one goes to a human." |
| W-003 / W-004 — network event | "Two devices in the same micro-zone went dark together and the cell reports high congestion. Network event — nobody is woken." |
| W-005 — dead battery | "Same silence, same hour, every day. Battery." |
| W-006 — left the site | "Last seen at the exit gate, heading out. Logged, nothing more." |
| W-013 — liveness probe | "And a sixth phone the network called unreachable: we forced a fresh fix, it answered — alive. The stale reading was overruled. The cost ladder doubles as a liveness probe." |

> "Six silent devices, one human woken. No extra sensor — evidence we already had."

## 1:45–2:05 · `Planner guard` — *Agentic AI*

Two cards: *Proposal 1 ACCEPTED*, *Proposal 2 REJECTED*.

> "A language model sits under the rules: it may re-order the plan, nothing else. Here it re-ranks validly, then tries
> to add a worker, and the gate refuses the whole proposal. The provider is simulated behind this button; the live run
> against Gemini and Groq is committed under evidence/, with the 2-of-8 availability we measured."

## 2:05–2:17 · `Nokia API down` — *Technical Feasibility*

Red banner "Operator API degraded", ledger rows "NO ANSWER — counted as UNKNOWN, not as safe".

> "When the operator API fails, nobody is marked safe. Unknown is not safe."

## 2:17–2:35 · Evidence ledger + GitHub `evidence/README.md` — *Feasibility & API Usage*

Scroll the ledger slowly (times in site local time, `trigger` badge on every row). Then a new tab, five seconds:
`github.com/kutsibalci/heatshield/blob/main/evidence/README.md` — the six-APIs-returned-200 table.

> "The product is this ledger: which worker, which minute, what temperature, what evidence, what decision. All six
> CAMARA APIs returned 200 on the live Nokia platform on 9 September; the raw output is committed unaltered, including
> the three results we do not round up."

## 2:35–2:50 · `Three jurisdictions` — *Scalability & Commercial*

Three cards: Qatar prohibited · Saudi Arabia no ban · UAE prohibited.

> "Saudi Arabia and the UAE are a configuration entry. The buyer is the contractor who already carries the legal
> exposure; the price is per worker-month, and the operator earns two live subscriptions per worker for the length of
> employment."

## 2:50–3:00 · Closing card — *Presentation*

Final frame, plain text: `heatshield-demo.onrender.com/demo` · `github.com/kutsibalci/heatshield` · *126 tests · 6 CAMARA APIs*.

> "A hundred and twenty-six tests. Six APIs. And not one worker pressed a single button. We do not claim a safety
> guarantee — the inputs are best-effort and we publish their limits. What we produce is the evidence that does not
> exist today."

---

## If something goes wrong

- Port busy locally: `Get-CimInstance Win32_Process | ? { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'uvicorn' } | % { Stop-Process -Id $_.ProcessId -Force }`
- Public instance asleep: the first request after idle takes up to a minute — open the page before you press record.
- No internet: the local server runs everything from fixtures, identical output.
- Bad microphone: record silent and add the narration as subtitles. The jury reads the text, not the mic.
- Screen cluttered: **Clear screen** — it only clears the page; every scenario rebuilds its own site on the server.
