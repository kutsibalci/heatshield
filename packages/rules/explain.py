from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Explain:
    """Tek bir sinyalin karara katkısı."""

    signal: str          # ör. "number_recycling"
    value: Any           # API'nin döndürdüğü (veya türetilen) değer
    weight: float        # 0..1 — karara etkisi
    note: str            # insan-okur açıklama (TR)
    source: str = ""     # api adı / fixture / simulator / live
    triggered: bool = False  # bu sinyal bir kuralı tetikledi mi

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Decision:
    """Genel karar zarfı. Projeler kendi `decision` sözlüğünü kullanır."""

    decision: str
    risk_score: float = 0.0
    rule: str = ""                    # tetiklenen kuralın adı
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
