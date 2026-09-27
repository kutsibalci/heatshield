"""HeatShield decision rules — PURE functions (side-effect free, tested with fixtures).

Input: signal dicts; output: Decision / Explain lists. NO API calls, NO clock reads (now is a parameter).

Functions (idea capture reference):
  site_state            §5.2 step 2 — WBGT threshold OR summer-ban hours → breach + severity
  exposure_score        §8 Move 2 — severity × exposure_minutes × staleness × vulnerability
                        (severity is SITE-wide; the other three are per WORKER — see exposure_window)
  plan_verification     §8 Move 2/3 — ranked budget spending, reserve, cost ladder
                        (tiebreak on equal score: stalest signal first, NOT alphabetical phone)
  classify_unreachable  §9 — cluster test, device history, trajectory → verdict + confidence
  escalate              §5.2 step 5 — notify_medic / request_qod / provision_slice / log_only
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Config, Jurisdiction
from .explain import Decision, Explain
from .timeutil import iso_z, to_utc as _dt

VERDICTS = ("probable_collapse", "probable_network", "probable_battery", "probable_left", "prolonged_exposure")
STATES = ("passive_watch", "alert", "confirming", "distress", "emergency")


# ----------------------------------------------------------------------------- helpers

def _minutes_between(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 60.0


def local_time(now: datetime, jur: Jurisdiction) -> datetime:
    """tz-aware → convert to the jurisdiction's local time; naive → already assumed to be local."""
    if now.tzinfo is None:
        return now
    return now.astimezone(timezone(timedelta(hours=jur.tz_offset_hours)))


# ----------------------------------------------------------------------------- 1) site state
def site_state(wbgt_c: float | None, now: datetime, jurisdiction: Jurisdiction, cfg: Config | None = None,
               wbgt_at: datetime | Any = None) -> dict:
    """WBGT threshold OR summer-ban hours → {"breached", "severity", "level", "reason", "explain"}.

    severity: 0 (no breach) · 1.0 (at the legal limit / summer ban) · 1 + (wbgt-limit)/severe_delta (threshold exceeded).
    level: none | marginal | severe (severe: wbgt ≥ limit + severe_delta_c).
    """
    cfg = cfg or Config(jurisdiction=jurisdiction)
    ex: list[Explain] = []
    lt = local_time(now, jurisdiction)
    limit = jurisdiction.wbgt_limit_c
    delta = cfg.budget.severe_delta_c

    # The AGE of the reading enters the decision: a stale reading is the same as no reading.
    age_min = _minutes_between(_dt(wbgt_at), now) if wbgt_at is not None else None
    stale = age_min is not None and age_min > cfg.budget.wbgt_max_age_minutes
    known = wbgt_c is not None and not stale
    wbgt_breach = known and wbgt_c > limit
    ex.append(Explain("wbgt_c", wbgt_c, 1.0, f"WBGT {wbgt_c} °C; legal limit {limit} °C ({jurisdiction.name})",
                      source="meteorology-feed", triggered=bool(wbgt_breach)))
    if not known:
        ex.append(Explain("wbgt_feed", {"age_minutes": None if age_min is None else round(age_min, 1),
                                        "max_age_minutes": cfg.budget.wbgt_max_age_minutes, "stale": stale},
                          1.0,
                          "No usable WBGT reading: the legal threshold CANNOT be assessed. This is not the same as "
                          "'no breach' - a dead feed must never be reported as safety",
                          source="meteorology-feed", triggered=True))

    ban = jurisdiction.summer_ban
    md = lt.strftime("%m-%d")
    hm = lt.strftime("%H:%M")
    in_season = ban.start <= md <= ban.end
    in_hours = ban.from_ <= hm < ban.to
    ban_breach = in_season and in_hours
    ex.append(Explain("summer_ban", {"date": md, "local_time": hm, "in_season": in_season, "in_hours": in_hours}, 1.0,
                      f"Summer ban {ban.start}…{ban.end} {ban.from_}–{ban.to} (local) — " + ("ACTIVE" if ban_breach else "not active"),
                      source="jurisdiction-config", triggered=ban_breach))

    breached = wbgt_breach or ban_breach
    if wbgt_breach:
        excess = wbgt_c - limit
        severity = 1.0 + excess / delta
        level = "severe" if excess >= delta else "marginal"
        reason = f"WBGT limit exceeded (+{excess:.1f} °C)"
    elif ban_breach:
        severity, level, reason = 1.0, "marginal", "summer ban hours (work legally prohibited)"
    elif not known:
        # We are outside summer-ban hours and there is no usable reading. We CANNOT DECLARE a breach — no
        # query authority arises, purpose limitation is preserved. But we CANNOT SAY "no breach" either: a
        # dead feed must not silently turn into a statement of safety. The state is reported explicitly
        # as "could not be assessed".
        severity, level = 0.0, "unknown"
        reason = (("WBGT reading is stale" if stale else "no WBGT reading")
                  + " — the legal threshold cannot be assessed. This is not a statement that the site is safe")
    else:
        severity, level, reason = 0.0, "none", "no breach — no authority to query, free geofence events only"
    return {"breached": breached, "severity": round(severity, 3), "level": level, "reason": reason,
            "wbgt_breach": wbgt_breach, "ban_breach": ban_breach,
            "wbgt_known": known, "wbgt_stale": stale,
            "wbgt_age_minutes": None if age_min is None else round(age_min, 1), "explain": ex}


