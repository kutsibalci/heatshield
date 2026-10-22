"""HeatShield agent policy — the deterministic sweep engine.

Idea capture §5.2 (escalation ladder), §8 (verification budget) and §9 (collapse vs dead battery) become
running code here. The decision logic lives in the PURE functions in `packages/rules`; this file only
manages the ordering, the state and the Nokia calls.

Single entry point:  `step(site, now, nac_facade, cfg, wbgt_c=None) -> dict`  (one sweep = one report)

Three moves (§8):
  Move 1 — No breach, NO SPENDING. Geofence events are pushed by the network for free; query authority
           arises only when the legal threshold is exceeded (also purpose limitation: it cannot become a surveillance tool).
  Move 2 — Rank, spend from the top. score = severity × exposure_minutes × staleness × vulnerability.
           When the budget runs out we STOP even if the list has not; how many workers went unqueried goes into the report.
  Move 3 — Climb the cost ladder only if the cheap signal is ambiguous:
           location_verify (yes/no) → reachability → location_retrieve (only after escalation).

Privacy: the raw phone number lives only in `WorkerRuntime.phone` and goes only to the NaC call.
`to_dict()` never returns the raw number. A coordinate is fetched only for a "probable collapse" case, once.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from nac_client.privacy import hash_phone, mask_phone, normalize_phone
from rules import Config
from rules import heatshield as R
from rules.timeutil import iso_z as _iso, to_utc as _dt
from rules.heatshield import STATES  # passive_watch | alert | confirming | distress | emergency

__all__ = ["SiteRuntime", "WorkerRuntime", "step", "apply_geofence_event", "sweep_due", "STATES"]


# --------------------------------------------------------------------------- worker
@dataclass
class WorkerRuntime:
    """A worker's live state. `phone` is kept raw (NaC call only); what goes out is masked/hashed."""

    worker_id: str
    phone: str
    name: str = ""
    micro_zone: str = "z1"
    first_day_on_site: str | None = None      # ISO date — first-week weight
    prior_incident: bool = False
    shift: str = "day"                        # day | night_to_day | transition
    badge_in: bool = True                     # morning turnstile record (§9A reconciliation)

    # --- employer registry fields (they do NOT enter the decision; they show who we are talking about,
    #     on screen and in the ledger)
    # Nationality and trade are not used in scoring: ranking a worker by nationality is discrimination and
    # the product's only capital is honesty. They exist to tell the medic/shift supervisor "who, where, doing
    # what job" (and to show which workforce the coverage metric was measured on).
    nationality: str = ""
    trade: str = ""                           # trade: steel fixer, scaffolder, pipe fitter ...
    crew: str = ""                            # crew/gang name — the shift supervisor knows this unit
    # `micro_zone` is an EMPLOYER LABEL, not derived from location: the measured 1000 m location uncertainty
    # is on the order of the 500 m perimeter, so the sub-zone CANNOT be verified from the network. `zone_label`
    # is that label's human-readable name (Marina District, Energy City ...) and is used only for WBGT
    # attribution, crew grouping and the cluster test. The perimeter is ONE piece.
    zone_label: str = ""

    # --- live signals
    inside: bool = True                       # from geofence events (free)
    entered_at: datetime | None = None        # moment of entering the site — start of the exposure window
    exited_at: datetime | None = None
    last_geofence_at: datetime | None = None  # time of the most recently APPLIED geofence event (ordering guard)
    last_signal_at: datetime | None = None
    last_verified_at: datetime | None = None
    verified_inside: bool | None = None
    moving_out: bool = False
    last_reachability_at: datetime | None = None
    reachable: bool | None = None
    location_retrieved: bool = False
    last_location: dict | None = None         # only after escalation, one time; a coarse summary in the ledger
    device_history: dict = field(default_factory=dict)   # {"recurring_unreachable_window": bool, "continuous_reachable_hours": float}
    # A worker on the shift roster but never seen on the network: exposure is weighted by this probability.
    presence_prior: float = 1.0
    # Foreign line + roaming: queries are served by the subscriber's HOME operator. If a migrant worker's
    # home-country operator does not offer Open Gateway, this worker is technically unreachable altogether.
    # Not "absent" but PERMANENTLY UNKNOWN — and counted separately in the coverage metric.
    reachable_via_operator: bool = True

    # --- decision state
    state: str = "passive_watch"
    cleared: bool = False
    escalated: bool = False
    verdict: str | None = None
    confidence: float | None = None
    score: float = 0.0
    vulnerability: float = 1.0
    queries_used: int = 0

    @property
    def masked(self) -> str:
        return mask_phone(self.phone)

    @property
    def phone_hash(self) -> str:
        return hash_phone(self.phone)

    def to_signals(self) -> dict:
        """The dict view the rule functions expect (plain data, no raw number)."""
        return {
            "worker_id": self.worker_id, "masked": self.masked, "name": self.name, "micro_zone": self.micro_zone,
            "zone_label": self.zone_label, "nationality": self.nationality, "trade": self.trade, "crew": self.crew,
            "first_day_on_site": self.first_day_on_site, "prior_incident": self.prior_incident, "shift": self.shift,
            "entered_at": _iso(self.entered_at),
            "last_signal_at": _iso(self.last_signal_at), "last_verified_at": _iso(self.last_verified_at),
            "verified_inside": self.verified_inside, "last_reachability_at": _iso(self.last_reachability_at),
            "reachable": self.reachable, "location_retrieved": self.location_retrieved,
            "cleared": self.cleared, "escalated": self.escalated, "score": self.score, "vulnerability": self.vulnerability,
            "presence_prior": self.presence_prior, "reachable_via_operator": self.reachable_via_operator,
            "queries_used": self.queries_used,
        }

    def to_dict(self) -> dict:
        d = self.to_signals()
        d.update({
            "phone_masked": self.masked, "phone_hash": self.phone_hash, "inside": self.inside,
            "exited_at": _iso(self.exited_at), "state": self.state, "verdict": self.verdict,
            "confidence": self.confidence, "queries_used": self.queries_used, "badge_in": self.badge_in,
            "device_history": self.device_history, "moving_out": self.moving_out,
            "last_location_masked": self.last_location,
        })
        return d


