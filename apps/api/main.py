"""HeatShield API — şantiyede sıcak çarpması koruması. "Şebeke sensördür; işçi hiçbir şey yapmaz."

Ajan döngüsü (idea capture §5.2):
  pasif izleme (ücretsiz geofence olayları) → yasal eşik ihlali → doğrulama bütçesiyle sıralı sorgu →
  bayılma/ağ/pil/çıkış ayrımı → sağlıkçı bildirimi + garantili bant genişliği (QoD).

Ürünün zor kısmı hangi API'nin çağrılacağı değil, **kimin sorgulanacağına karar vermek**:
`packages/agent/policy.py` içindeki tarama motoru, `packages/rules` içindeki saf fonksiyonlarla
her taramada bütçeyi risk sırasına göre harcar ve harcamadıklarını da rapor eder.

Gizlilik / hukuki dayanak (idea capture §10):
- İhlal yoksa **sorgu yetkisi yoktur** — sistem sessiz bir üretkenlik gözetimi aracına dönüşemez.
- Varsayılan sorgu "bölge içinde mi?" hükmüdür (koordinat değil). Koordinat yalnızca olası bayılma
  vakasında, bir kez alınır ve deftere kaba özet olarak yazılır.
- Ham telefon numarası hiçbir yanıtta yer almaz (maske + hash); loglar `MaskingFilter` ile maskelenir.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import Body, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from .common import NacFacade, _mask_deep, build_nac, mount_common  # noqa: E402  (sys.path'i de ayarlar)
from nac_client import NacError, mask_phone  # noqa: E402
from nac_client.client import NacResult  # noqa: E402
from nac_client.privacy import hash_phone, normalize_phone  # noqa: E402
from agent import SiteRuntime, WorkerRuntime, apply_geofence_event, new_worker, step, sweep_due  # noqa: E402
from rules import Config, JURISDICTIONS  # noqa: E402

log = logging.getLogger("heatshield.api")

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8000")
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "heatshield-dev-token")
CFG = Config.from_env()

GEO_ENTERED = "org.camaraproject.geofencing-subscriptions.v0.area-entered"
GEO_LEFT = "org.camaraproject.geofencing-subscriptions.v0.area-left"

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Uygulamanın ömrü = ajanın ömrü: zamanlayıcı burada başlar, burada durur.

    `on_event("startup")` yerine `lifespan`: kapanışta görevi iptal etmenin garantili tek yeri
    burasıdır (on_event'te shutdown'da asılı kalan bir görev kalıyordu).
    """
    scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()


app = FastAPI(title="HeatShield — Heat-Stroke Protection on Construction Sites", version="0.1", lifespan=lifespan)
nac = build_nac()
mount_common(app, nac, Path(__file__).resolve().parents[1] / "web")


# ============================================================================ güvenli facade
class SafeFacade(NacFacade):
    """Nokia çağrısı düşerse tarama çökmez: sinyal 'bilinmiyor' olur, kaynak `error(<kind>)` yazılır."""

    def call(self, name: str, *args, **kwargs):
        try:
            return super().call(name, *args, **kwargs)
        except NacError as e:
            log.warning("nac degraded api=%s kind=%s", name, e.kind)
            # Operatorun 4xx/5xx govdesinin ilk 200 karakteri `NacError.message` icine giriyor ve
            # buradan `/v1/state`e yayiliyordu — maskesiz. Log yolu `MaskingFilter` ile kapaliydi
            # ama bu yol logdan degil YANITTAN geciyor.
            store.degraded.append(_mask_deep({"t": _iso(_now()), "call": name, **e.to_dict()}))
            return NacResult(api=name, data={}, source=f"error({e.kind})", latency_ms=0, correlator=str(uuid.uuid4()))


# ============================================================================ yardımcılar
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(v) -> datetime | None:
    if v is None:
        return None
    dt = v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


# ============================================================================ store
class Store:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.sites: dict[str, SiteRuntime] = {}
        self.subs: dict[str, tuple[str, str]] = {}   # subscription_id → (site_id, worker_id)
        self.seen_cloudevent_ids: set[str] = set()
        self.degraded: list[dict] = []
        self.lock = threading.RLock()

    def site_or_404(self, sid: str) -> SiteRuntime:
        s = self.sites.get(sid)
        if not s:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "site not found"})
        return s


store = Store()
facade = SafeFacade(nac)