def sweep_interval_minutes(level: str, cfg: Config) -> int:
    return int(cfg.budget.sweep_minutes["severe" if level == "severe" else "marginal"])


# ----------------------------------------------------------------------------- 2) exposure score
def vulnerability_weight(worker: dict, now: datetime, cfg: Config) -> tuple[float, list[Explain]]:
    """Static worker weight: first week > prior incident > shift transition > default (the highest one is taken)."""
    w = cfg.vulnerability
    ex: list[Explain] = []
    weight, why = w.default, "default"
    fd = _dt(worker.get("first_day_on_site"))
    days = (now - fd).days if fd else None
    first_week = days is not None and 0 <= days < w.first_week_days
    if first_week:
        weight, why = w.first_week, f"day {days} on site — not yet acclimatised"
    ex.append(Explain("first_week", days, w.first_week, "First-week worker (the group the deaths concentrate in)", source="worker-registry", triggered=first_week))
    pi = bool(worker.get("prior_incident"))
    if pi and w.prior_incident > weight:
        weight, why = w.prior_incident, "prior heat-stress incident"
    ex.append(Explain("prior_incident", pi, w.prior_incident, "Past heat-stress incident", source="worker-registry", triggered=pi))
    st = str(worker.get("shift") or "day") in ("night_to_day", "transition")
    if st and w.shift_transition > weight:
        weight, why = w.shift_transition, "night→day shift transition"
    ex.append(Explain("shift_transition", worker.get("shift") or "day", w.shift_transition, "Night→day shift transition", source="worker-registry", triggered=st))
    ex.append(Explain("vulnerability", weight, 1.0, f"Vulnerability weight: {why}", source="rules", triggered=weight > w.default))
    return weight, ex


# Signal fields that may carry the moment the worker ENTERED the site — tried in priority order.
# If the runtime (agent/policy.py) publishes one of these, the exposure clock runs from that moment.
# WHY `last_signal_at` / `last_verified_at` ARE NOT HERE: both become "now" both at the moment of entry
# and during a verification made during the breach — they cannot be told apart. Taking them for an
# entry on a verified worker and resetting the clock would understate the exposure of a worker "we
# CONFIRMED to be inside the zone"; that is the wrong side to err on for safety. If there is no field we
# stay cautious and use the breach start (exposure is never UNDERestimated).
ENTRY_SIGNAL_FIELDS = ("entered_at", "area_entered_at", "geofence_entered_at", "inside_since", "first_seen_at")


