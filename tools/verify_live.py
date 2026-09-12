"""verify_live.py — Nokia NaC canlı erişilebilirlik sondası (spec kuralı 0.3).

Portföyün kullandığı BÜTÜN CAMARA uçlarını tek tek dener, sonucu kaydeder ve
her projenin `docs/api-availability.md` tablosundaki "SIMULATOR planında test"
kolonunu otomatik doldurur. Amaç: elle tablo doldurma işini sıfıra indirmek.

Kullanım
--------
    # 1) Önce yerel simülatöre karşı prova (anahtar gerekmez, hiçbir şeyi bozmaz)
    python tools/verify_live.py --mode simulator --base http://127.0.0.1:8081

    # 2) Nokia hesabı açıldıktan sonra gerçek çağrı + tabloları güncelle
    python tools/verify_live.py --mode live --patch

    # 3) Abonelik / QoD gibi KAYIT OLUŞTURAN uçları da dene (oluşturur ve siler)
    python tools/verify_live.py --mode live --include-write --sink https://<tunel>/webhooks/probe --patch

Ortam değişkenleri (.env veya kabuk):
    NAC_RAPIDAPI_KEY        RapidAPI anahtarı (zorunlu, live)
    NAC_OAUTH_TOKEN         passthrough/ uçları için Bearer token
    NAC_OAUTH_TOKEN_URL     verilirse client-credentials ile token alınır
    NAC_OAUTH_CLIENT_ID     ^
    NAC_OAUTH_CLIENT_SECRET ^
    NAC_PROBE_PHONE         sonda numarası (varsayılan +905551234567)
    PUBLIC_BASE_URL         abonelik sink'i için public HTTPS taban adresi

Kural: ham numara hiçbir çıktıda görünmez (`_mask_deep`). Uydurma sonuç yazılmaz —
denenmeyen uç tabloda "☐ yapılmadı" kalır.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

SHARED = Path(__file__).resolve().parent.parent  # repository root (heatshield/)
ROOT = SHARED
sys.path.insert(0, str(SHARED))                  # packages/ lives at the repository root

from packages.nac_client import NacClient, NacConfig, NacError, mask_phone  # noqa: E402
from packages.nac_client.client import P  # doğrulanmış path sözlüğü        # noqa: E402

# Yamalanacak projeler. `_template` BİLEREK dışarıda: tablosu yeni projelere kopyalanan
# boş iskelet ("proje agent'ı doldurur") — sonda sonucuyla kirletilmez.
PROJECTS = ["heatshield"]

# ---------------------------------------------------------------- sonda tanımları


@dataclass
class Probe:
    key: str
    label: str  # tabloda/raporda görünen ad
    paths: list[str]  # bu satırı tabloda bulmak için kullanılacak path parçaları
    run: Callable[[NacClient, "ProbeCtx"], Any]
    write: bool = False  # kayıt oluşturur mu (abonelik/QoD) → --include-write ister
    note: str = ""  # canlıda bilinen kısıt


@dataclass
class ProbeCtx:
    phone: str
    sink: str
    lat: float = 25.2854
    lng: float = 51.5310
    radius_m: float = 300.0
    created: list[tuple[str, str]] = field(default_factory=list)  # (tip, id) → temizlik


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).date().isoformat()


def _cleanup_sub(ctx: ProbeCtx, kind: str, res: Any) -> Any:
    """Abonelik/oturum yanıtından id'yi al, temizlik listesine ekle."""
    sid = (res.data or {}).get("subscriptionId") or (res.data or {}).get("sessionId") or (res.data or {}).get("id")
    if sid:
        ctx.created.append((kind, str(sid)))
    return res


