# API Erişilebilirlik Kaydı — HeatShield

> Spec kuralı 0.3/4: seçilen fikrin API'leri spec'te gerçekten var mı, tek tek doğrulandı mı? Path'ler `network-as-code`
> Python SDK v10.0.0'ın `raw_client.py` dosyalarından çıkarıldı (`../../_ortak/docs/nac-api-reference.md`). Canlı SIMULATOR
> planı sonuçları bu tabloya işlenecek.

| API | Path (SDK v10.0.0'dan doğrulandı) | HeatShield'daki rolü | Prototipte kaynak | SIMULATOR planında test | Not |
|---|---|---|---|---|---|
| Geofencing Subscriptions | `POST /geofencing-subscriptions/v0.3/subscriptions`, `GET/DELETE …/{id}` | **Omurga.** Saha çevresine giriş/çıkış olayları — cihazda uygulama yok, **sorgu bütçesi harcanmaz** (§8 Move 1) | `nac_client.geofence_subscribe` → fixture / yerel simülatör | ✅ 200 · 228 ms · 09.09.2026 | Sink `PUBLIC_BASE_URL/webhooks/geofence`, `sinkCredential` ACCESSTOKEN. Abonelik başına **tek** olay tipi (iki tip → 400); işçi başına iki abonelik açılır. Yarıçap alt sınırı 500 m varsayımı **doğrulanmadı** — sonda 300 m ile 200 aldı |
| Location Verification | `POST /location-verification/v1/verify` | Çıkış olayı olmayan işçi gerçekten içeride mi? **Koordinat değil hüküm** (TRUE/FALSE/UNKNOWN/PARTIAL) | `nac_client.location_verify` | ✅ 200 · 152 ms · 09.09.2026 | Maliyet merdiveninin ilk basamağı. PARTIAL → sınırda: temkinli "içeride" + yörünge sinyali. **Sandbox'ta hüküm talep edilen alana göre değişmiyor — PARTIAL oranı ölçülemez** (aşağıda) |
| Device Reachability Status | `POST /device-status/device-reachability-status/v1/retrieve` | İhlal bölgesinde susan cihaz = olası bayılma adayı | `nac_client.reachability` | ✅ 200 · 145 ms · 09.09.2026 | Yalnızca "içeride" doğrulanmış işçiler için (ikinci basamak) |
| Congestion Insights | `POST /congestion-insights/v0/query` | İki rol: (1) susan cihaz tıbbi olay mı ağ olayı mı, (2) tıkanıklık yüksekse dilim talebi | `nac_client.congestion_query` | ✅ 200 · 139 ms · 09.09.2026 | Mikro-bölge başına 5 dk önbellek (`HS_*`/`congestion_cache_minutes`) — bütçe koruması |
| Quality on Demand | `POST /quality-on-demand/v1/sessions` (+ `GET/DELETE …/{id}`) | Olası bayılmada sağlıkçı ↔ hekim video değerlendirmesi için garantili bant genişliği | `nac_client.qod_create` | ✅ 200 · 174 ms · 09.09.2026 | Profil `HS_QOD_PROFILE` (varsayılan `QOS_L`), süre 900 sn |
| Location Retrieval | `POST /location-retrieval/v0/retrieve` | **Yalnızca** eskalasyon sonrası, tek sefer: sağlıkçının koşacağı yer | `nac_client.location_retrieve` | ✅ 200 · 163 ms · 09.09.2026 | Deftere kaba özet (3 ondalık) yazılır; sürekli konum geçmişi tutulmaz |

**Network Slicing** — idea capture §6'da geçiyor ama Nokia NaC'de ayrı bir ürün olarak bu prototipte kullanılmadı:
`packages/agent/policy.py` içinde `# MOCK: Network Slicing …` yorumuyla işaretlendi; tıkanıklık yüksekken defterde
"dilim talebi" kaydı üretilir, gerçek çağrı yapılmaz. Canlıya geçişte QoD + operatör dilim API'si ile değiştirilecek.

Kullanılmayan (bilinçli): SIM/Device Swap, KYC ailesi, Number Verification — HeatShield kimlik doğrulamaz, yalnızca
maruziyet ölçer; bu API'leri çağırmak veri minimizasyonuna aykırı olur.

## MOCK işaretleri
- `apps/simulator` + `FixtureBackend` — Nokia SIMULATOR planının yerel taklidi. **Gerçek path'ler**, aynı gövde/yanıt şemaları.
- `SafeFacade` (apps/api/main.py) — Nokia hatasında `NacResult(data={}, source="error(<kind>)")`. Uydurma veri üretmez. Fixture'a düşme (`NAC_FALLBACK_TO_FIXTURE`) **varsayılan kapalı**;
  açılsa bile `agent.policy._trusted()` bu cevapların güvenlik hükmü üretmesini engeller — bir işçiyi "temizlendi" sayamaz;
  `/v1/demo/api-down` senaryosu bunu gösterir: hiçbir işçi "doğrulandı/temizlendi" sayılmaz.
- Meteoroloji beslemesi (WBGT) prototipte `POST /v1/sites/{id}/wbgt` ile dışarıdan verilir — gerçek sağlayıcı entegrasyonu Faz 2.

## Canlı test kontrol listesi
> Bu listeyi elle doldurmak gerekmiyor: kökten `_ortak/verify-live.ps1` çalıştır — sonda 22 ucu dener,
> bu tablonun test kolonunu ve aşağıdaki kutuları doldurur. Ayrıntı: [`../../_ortak/tools/README.md`](../../_ortak/tools/README.md),
> ana envanter: [`../../_ortak/docs/api-availability-master.md`](../../_ortak/docs/api-availability-master.md).

- [x] https://networkascode.nokia.io/auth/sign-up — kayıt, SIMULATOR planı  _(22.08.2026)_
- [x] Halka açık HTTPS sink (Cloudflare tüneli) ile Geofencing aboneliği → **200 · 228 ms**, kimlik
      `f402f71d-a5c1-4061-847f-d25fbcf79e2f`, doğrulandı ve silindi  _(09.09.2026)_
- [ ] **`area-entered` CloudEvent'i sink'e ulaştı mı? — HAYIR, hâlâ kanıtlanmadı.** Simüle cihaz
      fiziksel olarak hareket etmiyor → geçiş olayı doğmuyor; sink kaydında yalnızca kendi bağlantı
      testimiz var. Kanıtlanan: abonelik yaşam döngüsü. Kanıtlanmayan: geçişin kendisi
- [x] `NAC_MODE=live` ile Location Verification + Reachability + Congestion Insights + Location
      Retrieval çağrıları  _(09.09.2026, dördü de 200)_
- [x] **QoD oturumu açılıyor mu (`qosStatus`)? — EVET, açıldı.** 200 · 174 ms, `QOS_E`, 600 sn,
      `203.0.113.10`, kimlik `e287ba44-c354-4ca8-9665-f7dd108ca76f`; oturum listesinden okundu
      (200 · 127 ms), sonra silindi. **`qosStatus: REQUESTED` döndü — `AVAILABLE` değil;
      aktivasyon doğrulanmadı**  _(09.09.2026)_
- [ ] `Number Verification` çağrısı (platform sağlığı testi) — 401, üç ayaklı OIDC gerekiyor.
      **HeatShield'in API'si değil**, kullanılmıyor
- [x] Sonuçlar bu tabloya işlendi  _(tools/verify_live.py, 09.09.2026)_

## Canlı sonda — 9 Eylül 2026: altının altısı 200

Ham kayıt: [`../../_ortak/docs/live-probe/probe-20260909-124244.json`](../../_ortak/docs/live-probe/probe-20260909-124244.json)
(telefon numarası maskeli). Portföy sondası 22 uçtan 18'ini yanıtladı; bunların altısı HeatShield'in.

**Üç nüans — yumuşatılmıyor:**

1. **Sink'e Nokia'dan hiçbir bildirim gelmedi.** Simüle cihaz hareket etmiyor, geçiş olayı doğmuyor;
   abonelik de saniyeler içinde silindi. *Canlı olan:* aboneliğin gerçek platformda oluşturulması,
   doğrulanması, silinmesi ve halka açık HTTPS ucumuz. *Simüle olan:* yalnızca şebekenin sınır
   geçişini fiilen tespit etmesi. Kanıtlanamayan tek halka Nokia tarafında.
2. **QoD `REQUESTED` döndü, `AVAILABLE` değil.** Oturum açılabiliyor; garantili bant genişliğinin
   etkinleştiği doğrulanmadı.
3. **Location Verification sandbox'ta alan ayırt etmiyor.** Cihazın kendi koordinatı merkez alınıp
   yarıçap 100 m'den 5 km'ye büyütüldü, hem `maxAge=3600` hem `maxAge=60` ile: **on sekiz çağrı,
   hepsi FALSE** — cihazı kilometrelerce içine alan daireler dâhil
   ([`partial-scan-20260909-124602.json`](../../_ortak/tools/partial-scan-20260909-124602.json)).
   Taşıma katmanı, şema ve tazelik parametresi kanıtlandı; **karar mantığı kanıtlanmadı**. Bu simüle
   ortamın sınırı, kodun değil — ve sonucu şu: **PARTIAL oranı sandbox'ta ölçülemez**, hiçbir PARTIAL
   istatistiği canlı veriye dayandırılmıyor. Nokia'ya sorulacak ilk soru bu.

**Canlı platformdan öğrenilen iki sözleşme kuralı:**

- Tek abonelikte iki olay tipi **400** döndürüyor — CAMARA abonelik başına tek olay tipi istiyor.
  Kod 9 Eylül'de düzeltildi: `apps/api/main.py` işçi başına **iki ayrı abonelik** açıyor
  (`area-entered` + `area-left`).
- `maxAge` **zorunlu**: parametresiz `location_retrieve` **422** dönüyor. Tazelik ekseni bir tasarım
  tercihi değil, platformun gereği.

**Açık kalan sayısal tutarsızlıklar (canlı demodan önce env'den verilmeli):**

| Ayar | Koddaki varsayılan | Canlıda 200 dönen |
|---|---|---|
| `HS_QOD_PROFILE` | `QOS_L` | **`QOS_E`** |
| `HS_QOD_APP_SERVER` | `10.0.0.10` (RFC1918) | **`203.0.113.10`** |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | halka açık HTTPS (Cloudflare tüneli) |
| Geofence yarıçapı | tabloda "alt sınır 500 m" | sonda **300 m** ile 200 aldı — sandbox alt sınırı zorlamıyor görünüyor, varsayım canlı operatörde doğrulanmadı |

## Canlı fizibilite bulguları (22 Ağustos 2026) — 9 Eylül'de aşıldı

Nokia'nın kendi açık kaynak entegrasyon testleri incelendi (`nokia/network-as-code-ts`,
`integration-tests/`). Ayrıntı, rekabet analizi ve hukuki teyit:
[`../../_ortak/docs/faz2-fizibilite-heatshield.md`](../../_ortak/docs/faz2-fizibilite-heatshield.md).

| Uç | Nokia'nın CI'ı | Sonuç |
|---|---|---|
| Location Verification | ✅ aktif, `TRUE` dönüyor | Canlı gösterilebilir. Simüle cihaz Budapeşte'de sabit (47.48627616952785, 19.07915612501993) |
| Device Reachability | ✅ aktif | Canlı gösterilebilir |
| Congestion Insights | ✅ aktif, bildirim teslimatı doğrulanıyor | Canlı gösterilebilir; seviyeler `None/Low/Medium/High` |
| Location Retrieval | ✅ aktif | Canlı gösterilebilir |
| **Geofencing Subscriptions** | ⚠ **tüm testler `it.skip()`** | Simüle cihaz hareket etmiyor → `area-entered/left` doğmuyor. **9 Eylül'de hedefe ulaşıldı:** abonelik yaşam döngüsü canlıda kanıtlandı (200 · 228 ms), olay teslimatı yerel simülatörde kalıyor |
| **Quality on Demand** | ⚠ **20 testin 19'u kapalı** | Faz 2'de bir kez denenecek; çalışmazsa yol haritasına indirilecek. **9 Eylül'de denendi ve oturum açıldı** (200 · 174 ms) — yol haritasına inmedi; `qosStatus: REQUESTED` |

**Mimari sonuç:** Geofencing bir maliyet optimizasyonu, Location Verification ise kararın kendisi.
Geofencing olayları canlı gösterilemezse ajan aptallaşmaz, yalnızca pahalı yoldan çalışır — ve
9 Eylül ölçümü bu ayrımı daha da önemli kıldı: **canlıda kanıtlanan şey uçların kendisi, kararın
içeriği değil.** Location Verification 200 dönüyor ve iyi biçimli bir hüküm veriyor, ama sandbox'ta
hüküm talep edilen alana göre değişmiyor. Karar mantığının doğruluğu bu yüzden 98 otomatik test ve
yerel simülatörle gösteriliyor; canlı platform taşıma katmanını, şemayı ve abonelik yaşam döngüsünü
kanıtlıyor. İkisini birbirinin yerine koymuyoruz.

**WBGT beslemesi (Faz 2):** Open-Meteo (ücretsiz, anahtarsız) sıcaklık + nem + rüzgâr + kısa dalga
ışınım veriyor; WBGT bunlardan Liljegren (2008) yöntemiyle hesaplanıyor. Ürün WBGT'yi ölçmez,
tüketir: saha sensörü varsa ondan, yoksa hesaplanmış değerden — ekranda kaynağı "ölçüm" ya da
"tahmin" diye etiketlenir. Katar mevzuatı eşiği **işyeri bazında** ölçülen değere bağladığı için
ulusal meteoroloji değeri hukuken ölçüm yerine geçmez; bu sınır açıkça yazılacak.