def worker_entered_at(worker: dict) -> tuple[datetime | None, str | None]:
    """The moment the worker entered the site + which field it came from (the first field found wins)."""
    for field_name in ENTRY_SIGNAL_FIELDS:
        dt = _dt(worker.get(field_name))
        if dt is not None:
            return dt, field_name
    return None, None


def exposure_window(worker: dict, breach_started_at: datetime | Any, now: datetime) -> tuple[datetime | None, float, str, str]:
    """Exposure window brought down to the PER-WORKER level → (start, minutes, source_field, reason).

    Exposure counts from the LATER of the site's breach start and the worker's own entry moment:
    if the breach began at 12:00 and the worker entered at 12:30, at 13:00 their exposure is 30 minutes, not 60.
    """
    b = _dt(breach_started_at)
    entered, field_name = worker_entered_at(worker)
    start, src = b, "breach_started_at"
    if entered is not None and (b is None or entered > b):
        start, src = entered, field_name
        why = f"Breach was already running, worker entered later ({field_name}) — the clock runs from entry"
    elif entered is not None:
        why = f"Worker was on site before the breach ({field_name}) — the clock runs from the breach start"
    else:
        why = "No signal reports the entry moment → cautious: breach start (exposure is never underestimated)"
    return start, max(1.0, _minutes_between(start, now) or 1.0), src, why


def staleness_minutes(worker: dict, now: datetime, cap: float | None = None) -> float:
    """Minutes since the last trustworthy signal. With no signal at all, counts as cap (or infinity)."""
    stale = _minutes_between(_dt(worker.get("last_signal_at")), now)
    if stale is None:
        return float(cap) if cap is not None else math.inf
    stale = max(0.0, stale)
    return min(float(cap), stale) if cap is not None else stale


def exposure_detail(worker: dict, breach_started_at: datetime | Any, now: datetime, cfg: Config, severity: float = 1.0) -> Decision:
    """score = severity × exposure_minutes × staleness × vulnerability (idea capture §8, Move 2).

    severity is a SITE property (WBGT is the same across the whole construction site) — deliberately not per worker.
    The other three components are per worker: exposure window, signal staleness, vulnerability.
    """
    b = _dt(breach_started_at)
    start, exposure_min, src, why = exposure_window(worker, b, now)
    last = _dt(worker.get("last_signal_at"))
    cap = cfg.distress.staleness_cap_minutes
    stale_min = staleness_minutes(worker, now, cap)
    staleness = 1.0 + stale_min / 10.0  # 0 min → 1.0 ; 30 min → 4.0
    vul, vex = vulnerability_weight(worker, now, cfg)
    # Presence prior: it is not certain that a worker never seen on the network IS on site.
    # We do not drop them from the queue (the unknown is not safe) but we weight their exposure by a probability.
    prior = float(worker.get("presence_prior", 1.0) or 1.0)
    effective_exposure = exposure_min * prior
    score = severity * effective_exposure * staleness * vul
    ex = [
        Explain("severity", severity, 1.0, "Breach severity (how far WBGT is above the limit) — SITE-wide, does not vary by worker",
                source="meteorology-feed", triggered=severity >= 2.0),
        Explain("exposure_minutes", round(exposure_min, 1), 1.0,
                f"This worker's own exposure is {exposure_min:.0f} min (no clearing signal) — {why}",
                source="rules", triggered=exposure_min >= 20),
        Explain("exposure_start", iso_z(start), 0.8, f"Exposure clock starts at: {src}", source="rules",
                triggered=src != "breach_started_at"),
        Explain("staleness", round(staleness, 2), 1.0, f"Last trustworthy signal {stale_min:.0f} min ago" if last else "No trustworthy signal at all", source="rules", triggered=stale_min >= 20),
        *([Explain("presence_prior", prior, 0.9,
                   "On the shift roster but never seen on the network - being on the roster is not the same as "
                   "being on site, so exposure is weighted by a presence probability rather than assumed",
                   source="worker-registry", triggered=True)] if prior != 1.0 else []),
        *vex,
    ]
    return Decision(decision="exposure_score", risk_score=score, rule="severity×exposure×staleness×vulnerability", explain=ex,
                    meta={"score": round(score, 2), "vulnerability": vul, "staleness_minutes": round(stale_min, 1),
                          "exposure_minutes": round(exposure_min, 1), "exposure_start": iso_z(start),
                          "effective_exposure_minutes": round(effective_exposure, 1), "presence_prior": prior,
                          "exposure_source": src})


