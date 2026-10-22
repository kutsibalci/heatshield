"""Shared by every project's API: settings, the NacClient singleton, fixture fallback, demo static files,
debug endpoints."""
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

try:  # load .env if present (optional dependency)
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


# Fields that must NEVER appear in plain text in the debug panel. A privacy audit
# found `sinkCredential.accessToken` in plain text 106 times in the `/v1/_debug/calls` response and
# confirmed the attack chain: the token could be harvested and used to forge geofence events.
_SECRET_KEYS = {"accesstoken", "authorization", "apikey", "x-rapidapi-key", "sinkcredential",
                "credential", "password", "secret", "token", "bearer"}


def _mask_deep(value):
    """Masks every E.164 number AND every credential in a nested structure (for debug output)."""
    if isinstance(value, dict):
        return {k: ("***redacted***" if str(k).lower() in _SECRET_KEYS else _mask_deep(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_deep(v) for v in value]
    if isinstance(value, str):
        return _PHONE_RE.sub(lambda m: mask_phone(m.group(0)), value)
    return value


def _coarse_profile(prof: dict) -> dict:
    """Profile for the debug view: the location is coarsened to 3 decimals (~110 m).

    The product's own principle says "a coordinate is written to the ledger as a coarse summary"; a debug
    endpoint returning more detailed data than that would make the principle meaningless.
    """
    out = dict(prof)
    loc = out.get("location")
    if isinstance(loc, dict) and loc.get("lat") is not None:
        out["location"] = {**loc, "lat": round(float(loc["lat"]), 3), "lng": round(float(loc["lng"]), 3),
                           "coarsened": True}
    return out


def build_nac() -> NacClient:
    """Client according to NAC_MODE. Loads the fixtures in HTTP modes too → a fallback if the API goes down."""
    cfg = NacConfig.from_env()
    if not cfg.fixture_path:
        cfg.fixture_path = str(FIXTURE_PATH)
    fx = FixtureBackend.from_json(cfg.fixture_path) if Path(cfg.fixture_path).exists() else FixtureBackend()
    return NacClient(cfg, fixtures=fx)


class NacFacade:
    """The thin layer the decision engine uses.

    Falling back is OFF BY DEFAULT. When the operator API goes down, producing a made-up answer from
    fixtures erases the difference between "the API crashed" and "the worker is outside the zone", and
    directly contradicts the product's safety stance ("an error never produces a 'safe' verdict").
    If deliberately wanted it is enabled with `NAC_FALLBACK_TO_FIXTURE=1`; even then the decision
    layer, via `agent.policy._trusted()`, keeps these answers from producing a safety verdict.
    """

    def __init__(self, nac: NacClient):
        self.nac = nac
        self.fallback = NacClient(NacConfig(mode="fixture"), fixtures=nac.fx) if nac.fx is not None else None
        self.fallback_enabled = os.environ.get("NAC_FALLBACK_TO_FIXTURE", "0") == "1"

    def call(self, name: str, *args, **kwargs):
        """Calls nac.<name>(*args); on error with a fallback available, returns from fixtures and
        marks source='fixture-fallback'."""
        try:
            return getattr(self.nac, name)(*args, **kwargs)
        except NacError as e:
            if self.fallback_enabled and self.fallback is not None:
                res = getattr(self.fallback, name)(*args, **kwargs)
                res.source = f"fixture-fallback({e.kind})"
                return res
            raise


def mount_common(app: FastAPI, nac: NacClient, web_dir: Path | None = None, debug_dependencies: list | None = None) -> None:
    web_dir = web_dir or (ROOT / "apps" / "web")
    r = APIRouter()
    debug = APIRouter(dependencies=debug_dependencies or [])   # debug endpoints count as personal data

    @r.get("/health")
    def health():
        return {"ok": True, "nac": nac.health(), "fixture_path": str(FIXTURE_PATH), "profiles": len(nac.fx.profiles) if nac.fx else 0}

    @debug.get("/v1/_debug/calls")
    def debug_calls(limit: int = 30):
        # The debug panel stays open on the jury screen: the request body is already masked; we mask
        # the RESPONSE body too (device-status / QoD responses echo the `device.phoneNumber` field back verbatim).
        return [_mask_deep(c.to_dict()) for c in nac.calls[-limit:]]

    @debug.get("/v1/_debug/profiles")
    def debug_profiles():
        # This endpoint shows the fixture profiles and stays open on the jury screen. Its sibling (`_debug/calls`)
        # masked, this one did not: a privacy audit measured that we were dumping 27 raw numbers here, each with
        # its coordinate, without authentication. Our claim "a raw number never appears in any
        # response" collapsed with a single GET.
        #
        # The keys (phone numbers) are masked too; the `location` field inside is coarsened —
        # this is a debug view, not a location database.
        if not nac.fx:
            return {}
        return {mask_phone(p): _coarse_profile(prof) for p, prof in nac.fx.profiles.items()}

    @r.get("/demo")
    def demo():
        return FileResponse(web_dir / "index.html")

    app.include_router(r)
    app.include_router(debug)
    if web_dir.exists():
        app.mount("/static", StaticFiles(directory=web_dir), name="static")