# --------------------------------------------------------------------------- site
@dataclass
class SiteRuntime:
    """A construction site's live state + evidence ledger (the compliance record is the product itself)."""

    site_id: str
    name: str
    lat: float
    lng: float
    radius_m: float
    cfg: Config
    workers: dict[str, WorkerRuntime] = field(default_factory=dict)
    state: str = "passive_watch"
    wbgt_c: float | None = None
    wbgt_at: datetime | None = None       # the moment the reading was TAKEN — staleness enters the decision
    breached: bool = False
    severity: float = 0.0
    level: str = "none"
    breach_started_at: datetime | None = None
    last_sweep_at: datetime | None = None
    last_reconcile_at: datetime | None = None
    sweeps: int = 0
    queries_total: int = 0
    ledger_pruned: int = 0              # number of rows dropped when the cap was hit (not silently)
    verifications_total: int = 0        # how many Location Verifications were made
    partial_verifications: int = 0      # how many returned PARTIAL — a measure of positioning uncertainty
    ledger: list[dict] = field(default_factory=list)
    congestion_cache: dict[str, tuple[datetime, str, str]] = field(default_factory=dict)  # zone → (t, level, source)
    qod_sessions: dict[str, dict] = field(default_factory=dict)
    # CAMARA wants a single event type per subscription → TWO subscriptions per worker.
    subscriptions: dict[str, str] = field(default_factory=dict)        # worker_id → area-entered subscription id
    subscriptions_left: dict[str, str] = field(default_factory=dict)   # worker_id → area-left subscription id
    # CONCURRENCY: writers (sweep, adding a worker, event, WBGT) hold this lock — per site, so that
    # a slow operator call on one site does not stall the others. Readers do NOT hold the lock: the dicts
    # that grow outside the sweep (workers, subscriptions*, qod_sessions) are changed not in place but by
    # copying and reassigning; the dict a reader holds never changes.
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)
    # A planner specific to this site (demo/test). None → the process-wide planner. Patching an environment
    # variable or a class method used to affect sweeps across the WHOLE process.
    planner: Any = field(default=None, repr=False, compare=False)

    # ---- evidence ledger
    def log(self, now: datetime, event: str, detail: str, *, worker: WorkerRuntime | None = None,
            source: str = "rules", explain: list | None = None, extra: dict | None = None) -> dict:
        entry = {
            # Pruned rows are counted too: a number identifies a row for its whole life, it never goes back.
            "seq": self.ledger_pruned + len(self.ledger) + 1, "t": _iso(now), "site_id": self.site_id, "event": event, "detail": detail,
            "worker_id": worker.worker_id if worker else None, "worker_masked": worker.masked if worker else None,
            "source": source, "explain": [e.to_dict() if hasattr(e, "to_dict") else e for e in (explain or [])],
        }
        if extra:
            entry.update(extra)
        self.ledger.append(entry)
        return entry

    def prune_ledger(self, cfg: Config) -> int:
        """Keep the ledger at its cap; STORE THE NUMBER OF DROPPED ROWS.

        A privacy audit measured that our claim "no location history is kept" was WRONG: the ledger
        was never pruned and a single worker accumulated 11 timestamped presence records. The right
        answer is not to hide the ledger — that ledger is the product itself, the contractor's proof of
        compliance. The right answer is to keep it BOUNDED, COUNTABLE and declared.
        """
        cap = cfg.budget.ledger_max_entries
        excess = len(self.ledger) - cap
        if excess > 0:
            del self.ledger[:excess]
            self.ledger_pruned += excess
        return self.ledger_pruned

    def presence_record(self) -> dict:
        """Counts what the ledger ACTUALLY holds — a measurement, not a claim.

        We used to return a hard-coded zero as `location_history_size: 0`; that was wrong.
        """
        worker_entries = sum(1 for e in self.ledger if e.get("worker_id"))
        coords = sum(1 for e in self.ledger if e.get("coarse"))
        return {"ledger_entries": len(self.ledger), "ledger_pruned": self.ledger_pruned,
                "ledger_cap": self.cfg.budget.ledger_max_entries,
                "worker_presence_entries": worker_entries,
                "coordinates_stored": coords,
                "note": ("The ledger IS a per-worker presence record - that is the product, not a "
                         "side effect. It is bounded, counted, and every coordinate in it is coarsened "
                         "to 3 decimals. Production needs durable storage with a legal retention period.")}

    def coverage(self) -> dict:
        """§9A honest coverage metric: how many workers who badged in are visible on the network?"""
        badged = [w for w in self.workers.values() if w.badge_in]
        # "Visible" = a signal has actually been seen ON THE NETWORK. We do not look at the `inside` flag:
        # shift-roster reconciliation can set it without evidence (see _reconcile_roster), and the
        # coverage metric is this project's measure of honesty — it cannot be inflated by reconciliation.
        visible = [w for w in badged if w.last_signal_at is not None]
        # A worker on a foreign line is not missing but PERMANENTLY UNREACHABLE. Counted separately so
        # that the coverage rate does not lump "we couldn't find them" together with "we can never look".
        unreachable = [w for w in badged if not w.reachable_via_operator]
        pct = round(100 * len(visible) / len(badged)) if badged else 100
        return {"badged_in": len(badged), "network_visible": len(visible), "coverage_pct": pct,
                "missing": [w.worker_id for w in badged if w not in visible and w.reachable_via_operator],
                "permanently_unreachable": [w.worker_id for w in unreachable],
                "permanently_unreachable_pct": round(100 * len(unreachable) / len(badged)) if badged else 0}

    def to_dict(self) -> dict:
        return {
            "site_id": self.site_id, "name": self.name, "area": {"lat": self.lat, "lng": self.lng, "radius_m": self.radius_m},
            "state": self.state, "wbgt_c": self.wbgt_c, "breached": self.breached, "severity": self.severity,
            "level": self.level, "breach_started_at": _iso(self.breach_started_at), "last_sweep_at": _iso(self.last_sweep_at),
            "sweeps": self.sweeps, "queries_total": self.queries_total,
            # PARTIAL rate: a direct measure of how large the positioning uncertainty is relative
            # to the site radius. Real data to show in the demo.
            "verifications_total": self.verifications_total, "partial_verifications": self.partial_verifications,
            "partial_rate_pct": round(100 * self.partial_verifications / self.verifications_total)
            if self.verifications_total else 0,
            "last_reconcile_at": _iso(self.last_reconcile_at),
            "jurisdiction": self.cfg.jurisdiction.to_dict(),
            "workers": [w.to_dict() for w in self.workers.values()], "coverage": self.coverage(),
            "qod_sessions": self.qod_sessions, "subscriptions": self.subscriptions,
            "ledger_size": len(self.ledger),
        }