PROBES: list[Probe] = [
    # --- passthrough (Bearer + RapidAPI anahtarı) -------------------------------
    Probe("number_recycling", "Number Recycling", [P["number_recycling"]],
          lambda c, x: c.number_recycling(x.phone, _days_ago(30))),
    Probe("call_forwardings", "Call Forwarding Signal — tümü", [P["call_forwardings"]],
          lambda c, x: c.call_forwardings(x.phone)),
    Probe("unconditional_cf", "Call Forwarding Signal — koşulsuz", [P["unconditional_cf"]],
          lambda c, x: c.unconditional_call_forwarding(x.phone)),
    Probe("kyc_tenure", "KYC Tenure", [P["kyc_tenure"]],
          lambda c, x: c.kyc_tenure(x.phone, _days_ago(90))),
    Probe("kyc_age", "KYC Age Verification", [P["kyc_age"]],
          lambda c, x: c.kyc_age(x.phone, 18)),
    Probe("kyc_match", "KYC Match", [P["kyc_match"]],
          lambda c, x: c.kyc_match(x.phone, name="Test Kullanici")),
    Probe("number_verify", "Number Verification", [P["number_verify"]],
          lambda c, x: c.number_verify(x.phone),
          note="Canlıda 3-legged OIDC + mobil veri ister; laboratuvar Wi‑Fi'ından 401/403 beklenir."),
    Probe("sim_swap_check", "SIM Swap check", [P["sim_swap_check"]],
          lambda c, x: c.sim_swap_check(x.phone, 72)),
    Probe("sim_swap_date", "SIM Swap date", [P["sim_swap_date"]],
          lambda c, x: c.sim_swap_date(x.phone)),
    Probe("device_swap_check", "Device Swap check", [P["device_swap_check"]],
          lambda c, x: c.device_swap_check(x.phone, 24)),
    Probe("device_swap_date", "Device Swap date", [P["device_swap_date"]],
          lambda c, x: c.device_swap_date(x.phone)),
    Probe("consent", "Consent Info", [P["consent"]],
          lambda c, x: c.consent(x.phone, ["device-swap:read", "sim-swap:read"], "identity-assurance")),
    # --- device-status / konum (yalnız RapidAPI anahtarı) -----------------------
    Probe("reachability", "Device Reachability Status", [P["reachability"]],
          lambda c, x: c.reachability(x.phone)),
    Probe("roaming", "Device Roaming Status", [P["roaming"]],
          lambda c, x: c.roaming(x.phone)),
    Probe("location_retrieve", "Location Retrieval", [P["location_retrieve"]],
          lambda c, x: c.location_retrieve(x.phone, 3600)),
    Probe("location_verify", "Location Verification", [P["location_verify"]],
          lambda c, x: c.location_verify(x.phone, x.lat, x.lng, x.radius_m, 3600)),
    Probe("congestion_query", "Congestion Insights", [P["congestion_query"]],
          lambda c, x: c.congestion_query(x.phone)),
    # --- abonelikler ve QoD: KAYIT OLUŞTURUR ------------------------------------
    Probe("geofencing_subs", "Geofencing Subscriptions", [P["geofencing_subs"]],
          lambda c, x: _cleanup_sub(x, "geofencing", c.geofence_subscribe(x.phone, x.lat, x.lng, x.radius_m, x.sink)),
          write=True, note="Canlıda sink public HTTPS olmalı (ngrok/cloudflare tüneli)."),
    Probe("reachability_subs", "Device Reachability Status Subscriptions", [P["reachability_subs"]],
          lambda c, x: _cleanup_sub(x, "reachability", c.reachability_subscribe(x.phone, x.sink)),
          write=True, note="Canlıda sink public HTTPS olmalı."),
    Probe("roaming_subs", "Device Roaming Status Subscriptions", [P["roaming_subs"]],
          lambda c, x: _cleanup_sub(x, "roaming", c.roaming_subscribe(x.phone, x.sink)),
          write=True, note="Canlıda sink public HTTPS olmalı."),
    Probe("qod_sessions", "Quality on Demand", [P["qod_sessions"]],
          lambda c, x: _cleanup_sub(x, "qod", c.qod_create(x.phone, "203.0.113.10", "QOS_E", 600)),
          write=True, note="qosProfile adı operatöre göre değişir; 400 alırsan portaldaki profil listesine bak."),
    Probe("qod_retrieve", "Quality on Demand — oturum listesi", [P["qod_retrieve"]],
          lambda c, x: c.qod_list(x.phone)),
]

