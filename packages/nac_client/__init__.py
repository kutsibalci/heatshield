"""nac_client — Nokia Network as Code sarmalayıcı.

Tek giriş noktası. Nokia API çağrıları bu paketin dışından ASLA doğrudan yapılmaz.

Özellikler:
- Gerçek endpoint path'leri (SDK v10.0.0'dan doğrulandı — docs/nac-api-reference.md)
- İki katmanlı auth: x-rapidapi-key + (passthrough API'lerde) Bearer OAuth2 token
- Timeout + retry + circuit breaker (resilience.py)
- Hata normalizasyonu (errors.py)
- Telefon maskeleme / hash (privacy.py) — loglarda ham numara yok
- Üç mod: fixture (in-memory), simulator (yerel mock HTTP), live (RapidAPI)
"""
from .client import NacClient, NacConfig, NacResult
from .errors import NacError, NacCircuitOpen, NacTimeout
from .fixtures import FixtureBackend, DEFAULT_PROFILE
from .privacy import mask_phone, hash_phone

__all__ = [
    "NacClient", "NacConfig", "NacResult",
    "NacError", "NacCircuitOpen", "NacTimeout",
    "FixtureBackend", "DEFAULT_PROFILE",
    "mask_phone", "hash_phone",
]
