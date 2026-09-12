"""Canlı abonelik denemesi için minimal webhook alıcısı.

Nokia'ya abonelik açarken halka açık bir HTTPS sink adresi vermek zorundayız. Bu sunucu her
yolu ve her metodu kabul eder, gelen her isteği hem ekrana hem `sink-log.jsonl` dosyasına yazar.
Amaç iki şeyi kanıtlamak:
  1. Abonelik gerçek platformda oluşturulabiliyor mu (sink adresi kabul ediliyor mu),
  2. Nokia herhangi bir bildirim gönderiyorsa gövdesi tam olarak neye benziyor.

Sadece stdlib kullanır — bağımlılık yok, hiçbir şeyle çakışmaz.

Çalıştırma:   python tools/sink.py 8020
Sonra:        ngrok http 8020
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
    """Ham numarayı hiçbir yere yazmayız — log dosyası da dahil."""
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
            # Authorization başlığını KAYDETMEYİZ; yalnızca var mı yok mu.
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

        # CloudEvents doğrulama isteği (abort/validation) da dahil her şeye 204 döneriz:
        # bilinmeyen bir tipe 4xx dönmek operatörün aboneliği düşürmesine yol açabilir.
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
        pass          # kendi çıktımızı kullanıyoruz


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8020
    print(f"Sink dinliyor: http://127.0.0.1:{port}  (her yol, her metod → 204)")
    print(f"Kayit dosyasi: {LOG}")
    print("Simdi ayri bir terminalde:  ngrok http", port)
    ThreadingHTTPServer(("127.0.0.1", port), Sink).serve_forever()


if __name__ == "__main__":
    main()