# ---------------------------------------------------------------- yardımcılar

_PHONE_RE = re.compile(r"\+[0-9]{8,15}")


def _shape(data: Any) -> list[str]:
    """Yanıtın biçimi — jüri/tablo için "hangi alanlar döndü" bilgisi.
    CAMARA bazı uçlarda dizi döner (ör. call-forwardings), o zaman alan adı yoktur."""
    if isinstance(data, dict):
        return sorted(data.keys())
    if isinstance(data, list):
        inner = sorted({k for it in data if isinstance(it, dict) for k in it})
        return [f"dizi[{len(data)}]"] + inner
    if data is None or data == {}:
        return ["(boş gövde)"]
    return [type(data).__name__]


def _mask_deep(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _mask_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_deep(v) for v in value]
    if isinstance(value, str):
        return _PHONE_RE.sub(lambda m: mask_phone(m.group(0)), value)
    return value


def load_dotenv(path: Path) -> int:
    """Basit .env okuyucu (python-dotenv bağımlılığı eklemeden)."""
    if not path.exists():
        return 0
    n = 0
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v
            n += 1
    return n


def fetch_oauth_token() -> str | None:
    """NAC_OAUTH_TOKEN_URL verilmişse client-credentials ile token alır."""
    url = os.environ.get("NAC_OAUTH_TOKEN_URL")
    cid = os.environ.get("NAC_OAUTH_CLIENT_ID")
    secret = os.environ.get("NAC_OAUTH_CLIENT_SECRET")
    if not (url and cid and secret):
        return None
    import httpx

    scope = os.environ.get("NAC_OAUTH_SCOPE")
    data = {"grant_type": "client_credentials", **({"scope": scope} if scope else {})}
    r = httpx.post(url, data=data, auth=(cid, secret), timeout=15.0)
    r.raise_for_status()
    tok = r.json().get("access_token")
    if not tok:
        raise RuntimeError(f"token yanıtında access_token yok: {list(r.json())}")
    return tok


# ---------------------------------------------------------------- sonda koşucusu

VERDICT_OK = "✅"
VERDICT_REACHED = "◐"

# CAMARA hata gövdeleri kodludur (ör. LOCATION_RETRIEVAL.UNABLE_TO_LOCATE). Böyle bir kod
# gördüysek uç GERÇEKTEN VAR ve CAMARA konuşuyor — sadece test verisi olumlu sonuç vermedi.
# ALAN.KOD — en bilgilendirici. Her iki yanda en az 2 karakter: `QOD.SESSION_NOT_FOUND` gibi kısa
# alan adlarını da yakalar, düz metindeki "A.B" baş harflerini yakalamaz.
_DOTTED_RE = re.compile(r"\b([A-Z][A-Z0-9_]+\.[A-Z][A-Z0-9_]+)\b")
_BARE_RE = re.compile(r"\b([A-Z][A-Z0-9]{2,}(?:_[A-Z0-9]+)+)\b")  # SADECE_KOD


def camara_code(rec: dict) -> str | None:
    """Hata gövdesinden CAMARA hata kodunu çıkarır. `ALAN.KOD` biçimi `KOD`a tercih edilir."""
    if rec.get("ok") or rec.get("skipped"):
        return None
    raw = str(rec.get("message") or "")
    texts = [raw]
    # gövde JSON ise iç `code`/`message` alanlarını da tara
    i, j = raw.find("{"), raw.rfind("}")
    if 0 <= i < j:
        try:
            obj = json.loads(raw[i:j + 1])
            if isinstance(obj, dict):
                texts = [str(obj.get("message") or ""), str(obj.get("code") or ""), raw]
        except json.JSONDecodeError:
            pass
    for rx in (_DOTTED_RE, _BARE_RE):
        for t in texts:
            m = rx.search(t)
            if m:
                return m.group(1)
    return None


