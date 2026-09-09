"""rules — karar motoru. Saf fonksiyonlar, yan etkisiz, fixture ile test edilebilir.

Her karar `Explain` listesi üretir: hangi sinyal, hangi değer, hangi ağırlık, hangi kural.
Jüri "neden bu karar?" diye sorduğunda ekranda bu liste gösterilir.

HeatShield: config.py (Jurisdiction / bütçe / kırılganlık) + heatshield.py (site_state, exposure_score,
plan_verification, classify_unreachable, escalate).
"""
from .explain import Explain, Decision
from .config import Config, Jurisdiction, BudgetConfig, VulnerabilityWeights, DistressConfig, JURISDICTIONS, QATAR, SAUDI, UAE
from . import heatshield

__all__ = ["Explain", "Decision", "Config", "Jurisdiction", "BudgetConfig", "VulnerabilityWeights", "DistressConfig",
           "JURISDICTIONS", "QATAR", "SAUDI", "UAE", "heatshield"]
