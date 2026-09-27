"""The six one-click demo scenarios. They reset the store and inject faults into the shared client, so
they exist only with HS_DEMO_MODE=1 and never against the live operator (`require_demo_mode`).
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from . import runtime, security
from .models import DemoIn, SiteIn, WorkerIn
from .runtime import (CROWD, NAMED, ROTATION_IDS, SITE, SITE_NAME, WELFARE_IDS, ZONES, _add_worker, _create_site,
                      _iso, _parse_dt, _stamp_trigger, facade, nac, store)
from nac_client import mask_phone
from nac_client.privacy import normalize_phone
from agent import SiteRuntime, apply_geofence_event, step

log = logging.getLogger("heatshield.api")
router = APIRouter()

DEMO_LIMITER = security.RateLimiter()


def require_demo_mode(request: Request) -> None:
    """Demo scenarios RESET the store and inject faults into the shared client. So they only run
    on an instance explicitly enabled for demos and not connected to the live operator."""
    if not security.demo_mode():
        raise HTTPException(404, {"code": "NOT_FOUND",
                                  "message": "demo scenarios are off; set HS_DEMO_MODE=1 on a fixture or simulator instance"})
    if getattr(nac.cfg, "mode", "fixture") == "live":
        raise HTTPException(409, {"code": "DEMO_NOT_ALLOWED_LIVE",
                                  "message": "demo scenarios reset the store and inject faults; they never run against the live operator"})
    retry = DEMO_LIMITER.hit(request.client.host if request.client else "unknown")
    if retry:
        raise HTTPException(429, {"code": "RATE_LIMITED", "message": f"too many demo runs; try again in {retry} s"},
                            headers={"Retry-After": str(retry)})


# ============================================================================ demo (one button)
DEMO_START = datetime(2026, 8, 17, 6, 0, tzinfo=timezone.utc)  # 09:00 Doha local time


def _prime(phone: str, patch: dict) -> None:
    """Demo data/fault injection — into both processes at once.

    In simulator mode Nokia calls go to a SEPARATE process (8081). If we do not forward the injection
    there too, two things break at once: (1) the api-down scenario contradicts its own headline on screen
    (only W-011 fails, the other ten workers show as "verified"), (2) because the persistent `fail` values
    in profiles.json cannot be cleared, the circuit breaker leaks into the next demo.
    """
    p = normalize_phone(phone)
    if nac.fx is not None:
        nac.fx.update_profile(p, patch)
    if getattr(nac.cfg, "mode", "fixture") == "simulator":
        try:
            httpx.put(f"{nac.cfg.base_url.rstrip('/')}/_sim/profiles/{p}", json=patch, timeout=2.0)
        except Exception as e:  # if the simulator is down the demo should still run
            log.warning("simulator prime failed phone=%s err=%s", mask_phone(p), e)


def _worker_in(spec: dict, t0: datetime) -> WorkerIn:
    """roster.json record → WorkerIn. `days_on_site` is converted to a date relative to the demo clock (since the
    vulnerability weight is measured as `now - first_day_on_site`, writing an absolute date would be wrong)."""
    return WorkerIn(
        worker_id=spec["worker_id"], phone=spec["phone"], name=spec["name"],
        micro_zone=spec["micro_zone"], zone_label=spec.get("zone_label", ""),
        nationality=spec.get("nationality", ""), trade=spec.get("trade", ""), crew=spec.get("crew", ""),
        first_day_on_site=(t0 - timedelta(days=int(spec["days_on_site"]))).date().isoformat(),
        prior_incident=bool(spec.get("prior_incident")), shift=spec.get("shift", "day"),
        moving_out=bool(spec.get("moving_out")), device_history=dict(spec.get("device_history") or {}),
        reachable_via_operator=bool(spec.get("reachable_via_operator", True)))


def _setup_site(t0: datetime, fillers: int = len(CROWD), jurisdiction: str = "QA") -> SiteRuntime:
    """13 named scenario workers + the crowd (default 227 → a roster of 240).

    The crowd makes visible why the budget is not enough: on a 240-person site there are 18 queries
    per sweep, so more than two hundred workers stay on the list every sweep. This is how the measured
    "full-coverage ceiling of ~110 workers/site" claim becomes concrete on screen.
    """
    site = _create_site(SiteIn(name=SITE_NAME, jurisdiction=jurisdiction, **SITE), at=t0)
    for _, _, spec in NAMED:
        _add_worker(site, _worker_in(spec, t0))
    for spec in CROWD[:max(0, fillers)]:
        _add_worker(site, _worker_in(spec, t0))
    # morning shift entry — free geofence events (no query is spent)
    for w in list(site.workers.values()):
        if w.worker_id == "W-012":
            w.badge_in, w.inside, w.last_signal_at = True, False, None  # badged in, the device was never seen
            continue
        apply_geofence_event(site, w.worker_id, "enter", t0)
    return site


def _ban_hour_rotation(site: SiteRuntime, out_at: datetime, in_at: datetime) -> dict:
    """Ban-hour rotation — ENTIRELY FREE, zero queries.

    In Qatar, outdoor work is banned between 10:00 and 15:30 (Ministerial Decision 17/2021). At 10:00 work
    stops; crews leave the perimeter. At 10:15 some of them re-enter at the site's shaded rest
    stations; the unacclimatised crew and those with a heat-stress record spend the whole ban window
    in the welfare compound OUTSIDE the perimeter. Both crossings are PUSHED by the network
    (geofencing-subscriptions) — not a cent of the budget is spent.

    It has two effects in the data, and both are measurable:
      * Those who re-enter have zero staleness (the network just said "inside") → they drop to the bottom of the
        ranking, but they do NOT drop OUT of the queue. The budget goes to those with no signal since the morning.
      * Those in the welfare compound become "presumed safe" → the 10% reserve re-verifies the stalest of them
        every sweep, because a stale exit event is a silent failure mode.
    A free event does NOT DECLARE anyone safe; it only sets their place in the order.
    """
    rotating = [w.worker_id for w in site.workers.values() if w.worker_id in ROTATION_IDS]
    welfare = [w.worker_id for w in site.workers.values() if w.worker_id in WELFARE_IDS]
    for wid in rotating + welfare:
        apply_geofence_event(site, wid, "exit", out_at)
    for wid in rotating:
        apply_geofence_event(site, wid, "enter", in_at)
    events = len(rotating) * 2 + len(welfare)
    b = site.cfg.budget
    usable = b.queries_per_site_per_minute - int(math.ceil(b.queries_per_site_per_minute * b.reserve_ratio))
    site.log(in_at, "ban_hour_rotation",
             f"Ban hours began: work stopped and {len(rotating) + len(welfare)} workers crossed the "
             f"perimeter out. {len(rotating)} are back inside at the shaded rest stations; "
             f"{len(welfare)} (the unacclimatised cohort and everyone with a heat-stress record) stay "
             f"in the off-site welfare compound for the whole window and count as presumed safe — the "
             f"reserve re-verifies the stalest of them every sweep. {events} network-pushed events, "
             f"ZERO queries: the queue is still {len(site.workers)} workers deep and the budget covers "
             f"{usable} per sweep",
             source="geofencing-subscriptions",
             extra={"returned_inside": len(rotating), "welfare_compound": len(welfare),
                    "events": events, "cost": 0})
    return {"returned_inside": len(rotating), "welfare_compound": len(welfare), "free_events": events,
            "queries": 0, "out_at": _iso(out_at), "in_at": _iso(in_at),
            "note": "Ban-hour rotation pushed by the network (geofencing-subscriptions). No query budget spent."}


def _demo_start(body: DemoIn | None) -> tuple[DemoIn, datetime]:
    body = body or DemoIn()
    if body.reset:
        store.reset()
    for api_name in list(nac.breaker.snapshot()):
        nac.breaker.success(api_name)          # reset the circuit breaker between scenarios
    for _, phone, *_ in NAMED:
        _prime(phone, {"fail": None})          # clear the previous scenario's fault injection
    return body, _parse_dt(body.start) or DEMO_START


def _sweep(site: SiteRuntime, at: datetime, wbgt: float | None, label: str) -> dict:
    r = _stamp_trigger(step(site, at, facade, site.cfg, wbgt), site, at, "demo")
    r["label"] = label
    r["local_time"] = (at + timedelta(hours=site.cfg.jurisdiction.tz_offset_hours)).strftime("%H:%M")
    return r


@router.post("/v1/demo/heat-day", dependencies=[Depends(require_demo_mode)])
def demo_heat_day(body: DemoIn | None = None):
    """A whole hot day: silent monitoring → legal breach → budgeted sweep → collapse → QoD →
    end of the breach."""
    with store.lock:
        body, t0 = _demo_start(body)
        site = _setup_site(t0, body.fillers)
        first = [_sweep(site, t0 + timedelta(minutes=0), 30.2, "09:00 — morning: below the threshold, NO authority to query")]
        # 10:00 — ban hours: work stops, crews go out to the shaded rest area outside the perimeter
        # and re-enter at 10:15. Free events pushed by the network; zero queries, zero cost.
        rotation = _ban_hour_rotation(site, t0 + timedelta(minutes=60), t0 + timedelta(minutes=75))
        sweeps = first + [
            _sweep(site, t0 + timedelta(minutes=75), 31.4, "10:15 — summer ban hours began: breach (marginal)"),
            _sweep(site, t0 + timedelta(minutes=85), 31.6, "10:25 — second sweep: stale signals to the front; one device went silent → probable collapse (0.95), medic called, guaranteed bandwidth opened"),
            _sweep(site, t0 + timedelta(minutes=95), 35.4, "10:35 — WBGT jumped to 35.4 °C: SEVERE breach, 2-minute sweeps; a one-time coordinate for the medic"),
            _sweep(site, t0 + timedelta(minutes=97), 35.4, "10:37 — next 2-minute sweep: the open case stays open (an alarm does not close itself); the other silent devices keep their network / battery / left verdicts, and the budget moves on to the workers nobody has heard from for twenty minutes"),
            _sweep(site, t0 + timedelta(minutes=600), 29.8, "19:00 — breach over: the authority to query lapsed"),
        ]
        return _demo_out(site, sweeps, "heat-day",
                         "Not one query while there was no breach; during the breach the budget was spent in risk order.",
                         extra={"ban_hour_rotation": rotation})


@router.post("/v1/demo/collapse", dependencies=[Depends(require_demo_mode)])
def demo_collapse(body: DemoIn | None = None):
    """Collapse, or a dead battery? Five silent devices, four verdicts — and one turned out to be a stale reading.

    W-013 is the new part of this demo: `device-reachability-status` shows it as "unreachable", but that reading is
    the state the network HOLDS and can be up to an hour stale. A strict-freshness location query pages the
    device, an answer comes back → not a collapse. The medic is not called for nothing.
    """
    with store.lock:
        body, t0 = _demo_start(body)
        site = _setup_site(t0, body.fillers)
        rotation = _ban_hour_rotation(site, t0 + timedelta(minutes=60), t0 + timedelta(minutes=75))
        sweeps = [
            _sweep(site, t0 + timedelta(minutes=75), 35.6, "10:15 — severe breach: who is still inside?"),
            _sweep(site, t0 + timedelta(minutes=77), 35.6, "10:17 — reachability sweep: six devices went silent"),
            _sweep(site, t0 + timedelta(minutes=79), 35.6, "10:19 — escalation: medic + guaranteed bandwidth"),
        ]
        verdicts = {}
        for s in sweeps:
            for v in s["verdicts"]:
                verdicts[v["worker_id"]] = v
        return _demo_out(site, sweeps, "collapse",
                         "Six devices went silent. Five verdicts of four kinds — one probable collapse, two network events, one dead battery, one worker who had left — and a sixth device the network was merely out of date about: a fresh-fix probe answered, so it was alive. One worker went to a medic.",
                         extra={"verdict_matrix": list(verdicts.values()), "ban_hour_rotation": rotation})


@router.post("/v1/demo/no-breach", dependencies=[Depends(require_demo_mode)])
def demo_no_breach(body: DemoIn | None = None):
    """Move 1 — below the threshold there is no authority to query: zero calls, zero cost, zero surveillance."""
    with store.lock:
        body, t0 = _demo_start(body)
        site = _setup_site(t0, body.fillers)
        sweeps = [
            _sweep(site, t0, 30.2, "09:00 — WBGT 30.2 °C: below the limit"),
            _sweep(site, t0 + timedelta(hours=8), 31.9, "17:00 — 31.9 °C: still below the limit, and the ban hours are over too"),
        ]
        return _demo_out(site, sweeps, "no-breach",
                         "Two sweeps, zero network queries. Outside a legal breach the system has no authority to query.")


@router.post("/v1/demo/api-down", dependencies=[Depends(require_demo_mode)])
def demo_api_down(body: DemoIn | None = None):
    """When the Nokia API goes down: nobody is silently marked 'safe' — the signal stays 'unknown'."""
    with store.lock:
        body, t0 = _demo_start(body)
        site = _setup_site(t0, 0)
        for _, phone, *_ in NAMED:
            _prime(phone, {"fail": 500})
        sweeps = [_sweep(site, t0 + timedelta(minutes=75), 35.6, "10:15 — the breach is on, but Location Verification returns 500")]
        unknown = [w for w in site.workers.values() if w.verified_inside is None and w.inside]
        return _demo_out(site, sweeps, "api-down",
                         "No signal → no worker was counted as verified; they stayed in the queue and the ledger wrote 'unknown'.",
                         extra={"unknown_workers": [w.worker_id for w in unknown], "breaker": nac.health()["breaker"]})


@router.post("/v1/demo/planner-guard", dependencies=[Depends(require_demo_mode)])
def demo_planner_guard(body: DemoIn | None = None):
    """The model sits BELOW the rules: it can argue about the order, it cannot widen the plan.

    The same breach is swept twice. In the first, the model proposes a valid reordering and it is applied.
    In the second, it tries to slip in a worker who does not exist in the plan at all; the safety gate rejects
    the proposal outright, the rules' order runs unchanged, and the rejection reason is written to the evidence ledger.

    No key is needed: the provider call is mocked in place for the duration of this demo, and this is stated
    explicitly in the response as `simulated_model=true` — it is not presented as a live model.
    """
    from agent import llm_adapter as LA

    def _valid(prompt):   # a rule-abiding proposal: move the most vulnerable to the front (reverse the list)
        ids = [i["id"] for i in json.loads(prompt)["plan"]]
        return json.dumps({"order": [{"id": i, "why": "unacclimatised / unseen longest"} for i in reversed(ids)],
                           "note": "risk order revised"})

    def _injects(prompt):  # violation: add a worker who is not in the plan
        ids = [i["id"] for i in json.loads(prompt)["plan"]]
        return json.dumps({"order": [{"id": "W-999", "why": "not in the plan at all"}] + [{"id": i} for i in ids]})

    def _scripted(reply) -> "LA.LLMPlanner":
        # The mock provider is given ONLY to this demo site (`site.planner`). Previously the environment
        # variables and `LLMPlanner._call_gemini` were changed process-wide: while the demo ran,
        # another site's real sweep could have gone to the mock model. The model name is deliberately
        # "demo-simulated": so it does not look like live Gemini in the ledger.
        planner = LA.LLMPlanner("gemini", "demo-key", model="demo-simulated")
        planner._call_gemini = reply
        return planner

    with store.lock:
        body, t0 = _demo_start(body)
        site = _setup_site(t0, 0)
        site.planner = _scripted(_valid)
        s1 = _sweep(site, t0 + timedelta(minutes=75), 35.6, "10:15 — the model reorders the queue")
        site.planner = _scripted(_injects)
        s2 = _sweep(site, t0 + timedelta(minutes=85), 35.6, "10:25 — the model slips in a worker who is not in the plan")
        site.planner = None

        accepted, rejected = s1.get("planner", {}), s2.get("planner", {})
        return _demo_out(site, [s1, s2], "planner-guard",
                         "The model could change the order; the moment it tried to widen the plan the gate refused and the rules' order stood.",
                         extra={"simulated_model": True,
                                "note": "The provider call is simulated in this demo; it is not presented as a live model. "
                                        "With a real key the same code path goes to Gemini/Groq.",
                                "accepted": {"used": accepted.get("used"), "moved": accepted.get("moved"),
                                             "before": accepted.get("order_before"), "after": accepted.get("order_after")},
                                "rejected": {"used": rejected.get("used"), "violation": rejected.get("violation"),
                                             "offending": rejected.get("offending"),
                                             "order_held": rejected.get("order_before") == rejected.get("order_after")}})


@router.post("/v1/demo/jurisdiction", dependencies=[Depends(require_demo_mode)])
def demo_jurisdiction(body: DemoIn | None = None):
    """Same moment, three jurisdictions: Qatar / Saudi Arabia / UAE. A jurisdiction is a config record."""
    with store.lock:
        body, t0 = _demo_start(body)
        at = t0.replace(hour=8, minute=45)   # 11:45 Doha/Riyadh · 12:45 Dubai local
        out = []
        for code in ("QA", "SA", "AE"):
            site = _setup_site(at, 0, jurisdiction=code)
            r = _sweep(site, at, 31.5, f"{code} — WBGT 31.5 °C (below the limit): the verdict comes from the ban hours alone")
            out.append({"code": code, "jurisdiction": site.cfg.jurisdiction.to_dict(), "sweep": r,
                        "breached": r["breached"], "reason": r["reason"], "local_time": r["local_time"],
                        "site": site.to_dict()})
        return {"scenario": "jurisdiction", "at": _iso(at), "results": out,
                "headline": "Same temperature, same minute — different jurisdiction, different legal outcome.",
                "config": runtime.CFG.to_dict()}


def _query_unit_cost_usd() -> float | None:
    """Unit cost per query — ONLY if the operator supplies its own figure (HS_QUERY_UNIT_COST_USD).

    There used to be a hard-coded `queries * 0.02` here. That 0.02 had no source at all: Nokia has no published
    unit price and we hold no quote. An unsourced dollar figure collapses under a single question from the jury,
    and honesty is this project's only capital. The default is now "no figure".
    """
    raw = (os.environ.get("HS_QUERY_UNIT_COST_USD") or "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        log.warning("HS_QUERY_UNIT_COST_USD is not a number: %r — no cost figure is reported", raw)
        return None
    return v if v >= 0 else None


def _cost_block(queries: int) -> dict:
    """Cost block: either a figure with a known source, or no figure at all."""
    unit = _query_unit_cost_usd()
    if unit is None:
        return {"queries": queries, "unit_cost_usd": None, "estimated_cost_usd": None, "priced": False,
                "basis": "No unit price is published by the operator and we hold no quote, so no dollar figure is "
                         "reported. Set HS_QUERY_UNIT_COST_USD to price a run with your own rate card; it is then "
                         "labelled an illustrative unit cost, never an operator quote."}
    return {"queries": queries, "unit_cost_usd": unit, "estimated_cost_usd": round(queries * unit, 2), "priced": True,
            "basis": f"queries x HS_QUERY_UNIT_COST_USD ({unit} USD) — an illustrative unit cost supplied by whoever "
                     f"runs this deployment. NOT a Nokia price and NOT an operator quote."}


# Measured full-coverage ceiling (README "Measured limits"): with 20 queries per sweep and a 2-minute
# cadence, the number of workers for which a site can see EVERYONE within the re-verification window.
# The demo site is more than twice this ceiling — this is how what the ceiling means shows up on screen.
FULL_COVERAGE_CEILING = 110


def _scale_block(site: SiteRuntime, sweeps: list[dict]) -> dict:
    """Scale block: the NUMBER behind the budget constraint — not a claim, a measurement read from this run.

    The sentence "full-coverage ceiling of ~110 workers/site" is abstract on its own. Here we report the real depth
    of the queue, how many people the sweep reached, and how many workers could not be queried even once
    during the breach. The right model for a large site is sub-sites; instead of hiding that, we count it.
    """
    breached = [s for s in sweeps if s.get("breached")]
    depth = [s["budget"]["skipped_for_budget"] + s["budget"]["planned"] - s["budget"]["reserve_rechecks"]
             for s in breached]
    inside = [w for w in site.workers.values() if w.inside and not w.cleared]
    never = [w.worker_id for w in inside if w.queries_used == 0]
    budget = site.cfg.budget.queries_per_site_per_minute
    reserve = int(math.ceil(budget * site.cfg.budget.reserve_ratio))
    return {
        "roster": len(site.workers),
        "in_queue_at_breach": max(depth) if depth else 0,
        "queue_depth_per_breached_sweep": depth,
        "budget_per_sweep": budget,
        "usable_per_sweep": budget - reserve,
        "reserve_per_sweep": reserve,
        "skipped_for_budget_per_sweep": [s["budget"]["skipped_for_budget"] for s in breached],
        "never_queried_in_this_breach": len(never),
        "full_coverage_ceiling_workers": FULL_COVERAGE_CEILING,
        "over_ceiling": len(site.workers) > FULL_COVERAGE_CEILING,
        "basis": (f"Measured on this run, not assumed. Our own ceiling is ~{FULL_COVERAGE_CEILING} workers "
                  f"per site at {budget} queries per sweep on a two-minute cadence, and this roster of "
                  f"{len(site.workers)} is "
                  + (f"{len(site.workers) / FULL_COVERAGE_CEILING:.1f}x over it: the budget reaches "
                     f"{budget - reserve} workers per sweep and the rest stay in the queue, counted above. "
                     f"A site this size should be modelled as several sub-sites with their own budgets - we "
                     f"would rather state that ceiling than be asked about it. "
                     if len(site.workers) > FULL_COVERAGE_CEILING else "inside it. ")
                  + "Free geofence events do the rest of the work: they cannot declare anybody safe, but "
                    "they tell the ranking who was seen a minute ago and who has not been seen since the "
                    "morning, and they cost nothing."),
    }


def _zone_block(site: SiteRuntime) -> dict:
    """Sub-zones — real Lusail precinct names, but as LABELS.

    The perimeter is ONE piece and stays that way: since the measured 1000 m location uncertainty is on the order
    of the 500 m radius, a sub-zone CANNOT BE VERIFIED from the network. This list is employer labels —
    used for crew grouping, WBGT attribution and the cluster test (how many devices with the same label went
    silent together); it is never derived from any coordinate and no geofence is drawn around it.
    """
    zones = []
    for zid, meta in ZONES.items():
        ws = [w for w in site.workers.values() if w.micro_zone == zid]
        if not ws:
            continue
        zones.append({"id": zid, "label": meta["label"], "detail": meta.get("detail", ""),
                      "workers": len(ws),
                      "inside": len([w for w in ws if w.inside and not w.cleared]),
                      "silent": len([w for w in ws if w.reachable is False])})
    return {"perimeter": "single — one geofence for the whole plot, never per zone",
            "source": "Lusail precinct names; see fixtures/roster.json `_sources`",
            "why_labels_only": ("Our own live measurement puts the platform's location uncertainty at "
                               "1000 m against this 500 m perimeter, so a smaller sub-zone cannot be "
                               "verified from the network. These are EMPLOYER labels used for crew "
                               "grouping, WBGT attribution and the cluster test - never derived from a "
                               "position, and no geofence is drawn around them."),
            "list": zones}


# The steps the ranking spends on (the budget is spent on these) — post-escalation calls are counted separately:
# location retrieval / congestion / QoD come AFTER a verdict, and would come in the "query everyone" world too.
VERIFICATION_APIS = ("location-verification", "device-reachability-status")


def _naive_baseline(sweeps: list[dict]) -> dict:
    """The "if we had queried everyone" baseline — measured, not made up.

    Previously it said `len(workers) × breached_sweeps × 2`; the 2 there was an assumption (two APIs per
    worker) and came from nowhere. The real baseline is read from the sweep's OWN report: on every
    breached sweep, one verification for each worker the roster counts as on site (badged in, no exit
    processed) — counted, not assumed.

    Apples to apples: the baseline counts verification steps, and we count verification steps
    too. Post-escalation calls (coordinate, congestion, QoD) would happen in both worlds, so they are
    removed from both and reported separately. Free geofence events are common to both as well.
    The baseline is deliberately CONSERVATIVE: only ONE verification per worker is counted (a naive system
    asking both steps would spend twice as much) and workers whose exit was processed are dropped up front.
    """
    def on_roster(sweep: dict) -> int:
        return len([w for w in (sweep.get("workers") or []) if w.get("badge_in") and not w.get("exited_at")])

    per_sweep = [on_roster(s) for s in sweeps if s.get("breached")]
    calls = [c for s in sweeps for c in (s.get("calls") or [])]
    # Liveness probes FOLLOW a silence verdict — they would happen in both worlds; only the budgeted pool is counted
    verification = sum(1 for c in calls if c.get("api") in VERIFICATION_APIS and c.get("pool") in ("main", "reserve"))
    return {"queries": sum(per_sweep), "per_breached_sweep": per_sweep,
            "verification_queries_made": verification,
            "escalation_queries_made": len(calls) - verification,
            "basis": "one verification per worker the roster places on site, on every breached sweep — no ranking, "
                     "no staleness filter, no budget cap. Counted from the sweeps themselves, not assumed. Compared "
                     "against our own verification queries; post-escalation calls (coordinate, congestion, QoD) "
                     "follow a verdict and would occur in both worlds, so they are excluded from both sides and "
                     "reported separately."}


def _demo_out(site: SiteRuntime, sweeps: list[dict], scenario: str, headline: str, extra: dict | None = None) -> dict:
    naive = _naive_baseline(sweeps)
    out = {
        "scenario": scenario, "headline": headline, "site": site.to_dict(), "sweeps": sweeps,
        "ledger": site.ledger, "coverage": site.coverage(), "degraded": list(store.degraded[-10:]),
        "presence": site.presence_record(),
        "scale": _scale_block(site, sweeps),
        "zones": _zone_block(site),
        "totals": {
            "queries": site.queries_total,
            "queries_if_polled_everyone": naive["queries"],
            "naive_baseline": naive,
            "escalations": sum(len(s["escalations"]) for s in sweeps),
            "skipped_for_budget": sum(s["budget"]["skipped_for_budget"] for s in sweeps),
            "cost": _cost_block(site.queries_total),
        },
        "config": site.cfg.to_dict(),
    }
    out.update(extra or {})
    return out