# --------------------------------------------------------------------------- free signals
def apply_geofence_event(site: SiteRuntime, worker_id: str, kind: str, now: datetime) -> dict:
    """Geofence event (area-entered / area-left) — THE NETWORK PUSHES, no query budget is spent (Move 1)."""
    w = site.workers.get(worker_id)
    if w is None:
        raise KeyError(worker_id)
    now = _dt(now)
    # NOT FAIL-OPEN: an unrecognised `kind` (e.g. the typo "area_left") used to count as an EXIT,
    # drop the worker from the queue and, via `exited_at`, permanently block reconciliation.
    # A typo must not erase a worker from the safety view.
    if kind not in ("enter", "area-entered", "exit", "left", "area-left"):
        raise ValueError(f"unknown geofence event kind: {kind!r}")
    # ORDERING GUARD: webhook delivery gives no ordering guarantee. A 09:00 exit delivered AFTER a
    # 10:00 entry used to throw the worker out and drop them from the queue. The older event is not
    # applied, but it is not hidden from the ledger either.
    if w.last_geofence_at is not None and now < w.last_geofence_at:
        return site.log(now, "geofence_out_of_order",
                        f"A {kind} event from {_iso(now)} arrived after a newer one ({_iso(w.last_geofence_at)}) "
                        f"— recorded, not applied", worker=w, source="geofencing-subscriptions",
                        extra={"kind": kind, "newer_event_at": _iso(w.last_geofence_at)})
    w.last_geofence_at = now
    if kind in ("enter", "area-entered"):
        w.inside, w.exited_at, w.cleared = True, None, False
        w.verified_inside, w.last_verified_at = True, now
        # Exposure counts from this moment: even if the breach began at 12:00, a worker who entered
        # at 12:30 has been exposed for 30 minutes at 13:00, not 60. `rules.exposure_window()` reads this field.
        w.entered_at = now
        detail = "entered the site (free geofence event)"
    else:
        w.inside, w.exited_at = False, now
        w.cleared, w.escalated, w.verdict, w.confidence = True, False, None, None
        w.verified_inside, w.last_verified_at = False, now
        w.state = "passive_watch"
        w.entered_at = None          # the exposure window of a worker who left is closed
        detail = "left the site — drops out of the queue, its budget returns to the pool"
    w.last_signal_at = now
    return site.log(now, f"geofence_{kind}", detail, worker=w, source="geofencing-subscriptions")


def sweep_due(site: SiteRuntime, now: datetime, cfg: Config | None = None) -> bool:
    """Sweep frequency depends on severity: marginal breach 10 min, severe 2 min (§8)."""
    cfg = cfg or site.cfg
    if site.last_sweep_at is None:
        return True
    interval = R.sweep_interval_minutes(site.level, cfg)
    return (_dt(now) - site.last_sweep_at) >= timedelta(minutes=interval)


# --------------------------------------------------------------------------- Nokia calls (only through the facade)
def _call(nac, name: str, *args, **kw):
    """facade.call → (data, source, latency_ms). Without a facade, (None, 'none', 0)."""
    if nac is None:
        return None, "none", 0
    r = nac.call(name, *args, **kw)
    return r.data, r.source, r.latency_ms


# --------------------------------------------------------------------------- LLM planner (BELOW the rules)
_PLANNER = None


def _planner():
    """A single planner for the lifetime of the process. Without a key, `RulesPlanner` is returned."""
    global _PLANNER
    if _PLANNER is None:
        from .llm_adapter import get_planner
        _PLANNER = get_planner()
    return _PLANNER


def reset_planner() -> None:
    """Force the planner choice to be made again, for test/demo (when an environment variable changes)."""
    global _PLANNER
    _PLANNER = None


def _rerank_with_model(site: SiteRuntime, plan: list, now: datetime, st: dict, cfg: Config) -> tuple[list, dict]:
    """Have the model propose a RE-ORDERING of the plan; pass it through the safety gate; log the decision.

    The model does NOT PRODUCE the plan. It sees the list the rules produced and proposes an order. `guard_plan`
    checks the proposal structurally: it cannot add a worker, pick an API, exceed the budget or produce duplicates.
    If rejected, the rules' order is applied and the reason for rejection is visible in the ledger.
    """
    p = site.planner or _planner()
    name = getattr(p, "name", "deterministic-rules")
    if getattr(p, "provider", "none") == "none" or len(plan) < 2:
        return plan, {"used": False, "planner": name, "reason": "deterministic order applied"}

    snapshot = {
        "wbgt_c": site.wbgt_c, "legal_limit_c": cfg.jurisdiction.wbgt_limit_c,
        "severity": st.get("severity"), "level": st.get("level"),
        "jurisdiction": cfg.jurisdiction.code, "workers_in_plan": len(plan),
        "budget_per_sweep": cfg.budget.queries_per_site_per_minute,
    }
    before = [_worker_id_of(a) for a in plan]
    out = p.propose(snapshot, plan)
    v = dict(p.last_verdict())
    after = [_worker_id_of(a) for a in out]
    v["planner"] = name
    v["order_before"], v["order_after"] = before, after

    if v.get("used"):
        moved = sum(1 for i, wid in enumerate(after) if i < len(before) and before[i] != wid)
        v["moved"] = moved
        detail = (f"The model reordered the queue ({moved} positions changed) — it passed the gate"
                  if moved else "The model confirmed the rules' order")
        site.log(now, "planner_accepted", detail, source=name,
                 extra={"note": v.get("note"), "why": v.get("why"), "latency_ms": v.get("latency_ms")})
    else:
        why = v.get("violation") or v.get("reason") or "unknown"
        site.log(now, "planner_rejected",
                 f"The model's proposal was NOT APPLIED ({why}) — the rules' order stands", source=name,
                 extra={"violation": v.get("violation"), "guard_detail": v.get("detail"),
                        "offending": v.get("offending"), "latency_ms": v.get("latency_ms")})
    return (out if v.get("used") else plan), v


def _reconcile_roster(site: SiteRuntime, now: datetime, cfg: Config, force: bool) -> list[str]:
    """Periodic reconciliation — the event stream is NOT THE SINGLE SOURCE OF TRUTH.

    A missed "entry" event makes a worker invisible to the system: nobody looks for them, because the
    system never knew they were there. The fix is not to seek a delivery guarantee (operator feedback,
    9 September 2026); the fix is reconciliation with the employer's shift roster: pull workers who
    badged in but were never seen on the network back into the queue.

    That way a missed event turns an "invisible worker" into a "worker whose state is UNKNOWN" — and the
    system already handles the latter correctly, because it does not count the unknown as safe.

    Runs at the START of every breach (force) and then at `reconcile_minutes` intervals.
    """
    if not force and site.last_reconcile_at is not None:
        if (now - site.last_reconcile_at) < timedelta(minutes=cfg.budget.reconcile_minutes):
            return []
    site.last_reconcile_at = now
    pulled: list[str] = []
    for w in site.workers.values():
        if not w.badge_in or w.escalated:
            continue
        if not w.reachable_via_operator:
            continue          # home operator unreachable — querying wastes budget; counted separately in coverage
        if w.exited_at is not None:
            continue                      # there is a real exit event — reconciliation does not call it back
        if w.last_signal_at is not None:
            continue                      # seen on the network; the event stream works for this worker
        if w.inside and not w.cleared:
            continue                      # already in the queue
        # WE DO NOT OPEN A SEPARATE RESERVE (an arbitrary split always ends up in the wrong ratio). It joins
        # the same queue; the presence prior sets the balance — being on the roster is not being on site.
        w.inside, w.cleared, w.state = True, False, "confirming"
        w.presence_prior = cfg.budget.presence_prior_roster_only
        pulled.append(w.worker_id)
        site.log(now, "roster_reconciliation",
                 "On the shift roster but never seen on the network — a missed entry event would make this "
                 "worker invisible. Pulled back into the queue as UNKNOWN, not assumed safe",
                 worker=w, source="worker-registry")
    if pulled:
        site.log(now, "reconciliation_summary",
                 f"Roster reconciliation pulled {len(pulled)} worker(s) back into the queue "
                 f"(the event stream is not treated as the single source of truth)",
                 source="rules", extra={"workers": pulled})
    return pulled