# ============================================================================ tetikleyici damgası
def _stamp_trigger(report: dict, site: SiteRuntime, at: datetime, trigger: str) -> dict:
    """Bu taramayı KİM başlattı? Rapora ve o taramanın ürettiği HER defter satırına yazılır.

    Denetçi defteri tek başına okur; rapor saklanmaz. "Ajan mı karar verdi, biri düğmeye mi bastı"
    sorusunun cevabı satırın içinde olmalı: `trigger: scheduler` (ajanın kendi kadansı) ·
    `api` (dışarıdan tetiklenen tarama) · `demo` (jüri ekranındaki senaryo).
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


# ============================================================================ zamanlayıcı (ajanın kendi saati)
def _env_flag(name: str) -> bool | None:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return None
    return v.strip().lower() in ("1", "true", "yes", "on")


class Scheduler:
    """Ajanın kendi saati — "The agent decides. Nobody presses a button." iddiasının kodu.

    Kadans mantığı (`agent.policy.sweep_due`) yazılıydı ama onu çağıran yoktu: tarama YALNIZCA
    `POST /v1/sites/{id}/sweep` ile dışarıdan tetikleniyordu. Bu görev o boşluğu kapatır — arka
    plan görevi periyodik uyanır, her saha için `sweep_due()` sorar, gerekiyorsa taramayı çalıştırır.

    Kurallar:
    - Mevcut eşzamanlılık modeli korunur: tur bütünüyle `store.lock` altında çalışır.
    - Bloklayan iş (senkron Nokia çağrıları) `asyncio.to_thread` ile olay döngüsünün DIŞINDA koşar.
    - Hata görevi ÖLDÜRMEZ: sayaca yazılır, loglanır, bir sonraki tur devam eder.
    - WBGT ölçümü hiç girilmemiş sahada verilecek karar yoktur — boşuna çalışmaz.
    - Varsayılan: testte ve fixture modunda KAPALI (demolar belirlenimci kalsın); `HS_SCHEDULER`
      ile açıkça açılıp kapatılır, uyanma aralığı `HS_SCHEDULER_TICK_S` (varsayılan 30 sn).
    """

    def __init__(self) -> None:
        self.enabled = False
        self.reason = "not started"
        self.tick_s = 30.0
        self.task: asyncio.Task | None = None
        self.started_at: datetime | None = None
        self.last_tick_at: datetime | None = None
        self.ticks = 0
        self.sweeps_run = 0
        self.errors = 0
        self.last_error: str | None = None

    # ---- yapılandırma
    @staticmethod
    def tick_seconds() -> float:
        raw = os.environ.get("HS_SCHEDULER_TICK_S", "30")
        try:
            return max(0.01, float(raw))
        except ValueError:
            log.warning("HS_SCHEDULER_TICK_S is not a number: %r — falling back to 30 s", raw)
            return 30.0

    def _decide(self) -> tuple[bool, str]:
        flag = _env_flag("HS_SCHEDULER")
        mode = getattr(nac.cfg, "mode", "fixture")
        if flag is not None:
            return flag, f"HS_SCHEDULER={os.environ.get('HS_SCHEDULER')!r}"
        if "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules:
            return False, "default: off under pytest (the test suite stays deterministic)"
        if mode == "fixture":
            return False, "default: off in fixture mode (demos stay deterministic) — set HS_SCHEDULER=1 to run it"
        return True, f"default: on for NAC_MODE={mode}"

    # ---- ömür döngüsü
    def start(self) -> None:
        self.enabled, self.reason = self._decide()
        self.tick_s = self.tick_seconds()
        self.ticks = self.sweeps_run = self.errors = 0
        self.last_error = None
        self.last_tick_at = None
        if not self.enabled:
            log.info("scheduler disabled (%s)", self.reason)
            return
        self.started_at = _now()
        self.task = asyncio.create_task(self._loop(), name="heatshield-scheduler")
        log.info("scheduler started tick=%ss (%s)", self.tick_s, self.reason)

    async def stop(self) -> None:
        task, self.task = self.task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        log.info("scheduler stopped after %d ticks (%d sweeps, %d errors)", self.ticks, self.sweeps_run, self.errors)

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.tick_s)
                await asyncio.to_thread(self.tick)
            except asyncio.CancelledError:
                raise
            except Exception as e:          # noqa: BLE001 — görev ASLA ölmez
                self.errors += 1
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("scheduler tick failed — the loop continues")

    # ---- bir tur
    def tick(self, now: datetime | None = None) -> list[dict]:
        """Bir uyanış: her saha için `sweep_due()` sor, gerekiyorsa tara. Testlerden doğrudan çağrılabilir."""
        at = now or _now()
        ran: list[dict] = []
        with store.lock:
            self.ticks += 1
            self.last_tick_at = at
            for site in list(store.sites.values()):
                if site.wbgt_c is None:
                    continue                                   # ölçüm yok → verilecek karar yok
                if not sweep_due(site, at, site.cfg):
                    continue                                   # kadans dolmadı
                try:
                    r = _stamp_trigger(step(site, at, facade, site.cfg, None), site, at, "scheduler")
                except Exception as e:      # noqa: BLE001 — bir saha çökerse diğerleri taranmaya devam eder
                    self.errors += 1
                    self.last_error = f"{site.site_id}: {type(e).__name__}: {e}"
                    log.exception("scheduled sweep failed site=%s — the loop continues", site.site_id)
                    continue
                self.sweeps_run += 1
                ran.append({"site_id": site.site_id, "at": _iso(at), "breached": r.get("breached"),
                            "level": r.get("level"), "queries": len(r.get("calls") or []),
                            "next_due_in_min": r.get("sweep_interval_min")})
        return ran

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled, "reason": self.reason, "tick_seconds": self.tick_s,
            "running": bool(self.task and not self.task.done()),
            "started_at": _iso(self.started_at), "last_tick_at": _iso(self.last_tick_at),
            "ticks": self.ticks, "sweeps_run": self.sweeps_run, "errors": self.errors, "last_error": self.last_error,
            "env": {"HS_SCHEDULER": os.environ.get("HS_SCHEDULER"), "HS_SCHEDULER_TICK_S": os.environ.get("HS_SCHEDULER_TICK_S")},
        }


scheduler = Scheduler()


# ============================================================================ istek modelleri
class SiteIn(BaseModel):
    name: str = "Lusail Construction Site"
    jurisdiction: str = Field(default="QA", pattern="^(QA|SA|AE|qa|sa|ae)$")
    lat: float = 25.38
    lng: float = 51.49
    radius_m: float = 500


class WorkerIn(BaseModel):
    worker_id: str
    phone: str
    name: str = ""
    micro_zone: str = "z1"
    first_day_on_site: str | None = None
    prior_incident: bool = False
    shift: str = "day"
    badge_in: bool = True
    device_history: dict = Field(default_factory=dict)
    moving_out: bool = False
    subscribe: bool = True


class SweepIn(BaseModel):
    now: datetime | None = None
    wbgt_c: float | None = None
    force: bool = True   # False → tarama sıklığı (sweep_due) dikkate alınır


class WbgtIn(BaseModel):
    wbgt_c: float
    now: datetime | None = None


class DemoIn(BaseModel):
    reset: bool = True
    start: datetime | None = None
    fillers: int = 14


# ============================================================================ kurulum
def _site_cfg(code: str) -> Config:
    return CFG.with_jurisdiction(code.upper())


def _create_site(body: SiteIn) -> SiteRuntime:
    cfg = _site_cfg(body.jurisdiction)
    site = SiteRuntime(site_id=f"site-{uuid.uuid4().hex[:6]}", name=body.name, lat=body.lat, lng=body.lng,
                       radius_m=max(float(body.radius_m), CFG.min_perimeter_radius_m), cfg=cfg)
    store.sites[site.site_id] = site
    site.log(_now(), "site_registered", f"{site.name} — {cfg.jurisdiction.name}: {cfg.jurisdiction.legal_ref}", source="jurisdiction-config")
    return site


def _add_worker(site: SiteRuntime, body: WorkerIn) -> WorkerRuntime:
    w = new_worker(body.worker_id, body.phone, name=body.name, micro_zone=body.micro_zone,
                   first_day_on_site=body.first_day_on_site, prior_incident=body.prior_incident,
                   shift=body.shift, badge_in=body.badge_in, device_history=dict(body.device_history),
                   moving_out=body.moving_out)
    site.workers[w.worker_id] = w
    if body.subscribe:
        sink = f"{PUBLIC_BASE_URL.rstrip('/')}/webhooks/geofence"
        # CAMARA: abonelik başına TEK olay tipi. Giriş ve çıkış için AYRI abonelik açılır —
        # ikisini tek gövdede göndermek canlı platformda 400 döndürür (Nokia SDK bunu zorluyor).
        for etype in (GEO_ENTERED, GEO_LEFT):
            r = facade.call("geofence_subscribe", w.phone, site.lat, site.lng, site.radius_m, sink,
                            types=[etype], sink_token=WEBHOOK_TOKEN or None)
            sid = (r.data or {}).get("id")
            if not sid:
                continue
            store.subs[sid] = (site.site_id, w.worker_id)
            if etype == GEO_ENTERED:
                site.subscriptions[w.worker_id] = sid          # birincil kimlik (geriye dönük)
            else:
                site.subscriptions_left[w.worker_id] = sid
    return w


# ============================================================================ uçlar
@app.post("/v1/sites", status_code=201)
def create_site(body: SiteIn):
    with store.lock:
        return _create_site(body).to_dict()


@app.get("/v1/sites")
def list_sites():
    return [s.to_dict() for s in store.sites.values()]


@app.get("/v1/sites/{site_id}")
def get_site(site_id: str):
    return store.site_or_404(site_id).to_dict()


@app.post("/v1/sites/{site_id}/workers", status_code=201)
def add_worker(site_id: str, body: WorkerIn):
    with store.lock:
        site = store.site_or_404(site_id)
        try:
            return _add_worker(site, body).to_dict()
        except ValueError as e:
            raise HTTPException(400, {"code": "INVALID_ARGUMENT", "message": str(e)})


@app.post("/v1/sites/{site_id}/wbgt")
def set_wbgt(site_id: str, body: WbgtIn):
    """Meteoroloji beslemesi (WBGT). Ölçüm defterlenir; karar taramada verilir."""
    with store.lock:
        site = store.site_or_404(site_id)
        site.wbgt_c = body.wbgt_c
        at = _parse_dt(body.now) or _now()
        site.log(at, "wbgt_reading", f"WBGT {body.wbgt_c} °C (legal limit {site.cfg.jurisdiction.wbgt_limit_c} °C)",
                 source="meteorology-feed", extra={"wbgt_c": body.wbgt_c})
        return {"site_id": site.site_id, "wbgt_c": site.wbgt_c, "limit_c": site.cfg.jurisdiction.wbgt_limit_c}


@app.post("/v1/sites/{site_id}/sweep")
def sweep(site_id: str, body: SweepIn | None = None):
    """Bir tarama çalıştırır ve tam raporu döner (plan, çağrılar, kararlar, bütçe, deftere eklenenler)."""
    body = body or SweepIn()
    with store.lock:
        site = store.site_or_404(site_id)
        at = _parse_dt(body.now) or _now()
        if not body.force and not sweep_due(site, at, site.cfg):
            return {"skipped": True, "reason": f"sweep cadence not due yet ({site.level})", "last_sweep_at": _iso(site.last_sweep_at)}
        return _stamp_trigger(step(site, at, facade, site.cfg, body.wbgt_c), site, at, "api")


@app.get("/v1/sites/{site_id}/ledger")
def ledger(site_id: str, worker_id: str | None = None, event: str | None = None, limit: int = Query(200, le=1000)):
    """Kanıt defteri — denetçi/sigortacı için zaman damgalı, işçi bazlı uyum kaydı."""
    site = store.site_or_404(site_id)
    items = [e for e in site.ledger if (not worker_id or e.get("worker_id") == worker_id) and (not event or e["event"] == event)]
    return {"site_id": site_id, "count": len(items), "items": items[-limit:], "coverage": site.coverage()}


@app.get("/v1/jurisdictions")
def jurisdictions():
    """Bölgesel ölçeklenebilirlik: yargı alanı bir yapılandırma kaydıdır, yeniden yazım değil."""
    return {code: j.to_dict() for code, j in JURISDICTIONS.items()}


@app.get("/v1/state")
def state():
    return {
        "now": _iso(_now()), "config": CFG.to_dict(),
        "sites": [s.to_dict() for s in store.sites.values()],
        "subscriptions": [{"id": sid, "site_id": s, "worker_id": w} for sid, (s, w) in store.subs.items()],
        "degraded": store.degraded[-10:],
        "webhook": {"sink": f"{PUBLIC_BASE_URL.rstrip('/')}/webhooks/geofence", "token_required": bool(WEBHOOK_TOKEN)},
        "scheduler": scheduler.to_dict(),   # ajanın kendi saati: açık mı, son tur ne zaman döndü
        # ESKIDEN BURADA SABIT 0 VARDI ve "surekli konum gecmisi tutulmaz" diyordu — YANLISTI.
        # Defter isci bazinda zaman damgali bir varlik kaydidir; bunu gizlemek yerine SAYIYORUZ.
        "presence_record": {sid: s.presence_record() for sid, s in store.sites.items()},
    }


@app.get("/v1/scheduler")
def scheduler_state():
    """Ajanın kendi saati: açık mı, hangi aralıkla uyanıyor, son tur ne zaman döndü, kaç hata yedi."""
    return scheduler.to_dict()


@app.get("/v1/config")
def config():
    return CFG.to_dict()


@app.post("/v1/reset")
def reset(authorization: Optional[str] = Header(None)):
    """Tum kanit defterlerini siler — bu yuzden korumali.

    Uyum kaydi urunun kendisiyse, onu herkesin silebilmesi bir butunluk acigidir.
    `WEBHOOK_TOKEN` tanimliyken Bearer zorunlu; tanimli degilse (yerel gelistirme) serbest.
    """
    if WEBHOOK_TOKEN and (not authorization or not authorization.startswith("Bearer ")
                          or authorization[7:] != WEBHOOK_TOKEN):
        raise HTTPException(401, {"code": "UNAUTHENTICATED",
                                  "message": "resetting the evidence ledger requires a bearer token"})
    with store.lock:
        store.reset()
    return {"ok": True}


# ============================================================================ webhook (ücretsiz sinyal)
@app.post("/webhooks/geofence")
def webhook_geofence(ev: dict = Body(...), authorization: Optional[str] = Header(default=None)):
    """Geofencing aboneliğinin sink'i. Şebeke iter → sorgu bütçesi harcanmaz (§8 Move 1)."""
    if WEBHOOK_TOKEN and (not authorization or not authorization.startswith("Bearer ") or authorization[7:] != WEBHOOK_TOKEN):
        raise HTTPException(401, {"code": "UNAUTHENTICATED", "message": "invalid webhook token"})
    if not isinstance(ev, dict) or "type" not in ev:
        raise HTTPException(400, {"code": "INVALID_ARGUMENT", "message": "a CloudEvent is expected"})
    with store.lock:
        data = ev.get("data") or {}
        found = store.subs.get(data.get("subscriptionId") or "")
        if not found:
            phone = (data.get("device") or {}).get("phoneNumber")
            h = hash_phone(phone) if phone else None
            for s in store.sites.values():
                for w in s.workers.values():
                    if h and w.phone_hash == h:
                        found = (s.site_id, w.worker_id)
                        break
        if not found:
            raise HTTPException(404, {"code": "UNKNOWN_SUBSCRIPTION", "message": "no subscription/worker matched"})
        ce_id = ev.get("id") or f"ce-{uuid.uuid4().hex[:8]}"
        if ce_id in store.seen_cloudevent_ids:
            return {"ok": True, "duplicate": True}
        store.seen_cloudevent_ids.add(ce_id)
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
        site = store.sites[site_id]
        entry = apply_geofence_event(site, worker_id, kind, _parse_dt(ev.get("time")) or _now())
        return {"ok": True, "site_id": site_id, "worker_id": worker_id, "kind": kind, "ledger": entry, "cost": 0}


