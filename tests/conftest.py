"""Test ortamı — yol ayarı VE ortam yalıtımı.

Bir mutasyon denetimi şunu buldu: `conftest.py` yalnızca `sys.path` ayarlıyordu, ortamı
temizlemiyordu. `GEMINI_API_KEY` tanımlı bir makinede tarama testleri **gerçek Gemini'ye ağ
çağrısı yapıyordu**. Depoyu klonlayan bir jüri üyesinde tam olarak bu olur: testler yavaşlar,
kota harcar, hatta anahtarsız makinede farklı sonuç verir.

Testler dış dünyaya ÇIKMAZ. Bu dosya bunu garanti eder.
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "packages", ROOT / "apps", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# Testlerin davranışını değiştirebilecek her ortam değişkeni
_ISOLATED = (
    "GEMINI_API_KEY", "GROQ_API_KEY", "HS_PLANNER", "HS_GEMINI_MODEL", "HS_GROQ_MODEL",
    "HS_SCHEDULER", "HS_SCHEDULER_TICK_S", "HS_QUERY_UNIT_COST_USD",
    "NAC_RAPIDAPI_KEY", "NAC_OAUTH_TOKEN", "NAC_BASE_URL",
)


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch):
    """Her testten önce ortamı belirlenimci hale getir.

    `NAC_MODE` ve `NAC_FALLBACK_TO_FIXTURE` açıkça sabitlenir: ortamdaki `NAC_MODE=live`
    eskiden `setdefault` yüzünden EZİLMİYORDU — yani canlı anahtarı olan bir makinede
    testler gerçek Nokia platformuna gidebilirdi.
    """
    for name in _ISOLATED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NAC_MODE", "fixture")
    monkeypatch.setenv("NAC_FALLBACK_TO_FIXTURE", "0")
    monkeypatch.setenv("NAC_FIXTURE_PATH", str(ROOT / "fixtures" / "profiles.json"))
    yield
