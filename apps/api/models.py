"""Request bodies. Every field is bounded: these are the inputs anyone with a token can send."""
from __future__ import annotations

import json
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from . import runtime
from .runtime import CROWD, ROSTER


# ============================================================================ request models
class SiteIn(BaseModel):
    # The defaults are the same place as the demo site: the real Lusail Marina District coordinates
    # (sources in fixtures/roster.json `_sources`). The radius STAYS 500 m — this is the perimeter set against the
    # measured 1000 m location uncertainty, and all our claims rest on that ratio.
    name: str = Field(default=ROSTER["site"]["name"], max_length=120)
    # The default is the deployment's jurisdiction (HS_JURISDICTION) — not a hard-coded "QA".
    jurisdiction: str = Field(default_factory=lambda: runtime.CFG.jurisdiction.code, pattern="^(QA|SA|AE|qa|sa|ae)$")
    lat: float = Field(default=ROSTER["site"]["lat"], ge=-90, le=90)
    lng: float = Field(default=ROSTER["site"]["lng"], ge=-180, le=180)
    radius_m: float = Field(default=ROSTER["site"]["radius_m"], gt=0, le=50_000)


class WorkerIn(BaseModel):
    # Length limits: unbounded text fields could bloat memory and the evidence ledger.
    worker_id: str = Field(min_length=1, max_length=64)
    phone: str = Field(max_length=32)
    name: str = Field(default="", max_length=120)
    micro_zone: str = Field(default="z1", max_length=32)
    # Employer record fields — they do not enter the decision; they show on screen/in the ledger
    # who we are talking about.
    # (Nationality and trade are NOT used in SCORING; see agent/policy.py WorkerRuntime.)
    zone_label: str = Field(default="", max_length=64)
    nationality: str = Field(default="", max_length=64)
    trade: str = Field(default="", max_length=64)
    crew: str = Field(default="", max_length=64)
    first_day_on_site: str | None = Field(default=None, max_length=32)
    prior_incident: bool = False
    shift: str = Field(default="day", max_length=16)
    badge_in: bool = True
    device_history: dict = Field(default_factory=dict)
    moving_out: bool = False
    # Foreign line + roaming: if the home operator does not offer Open Gateway, this worker is technically
    # not reachable at all. Not "we couldn't find them" but PERMANENTLY UNKNOWN — counted separately
    # in the coverage metric.
    reachable_via_operator: bool = True
    subscribe: bool = True

    @field_validator("device_history")
    @classmethod
    def _small_history(cls, v: dict) -> dict:
        if len(json.dumps(v, default=str)) > 2_000:
            raise ValueError("device_history is limited to 2,000 characters")
        return v


WBGT_RANGE = {"ge": -20.0, "le": 60.0, "allow_inf_nan": False}   # WBGT cannot exceed the dry-bulb temperature


class SweepIn(BaseModel):
    now: datetime | None = None
    wbgt_c: float | None = Field(default=None, **WBGT_RANGE)
    force: bool = True   # False → the sweep cadence (sweep_due) is respected


class WbgtIn(BaseModel):
    wbgt_c: float = Field(**WBGT_RANGE)
    now: datetime | None = None


class DemoIn(BaseModel):
    reset: bool = True
    start: datetime | None = None
    # Default: the whole roster (227 crowd + 13 named workers = 240). The upper bound is
    # the size of the crowd in roster.json; the cap is memory protection on the public instance (finding D).
    fillers: int = Field(default=len(CROWD), ge=0, le=len(CROWD))