# ============================================================================ demo (tek tuş)
DEMO_START = datetime(2026, 8, 17, 6, 0, tzinfo=timezone.utc)  # 09:00 Doha yerel saati
SITE = {"lat": 25.38, "lng": 51.49, "radius_m": 500}
NAMED = [
    # (worker_id, phone, name, zone, ilk_gun_offset_gun, prior, shift, moving_out, device_history)
    ("W-001", "+99999910001", "Rajan", "z1", 2, False, "day", False, {"continuous_reachable_hours": 7}),
    ("W-002", "+99999910002", "Bikash", "z1", 1, False, "day", False, {"continuous_reachable_hours": 8}),
    ("W-003", "+99999910003", "Anil", "z2", 400, False, "day", False, {}),
    ("W-004", "+99999910004", "Suman", "z2", 380, False, "day", False, {}),
    ("W-005", "+99999910005", "Prakash", "z3", 500, False, "day", False, {"recurring_unreachable_window": True}),
    ("W-006", "+99999910006", "Kamal", "z3", 300, False, "day", True, {}),
    ("W-007", "+99999910007", "Deepak", "z1", 600, False, "day", False, {}),
    ("W-008", "+99999910008", "Ravi", "z1", 200, True, "day", False, {}),
    ("W-009", "+99999910009", "Mohan", "z1", 250, False, "night_to_day", False, {}),
    ("W-010", "+99999910010", "Sunil", "z1", 700, False, "day", False, {}),
    ("W-011", "+99999910011", "Ganesh", "z1", 800, False, "day", False, {}),
    ("W-012", "+99999910012", "Arjun", "z1", 900, False, "day", False, {}),
    ("W-013", "+99999910013", "Sanjay", "z1", 350, False, "day", False, {"continuous_reachable_hours": 7}),
]