def endpoint_reached(rec: dict) -> bool:
    """Uç mevcut mu? 200 ya da CAMARA kodlu iş kuralı hatası → evet.
    401/403 (yetki), timeout/network/devre kesici → hayır, bilgi yok."""
    if rec.get("ok"):
        return True
    if rec.get("kind") in ("auth", "timeout", "network", "circuit_open", "probe_error"):
        return False
    return camara_code(rec) is not None


def verdict_cell(rec: dict, stamp: str) -> str:
    """api-availability.md son kolonuna yazılacak tek satırlık hüküm."""
    if rec["ok"]:
        return f"{VERDICT_OK} 200 · {rec['latency_ms']} ms · {stamp}"
    kind, status = rec.get("kind"), rec.get("status")
    code = camara_code(rec)
    if code:
        # uç ayakta, iş kuralı hatası döndü — tabloda "yok" gibi görünmesin
        return f"{VERDICT_REACHED} uç var · {status or ''} `{code}` · {stamp}".replace("·  ·", "·")
    label = {
        "auth": "plan/yetki kapsamı dışı",
        "not_found": "path bulunamadı",
        "bad_request": "gövde reddedildi",
        "rate_limit": "kota doldu",
        "server": "sunucu hatası",
        "timeout": "zaman aşımı",
        "network": "ağ hatası",
        "circuit_open": "devre kesici açık",
    }.get(kind or "", kind or "hata")
    st = f" {status}" if status else ""
    return f"⚠{st} {label} · {stamp}"


def run_probes(client: NacClient, ctx: ProbeCtx, include_write: bool, only: set[str] | None) -> list[dict]:
    out: list[dict] = []
    for pr in PROBES:
        if only and pr.key not in only:
            continue
        if pr.write and not include_write:
            out.append({"key": pr.key, "label": pr.label, "paths": pr.paths, "skipped": True,
                        "reason": "kayıt oluşturan uç — --include-write ile denenir", "note": pr.note})
            print(f"  ⏭  {pr.label:<44} atlandı (--include-write gerekli)")
            continue
        try:
            res = pr.run(client, ctx)
            rec = {"key": pr.key, "label": pr.label, "paths": pr.paths, "ok": True, "status": 200,
                   "latency_ms": res.latency_ms, "source": res.source, "correlator": res.correlator,
                   "request": _mask_deep(res.request), "response_keys": _shape(res.data),
                   "response": _mask_deep(res.data), "note": pr.note}
            print(f"  ✅ {pr.label:<44} 200  {res.latency_ms:>5} ms  {res.source}")
        except NacError as e:
            rec = {"key": pr.key, "label": pr.label, "paths": pr.paths, "ok": False, **e.to_dict(), "note": pr.note}
            code = camara_code(rec)
            rec["camara_code"] = code
            rec["endpoint_reached"] = endpoint_reached(rec)
            if code:
                print(f"  ◐  {pr.label:<44} uç var, iş kuralı hatası: {e.status} {code}")
            else:
                print(f"  ⚠  {pr.label:<44} {e.kind}{f' {e.status}' if e.status else ''}")
        except Exception as e:  # sonda çökmesin
            rec = {"key": pr.key, "label": pr.label, "paths": pr.paths, "ok": False, "kind": "probe_error",
                   "message": f"{type(e).__name__}: {e}", "note": pr.note}
            print(f"  ✖  {pr.label:<44} sonda hatası: {type(e).__name__}")
        out.append(rec)
    return out


def cleanup(client: NacClient, ctx: ProbeCtx) -> list[dict]:
    """Sonda sırasında oluşturulan abonelik/oturumları sil — arkada çöp bırakma."""
    done = []
    for kind, sid in ctx.created:
        try:
            if kind == "geofencing":
                client.geofence_delete(sid)
            elif kind == "reachability":
                client.reachability_unsubscribe(sid)
            elif kind == "roaming":
                client.roaming_unsubscribe(sid)
            elif kind == "qod":
                client.qod_delete(sid)
            done.append({"kind": kind, "id": sid, "deleted": True})
            print(f"  🧹 {kind} {sid} silindi")
        except NacError as e:
            done.append({"kind": kind, "id": sid, "deleted": False, "error": e.kind})
            print(f"  ⚠  {kind} {sid} silinemedi: {e.kind} — portaldan elle kontrol et")
    return done


