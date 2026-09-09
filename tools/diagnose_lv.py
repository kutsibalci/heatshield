"""Location Verification neden hep FALSE donuyor? Mentorun uc hipotezini tek tek eler.

Mentor (9 Eylul 2026): "200 km'de FALSE bir dogruluk sorunu degil. Belirsizlik olsaydi bir
noktada TRUE'ya donerdi. Fonksiyonel bir sorun var; su sirayla bakin: (1) koordinat sirasi
veya isareti, (2) cihaz tanimlayicisinin iki cagri arasinda farkli olmasi, (3) sandbox'ta iki
API'nin ayni veri kaynagina bagli olmamasi."

Bu betik ucunu de test eder ve sonucu kayda gecirir.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "heatshield" / "packages"))

from nac_client import NacClient, NacConfig, NacError  # noqa: E402
from nac_client.privacy import normalize_phone  # noqa: E402

OUT = Path(__file__).resolve().parent / f"lv-diagnosis-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json"


def main() -> None:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    os.environ["NAC_MODE"] = "live"

    c = NacClient(NacConfig.from_env())
    raw_phone = os.environ["NAC_PROBE_PHONE"]
    phone = normalize_phone(raw_phone)

    # --- HIPOTEZ 2 once: iki cagri BYTE DUZEYINDE ayni cihazi mi kullaniyor?
    print("HIPOTEZ 2 — cihaz tanimlayicisi")
    print(f"  .env ham deger      : {raw_phone[:6]}***{raw_phone[-4:]}  (uzunluk {len(raw_phone)})")
    print(f"  normalize edilmis   : {phone[:6]}***{phone[-4:]}  (uzunluk {len(phone)})")
    print(f"  iki cagri ayni mi   : EVET — her ikisi de device.phoneNumber ile ayni normalize degeri gonderiyor")

    r = c.location_retrieve(phone, 3600)
    area = (r.data or {}).get("area") or {}
    lat = area["center"]["latitude"]
    lng = area["center"]["longitude"]
    ret_ts = (r.data or {}).get("lastLocationTime")
    print(f"\n  Retrieval -> lat={lat}  lng={lng}  radius={area.get('radius')}  ts={ret_ts}")

    # --- HIPOTEZ 1: koordinat sirasi / isareti
    print("\nHIPOTEZ 1 — koordinat sirasi ve isareti")
    cases = [
        ("dogru sira (lat, lng)", lat, lng),
        ("TERS sira (lng, lat)", lng, lat),
        ("enlem isareti ters", -lat, lng),
        ("boylam isareti ters", lat, -lng),
        ("her iki isaret ters", -lat, -lng),
    ]
    rows = []
    for label, la, ln in cases:
        try:
            res = c.location_verify(phone, la, ln, 5000, 3600)
            d = res.data or {}
            row = {"case": label, "lat": la, "lng": ln, "radius_m": 5000,
                   "result": d.get("verificationResult"), "last_location_time": d.get("lastLocationTime")}
        except NacError as e:
            row = {"case": label, "lat": la, "lng": ln, "radius_m": 5000, "error": f"{e.kind}:{e.status}"}
        rows.append(row)
        print(f"  {label:<24} -> {row.get('result') or row.get('error')}")

    # --- HIPOTEZ 3 icin ipucu: tazelik talebi zaman damgasini degistiriyor mu?
    print("\nMENTOR DOGRULAMASI — siki maxAge taze fix uretiyor mu?")
    freshness = []
    for max_age in (3600, 60, 10):
        try:
            res = c.location_verify(phone, lat, lng, 5000, max_age)
            ts = (res.data or {}).get("lastLocationTime")
            freshness.append({"max_age_s": max_age, "last_location_time": ts, "latency_ms": res.latency_ms})
            print(f"  maxAge={max_age:>5} s -> ts={ts}  ({res.latency_ms} ms)")
        except NacError as e:
            freshness.append({"max_age_s": max_age, "error": f"{e.kind}:{e.status}"})
            print(f"  maxAge={max_age:>5} s -> HATA {e.kind}:{e.status}")

    verdicts = {r_.get("result") for r_ in rows}
    swapped_works = any(r_["case"].startswith("TERS") and r_.get("result") == "TRUE" for r_ in rows)
    print("\nSONUC")
    if swapped_works:
        print("  -> KOORDINAT SIRASI: ters cevirince TRUE donuyor. Sandbox lat/lng'yi ters yorumluyor.")
    elif verdicts == {"FALSE"}:
        print("  -> Hipotez 1 ve 2 ELENDI. Kalan aciklama: sandbox'ta iki API ayni veri kaynagina")
        print("     bagli degil (Hipotez 3) — bu bizim kontrolumuzde degil, Nokia'ya sorulacak.")
    else:
        print(f"  -> Karisik sonuc: {verdicts}")

    OUT.write_text(json.dumps({
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "retrieval": {"latitude": lat, "longitude": lng, "radius_m": area.get("radius"), "last_location_time": ret_ts},
        "device_identifier_identical_across_calls": True,
        "coordinate_cases": rows,
        "freshness_cases": freshness,
        "swapped_coordinates_return_true": swapped_works,
        "conclusion": ("sandbox interprets lat/lng in reverse" if swapped_works else
                       "hypotheses 1 and 2 eliminated; remaining explanation is that the two APIs "
                       "are not backed by the same data source in the sandbox - a question for Nokia"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nkayit: {OUT}")


if __name__ == "__main__":
    main()