def _prime(phone: str, patch: dict) -> None:
    """Demo veri/hata enjeksiyonu — her iki sürece birden.

    Simülatör modunda Nokia çağrıları AYRI bir sürece (8081) gider. Enjeksiyonu oraya da
    iletmezsek iki şey birden bozulur: (1) api-down senaryosu ekranda kendi manşetini
    yalanlar (yalnız W-011 düşer, diğer on işçi "doğrulandı" görünür), (2) profiles.json'daki
    kalıcı `fail` değerleri temizlenemediği için devre kesici sonraki demoya sızar.
    """
    p = normalize_phone(phone)
    if nac.fx is not None:
        nac.fx.update_profile(p, patch)
    if getattr(nac.cfg, "mode", "fixture") == "simulator":
        try:
            httpx.put(f"{nac.cfg.base_url.rstrip('/')}/_sim/profiles/{p}", json=patch, timeout=2.0)
        except Exception as e:  # simülatör kapalıysa demo yine de çalışsın
            log.warning("simulator prime failed phone=%s err=%s", mask_phone(p), e)


def _setup_site(t0: datetime, fillers: int = 14, jurisdiction: str = "QA") -> SiteRuntime:
    """12 adlandırılmış işçi + kalabalık. Kalabalık, bütçenin neden yetmediğini görünür kılar."""
    site = _create_site(SiteIn(name="Lusail Construction Site", jurisdiction=jurisdiction, **SITE))
    for wid, phone, name, zone, days, prior, shift, moving, hist in NAMED:
        _add_worker(site, WorkerIn(worker_id=wid, phone=phone, name=name, micro_zone=zone,
                                   first_day_on_site=(t0 - timedelta(days=days)).date().isoformat(),
                                   prior_incident=prior, shift=shift, moving_out=moving, device_history=hist))
    for i in range(fillers):
        phone = f"+9999992{2000 + i:04d}"
        _prime(phone, {"location": {"lat": SITE["lat"] + 0.0004, "lng": SITE["lng"] - 0.0004, "radius": 120},
                       "reachable": True, "connectivity": ["DATA", "SMS"], "congestion": "Low"})
        _add_worker(site, WorkerIn(worker_id=f"W-{100 + i}", phone=phone, name=f"Worker {100 + i}", micro_zone="z4",
                                   first_day_on_site=(t0 - timedelta(days=365 + i)).date().isoformat()))
    # sabah vardiya girişi — ücretsiz geofence olayları (hiçbir sorgu harcanmaz)
    for w in list(site.workers.values()):
        if w.worker_id == "W-012":
            w.badge_in, w.inside, w.last_signal_at = True, False, None  # rozetle girdi, cihazı hiç görünmedi
            continue
        apply_geofence_event(site, w.worker_id, "enter", t0)
    return site