# ---------------------------------------------------------------- tablo yamacı

TEST_COL_HINT = "SIMULATOR planında test"
PLACEHOLDER_RE = re.compile(r"^\s*(☐|\[ \]|—|-)?\s*(yapılmadı|yapilmadi)?\s*$", re.IGNORECASE)


def _split_row(line: str) -> list[str]:
    """Markdown tablo satırını hücrelere böler (baş/son boru hariç)."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return s.split("|")


def _join_row(cells: list[str]) -> str:
    return "| " + " | ".join(c.strip() for c in cells) + " |"


def patch_availability(project_dir: Path, results: list[dict], stamp: str, dry: bool) -> dict:
    """`docs/api-availability.md` içindeki test kolonunu path eşleşmesine göre doldurur."""
    doc = project_dir / "docs" / "api-availability.md"
    if not doc.exists():
        return {"project": project_dir.name, "error": "api-availability.md yok"}

    lines = doc.read_text(encoding="utf-8").splitlines()
    by_path: list[tuple[str, dict]] = []
    for rec in results:
        for p in rec["paths"]:
            by_path.append((p.strip("/"), rec))
    # uzun path'ler önce → "sim-swap/v0/check" ile "sim-swap/v0" karışmasın
    by_path.sort(key=lambda t: -len(t[0]))

    col_idx: int | None = None
    touched: list[str] = []
    unmatched: list[str] = []
    skipped_rows: list[str] = []

    for i, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        cells = _split_row(line)
        if col_idx is None:
            for j, c in enumerate(cells):
                if TEST_COL_HINT in c:
                    col_idx = j
                    break
            continue  # başlık satırını yamalamayız
        if set(line.replace("|", "").replace("-", "").replace(":", "").strip()) == set():
            continue  # ayırıcı satır
        if col_idx >= len(cells):
            continue
        row_text = line
        hit = next((rec for p, rec in by_path if p in row_text), None)
        if hit is None:
            first = cells[0].strip().strip("*")[:40]
            if first:
                unmatched.append(first)
            continue
        if hit.get("skipped"):
            # denenmedi → tabloda "☐ yapılmadı" olarak KALSIN; uydurma hüküm yazmıyoruz
            skipped_rows.append(hit["label"])
            continue
        cur = cells[col_idx].strip()
        if cur.startswith((VERDICT_OK, VERDICT_REACHED, "⚠")):
            pass  # önceki sonda sonucu — üzerine yaz (en güncel kazanır)
        elif not PLACEHOLDER_RE.match(cur):
            # elle yazılmış not var: hükmü ekle, notu koru
            cells[col_idx] = f"{verdict_cell(hit, stamp)} — {cur}"
            lines[i] = _join_row(cells)
            touched.append(hit["label"])
            continue
        cells[col_idx] = verdict_cell(hit, stamp)
        lines[i] = _join_row(cells)
        touched.append(hit["label"])

    # "Canlı test kontrol listesi" — doğrulanan maddeleri işaretle
    any_ok = any(r.get("ok") for r in results)
    nv_ok = any(r.get("ok") for r in results if r["key"] == "number_verify")
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("- [ ]"):
            continue
        low = line.lower()
        if any_ok and ("sign-up" in low or "kayıt" in low or "kayit" in low):
            lines[i] = line.replace("- [ ]", "- [x]", 1) + f"  _({stamp})_"
        elif nv_ok and "number verification" in low:
            lines[i] = line.replace("- [ ]", "- [x]", 1) + f"  _({stamp})_"
        elif any_ok and ("bu tabloya işlendi" in low or "tabloya işlendi" in low or "sonuçlar bu tabloya" in low):
            lines[i] = line.replace("- [ ]", "- [x]", 1) + f"  _(tools/verify_live.py, {stamp})_"

    new = "\n".join(lines) + "\n"
    if not dry:
        doc.write_text(new, encoding="utf-8")
    return {"project": project_dir.name, "col": col_idx, "patched": touched,
            "unmatched": unmatched, "skipped_rows": skipped_rows, "dry": dry}


# ---------------------------------------------------------------- ana envanter tablosu

def which_projects(paths: list[str]) -> list[str]:
    """Bu ucu hangi projelerin api-availability.md'si anıyor — türetilmiş, elle liste tutmuyoruz."""
    hits = []
    for name in PROJECTS:
        doc = ROOT / name / "docs" / "api-availability.md"
        if not doc.exists():
            continue
        text = doc.read_text(encoding="utf-8")
        if any(p.strip("/") in text for p in paths):
            hits.append(name)
    return hits


