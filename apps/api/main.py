"""HeatShield API — heat-stroke protection on construction sites.
"The network is the sensor; the worker does nothing."

Agent loop (idea capture §5.2):
  passive monitoring (free geofence events) → legal threshold breach →
  ranked queries under a verification budget →
  collapse / network / battery / exit discrimination → medic notification + guaranteed bandwidth (QoD).

The hard part of the product is not which API to call but **deciding whom to query**:
the sweep engine in `packages/agent/policy.py`, using the pure functions in `packages/rules`,
spends the budget in risk order on every sweep and also reports whom it did not spend it on.

Privacy / legal basis (idea capture §10):
- Without a breach there is **no authority to query** — the system cannot turn into a silent
  productivity-surveillance tool.
- The default query is an "inside the zone?" verdict (not a coordinate). A coordinate is taken only in a probable
  collapse case, only once, and is written to the ledger as a coarse summary.
- A raw phone number never appears in any response (mask + hash); logs are masked by `MaskingFilter`.
"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from . import runtime, security
from .common import mount_common
from .demo import DEMO_LIMITER, router as demo_router  # noqa: F401  (DEMO_LIMITER re-exported for tests)
from .models import SiteIn, SweepIn, WbgtIn, WorkerIn
from .runtime import (MAX_SITES, MAX_WORKERS_PER_SITE, NAMED, PUBLIC_BASE_URL,  # noqa: F401
                      _add_worker, _create_site, _iso, _now, _parse_dt, _stamp_trigger, facade, log, nac, store)
from .scheduler import scheduler
from .security import missing_production_secrets, require_api_token, require_read_access  # noqa: F401
from nac_client.privacy import hash_phone
from agent import apply_geofence_event, step, sweep_due
from rules import Config, JURISDICTIONS  # noqa: F401  (Config re-exported for tests)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """The app's lifetime = the agent's lifetime: the scheduler starts here and stops here.

    `lifespan` instead of `on_event("startup")`: this is the only guaranteed place to cancel the task
    on shutdown (with on_event a task was left hanging at shutdown).
    """
    mode = getattr(nac.cfg, "mode", "fixture")
    missing = missing_production_secrets(mode, os.environ)
    if missing:
        raise RuntimeError(f"NAC_MODE=live needs {', '.join(missing)} — refusing to start with generated secrets")
    if not security._API_TOKEN_FROM_ENV:
        # Local development convenience: a token was generated and is printed once. Live mode never gets here.
        log.warning("HS_API_TOKEN is not set; writes need this process-only token: %s", security.API_TOKEN)
    scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()


app = FastAPI(title="HeatShield — Heat-Stroke Protection on Construction Sites", version="0.1", lifespan=lifespan)

# The console uses no external resources; it has inline script/style (no build step). No CORS header is sent:
# browsers block cross-origin reads. /docs and /redoc are outside the CSP because they load Swagger from a CDN.
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError):
    """The 422 response does NOT ECHO the submitted value: (1) NaN/infinity cannot be written to JSON and the response
    turned into a 500, (2) a malformed worker record's name/number must not circulate in responses and logs."""
    errors = [{k: v for k, v in e.items() if k in ("type", "loc", "msg")} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if request.url.path not in ("/docs", "/redoc"):
        response.headers.setdefault("Content-Security-Policy", CSP)
    return response

mount_common(app, nac, Path(__file__).resolve().parents[1] / "web", debug_dependencies=[Depends(require_read_access)])

app.include_router(demo_router)


# ============================================================================ endpoints
@app.post("/v1/sites", status_code=201, dependencies=[Depends(require_api_token)])
def create_site(body: SiteIn):
    with store.lock:
        if len(store.sites) >= MAX_SITES:
            raise HTTPException(429, {"code": "SITE_CAP", "message": f"this instance holds at most {MAX_SITES} sites; "
                                      "run a demo scenario (it resets the store) or deploy your own copy"})
        return _create_site(body).to_dict()


@app.get("/v1/sites", dependencies=[Depends(require_read_access)])
def list_sites():
    return [s.to_dict() for s in store.sites.values()]


@app.get("/v1/sites/{site_id}", dependencies=[Depends(require_read_access)])
def get_site(site_id: str):
    return store.site_or_404(site_id).to_dict()


@app.post("/v1/sites/{site_id}/workers", status_code=201, dependencies=[Depends(require_api_token)])
def add_worker(site_id: str, body: WorkerIn):
    site = store.site_or_404(site_id)
    with site.lock:
        if body.worker_id in site.workers:
            # Silently overwriting left the old record's subscriptions orphaned.
            raise HTTPException(409, {"code": "ALREADY_EXISTS", "message": f"worker {body.worker_id} is already on this site"})
        if len(site.workers) >= MAX_WORKERS_PER_SITE:
            raise HTTPException(429, {"code": "WORKER_CAP", "message": f"a site on this instance holds at most "
                                      f"{MAX_WORKERS_PER_SITE} workers (measured full-coverage ceiling is ~110 anyway)"})
        try:
            return _add_worker(site, body).to_dict()
        except ValueError as e:
            raise HTTPException(400, {"code": "INVALID_ARGUMENT", "message": str(e)}) from e


@app.post("/v1/sites/{site_id}/wbgt", dependencies=[Depends(require_api_token)])
def set_wbgt(site_id: str, body: WbgtIn):
    """Meteorology feed (WBGT). The reading is logged to the ledger; the decision is made in the sweep."""
    site = store.site_or_404(site_id)
    with site.lock:
        at = _parse_dt(body.now) or _now()
        # The TIME of the reading is stored too: otherwise the staleness check never kicks in and the agent keeps
        # sweeping on a value from days ago.
        site.wbgt_c, site.wbgt_at = body.wbgt_c, at
        site.log(at, "wbgt_reading", f"WBGT {body.wbgt_c} °C (legal limit {site.cfg.jurisdiction.wbgt_limit_c} °C)",
                 source="meteorology-feed", extra={"wbgt_c": body.wbgt_c})
        return {"site_id": site.site_id, "wbgt_c": site.wbgt_c, "limit_c": site.cfg.jurisdiction.wbgt_limit_c}


@app.post("/v1/sites/{site_id}/sweep", dependencies=[Depends(require_api_token)])
def sweep(site_id: str, body: SweepIn | None = None):
    """Runs one sweep and returns the full report (plan, calls, decisions, budget, ledger additions)."""
    body = body or SweepIn()
    site = store.site_or_404(site_id)
    with site.lock:
        at = _parse_dt(body.now) or _now()
        if not body.force and not sweep_due(site, at, site.cfg):
            return {"skipped": True, "reason": f"sweep cadence not due yet ({site.level})", "last_sweep_at": _iso(site.last_sweep_at)}
        return _stamp_trigger(step(site, at, facade, site.cfg, body.wbgt_c), site, at, "api")


@app.get("/v1/sites/{site_id}/ledger", dependencies=[Depends(require_read_access)])
def ledger(site_id: str, worker_id: str | None = None, event: str | None = None, limit: int = Query(200, le=1000)):
    """Evidence ledger — a timestamped, per-worker compliance record for the auditor/insurer."""
    site = store.site_or_404(site_id)
    items = [e for e in list(site.ledger) if (not worker_id or e.get("worker_id") == worker_id) and (not event or e["event"] == event)]
    return {"site_id": site_id, "count": len(items), "items": items[-limit:], "coverage": site.coverage()}


@app.get("/v1/jurisdictions")
def jurisdictions():
    """Regional scalability: a jurisdiction is a configuration record, not a rewrite."""
    return {code: j.to_dict() for code, j in JURISDICTIONS.items()}


@app.get("/v1/state", dependencies=[Depends(require_read_access)])
def state():
    return {
        "now": _iso(_now()), "config": runtime.CFG.to_dict(),
        "sites": [s.to_dict() for s in store.sites.values()],
        "subscriptions": [{"id": sid, "site_id": s, "worker_id": w} for sid, (s, w) in store.subs.items()],
        "degraded": list(store.degraded[-10:]),
        "webhook": {"sink": f"{PUBLIC_BASE_URL.rstrip('/')}/webhooks/geofence", "token_required": True},
        "scheduler": scheduler.to_dict(),   # the agent's own clock: is it on, when did the last tick run
        # THIS USED TO BE A HARD-CODED 0 saying "no continuous location history is kept" — THAT WAS WRONG.
        # The ledger is a timestamped per-worker presence record; instead of hiding that, we COUNT it.
        "presence_record": {sid: s.presence_record() for sid, s in store.sites.items()},
    }


@app.get("/v1/scheduler")
def scheduler_state():
    """The agent's own clock: is it on, how often it wakes, when the last tick ran, how many errors it has taken."""
    return scheduler.to_dict()


@app.get("/v1/config")
def config():
    return runtime.CFG.to_dict()


@app.post("/v1/reset", dependencies=[Depends(require_api_token)])
def reset():
    """Wipes all evidence ledgers — which is why it requires the admin token (the webhook key is not enough).

    If the compliance record is the product itself, letting anyone wipe it is an integrity hole.
    """
    with store.lock:
        store.reset()
    return {"ok": True}


# ============================================================================ webhook (free signal)
@app.post("/webhooks/geofence")
def webhook_geofence(ev: dict = Body(...), authorization: Optional[str] = Header(default=None)):
    """Sink of the geofencing subscription. The network pushes → no query budget is spent (§8 Move 1)."""
    if not security._bearer_ok(authorization, security.WEBHOOK_TOKEN):
        raise HTTPException(401, {"code": "UNAUTHENTICATED", "message": "invalid webhook token"})
    if not isinstance(ev, dict) or "type" not in ev:
        raise HTTPException(400, {"code": "INVALID_ARGUMENT", "message": "a CloudEvent is expected"})
    with store.lock:                        # only lookup + dedupe; the event is applied under the site lock
        data = ev.get("data") or {}
        found = store.subs.get(data.get("subscriptionId") or "")
        if not found:
            phone = (data.get("device") or {}).get("phoneNumber")
            found = store.phone_index.get(hash_phone(phone)) if phone else None
        if not found:
            raise HTTPException(404, {"code": "UNKNOWN_SUBSCRIPTION", "message": "no subscription/worker matched"})
        # Without an `id`, the event's content becomes its ID: a random ID counted two deliveries
        # of one event as two events.
        ce_id = ev.get("id") or "sha256:" + hashlib.sha256(json.dumps(ev, sort_keys=True, default=str).encode()).hexdigest()
        if ce_id in store.seen_cloudevent_ids:
            return {"ok": True, "duplicate": True}
        store.seen_cloudevent_ids[ce_id] = None
        while len(store.seen_cloudevent_ids) > runtime.DEDUPE_WINDOW:
            store.seen_cloudevent_ids.popitem(last=False)
        etype = ev.get("type", "")
        if etype.endswith("subscription-ends"):
            return {"ok": True, "handled": "subscription-ends"}
        if etype.endswith("area-entered"):
            kind = "enter"
        elif etype.endswith("area-left"):
            kind = "exit"
        else:
            raise HTTPException(400, {"code": "UNSUPPORTED_TYPE", "message": f"unsupported CloudEvent type: {etype}"})
        site_id, worker_id = found
        site = store.sites.get(site_id)
    if site is None:
        raise HTTPException(404, {"code": "UNKNOWN_SUBSCRIPTION", "message": "the site for this subscription is gone"})
    with site.lock:
        entry = apply_geofence_event(site, worker_id, kind, _parse_dt(ev.get("time")) or _now())
        return {"ok": True, "site_id": site_id, "worker_id": worker_id, "kind": kind, "ledger": entry, "cost": 0}


@app.get("/", include_in_schema=False)
def root():
    """The bare address should go to the UI: the jury (and users) don't know the '/demo' suffix.
    The JSON info is at /v1/about."""
    return RedirectResponse(url="/demo", status_code=302)


@app.get("/v1/about")
def about():
    return {"app": "heatshield", "motto": "The network is the sensor; the worker does nothing.", "demo": "/demo", "health": "/health",
            "scenarios": ["heat-day", "collapse", "no-breach", "jurisdiction", "api-down", "planner-guard"],
            "jurisdictions": list(JURISDICTIONS)}
