# Geliştirici Rehberi — ortak şablon (tüm MENA Ignite projeleri)

Her proje klasörü bu şablondan türetildi ve **kendi başına çalışır** (kopyalanıp ayrı repo olarak verilebilir).

## Klasör yapısı
```
<proje>/
  apps/api/main.py        FastAPI — karar motoru + uçlar + /demo statik arayüz (common.py: health, debug, static)
  apps/web/index.html     Demo arayüzü (vanilla HTML/JS; base.css + base.js ortak) — build adımı yok, tek tuşla açılır
  apps/simulator/main.py  Yerel Nokia NaC mock'u — GERÇEK path'ler, FixtureBackend'den veri (değiştirmeyin)
  packages/nac_client/    Nokia sarmalayıcı — auth, retry, timeout, circuit breaker, maskeleme, 3 mod (değiştirmeyin; ekleme gerekirse client.py'ye metot ekleyin)
  packages/rules/         Karar kuralları — SAF fonksiyon, yan etkisiz; girdi: sinyal dict'i, çıktı: Decision(explain[])
  fixtures/profiles.json  Demo/test profilleri (telefon → sinyaller). Şema: packages/nac_client/fixtures.py docstring
  tests/                  pytest — kurallar fixture'dan beslenir; API uçları TestClient ile
  docs/api-availability.md  Kullanılan API'lerin path doğrulaması + canlı test durumu
  run.ps1 / run.sh        Tek tuşla demo (simülatör + API)
```

## Zorunlu tasarım kuralları (spec 0.4)
1. Nokia çağrıları **yalnızca** `nac_client` üzerinden. `apps/api` içinde `httpx` ile Nokia'ya gitmek yasak.
2. Her karar `explain[]` üretir (`rules.Explain`): signal, value, weight, note, source, triggered. UI'da gösterilir.
3. Kurallar saf fonksiyon: `decide(signals: dict, config: Config) -> Decision`. Test fixture'dan beslenir.
4. Eşikler config'te (`rules/config.py` dataclass + env override), hard-code yok.
5. Timeout + retry + circuit breaker `nac_client`'ta hazır; API katmanı `NacFacade.call()` ile çağırır. **Fixture'a düşme varsayılan KAPALI** (`NAC_FALLBACK_TO_FIXTURE=1` ile açılır): çöken bir API uydurma veriyle "güvende" hükmü üretemez, `SafeFacade` `source="error(<kind>)"` döndürür. Açıldığında bile `agent.policy._trusted()` bu cevapların bir işçiyi "temizlendi" saymasını engeller.
6. Loglarda ve API yanıtlarında telefon **maskeli** (`mask_phone`), saklanan alan `hash_phone`.
7. Demo senaryoları **tek tuşla** tetiklenir (`POST /v1/demo/<senaryo>` veya UI butonu) — jüri önünde elle veri girilmez.
8. Spec'te olmayan endpoint varsayılmaz; taklit gerekiyorsa kodda `# MOCK:` yorumu.

## Çalıştırma
```
python -m pip install -r requirements.txt
python -m pytest -q                       # testler
.\run.ps1 -Mode simulator                 # http://127.0.0.1:8000/demo  (simülatör 8081)
.\run.ps1 -Mode fixture                   # HTTP yok, tamamen bellek içi
```
Windows'ta bu makinede Python: `C:\Users\Acer\AppData\Local\Programs\Python\Python313\python.exe`

## nac_client hızlı kullanım
```python
from nac_client import NacClient, NacConfig, FixtureBackend
c = NacClient(NacConfig(mode="fixture"), fixtures=FixtureBackend.from_json("fixtures/profiles.json"))
r = c.number_recycling("+905551110001", "2026-01-01")   # r.data == {"phoneNumberRecycled": bool}, r.source, r.latency_ms
c.unconditional_call_forwarding(p) / c.call_forwardings(p) / c.kyc_tenure(p, "YYYY-MM-DD") / c.kyc_age(p, 18) / c.kyc_match(p, name=...)
c.number_verify(p) / c.sim_swap_check(p, 72) / c.sim_swap_date(p) / c.device_swap_check(p, 24) / c.device_swap_date(p)
c.reachability(p) / c.roaming(p) / c.location_retrieve(p) / c.location_verify(p, lat, lng, radius_m) / c.congestion_query(p)
c.geofence_subscribe(p, lat, lng, r, sink, sink_token=..) / c.geofence_delete(id) / c.reachability_subscribe(p, sink) / c.roaming_subscribe(p, sink)
c.qod_create(p, "10.0.0.1", "QOS_L") / c.qod_delete(id) / c.consent(p, ["scope"], "purpose")
```
Fixture profil alanları için `packages/nac_client/fixtures.py` başındaki docstring'e bak (recycled_date, tenure_since, age_check, sim_swap_at, call_forwarding, reachable, roaming, location{lat,lng,radius}, congestion, latency_ms, fail...).

Simülatör webhook tetikleme (demo): `POST http://127.0.0.1:8081/_sim/emit {"phone": "+974...", "type": "org.camaraproject.geofencing-subscriptions.v0.area-entered"}` → aboneliğin `sink` adresine CloudEvent gönderir.
