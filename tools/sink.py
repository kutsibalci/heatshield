"""Minimal webhook receiver for the live subscription trial.

When opening a subscription with Nokia we have to provide a public HTTPS sink address. This server
accepts every path and every method, and writes each incoming request both to the screen and to the
`sink-log.jsonl` file. The goal is to prove two things:
  1. Whether a subscription can be created on the real platform (whether the sink address is accepted),
  2. If Nokia sends any notification, exactly what its body looks like.

Uses only the stdlib — no dependencies, conflicts with nothing.

Run:          python tools/sink.py 8020
Then:         ngrok http 8020
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG = Path(__file__).resolve().parent / "sink-log.jsonl"
_PHONE_KEYS = ("phoneNumber", "phone", "msisdn")


def _mask(value):
    """We never write the raw number anywhere — the log file included."""
    if isinstance(value, dict):
        return {k: ("+" + "*" * 8 + str(v)[-4:] if k in _PHONE_KEYS and isinstance(v, str) else _mask(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v) for v in value]
    return value


class Sink(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _handle(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except Exception:
            body = raw.decode("utf-8", "replace")[:2000]

        entry = {
            "t": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "method": method,
            "path": self.path,
            # We do NOT RECORD the Authorization header; only whether it is present.
            "has_authorization": "Authorization" in self.headers,
            "content_type": self.headers.get("Content-Type"),
            "body": _mask(body),
        }
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        etype = (body or {}).get("type") if isinstance(body, dict) else None
        print(f"[{entry['t']}] {method} {self.path}" + (f"  type={etype}" if etype else ""), flush=True)
        if isinstance(body, dict):
            print("   " + json.dumps(_mask(body), ensure_ascii=False)[:600], flush=True)

        # We return 204 to everything, including the CloudEvents validation request (abort/validation):
        # returning 4xx to an unknown type could lead the operator to drop the subscription.
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):  # noqa: N802
        self._handle("POST")

    def do_GET(self):  # noqa: N802
        self._handle("GET")

    def do_PUT(self):  # noqa: N802
        self._handle("PUT")

    def log_message(self, *_args):
        pass          # we use our own output


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8020
    print(f"Sink listening: http://127.0.0.1:{port}  (any path, any method → 204)")
    print(f"Log file: {LOG}")
    print("Now, in a separate terminal:  ngrok http", port)
    ThreadingHTTPServer(("127.0.0.1", port), Sink).serve_forever()


if __name__ == "__main__":
    main()