def exposure_score(worker: dict, breach_started_at: datetime | Any, now: datetime, cfg: Config, severity: float = 1.0) -> float:
    return exposure_detail(worker, breach_started_at, now, cfg, severity).risk_score


# ----------------------------------------------------------------------------- 3) verification plan
@dataclass
class Action:
    """A single planned API call. api: location_verify | reachability | location_retrieve | congestion_query"""

    worker: dict
    api: str
    reason: str
    rank: int
    cost: int = 1
    pool: str = "main"           # main | reserve
    freshness: str | None = None  # for location_verify: "loose" (accept cache) | "strict" (fresh fix)
    explain: list[Explain] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"worker": self.worker.get("masked"), "api": self.api, "reason": self.reason, "rank": self.rank,
                "cost": self.cost, "pool": self.pool, "freshness": self.freshness,
                "explain": [e.to_dict() for e in self.explain]}


def _next_step_for(worker: dict, now: datetime, cfg: Config) -> tuple[str, str, str | None] | None:
    """Cost ladder — on the FRESHNESS axis (operator feedback, 9 September 2026).

      1. geofence events             push, no query             (outside this function)
      2. location_verify / loose     accept a cached answer     cheap: a database read
      3. location_verify / strict    forces a fresh fix         expensive: paging + RRC + measurement
      4. location_retrieve           coordinate, after escalation — the reason is PRIVACY, NOT COST

    The old ladder rested on the assumption "a verdict is cheap, a coordinate is expensive". What sets the
    cost on the network is not what is returned but the requested freshness: a Retrieval that can be
    answered from cache is cheap too. The fourth rung's place is now held by privacy — and that reason
    alone is already sufficient; it does not need to lean on a cost argument.

    Returns: (api, reason, freshness) — freshness is "loose" | "strict" only for location_verify.
    """
    if worker.get("cleared"):
        return None
    if worker.get("escalated"):
        if not worker.get("location_retrieved"):
            return "location_retrieve", "escalated — coordinates for where the medic has to run (once only, privacy-gated)", None
        return None
    v_at = _dt(worker.get("last_verified_at"))
    fresh = v_at is not None and (_minutes_between(v_at, now) or 0) <= cfg.budget.reverify_minutes
    if worker.get("verified_inside") is not True or not fresh:
        # If the cheap answer was already tried and came back ambiguous (UNKNOWN / PARTIAL), climb a rung:
        # ask for a fresh fix.
        if v_at is not None and worker.get("verified_inside") is None:
            return ("location_verify",
                    "the cached answer was ambiguous → demand a fresh fix (rung 3: forces a positioning procedure)",
                    "strict")
        return ("location_verify",
                "no exit event → still inside the zone? (rung 2: a cached answer is accepted, cheap)",
                "loose")
    r_at = _dt(worker.get("last_reachability_at"))
    r_fresh = r_at is not None and (_minutes_between(r_at, now) or 0) < 1
    if worker.get("reachable") is None or not r_fresh:
        return "reachability", "verified inside the zone → is the device reachable? (a confirmer, not a trigger)", None
    return None


