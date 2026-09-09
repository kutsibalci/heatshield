"""FixtureBackend — Nokia SIMULATOR'ün yerel taklidi.

Profil (telefon başına) → CAMARA biçiminde yanıt üretir. Hem NacClient'ın `fixture` modu hem de
`apps/simulator` HTTP mock sunucusu bu sınıfı kullanır; böylece tek fixture seti iki yerde de aynı davranır.

Profil alanları (hepsi opsiyonel, DEFAULT_PROFILE ile birleşir):
  recycled_date        "YYYY-MM-DD" | None   numara bu tarihte yeni aboneye tahsis edildi
  tenure_since         "YYYY-MM-DD"          mevcut abonenin hattı aldığı tarih
  contract_type        PAYG | PAYM | Business
  age_check            "true" | "false" | "not_available"
  verified_status      bool                  operatör kimliği belgeyle doğruladı mı
  kyc                  {name, given_name, family_name, birthdate, address, email, id_document}
  number_verified      bool                  Number Verification cevabı
  device_phone_number  str | None            cihazın gerçek numarası (verify: eşitlik kontrolü için)
  sim_swap_at          ISO datetime | None
  device_swap_at       ISO datetime | None
  call_forwarding      ["inactive"] | ["unconditional"] | ["conditional_busy", ...]
  reachable            bool ; connectivity ["DATA","SMS"]
  roaming              bool ; country_code int ; country_name [str]
  location             {"lat":..,"lng":..,"radius":..} | None   (None → location UNKNOWN)
  congestion           Low | Medium | High
  qod_available        bool
  consent              {"<scope>": "valid" | "PENDING" | "REVOKED"} (varsayılan: hepsi valid)
  latency_ms           int                   yapay gecikme (demo: yavaş API)
  fail                 None | "timeout" | 500 | 429 | 403   hata enjeksiyonu (demo: circuit breaker)
"""
from __future__ import annotations

import copy
import json
import math
import uuid
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from typing import Any, Callable

from .errors import NacError, NacTimeout

DEFAULT_PROFILE: dict[str, Any] = {
    "recycled_date": None,
    "tenure_since": "2018-01-01",
    "contract_type": "PAYM",
    "age_check": "true",
    "verified_status": True,
    "kyc": {},
    "number_verified": True,
    "device_phone_number": None,
    "sim_swap_at": None,
    "device_swap_at": None,
    "call_forwarding": ["inactive"],
    "reachable": True,
    "connectivity": ["DATA", "SMS"],
    "roaming": False,
    "country_code": 90,
    "country_name": ["Turkey"],
    "location": None,
    "congestion": "Low",
    "qod_available": True,
    "consent": {},
    "latency_ms": 0,
    "fail": None,
}


