"""HeatShield threshold and budget configuration — nothing hard-coded, it is all here (+ env override).

Jurisdiction is plug-and-play: Qatar is the reference; Saudi Arabia and the UAE are configuration entries.
Source: idea capture §6 (regional scalability) and §8 (verification budget policy).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict, replace


@dataclass(frozen=True)
class SummerBan:
    """Summer ban: start/end "MM-DD", from_/to "HH:MM" (local time)."""

    start: str = "06-01"
    end: str = "09-15"
    from_: str = "10:00"
    to: str = "15:30"

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "from": self.from_, "to": self.to}


@dataclass(frozen=True)
class Jurisdiction:
    name: str
    code: str
    wbgt_limit_c: float = 32.1
    summer_ban: SummerBan = field(default_factory=SummerBan)
    tz_offset_hours: float = 3.0  # for local time (a naive datetime → assumed to be local)
    legal_ref: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name, "code": self.code, "wbgt_limit_c": self.wbgt_limit_c,
            "summer_ban": self.summer_ban.to_dict(), "tz_offset_hours": self.tz_offset_hours, "legal_ref": self.legal_ref,
        }


QATAR = Jurisdiction(
    name="Qatar", code="QA", wbgt_limit_c=32.1,
    summer_ban=SummerBan("06-01", "09-15", "10:00", "15:30"), tz_offset_hours=3.0,
    legal_ref="Ministerial Decision No. 17/2021 (WBGT > 32.1 °C year-round; 10:00–15:30 ban, 1 Jun – 15 Sep)",
)
SAUDI = Jurisdiction(
    name="Saudi Arabia", code="SA", wbgt_limit_c=32.1,
    summer_ban=SummerBan("06-15", "09-15", "12:00", "15:00"), tz_offset_hours=3.0,
    legal_ref="MHRSD midday ban (12:00–15:00, 15 Jun – 15 Sep); WBGT threshold kept identical to the Qatar reference",
)
UAE = Jurisdiction(
    name="United Arab Emirates", code="AE", wbgt_limit_c=32.1,
    summer_ban=SummerBan("06-15", "09-15", "12:30", "15:00"), tz_offset_hours=4.0,
    legal_ref="MoHRE midday ban (12:30–15:00, 15 Jun – 15 Sep); WBGT threshold kept identical to the Qatar reference",
)
JURISDICTIONS: dict[str, Jurisdiction] = {"QA": QATAR, "SA": SAUDI, "AE": UAE}


@dataclass(frozen=True)
class BudgetConfig:
    """Per-site query budget per minute (idea capture §8)."""

    queries_per_site_per_minute: int = 20
    reserve_ratio: float = 0.1                    # reserve for re-checking presumed-safe workers
    sweep_minutes: dict = field(default_factory=lambda: {"marginal": 10, "severe": 2})
    severe_delta_c: float = 2.0                   # limit + delta → "severe"
    reverify_minutes: int = 10                    # no repeat location_verify for a worker verified within this time
    congestion_cache_minutes: int = 5             # the congestion query is not repeated for the same micro-zone

    # --- the FRESHNESS axis of the cost ladder (operator feedback, 9 September 2026)
    # What sets the cost on the network is not "verdict or coordinate" but the REQUESTED FRESHNESS.
    # A query that can be answered from cache is cheap (a database read). If a fresh fix is needed,
    # the positioning procedure runs: paging + RRC setup + measurement + reporting — that is the real
    # signalling load, and it is independent of which endpoint you call.
    loose_max_age_s: int = 600                    # rung 2: accept a cached answer (cheap)
    strict_max_age_s: int = 60                    # rung 3: force a fresh fix (expensive)
    reconcile_minutes: int = 15                   # periodic reconciliation: sweep workers whose state is unknown
    # If the WBGT reading is older than this, the threshold CANNOT BE ASSESSED. Saying "no breach" when there
    # is no reading silently turns a dead feed into a "safe" verdict — this was the exact exception to the
    # product's claim that "an error never produces a safe verdict" (9 September audit finding).
    wbgt_max_age_minutes: int = 30
    # A worker on the shift roster but NEVER seen on the network: being on the roster is not being
    # on site. Instead of opening a separate reserve (an arbitrary split always ends up in the wrong
    # ratio) they join the same queue and their exposure is multiplied by this prior — a defined value
    # instead of a special case.
    presence_prior_roster_only: float = 0.5
    # The evidence ledger IS the product, so it is not deleted — but in an in-memory prototype it cannot
    # grow without bound: an 8-hour shift with 400 workers produces 7,341 rows / 13.9 MB (measured).
    # Production needs durable storage + a legal retention period; here we set an explicit cap and
    # COUNT how many rows were dropped. Silent pruning is unacceptable in an evidence ledger.
    ledger_max_entries: int = 5000

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class VulnerabilityWeights:
    first_week: float = 2.0
    prior_incident: float = 1.5
    shift_transition: float = 1.3
    default: float = 1.0
    first_week_days: int = 7

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DistressConfig:
    """Collapse-vs-dead-battery classification thresholds (idea capture §9)."""

    cluster_min_unreachable: int = 2       # if ≥ this many neighbours are silent too → network event
    collapse_min_confidence: float = 0.6   # lower bound for notifying the medic
    continuous_reachable_hours: float = 6  # continuously reachable for this long + a drop → suspicious
    staleness_cap_minutes: int = 30
    # PROLONGED EXPOSURE — silence is not the only signal.
    # All distress inference relied on the device GOING SILENT. But a worker with the phone in their
    # pocket, fully charged and reachable, can also collapse; the network never sees it. And the law
    # already orders work to STOP: a worker still verified inside while the threshold is exceeded is,
    # even with a working phone, both a compliance breach and a health risk. Past this duration,
    # escalation INDEPENDENT of silence.
    prolonged_exposure_minutes: int = 45

    def to_dict(self) -> dict:
        return asdict(self)


def _with_limit(jur: Jurisdiction, limit_c: float | None) -> Jurisdiction:
    return jur if limit_c is None else replace(jur, wbgt_limit_c=limit_c)


@dataclass
class Config:
    jurisdiction: Jurisdiction = QATAR
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    vulnerability: VulnerabilityWeights = field(default_factory=VulnerabilityWeights)
    distress: DistressConfig = field(default_factory=DistressConfig)
    # 9 September 2026 live measurement: a QoD session with QOS_E + 203.0.113.10 returned 200 (a sessionId came back).
    # An RFC1918 (10.x) address was not tried; since there is no documented proof it is accepted, we moved
    # the default to the value measured to WORK live. 203.0.113.0/24 is the RFC 5737 documentation range —
    # not a real server, but a routable address format.
    qod_profile: str = "QOS_E"
    qod_app_server_ipv4: str = "203.0.113.10"   # medic video server (demo; RFC 5737)
    min_perimeter_radius_m: float = 500.0
    # HS_WBGT_LIMIT_C: the deployment's own threshold. Stored here because every site is built with
    # `with_jurisdiction()`; otherwise changing the jurisdiction silently reverted to the legal value.
    wbgt_limit_override_c: float | None = None

    @classmethod
    def from_env(cls, jurisdiction_code: str | None = None) -> "Config":
        e = os.environ.get
        jur = JURISDICTIONS.get((jurisdiction_code or e("HS_JURISDICTION", "QA")).upper(), QATAR)
        override = float(e("HS_WBGT_LIMIT_C")) if e("HS_WBGT_LIMIT_C") else None
        budget = BudgetConfig(
            queries_per_site_per_minute=int(e("HS_BUDGET_PER_MIN", "20")),
            reserve_ratio=float(e("HS_RESERVE_RATIO", "0.1")),
            sweep_minutes={"marginal": int(e("HS_SWEEP_MARGINAL_MIN", "10")), "severe": int(e("HS_SWEEP_SEVERE_MIN", "2"))},
            severe_delta_c=float(e("HS_SEVERE_DELTA_C", "2.0")),
        )
        vul = VulnerabilityWeights(
            first_week=float(e("HS_W_FIRST_WEEK", "2.0")),
            prior_incident=float(e("HS_W_PRIOR_INCIDENT", "1.5")),
            shift_transition=float(e("HS_W_SHIFT_TRANSITION", "1.3")),
        )
        return cls(jurisdiction=_with_limit(jur, override), budget=budget, vulnerability=vul,
                   qod_profile=e("HS_QOD_PROFILE", "QOS_E"), qod_app_server_ipv4=e("HS_QOD_APP_SERVER", "203.0.113.10"),
                   wbgt_limit_override_c=override)

    def with_jurisdiction(self, code: str) -> "Config":
        jur = JURISDICTIONS.get(code.upper(), self.jurisdiction)
        return replace(self, jurisdiction=_with_limit(jur, self.wbgt_limit_override_c))

    def to_dict(self) -> dict:
        return {
            "jurisdiction": self.jurisdiction.to_dict(), "budget": self.budget.to_dict(),
            "vulnerability": self.vulnerability.to_dict(), "distress": self.distress.to_dict(),
            "qod_profile": self.qod_profile,
        }