def _liveness_probe(site: SiteRuntime, w: WorkerRuntime, now: datetime, nac, cfg: Config, report: dict) -> bool | None:
    """Strict-freshness location query = liveness proof. Returns: True (alive) · False (no answer) · None (inconclusive).

    Why this probe: `device-reachability-status` reads the state the network HOLDS. When a device really
    goes off, the state flipping to "unreachable" depends on the periodic registration timer (a common
    default is 54 minutes + margin), so at the moment of a collapse it may tell us nothing. A strict-freshness
    location query, on the other hand, forces the network to produce a fresh fix; to do that it has to
    page the device. If an answer comes back, the device responded AT THAT MOMENT.

    CRITICAL CHECK: is the returned `lastLocationTime` really fresh? If a forty-minute-old stamp comes back,
    the network did not make a fresh fix and the whole premise of this probe collapses. In that case we
    return "inconclusive" and write what happened into the ledger — we do not silently count it as alive.
    """
    max_age = cfg.budget.strict_max_age_s
    data, source, ms = _call(nac, "location_verify", w.phone, site.lat, site.lng, site.radius_m, max_age)
    site.verifications_total += 1
    w.queries_used, site.queries_total = w.queries_used + 1, site.queries_total + 1
    res = (data or {}).get("verificationResult")
    ts = _dt((data or {}).get("lastLocationTime"))
    raw_age_s = (now - ts).total_seconds() if ts else None
    # A FUTURE-DATED STAMP IS NOT EVIDENCE. I used to clamp a negative age to zero; an audit measured
    # that this made the freshness threshold COMPLETELY ineffective (8 hours / 240 sweeps: the probe said
    # "alive" 200 times, zero collapse verdicts) and it was erasing a `probable_collapse` verdict with
    # 0.95 confidence. A clock inconsistency is not proof of liveness — it is rejected.
    CLOCK_SKEW_S = 5.0
    if raw_age_s is None:
        age_s, future = None, False
    elif raw_age_s < -CLOCK_SKEW_S:
        age_s, future = None, True
    else:
        age_s, future = max(0.0, raw_age_s), False
    fresh = age_s is not None and age_s <= max_age * 2      # is the stamp really fresh

    report["calls"].append({"worker_id": w.worker_id, "masked": w.masked, "api": "location-verification",
                            "pool": "liveness", "result": res, "source": source, "latency_ms": ms,
                            "freshness": "strict", "max_age_s": max_age,
                            "last_location_time": (data or {}).get("lastLocationTime"),
                            "answer_age_s": None if age_s is None else round(age_s, 1),
                            "reason": "liveness probe — a fresh fix requires the network to page the device"})

    if not _trusted(source) or res is None or res == "UNKNOWN":
        # UNKNOWN is counted as "not a trustworthy signal" on the main path too (see the location_verify branch).
        # The same answer being proof of liveness here was a direct contradiction.
        site.log(now, "liveness_unknown",
                 f"Liveness probe: no usable answer ({res}) — the device is NOT counted as alive",
                 worker=w, source=source)
        return None
    if future:
        site.log(now, "liveness_inconclusive",
                 f"The position timestamp is in the future by {abs(raw_age_s):.0f}s — a clock inconsistency, "
                 f"not liveness evidence. The device is NOT counted as alive",
                 worker=w, source=source, extra={"raw_age_s": round(raw_age_s, 1)})
        return None
    if age_s is None:
        # An answer came but with no position timestamp → the network did not produce a fresh fix.
        # NOT proof of liveness.
        site.log(now, "liveness_inconclusive",
                 "The network answered a fresh-fix request without a position timestamp — no fix was produced, "
                 "so this is NOT liveness evidence. The device is not counted as alive",
                 worker=w, source=source)
        return None
    if not fresh:
        site.log(now, "liveness_inconclusive",
                 f"The network answered but the position is {age_s:.0f}s old (limit {max_age * 2}s) — no fresh fix was "
                 f"produced, so this is NOT liveness evidence. Falling back to the reachability reading",
                 worker=w, source=source, extra={"answer_age_s": round(age_s, 1)})
        return None
    site.log(now, "liveness_confirmed",
             f"The device answered a fresh-fix request just now (fix age {age_s:.0f} s, limit {max_age * 2} s) — it responded "
             f"to paging, so this is not a collapse; the stale reachability reading is overruled",
             worker=w, source=source, extra={"answer_age_s": round(age_s, 1)})
    return True


def _worker_id_of(act) -> str:
    w = getattr(act, "worker", None) or {}
    return str(w.get("worker_id") or w.get("masked") or "")


def _trusted(source: str | None) -> bool:
    """Did the answer come from the real operator?

    Answers sourced from `error(...)` (the call failed) and `fixture-fallback(...)` (fell back to the backup)
    cannot produce a SAFETY VERDICT: "the API crashed" and "the worker is outside the zone" are not the same thing.
    This is the code counterpart of the product's claim that "an error never produces a 'safe' verdict".
    """
    s = source or ""
    return not (s.startswith("error(") or s.startswith("fixture-fallback("))


def _congestion(site: SiteRuntime, w: WorkerRuntime, now: datetime, nac, cfg: Config) -> tuple[str | None, str]:
    """Congestion per micro-zone; not asked again for `congestion_cache_minutes` (budget protection)."""
    cached = site.congestion_cache.get(w.micro_zone)
    if cached and (now - cached[0]) <= timedelta(minutes=cfg.budget.congestion_cache_minutes):
        return cached[1], cached[2] + "(cache)"
    data, source, _ = _call(nac, "congestion_query", w.phone)
    level = None
    if isinstance(data, list) and data:
        level = data[0].get("congestionLevel")
    elif isinstance(data, dict):
        level = data.get("congestionLevel")
    if level is not None:
        site.congestion_cache[w.micro_zone] = (now, level, source)
        w.queries_used += 1
        site.queries_total += 1
    return level, source