def _demo_start(body: DemoIn | None) -> tuple[DemoIn, datetime]:
    body = body or DemoIn()
    if body.reset:
        store.reset()
    for api_name in list(nac.breaker.snapshot()):
        nac.breaker.success(api_name)          # devre kesiciyi senaryolar arasında sıfırla
    for _, phone, *_ in NAMED:
        _prime(phone, {"fail": None})          # önceki senaryonun hata enjeksiyonunu temizle
    return body, _parse_dt(body.start) or DEMO_START


def _sweep(site: SiteRuntime, at: datetime, wbgt: float | None, label: str) -> dict:
    r = _stamp_trigger(step(site, at, facade, site.cfg, wbgt), site, at, "demo")
    r["label"] = label
    r["local_time"] = (at + timedelta(hours=site.cfg.jurisdiction.tz_offset_hours)).strftime("%H:%M")
    return r


@app.post("/v1/demo/heat-day")
def demo_heat_day(body: DemoIn | None = None):
    """Bir sıcak günün tamamı: sessiz izleme → yasal ihlal → bütçeli tarama → bayılma → QoD → ihlalin bitişi."""
    with store.lock:
        body, t0 = _demo_start(body)
        site = _setup_site(t0, body.fillers)
        sweeps = [
            _sweep(site, t0 + timedelta(minutes=0), 30.2, "09:00 — morning: below the threshold, NO authority to query"),
            _sweep(site, t0 + timedelta(minutes=75), 31.4, "10:15 — summer ban hours began: breach (marginal)"),
            _sweep(site, t0 + timedelta(minutes=85), 31.6, "10:25 — second sweep: the stale signals moved to the front"),
            _sweep(site, t0 + timedelta(minutes=95), 35.4, "10:35 — WBGT jumped: SEVERE breach, 2-minute sweeps"),
            _sweep(site, t0 + timedelta(minutes=97), 35.4, "10:37 — after escalation: a one-time coordinate for the medic"),
            _sweep(site, t0 + timedelta(minutes=600), 29.8, "19:00 — breach over: the authority to query lapsed"),
        ]
        return _demo_out(site, sweeps, "heat-day",
                         "Not one query while there was no breach; during the breach the budget was spent in risk order.")


