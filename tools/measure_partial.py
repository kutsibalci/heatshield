"""PARTIAL distribution measurement — how large is the positioning uncertainty relative to the site radius?

Mentor (9 September 2026): "If the uncertainty is comparable in size to 500 metres, PARTIAL becomes the
dominant answer. Measure it — it is a cheap experiment and gives you real data you can show in the demo."

Method: we take the device's OWN location returned by Location Retrieval as the centre and call
Location Verification with a growing radius. PARTIAL is expected while the uncertainty region extends
beyond the circle, and TRUE once it lies entirely inside. The radius at which it switches to TRUE gives
the size of the uncertainty.

Run (from the repo root):
    python tools/measure_partial.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from _common import evidence_file, require_live_flag
from _common import load_env as _load_env  # noqa: E402  (repo root + package path)
from nac_client import NacClient, NacConfig, NacError  # noqa: E402

RADII = [100, 250, 500, 750, 1000, 1500, 2000, 3000, 5000]
OUT = evidence_file("partial-scan")


def load_env() -> None:
    if _load_env() is None:
        print("No .env found — live mode needs one.")
        sys.exit(1)


def main() -> None:
    require_live_flag("Location Verification radius scan (PARTIAL rate) against the live Nokia sandbox.")
    load_env()
    phone = os.environ.get("NAC_PROBE_PHONE", "").strip()
    if not phone:
        print("NAC_PROBE_PHONE is not set.")
        sys.exit(1)

    os.environ["NAC_MODE"] = "live"
    c = NacClient(NacConfig.from_env())

    # 1) The device's own location + the uncertainty radius reported by the platform
    # maxAge is MANDATORY: a call without it returns 422 on the live platform (measured 9 September 2026).
    r = c.location_retrieve(phone, 3600)
    area = (r.data or {}).get("area") or {}
    ctr = area.get("center") or {}
    lat, lng = ctr.get("latitude"), ctr.get("longitude")
    reported = area.get("radius")
    print(f"Location Retrieval  -> centre ({lat}, {lng})  platform-reported radius: {reported} m")
    print(f"lastLocationTime    -> {(r.data or {}).get('lastLocationTime')}")
    if lat is None:
        print("No coordinate returned; nothing to measure.")
        sys.exit(1)

    print("\nSame centre, growing radius:")
    rows = []
    for radius in RADII:
        try:
            res = c.location_verify(phone, lat, lng, radius, 3600)      # loose freshness
            verdict = (res.data or {}).get("verificationResult")
            ms = res.latency_ms
            err = None
            try:                                                        # strict freshness: forces a fresh fix
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
        print(f"  r={radius:>5} m  ->  loose: {str(verdict or err):<10} {ms:>4} ms   |   strict: {str(strict_verdict):<10} {strict_ms:>4} ms")

    firsts = [r_["radius_m"] for r_ in rows if r_["loose"] == "TRUE"]
    partials = [r_["radius_m"] for r_ in rows if r_["loose"] == "PARTIAL"]
    print("\nSUMMARY")
    print(f"  radii returning PARTIAL : {partials or '-'}")
    print(f"  first TRUE               : {firsts[0] if firsts else 'none'} m")
    print(f"  platform-reported        : {reported} m")
    if firsts:
        print(f"  -> Uncertainty is of the order of {firsts[0]} m. With a 500 m site radius "
              f"{'PARTIAL dominates' if firsts[0] > 500 else 'TRUE/FALSE stay meaningful'}.")

    OUT.write_text(json.dumps({
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "center": {"latitude": lat, "longitude": lng},
        "platform_reported_radius_m": reported,
        "last_location_time": (r.data or {}).get("lastLocationTime"),
        "scan": rows,
        "first_true_radius_m": firsts[0] if firsts else None,
        "partial_radii_m": partials,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nsaved: {OUT}")


if __name__ == "__main__":
    main()