def plan_verification(workers_without_exit: list[dict], budget: int, cfg: Config, now: datetime | None = None,
                      presumed_safe: list[dict] | None = None,
                      breach_started_at: datetime | Any = None) -> list[Action]:
    """Call list ranked by score and cut by the budget (idea capture §8).

    workers_without_exit: signal dicts, each containing a "score" (computed with exposure_score).
    budget: query allowance remaining this minute. A reserve_ratio share is set aside for presumed_safe re-checks.
    """
    now = now or datetime.now(timezone.utc)
    budget = max(0, int(budget))
    reserve = int(math.ceil(budget * cfg.budget.reserve_ratio)) if budget else 0
    usable = budget - reserve
    # TIEBREAK on equal score: stalest signal first (the worker with the highest information gain).
    # Previously the masked phone was here — alphabetical order is not a safety rationale;
    # when scores tied, the agent's claim to "put the riskiest first" effectively fell back to the alphabet.
    # The masked number remains only as the LAST resort (same score + same staleness → determinism).
    def _unseen_this_breach(w: dict) -> bool:
        """Has this worker been queried even once during this breach?"""
        v = _dt(w.get("last_verified_at"))
        if v is None:
            return True
        b = _dt(breach_started_at)
        return b is not None and v < b

    def _rank_key(w: dict) -> tuple:
        stale = staleness_minutes(w, now)   # NO cap: unseen for 45 min goes ahead of unseen for 30 min
        # STARVATION GUARD (measured: with 400 workers, 80% were NOT queried EVEN ONCE).
        # When staleness caps out at 30 minutes, everyone's staleness saturates and the ranking is decided
        # by vulnerability alone — the same cohort wins every sweep. The product's own sentence said
        # "nobody unseen for twenty minutes should have to wait"; the code did not do that at scale.
        # Hence a hard constraint: a worker NEVER queried during this breach goes ahead of one already queried.
        # ESCALATION FIRST: the coordinate the medic will run to does not wait until the never-queried
        # workers are done. On a 40-person site that call was being left outside the plan (measured).
        # The budget is still not exceeded; the coordinate gate is still one-time (see _next_step_for).
        if w.get("escalated") and not w.get("location_retrieved"):
            tier = 0
        else:
            tier = 1 if _unseen_this_breach(w) else 2
        return (tier, -(w.get("score") or 0.0), -stale, w.get("masked") or "")

    ranked = sorted(workers_without_exit, key=_rank_key)
    tied = {s for s, n in Counter((w.get("score") or 0.0) for w in ranked).items() if n > 1}
    actions: list[Action] = []
    spent = 0
    for rank, w in enumerate(ranked, start=1):
        step = _next_step_for(w, now, cfg)
        if step is None:
            continue
        api, why, freshness = step
        if spent + 1 > usable:
            break  # budget exhausted → the list is not done but we stop (Move 2)
        spent += 1
        score = round(w.get("score") or 0.0, 2)
        stale = staleness_minutes(w, now)
        ex = [
            Explain("exposure_score", score, 1.0, f"Rank #{rank} - {why}", source="rules", triggered=True),
            *([Explain("never_queried", True, 1.0,
                       "Not queried once since this breach began - takes absolute priority over anyone already "
                       "checked, however high their score. Nobody waits unseen while a verified worker is re-asked",
                       source="rules", triggered=True)] if _unseen_this_breach(w) and api != "location_retrieve" else []),
            *([Explain("escalation_first", True, 1.0,
                       "An escalated case goes to the front: the medic's coordinate does not wait behind "
                       "routine verification", source="rules", triggered=True)] if api == "location_retrieve" else []),
            Explain("budget", {"spent": spent, "usable": usable, "reserve": reserve}, 0.5, "Spent from the remaining budget", source="rules"),
        ]
        if (w.get("score") or 0.0) in tied:
            ex.append(Explain("tiebreak", {"stale_minutes": None if math.isinf(stale) else round(stale, 1), "tied_score": score}, 0.7,
                              "Scores tied → stalest signal first (no signal at all goes to the front); alphabetical phone order was NOT used",
                              source="rules", triggered=True))
        if freshness:
            ex.append(Explain("cost_ladder", {"rung": 3 if freshness == "strict" else 2, "freshness": freshness,
                                              "max_age_s": cfg.budget.strict_max_age_s if freshness == "strict" else cfg.budget.loose_max_age_s},
                              0.8, "Rung 3 forces a fresh positioning procedure (paging + RRC + measurement); rung 2 accepts a cached answer"
                              if freshness == "strict" else
                              "Rung 2 accepts a cached answer - cheap for the network, a database read",
                              source="rules", triggered=(freshness == "strict")))
        actions.append(Action(w, api, why, rank, 1, "main", freshness, ex))
    # reserve: re-verify the stalest presumed-safe workers (those with an exit event) (a stale exit = silent failure mode)
    if presumed_safe and reserve:
        safe_ranked = sorted(presumed_safe, key=lambda w: (_dt(w.get("last_signal_at")) or datetime.min.replace(tzinfo=timezone.utc)))
        for i, w in enumerate(safe_ranked[:reserve], start=1):
            actions.append(Action(w, "location_verify", "presumed-safe recheck (reserve) — a stale exit event is a silent failure mode", i, 1, "reserve", "loose", [
                Explain("reserve_recheck", w.get("last_signal_at"), 0.3, "An exit event is not trusted forever; 10% reserve", source="rules", triggered=True),
            ]))
    return actions