@app.post("/v1/demo/collapse")
def demo_collapse(body: DemoIn | None = None):
    """Bayılma mı, bitmiş pil mi? Beş sessiz cihaz, dört hüküm — biri bayat okumaymış.

    W-013 bu demonun yeni parçası: `device-reachability-status` onu "erişilemez" gösteriyor, ama o okuma
    şebekenin TUTTUĞU durumdur ve bir saate kadar bayat olabilir. Sıkı tazelikli konum sorgusu cihazı
    sayfalıyor, cevap geliyor → bayılma değil. Sağlıkçı boşuna çağrılmıyor.
    """
    with store.lock:
        body, t0 = _demo_start(body)
        site = _setup_site(t0, 0)
        sweeps = [
            _sweep(site, t0 + timedelta(minutes=75), 35.6, "10:15 — severe breach: who is still inside?"),
            _sweep(site, t0 + timedelta(minutes=77), 35.6, "10:17 — reachability sweep: four devices went silent"),
            _sweep(site, t0 + timedelta(minutes=79), 35.6, "10:19 — escalation: medic + guaranteed bandwidth"),
        ]
        verdicts = {}
        for s in sweeps:
            for v in s["verdicts"]:
                verdicts[v["worker_id"]] = v
        return _demo_out(site, sweeps, "collapse",
                         "Five silent devices. Four verdicts — and one the network was simply out of date about: a fresh-fix probe answered, proving the device was alive. Only one worker went to a medic.",
                         extra={"verdict_matrix": list(verdicts.values())})


@app.post("/v1/demo/no-breach")
def demo_no_breach(body: DemoIn | None = None):
    """Move 1 — eşik altında sorgu yetkisi yok: sıfır çağrı, sıfır maliyet, sıfır gözetim."""
    with store.lock:
        body, t0 = _demo_start(body)
        site = _setup_site(t0, body.fillers)
        sweeps = [
            _sweep(site, t0, 30.2, "09:00 — WBGT 30.2 °C: below the limit"),
            _sweep(site, t0 + timedelta(hours=8), 31.9, "17:00 — 31.9 °C: still below the limit, and the ban hours are over too"),
        ]
        return _demo_out(site, sweeps, "no-breach",
                         "Two sweeps, zero network queries. Outside a legal breach the system has no authority to query.")


@app.post("/v1/demo/api-down")
def demo_api_down(body: DemoIn | None = None):
    """Nokia API çöktüğünde: hiç kimse sessizce 'güvende' işaretlenmez — sinyal 'bilinmiyor' kalır."""
    with store.lock:
        body, t0 = _demo_start(body)
        site = _setup_site(t0, 0)
        for _, phone, *_ in NAMED:
            _prime(phone, {"fail": 500})
        sweeps = [_sweep(site, t0 + timedelta(minutes=75), 35.6, "10:15 — the breach is on, but Location Verification returns 500")]
        unknown = [w for w in site.workers.values() if w.verified_inside is None and w.inside]
        return _demo_out(site, sweeps, "api-down",
                         "No signal → no worker was counted as verified; they stayed in the queue and the ledger wrote 'unknown'.",
                         extra={"unknown_workers": [w.worker_id for w in unknown], "breaker": nac.health()["breaker"]})


