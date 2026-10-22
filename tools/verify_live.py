"""verify_live.py — live availability probe for the six CAMARA APIs HeatShield calls.

Calls each endpoint once, records status, latency and the response shape, cleans up anything it
created, and writes a masked report to `evidence/probe-<timestamp>.json` and `.md`.

The 9 Sep 2026 probe in `evidence/` was run with the version of this tool at tag `v0.1-hackathon`.
That version also probed APIs the product never calls (KYC, SIM swap, number verification, …);
they have since been removed from the client, so this tool no longer probes them.

Usage
-----
    # rehearsal against the local simulator (no key, no network beyond localhost)
    python tools/verify_live.py --mode simulator --base http://127.0.0.1:8081

    # real calls against Nokia Network as Code — spends quota
    python tools/verify_live.py --mode live

    # also the endpoints that CREATE records (geofence subscription, QoD session); both are deleted again
    python tools/verify_live.py --mode live --include-write --sink https://<tunnel>/webhooks/probe

Environment (.env or shell):
    NAC_RAPIDAPI_KEY   RapidAPI key (required for live)
    NAC_PROBE_PHONE    probe number (default +99999000001000, a Nokia sandbox test device)
    PUBLIC_BASE_URL    public HTTPS base for the subscription sink

The raw number appears in no output (`_mask_deep`), and nothing is written for an endpoint that was not tried.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from _common import EVIDENCE, ROOT, load_env  # noqa: E402  (repo root + package path)
from nac_client import NacClient, NacConfig, NacError, mask_phone  # noqa: E402
from nac_client.client import P  # the verified path table  # noqa: E402

DEFAULT_PROBE_PHONE = "+99999000001000"


@dataclass
class Probe:
    key: str
    label: str
    paths: list[str]
    run: Callable[[NacClient, "ProbeCtx"], Any]
    write: bool = False  # creates a record (subscription / QoD session) → needs --include-write
    note: str = ""       # a known limitation in live use


@dataclass
class ProbeCtx:
    phone: str
    sink: str
    lat: float = 25.2854
    lng: float = 51.5310
    radius_m: float = 300.0
    created: list[tuple[str, str]] = field(default_factory=list)  # (kind, id) → cleanup


def _track(ctx: ProbeCtx, kind: str, res: Any) -> Any:
    """Take the id from a subscription/session response and add it to the cleanup list."""
    sid = (res.data or {}).get("subscriptionId") or (res.data or {}).get("sessionId") or (res.data or {}).get("id")
    if sid:
        ctx.created.append((kind, str(sid)))
    return res


PROBES: list[Probe] = [
    Probe("reachability", "Device Reachability Status", [P["reachability"]],
          lambda c, x: c.reachability(x.phone)),
    Probe("location_retrieve", "Location Retrieval", [P["location_retrieve"]],
          lambda c, x: c.location_retrieve(x.phone, 3600)),
    Probe("location_verify", "Location Verification", [P["location_verify"]],
          lambda c, x: c.location_verify(x.phone, x.lat, x.lng, x.radius_m, 3600)),
    Probe("congestion_query", "Congestion Insights", [P["congestion_query"]],
          lambda c, x: c.congestion_query(x.phone)),
    Probe("geofencing_subs", "Geofencing Subscriptions", [P["geofencing_subs"]],
          lambda c, x: _track(x, "geofencing", c.geofence_subscribe(x.phone, x.lat, x.lng, x.radius_m, x.sink)),
          write=True, note="Live: the sink must be public HTTPS (an ngrok/cloudflare tunnel)."),
    Probe("qod_sessions", "Quality on Demand", [P["qod_sessions"]],
          lambda c, x: _track(x, "qod", c.qod_create(x.phone, "203.0.113.10", "QOS_E", 600)),
          write=True, note="qosProfile names vary by operator; on a 400, check the profile list in the portal."),
    Probe("qod_retrieve", "Quality on Demand — session list", [P["qod_retrieve"]],
          lambda c, x: c.qod_list(x.phone)),
]

_PHONE_RE = re.compile(r"\+[0-9]{8,15}")


def _shape(data: Any) -> list[str]:
    """Which fields came back. Some CAMARA endpoints return an array; then there are no field names."""
    if isinstance(data, dict):
        return sorted(data.keys())
    if isinstance(data, list):
        inner = sorted({k for it in data if isinstance(it, dict) for k in it})
        return [f"array[{len(data)}]"] + inner
    if data is None or data == {}:
        return ["(empty body)"]
    return [type(data).__name__]


def _mask_deep(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _mask_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_deep(v) for v in value]
    if isinstance(value, str):
        return _PHONE_RE.sub(lambda m: mask_phone(m.group(0)), value)
    return value


# CAMARA error bodies carry codes (e.g. LOCATION_RETRIEVAL.UNABLE_TO_LOCATE). If we saw such a code, the
# endpoint REALLY EXISTS and speaks CAMARA — the test data just did not produce a positive result.
# DOMAIN.CODE is the most informative; at least 2 characters on each side, so short domains such as
# `QOD.SESSION_NOT_FOUND` match but "A.B" initials in plain text do not.
_DOTTED_RE = re.compile(r"\b([A-Z][A-Z0-9_]+\.[A-Z][A-Z0-9_]+)\b")
_BARE_RE = re.compile(r"\b([A-Z][A-Z0-9]{2,}(?:_[A-Z0-9]+)+)\b")  # BARE_CODE


def camara_code(rec: dict) -> str | None:
    """The CAMARA error code in the error body; `DOMAIN.CODE` is preferred over `CODE`."""
    if rec.get("ok") or rec.get("skipped"):
        return None
    raw = str(rec.get("message") or "")
    texts = [raw]
    i, j = raw.find("{"), raw.rfind("}")
    if 0 <= i < j:                                       # a JSON body: also scan its `code`/`message`
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
    """Does the endpoint exist? 200 or a CAMARA-coded business-rule error → yes.
    401/403, timeout, network or an open circuit → no information."""
    if rec.get("ok"):
        return True
    if rec.get("kind") in ("auth", "timeout", "network", "circuit_open", "probe_error"):
        return False
    return camara_code(rec) is not None


def run_probes(client: NacClient, ctx: ProbeCtx, include_write: bool, only: set[str] | None) -> list[dict]:
    out: list[dict] = []
    for pr in PROBES:
        if only and pr.key not in only:
            continue
        if pr.write and not include_write:
            out.append({"key": pr.key, "label": pr.label, "paths": pr.paths, "skipped": True,
                        "reason": "creates a record — tried only with --include-write", "note": pr.note})
            print(f"  ⏭  {pr.label:<40} skipped (needs --include-write)")
            continue
        try:
            res = pr.run(client, ctx)
            rec = {"key": pr.key, "label": pr.label, "paths": pr.paths, "ok": True, "status": 200,
                   "latency_ms": res.latency_ms, "source": res.source, "correlator": res.correlator,
                   "request": _mask_deep(res.request), "response_keys": _shape(res.data),
                   "response": _mask_deep(res.data), "note": pr.note}
            print(f"  ✅ {pr.label:<40} 200  {res.latency_ms:>5} ms  {res.source}")
        except NacError as e:
            rec = {"key": pr.key, "label": pr.label, "paths": pr.paths, "ok": False, **e.to_dict(), "note": pr.note}
            rec["camara_code"] = camara_code(rec)
            rec["endpoint_reached"] = endpoint_reached(rec)
            if rec["camara_code"]:
                print(f"  ◐  {pr.label:<40} endpoint exists, business-rule error: {e.status} {rec['camara_code']}")
            else:
                print(f"  ⚠  {pr.label:<40} {e.kind}{f' {e.status}' if e.status else ''}")
        except Exception as e:  # noqa: BLE001 — one broken probe must not stop the others
            rec = {"key": pr.key, "label": pr.label, "paths": pr.paths, "ok": False, "kind": "probe_error",
                   "message": f"{type(e).__name__}: {e}", "note": pr.note}
            print(f"  ✖  {pr.label:<40} probe error: {type(e).__name__}")
        out.append(rec)
    return out


def cleanup(client: NacClient, ctx: ProbeCtx) -> list[dict]:
    """Delete the subscriptions/sessions the probe created — leave nothing behind on the platform."""
    done = []
    for kind, sid in ctx.created:
        try:
            if kind == "geofencing":
                client.geofence_delete(sid)
            elif kind == "qod":
                client.qod_delete(sid)
            done.append({"kind": kind, "id": sid, "deleted": True})
            print(f"  🧹 {kind} {sid} deleted")
        except NacError as e:
            done.append({"kind": kind, "id": sid, "deleted": False, "error": e.kind})
            print(f"  ⚠  {kind} {sid} could not be deleted: {e.kind} — check the portal by hand")
    return done


def write_report(results: list[dict], meta: dict, cleaned: list[dict], out_dir: Path = EVIDENCE) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = meta["started"].replace(":", "").replace("-", "").replace("T", "-")[:15]
    j = out_dir / f"probe-{slug}.json"
    j.write_text(json.dumps({"meta": meta, "results": results, "cleanup": cleaned}, ensure_ascii=False, indent=2),
                 encoding="utf-8")

    ok = [r for r in results if r.get("ok")]
    reached = [r for r in results if not r.get("ok") and not r.get("skipped") and endpoint_reached(r)]
    bad = [r for r in results if not r.get("ok") and not r.get("skipped") and not endpoint_reached(r)]
    skipped = [r for r in results if r.get("skipped")]
    rows = []
    for r in results:
        if r.get("skipped"):
            state = "⏭ skipped"
        elif r.get("ok"):
            state = f"✅ 200 · {r['latency_ms']} ms"
        elif endpoint_reached(r):
            state = f"◐ endpoint exists · {r.get('status', '')} `{camara_code(r)}`"
        else:
            state = f"⚠ {r.get('kind')}" + (f" {r['status']}" if r.get("status") else "")
        keys = ", ".join(r.get("response_keys") or []) if r.get("ok") else (r.get("message", "") or "")[:80]
        rows.append(f"| {r['label']} | `{r['paths'][0]}` | {state} | {keys} | {r.get('note', '')} |")

    md = out_dir / f"probe-{slug}.md"
    md.write_text(
        f"# NaC live probe report — {meta['started'][:16]}\n\n"
        f"- Mode: **{meta['mode']}** · base: `{meta['base_url']}`\n"
        f"- Probe number: `{meta['phone_masked']}`\n"
        f"- RapidAPI key: {'present' if meta['has_key'] else 'MISSING'}\n"
        f"- Result: **{len(ok)} succeeded**, {len(reached)} endpoints exist (business-rule error), "
        f"{len(bad)} unreachable, {len(skipped)} skipped — {len(results)} in total\n\n"
        "| API | Path | Result | Response fields / error | Note |\n|---|---|---|---|---|\n"
        + "\n".join(rows) + "\n\n"
        + ("## Cleanup\n" + "\n".join(
            f"- {c['kind']} `{c['id']}` → {'deleted' if c['deleted'] else 'NOT DELETED (' + c.get('error', '?') + ')'}"
            for c in cleaned) + "\n" if cleaned else "")
        + "\n> No raw phone number appears in this report (`_mask_deep`).\n",
        encoding="utf-8",
    )
    return j, md


def main() -> int:
    ap = argparse.ArgumentParser(description="Live availability probe for the six CAMARA APIs HeatShield calls")
    ap.add_argument("--mode", required=True, choices=["live", "simulator", "fixture"],
                    help="required: live makes real calls with the real key and spends quota")
    ap.add_argument("--base", default=None, help="base URL (default depends on the mode)")
    ap.add_argument("--phone", default=None, help="probe number (E.164)")
    ap.add_argument("--sink", default=None, help="subscription webhook address (public HTTPS)")
    ap.add_argument("--include-write", action="store_true", help="also try endpoints that create records (subscription, QoD)")
    ap.add_argument("--only", default=None, help="comma-separated probe keys, e.g. location_verify,reachability")
    ap.add_argument("--timeout", type=float, default=8.0)
    ap.add_argument("--retries", type=int, default=1)
    args = ap.parse_args()

    env = load_env()
    if env:
        print(f".env loaded from {env}")
    base = args.base or ("http://127.0.0.1:8081" if args.mode == "simulator" else "https://network-as-code.p-eu.rapidapi.com")
    cfg = NacConfig(
        mode=args.mode, base_url=base,
        rapidapi_key=os.environ.get("NAC_RAPIDAPI_KEY", ""),
        rapidapi_host=os.environ.get("NAC_RAPIDAPI_HOST", "network-as-code.nokia.rapidapi.com"),
        timeout_s=args.timeout, retries=args.retries,
        breaker_threshold=99,  # a probe: one failing endpoint must not shut off the others
    )
    if args.mode == "live" and not cfg.rapidapi_key:
        print("ERROR: NAC_RAPIDAPI_KEY is not set. Create .env from .env.example, or rehearse with --mode simulator.",
              file=sys.stderr)
        return 2

    phone = args.phone or os.environ.get("NAC_PROBE_PHONE") or DEFAULT_PROBE_PHONE
    public = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    sink = args.sink or (f"{public}/webhooks/probe" if public else "http://127.0.0.1:8000/webhooks/probe")
    ctx = ProbeCtx(phone=phone, sink=sink)
    only = set(args.only.split(",")) if args.only else None

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"\nNaC probe — mode={args.mode} base={base} number={mask_phone(phone)}")
    print("-" * 78)
    client = NacClient(cfg)
    results = run_probes(client, ctx, args.include_write, only)
    cleaned = cleanup(client, ctx) if ctx.created else []

    meta = {"started": started, "mode": args.mode, "base_url": base, "phone_masked": mask_phone(phone),
            "sink": sink, "has_key": bool(cfg.rapidapi_key), "include_write": args.include_write}
    j, md = write_report(results, meta, cleaned)
    ok = sum(1 for r in results if r.get("ok"))
    reached = sum(1 for r in results if not r.get("ok") and not r.get("skipped") and endpoint_reached(r))
    bad = sum(1 for r in results if not r.get("ok") and not r.get("skipped") and not endpoint_reached(r))
    skipped = sum(1 for r in results if r.get("skipped"))
    print("-" * 78)
    print(f"{ok} succeeded · {reached} endpoints exist (business-rule error) · {bad} unreachable · {skipped} skipped")
    print(f"report: {md.relative_to(ROOT)}\n        {j.relative_to(ROOT)}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
