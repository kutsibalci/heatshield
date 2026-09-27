"""Process-wide state and the services the routes share: configuration, the demo roster, the operator
client, the store, and site/worker setup.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fastapi import HTTPException

from . import security
from .common import ROOT, NacFacade, _mask_deep, build_nac
from nac_client import NacError
from nac_client.client import NacResult
from agent import SiteRuntime, WorkerRuntime, new_worker
from rules import Config
from rules.timeutil import iso_z as _iso, to_utc as _parse_dt  # noqa: F401  (re-exported to routes)

if TYPE_CHECKING:
    from .models import SiteIn, WorkerIn

log = logging.getLogger("heatshield.api")

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8000")

# Anonymous creation caps: the public instance has 512 MB; ~5.3 KB per worker was measured (audit D).
# Demo scenarios are not subject to this cap — they build their own site from scratch and clear the store.
MAX_SITES = int(os.environ.get("HS_MAX_SITES") or 50)
MAX_WORKERS_PER_SITE = int(os.environ.get("HS_MAX_WORKERS_PER_SITE") or 500)
# Webhook dedupe window: the oldest ID is dropped (FIFO). Previously, when the window filled up, the set was
# wiped ENTIRELY — any event redelivered at that moment was applied again.
DEDUPE_WINDOW = 10_000
CFG = Config.from_env()

# The demo site and roster are DATA, not code: fixtures/roster.json. It holds 13 named
# scenario workers (their behaviour frozen) and a crowd of 227 — 240 in total, the scale of a real Qatari
# mega-project site. The network-side mock is in fixtures/profiles.json; the coordinates are
# the real Lusail Marina District (sources in roster.json `_sources`).
ROSTER = json.loads((ROOT / "fixtures" / "roster.json").read_text(encoding="utf-8"))
SITE = {"lat": ROSTER["site"]["lat"], "lng": ROSTER["site"]["lng"], "radius_m": ROSTER["site"]["radius_m"]}
SITE_NAME = ROSTER["site"]["name"]
ZONES = ROSTER["zones"]
# (worker_id, phone, spec) — call sites using `for _, phone, *_ in NAMED` keep working.
NAMED = [(w["worker_id"], w["phone"], w) for w in ROSTER["named"]]
CROWD = ROSTER["crowd"]
# Ban-hour rotation (see `_ban_hour_rotation`) — the roster splits into three states:
#   rest_rotation        leaves the perimeter at 10:00, re-enters at the shaded rest area at 10:15.
#                        Two FREE geofence events; because the network says "inside", their staleness
#                        is zero and they drop to the bottom of the queue. They do NOT drop OUT of the queue.
#   welfare_compound     spends the whole ban window in the welfare compound OUTSIDE the perimeter
#                        (the site's own heat-stress policy: the unacclimatised crew and those with a
#                        heat-incident record). There is an exit event → "presumed safe" → re-checked
#                        from the 10% reserve, because a stale exit event is a silent failure mode.
#   unseen_since_morning workers with not a single network signal since the morning entry — the group
#                        the budget is really spent on.
ROTATION_IDS = frozenset(c["worker_id"] for c in CROWD if c.get("status") == "rest_rotation")
WELFARE_IDS = frozenset(c["worker_id"] for c in CROWD if c.get("status") == "welfare_compound")

GEO_ENTERED = "org.camaraproject.geofencing-subscriptions.v0.area-entered"
GEO_LEFT = "org.camaraproject.geofencing-subscriptions.v0.area-left"

nac = build_nac()


# ============================================================================ safe facade
class SafeFacade(NacFacade):
    """If a Nokia call fails the sweep does not crash: the signal becomes 'unknown',
    the source reads `error(<kind>)`."""

    def call(self, name: str, *args, **kwargs):
        try:
            return super().call(name, *args, **kwargs)
        except NacError as e:
            log.warning("nac degraded api=%s kind=%s", name, e.kind)
            # The first 200 characters of the operator's 4xx/5xx body go into `NacError.message` and
            # from there spread to `/v1/state` — unmasked. The log path was covered by `MaskingFilter`,
            # but this path goes through the RESPONSE, not the log.
            store.record_degraded(_mask_deep({"t": _iso(_now()), "call": name, **e.to_dict()}))
            return NacResult(api=name, data={}, source=f"error({e.kind})", latency_ms=0, correlator=str(uuid.uuid4()))


# ============================================================================ helpers
def _now() -> datetime:
    return datetime.now(timezone.utc)




# ============================================================================ store
class Store:
    """Application state. Lock discipline:

    - `store.lock` protects the store's STRUCTURE (sites, subs, phone_index, dedupe) and keeps demo scenarios
      apart from each other. It is held briefly; outside demos it is NOT held during an operator call.
    - `site.lock` protects one site's state (sweep, adding workers, events, WBGT).
    - Order: site.lock → store.lock is allowed, the reverse NEVER. So a deadlock cannot occur.
    - Readers hold no lock: containers are not mutated in place; they are copied and reassigned.
    """

    def __init__(self) -> None:
        # The lock is created ONCE. When it was recreated inside `reset()`, those waiting on the old lock
        # and those taking the new one were writing at the same time.
        self.lock = threading.RLock()
        self._degraded_lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.sites: dict[str, SiteRuntime] = {}
        self.subs: dict[str, tuple[str, str]] = {}          # subscription_id → (site_id, worker_id)
        self.phone_index: dict[str, tuple[str, str]] = {}   # phone_hash → (site_id, worker_id)
        self.seen_cloudevent_ids: OrderedDict[str, None] = OrderedDict()
        self.degraded: tuple[dict, ...] = ()

    def record_degraded(self, item: dict) -> None:
        with self._degraded_lock:                # bounded: /v1/state shows the last 10
            self.degraded = (*self.degraded, item)[-200:]

    def site_or_404(self, sid: str) -> SiteRuntime:
        # A brief `store.lock`: nobody outside may write to a demo's site while the demo is building it.
        with self.lock:
            s = self.sites.get(sid)
        if not s:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "site not found"})
        return s


store = Store()
facade = SafeFacade(nac)


# ============================================================================ trigger stamp
def _stamp_trigger(report: dict, site: SiteRuntime, at: datetime, trigger: str) -> dict:
    """WHO started this sweep? Written into the report and into EVERY ledger row that sweep produced.

    The auditor reads the ledger on its own; the report is not kept. The answer to "did the agent decide, or did
    somebody press a button" must be inside the row: `trigger: scheduler` (the agent's own cadence) ·
    `api` (a sweep triggered from outside) · `demo` (the scenario on the jury screen).
    """
    report["trigger"] = trigger
    for e in report.get("ledger_added") or []:
        e["trigger"] = trigger
    if trigger == "scheduler":
        entry = site.log(
            at, "sweep_scheduled",
            f"Automatic sweep — the agent's own {report.get('sweep_interval_min')}-minute cadence came due. "
            f"Nobody pressed a button.",
            source="scheduler",
            extra={"trigger": trigger, "breached": report.get("breached"), "level": report.get("level"),
                   "queries": len(report.get("calls") or [])})
        report.setdefault("ledger_added", []).append(entry)
    return report


# ============================================================================ setup
def _site_cfg(code: str) -> Config:
    return CFG.with_jurisdiction(code.upper())


def _create_site(body: SiteIn, at: datetime | None = None) -> SiteRuntime:
    cfg = _site_cfg(body.jurisdiction)
    site = SiteRuntime(site_id=f"site-{uuid.uuid4().hex[:6]}", name=body.name, lat=body.lat, lng=body.lng,
                       radius_m=max(float(body.radius_m), CFG.min_perimeter_radius_m), cfg=cfg)
    with store.lock:
        store.sites = {**store.sites, site.site_id: site}
    site.log(at or _now(), "site_registered", f"{site.name} — {cfg.jurisdiction.name}: {cfg.jurisdiction.legal_ref}", source="jurisdiction-config")
    return site


def _add_worker(site: SiteRuntime, body: WorkerIn) -> WorkerRuntime:
    w = new_worker(body.worker_id, body.phone, name=body.name, micro_zone=body.micro_zone,
                   zone_label=body.zone_label or body.micro_zone, nationality=body.nationality,
                   trade=body.trade, crew=body.crew,
                   first_day_on_site=body.first_day_on_site, prior_incident=body.prior_incident,
                   shift=body.shift, badge_in=body.badge_in, device_history=dict(body.device_history),
                   moving_out=body.moving_out, reachable_via_operator=body.reachable_via_operator)
    # The caller holds `site.lock`. Containers are copied and reassigned: a lock-free reader never
    # iterates a changing dict.
    site.workers = {**site.workers, w.worker_id: w}
    with store.lock:
        store.phone_index = {**store.phone_index, w.phone_hash: (site.site_id, w.worker_id)}
    if body.subscribe:
        sink = f"{PUBLIC_BASE_URL.rstrip('/')}/webhooks/geofence"
        # CAMARA: ONE event type per subscription. SEPARATE subscriptions are opened for enter and exit —
        # sending both in one body returns 400 on the live platform (the Nokia SDK enforces this).
        for etype in (GEO_ENTERED, GEO_LEFT):
            r = facade.call("geofence_subscribe", w.phone, site.lat, site.lng, site.radius_m, sink,
                            types=[etype], sink_token=security.WEBHOOK_TOKEN or None)
            sid = (r.data or {}).get("id")
            if not sid:
                continue
            with store.lock:
                store.subs = {**store.subs, sid: (site.site_id, w.worker_id)}
            if etype == GEO_ENTERED:
                site.subscriptions = {**site.subscriptions, w.worker_id: sid}   # primary ID (backward compatible)
            else:
                site.subscriptions_left = {**site.subscriptions_left, w.worker_id: sid}
    return w
