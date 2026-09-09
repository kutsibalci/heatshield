"""PARTIAL dağılımı ölçümü — konumlandırma belirsizliği saha yarıçapına göre ne kadar büyük?

Mentör (9 Eylül 2026): "Belirsizlik 500 metreyle kıyaslanabilir büyüklükteyse PARTIAL baskın
cevap hâline gelir. Ölçün — ucuz bir deney ve size demoda gösterebileceğiniz gerçek veri verir."

Yöntem: cihazın Location Retrieval'dan dönen KENDİ konumunu merkez alıp yarıçapı büyüterek
Location Verification çağırırız. Belirsizlik bölgesi daireyi aşarken PARTIAL, tamamen içine
girdiğinde TRUE beklenir. TRUE'ya geçtiği yarıçap, belirsizliğin büyüklüğünü verir.

Çalıştırma (depo kökünden):
    python _ortak/tools/measure_partial.py
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

RADII = [100, 250, 500, 750, 1000, 1500, 2000, 3000, 5000]
OUT = Path(__file__).resolve().parent / f"partial-scan-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json"


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        print(".env yok — canlı mod icin gerekli.")
        sys.exit(1)
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def main() -> None:
    load_env()
    phone = os.environ.get("NAC_PROBE_PHONE", "").strip()
    if not phone:
        print("NAC_PROBE_PHONE tanimli degil.")
        sys.exit(1)

    os.environ["NAC_MODE"] = "live"
    c = NacClient(NacConfig.from_env())

    # 1) Cihazin kendi konumu + platformun bildirdigi belirsizlik yaricapi
    # maxAge ZORUNLU: parametresiz cagri canli platformda 422 donuyor (9 Eylul 2026 olcumu).
    r = c.location_retrieve(phone, 3600)
    area = (r.data or {}).get("area") or {}
    ctr = area.get("center") or {}
    lat, lng = ctr.get("latitude"), ctr.get("longitude")
    reported = area.get("radius")
    print(f"Location Retrieval  -> merkez ({lat}, {lng})  platformun bildirdigi yaricap: {reported} m")
    print(f"lastLocationTime    -> {(r.data or {}).get('lastLocationTime')}")
    if lat is None:
        print("Koordinat donmedi, olcum yapilamaz.")
        sys.exit(1)

    print("\nAyni merkez, buyuyen yaricap:")
    rows = []
    for radius in RADII:
        try:
            res = c.location_verify(phone, lat, lng, radius, 3600)      # gevsek tazelik
            verdict = (res.data or {}).get("verificationResult")
            ms = res.latency_ms
            err = None
            try:                                                        # siki tazelik: taze fix zorlar
                strict = c.location_verify(phone, lat, lng, radius, 60)
                strict_verdict = (strict.data or {}).get("verificationResult")
                strict_ms = strict.latency_ms
            except NacError as e2:
                strict_verdict, strict_ms = f"{e2.kind}:{e2.status}", 0
        except NacError as e:
            verdict, ms, err = None, 0, f"{e.kind}:{e.status}"
        if err:
            strict_verdict, strict_ms = None, 0
        rows.append({"radius_m": radius, "loose": verdict, "loose_ms": ms,
                     "strict": strict_verdict, "strict_ms": strict_ms, "error": err})
        print(f"  r={radius:>5} m  ->  gevsek: {str(verdict or err):<10} {ms:>4} ms   |   siki: {str(strict_verdict):<10} {strict_ms:>4} ms")

    firsts = [r_["radius_m"] for r_ in rows if r_["loose"] == "TRUE"]
    partials = [r_["radius_m"] for r_ in rows if r_["loose"] == "PARTIAL"]
    print("\nOZET")
    print(f"  PARTIAL donen yaricaplar : {partials or '-'}")
    print(f"  Ilk TRUE                 : {firsts[0] if firsts else 'hicbiri'} m")
    print(f"  Platformun bildirdigi    : {reported} m")
    if firsts:
        print(f"  -> Belirsizlik ~{firsts[0]} m mertebesinde. Saha yaricapi 500 m ise "
              f"{'PARTIAL baskin olur' if firsts[0] > 500 else 'TRUE/FALSE anlamli kalir'}.")

    OUT.write_text(json.dumps({
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "center": {"latitude": lat, "longitude": lng},
        "platform_reported_radius_m": reported,
        "last_location_time": (r.data or {}).get("lastLocationTime"),
        "scan": rows,
        "first_true_radius_m": firsts[0] if firsts else None,
        "partial_radii_m": partials,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nkayit: {OUT}")


if __name__ == "__main__":
    main()
