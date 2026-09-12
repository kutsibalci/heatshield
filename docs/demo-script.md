# HeatShield — 3 dakikalık video senaryosu

**Süre:** 3:00 · **Ekran:** `http://127.0.0.1:8000/demo` · **Dil:** İngilizce anlatım (jüri uluslararası)

**Başlatma** (PowerShell, `heatshield/` içinden — `run.ps1` yürütme politikasına takılıyor, doğrudan çağır):
```powershell
& "C:\Users\Acer\AppData\Local\Programs\Python\Python313\python.exe" -m uvicorn apps.api.main:app --port 8000
```

**Kayıttan önce:** `Sıfırla`'ya bas · tarayıcı %100 zoom · tam ekran (F11) · bildirimleri kapat.
Her senaryo kendi sahasını sıfırdan kurar; sırayı bozmadan tek çekimde gitmeyi dene, bozulursa
`Sıfırla` ve o senaryodan devam.

---

## 0 · Açılış — 0:00–0:15

Ekranda demo sayfası, henüz hiçbir butona basılmamış.

> "Qatar stops all outdoor work when the wet-bulb globe temperature passes 32.1 degrees. The law is
> strong; the enforcement is blind. Nobody can answer, in real time, which worker is still outside.
> HeatShield answers it with the mobile network — no app, no wearable, nothing the worker has to do."

## 1 · `İhlal yok → sıfır sorgu` — 0:15–0:30

Bas. İki tarama çalışır, bütçe kutuları boş, **toplam sorgu 0**.

> "Below the threshold the system has no authority to query anyone. Two sweeps, zero API calls.
> That is a legal design, not an optimisation: this cannot quietly become a surveillance tool."

## 2 · `Sıcak gün (tam döngü)` — 0:30–1:25

Bas. Altı tarama sırayla oynar. Üç yerde dur:

| Ekranda | Söyle |
|---|---|
| 10:15 — çevre kırmızı, bütçe kutuları dolar, **kesikli kırmızı kutular** | "Breach. Budget: twenty queries. We spend the budget, not the list — and the report names the workers that had to wait." |
| 10:25 — sıralama tablosu değişir | "Second sweep: the worker just verified drops back; the one unseen for twenty minutes moves up. And a worker never queried in this breach beats anyone already checked — we found a starvation bug by measurement, and this is the fix." |
| 10:37 — beyaz halkalı kırmızı nokta, QoD kartı | "One device went silent. Escalation: the medic, guaranteed bandwidth, and only now a coordinate — once, for the place the medic has to run to." |

Sağ üstteki maliyet kartını imleçle göster: toplam sorgu vs herkesi her taramada yoklamak.

## 3 · `Bayılma mı, bitmiş pil mi?` — 1:25–2:05

Bas. Dört sessiz cihaz, dört kart, `explain[]` açık.

> "This is where products like this die: treat every silent phone as an emergency and the site
> switches you off within a week."

İmleçle sırayla göster:

| Kart | Söyle |
|---|---|
| W-002 — **probable collapse 0.95** | "Alone, neighbours still reachable, congestion low, online for eight hours straight. This one goes to a human." |
| W-003 / W-004 — ağ olayı | "Two devices in the same micro-zone went dark together and the cell reports high congestion. Network event — nobody is woken." |
| W-005 — bitmiş pil | "Same silence, same hour, every day. Battery." |
| W-006 — sahadan ayrıldı | "Last seen at the exit gate, heading out. Logged, nothing more." |

> "Four silent devices, one escalation. No extra sensor — evidence we already had."

## 4 · `Planner guard` — 2:05–2:25

Bas. İki satır görünür: kabul edilen yeniden sıralama, sonra **reddedilen** öneri.

> "A language model sits under the rules. It may re-order the plan the rules produced — nothing else.
> Here it re-ranks validly, then tries to widen the plan, and the guard refuses the whole proposal.
> Both outcomes go to the ledger. A guard you never see refuse is not a guard."

## 5 · `Nokia API çöktü` — 2:25–2:40

Bas. Devre kesici açık, çağrılar `error(server)`, **kimse "doğrulandı" değil**, defter "unknown".

> "When the operator API fails, nobody is marked safe. The ledger says unknown, and the workers stay
> in the queue. In a safety system, optimism cannot be the default."

## 6 · Kapanış — kanıt defteri — 2:40–3:00

Sol alttaki defteri yavaşça kaydır.

> "The product is this ledger: which worker, which minute, what temperature, what evidence, what
> decision. Six CAMARA APIs, all six verified live on Nokia Network as Code. A hundred and
> twenty-six tests. And not one worker pressed a single button."

Son kare: depo adresi `github.com/kutsibalci/heatshield` ekranda 2 sn.

---

## Kesilenler ve neden

- **Üç yargı alanı** senaryosu çıkarıldı (30 sn) — deck'te ve Idea Capture'da anlatılıyor, videoda
  vakit yok. İstersen kapanışta tek cümle: "Saudi Arabia and the UAE are a configuration entry."
- Açılış 25 → 15 sn. Jüri problemi biliyor; çözümü görmek istiyor.

## Yedek plan

- Port doluysa: `Get-CimInstance Win32_Process | ? CommandLine -match uvicorn | % { Stop-Process -Id $_.ProcessId -Force }`
- İnternet yoksa sorun yok: varsayılan `NAC_MODE=fixture`, her şey aynı çalışır.
- Ekran karışırsa `Sıfırla` — her senaryo kendi sahasını kurar (idempotent).
- Ses kötü çıkarsa: sessiz çek, anlatımı altyazı olarak ekle. Jüri metni okur, mikrofon kalitesini değil.