@app.post("/v1/demo/planner-guard")
def demo_planner_guard(body: DemoIn | None = None):
    """Model kuralların ALTINDA: sırayı tartışabilir, planı genişletemez.

    Aynı ihlal iki kez taranır. İlkinde model geçerli bir yeniden sıralama önerir ve uygulanır.
    İkincisinde plana hiç var olmayan bir işçi sokmaya çalışır; güvenlik kapısı öneriyi bütünüyle
    reddeder, kuralların sırası aynen yürür ve red gerekçesi kanıt defterine yazılır.

    Anahtar gerektirmez: sağlayıcı çağrısı bu demo boyunca yerinde taklit edilir ve bu durum
    yanıtta `simulated_model=true` olarak açıkça belirtilir — canlı model gibi sunulmaz.
    """
    from agent import llm_adapter as LA
    from agent import policy as AP

    def _valid(_self, prompt):   # kurala uyan öneri: en kırılganı öne al (listeyi tersine çevir)
        ids = [i["id"] for i in json.loads(prompt)["plan"]]
        return json.dumps({"order": [{"id": i, "why": "unacclimatised / unseen longest"} for i in reversed(ids)],
                           "note": "risk order revised"})

    def _injects(_self, prompt):  # ihlal: planda olmayan bir işçi ekle
        ids = [i["id"] for i in json.loads(prompt)["plan"]]
        return json.dumps({"order": [{"id": "W-999", "why": "not in the plan at all"}] + [{"id": i} for i in ids]})

    with store.lock:
        body, t0 = _demo_start(body)
        original = LA.LLMPlanner._call_gemini
        # Ortamı DEĞİŞTİRMEDEN ÖNCE yedekle: demo bittiğinde canlı planlayıcı aynen geri gelmeli.
        # (Bir denetimde bulunmuştu: demo `HS_PLANNER`'ı sabit "rules"a çekip gerçek anahtarı
        #  siliyordu; jüri demoya bastıktan sonra süreç boyunca canlı Gemini bir daha çalışmıyordu.)
        saved = {k: os.environ.get(k) for k in ("HS_PLANNER", "GEMINI_API_KEY", "HS_GEMINI_MODEL")}
        try:
            # Model adı bilerek "demo-simulated": defterde canlı Gemini gibi görünmesin.
            os.environ["HS_PLANNER"], os.environ["GEMINI_API_KEY"] = "gemini", "demo-key"
            os.environ["HS_GEMINI_MODEL"] = "demo-simulated"

            LA.LLMPlanner._call_gemini = _valid
            AP.reset_planner()
            site = _setup_site(t0, 0)
            s1 = _sweep(site, t0 + timedelta(minutes=75), 35.6, "10:15 — the model reorders the queue")

            LA.LLMPlanner._call_gemini = _injects
            AP.reset_planner()
            s2 = _sweep(site, t0 + timedelta(minutes=85), 35.6, "10:25 — the model slips in a worker who is not in the plan")
        finally:
            LA.LLMPlanner._call_gemini = original
            for k, v in saved.items():          # ne varsa aynen geri koy, yoksa yokluğunu geri koy
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            AP.reset_planner()

        accepted, rejected = s1.get("planner", {}), s2.get("planner", {})
        return _demo_out(site, [s1, s2], "planner-guard",
                         "The model could change the order; the moment it tried to widen the plan the gate refused and the rules' order stood.",
                         extra={"simulated_model": True,
                                "note": "The provider call is simulated in this demo; it is not presented as a live model. "
                                        "With a real key the same code path goes to Gemini/Groq.",
                                "accepted": {"used": accepted.get("used"), "moved": accepted.get("moved"),
                                             "before": accepted.get("order_before"), "after": accepted.get("order_after")},
                                "rejected": {"used": rejected.get("used"), "violation": rejected.get("violation"),
                                             "offending": rejected.get("offending"),
                                             "order_held": rejected.get("order_before") == rejected.get("order_after")}})


@app.post("/v1/demo/jurisdiction")
def demo_jurisdiction(body: DemoIn | None = None):
    """Aynı an, üç yargı alanı: Katar / Suudi Arabistan / BAE. Yargı alanı bir config kaydıdır."""
    with store.lock:
        body, t0 = _demo_start(body)
        at = t0.replace(hour=8, minute=45)   # 11:45 Doha/Riyad · 12:45 Dubai yerel
        out = []
        for code in ("QA", "SA", "AE"):
            site = _setup_site(at, 0, jurisdiction=code)
            r = _sweep(site, at, 31.5, f"{code} — WBGT 31.5 °C (below the limit): the verdict comes from the ban hours alone")
            out.append({"code": code, "jurisdiction": site.cfg.jurisdiction.to_dict(), "sweep": r,
                        "breached": r["breached"], "reason": r["reason"], "local_time": r["local_time"],
                        "site": site.to_dict()})
        return {"scenario": "jurisdiction", "at": _iso(at), "results": out,
                "headline": "Same temperature, same minute — different jurisdiction, different legal outcome.",
                "config": CFG.to_dict()}