def write_master_table(results: list[dict], meta: dict, stamp: str, dry: bool) -> Path:
    """Portföydeki BÜTÜN uçların tek tablosu — kök docs/ altında."""
    doc = SHARED / "docs" / "api-availability-master.md"
    rows = []
    for r in results:
        if r.get("skipped"):
            cell = "☐ atlandı (`--include-write`)"
        else:
            cell = verdict_cell(r, stamp)
        users = which_projects(r["paths"]) or ["—"]
        pt = "Bearer + key" if r["paths"][0].startswith("passthrough/") else "yalnız key"
        rows.append(f"| **{r['label']}** | `{r['paths'][0]}` | {pt} | {', '.join(users)} | {cell} |")

    ok = sum(1 for r in results if r.get("ok"))
    reached = sum(1 for r in results if not r.get("ok") and not r.get("skipped") and endpoint_reached(r))
    tried = sum(1 for r in results if not r.get("skipped"))
    body = (
        "# API Erişilebilirlik — Ana Envanter\n\n"
        "> Portföyün kullandığı **bütün** CAMARA uçları tek tabloda. Bu dosyayı elle düzenlemeyin:\n"
        "> `python tools/verify_live.py --mode live --patch` her çalıştığında yeniden üretilir.\n"
        f"> Son sonda: **{meta['started'][:16]}** · mod `{meta['mode']}` · taban `{meta['base_url']}`"
        f" · numara `{meta['phone_masked']}`\n"
        f"> Sonuç: **{ok} çalıştı**, {reached} uç var (iş kuralı hatası), {tried - ok - reached} erişilemedi"
        f" — denenen {tried}/{len(results)}\n\n"
        "Path kaynağı: `network-as-code` SDK v10.0.0 → [`nac-api-reference.md`](nac-api-reference.md).\n"
        "Hüküm işaretleri: ✅ 200 döndü · ◐ uç mevcut, CAMARA iş kuralı hatası (401/403 değil) · ⚠ erişilemedi · ☐ denenmedi.\n\n"
        "| API | Path | Auth | Kullanan projeler | Son sonda |\n|---|---|---|---|---|\n"
        + "\n".join(rows) + "\n\n"
        "## Notlar\n"
        + "\n".join(f"- **{r['label']}**: {r['note']}" for r in results if r.get("note")) + "\n\n"
        "Ayrıntılı çıktı (istek/yanıt gövdeleri, maskeli): [`live-probe/`](live-probe/).\n"
    )
    if not dry:
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text(body, encoding="utf-8")
    return doc


# ---------------------------------------------------------------- rapor