# --------------------------------------------------------------------------- sweep
def _new_report(site: SiteRuntime, now: datetime, st: dict, cfg: Config) -> dict[str, Any]:
    """The sweep report skeleton every stage below fills in."""
    report: dict[str, Any] = {
        "site_id": site.site_id, "now": _iso(now), "wbgt_c": site.wbgt_c, "breached": st["breached"],
        "wbgt_known": st.get("wbgt_known"), "wbgt_stale": st.get("wbgt_stale"),
        "wbgt_age_minutes": st.get("wbgt_age_minutes"), "wbgt_at": _iso(site.wbgt_at),
        # Did the breach arise from the WBGT threshold or from the ban hours — two different legal bases.
        "wbgt_breach": st.get("wbgt_breach"), "ban_breach": st.get("ban_breach"),
        "severity": st["severity"], "level": st["level"], "reason": st["reason"],
        "site_explain": [e.to_dict() for e in st["explain"]],
        "sweep_interval_min": R.sweep_interval_minutes(st["level"], cfg),
        "plan": [], "calls": [], "verdicts": [], "escalations": [],
        "budget": {"per_sweep": cfg.budget.queries_per_site_per_minute, "planned": 0, "spent": 0,
                   "skipped_for_budget": 0, "reserve_rechecks": 0},
        "jurisdiction": cfg.jurisdiction.to_dict(),
    }
    return report


def _quiet_sweep(site: SiteRuntime, now: datetime, st: dict, cfg: Config, report: dict) -> dict:
    """Move 1: no breach, no query authority. Nothing is queried; open cases survive a dead feed."""
    quiet_from = len(site.ledger)          # AT THE VERY START: rows created on this path also reach the report
    if not st.get("wbgt_known"):
        # NO READING or a STALE one — and this does NOT MEAN "the breach is over".
        # An audit measured that this path used to wipe all worker state: an open
        # `probable_collapse` case, with the medic already called, was silently closed just because
        # the meteorology feed went quiet, and the ledger wrote "Breach over".
        # A dead feed is not the end of a breach: OPEN CASES ARE PRESERVED, only no new
        # query authority arises (purpose limitation is preserved).
        site.log(now, "wbgt_feed_unusable", st["reason"], source="meteorology-feed",
                 explain=st["explain"],
                 extra={"wbgt_c": site.wbgt_c, "wbgt_age_minutes": st.get("wbgt_age_minutes"),
                        "max_age_minutes": cfg.budget.wbgt_max_age_minutes,
                        "open_cases_preserved": [w.worker_id for w in site.workers.values() if w.escalated]})
        report["note"] = ("Move 1 — the WBGT threshold could NOT be assessed (no usable reading). "
                          "No new query authority arises, open cases are NOT closed, and this is NOT a "
                          "statement that the site is safe.")
    else:
        if site.state != "passive_watch":
            site.log(now, "site_cleared", "Breach over — open cases closed, the authority to query lapsed",
                     source="rules", explain=st["explain"])
        site.state = "passive_watch"
        site.breach_started_at = None
        for w in site.workers.values():
            w.state, w.escalated, w.verdict, w.confidence, w.score = "passive_watch", False, None, None, 0.0
        report["note"] = "Move 1 — no breach: only free geofence events are watched; NO query is made."
    site.last_sweep_at, site.sweeps = now, site.sweeps + 1
    report["state"] = site.state
    report["workers"] = [w.to_dict() for w in site.workers.values()]
    report["ledger_added"] = site.ledger[quiet_from:]
    return report


def _score_and_plan(site: SiteRuntime, now: datetime, st: dict, cfg: Config, report: dict) -> list:
    """Move 2: score every worker still inside, plan the budget in risk order, let the model re-order."""
    candidates: list[dict] = []
    presumed_safe: list[dict] = []
    for w in site.workers.values():
        sig = w.to_signals()
        if w.inside and not w.cleared:
            d = R.exposure_detail(sig, site.breach_started_at, now, cfg, st["severity"])
            w.score = round(d.risk_score, 2)
            w.vulnerability = d.meta["vulnerability"]
            sig = w.to_signals()
            sig["exposure_explain"] = [e.to_dict() for e in d.explain]
            candidates.append(sig)
        else:
            presumed_safe.append(sig)

    budget = cfg.budget.queries_per_site_per_minute
    plan = R.plan_verification(candidates, budget, cfg, now, presumed_safe=presumed_safe,
                               breach_started_at=site.breach_started_at)
    unlimited = R.plan_verification(candidates, 10_000, cfg, now)

    # ---- Move 2b: the model sits BELOW the rules — it can only re-order the plan (llm_adapter.py)
    plan, report["planner"] = _rerank_with_model(site, plan, now, st, cfg)

    main_planned = [a for a in plan if a.pool == "main"]
    report["budget"].update({
        "planned": len(plan), "skipped_for_budget": max(0, len(unlimited) - len(main_planned)),
        "reserve_rechecks": len([a for a in plan if a.pool == "reserve"]),
    })
    report["plan"] = [a.to_dict() for a in plan]
    report["ranked"] = [{"worker_id": c["worker_id"], "masked": c["masked"], "score": c["score"],
                         "vulnerability": c["vulnerability"], "explain": c.get("exposure_explain", [])}
                        for c in sorted(candidates, key=lambda c: -c["score"])]
    if report["budget"]["skipped_for_budget"]:
        site.log(now, "budget_exhausted",
                 f"Budget exhausted: {report['budget']['skipped_for_budget']} workers were not queried in this sweep "
                 f"(the list did not end, the budget did — §8 Move 2)", source="rules")
    return plan