def _query_unit_cost_usd() -> float | None:
    """Sorgu başına birim maliyet — YALNIZCA operatör kendi rakamını verirse (HS_QUERY_UNIT_COST_USD).

    Burada sabit bir `queries * 0.02` vardı. O 0.02'nin hiçbir kaynağı yoktu: Nokia'nın yayınlanmış
    bir birim fiyatı yok, elimizde bir teklif de yok. Kaynaksız bir dolar rakamı jüri önünde tek
    soruyla çöker ve bu projenin tek sermayesi dürüstlüktür. Varsayılan artık "rakam yok".
    """
    raw = (os.environ.get("HS_QUERY_UNIT_COST_USD") or "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        log.warning("HS_QUERY_UNIT_COST_USD is not a number: %r — no cost figure is reported", raw)
        return None
    return v if v >= 0 else None


def _cost_block(queries: int) -> dict:
    """Maliyet bloğu: ya kaynağı belli bir rakam, ya hiç rakam."""
    unit = _query_unit_cost_usd()
    if unit is None:
        return {"queries": queries, "unit_cost_usd": None, "estimated_cost_usd": None, "priced": False,
                "basis": "No unit price is published by the operator and we hold no quote, so no dollar figure is "
                         "reported. Set HS_QUERY_UNIT_COST_USD to price a run with your own rate card; it is then "
                         "labelled an illustrative unit cost, never an operator quote."}
    return {"queries": queries, "unit_cost_usd": unit, "estimated_cost_usd": round(queries * unit, 2), "priced": True,
            "basis": f"queries x HS_QUERY_UNIT_COST_USD ({unit} USD) — an illustrative unit cost supplied by whoever "
                     f"runs this deployment. NOT a Nokia price and NOT an operator quote."}


# Sıralamanın harcadığı basamaklar (bütçe bunlara harcanır) — eskalasyon sonrası çağrılar ayrı sayılır:
# konum çekme / congestion / QoD bir hükümden SONRA gelir, "herkesi sorgula" dünyasında da gelirdi.
VERIFICATION_APIS = ("location-verification", "device-reachability-status")


def _naive_baseline(sweeps: list[dict]) -> dict:
    """"Herkesi sorgulasaydık" taban çizgisi — ölçülür, uydurulmaz.

    Eskiden `len(workers) × ihlalli_tarama × 2` yazıyordu; oradaki 2 bir varsayımdı (işçi başına iki
    API) ve hiçbir yerden gelmiyordu. Gerçek taban çizgisi taramanın KENDİ raporundan okunur: her
    ihlalli taramada, vardiya listesinin sahada saydığı (rozetli, çıkışı işlenmemiş) her işçi için
    bir doğrulama — sayılır, varsayılmaz.

    Elmayla elma: taban çizgisi doğrulama basamaklarını sayar, biz de doğrulama basamaklarını
    sayarız. Eskalasyondan sonraki çağrılar (koordinat, congestion, QoD) her iki dünyada da olurdu,
    ikisinden de çıkarılır ve ayrıca raporlanır. Ücretsiz geofence olayları da ortaktır.
    Taban çizgisi bilerek TUTUCUdur: işçi başına TEK doğrulama sayılır (iki basamağı da soran naif
    bir sistem iki katını harcardı) ve çıkışı işlenmiş işçiler baştan düşülür.
    """
    def on_roster(sweep: dict) -> int:
        return len([w for w in (sweep.get("workers") or []) if w.get("badge_in") and not w.get("exited_at")])

    per_sweep = [on_roster(s) for s in sweeps if s.get("breached")]
    calls = [c for s in sweeps for c in (s.get("calls") or [])]
    verification = sum(1 for c in calls if c.get("api") in VERIFICATION_APIS)
    return {"queries": sum(per_sweep), "per_breached_sweep": per_sweep,
            "verification_queries_made": verification,
            "escalation_queries_made": len(calls) - verification,
            "basis": "one verification per worker the roster places on site, on every breached sweep — no ranking, "
                     "no staleness filter, no budget cap. Counted from the sweeps themselves, not assumed. Compared "
                     "against our own verification queries; post-escalation calls (coordinate, congestion, QoD) "
                     "follow a verdict and would occur in both worlds, so they are excluded from both sides and "
                     "reported separately."}


def _demo_out(site: SiteRuntime, sweeps: list[dict], scenario: str, headline: str, extra: dict | None = None) -> dict:
    naive = _naive_baseline(sweeps)
    out = {
        "scenario": scenario, "headline": headline, "site": site.to_dict(), "sweeps": sweeps,
        "ledger": site.ledger, "coverage": site.coverage(), "degraded": store.degraded[-10:],
        "totals": {
            "queries": site.queries_total,
            "queries_if_polled_everyone": naive["queries"],
            "naive_baseline": naive,
            "escalations": sum(len(s["escalations"]) for s in sweeps),
            "skipped_for_budget": sum(s["budget"]["skipped_for_budget"] for s in sweeps),
            "cost": _cost_block(site.queries_total),
        },
        "config": site.cfg.to_dict(),
    }
    out.update(extra or {})
    return out


@app.get("/")
def root():
    return {"app": "heatshield", "motto": "The network is the sensor; the worker does nothing.", "demo": "/demo", "health": "/health",
            "scenarios": ["heat-day", "collapse", "no-breach", "jurisdiction", "api-down", "planner-guard"],
            "jurisdictions": list(JURISDICTIONS)}
