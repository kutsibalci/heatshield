"""HeatShield eşik ve bütçe yapılandırması — hard-code yok, hepsi burada (+ env override).

Yargı alanı (Jurisdiction) tak-çıkar: Katar referans; Suudi Arabistan ve BAE konfigürasyon kaydı.
Kaynak: idea capture §6 (bölgesel ölçeklenebilirlik) ve §8 (doğrulama bütçesi politikası).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict, replace


@dataclass(frozen=True)
class SummerBan:
    """Yaz yasağı: start/end "AA-GG", from_/to "SS:DD" (yerel saat)."""

    start: str = "06-01"
    end: str = "09-15"
    from_: str = "10:00"
    to: str = "15:30"

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "from": self.from_, "to": self.to}


@dataclass(frozen=True)
class Jurisdiction:
    name: str
    code: str
    wbgt_limit_c: float = 32.1
    summer_ban: SummerBan = field(default_factory=SummerBan)
    tz_offset_hours: float = 3.0  # yerel saat için (naive datetime → yerel kabul edilir)
    legal_ref: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name, "code": self.code, "wbgt_limit_c": self.wbgt_limit_c,
            "summer_ban": self.summer_ban.to_dict(), "tz_offset_hours": self.tz_offset_hours, "legal_ref": self.legal_ref,
        }


QATAR = Jurisdiction(
    name="Qatar", code="QA", wbgt_limit_c=32.1,
    summer_ban=SummerBan("06-01", "09-15", "10:00", "15:30"), tz_offset_hours=3.0,
    legal_ref="Ministerial Decision No. 17/2021 (WBGT > 32.1 °C year-round; 10:00–15:30 ban, 1 Jun – 15 Sep)",
)
SAUDI = Jurisdiction(
    name="Saudi Arabia", code="SA", wbgt_limit_c=32.1,
    summer_ban=SummerBan("06-15", "09-15", "12:00", "15:00"), tz_offset_hours=3.0,
    legal_ref="MHRSD midday ban (12:00–15:00, 15 Jun – 15 Sep); WBGT threshold kept identical to the Qatar reference",
)
UAE = Jurisdiction(
    name="United Arab Emirates", code="AE", wbgt_limit_c=32.1,
    summer_ban=SummerBan("06-15", "09-15", "12:30", "15:00"), tz_offset_hours=4.0,
    legal_ref="MoHRE midday ban (12:30–15:00, 15 Jun – 15 Sep); WBGT threshold kept identical to the Qatar reference",
)
JURISDICTIONS: dict[str, Jurisdiction] = {"QA": QATAR, "SA": SAUDI, "AE": UAE}


@dataclass(frozen=True)
class BudgetConfig:
    """Site başına dakikalık sorgu bütçesi (idea capture §8)."""

    queries_per_site_per_minute: int = 20
    reserve_ratio: float = 0.1                    # presumed-safe yeniden kontrol rezervi
    sweep_minutes: dict = field(default_factory=lambda: {"marginal": 10, "severe": 2})
    severe_delta_c: float = 2.0                   # limit + delta → "severe"
    reverify_minutes: int = 10                    # bu süre içinde doğrulanan işçiye tekrar location_verify yok
    congestion_cache_minutes: int = 5             # aynı mikro-bölge için congestion sorgusu tekrar edilmez

    # --- maliyet merdiveninin TAZELİK ekseni (operatör geri bildirimi, 9 Eylül 2026)
    # Şebekede maliyeti belirleyen şey "hüküm mü koordinat mı" değil, TALEP EDİLEN TAZELİKTİR.
    # Önbellekten cevaplanabilen sorgu ucuzdur (veritabanı okuması). Taze bir fix gerekiyorsa
    # konumlandırma prosedürü çalışır: paging + RRC kurulumu + ölçüm + raporlama — asıl sinyalleşme
    # yükü budur ve hangi ucu çağırdığınızdan bağımsızdır.
    loose_max_age_s: int = 600                    # basamak 2: önbellek cevabı kabul (ucuz)
    strict_max_age_s: int = 60                    # basamak 3: taze fix zorlar (pahalı)
    reconcile_minutes: int = 15                   # periyodik mutabakat: durumu bilinmeyen işçileri tara
    # WBGT okuması bu yaştan eskiyse eşik DEĞERLENDİRİLEMEZ. Ölçüm yoksa "ihlal yok" demek,
    # ölen bir beslemeyi sessizce "güvende" hükmüne çevirir — bu, ürünün "hata asla güvende
    # hükmü üretmez" iddiasının tam istisnasıydı (9 Eylül denetim bulgusu).
    wbgt_max_age_minutes: int = 30
    # Vardiya listesinde olup şebekede HİÇ görünmemiş işçi: listede olmak sahada olmak
    # değildir. Ayrı bir rezerv açmak yerine (keyfi bölme, her zaman yanlış oranda kalır)
    # aynı kuyruğa girer ve maruziyeti bu önselle çarpılır — özel durum yerine tanımlı değer.
    presence_prior_roster_only: float = 0.5
    # Kanit defteri urunun KENDISI, o yuzden silinmez — ama bellek ici prototipte sinirsiz
    # buyuyemez: 8 saatlik bir vardiya 400 iscide 7.341 satir / 13,9 MB uretiyor (olculdu).
    # Uretimde kalici depolama + yasal saklama suresi gerekir; burada acik bir tavan koyuyoruz
    # ve kac satirin dusuruldugunu SAYIYORUZ. Sessizce budamak bir kanit defterinde kabul edilemez.
    ledger_max_entries: int = 5000

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class VulnerabilityWeights:
    first_week: float = 2.0
    prior_incident: float = 1.5
    shift_transition: float = 1.3
    default: float = 1.0
    first_week_days: int = 7

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DistressConfig:
    """Bayılma-vs-bitmiş-pil sınıflandırma eşikleri (idea capture §9)."""

    cluster_min_unreachable: int = 2       # ≥ bu kadar komşu da sessizse → ağ olayı
    collapse_min_confidence: float = 0.6   # medic bildirimi için alt sınır
    continuous_reachable_hours: float = 6  # bu kadar süre kesintisiz erişilebilir + düşüş → şüpheli
    staleness_cap_minutes: int = 30
    # UZAMIS MARUZIYET — sessizlik tek sinyal degildir.
    # Bütün sıkıntı çıkarımı cihazın SUSMASINA dayanıyordu. Ama telefonu cebinde, şarjı dolu
    # ve erişilebilir bir işçi de bayılabilir; şebeke bunu hiç görmez. Kanun ise zaten işin
    # DURMASINI emrediyor: eşik aşılmışken hâlâ içeride doğrulanan işçi, telefonu çalışsa da
    # hem uyum ihlali hem sağlık riskidir. Bu süre aşılınca sessizlikten BAĞIMSIZ eskalasyon.
    prolonged_exposure_minutes: int = 45

    def to_dict(self) -> dict:
        return asdict(self)


def _with_limit(jur: Jurisdiction, limit_c: float | None) -> Jurisdiction:
    return jur if limit_c is None else replace(jur, wbgt_limit_c=limit_c)


@dataclass
class Config:
    jurisdiction: Jurisdiction = QATAR
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    vulnerability: VulnerabilityWeights = field(default_factory=VulnerabilityWeights)
    distress: DistressConfig = field(default_factory=DistressConfig)
    # 9 Eylül 2026 canlı ölçümü: QoD oturumu QOS_E + 203.0.113.10 ile 200 döndü (sessionId geldi).
    # RFC1918 (10.x) adresi denenmedi; belgelenmiş bir kabul kanıtı olmadığı için varsayılanı
    # canlıda ÇALIŞTIĞI ölçülen değere çektik. 203.0.113.0/24 RFC 5737 dokümantasyon aralığıdır —
    # gerçek bir sunucu değil, ama yönlendirilebilir bir adres biçimi.
    qod_profile: str = "QOS_E"
    qod_app_server_ipv4: str = "203.0.113.10"   # sağlıkçı video sunucusu (demo; RFC 5737)
    min_perimeter_radius_m: float = 500.0
    # HS_WBGT_LIMIT_C: dağıtımın kendi eşiği. Her saha `with_jurisdiction()` ile kurulduğu için
    # burada saklanır; yoksa yargı alanı değişince yasal değere sessizce geri dönülüyordu.
    wbgt_limit_override_c: float | None = None

    @classmethod
    def from_env(cls, jurisdiction_code: str | None = None) -> "Config":
        e = os.environ.get
        jur = JURISDICTIONS.get((jurisdiction_code or e("HS_JURISDICTION", "QA")).upper(), QATAR)
        override = float(e("HS_WBGT_LIMIT_C")) if e("HS_WBGT_LIMIT_C") else None
        budget = BudgetConfig(
            queries_per_site_per_minute=int(e("HS_BUDGET_PER_MIN", "20")),
            reserve_ratio=float(e("HS_RESERVE_RATIO", "0.1")),
            sweep_minutes={"marginal": int(e("HS_SWEEP_MARGINAL_MIN", "10")), "severe": int(e("HS_SWEEP_SEVERE_MIN", "2"))},
            severe_delta_c=float(e("HS_SEVERE_DELTA_C", "2.0")),
        )
        vul = VulnerabilityWeights(
            first_week=float(e("HS_W_FIRST_WEEK", "2.0")),
            prior_incident=float(e("HS_W_PRIOR_INCIDENT", "1.5")),
            shift_transition=float(e("HS_W_SHIFT_TRANSITION", "1.3")),
        )
        return cls(jurisdiction=_with_limit(jur, override), budget=budget, vulnerability=vul,
                   qod_profile=e("HS_QOD_PROFILE", "QOS_E"), qod_app_server_ipv4=e("HS_QOD_APP_SERVER", "203.0.113.10"),
                   wbgt_limit_override_c=override)

    def with_jurisdiction(self, code: str) -> "Config":
        jur = JURISDICTIONS.get(code.upper(), self.jurisdiction)
        return replace(self, jurisdiction=_with_limit(jur, self.wbgt_limit_override_c))

    def to_dict(self) -> dict:
        return {
            "jurisdiction": self.jurisdiction.to_dict(), "budget": self.budget.to_dict(),
            "vulnerability": self.vulnerability.to_dict(), "distress": self.distress.to_dict(),
            "qod_profile": self.qod_profile,
        }