# ----------------------------------------------------------------------------- 4) collapse vs dead battery
def classify_unreachable(worker: dict, neighbours_reachable: int, neighbours_unreachable: int, congestion_level: str | None,
                         device_history: dict | None, last_verified_moving_out: bool, cfg: Config | None = None) -> dict:
    """Unreachable device: collapse, network, battery or exit? (idea capture §9)

    Returns: {"verdict", "confidence", "explain", "decision"}.
    """
    cfg = cfg or Config()
    d = cfg.distress
    hist = device_history or {}
    ex: list[Explain] = []
    cong = (congestion_level or "unknown")
    cluster = neighbours_unreachable >= d.cluster_min_unreachable
    high_cong = cong.lower() == "high"
    recurring = bool(hist.get("recurring_unreachable_window"))
    cont_h = float(hist.get("continuous_reachable_hours") or 0)
    stationary = hist.get("stationary_inside", True)

    ex.append(Explain("cluster_test", {"reachable": neighbours_reachable, "unreachable": neighbours_unreachable}, 1.0,
                      f"{neighbours_unreachable} silent, {neighbours_reachable} reachable in this micro-zone" + (f" — CLUSTER (≥{d.cluster_min_unreachable}): network event" if cluster else f" — below the cluster threshold of {d.cluster_min_unreachable}: not a network event by itself"),
                      source="device-reachability-status", triggered=cluster))
    ex.append(Explain("congestion", cong, 0.8, "Serving-cell congestion (Congestion Insights)", source="congestion-insights", triggered=high_cong))
    ex.append(Explain("device_history", {"recurring_unreachable_window": recurring, "continuous_reachable_hours": cont_h}, 0.6,
                      "A silence window that repeats daily, or a drop after hours of being up?", source="ledger-history", triggered=recurring or cont_h >= d.continuous_reachable_hours))
    ex.append(Explain("trajectory", {"moving_out": last_verified_moving_out, "stationary_inside": stationary}, 0.7,
                      "Was this worker on their way out, or stationary inside the zone? This comes from a SITE signal "
                      "(gate badge-out, supervisor record) - it cannot be derived from Location Verification: a PARTIAL "
                      "result is geometric, not a statement about movement",
                      source="worker-registry", triggered=last_verified_moving_out))

    if last_verified_moving_out:
        verdict, conf = "probable_left", 0.7 + (0.1 if not cluster else 0.0)
    elif cluster or high_cong:
        verdict = "probable_network"
        conf = 0.55 + 0.1 * min(neighbours_unreachable, 3) + (0.1 if high_cong else 0.0)
    elif recurring:
        verdict, conf = "probable_battery", 0.6 + (0.1 if neighbours_reachable >= 2 else 0.0)
    else:
        verdict = "probable_collapse"
        conf = 0.5
        if neighbours_reachable >= 2:
            conf += 0.15
        if cong.lower() == "low":
            conf += 0.1
        if cont_h >= d.continuous_reachable_hours:
            conf += 0.15
        if stationary:
            conf += 0.05
        vw = worker.get("vulnerability") or 1.0
        if vw > 1.0:
            conf += 0.05
    conf = round(max(0.0, min(0.95, conf)), 2)
    ex.append(Explain("verdict", verdict, 1.0, f"Verdict: {verdict} (confidence {conf:.2f})", source="rules", triggered=verdict == "probable_collapse"))
    dec = Decision(decision=verdict, risk_score=conf, rule="cluster+history+trajectory", explain=ex, meta={"confidence": conf})
    return {"verdict": verdict, "confidence": conf, "explain": ex, "decision": dec}