def _run_location_verify(site: SiteRuntime, now: datetime, w: WorkerRuntime, act, nac, cfg: Config, report: dict) -> None:
    """Rung 2/3 of the cost ladder: "inside the zone?" — a verdict, never a coordinate."""
    # FRESHNESS rung: loose → accept a cached answer (cheap); strict → force a fresh fix (expensive).
    # This is what sets the cost on the network, not "verdict or coordinate".
    strict = act.freshness == "strict"
    max_age = cfg.budget.strict_max_age_s if strict else cfg.budget.loose_max_age_s
    data, source, ms = _call(nac, "location_verify", w.phone, site.lat, site.lng, site.radius_m, max_age)
    res = (data or {}).get("verificationResult")
    if res is not None and not _trusted(source):
        # A fallback answer CANNOT drop the worker from the queue — otherwise a crashing API
        # silently produces a "safe" verdict. Counted as not verified; staleness keeps rising.
        site.log(now, "untrusted_signal", f"Answer from an untrusted source ({source}) — counted as not verified",
                 worker=w, source=source)
        res = None
    w.queries_used, site.queries_total = w.queries_used + 1, site.queries_total + 1
    site.verifications_total += 1
    if res is not None:
        # UNKNOWN and PARTIAL are answers (to protect the budget we do not ask again right away) but
        # they ARE NOT TRUSTWORTHY SIGNALS: `last_signal_at` is not updated → staleness keeps
        # rising, the worker moves up in the ranking.
        w.last_verified_at = now
        if res not in ("UNKNOWN", "PARTIAL"):
            w.last_signal_at = now
    if res == "TRUE":
        # `moving_out` is NOT TOUCHED: trajectory is a site signal (turnstile exit, shift supervisor
        # record) and cannot be derived from Location Verification. It used to be derived from PARTIAL;
        # the operator's correction removed that basis — instead of keeping a fake signal, we removed it.
        w.verified_inside, w.state = True, "confirming"
    elif res == "FALSE":
        w.verified_inside, w.inside, w.cleared, w.state = False, False, True, "passive_watch"
        site.log(now, "worker_cleared", "Location Verification: outside the zone — dropped out of the queue", worker=w, source=source)
    elif res == "PARTIAL":
        # PARTIAL is a GEOMETRIC statement: the uncertainty region INTERSECTS the area but is not
        # contained in it. It has NOTHING to do with movement. (It used to be interpreted here as
        # "moving toward the exit" — that was wrong; corrected by the operator.) If the positioning
        # uncertainty is on the same order as the site radius, PARTIAL becomes the dominant answer,
        # so it counts as "insufficient evidence": not verified, and the UNKNOWN IS NOT SAFE.
        site.partial_verifications += 1
        w.verified_inside, w.state = None, "confirming"
        site.log(now, "insufficient_evidence",
                 "PARTIAL — the uncertainty region intersects the zone without being contained in it. "
                 "Geometric, not a movement signal: counted as not verified, staleness keeps rising",
                 worker=w, source=source)
    else:  # UNKNOWN / error → count as not verified, try again on the next sweep
        w.verified_inside, w.state = None, "confirming"
    report["calls"].append({"worker_id": w.worker_id, "masked": w.masked, "api": "location-verification",
                            "pool": act.pool, "result": res, "source": source, "latency_ms": ms,
                            "reason": act.reason, "freshness": act.freshness, "max_age_s": max_age,
                            "last_location_time": (data or {}).get("lastLocationTime")})
    rung = "rung 3, fresh fix demanded" if strict else "rung 2, cached answer accepted"
    shown = (f"NO ANSWER — operator API error ({source}); counted as UNKNOWN, not as safe" if res is None
             else f"{res} ({rung}; coordinates NOT requested)")
    site.log(now, "location_verify", f"Inside the zone? → {shown}",
             worker=w, source=source, explain=act.explain,
             extra={"pool": act.pool, "freshness": act.freshness, "max_age_s": max_age,
                    "last_location_time": (data or {}).get("lastLocationTime")})


def _run_reachability(site: SiteRuntime, now: datetime, w: WorkerRuntime, act, nac, cfg: Config, report: dict) -> bool:
    """Is a worker verified inside still reachable? Returns True when the device went silent."""
    data, source, ms = _call(nac, "reachability", w.phone)
    reachable = (data or {}).get("reachable")
    if reachable is not None and not _trusted(source):
        site.log(now, "untrusted_signal", f"Answer from an untrusted source ({source}) — reachability counted as unknown",
                 worker=w, source=source)
        reachable = None
    w.reachable, w.last_reachability_at = reachable, now
    w.queries_used, site.queries_total = w.queries_used + 1, site.queries_total + 1
    if reachable:
        w.last_signal_at = now
        w.state = "confirming"
    report["calls"].append({"worker_id": w.worker_id, "masked": w.masked, "api": "device-reachability-status",
                            "pool": act.pool, "result": reachable, "source": source, "latency_ms": ms, "reason": act.reason})
    shown_r = ("yes" if reachable else "NO — the device went silent" if reachable is False
               else f"NO ANSWER — operator API error ({source}); counted as unknown")
    site.log(now, "reachability", f"Is the device reachable? → {shown_r}", worker=w, source=source, explain=act.explain)
    return not reachable


def _run_location_retrieve(site: SiteRuntime, now: datetime, w: WorkerRuntime, act, nac, cfg: Config, report: dict) -> None:
    """The one-time, privacy-gated coordinate for the medic after an escalation."""
    data, source, ms = _call(nac, "location_retrieve", w.phone, cfg.budget.strict_max_age_s)
    area = (data or {}).get("area") or {}
    c = area.get("center") or {}
    w.queries_used, site.queries_total = w.queries_used + 1, site.queries_total + 1
    # TWO CRACKS IN THE GATE (privacy audit):
    #  (a) if the call did not return a `center`, the flag was not set → as long as the worker stayed
    #      `escalated`, a coordinate was requested again ON EVERY SWEEP. Now an attempt also counts once.
    #  (b) `_trusted()` had not been applied to this branch → a fabricated coordinate from a fallback
    #      could have been stored as if real and shown to the medic.
    w.location_retrieved = True          # (a) the attempt was made; the one-time gate is closed
    if c and _trusted(source):
        # COARSENING AT THE SOURCE: the field name `last_location_masked` implied it was masked,
        # but the code did not round; the API was publishing full-precision coordinates.
        # The 3 decimals (~110 m) written to the ledger were right — the same measure is applied here.
        w.last_location = {"lat": round(float(c.get("latitude", 0)), 3),
                           "lng": round(float(c.get("longitude", 0)), 3),
                           "radius_m": area.get("radius"), "coarsened_decimals": 3,
                           "at": _iso(now), "purpose": "where the medic has to run (one time only)"}
    elif c:
        site.log(now, "untrusted_signal",
                 f"A coordinate arrived from an untrusted source ({source}) — NOT stored and NOT shown "
                 f"to the medic. A fabricated position is worse than none",
                 worker=w, source=source)
    report["calls"].append({"worker_id": w.worker_id, "masked": w.masked, "api": "location-retrieval",
                            "pool": act.pool, "result": bool(c), "source": source, "latency_ms": ms, "reason": act.reason})
    # If no center came back, (0, 0) is NOT WRITTEN: a fake coordinate both misleads the medic and
    # inflates the `coordinates_stored` counter.
    stored = w.last_location if (c and _trusted(source)) else None
    site.log(now, "location_retrieve",
             "A ONE-TIME coordinate after escalation — for the medic" if stored else
             "A ONE-TIME coordinate was requested after escalation — none usable came back; the gate is closed",
             worker=w, source=source, explain=act.explain,
             extra={"coarse": {"lat": stored["lat"], "lng": stored["lng"]} if stored else None})