def write_report(results: list[dict], meta: dict, cleaned: list[dict]) -> tuple[Path, Path]:
    out_dir = SHARED / "evidence"
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = meta["started"].replace(":", "").replace("-", "").replace("T", "-")[:15]
    j = out_dir / f"probe-{slug}.json"
    j.write_text(json.dumps({"meta": meta, "results": results, "cleanup": cleaned}, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = [r for r in results if r.get("ok")]
    reached = [r for r in results if not r.get("ok") and not r.get("skipped") and endpoint_reached(r)]
    bad = [r for r in results if not r.get("ok") and not r.get("skipped") and not endpoint_reached(r)]
    skipped = [r for r in results if r.get("skipped")]
    rows = []
    for r in results:
        if r.get("skipped"):
            state = "⏭ atlandı"
        elif r.get("ok"):
            state = f"✅ 200 · {r['latency_ms']} ms"
        elif endpoint_reached(r):
            state = f"◐ uç var · {r.get('status','')} `{camara_code(r)}`"
        else:
            state = f"⚠ {r.get('kind')}" + (f" {r['status']}" if r.get("status") else "")
        keys = ", ".join(r.get("response_keys") or []) if r.get("ok") else (r.get("message", "") or "")[:80]
        rows.append(f"| {r['label']} | `{r['paths'][0]}` | {state} | {keys} | {r.get('note','')} |")

    md = out_dir / f"probe-{slug}.md"
    md.write_text(
        f"# NaC canlı sonda raporu — {meta['started'][:16]}\n\n"
        f"- Mod: **{meta['mode']}** · taban: `{meta['base_url']}`\n"
        f"- Sonda numarası: `{meta['phone_masked']}`\n"
        f"- RapidAPI anahtarı: {'var' if meta['has_key'] else 'YOK'} · Bearer token: {'var' if meta['has_token'] else 'YOK'}\n"
        f"- Sonuç: **{len(ok)} çalıştı**, {len(reached)} uç var (iş kuralı hatası), {len(bad)} erişilemedi, {len(skipped)} atlandı"
        f" — toplam {len(results)}\n"
        f"- **Uç mevcut sayılan: {len(ok) + len(reached)}/{len(results) - len(skipped)}**"
        " (200 ya da CAMARA kodlu hata; 401/403/timeout hariç)\n\n"
        "| API | Path | Sonuç | Yanıt alanları / hata | Not |\n|---|---|---|---|---|\n"
        + "\n".join(rows) + "\n\n"
        + ("## Temizlik\n" + "\n".join(f"- {c['kind']} `{c['id']}` → {'silindi' if c['deleted'] else 'SİLİNEMEDİ (' + c.get('error','?') + ')'}" for c in cleaned) + "\n" if cleaned else "")
        + "\n> Ham telefon numarası bu raporda yoktur (`_mask_deep`).\n",
        encoding="utf-8",
    )
    return j, md


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Nokia NaC canlı erişilebilirlik sondası")
    ap.add_argument("--mode", default="live", choices=["live", "simulator", "fixture"])
    ap.add_argument("--base", default=None, help="taban URL (varsayılan: moda göre)")
    ap.add_argument("--phone", default=None, help="sonda numarası (E.164)")
    ap.add_argument("--sink", default=None, help="abonelik webhook adresi (public HTTPS)")
    ap.add_argument("--include-write", action="store_true", help="abonelik/QoD gibi kayıt oluşturan uçları da dene")
    ap.add_argument("--only", default=None, help="virgülle ayrılmış sonda anahtarları (ör. number_verify,consent)")
    ap.add_argument("--patch", action="store_true", help="api-availability.md tablolarını güncelle")
    ap.add_argument("--dry-run", action="store_true", help="--patch ile: dosyaya yazma, ne yapacağını göster")
    ap.add_argument("--force", action="store_true",
                    help="canlı olmayan sonucu da proje tablolarına yaz (normalde engellenir)")
    ap.add_argument("--timeout", type=float, default=8.0)
    ap.add_argument("--retries", type=int, default=1)
    args = ap.parse_args()

    n = load_dotenv(ROOT / ".env") or load_dotenv(SHARED / ".env")
    if n:
        print(f".env → {n} değişken yüklendi")

    if args.mode == "live" and os.environ.get("NAC_OAUTH_TOKEN_URL") and not os.environ.get("NAC_OAUTH_TOKEN"):
        try:
            tok = fetch_oauth_token()
            if tok:
                os.environ["NAC_OAUTH_TOKEN"] = tok
                print("OAuth2 client-credentials token alındı")
        except Exception as e:
            print(f"UYARI: token alınamadı ({type(e).__name__}: {e}) — passthrough uçları 401 dönecek")

    base = args.base or (
        "http://127.0.0.1:8081" if args.mode == "simulator" else "https://network-as-code.p-eu.rapidapi.com"
    )
    cfg = NacConfig(
        mode=args.mode, base_url=base,
        rapidapi_key=os.environ.get("NAC_RAPIDAPI_KEY", ""),
        rapidapi_host=os.environ.get("NAC_RAPIDAPI_HOST", "network-as-code.nokia.rapidapi.com"),
        oauth_token=os.environ.get("NAC_OAUTH_TOKEN", ""),
        timeout_s=args.timeout, retries=args.retries,
        breaker_threshold=99,  # sonda: bir uçtaki hata diğerlerini kapatmasın
    )
    if args.mode == "live" and not cfg.rapidapi_key:
        print("ERROR: NAC_RAPIDAPI_KEY is not set. Create .env from .env.example, or rehearse with --mode simulator.", file=sys.stderr)
        return 2

    phone = args.phone or os.environ.get("NAC_PROBE_PHONE") or "+905551234567"
    sink = args.sink or (os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") + "/webhooks/probe" if os.environ.get("PUBLIC_BASE_URL") else "http://127.0.0.1:8000/webhooks/probe")
    ctx = ProbeCtx(phone=phone, sink=sink)
    only = set(args.only.split(",")) if args.only else None

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"\nNaC sondası — mod={args.mode} taban={base} numara={mask_phone(phone)}")
    print(f"{'-' * 78}")
    client = NacClient(cfg)
    results = run_probes(client, ctx, args.include_write, only)
    cleaned = cleanup(client, ctx) if ctx.created else []

    meta = {"started": started, "mode": args.mode, "base_url": base, "phone_masked": mask_phone(phone),
            "sink": sink, "has_key": bool(cfg.rapidapi_key), "has_token": bool(cfg.oauth_token),
            "include_write": args.include_write}
    j, md = write_report(results, meta, cleaned)
    ok = sum(1 for r in results if r.get("ok"))
    reached = sum(1 for r in results if not r.get("ok") and not r.get("skipped") and endpoint_reached(r))
    bad = sum(1 for r in results if not r.get("ok") and not r.get("skipped") and not endpoint_reached(r))
    sk = sum(1 for r in results if r.get("skipped"))
    print(f"{'-' * 78}")
    print(f"{ok} çalıştı · {reached} uç var (iş kuralı hatası) · {bad} erişilemedi · {sk} atlandı")
    print(f"rapor: {md.relative_to(SHARED)}\n       {j.relative_to(SHARED)}")

    if args.patch:
        stamp = datetime.now().strftime("%d.%m.%Y")
        # Güvenlik kilidi: canlı olmayan sonucun "SIMULATOR planında test" kolonuna yazılması
        # uydurma kanıt olur. Ana envanter tablosu yazılır (başlığında modu söylüyor), proje
        # tabloları --force olmadan dokunulmaz.
        table_dry = args.dry_run or (args.mode != "live" and not args.force)
        if args.mode != "live" and not args.force:
            print("\nKİLİT: mod canlı değil → proje tabloları DEĞİŞTİRİLMEDİ (uydurma kanıt yazmıyoruz).")
            print("       Yine de ne yazılacağını görmek için: --force ekle.")
        print("\napi-availability.md" + (" (yazılmıyor)" if table_dry else " güncelleniyor") + ":")
        for name in PROJECTS:
            r = patch_availability(ROOT / name, results, stamp, table_dry)
            if r.get("error"):
                print(f"  {name:<14} ✖ {r['error']}")
            else:
                verb = "satır hazır (yazılmadı)" if table_dry else "satır yazıldı"
                extra = f" · {len(r['skipped_rows'])} satır denenmedi" if r["skipped_rows"] else ""
                extra += f" · TABLODA EŞLEŞMEYEN: {', '.join(r['unmatched'])}" if r["unmatched"] else ""
                print(f"  {name:<14} kolon={r['col']} · {len(r['patched'])} {verb}{extra}")
        # Ana envanter her modda yazılır — başlığında hangi modda koştuğunu açıkça söylüyor.
        m = write_master_table(results, meta, stamp, args.dry_run)
        print(f"  ana envanter   {m.relative_to(SHARED)}" + (" (dry-run)" if args.dry_run else ""))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
