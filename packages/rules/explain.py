from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Explain:
    """A single signal's contribution to the decision."""

    signal: str          # e.g. "number_recycling"
    value: Any           # the value the API returned (or a derived one)
    weight: float        # 0..1 — its effect on the decision
    note: str            # human-readable explanation
    source: str = ""     # api name / fixture / simulator / live
    triggered: bool = False  # did this signal trigger a rule

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Decision:
    """Generic decision envelope. Projects use their own `decision` vocabulary."""

    decision: str
    risk_score: float = 0.0
    rule: str = ""                    # name of the rule that fired
    explain: list[Explain] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "risk_score": round(self.risk_score, 3),
            "rule": self.rule,
            "explain": [e.to_dict() for e in self.explain],
            "actions": self.actions,
            "meta": self.meta,
        }