def _parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_date(v: str | None) -> date | None:
    if not v:
        return None
    return date.fromisoformat(v[:10])


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class FixtureBackend:
    """Telefon → profil deposu + CAMARA cevap üreticileri."""

    def __init__(self, profiles: dict[str, dict] | None = None, now: Callable[[], datetime] | None = None):
        self.profiles: dict[str, dict] = {}
        self.subscriptions: dict[str, dict] = {}
        self.qod_sessions: dict[str, dict] = {}
        self._now = now or (lambda: datetime.now(timezone.utc))
        for phone, prof in (profiles or {}).items():
            self.set_profile(phone, prof)

    # ---- profil yönetimi -------------------------------------------------
    @classmethod
    def from_json(cls, path: str | Path, **kw) -> "FixtureBackend":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data.get("profiles", data), **kw)

    def set_profile(self, phone: str, profile: dict) -> None:
        merged = copy.deepcopy(DEFAULT_PROFILE)
        merged.update(profile or {})
        self.profiles[phone] = merged

    def update_profile(self, phone: str, patch: dict) -> dict:
        prof = self.profiles.get(phone) or copy.deepcopy(DEFAULT_PROFILE)
        prof.update(patch)
        self.profiles[phone] = prof
        return prof

    def get(self, phone: str) -> dict:
        return self.profiles.get(phone) or copy.deepcopy(DEFAULT_PROFILE)

    def now(self) -> datetime:
        return self._now()

    def set_clock(self, at: datetime | None) -> None:
        """Yerel taklidin saatini disaridan hizala.

        Neden gerekli: fixture gercek duvar saatini yaziyordu, demo/test ise simule bir tarihte
        kosuyor. Uretilen `lastLocationTime` damgalari simule `now`a gore GELECEKTEN geliyordu
        ve canlilik probunun tazelik kontrolunu tamamen etkisiz kiliyordu (denetim bulgusu K1).
        Yerel taklit, taklit ettigi dunyanin saatini kullanmali.
        """
        if at is not None:
            self._now = lambda: at

    # ---- hata / gecikme enjeksiyonu (NacClient fixture modunda uygulanır) ----
    def preflight(self, api: str, phone: str | None) -> None:
        if not phone:
            return
        prof = self.get(phone)
        fail = prof.get("fail")
        if fail == "timeout":
            raise NacTimeout(api, 0)
        if isinstance(fail, int):
            from .errors import error_from_status
            raise error_from_status(api, fail, "fixture: enjekte edilmiş hata")

    # ---- CAMARA cevapları --------------------------------------------------
    def number_recycling(self, phone: str, specified_date: str) -> dict:
        prof = self.get(phone)
        rd = _parse_date(prof.get("recycled_date"))
        sd = _parse_date(specified_date)
        return {"phoneNumberRecycled": bool(rd and sd and rd > sd)}

    def call_forwardings(self, phone: str) -> list[str]:
        return list(self.get(phone).get("call_forwarding") or ["inactive"])

    def unconditional_call_forwarding(self, phone: str) -> dict:
        return {"active": "unconditional" in self.call_forwardings(phone)}

    def kyc_tenure(self, phone: str, tenure_date: str) -> dict:
        prof = self.get(phone)
        since = _parse_date(prof.get("tenure_since"))
        td = _parse_date(tenure_date)
        # tenureDateCheck: abone, tenureDate'ten beri (veya daha eskiden) bu hatta mı?
        return {"tenureDateCheck": bool(since and td and since <= td), "contractType": prof.get("contract_type")}

    def tenure_days(self, phone: str) -> int:
        since = _parse_date(self.get(phone).get("tenure_since"))
        return (self.now().date() - since).days if since else 0

    def kyc_age(self, phone: str, age_threshold: int, **fields) -> dict:
        prof = self.get(phone)
        out = {"ageCheck": prof.get("age_check", "not_available"), "verifiedStatus": prof.get("verified_status")}
        if fields:
            out["identityMatchScore"] = self._match_score(prof, fields)
        return out

    def _match_score(self, prof: dict, fields: dict) -> int:
        kyc = prof.get("kyc") or {}
        scores = []
        for k, v in fields.items():
            if v is None:
                continue
            ref = kyc.get(k)
            if ref is None:
                continue
            scores.append(100 if str(ref).strip().lower() == str(v).strip().lower() else _fuzzy(str(ref), str(v)))
        return int(sum(scores) / len(scores)) if scores else 0

    def kyc_match(self, phone: str, **fields) -> dict:
        prof = self.get(phone)
        kyc = prof.get("kyc") or {}
        out: dict[str, Any] = {}
        for k, v in fields.items():
            if v is None:
                continue
            camel = _camel(k)
            ref = kyc.get(k)
            if ref is None:
                out[f"{camel}Match"] = "not_available"
                continue
            score = 100 if str(ref).strip().lower() == str(v).strip().lower() else _fuzzy(str(ref), str(v))
            out[f"{camel}Match"] = "true" if score >= 80 else "false"
            if k not in ("birthdate", "id_document", "gender", "country"):
                out[f"{camel}MatchScore"] = score
        return out

    def number_verify(self, phone: str) -> dict:
        prof = self.get(phone)
        dev = prof.get("device_phone_number")
        if dev is not None:
            return {"devicePhoneNumberVerified": dev == phone}
        return {"devicePhoneNumberVerified": bool(prof.get("number_verified"))}

    def sim_swap_check(self, phone: str, max_age_h: int | None) -> dict:
        at = _parse_dt(self.get(phone).get("sim_swap_at"))
        window = timedelta(hours=max_age_h if max_age_h is not None else 240)
        return {"swapped": bool(at and self.now() - at <= window)}

    def sim_swap_date(self, phone: str) -> dict:
        at = self.get(phone).get("sim_swap_at")
        return {"latestSimChange": at}

    def device_swap_check(self, phone: str, max_age_h: int | None) -> dict:
        at = _parse_dt(self.get(phone).get("device_swap_at"))
        window = timedelta(hours=max_age_h if max_age_h is not None else 240)
        return {"swapped": bool(at and self.now() - at <= window)}

    def device_swap_date(self, phone: str) -> dict:
        return {"latestDeviceChange": self.get(phone).get("device_swap_at"), "monitoredPeriod": 120}

    def reachability(self, phone: str) -> dict:
        prof = self.get(phone)
        return {
            "device": {"phoneNumber": phone},
            "lastStatusTime": self.now().isoformat(),
            "reachable": bool(prof.get("reachable")),
            "connectivity": list(prof.get("connectivity") or []) if prof.get("reachable") else [],
        }

    def roaming(self, phone: str) -> dict:
        prof = self.get(phone)
        return {
            "device": {"phoneNumber": phone},
            "lastStatusTime": self.now().isoformat(),
            "roaming": bool(prof.get("roaming")),
            "countryCode": prof.get("country_code"),
            "countryName": prof.get("country_name"),
        }

    def location_retrieve(self, phone: str, max_age: int | None = None) -> dict:
        loc = self.get(phone).get("location")
        if not loc:
            raise NacError("not_found", "location-retrieval", "LOCATION_RETRIEVAL.UNABLE_TO_LOCATE", 404)
        return {
            "lastLocationTime": self.now().isoformat(),
            "area": {"areaType": "CIRCLE", "center": {"latitude": loc["lat"], "longitude": loc["lng"]}, "radius": loc.get("radius", 500)},
        }

    def location_verify(self, phone: str, lat: float, lng: float, radius: float, max_age: int | None = None) -> dict:
        p = self.get(phone)
        loc = p.get("location")
        if not loc:
            return {"verificationResult": "UNKNOWN", "lastLocationTime": None}
        # SIKI TAZELIK + ULASILAMAYAN CIHAZ: taze bir fix, sebekenin cihazi SAYFALAMASINI gerektirir.
        # Kapali ya da menzil disindaki bir cihaz sayfalamaya cevap veremez, dolayisiyla taze konum
        # da uretemez. Fixture bunu modellemezse "canlilik probu" anlamsizlasir — kapali telefon
        # canli gorunur. Gevsek tazelikte ise onbellekteki son konum donebilir, bu gercekcidir.
        strict = max_age is not None and max_age <= 120
        # `stale_reachability`: cihaz ASLINDA canli, ama device-status bayat bir "erisilemez"
        # okumasi donduruyor (periyodik kayit zamanlayicisi henuz guncellenmedi). Taze fix
        # istegi bu cihazi sayfalar ve CEVAP ALIR — canlilik probunun tam olarak yakaladigi durum.
        if strict and p.get("reachable") is False and not p.get("stale_reachability"):
            return {"verificationResult": "UNKNOWN", "lastLocationTime": None}
        d = haversine_m(loc["lat"], loc["lng"], lat, lng)
        dev_r = loc.get("radius", 500)
        if d + dev_r <= radius:
            res, rate = "TRUE", None
        elif d - dev_r <= radius:
            overlap = max(0.0, min(1.0, (radius - (d - dev_r)) / (2 * dev_r))) if dev_r else 0.5
            res, rate = "PARTIAL", int(overlap * 100)
        else:
            res, rate = "FALSE", None
        out = {"verificationResult": res, "lastLocationTime": self.now().isoformat()}
        if rate is not None:
            out["matchRate"] = rate
        return out

    def congestion_query(self, phone: str, start=None, end=None) -> list[dict]:
        level = self.get(phone).get("congestion", "Low")
        t0 = self.now()
        return [{
            "timeIntervalStart": t0.isoformat(),
            "timeIntervalStop": (t0 + timedelta(hours=1)).isoformat(),
            "congestionLevel": level,
            "confidenceLevel": 85,
        }]

    def consent(self, phone: str, scopes: list[str], purpose: str) -> dict:
        prof = self.get(phone)
        table = prof.get("consent") or {}
        info = []
        for s in scopes:
            st = table.get(s, "valid")
            info.append({
                "scopes": [s], "purpose": purpose,
                "statusValidForProcessing": st == "valid",
                "statusReason": None if st == "valid" else st,
                "expirationDate": None,
            })
        return {"statusInfo": info, "captureUrl": None if all(i["statusValidForProcessing"] for i in info) else "https://consent.example/capture"}

    # ---- abonelikler / QoD -------------------------------------------------
    def create_subscription(self, kind: str, body: dict) -> dict:
        sid = str(uuid.uuid4())
        sub = {
            "id": sid,
            "kind": kind,
            "protocol": body.get("protocol", "HTTP"),
            "sink": body.get("sink"),
            "types": body.get("types", []),
            "config": body.get("config", {}),
            "startsAt": self.now().isoformat(),
            "expiresAt": (body.get("config") or {}).get("subscriptionExpireTime"),
            "status": "ACTIVE",
        }
        self.subscriptions[sid] = sub
        return {k: v for k, v in sub.items() if k != "kind"}

    def get_subscription(self, sid: str) -> dict:
        sub = self.subscriptions.get(sid)
        if not sub:
            raise NacError("not_found", "subscriptions", "abonelik yok", 404)
        return {k: v for k, v in sub.items() if k != "kind"}

    def delete_subscription(self, sid: str) -> dict:
        sub = self.subscriptions.pop(sid, None)
        if not sub:
            raise NacError("not_found", "subscriptions", "abonelik yok", 404)
        return {"id": sid}

    def list_subscriptions(self, kind: str | None = None) -> list[dict]:
        return [{k: v for k, v in s.items() if k != "kind"} for s in self.subscriptions.values() if kind is None or s["kind"] == kind]

    def qod_create(self, phone: str, body: dict) -> dict:
        prof = self.get(phone)
        sid = str(uuid.uuid4())
        sess = {
            "sessionId": sid,
            "device": {"phoneNumber": phone},
            "applicationServer": body.get("applicationServer"),
            "qosProfile": body.get("qosProfile"),
            "duration": body.get("duration", 3600),
            "startedAt": self.now().isoformat(),
            "expiresAt": (self.now() + timedelta(seconds=body.get("duration", 3600))).isoformat(),
            "qosStatus": "AVAILABLE" if prof.get("qod_available", True) else "UNAVAILABLE",
        }
        self.qod_sessions[sid] = sess
        return sess

    def qod_get(self, sid: str) -> dict:
        s = self.qod_sessions.get(sid)
        if not s:
            raise NacError("not_found", "qod", "oturum yok", 404)
        return s

    def qod_delete(self, sid: str) -> dict:
        s = self.qod_sessions.pop(sid, None)
        if not s:
            raise NacError("not_found", "qod", "oturum yok", 404)
        return s

    def qod_list(self, phone: str) -> list[dict]:
        return [s for s in self.qod_sessions.values() if s["device"]["phoneNumber"] == phone]

    # ---- CloudEvents üretimi (simülatörün webhook tetiklemesi için) --------
    def cloud_event(self, event_type: str, subscription_id: str, phone: str, extra: dict | None = None) -> dict:
        return {
            "id": str(uuid.uuid4()),
            "source": "local-simulator",
            "type": event_type,
            "specversion": "1.0",
            "datacontenttype": "application/json",
            "time": self.now().isoformat(),
            "data": {"subscriptionId": subscription_id, "device": {"phoneNumber": phone}, **(extra or {})},
        }


def _camel(snake: str) -> str:
    parts = snake.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _fuzzy(a: str, b: str) -> int:
    """Basit benzerlik: ortak karakter oranı (0-100). Prototip için yeterli."""
    a, b = a.lower(), b.lower()
    if not a or not b:
        return 0
    from difflib import SequenceMatcher
    return int(SequenceMatcher(None, a, b).ratio() * 100)
