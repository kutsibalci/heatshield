"""nac_client — Nokia Network as Code wrapper.

Single entry point. Nokia API calls are NEVER made directly from outside this package.

Features:
- Real endpoint paths (verified against SDK v10.0.0 — docs/nac-api-reference.md)
- Two-layer auth: x-rapidapi-key + (on passthrough APIs) a Bearer OAuth2 token
- Timeout + retry + circuit breaker (resilience.py)
- Error normalization (errors.py)
- Phone masking / hashing (privacy.py) — no raw numbers in logs
- Three modes: fixture (in-memory), simulator (local mock HTTP), live (RapidAPI)
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