def _execute_plan(site: SiteRuntime, now: datetime, plan: list, nac, cfg: Config, report: dict) -> list[WorkerRuntime]:
    """Move 3: run the plan in order. Returns the workers whose device went silent."""
    unreachable_now: list[WorkerRuntime] = []
    for act in plan:
        w = site.workers.get(act.worker.get("worker_id"))
        if w is None:
            continue
        if act.api == "location_verify":
            _run_location_verify(site, now, w, act, nac, cfg, report)
        elif act.api == "reachability":
            if _run_reachability(site, now, w, act, nac, cfg, report):
                unreachable_now.append(w)
        elif act.api == "location_retrieve":
            _run_location_retrieve(site, now, w, act, nac, cfg, report)
    return unreachable_now


def _request_qod(site: SiteRuntime, now: datetime, w: WorkerRuntime, nac, cfg: Config, report: dict) -> None:
    """Guaranteed bandwidth for the medic ↔ physician video call."""
    data, source, ms = _call(nac, "qod_create", w.phone, cfg.qod_app_server_ipv4, cfg.qod_profile, 900)
    # A QoD session request is also a network call: if it is not counted, the card arithmetic
    # does not add up (the jury added 83 + 7 and saw 89). Audit A / Y1.
    site.queries_total += 1
    sid = (data or {}).get("sessionId")
    if sid:
        site.qod_sessions = {**site.qod_sessions, w.worker_id: {
            "session_id": sid, "qos_profile": cfg.qod_profile,
            "status": (data or {}).get("qosStatus"), "source": source, "at": _iso(now)}}
        site.state = "emergency"
        site.log(now, "qod_session", f"Guaranteed bandwidth opened ({cfg.qod_profile}) — medic video assessment",
                 worker=w, source=source, extra={"session_id": sid})
    report["calls"].append({"worker_id": w.worker_id, "masked": w.masked, "api": "quality-on-demand",
                            "pool": "emergency", "result": sid, "source": source, "latency_ms": ms,
                            "reason": "medic ↔ physician video assessment"})


def _classify_silent(site: SiteRuntime, now: datetime, unreachable_now: list[WorkerRuntime], nac, cfg: Config, report: dict) -> None:
    """§9: for every silent device — collapse, network event, dead battery or left the site?"""
    for w in unreachable_now:
        zone_peers = [x for x in site.workers.values() if x.micro_zone == w.micro_zone and x.worker_id != w.worker_id]
        n_unreach = sum(1 for x in zone_peers if x.reachable is False)
        n_reach = sum(1 for x in zone_peers if x.reachable is True)
        level, csource = _congestion(site, w, now, nac, cfg)
        if not csource.endswith("(cache)"):
            report["calls"].append({"worker_id": w.worker_id, "masked": w.masked, "api": "congestion-insights",
                                    "pool": "distress", "result": level, "source": csource, "latency_ms": 0,
                                    "reason": f"micro-zone {w.micro_zone}: a network event or a medical one?"})
        # ---- LIVENESS PROBE (operator advice, 9 September 2026)
        # Device Reachability reads the state the network HOLDS; when a device really goes off, the state
        # flipping to "unreachable" depends on the periodic registration timer and can take up to an hour.
        # A strict-freshness location query, on the other hand, forces the network to produce a FRESH fix:
        # to do that it has to establish contact with the device, i.e. page it. If an answer comes back, the
        # device responded AT THAT MOMENT — far fresher proof of liveness than a stale state reading, and
        # without touching messaging at all, staying within our own six APIs.
        alive = _liveness_probe(site, w, now, nac, cfg, report)
        if alive is True and w.escalated:
            # The case is ALREADY open and the medic has been called. The device answering later does NOT
            # CLOSE the case: an alarm does not close itself because the condition cleared — it needs human
            # acknowledgement (the "return to normal, unacknowledged" principle of industrial alarm management).
            w.reachable, w.last_reachability_at, w.last_signal_at = True, now, now
            site.log(now, "liveness_after_escalation",
                     "The device answered a fresh-fix request AFTER the case was escalated. The case stays "
                     "open: an alarm does not close itself because the signal came back — a human closes it",
                     worker=w, source="rules")
            continue
        if alive is True:
            # The device answered paging → not a collapse. The unreachability was a stale reading.
            w.reachable, w.last_reachability_at, w.last_signal_at = True, now, now
            w.verdict, w.confidence, w.state = None, None, "confirming"
            continue
        res = R.classify_unreachable(w.to_signals(), n_reach, n_unreach, level, w.device_history, w.moving_out, cfg)
        w.verdict, w.confidence = res["verdict"], res["confidence"]
        esc = R.escalate(res["verdict"], res["confidence"], cfg, level)
        actions = list(esc.actions)
        if "notify_medic" in actions:
            w.escalated, w.state = True, "distress"
            site.state = "distress"
        elif w.state != "passive_watch":
            w.state = "alert"
        if "request_qod" in actions:
            _request_qod(site, now, w, nac, cfg, report)
        if "provision_slice" in actions:
            # MOCK: Network Slicing is a separate product in NaC; in the prototype it is represented
            # by QoD + congestion evidence.
            site.log(now, "slice_requested", "Congestion is high → a dedicated slice requested for site-safety traffic (MOCK)",
                     worker=w, source="congestion-insights")
        entry = site.log(now, "distress_verdict", f"{res['verdict']} (confidence {res['confidence']:.2f}) → {', '.join(actions)}",
                         worker=w, source="rules", explain=res["explain"],
                         extra={"verdict": res["verdict"], "confidence": res["confidence"], "actions": actions,
                                "congestion": level, "congestion_source": csource,
                                "cluster": {"reachable": n_reach, "unreachable": n_unreach}})
        report["verdicts"].append({
            "worker_id": w.worker_id, "masked": w.masked, "name": w.name, "verdict": res["verdict"],
            "confidence": res["confidence"], "actions": actions, "congestion": level, "score": w.score,
            "cluster": {"reachable": n_reach, "unreachable": n_unreach},
            "explain": [e.to_dict() for e in res["explain"]], "escalate_explain": [e.to_dict() for e in esc.explain],
            "ledger_seq": entry["seq"], "state": w.state,
        })
        if "notify_medic" in actions:
            report["escalations"].append({"worker_id": w.worker_id, "masked": w.masked, "name": w.name,
                                          "confidence": res["confidence"], "actions": actions, "score": w.score})


