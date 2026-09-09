# HeatShield — Demo senaryosu (jüri sunumu)

**Süre:** ~4,5 dakika · **Ekran:** `http://127.0.0.1:8000/demo` · **Hazırlık:** `.\run.ps1 -Mode simulator`

---

## 0. Açılış (25 sn)

> "Katar'da yasa net: WBGT 32.1 dereceyi geçtiğinde açık havada çalışmak yasak — yıl boyu, her saat.
> Yasa güçlü, denetim kör. Bugün hiç kimse gerçek zamanlı olarak şu soruyu cevaplayamıyor:
> **eşik aşılmışken hangi işçi hâlâ dışarıda?**"

> "HeatShield mobil şebekeyi sensör olarak kullanır. İşçi bir uygulama açmaz, bir bileklik takmaz, bir tuşa basmaz."

---

## 1. İhlal yokken — `İhlal yok → sıfır sorgu` (30 sn)

WBGT 30.2 °C. İki tarama çalışır. **Bütçe kutuları boş, toplam sorgu: 0.**

> "Eşik altında sistemin sorgu yetkisi yok. Bu bir optimizasyon değil, hukuki tasarım:
> HeatShield sessizce bir üretkenlik gözetim aracına dönüşemez. Sabah girişleri şebekenin ittiği
> ücretsiz geofence olaylarıyla geldi — tek kuruş harcanmadı."

---

## 2. Sıcak gün — `Sıcak gün (tam döngü)` (100 sn)

Altı tarama sırayla oynar. Anlatım noktaları:

| Tarama | Ekranda | Söylenecek |
|---|---|---|
| 09:00 | yeşil WBGT, 0 sorgu | "Sessiz izleme." |
| 10:15 | kırmızı çevre, bütçe kutuları dolar, **kesikli kırmızı kutular** | "Yaz yasağı saati başladı — ihlal. Bütçe 20 sorgu. Listeyi değil bütçeyi bitiriyoruz: yedi işçi bu taramada sıraya kaldı ve bunu **raporda söylüyoruz**." |
| 10:25 | sıralama tablosu değişir | "İkinci taramada sıralama değişti: az önce doğrulanan işçi geriye düştü, **yirmi dakikadır görülmeyen** öne geçti. Bayatlık skorun çarpanı." |
| 10:35 | WBGT 35.4, tarama sıklığı 2 dk | "Şiddet arttı, kadans sıkılaştı. Ve bir cihaz sustu." |
| 10:37 | kırmızı nokta beyaz halkalı, QoD kartı | "Sağlıkçıya gitti, garantili bant genişliği açıldı ve **ancak şimdi** koordinat istendi — sağlıkçının koşacağı yer için, bir kez." |
| 19:00 | yeşil, sorgu yok | "İhlal bitti, sorgu yetkisi düştü." |

Sağ üstteki maliyet kartını göster: **toplam sorgu vs herkesi her taramada yoklasaydık.**

---

## 3. Ana gösteri — `Bayılma mı, bitmiş pil mi?` (70 sn)

> "Bu ürünün en kolay öldüğü yer burası. Erişilemeyen her cihazı acil vaka sayan bir sistem
> bir hafta içinde kapatılır."

Dört sessiz cihaz, dört farklı karar — kartlarda `explain[]` açık:

| İşçi | Kanıt | Karar |
|---|---|---|
| W-002 | tek başına sustu, komşuları açık, tıkanıklık düşük, 8 saattir kesintisiz açıktı | **OLASI BAYILMA (0.95)** → sağlıkçı + QoD + koordinat |
| W-003, W-004 | aynı mikro-bölgede birlikte sustu + tıkanıklık `High` | ağ olayı → **insan uyandırılmaz**, dilim talebi |
| W-005 | her gün aynı saatte susan cihaz | bitmiş pil → süpervizöre bilgi |
| W-006 | son doğrulama çıkış kapısında (PARTIAL), yörünge dışarı | sahadan ayrıldı → yalnızca kayıt |

> "Dört sessiz cihazdan **yalnızca biri** insana gitti. Ayrımı yapan şey ekstra bir sensör değil,
> zaten elimizde olan kanıt: küme testi, cihaz geçmişi, yörünge."

---

## 4. Bölgesel ölçek — `Üç yargı alanı` (30 sn)

Aynı dakika, aynı 31.5 °C:

- **Katar** (11:45 yerel) → çalışma **yasak** (10:00–15:30)
- **Suudi Arabistan** (11:45 yerel) → serbest (yasak 12:00'de başlar)
- **BAE** (12:45 yerel) → **yasak** (12:30–15:00)

> "Yargı alanı bizde bir yeniden yazım değil, bir yapılandırma kaydı. Riyad'daki bir jüri üyesi için
> kendi pazarı üç satırlık config."

---

## 5. Nokia API çöktü — `Nokia API çöktü` (25 sn)

> "Sorgu düşerse ne olur?"

Ekranda: devre kesici açık, tüm çağrılar `error(server)`, **hiçbir işçi 'doğrulandı' ya da 'temizlendi' değil**,
defter "bilinmiyor" yazıyor, işçiler kuyrukta kaldı.

> "Hata anında uydurma veri üretmiyoruz ve — daha önemlisi — kimseyi sessizce güvende işaretlemiyoruz.
> Bir güvenlik sisteminde iyimserlik varsayılan olamaz."

---

## 6. Kapanış — kanıt defteri (25 sn)

Sol alttaki kanıt defterini kaydır.

> "Ürünün kendisi bu defter: hangi işçi, hangi dakika, hangi WBGT değerinde, hangi kanıtla, hangi karar.
> Müteahhitin bugün ispat edemediği hukuki yükümlülüğü ispat eden şey bu.
> Ve tek bir işçi tek bir tuşa basmadı."

---

## Yedek plan

- İnternet yoksa: `NAC_MODE=fixture` (varsayılan) — her şey aynı çalışır.
- Simülatör açılmazsa: `.\run.ps1 -Mode fixture`.
- Ekran karışırsa: `Sıfırla` → her senaryo kendi sahasını sıfırdan kurar (idempotent).