# ----------------------------------------------------------------------------- 5) escalation
def escalate(verdict: str, confidence: float, cfg: Config | None = None, congestion_level: str | None = None) -> Decision:
    """verdict + confidence → action list. Only the cases that survive go to a human (no alarm fatigue)."""
    cfg = cfg or Config()
    ex: list[Explain] = []
    actions: list[str] = []
    high_cong = (congestion_level or "").lower() == "high"
    if verdict == "probable_collapse" and confidence >= cfg.distress.collapse_min_confidence:
        actions = ["notify_medic", "request_qod", "location_retrieve"]
        if high_cong:
            actions.append("provision_slice")
        rule = "collapse_high_confidence"
        note = f"Probable collapse, confidence {confidence:.2f} ≥ {cfg.distress.collapse_min_confidence} → medic + guaranteed video channel"
    elif verdict == "probable_collapse":
        actions = ["recheck_reachability", "log_only"]
        rule = "collapse_low_confidence"
        note = f"Probable collapse but confidence is low ({confidence:.2f}) → recheck on the next sweep"
    elif verdict == "prolonged_exposure":
        # The device did NOT GO SILENT; the worker is still INSIDE and the legal threshold has been exceeded
        # for a long time. All our distress inference reads silence, but a worker with the phone in their
        # pocket and fully charged can also collapse — the network never sees it. And the law already
        # orders work to STOP.
        actions = ["notify_supervisor", "log_only"]
        rule = "prolonged_exposure"
        note = ("Still verified inside a breached zone well past the legal stop-work point. The device is "
                "answering, so no silence signal will ever fire - but the law requires work to have stopped. "
                "A worker can collapse with a working phone in their pocket; silence is not the only signal")
    elif verdict == "probable_network":
        actions = ["provision_slice", "log_only"] if high_cong else ["log_only"]
        rule = "network_event"
        note = "Network event: no alarm" + (" — congestion is high → a slice is proposed for safety traffic" if high_cong else "")
    elif verdict == "probable_battery":
        actions = ["notify_supervisor", "log_only"]
        rule = "battery"
        note = "Habitual silence window → inform the supervisor, no alarm"
    else:
        actions = ["log_only"]
        rule = "left_site"
        note = "Went dark while moving toward the exit → probably left the site"
    ex.append(Explain("verdict", verdict, 1.0, note, source="rules", triggered="notify_medic" in actions))
    ex.append(Explain("confidence", confidence, 1.0, f"Threshold {cfg.distress.collapse_min_confidence}", source="rules", triggered=confidence >= cfg.distress.collapse_min_confidence))
    if congestion_level is not None:
        ex.append(Explain("congestion", congestion_level, 0.5, "Congestion, for the slice decision", source="congestion-insights", triggered=high_cong))
    return Decision(decision=actions[0], risk_score=confidence, rule=rule, explain=ex, actions=actions)
