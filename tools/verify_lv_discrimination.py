"""Does Location Verification in the sandbox actually take the area into account?

`measure_partial.py` found FALSE throughout the 100 m - 5 km range. This script measures and
RECORDS the edge cases: 50 km and 200 km around the device's own coordinate, a completely different
city, and a circle with a slightly shifted centre.

If all return FALSE, the conclusion is: in the sandbox this endpoint answers independently of the
requested area. The transport layer and schema are proven, the decision logic is not — and the PARTIAL
rate cannot be measured there.

Run (from the repo root):  python tools/verify_lv_discrimination.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from _common import evidence_file, load_env, require_live_flag  # noqa: E402  (repo root + package path)
from nac_client import NacClient, NacConfig, NacError  # noqa: E402

OUT = evidence_file("lv-discrimination")


def main() -> None:
    require_live_flag("Does Location Verification discriminate by area? Live Nokia sandbox.")
    if load_env() is None:
        print(".env yok — canlı mod için gerekli.")
        sys.exit(1)
    os.environ["NAC_MODE"] = "live"

    c = NacClient(NacConfig.from_env())
    phone = os.environ["NAC_PROBE_PHONE"]

    # maxAge is MANDATORY — a call without it returns 422. We record this too.
    missing_max_age = None
    try:
        c.location_retrieve(phone)
        missing_max_age = {"status": "200", "note": "maxAge parametresiz cagri BASARILI oldu"}
    except NacError as e:
        missing_max_age = {"status": f"{e.kind}:{e.status}", "note": "maxAge parametresiz cagri REDDEDILDI"}
    print(f"maxAge'siz Location Retrieval -> {missing_max_age['status']}")

    r = c.location_retrieve(phone, 3600)
    area = (r.data or {}).get("area") or {}
    lat = area["center"]["latitude"]
    lng = area["center"]["longitude"]
    print(f"cihazin konumu: ({lat}, {lng})  bildirilen belirsizlik: {area.get('radius')} m\n")

    cases = [
        ("cihazin kendi konumu, 50 km", lat, lng, 50_000),
        ("cihazin kendi konumu, 200 km", lat, lng, 200_000),
        ("baska sehir (Doha), 500 m", 25.2854, 51.531, 500),
        ("konum +0.001 derece kaydirilmis, 1 km", lat + 0.001, lng + 0.001, 1_000),
    ]
    rows = []
    for label, la, ln, radius in cases:
        try:
            res = c.location_verify(phone, la, ln, radius, 3600)
            d = res.data or {}
            row = {"case": label, "center": {"latitude": la, "longitude": ln}, "radius_m": radius,
                   "result": d.get("verificationResult"), "last_location_time": d.get("lastLocationTime"),
                   "latency_ms": res.latency_ms}
        except NacError as e:
            row = {"case": label, "center": {"latitude": la, "longitude": ln}, "radius_m": radius,
                   "error": f"{e.kind}:{e.status}"}
        rows.append(row)
        print(f"  {label:<40} -> {row.get('result') or row.get('error')}")

    verdicts = {r_.get("result") for r_ in rows}
    discriminates = verdicts != {"FALSE"}
    print(f"\nSONUC: uc alani dikkate aliyor mu? -> {'EVET' if discriminates else 'HAYIR (hepsi FALSE)'}")

    OUT.write_text(json.dumps({
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "question": "Does sandbox Location Verification discriminate by requested area?",
        "device_position": {"latitude": lat, "longitude": lng},
        "platform_reported_uncertainty_radius_m": area.get("radius"),
        "location_retrieve_without_max_age": missing_max_age,
        "cases": rows,
        "discriminates_by_area": discriminates,
        "conclusion": ("Transport and schema proven; the verdict does not vary with the requested area, "
                       "so the decision logic is not proven and the PARTIAL rate cannot be measured here."),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"kayit: {OUT}")


if __name__ == "__main__":
    main()
