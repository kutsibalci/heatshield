"""HeatShield ajan politikası — deterministik tarama (sweep) motoru.

Idea capture §5.2 (eskalasyon merdiveni), §8 (doğrulama bütçesi) ve §9 (bayılma vs bitmiş pil) burada
çalışan koda dönüşür. Karar mantığı `packages/rules` içindeki SAF fonksiyonlardadır; bu dosya yalnızca
sırayı, durumu ve Nokia çağrılarını yönetir.

Tek giriş noktası:  `step(site, now, nac_facade, cfg, wbgt_c=None) -> dict`  (bir tarama = bir rapor)

Üç hamle (§8):
  Move 1 — İhlal yoksa PARA HARCAMA. Geofence olayları şebeke tarafından ücretsiz itilir; sorgu yetkisi
           yalnızca yasal eşik aşıldığında doğar (aynı zamanda amaç sınırlaması: gözetim aracına dönüşemez).
  Move 2 — Sırala, tepeden harca. score = severity × exposure_minutes × staleness × vulnerability.
           Bütçe bitince liste bitmese de DURULUR; kaç işçinin sorgulanmadığı rapora yazılır.
  Move 3 — Maliyet merdivenini yalnızca ucuz sinyal belirsizse tırman:
           location_verify (evet/hayır) → reachability → location_retrieve (yalnızca eskalasyon sonrası).

Gizlilik: ham telefon numarası yalnızca `WorkerRuntime.phone` içinde ve sadece NaC çağrısına gider.
`to_dict()` asla ham numara döndürmez. Koordinat yalnızca "olası bayılma" vakası için, bir kez alınır.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from nac_client.privacy import hash_phone, mask_phone, normalize_phone
from rules import Config
from rules import heatshield as R
from rules.heatshield import STATES  # passive_watch | alert | confirming | distress | emergency

__all__ = ["SiteRuntime", "WorkerRuntime", "step", "apply_geofence_event", "sweep_due", "STATES"]


# --------------------------------------------------------------------------- yardımcılar
def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def _dt(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- işçi
@dataclass
class WorkerRuntime:
    """Bir işçinin canlı durumu. `phone` ham tutulur (yalnızca NaC çağrısı); dışarı maskeli/hash çıkar."""

    worker_id: str
    phone: str
    name: str = ""
    micro_zone: str = "z1"
    first_day_on_site: str | None = None      # ISO tarih — ilk hafta ağırlığı
    prior_incident: bool = False
    shift: str = "day"                        # day | night_to_day | transition
    badge_in: bool = True                     # sabah turnike kaydı (§9A mutabakat)

    # --- canlı sinyaller
    inside: bool = True                       # geofence olaylarından (ücretsiz)
    entered_at: datetime | None = None        # sahaya giriş anı — maruziyet penceresinin başlangıcı
    exited_at: datetime | None = None
    last_signal_at: datetime | None = None
    last_verified_at: datetime | None = None
    verified_inside: bool | None = None
    moving_out: bool = False
    last_reachability_at: datetime | None = None
    reachable: bool | None = None
    location_retrieved: bool = False
    last_location: dict | None = None         # yalnızca eskalasyon sonrası, tek sefer; defterde kaba özet
    device_history: dict = field(default_factory=dict)   # {"recurring_unreachable_window": bool, "continuous_reachable_hours": float}
    # Vardiya listesinde olup şebekede hiç görülmemiş işçi: maruziyeti bu olasılıkla ölçülür.
    presence_prior: float = 1.0
    # Yabancı hat + dolaşım: sorgular abonenin EV operatöründen karşılanır. Göçmen işçinin
    # memleket operatörü Open Gateway sunmuyorsa bu işçiye teknik olarak hiç erişilemez.
    # "Yok" değil, KALICI OLARAK BİLİNMİYOR — ve kapsama metriğinde ayrı sayılır.
    reachable_via_operator: bool = True

    # --- karar durumu
    state: str = "passive_watch"
    cleared: bool = False
    escalated: bool = False
    verdict: str | None = None
    confidence: float | None = None
    score: float = 0.0
    vulnerability: float = 1.0
    queries_used: int = 0

    @property
    def masked(self) -> str:
        return mask_phone(self.phone)

    @property
    def phone_hash(self) -> str:
        return hash_phone(self.phone)

    def to_signals(self) -> dict:
        """Kural fonksiyonlarının beklediği sözlük görünümü (saf veri, ham numara yok)."""
        return {
            "worker_id": self.worker_id, "masked": self.masked, "name": self.name, "micro_zone": self.micro_zone,
            "first_day_on_site": self.first_day_on_site, "prior_incident": self.prior_incident, "shift": self.shift,
            "entered_at": _iso(self.entered_at),
            "last_signal_at": _iso(self.last_signal_at), "last_verified_at": _iso(self.last_verified_at),
            "verified_inside": self.verified_inside, "last_reachability_at": _iso(self.last_reachability_at),
            "reachable": self.reachable, "location_retrieved": self.location_retrieved,
            "cleared": self.cleared, "escalated": self.escalated, "score": self.score, "vulnerability": self.vulnerability,
            "presence_prior": self.presence_prior, "reachable_via_operator": self.reachable_via_operator,
            "queries_used": self.queries_used,
        }

    def to_dict(self) -> dict:
        d = self.to_signals()
        d.update({
            "phone_masked": self.masked, "phone_hash": self.phone_hash, "inside": self.inside,
            "exited_at": _iso(self.exited_at), "state": self.state, "verdict": self.verdict,
            "confidence": self.confidence, "queries_used": self.queries_used, "badge_in": self.badge_in,
            "device_history": self.device_history, "moving_out": self.moving_out,
            "last_location_masked": self.last_location,
        })
        return d


# --------------------------------------------------------------------------- saha
@dataclass
class SiteRuntime:
    """Bir şantiyenin canlı durumu + kanıt defteri (uyum kaydı ürünün kendisi)."""

    site_id: str
    name: str
    lat: float
    lng: float
    radius_m: float
    cfg: Config
    workers: dict[str, WorkerRuntime] = field(default_factory=dict)
    state: str = "passive_watch"
    wbgt_c: float | None = None
    wbgt_at: datetime | None = None       # okumanın ALINDIĞI an — bayatlık karara girer
    breached: bool = False
    severity: float = 0.0
    level: str = "none"
    breach_started_at: datetime | None = None
    last_sweep_at: datetime | None = None
    last_reconcile_at: datetime | None = None
    sweeps: int = 0
    queries_total: int = 0
    ledger_pruned: int = 0              # tavan asilinca dusurulen satir sayisi (sessizce degil)
    verifications_total: int = 0        # kaç Location Verification yapıldı
    partial_verifications: int = 0      # kaçı PARTIAL döndü — konumlandırma belirsizliğinin ölçüsü
    ledger: list[dict] = field(default_factory=list)
    congestion_cache: dict[str, tuple[datetime, str, str]] = field(default_factory=dict)  # zone → (t, level, source)
    qod_sessions: dict[str, dict] = field(default_factory=dict)
    # CAMARA abonelik başına tek olay tipi ister → işçi başına İKİ abonelik.
    subscriptions: dict[str, str] = field(default_factory=dict)        # worker_id → area-entered abonelik id
    subscriptions_left: dict[str, str] = field(default_factory=dict)   # worker_id → area-left abonelik id

    # ---- kanıt defteri
    def log(self, now: datetime, event: str, detail: str, *, worker: WorkerRuntime | None = None,
            source: str = "rules", explain: list | None = None, extra: dict | None = None) -> dict:
        entry = {
            "seq": len(self.ledger) + 1, "t": _iso(now), "site_id": self.site_id, "event": event, "detail": detail,
            "worker_id": worker.worker_id if worker else None, "worker_masked": worker.masked if worker else None,
            "source": source, "explain": [e.to_dict() if hasattr(e, "to_dict") else e for e in (explain or [])],
        }
        if extra:
            entry.update(extra)
        self.ledger.append(entry)
        return entry

    def prune_ledger(self, cfg: Config) -> int:
        """Defteri tavanda tut; DUSURULEN SATIR SAYISINI SAKLA.

        Bir gizlilik denetimi "konum gecmisi tutulmaz" iddiamizin YANLIS oldugunu olctu: defter
        budanmiyordu ve tek isci icin 11 zaman damgali varlik kaydi olusuyordu. Dogru cevap
        defteri gizlemek degil — o defter urunun kendisi, muteahhidin uyum kaniti. Dogru cevap
        onu SINIRLI, SAYILABILIR ve beyan edilmis tutmak.
        """
        cap = cfg.budget.ledger_max_entries
        excess = len(self.ledger) - cap
        if excess > 0:
            del self.ledger[:excess]
            self.ledger_pruned += excess
        return self.ledger_pruned

    def presence_record(self) -> dict:
        """Defterin GERCEKTEN ne tuttugunu sayar — iddia degil, olcum.

        `location_history_size: 0` diye sabit bir sifir donduruyorduk; bu yanlisti.
        """
        worker_entries = sum(1 for e in self.ledger if e.get("worker_id"))
        coords = sum(1 for e in self.ledger if e.get("coarse"))
        return {"ledger_entries": len(self.ledger), "ledger_pruned": self.ledger_pruned,
                "ledger_cap": self.cfg.budget.ledger_max_entries,
                "worker_presence_entries": worker_entries,
                "coordinates_stored": coords,
                "note": ("The ledger IS a per-worker presence record - that is the product, not a "
                         "side effect. It is bounded, counted, and every coordinate in it is coarsened "
                         "to 3 decimals. Production needs durable storage with a legal retention period.")}

    def coverage(self) -> dict:
        """§9A dürüst kapsama metriği: rozetle giren kaç işçi şebekede görünüyor?"""
        badged = [w for w in self.workers.values() if w.badge_in]
        # "Görünür" = ŞEBEKEDE gerçekten bir sinyal görülmüş olması. `inside` bayrağına bakmayız:
        # vardiya listesi mutabakatı onu kanıt olmadan da açabiliyor (bkz. _reconcile_roster) ve
        # kapsama metriği bu projenin dürüstlük ölçüsüdür — mutabakatla şişirilemez.
        visible = [w for w in badged if w.last_signal_at is not None]
        # Yabancı hatta olan işçi kayıp değil, KALICI OLARAK ERİŞİLEMEZ. Ayrı sayılır ki
        # kapsama oranı "bulamadık" ile "hiç bakamayız"ı aynı kefeye koymasın.
        unreachable = [w for w in badged if not w.reachable_via_operator]
        pct = round(100 * len(visible) / len(badged)) if badged else 100
        return {"badged_in": len(badged), "network_visible": len(visible), "coverage_pct": pct,
                "missing": [w.worker_id for w in badged if w not in visible and w.reachable_via_operator],
                "permanently_unreachable": [w.worker_id for w in unreachable],
                "permanently_unreachable_pct": round(100 * len(unreachable) / len(badged)) if badged else 0}

    def to_dict(self) -> dict:
        return {
            "site_id": self.site_id, "name": self.name, "area": {"lat": self.lat, "lng": self.lng, "radius_m": self.radius_m},
            "state": self.state, "wbgt_c": self.wbgt_c, "breached": self.breached, "severity": self.severity,
            "level": self.level, "breach_started_at": _iso(self.breach_started_at), "last_sweep_at": _iso(self.last_sweep_at),
            "sweeps": self.sweeps, "queries_total": self.queries_total,
            # PARTIAL oranı: konumlandırma belirsizliğinin saha yarıçapına göre ne kadar büyük
            # olduğunun doğrudan ölçüsü. Demoda gösterilecek gerçek veri.
            "verifications_total": self.verifications_total, "partial_verifications": self.partial_verifications,
            "partial_rate_pct": round(100 * self.partial_verifications / self.verifications_total)
            if self.verifications_total else 0,
            "last_reconcile_at": _iso(self.last_reconcile_at),
            "jurisdiction": self.cfg.jurisdiction.to_dict(),
            "workers": [w.to_dict() for w in self.workers.values()], "coverage": self.coverage(),
            "qod_sessions": self.qod_sessions, "subscriptions": self.subscriptions,
            "ledger_size": len(self.ledger),
        }


# --------------------------------------------------------------------------- ücretsiz sinyaller
def apply_geofence_event(site: SiteRuntime, worker_id: str, kind: str, now: datetime) -> dict:
    """Geofence olayı (area-entered / area-left) — ŞEBEKE İTER, sorgu bütçesi harcanmaz (Move 1)."""
    w = site.workers.get(worker_id)
    if w is None:
        raise KeyError(worker_id)
    now = _dt(now)
    # FAIL-OPEN DEGIL: tanınmayan bir `kind` (ornegin "area_left" yazim hatasi) eskiden CIKIS
    # sayiliyor, isciyi kuyruktan dusuruyor ve `exited_at` ile mutabakati kalici olarak bloke
    # ediyordu. Bir yazim hatasi bir isciyi guvenlik gozunden silmemeli.
    if kind not in ("enter", "area-entered", "exit", "left", "area-left"):
        raise ValueError(f"unknown geofence event kind: {kind!r}")
    if kind in ("enter", "area-entered"):
        w.inside, w.exited_at, w.cleared = True, None, False
        w.verified_inside, w.last_verified_at = True, now
        # Maruziyet bu andan sayılır: ihlal 12:00'de başlasa da 12:30'da giren işçi 13:00'te
        # 30 dakikadır maruz kalmıştır, 60 değil. `rules.exposure_window()` bu alanı okur.
        w.entered_at = now
        detail = "entered the site (free geofence event)"
    else:
        w.inside, w.exited_at = False, now
        w.cleared, w.escalated, w.verdict, w.confidence = True, False, None, None
        w.verified_inside, w.last_verified_at = False, now
        w.state = "passive_watch"
        w.entered_at = None          # çıkan işçinin maruziyet penceresi kapanır
        detail = "left the site — drops out of the queue, its budget returns to the pool"
    w.last_signal_at = now
    return site.log(now, f"geofence_{kind}", detail, worker=w, source="geofencing-subscriptions")


def sweep_due(site: SiteRuntime, now: datetime, cfg: Config | None = None) -> bool:
    """Tarama sıklığı şiddete bağlı: marjinal ihlal 10 dk, şiddetli 2 dk (§8)."""
    cfg = cfg or site.cfg
    if site.last_sweep_at is None:
        return True
    interval = R.sweep_interval_minutes(site.level, cfg)
    return (_dt(now) - site.last_sweep_at) >= timedelta(minutes=interval)


# --------------------------------------------------------------------------- Nokia çağrıları (yalnızca facade üzerinden)
def _call(nac, name: str, *args, **kw):
    """facade.call → (data, source, latency_ms). Facade yoksa (None, 'none', 0)."""
    if nac is None:
        return None, "none", 0
    r = nac.call(name, *args, **kw)
    return r.data, r.source, r.latency_ms


# --------------------------------------------------------------------------- LLM planlayıcı (kuralların ALTINDA)
_PLANNER = None


def _planner():
    """Süreç ömrü boyunca tek planlayıcı. Anahtar yoksa `RulesPlanner` döner."""
    global _PLANNER
    if _PLANNER is None:
        from .llm_adapter import get_planner
        _PLANNER = get_planner()
    return _PLANNER


def reset_planner() -> None:
    """Test/demo için planlayıcı seçimini yeniden yaptır (ortam değişkeni değişince)."""
    global _PLANNER
    _PLANNER = None


def _rerank_with_model(site: SiteRuntime, plan: list, now: datetime, st: dict, cfg: Config) -> tuple[list, dict]:
    """Modele planı YENİDEN SIRALAMAYI önerttir; güvenlik kapısından geçir; kararı deftere yaz.

    Model plan ÜRETMEZ. Kuralların ürettiği listeyi görür ve sıra önerir. `guard_plan` öneriyi
    yapısal olarak denetler: yeni işçi ekleyemez, API seçemez, bütçeyi aşamaz, tekrar üretemez.
    Reddedilirse kuralların sırası uygulanır ve reddin gerekçesi defterde görünür.
    """
    p = _planner()
    name = getattr(p, "name", "deterministic-rules")
    if getattr(p, "provider", "none") == "none" or len(plan) < 2:
        return plan, {"used": False, "planner": name, "reason": "deterministic order applied"}

    snapshot = {
        "wbgt_c": site.wbgt_c, "legal_limit_c": cfg.jurisdiction.wbgt_limit_c,
        "severity": st.get("severity"), "level": st.get("level"),
        "jurisdiction": cfg.jurisdiction.code, "workers_in_plan": len(plan),
        "budget_per_sweep": cfg.budget.queries_per_site_per_minute,
    }
    before = [_worker_id_of(a) for a in plan]
    out = p.propose(snapshot, plan)
    v = dict(p.last_verdict())
    after = [_worker_id_of(a) for a in out]
    v["planner"] = name
    v["order_before"], v["order_after"] = before, after

    if v.get("used"):
        moved = sum(1 for i, wid in enumerate(after) if i < len(before) and before[i] != wid)
        v["moved"] = moved
        detail = (f"The model reordered the queue ({moved} positions changed) — it passed the gate"
                  if moved else "The model confirmed the rules' order")
        site.log(now, "planner_accepted", detail, source=name,
                 extra={"note": v.get("note"), "why": v.get("why"), "latency_ms": v.get("latency_ms")})
    else:
        why = v.get("violation") or v.get("reason") or "unknown"
        site.log(now, "planner_rejected",
                 f"The model's proposal was NOT APPLIED ({why}) — the rules' order stands", source=name,
                 extra={"violation": v.get("violation"), "guard_detail": v.get("detail"),
                        "offending": v.get("offending"), "latency_ms": v.get("latency_ms")})
    return (out if v.get("used") else plan), v


def _reconcile_roster(site: SiteRuntime, now: datetime, cfg: Config, force: bool) -> list[str]:
    """Periyodik mutabakat — olay akışı TEK DOĞRULUK KAYNAĞI DEĞİLDİR.

    Kaçan bir "giriş" olayı işçiyi sistemde görünmez yapar: kimse onu aramaz, çünkü sistem orada
    olduğunu hiç bilmez. Çözüm teslim garantisi aramak değil (operatör geri bildirimi, 9 Eylül 2026);
    çözüm işveren vardiya listesiyle mutabakat: rozetle giren ama şebekede hiç görünmemiş işçileri
    kuyruğa geri al.

    Böylece kaçan olay "görünmez işçi"den "durumu BİLİNMEYEN işçi"ye döner — ve sistem ikincisini
    zaten doğru ele alıyor, çünkü bilinmeyeni güvende saymıyor.

    Her ihlalin BAŞINDA (force) ve sonra `reconcile_minutes` aralıklarla çalışır.
    """
    if not force and site.last_reconcile_at is not None:
        if (now - site.last_reconcile_at) < timedelta(minutes=cfg.budget.reconcile_minutes):
            return []
    site.last_reconcile_at = now
    pulled: list[str] = []
    for w in site.workers.values():
        if not w.badge_in or w.escalated:
            continue
        if not w.reachable_via_operator:
            continue          # ev operatörü erişilemez — sorgulamak boşa bütçe, kapsamada ayrı sayılır
        if w.exited_at is not None:
            continue                      # gerçek bir çıkış olayı var — mutabakat onu geri çağırmaz
        if w.last_signal_at is not None:
            continue                      # şebekede görülmüş; olay akışı bu işçi için çalışıyor
        if w.inside and not w.cleared:
            continue                      # zaten kuyrukta
        # AYRI REZERV AÇMIYORUZ (keyfi bölme, her zaman yanlış oranda kalır). Aynı kuyruğa
        # girer; dengeyi mevcudiyet önseli kurar — listede olmak sahada olmak değildir.
        w.inside, w.cleared, w.state = True, False, "confirming"
        w.presence_prior = cfg.budget.presence_prior_roster_only
        pulled.append(w.worker_id)
        site.log(now, "roster_reconciliation",
                 "On the shift roster but never seen on the network — a missed entry event would make this "
                 "worker invisible. Pulled back into the queue as UNKNOWN, not assumed safe",
                 worker=w, source="worker-registry")
    if pulled:
        site.log(now, "reconciliation_summary",
                 f"Roster reconciliation pulled {len(pulled)} worker(s) back into the queue "
                 f"(the event stream is not treated as the single source of truth)",
                 source="rules", extra={"workers": pulled})
    return pulled


def _liveness_probe(site: SiteRuntime, w: WorkerRuntime, now: datetime, nac, cfg: Config, report: dict) -> bool | None:
    """Sıkı tazelikli konum sorgusu = canlılık kanıtı. Döner: True (canlı) · False (cevap yok) · None (belirsiz).

    Neden bu prob: `device-reachability-status` şebekenin TUTTUĞU durumu okur. Cihaz gerçekten
    kapandığında durumun "unreachable"a dönmesi periyodik kayıt zamanlayıcısına bağlıdır (yaygın
    varsayılan 54 dakika + marj), yani bayılma anında bize hiçbir şey söylemeyebilir. Sıkı tazelikli
    bir konum sorgusu ise şebekeyi taze bir fix üretmeye zorlar; bunun için cihazı sayfalaması
    gerekir. Cevap gelirse cihaz O AN yanıt vermiştir.

    KRİTİK DOĞRULAMA: dönen `lastLocationTime` gerçekten taze mi? Kırk dakikalık bir damga dönüyorsa
    şebeke taze fix yapmamış demektir ve bu probun tüm varsayımı çöker. O durumda "belirsiz" döneriz
    ve deftere ne olduğunu yazarız — sessizce canlı saymayız.
    """
    max_age = cfg.budget.strict_max_age_s
    data, source, ms = _call(nac, "location_verify", w.phone, site.lat, site.lng, site.radius_m, max_age)
    site.verifications_total += 1
    w.queries_used, site.queries_total = w.queries_used + 1, site.queries_total + 1
    res = (data or {}).get("verificationResult")
    ts = _dt((data or {}).get("lastLocationTime"))
    raw_age_s = (now - ts).total_seconds() if ts else None
    # GELECEK ZAMANLI DAMGA KANIT DEĞİLDİR. Önce negatif yaşı sıfıra kırpıyordum; bir denetim
    # bunun tazelik eşiğini TAMAMEN etkisiz kıldığını ölçtü (8 saat / 240 tarama: prob 200 kez
    # "canlı" dedi, sıfır bayılma hükmü) ve 0.95 güvenli bir `probable_collapse` hükmünü
    # siliyordu. Saat tutarsızlığı bir canlılık kanıtı değildir — reddedilir.
    CLOCK_SKEW_S = 5.0
    if raw_age_s is None:
        age_s, future = None, False
    elif raw_age_s < -CLOCK_SKEW_S:
        age_s, future = None, True
    else:
        age_s, future = max(0.0, raw_age_s), False
    fresh = age_s is not None and age_s <= max_age * 2      # damga gerçekten taze mi

    report["calls"].append({"worker_id": w.worker_id, "masked": w.masked, "api": "location-verification",
                            "pool": "liveness", "result": res, "source": source, "latency_ms": ms,
                            "freshness": "strict", "max_age_s": max_age,
                            "last_location_time": (data or {}).get("lastLocationTime"),
                            "answer_age_s": None if age_s is None else round(age_s, 1),
                            "reason": "liveness probe — a fresh fix requires the network to page the device"})

    if not _trusted(source) or res is None or res == "UNKNOWN":
        # UNKNOWN ana yolda da "güvenilir sinyal değil" sayılıyor (bkz. location_verify dalı).
        # Aynı cevabın burada canlılık kanıtı olması doğrudan çelişkiydi.
        site.log(now, "liveness_unknown",
                 f"Liveness probe: no usable answer ({res}) — the device is NOT counted as alive",
                 worker=w, source=source)
        return None
    if future:
        site.log(now, "liveness_inconclusive",
                 f"The position timestamp is in the future by {abs(raw_age_s):.0f}s — a clock inconsistency, "
                 f"not liveness evidence. The device is NOT counted as alive",
                 worker=w, source=source, extra={"raw_age_s": round(raw_age_s, 1)})
        return None
    if age_s is None:
        # Cevap geldi ama konum zaman damgası yok → şebeke taze bir fix üretmemiş. Canlılık kanıtı DEĞİL.
        site.log(now, "liveness_inconclusive",
                 "The network answered a fresh-fix request without a position timestamp — no fix was produced, "
                 "so this is NOT liveness evidence. The device is not counted as alive",
                 worker=w, source=source)
        return None
    if not fresh:
        site.log(now, "liveness_inconclusive",
                 f"The network answered but the position is {age_s:.0f}s old (limit {max_age * 2}s) — no fresh fix was "
                 f"produced, so this is NOT liveness evidence. Falling back to the reachability reading",
                 worker=w, source=source, extra={"answer_age_s": round(age_s, 1)})
        return None
    site.log(now, "liveness_confirmed",
             f"The device answered a fresh-fix request just now (fix age {age_s:.0f} s, limit {max_age * 2} s) — it responded "
             f"to paging, so this is not a collapse; the stale reachability reading is overruled",
             worker=w, source=source, extra={"answer_age_s": round(age_s, 1)})
    return True


def _worker_id_of(act) -> str:
    w = getattr(act, "worker", None) or {}
    return str(w.get("worker_id") or w.get("masked") or "")


def _trusted(source: str | None) -> bool:
    """Cevap gerçek operatörden mi geldi?

    `error(...)` (çağrı düştü) ve `fixture-fallback(...)` (yedeğe düşüldü) kaynaklı cevaplar
    GÜVENLİK HÜKMÜ üretemez: "API çöktü" ile "işçi bölge dışında" aynı şey değildir.
    Bu, ürünün "hata asla 'güvende' hükmü üretmez" iddiasının kod karşılığıdır.
    """
    s = source or ""
    return not (s.startswith("error(") or s.startswith("fixture-fallback("))


def _congestion(site: SiteRuntime, w: WorkerRuntime, now: datetime, nac, cfg: Config) -> tuple[str | None, str]:
    """Mikro-bölge başına tıkanıklık; `congestion_cache_minutes` boyunca tekrar sorulmaz (bütçe koruması)."""
    cached = site.congestion_cache.get(w.micro_zone)
    if cached and (now - cached[0]) <= timedelta(minutes=cfg.budget.congestion_cache_minutes):
        return cached[1], cached[2] + "(cache)"
    data, source, _ = _call(nac, "congestion_query", w.phone)
    level = None
    if isinstance(data, list) and data:
        level = data[0].get("congestionLevel")
    elif isinstance(data, dict):
        level = data.get("congestionLevel")
    if level is not None:
        site.congestion_cache[w.micro_zone] = (now, level, source)
        w.queries_used += 1
        site.queries_total += 1
    return level, source


# --------------------------------------------------------------------------- tarama
def step(site: SiteRuntime, now: datetime, nac=None, cfg: Config | None = None, wbgt_c: float | None = None) -> dict:
    """Bir tarama: site durumu → skorlama → plan → çağrılar → sınıflandırma → eskalasyon → kanıt defteri."""
    cfg = cfg or site.cfg
    now = _dt(now)
    # Yerel taklidin saatini simule zamana hizala. Fixture gercek duvar saatini yazarsa
    # urettigi zaman damgalari simule senaryoda "gelecekten" gelir; tazelik kontrolu anlamsizlasir.
    # Canli modda `fx` yalnizca yedek olarak durur, oraya dokunmak zararsizdir.
    _fx = getattr(getattr(nac, "nac", None), "fx", None) or getattr(nac, "fx", None)
    if _fx is not None and hasattr(_fx, "set_clock"):
        _fx.set_clock(now)
    if wbgt_c is not None:
        site.wbgt_c, site.wbgt_at = wbgt_c, now

    # ---- 1) site durumu (WBGT eşiği VEYA yaz yasağı saati)
    st = R.site_state(site.wbgt_c, now, cfg.jurisdiction, cfg, site.wbgt_at)
    site.breached, site.severity, site.level = st["breached"], st["severity"], st["level"]
    report: dict[str, Any] = {
        "site_id": site.site_id, "now": _iso(now), "wbgt_c": site.wbgt_c, "breached": st["breached"],
        "wbgt_known": st.get("wbgt_known"), "wbgt_stale": st.get("wbgt_stale"),
        "wbgt_age_minutes": st.get("wbgt_age_minutes"), "wbgt_at": _iso(site.wbgt_at),
        # İhlal WBGT eşiğinden mi yoksa yasak saatinden mi doğdu — ikisi farklı hukuki dayanak.
        "wbgt_breach": st.get("wbgt_breach"), "ban_breach": st.get("ban_breach"),
        "severity": st["severity"], "level": st["level"], "reason": st["reason"],
        "site_explain": [e.to_dict() for e in st["explain"]],
        "sweep_interval_min": R.sweep_interval_minutes(st["level"], cfg),
        "plan": [], "calls": [], "verdicts": [], "escalations": [],
        "budget": {"per_sweep": cfg.budget.queries_per_site_per_minute, "planned": 0, "spent": 0,
                   "skipped_for_budget": 0, "reserve_rechecks": 0},
        "jurisdiction": cfg.jurisdiction.to_dict(),
    }

    # ---- Move 1: ihlal yoksa sorgu yetkisi yok
    if not st["breached"]:
        quiet_from = len(site.ledger)          # EN BAŞTA: bu yolda doğan satırlar da rapora çıksın
        if not st.get("wbgt_known"):
            # ÖLÇÜM YOK ya da BAYAT — ve bu "ihlal bitti" DEMEK DEĞİLDİR.
            # Bir denetim ölçtü ki eskiden bu yol tüm işçi durumunu siliyordu: açık bir
            # `probable_collapse` vakası, sağlıkçı çağrılmışken, yalnızca meteoroloji beslemesi
            # sustuğu için sessizce kapanıyordu ve defter "Breach over" yazıyordu.
            # Beslemenin ölmesi ihlalin bitmesi değildir: AÇIK VAKALAR KORUNUR, yalnızca yeni
            # sorgu yetkisi doğmaz (amaç sınırlaması korunur).
            site.log(now, "wbgt_feed_unusable", st["reason"], source="meteorology-feed",
                     explain=st["explain"],
                     extra={"wbgt_c": site.wbgt_c, "wbgt_age_minutes": st.get("wbgt_age_minutes"),
                            "max_age_minutes": cfg.budget.wbgt_max_age_minutes,
                            "open_cases_preserved": [w.worker_id for w in site.workers.values() if w.escalated]})
            report["note"] = ("Move 1 — the WBGT threshold could NOT be assessed (no usable reading). "
                              "No new query authority arises, open cases are NOT closed, and this is NOT a "
                              "statement that the site is safe.")
        else:
            if site.state != "passive_watch":
                site.log(now, "site_cleared", "Breach over — open cases closed, the authority to query lapsed",
                         source="rules", explain=st["explain"])
            site.state = "passive_watch"
            site.breach_started_at = None
            for w in site.workers.values():
                w.state, w.escalated, w.verdict, w.confidence, w.score = "passive_watch", False, None, None, 0.0
            report["note"] = "Move 1 — no breach: only free geofence events are watched; NO query is made."
        site.last_sweep_at, site.sweeps = now, site.sweeps + 1
        report["state"] = site.state
        report["workers"] = [w.to_dict() for w in site.workers.values()]
        report["ledger_added"] = site.ledger[quiet_from:]
        return report

    breach_is_new = site.breach_started_at is None
    if breach_is_new:
        site.breach_started_at = now
        site.log(now, "breach_started", st["reason"], source="rules", explain=st["explain"],
                 extra={"wbgt_c": site.wbgt_c, "level": st["level"]})
    if site.state == "passive_watch":
        site.state = "alert"
    ledger_from = len(site.ledger)

    # ---- Move 1b: vardiya listesiyle mutabakat. Her ihlalin başında, sonra periyodik.
    reconciled = _reconcile_roster(site, now, cfg, force=breach_is_new)
    report["reconciled"] = reconciled

    # ---- 2) Move 2: skorla ve sırala
    candidates: list[dict] = []
    presumed_safe: list[dict] = []
    for w in site.workers.values():
        sig = w.to_signals()
        if w.inside and not w.cleared:
            d = R.exposure_detail(sig, site.breach_started_at, now, cfg, st["severity"])
            w.score = round(d.risk_score, 2)
            w.vulnerability = d.meta["vulnerability"]
            sig = w.to_signals()
            sig["exposure_explain"] = [e.to_dict() for e in d.explain]
            candidates.append(sig)
        else:
            presumed_safe.append(sig)

    budget = cfg.budget.queries_per_site_per_minute
    plan = R.plan_verification(candidates, budget, cfg, now, presumed_safe=presumed_safe,
                               breach_started_at=site.breach_started_at)
    unlimited = R.plan_verification(candidates, 10_000, cfg, now)

    # ---- Move 2b: model kuralların ALTINDA — planı yalnızca yeniden sıralayabilir (llm_adapter.py)
    plan, report["planner"] = _rerank_with_model(site, plan, now, st, cfg)

    main_planned = [a for a in plan if a.pool == "main"]
    report["budget"].update({
        "planned": len(plan), "skipped_for_budget": max(0, len(unlimited) - len(main_planned)),
        "reserve_rechecks": len([a for a in plan if a.pool == "reserve"]),
    })
    report["plan"] = [a.to_dict() for a in plan]
    report["ranked"] = [{"worker_id": c["worker_id"], "masked": c["masked"], "score": c["score"],
                         "vulnerability": c["vulnerability"], "explain": c.get("exposure_explain", [])}
                        for c in sorted(candidates, key=lambda c: -c["score"])]
    if report["budget"]["skipped_for_budget"]:
        site.log(now, "budget_exhausted",
                 f"Budget exhausted: {report['budget']['skipped_for_budget']} workers were not queried in this sweep "
                 f"(the list did not end, the budget did — §8 Move 2)", source="rules")

    # ---- 3) Move 3: planı sırayla uygula (maliyet merdiveni)
    unreachable_now: list[WorkerRuntime] = []
    for act in plan:
        w = site.workers.get(act.worker.get("worker_id"))
        if w is None:
            continue
        if act.api == "location_verify":
            # TAZELİK basamağı: gevşek → önbellek cevabı kabul (ucuz); sıkı → taze fix zorlar (pahalı).
            # Şebekede maliyeti belirleyen budur, "hüküm mü koordinat mı" değil.
            strict = act.freshness == "strict"
            max_age = cfg.budget.strict_max_age_s if strict else cfg.budget.loose_max_age_s
            data, source, ms = _call(nac, "location_verify", w.phone, site.lat, site.lng, site.radius_m, max_age)
            res = (data or {}).get("verificationResult")
            if res is not None and not _trusted(source):
                # Yedeğe düşmüş cevap işçiyi kuyruktan DÜŞÜREMEZ — aksi hâlde çöken bir API
                # sessizce "güvende" hükmü üretir. Doğrulanmadı sayılır, bayatlık artmaya devam eder.
                site.log(now, "untrusted_signal", f"Answer from an untrusted source ({source}) — counted as not verified",
                         worker=w, source=source)
                res = None
            w.queries_used, site.queries_total = w.queries_used + 1, site.queries_total + 1
            site.verifications_total += 1
            if res is not None:
                # UNKNOWN ve PARTIAL birer cevaptır (bütçeyi korumak için hemen tekrar sormayız) ama
                # GÜVENİLİR SİNYAL DEĞİLLERDİR: `last_signal_at` güncellenmez → bayatlık artmaya
                # devam eder, işçi sıralamada yükselir.
                w.last_verified_at = now
                if res not in ("UNKNOWN", "PARTIAL"):
                    w.last_signal_at = now
            if res == "TRUE":
                # `moving_out`a DOKUNULMAZ: yörünge bir saha sinyalidir (turnike çıkışı, vardiya amiri
                # kaydı), Location Verification'dan türetilemez. Eskiden PARTIAL'dan türetiliyordu;
                # operatör düzeltmesiyle o dayanak düştü — sahte sinyali korumak yerine kaldırdık.
                w.verified_inside, w.state = True, "confirming"
            elif res == "FALSE":
                w.verified_inside, w.inside, w.cleared, w.state = False, False, True, "passive_watch"
                site.log(now, "worker_cleared", "Location Verification: outside the zone — dropped out of the queue", worker=w, source=source)
            elif res == "PARTIAL":
                # PARTIAL GEOMETRİK bir ifadedir: belirsizlik bölgesi alanla KESİŞİYOR ama içinde
                # kalmıyor. Hareketle ilgisi YOKTUR. (Daha önce burada "çıkışa doğru hareket" diye
                # yorumlanıyordu — yanlıştı; operatör tarafından düzeltildi.) Konumlandırma
                # belirsizliği saha yarıçapıyla aynı mertebedeyse PARTIAL baskın cevap hâline gelir,
                # o yüzden "yetersiz kanıt" sayılır: doğrulanmadı, ve BİLİNMEYEN GÜVENDE DEĞİLDİR.
                site.partial_verifications += 1
                w.verified_inside, w.state = None, "confirming"
                site.log(now, "insufficient_evidence",
                         "PARTIAL — the uncertainty region intersects the zone without being contained in it. "
                         "Geometric, not a movement signal: counted as not verified, staleness keeps rising",
                         worker=w, source=source)
            else:  # UNKNOWN / hata → doğrulanmadı say, bir sonraki taramada tekrar dene
                w.verified_inside, w.state = None, "confirming"
            report["calls"].append({"worker_id": w.worker_id, "masked": w.masked, "api": "location-verification",
                                    "pool": act.pool, "result": res, "source": source, "latency_ms": ms,
                                    "reason": act.reason, "freshness": act.freshness, "max_age_s": max_age,
                                    "last_location_time": (data or {}).get("lastLocationTime")})
            rung = "rung 3, fresh fix demanded" if strict else "rung 2, cached answer accepted"
            shown = (f"NO ANSWER — operator API error ({source}); counted as UNKNOWN, not as safe" if res is None
                     else f"{res} ({rung}; coordinates NOT requested)")
            site.log(now, "location_verify", f"Inside the zone? → {shown}",
                     worker=w, source=source, explain=act.explain,
                     extra={"pool": act.pool, "freshness": act.freshness, "max_age_s": max_age,
                            "last_location_time": (data or {}).get("lastLocationTime")})
        elif act.api == "reachability":
            data, source, ms = _call(nac, "reachability", w.phone)
            reachable = (data or {}).get("reachable")
            if reachable is not None and not _trusted(source):
                site.log(now, "untrusted_signal", f"Answer from an untrusted source ({source}) — reachability counted as unknown",
                         worker=w, source=source)
                reachable = None
            w.reachable, w.last_reachability_at = reachable, now
            w.queries_used, site.queries_total = w.queries_used + 1, site.queries_total + 1
            if reachable:
                w.last_signal_at = now
                w.state = "confirming"
            else:
                unreachable_now.append(w)
            report["calls"].append({"worker_id": w.worker_id, "masked": w.masked, "api": "device-reachability-status",
                                    "pool": act.pool, "result": reachable, "source": source, "latency_ms": ms, "reason": act.reason})
            shown_r = ("yes" if reachable else "NO — the device went silent" if reachable is False
                       else f"NO ANSWER — operator API error ({source}); counted as unknown")
            site.log(now, "reachability", f"Is the device reachable? → {shown_r}", worker=w, source=source, explain=act.explain)
        elif act.api == "location_retrieve":
            data, source, ms = _call(nac, "location_retrieve", w.phone, cfg.budget.strict_max_age_s)
            area = (data or {}).get("area") or {}
            c = area.get("center") or {}
            w.queries_used, site.queries_total = w.queries_used + 1, site.queries_total + 1
            # KAPININ IKI CATLAGI (gizlilik denetimi):
            #  (a) cagri `center` dondurmezse bayrak set edilmiyordu → isci `escalated` kaldikca
            #      HER TARAMADA yeniden koordinat isteniyordu. Artik deneme de bir kez sayilir.
            #  (b) `_trusted()` bu dala uygulanmamisti → yedege dusmus uydurma bir koordinat
            #      gercekmis gibi saklanip saglikciya gosterilebilirdi.
            w.location_retrieved = True          # (a) deneme yapildi; tek seferlik kapi kapandi
            if c and _trusted(source):
                # KAYNAKTA KABALASTIRMA: alan adi `last_location_masked` maskeli oldugunu ima
                # ediyordu ama kod yuvarlamiyordu; API tam hassasiyetli koordinat yayiniyordu.
                # Deftere yazilan 3 ondalik (~110 m) dogruydu — ayni olcu burada da uygulanir.
                w.last_location = {"lat": round(float(c.get("latitude", 0)), 3),
                                   "lng": round(float(c.get("longitude", 0)), 3),
                                   "radius_m": area.get("radius"), "coarsened_decimals": 3,
                                   "at": _iso(now), "purpose": "where the medic has to run (one time only)"}
            elif c:
                site.log(now, "untrusted_signal",
                         f"A coordinate arrived from an untrusted source ({source}) — NOT stored and NOT shown "
                         f"to the medic. A fabricated position is worse than none",
                         worker=w, source=source)
            report["calls"].append({"worker_id": w.worker_id, "masked": w.masked, "api": "location-retrieval",
                                    "pool": act.pool, "result": bool(c), "source": source, "latency_ms": ms, "reason": act.reason})
            site.log(now, "location_retrieve", "A ONE-TIME coordinate after escalation — for the medic", worker=w,
                     source=source, explain=act.explain, extra={"coarse": {"lat": round(float(c.get("latitude", 0)), 3),
                                                                           "lng": round(float(c.get("longitude", 0)), 3)}})
    # ---- 4) §9: bayılma mı, ağ mı, pil mi, çıkış mı?
    for w in unreachable_now:
        zone_peers = [x for x in site.workers.values() if x.micro_zone == w.micro_zone and x.worker_id != w.worker_id]
        n_unreach = sum(1 for x in zone_peers if x.reachable is False)
        n_reach = sum(1 for x in zone_peers if x.reachable is True)
        level, csource = _congestion(site, w, now, nac, cfg)
        if not csource.endswith("(cache)"):
            report["calls"].append({"worker_id": w.worker_id, "masked": w.masked, "api": "congestion-insights",
                                    "pool": "distress", "result": level, "source": csource, "latency_ms": 0,
                                    "reason": f"micro-zone {w.micro_zone}: a network event or a medical one?"})
        # ---- CANLILIK PROBU (operatör tavsiyesi, 9 Eylül 2026)
        # Device Reachability, şebekenin TUTTUĞU durumu okur; cihaz gerçekten kapandığında durumun
        # "unreachable"a dönmesi periyodik kayıt zamanlayıcısına bağlıdır ve bir saati bulabilir.
        # Sıkı tazelikli bir konum sorgusu ise şebekeyi TAZE bir fix üretmeye zorlar: bunun için
        # cihazla bağlantı kurması, yani onu sayfalaması gerekir. Cevap gelirse cihaz O AN yanıt
        # vermiştir — bayat durum okumasından çok daha taze bir canlılık kanıtı, üstelik mesajlaşmaya
        # hiç girmeden, kendi altı API'mizin içinde kalarak.
        alive = _liveness_probe(site, w, now, nac, cfg, report)
        if alive is True and w.escalated:
            # Vaka ZATEN açık ve sağlıkçı çağrılmış. Cihazın sonradan cevap vermesi vakayı
            # KAPATMAZ: bir alarm, koşul düzeldi diye kendi kendine kapanmaz — insan onayı ister
            # (endüstriyel alarm yönetiminin "return to normal, unacknowledged" ilkesi).
            w.reachable, w.last_reachability_at, w.last_signal_at = True, now, now
            site.log(now, "liveness_after_escalation",
                     "The device answered a fresh-fix request AFTER the case was escalated. The case stays "
                     "open: an alarm does not close itself because the signal came back — a human closes it",
                     worker=w, source="rules")
            continue
        if alive is True:
            # Cihaz sayfalamaya cevap verdi → bayılma değil. Erişilemezlik bayat okumaydı.
            w.reachable, w.last_reachability_at, w.last_signal_at = True, now, now
            w.verdict, w.confidence, w.state = None, None, "confirming"
            continue
        res = R.classify_unreachable(w.to_signals(), n_reach, n_unreach, level, w.device_history, w.moving_out, cfg)
        w.verdict, w.confidence = res["verdict"], res["confidence"]
        esc = R.escalate(res["verdict"], res["confidence"], cfg, level)
        actions = list(esc.actions)
        if "notify_medic" in actions:
            w.escalated, w.state = True, "distress"
            site.state = "distress"
        elif w.state != "passive_watch":
            w.state = "alert"
        # garantili bant genişliği: sağlıkçı ↔ hekim video görüşmesi
        if "request_qod" in actions:
            data, source, ms = _call(nac, "qod_create", w.phone, cfg.qod_app_server_ipv4, cfg.qod_profile, 900)
            # QoD oturum istegi de bir sebeke cagrisi: sayilmazsa kart aritmetigi tutmuyor
            # (juri 83 + 7 toplayip 89 goruyordu). Denetim A / Y1.
            site.queries_total += 1
            sid = (data or {}).get("sessionId")
            if sid:
                site.qod_sessions[w.worker_id] = {"session_id": sid, "qos_profile": cfg.qod_profile,
                                                  "status": (data or {}).get("qosStatus"), "source": source, "at": _iso(now)}
                site.state = "emergency"
                site.log(now, "qod_session", f"Guaranteed bandwidth opened ({cfg.qod_profile}) — medic video assessment",
                         worker=w, source=source, extra={"session_id": sid})
            report["calls"].append({"worker_id": w.worker_id, "masked": w.masked, "api": "quality-on-demand",
                                    "pool": "emergency", "result": sid, "source": source, "latency_ms": ms,
                                    "reason": "medic ↔ physician video assessment"})
        if "provision_slice" in actions:
            # MOCK: Network Slicing NaC'de ayrı bir ürün; prototipte QoD + tıkanıklık kanıtıyla temsil edilir.
            site.log(now, "slice_requested", "Congestion is high → a dedicated slice requested for site-safety traffic (MOCK)",
                     worker=w, source="congestion-insights")
        entry = site.log(now, "distress_verdict", f"{res['verdict']} (confidence {res['confidence']:.2f}) → {', '.join(actions)}",
                         worker=w, source="rules", explain=res["explain"],
                         extra={"verdict": res["verdict"], "confidence": res["confidence"], "actions": actions,
                                "congestion": level, "congestion_source": csource,
                                "cluster": {"reachable": n_reach, "unreachable": n_unreach}})
        report["verdicts"].append({
            "worker_id": w.worker_id, "masked": w.masked, "name": w.name, "verdict": res["verdict"],
            "confidence": res["confidence"], "actions": actions, "congestion": level, "score": w.score,
            "cluster": {"reachable": n_reach, "unreachable": n_unreach},
            "explain": [e.to_dict() for e in res["explain"]], "escalate_explain": [e.to_dict() for e in esc.explain],
            "ledger_seq": entry["seq"], "state": w.state,
        })
        if "notify_medic" in actions:
            report["escalations"].append({"worker_id": w.worker_id, "masked": w.masked, "name": w.name,
                                          "confidence": res["confidence"], "actions": actions, "score": w.score})

    # ---- 4b) UZAMIS MARUZIYET: sessizlik tek sinyal degildir.
    # Yukaridaki siniflandirmanin tamami cihazin SUSMASINA dayaniyor. Ama telefonu cebinde,
    # sarji dolu ve erisilebilir bir isci de bayilabilir — sebeke bunu hic gormez, hicbir
    # sessizlik sinyali dogmaz. Kanun ise zaten isin DURMASINI emrediyor.
    limit_min = cfg.distress.prolonged_exposure_minutes
    for w in site.workers.values():
        if w.cleared or w.escalated or not w.inside or w.verdict is not None:
            continue
        if w.verified_inside is False:
            continue                   # bolge disinda oldugu DOGRULANMIS isci bu kuralin konusu degil
        _, exposure_min, _, _ = R.exposure_window(w.to_signals(), site.breach_started_at, now)
        if exposure_min < limit_min:
            continue
        esc = R.escalate("prolonged_exposure", 1.0, cfg, None)
        w.verdict, w.confidence, w.state = "prolonged_exposure", 1.0, "distress"
        confirmed = "verified inside" if w.verified_inside else "not confirmed outside"
        entry = site.log(now, "prolonged_exposure",
                         f"{confirmed.capitalize()} in a breached zone for {exposure_min:.0f} min "
                         f"(limit {limit_min}) while the device is still answering. No silence signal will "
                         f"ever fire here - but work should have stopped {exposure_min - limit_min:.0f} min ago",
                         worker=w, source="rules", explain=esc.explain,
                         extra={"exposure_minutes": round(exposure_min, 1), "limit_minutes": limit_min,
                                "actions": list(esc.actions), "reachable": w.reachable,
                                "verified_inside": w.verified_inside})
        report["verdicts"].append({
            "worker_id": w.worker_id, "masked": w.masked, "name": w.name, "verdict": "prolonged_exposure",
            "confidence": 1.0, "actions": list(esc.actions), "congestion": None, "score": w.score,
            "exposure_minutes": round(exposure_min, 1),
            "explain": [e.to_dict() for e in esc.explain],
            "escalate_explain": [e.to_dict() for e in esc.explain],
            "ledger_seq": entry["seq"], "state": w.state,
        })
        report["escalations"].append({"worker_id": w.worker_id, "masked": w.masked, "name": w.name,
                                      "confidence": 1.0, "actions": list(esc.actions), "score": w.score,
                                      "trigger": "prolonged_exposure"})

    site.last_sweep_at, site.sweeps = now, site.sweeps + 1
    site.prune_ledger(cfg)
    report["presence_record"] = site.presence_record()
    report["budget"]["spent"] = len(report["calls"])   # plan dışı acil çağrılar (congestion / QoD) dahil
    report["state"] = site.state
    report["workers"] = [w.to_dict() for w in site.workers.values()]
    report["ledger_added"] = site.ledger[ledger_from:]
    report["coverage"] = site.coverage()
    report["note"] = ("Move 2/3 — the budget was spent in rank order; a coordinate was taken only after escalation, once."
                      if report["calls"] else "There is a breach but no new worker to query (all of them freshly verified).")
    return report


def new_worker(worker_id: str, phone: str, **kw) -> WorkerRuntime:
    """Ham numarayı normalize ederek işçi kaydı üretir (tek yer)."""
    return WorkerRuntime(worker_id=worker_id, phone=normalize_phone(phone), **kw)
