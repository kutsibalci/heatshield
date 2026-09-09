"""Tüm projelerin API'sinde ortak: ayarlar, NacClient tekil örneği, fixture yedeği, demo statik dosyaları, debug uçları."""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT / "packages", ROOT / "apps"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

try:  # .env varsa yükle (opsiyonel bağımlılık)
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

from nac_client import FixtureBackend, NacClient, NacConfig, NacError  # noqa: E402
from nac_client.privacy import MaskingFilter, mask_phone  # noqa: E402

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger().addFilter(MaskingFilter())
for h in logging.getLogger().handlers:
    h.addFilter(MaskingFilter())

FIXTURE_PATH = Path(os.environ.get("NAC_FIXTURE_PATH") or (ROOT / "fixtures" / "profiles.json"))

_PHONE_RE = re.compile(r"\+[0-9]{8,15}")


# Debug panelinde ASLA duz metin gorunmemesi gereken alanlar. Bir gizlilik denetimi
# `sinkCredential.accessToken`i `/v1/_debug/calls` yanitinda 106 kez duz metin buldu ve
# saldiri zincirini dogruladi: token hasat edilip sahte geofence olayi uretilebiliyordu.
_SECRET_KEYS = {"accesstoken", "authorization", "apikey", "x-rapidapi-key", "sinkcredential",
                "credential", "password", "secret", "token", "bearer"}


def _mask_deep(value):
    """İç içe yapıdaki tüm E.164 numaraları VE kimlik bilgilerini maskeler (debug çıktısı için)."""
    if isinstance(value, dict):
        return {k: ("***redacted***" if str(k).lower() in _SECRET_KEYS else _mask_deep(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_deep(v) for v in value]
    if isinstance(value, str):
        return _PHONE_RE.sub(lambda m: mask_phone(m.group(0)), value)
    return value


def _coarse_profile(prof: dict) -> dict:
    """Hata ayıklama görünümü için profil: konum 3 ondalığa (~110 m) kabalaştırılır.

    Ürünün kendi ilkesi "koordinat deftere kaba özet olarak yazılır" diyor; bir hata ayıklama
    ucunun bundan daha ayrıntılı veri döndürmesi o ilkeyi anlamsız kılar.
    """
    out = dict(prof)
    loc = out.get("location")
    if isinstance(loc, dict) and loc.get("lat") is not None:
        out["location"] = {**loc, "lat": round(float(loc["lat"]), 3), "lng": round(float(loc["lng"]), 3),
                           "coarsened": True}
    return out


def build_nac() -> NacClient:
    """NAC_MODE'a göre istemci. HTTP modlarında da fixture'ları yükler → API çökerse yedek."""
    cfg = NacConfig.from_env()
    if not cfg.fixture_path:
        cfg.fixture_path = str(FIXTURE_PATH)
    fx = FixtureBackend.from_json(cfg.fixture_path) if Path(cfg.fixture_path).exists() else FixtureBackend()
    return NacClient(cfg, fixtures=fx)


class NacFacade:
    """Karar motorunun kullandığı ince katman.

    Yedeğe düşme VARSAYILAN OLARAK KAPALIDIR. Operatör API'si düştüğünde fixture'dan uydurma
    bir cevap üretmek, "API çöktü" ile "işçi bölge dışında" arasındaki farkı siler ve ürünün
    güvenlik duruşuna ("hata asla 'güvende' hükmü üretmez") doğrudan aykırıdır.
    Bilinçli olarak istenirse `NAC_FALLBACK_TO_FIXTURE=1` ile açılır; o durumda bile karar
    katmanı `agent.policy._trusted()` ile bu cevapları güvenlik hükmü üretmekten alıkoyar.
    """

    def __init__(self, nac: NacClient):
        self.nac = nac
        self.fallback = NacClient(NacConfig(mode="fixture"), fixtures=nac.fx) if nac.fx is not None else None
        self.fallback_enabled = os.environ.get("NAC_FALLBACK_TO_FIXTURE", "0") == "1"

    def call(self, name: str, *args, **kwargs):
        """nac.<name>(*args) çağrısı; hata + yedek varsa fixture'dan döner ve source='fixture-fallback' işaretler."""
        try:
            return getattr(self.nac, name)(*args, **kwargs)
        except NacError as e:
            if self.fallback_enabled and self.fallback is not None:
                res = getattr(self.fallback, name)(*args, **kwargs)
                res.source = f"fixture-fallback({e.kind})"
                return res
            raise


def mount_common(app: FastAPI, nac: NacClient, web_dir: Path | None = None) -> None:
    web_dir = web_dir or (ROOT / "apps" / "web")
    r = APIRouter()

    @r.get("/health")
    def health():
        return {"ok": True, "nac": nac.health(), "fixture_path": str(FIXTURE_PATH), "profiles": len(nac.fx.profiles) if nac.fx else 0}

    @r.get("/v1/_debug/calls")
    def debug_calls(limit: int = 30):
        # Debug paneli jüri ekranında açık kalır: istek gövdesi zaten maskeli, YANIT gövdesini de maskeliyoruz
        # (device-status / QoD yanıtları `device.phoneNumber` alanını aynen geri döndürüyor).
        return [_mask_deep(c.to_dict()) for c in nac.calls[-limit:]]

    @r.get("/v1/_debug/profiles")
    def debug_profiles():
        # Bu uç fixture profillerini gösterir ve jüri ekranında açık kalır. Kardeş ucu (`_debug/calls`)
        # maskeliyordu, bu maskelemiyordu: bir gizlilik denetimi burada 27 ham numarayı ve her birinin
        # koordinatını kimlik doğrulaması olmadan döktüğümüzü ölçtü. "Ham numara hiçbir yanıtta yer
        # almaz" iddiamız tek GET ile çöküyordu.
        #
        # Anahtarlar da (telefon numaraları) maskelenir; içerideki `location` alanı ise kabalaştırılır —
        # bu bir hata ayıklama görünümü, konum veritabanı değil.
        if not nac.fx:
            return {}
        return {mask_phone(p): _coarse_profile(prof) for p, prof in nac.fx.profiles.items()}

    @r.get("/demo")
    def demo():
        return FileResponse(web_dir / "index.html")

    app.include_router(r)
    if web_dir.exists():
        app.mount("/static", StaticFiles(directory=web_dir), name="static")
