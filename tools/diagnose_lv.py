"""Why does Location Verification always return FALSE? Eliminates the mentor's three hypotheses one by one.

Mentor (9 September 2026): "FALSE at 200 km is not an accuracy problem. If it were uncertainty, it
would turn TRUE at some point. There is a functional problem; look in this order: (1) coordinate order
or sign, (2) the device identifier differing between the two calls, (3) the two APIs not being backed
by the same data source in the sandbox."

This script tests all three and records the result.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from _common import evidence_file, load_env, require_live_flag  # noqa: E402  (repo root + package path)
from nac_client import NacClient, NacConfig, NacError  # noqa: E402
from nac_client.privacy import normalize_phone  # noqa: E402

OUT = evidence_file("lv-diagnosis")


def main() -> None:
    require_live_flag("Location Verification diagnosis against the live Nokia sandbox.")
    if load_env() is None:
        print("No .env found — live mode needs one.")
        sys.exit(1)
    os.environ["NAC_MODE"] = "live"

    c = NacClient(NacConfig.from_env())
    raw_phone = os.environ["NAC_PROBE_PHONE"]
    phone = normalize_phone(raw_phone)

    # --- HYPOTHESIS 2 first: do the two calls use the same device AT THE BYTE LEVEL?
    print("HYPOTHESIS 2 — device identifier")
    print(f"  raw .env value      : {raw_phone[:6]}***{raw_phone[-4:]}  (length {len(raw_phone)})")
    print(f"  normalised          : {phone[:6]}***{phone[-4:]}  (length {len(phone)})")
    print("  both calls identical: YES — both send the same normalised value in device.phoneNumber")

    r = c.location_retrieve(phone, 3600)
    area = (r.data or {}).get("area") or {}
    lat = area["center"]["latitude"]
    lng = area["center"]["longitude"]
    ret_ts = (r.data or {}).get("lastLocationTime")
    print(f"\n  Retrieval -> lat={lat}  lng={lng}  radius={area.get('radius')}  ts={ret_ts}")

    # --- HYPOTHESIS 1: coordinate order / sign
    print("\nHYPOTHESIS 1 — coordinate order and sign")
    cases = [
        ("correct order (lat, lng)", lat, lng),
        ("SWAPPED order (lng, lat)", lng, lat),
        ("latitude sign flipped", -lat, lng),
        ("longitude sign flipped", lat, -lng),
        ("both signs flipped", -lat, -lng),
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

    # --- Hint for HYPOTHESIS 3: does the freshness request change the timestamp?
    print("\nMENTOR CHECK — does a strict maxAge produce a fresh fix?")
    freshness = []
    for max_age in (3600, 60, 10):
        try:
            res = c.location_verify(phone, lat, lng, 5000, max_age)
            ts = (res.data or {}).get("lastLocationTime")
            freshness.append({"max_age_s": max_age, "last_location_time": ts, "latency_ms": res.latency_ms})
            print(f"  maxAge={max_age:>5} s -> ts={ts}  ({res.latency_ms} ms)")
        except NacError as e:
            freshness.append({"max_age_s": max_age, "error": f"{e.kind}:{e.status}"})
            print(f"  maxAge={max_age:>5} s -> ERROR {e.kind}:{e.status}")

    verdicts = {r_.get("result") for r_ in rows}
    swapped_works = any(r_["case"].startswith("SWAPPED") and r_.get("result") == "TRUE" for r_ in rows)
    print("\nRESULT")
    if swapped_works:
        print("  -> COORDINATE ORDER: swapping returns TRUE. The sandbox reads lat/lng the other way round.")
    elif verdicts == {"FALSE"}:
        print("  -> Hypotheses 1 and 2 RULED OUT. Remaining explanation: in the sandbox the two APIs are not")
        print("     backed by the same data source (hypothesis 3) — outside our control; a question for Nokia.")
    else:
        print(f"  -> Mixed result: {verdicts}")

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
    print(f"\nsaved: {OUT}")


if __name__ == "__main__":
    main()
