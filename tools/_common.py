"""Araçların ortak başlangıcı: depo kökü, paket yolu, .env ve kanıt klasörü.

Eskiden her araç kendi `.env` okuyucusunu taşıyordu ve üçü yolu depo klasörünün ADINA bağlı
kuruyordu — depo başka bir adla klonlanınca kırılıyordu, çıktılarını da `evidence/` yerine
`tools/` altına yazıyordu.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
for _p in (ROOT / "apps", ROOT / "packages"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def require_live_flag(what: str) -> None:
    """Canlı API'ye giden araçlar yalnızca açık `--live` bayrağıyla çalışır.

    `.env` bulunursa araç gerçek anahtarla gerçek çağrı yapar ve kota harcar. Bir "deneme
    çalıştırması" bunu fark etmeden yapabiliyordu; bayrak olmadan hiçbir şey okunmaz, çağrılmaz.
    """
    if "--live" not in sys.argv[1:]:
        print(f"{what}\nThis tool calls a live API and spends quota. Re-run with --live to proceed.")
        sys.exit(2)


def load_env() -> Path | None:
    """Depodaki `.env`'i (yoksa bir üst klasördekini) yükler; kabukta tanımlı değerler kazanır."""
    from dotenv import load_dotenv

    for candidate in (ROOT / ".env", ROOT.parent / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=False)
            return candidate
    return None


def evidence_file(prefix: str, suffix: str = "json") -> Path:
    """`evidence/<prefix>-YYYYmmdd-HHMMSS.<suffix>` — araçların çıktısı düzenlenmeden buraya yazılır."""
    EVIDENCE.mkdir(exist_ok=True)
    return EVIDENCE / f"{prefix}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.{suffix}"
