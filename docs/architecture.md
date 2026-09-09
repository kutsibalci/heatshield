# HeatShield — Mimari

## 1. Tek cümle

Yasal WBGT eşiği aşıldığı anda ajan **sorgu yetkisi kazanır**, sahadaki her işçiyi maruziyet skoruna göre sıralar,
dakikalık bütçesini tepeden harcar, susan cihazları bayılma / ağ / pil / çıkış diye ayırır ve yalnızca hayatta kalan
vakayı bir insana götürür — hepsini zaman damgalı bir kanıt defterine yazarak.

## 2. Katmanlar

```
apps/api/main.py            FastAPI — saha/işçi kayıtları, geofence webhook'u, tarama ucu, 5 tek-tuş demo, SafeFacade
  └── agent/policy.py       Tarama motoru: SiteRuntime / WorkerRuntime / step() — sıra, durum ve Nokia çağrıları
        └── rules/heatshield.py   SAF fonksiyonlar: site_state, exposure_score, plan_verification,
        │                          classify_unreachable, escalate  (ağ yok, saat parametre, explain[] üretir)
        └── rules/config.py       Jurisdiction (QA/SA/AE) + bütçe + kırılganlık + distress eşikleri (HS_* env)
  └── nac_client/           TEK Nokia giriş noktası (auth, retry, circuit breaker, maskeleme, 3 mod)
apps/simulator/             Nokia SIMULATOR'ün yerel taklidi (gerçek path'ler + /_sim/emit)
apps/web/index.html         Vanilla HTML/JS konsol (build yok, CDN yok)
```

Nokia çağrısı **yalnızca** `nac_client` üzerinden. Politika motoru bile `nac_facade.call(<metot>)` kullanır; testlerde
facade değiştirilerek tüm tarama mantığı ağsız koşulur.

## 3. Ajan döngüsü (idea capture §5.2)

```
   ┌──────────────── pasif izleme (passive_watch) ────────────────┐
   │  Geofencing Subscriptions → area-entered / area-left          │  ÜCRETSİZ
   │  WBGT beslemesi                                               │  sorgu yetkisi YOK
   └───────────────────────────┬───────────────────────────────────┘
                               │ site_state(): WBGT > 32.1 °C  VEYA  yaz yasağı saati
                               ▼
                          alert  ── exposure_score() ─▶ sıralama
                               │        severity × exposure_minutes × staleness × vulnerability
                               ▼
                     plan_verification()  ── bütçe: 20 sorgu/tarama, %10 rezerv
                               │   ① location_verify  (ucuz, evet/hayır)
                               │   ② reachability     (yalnızca "içeride" doğrulananlar)
                               │   ③ location_retrieve(YALNIZCA eskalasyon sonrası)
                               ▼
                        confirming ──▶ susan cihaz ──▶ classify_unreachable()
                                                        küme testi + tıkanıklık + cihaz geçmişi + yörünge
                               ┌───────────────┬───────────────┬──────────────┐
                        probable_collapse  probable_network  probable_battery  probable_left
                               │                │                 │               │
                     notify_medic + QoD    log_only(+dilim)   süpervizöre     log_only
                        (distress → emergency)
```

## 4. Doğrulama bütçesi politikası (§8) — ürünün asıl zorluğu

400 kişilik bir sahayı dakikada tek tek yoklamak mümkün değil. Ajan üç hamle yapar:

**Move 1 — İhlal yoksa hiçbir şey harcama.** Geofence olayları şebeke tarafından itilir, bedava. Sorgu yetkisi
yalnızca yasal ihlalde doğar. Bu aynı zamanda hukuki tasarım: sistem sessizce üretkenlik gözetimine dönüşemez.

