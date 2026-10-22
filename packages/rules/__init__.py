"""rules — the decision engine. Pure functions, side-effect free, testable with fixtures.

Every decision produces an `Explain` list: which signal, which value, which weight, which rule.
When the jury asks "why this decision?", this list is shown on screen.

HeatShield: config.py (Jurisdiction / budget / vulnerability) + heatshield.py (site_state, exposure_score,
plan_verification, classify_unreachable, escalate).
"""
from .explain import Explain, Decision
from .config import Config, Jurisdiction, BudgetConfig, VulnerabilityWeights, DistressConfig, JURISDICTIONS, QATAR, SAUDI, UAE
from . import heatshield

__all__ = ["Explain", "Decision", "Config", "Jurisdiction", "BudgetConfig", "VulnerabilityWeights", "DistressConfig",
           "JURISDICTIONS", "QATAR", "SAUDI", "UAE", "heatshield"]