def _prolonged_exposure(site: SiteRuntime, now: datetime, cfg: Config, report: dict) -> None:
    """4b: escalate workers kept inside a breached zone too long, even if their phone still answers."""
    # All of the classification above relies on the device GOING SILENT. But a worker with the phone in
    # their pocket, fully charged and reachable, can also collapse — the network never sees it, no
    # silence signal ever arises. And the law already orders work to STOP.
    limit_min = cfg.distress.prolonged_exposure_minutes
    for w in site.workers.values():
        if w.cleared or w.escalated or not w.inside or w.verdict is not None:
            continue
        if w.verified_inside is False:
            continue                   # a worker VERIFIED to be outside the zone is not the subject of this rule
        _, exposure_min, _, _ = R.exposure_window(w.to_signals(), site.breach_started_at, now)
        if exposure_min < limit_min:
            continue
        esc = R.escalate("prolonged_exposure", 1.0, cfg, None)
        w.verdict, w.confidence, w.state = "prolonged_exposure", 1.0, "distress"
        confirmed = "verified inside" if w.verified_inside else "not confirmed outside"
        entry = site.log(now, "prolonged_exposure",
                         f"{confirmed.capitalize()} in a breached zone for {exposure_min:.0f} min "
                         f"(limit {limit_min}) while the device is still answering. No silence signal will "
                         f"ever fire here - but work should have stopped {exposure_min - limit_min:.0f} min ago",
                         worker=w, source="rules", explain=esc.explain,
                         extra={"exposure_minutes": round(exposure_min, 1), "limit_minutes": limit_min,
                                "actions": list(esc.actions), "reachable": w.reachable,
                                "verified_inside": w.verified_inside})
        report["verdicts"].append({
            "worker_id": w.worker_id, "masked": w.masked, "name": w.name, "verdict": "prolonged_exposure",
            "confidence": 1.0, "actions": list(esc.actions), "congestion": None, "score": w.score,
            "exposure_minutes": round(exposure_min, 1),
            "explain": [e.to_dict() for e in esc.explain],
            "escalate_explain": [e.to_dict() for e in esc.explain],
            "ledger_seq": entry["seq"], "state": w.state,
        })
        report["escalations"].append({"worker_id": w.worker_id, "masked": w.masked, "name": w.name,
                                      "confidence": 1.0, "actions": list(esc.actions), "score": w.score,
                                      "trigger": "prolonged_exposure"})


def _finish_report(site: SiteRuntime, now: datetime, cfg: Config, report: dict, ledger_from: int) -> dict:
    """Close the sweep: counters, the ledger slice, pruning, budget breakdown, final state."""
    site.last_sweep_at, site.sweeps = now, site.sweeps + 1
    # The report slice is taken BEFORE PRUNING: pruning deletes from the front and shifts the `ledger_from` index.
    report["ledger_added"] = site.ledger[ledger_from:]
    site.prune_ledger(cfg)
    report["presence_record"] = site.presence_record()
    report["budget"]["spent"] = len(report["calls"])   # includes off-plan emergency calls (congestion / QoD)
    # The budget cuts the PLAN; a silent device's liveness/congestion check and the QoD session are outside
    # the plan and are deliberately not cut. The total is not hidden; it is broken down by pool.
    by_pool: dict[str, int] = {}
    for c in report["calls"]:
        by_pool[c["pool"]] = by_pool.get(c["pool"], 0) + 1
    planned_spent = by_pool.get("main", 0) + by_pool.get("reserve", 0)
    report["budget"].update({"by_pool": by_pool, "planned_spent": planned_spent,
                             "outside_plan": report["budget"]["spent"] - planned_spent})
    report["state"] = site.state
    report["workers"] = [w.to_dict() for w in site.workers.values()]
    report["coverage"] = site.coverage()
    report["note"] = ("Move 2/3 — the budget was spent in rank order; a coordinate was taken only after escalation, once."
                      if report["calls"] else "There is a breach but no new worker to query (all of them freshly verified).")
    return report


def step(site: SiteRuntime, now: datetime, nac=None, cfg: Config | None = None, wbgt_c: float | None = None) -> dict:
    """One sweep: site state → scoring → plan → calls → classification → escalation → evidence ledger."""
    cfg = cfg or site.cfg
    now = _dt(now)
    # Align the local mock's clock with simulated time. If the fixture writes the real wall clock, the
    # timestamps it produces come "from the future" in a simulated scenario; the freshness check becomes meaningless.
    # In live mode `fx` only sits there as a fallback, so touching it is harmless.
    _fx = getattr(getattr(nac, "nac", None), "fx", None) or getattr(nac, "fx", None)
    if _fx is not None and hasattr(_fx, "set_clock"):
        _fx.set_clock(now)
    if wbgt_c is not None:
        site.wbgt_c, site.wbgt_at = wbgt_c, now

    # ---- 1) site state (WBGT threshold OR summer-ban hours)
    st = R.site_state(site.wbgt_c, now, cfg.jurisdiction, cfg, site.wbgt_at)
    site.breached, site.severity, site.level = st["breached"], st["severity"], st["level"]
    report = _new_report(site, now, st, cfg)

    # ---- Move 1: no breach, no query authority
    if not st["breached"]:
        return _quiet_sweep(site, now, st, cfg, report)

    ledger_from = len(site.ledger)
    breach_is_new = site.breach_started_at is None
    if breach_is_new:
        site.breach_started_at = now
        site.log(now, "breach_started", st["reason"], source="rules", explain=st["explain"],
                 extra={"wbgt_c": site.wbgt_c, "level": st["level"]})
    if site.state == "passive_watch":
        site.state = "alert"

    # ---- Move 1b: reconciliation with the shift roster. At the start of every breach, then periodically.
    reconciled = _reconcile_roster(site, now, cfg, force=breach_is_new)
    report["reconciled"] = reconciled

    plan = _score_and_plan(site, now, st, cfg, report)
    # ---- 3) Move 3: execute the plan in order (cost ladder)
    unreachable_now = _execute_plan(site, now, plan, nac, cfg, report)
    # ---- 4) §9: collapse, network, battery or exit?
    _classify_silent(site, now, unreachable_now, nac, cfg, report)
    # ---- 4b) prolonged exposure: silence is not the only signal
    _prolonged_exposure(site, now, cfg, report)
    return _finish_report(site, now, cfg, report, ledger_from)




def new_worker(worker_id: str, phone: str, **kw) -> WorkerRuntime:
    """Creates a worker record, normalising the raw number (the single place this happens)."""
    return WorkerRuntime(worker_id=worker_id, phone=normalize_phone(phone), **kw)