**Move 2 — Sırala, tepeden harca, bütçe bitince dur.**
```
score = severity × exposure_minutes × staleness × vulnerability
```
| Çarpan | Anlamı | Nereden |
|---|---|---|
| `severity` | WBGT limitin ne kadar üstünde (32.4 °C ile 36 °C aynı acil durum değil) | `site_state()` |
| `exposure_minutes` | İhlal başlangıcından beri temizleyici sinyal yok | ihlal saati |
| `staleness` | Son güvenilir sinyalden bu yana geçen süre (30 dk'da tavan) | işçi durumu |
| `vulnerability` | İlk hafta 2.0 · önceki olay 1.5 · vardiya geçişi 1.3 · varsayılan 1.0 | işçi kaydı |

Bütçe bitince **liste bitmese de durulur** ve kaç işçinin sorgulanmadığı hem rapora hem deftere yazılır
(`budget_exhausted`). Ekranda kesikli kırmızı kutular bunu gösterir — sessiz kırpma yok.

**Move 3 — Maliyet merdivenini yalnızca gerekince tırman.** `location_verify` bir hüküm döner ve ucuzdur;
`location_retrieve` koordinat döner ve **yalnızca eskalasyon sonrası** çağrılır. Bu hem maliyet hem gizlilik argümanıdır.

**Temizleme kuralları.** Geofence çıkış olayı, "bölge dışında" hükmü veya sağlıkçı teyidi işçiyi kuyruktan düşürür ve
bütçesini havuza döndürür. Bütçenin %10'u, çıkış olayına sonsuza dek güvenmemek için presumed-safe işçilerin
yeniden kontrolüne ayrılır (bayat çıkış olayı sessiz bir hata modudur).

## 5. Bayılma mı, bitmiş pil mi? (§9)

| Kanıt | Kaynak | Etkisi |
|---|---|---|
| **Küme testi** — aynı mikro-bölgede ≥2 cihaz birlikte sustu | Reachability | ağ olayı → alarm yok |
| **Tıkanıklık** — servis hücresi `High` | Congestion Insights | ağ olayı ihtimalini artırır, dilim talebini tetikler |
| **Cihaz geçmişi** — her gün aynı saatte susuyor / 6+ saattir kesintisiz açıktı | defter geçmişi | pil vs şüpheli düşüş |
| **Yörünge** — son doğrulama çıkışa doğru (PARTIAL) mu, bölge içinde sabit mi | Location Verification | "sahadan ayrıldı" vs "bayılma" |

Karar + güven skoru + kanıt listesi deftere yazılır; sağlıkçı neye cevap verdiğini bilir. `collapse_min_confidence`
(0.6) altında kalan vakalar insana gitmez, bir sonraki taramada yeniden kontrol edilir.

## 6. Yargı alanı = yapılandırma kaydı

```python
QATAR = Jurisdiction("Katar", "QA", wbgt_limit_c=32.1, summer_ban=SummerBan("06-01","09-15","10:00","15:30"), tz_offset_hours=3)
SAUDI = ... SummerBan("06-15","09-15","12:00","15:00")
UAE   = ... SummerBan("06-15","09-15","12:30","15:00"), tz_offset_hours=4
```
`/v1/demo/jurisdiction` aynı anda üç yargı alanını yan yana koyar: 11:45 Doha'da çalışma yasak, Riyad'da serbest,
Dubai'de (12:45 yerel) yasak. Jüri üyesi kendi pazarını bir config satırı olarak görür.

## 7. Gizlilik ve hukuki dayanak (§10)

- **Amaç sınırlaması:** ihlal yoksa sorgu yetkisi yok (`/v1/demo/no-breach`: iki tarama, sıfır çağrı).
- **Veri minimizasyonu mimaridir:** varsayılan sorgu hüküm döner, koordinat değil. Koordinat yalnızca olası bayılmada,
  bir kez; deftere 3 ondalıklı kaba özet yazılır.
- **Sürekli konum geçmişi yok** — `/v1/state` içinde `location_history_size: 0`.
- **Ham numara hiçbir yanıtta yok:** `WorkerRuntime.to_dict()` maskeli numara + HMAC-SHA256 hash döner; loglar `MaskingFilter` ile maskelenir.
- **Dürüst kapsama metriği (§9A):** rozetle girip şebekede görünmeyen işçiler `coverage.missing` altında listelenir —
  telefonu olmayan işçi görünmez, sistem bunu saklamak yerine sabah mutabakat görevi olarak raporlar.

## 8. Dayanıklılık

`nac_client`: timeout 4 sn, 2 retry, API başına circuit breaker. `SafeFacade` hatayı yutar ve `error(<kind>)` kaynağı
yazar. Kritik davranış: **hata durumunda kimse "güvende" sayılmaz** — `verified_inside` `None` kalır, işçi kuyrukta
kalır, defter "bilinmiyor" yazar (`/v1/demo/api-down`).

## 9. Bilinçli sadeleştirmeler (prototip)

- Kalıcılık yok (bellek içi). Üretimde defter → Supabase/Postgres, TTL ve denetçi dışa aktarımı.
- Network Slicing gerçek çağrı değil (`# MOCK`), tıkanıklık kanıtıyla defter kaydı.
- Zamanlayıcı yok: taramalar `POST /v1/sites/{id}/sweep` ile tetiklenir (demo bunları sırayla oynatır);
  `sweep_due()` üretimdeki cadence mantığını zaten içerir.
- LLM yok: `llm_adapter.py` boş arayüz. Faz 2'de model, **kuralların ürettiği aday aksiyonları** yeniden sıralar —
  yeni API çağrısı üretemez, bütçeyi aşamaz (guard-rail: kurallar üstte, model altta).
